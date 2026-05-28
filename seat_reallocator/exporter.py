from pathlib import Path

import pandas as pd

SKIP_SHEETS = {'COLLATERALE'}


def _safe(val):
    """Return val unchanged; NaN → empty string."""
    if pd.isna(val):
        return ''
    return val


def pivot_order(group: pd.DataFrame) -> dict:
    """
    Convert all rows for one order into a single wide dict.

    Columns named vecchio_posto_NN / nuovo_posto_NN are seat-level.
    SPOSTATO seats use Nuovo posto as the new seat number; all others keep Posto.
    """
    group = group.sort_values('Posto').reset_index(drop=True)
    first = group.iloc[0]

    row: dict = {
        'ordine':  _safe(first.get('Codice ordine', '')),
        'cognome': _safe(first.get('Cognome', '')),
        'nome':    _safe(first.get('Nome', '')),
        'email':   _safe(first.get('Email', '')),
    }

    for i, (_, tix) in enumerate(group.iterrows(), start=1):
        n = f'{i:02d}'
        moved      = str(tix.get('Stato', '')).strip().upper() == 'SPOSTATO'
        nuovo_posto = tix.get('Nuovo posto') if moved else tix.get('Posto')

        row[f'cognome_{n}']         = _safe(tix.get('Cognome partecipante', ''))
        row[f'nome_{n}']            = _safe(tix.get('Nome partecipante', ''))
        row[f'barcode_{n}']         = _safe(tix.get('Codice supporto', ''))
        row[f'vecchio_settore_{n}'] = _safe(tix.get('Settore prezzi', ''))
        row[f'vecchio_blocco_{n}']  = _safe(tix.get('Settore', ''))
        row[f'vecchia_fila_{n}']    = _safe(tix.get('Fila', ''))
        row[f'vecchio_posto_{n}']   = _safe(tix.get('Posto', ''))
        row[f'nuovo_settore_{n}']   = _safe(tix.get('Settore prezzi', ''))
        row[f'nuovo_blocco_{n}']    = _safe(tix.get('Settore', ''))
        row[f'nuova_fila_{n}']      = _safe(tix.get('Fila', ''))
        row[f'nuovo_posto_{n}']     = _safe(nuovo_posto)

    return row


def process_sheet(df: pd.DataFrame) -> dict:
    """
    Process one event sheet and return per-ticket-count wide DataFrames.

    Only includes orders that have at least one SPOSTATO seat.

    Returns {ticket_count: DataFrame of wide rows}.
    """
    moved_orders = df.loc[
        df['Stato'].str.strip().str.upper() == 'SPOSTATO', 'Codice ordine'
    ].unique()

    if len(moved_orders) == 0:
        return {}

    relevant = df[df['Codice ordine'].isin(moved_orders)]
    relevant = relevant[~relevant['Stato posto'].str.strip().str.upper().isin({'CANCELLED'})]
    rows = [pivot_order(grp) for _, grp in relevant.groupby('Codice ordine', sort=False)]

    buckets: dict = {}
    for row in rows:
        n_tix = sum(1 for k in row if k.startswith('vecchio_posto_'))
        buckets.setdefault(n_tix, []).append(row)

    return {n: pd.DataFrame(rows_list) for n, rows_list in buckets.items()}


def export_swap_files(input_path: Path, output_dir: Path) -> list:
    """
    Transform report_annotated.xlsx into per-event, per-ticket-count swap files.

    Each output file contains one row per moved order, with all seat-level
    fields expanded horizontally (vecchio_posto_01, nuovo_posto_01, …).

    Returns the list of output file paths written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    xl = pd.ExcelFile(input_path)
    files_written: list = []

    for sheet in xl.sheet_names:
        if sheet.strip().upper() in SKIP_SHEETS:
            continue

        df = xl.parse(sheet, dtype=str)
        buckets = process_sheet(df)

        if not buckets:
            print(f'  [{sheet}] no reallocated orders — skipped')
            continue

        for n_tix, wide_df in sorted(buckets.items()):
            label    = f'{n_tix}_ticket' + ('s' if n_tix != 1 else '')
            fname    = f'{sheet}_{label}.xlsx'
            out_path = output_dir / fname
            wide_df.to_excel(out_path, index=False)
            files_written.append(out_path)
            print(f'  [{sheet}] {n_tix} ticket(s): {len(wide_df)} orders -> {out_path}')

    if files_written:
        print(f'\nDone. {len(files_written)} file(s) written to {output_dir}/')
    else:
        print('\nNo output files written.')

    return files_written
