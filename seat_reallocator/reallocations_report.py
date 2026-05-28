import argparse

import pandas as pd

_DEFAULT_OUT = 'data/swap_report.xlsx'
_PARTICIPANT_COLS = ['Nome partecipante', 'Cognome partecipante']


def _sheet_name(name: str) -> str:
    return str(name).replace(':', '.').replace('/', '-')[:31]


def _get(row, col: str):
    if row is None or col not in row.index:
        return ''
    v = row[col]
    return '' if pd.isna(v) else v


def build(source_path: str, out_path: str = _DEFAULT_OUT) -> None:
    sheets = pd.read_excel(
        source_path,
        sheet_name=None,
        dtype={'Codice ordine': str, 'Data evento': str},
    )

    with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
        for raw_name, df in sheets.items():
            if 'Stato' not in df.columns or 'Nuovo posto' not in df.columns:
                continue

            moved = df[df['Stato'] == 'SPOSTATO'].copy()
            if moved.empty:
                continue

            moved['_posto_num'] = pd.to_numeric(moved['Posto'], errors='coerce')
            moved['_nuovo_num'] = pd.to_numeric(moved['Nuovo posto'], errors='coerce')

            has_cross_row = (
                'Nuova fila' in moved.columns
                and moved['Nuova fila'].notna().any()
            )
            moved['_nuova_fila'] = (
                moved['Nuova fila'].fillna(moved['Fila']) if has_cross_row
                else moved['Fila']
            )

            # (Settore, Fila, posto_num) → row  for old positions
            old_lookup: dict = {}
            for _, row in moved.iterrows():
                key = (row['Settore'], row['Fila'], row['_posto_num'])
                old_lookup[key] = row

            # (Settore, fila, posto_num) → row  for new positions
            new_lookup: dict = {}
            for _, row in moved.iterrows():
                key = (row['Settore'], row['_nuova_fila'], row['_nuovo_num'])
                new_lookup[key] = row

            all_seats = sorted(set(old_lookup) | set(new_lookup))

            records = []
            for (settore, fila, posto) in all_seats:
                old_row = old_lookup.get((settore, fila, posto))
                new_row = new_lookup.get((settore, fila, posto))
                records.append({
                    'Settore': settore,
                    'Fila': fila,
                    'Posto': int(posto),
                    'Vecchio ordine': _get(old_row, 'Codice ordine'),
                    'Vecchio nome partecipante': _get(old_row, 'Nome partecipante'),
                    'Vecchio cognome partecipante': _get(old_row, 'Cognome partecipante'),
                    'Nuovo ordine': _get(new_row, 'Codice ordine'),
                    'Nuovo nome partecipante': _get(new_row, 'Nome partecipante'),
                    'Nuovo cognome partecipante': _get(new_row, 'Cognome partecipante'),
                })

            pd.DataFrame(records).to_excel(
                writer, sheet_name=_sheet_name(raw_name), index=False
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Build per-seat swap report from a reallocate output file.'
    )
    parser.add_argument('source', help='Path to annotated xlsx (reallocate or capofila output)')
    parser.add_argument('--out', default=_DEFAULT_OUT, help=f'Output path (default: {_DEFAULT_OUT})')
    args = parser.parse_args()
    build(args.source, args.out)
    print(f'Swap report written to {args.out}')
