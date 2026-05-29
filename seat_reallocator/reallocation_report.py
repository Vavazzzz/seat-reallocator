from pathlib import Path

import pandas as pd

_SKIP_SHEETS = {'COLLATERALE'}
_SKIP_STATI  = {'NON COINVOLTO'}

REPORT_COLS = [
    'Codice ordine',
    'Cognome',
    'Nome',
    'Settore',
    'Fila',
    'Posto',
    'Nuovo posto',
    'Stato',
    'Settore prezzi',
    'Nome partecipante',
    'Cognome partecipante',
]


def build_reallocation_report(input_path: Path, output_path: Path) -> int:
    """
    Build a flat per-seat reallocation report from an annotated xlsx.

    Reads all event sheets (skips COLLATERALE), drops NON COINVOLTO rows,
    and writes the REPORT_COLS subset to output_path.

    Returns the number of rows written.
    """
    xl = pd.ExcelFile(input_path)
    frames = []

    for sheet in xl.sheet_names:
        if sheet.strip().upper() in _SKIP_SHEETS:
            continue
        df = xl.parse(sheet, dtype={'Codice ordine': str})
        frames.append(df)

    if not frames:
        print('No event sheets found.')
        return 0

    big_df = pd.concat(frames, ignore_index=True)

    if 'Stato' in big_df.columns:
        big_df = big_df[~big_df['Stato'].str.strip().str.upper().isin(_SKIP_STATI)]

    missing = [c for c in REPORT_COLS if c not in big_df.columns]
    if missing:
        print(f'Warning: columns not present in input and will be empty: {missing}')
        for c in missing:
            big_df[c] = ''

    report = big_df[REPORT_COLS].copy()
    report = report.sort_values(
        ['Settore', 'Fila', 'Posto'],
        key=lambda s: pd.to_numeric(s, errors='coerce').fillna(s) if s.name in ('Fila', 'Posto') else s,
    ).reset_index(drop=True)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report.to_excel(output_path, index=False)

    return len(report)
