"""
Run the full reallocation pipeline and compare its output against a reference
reallocation.xlsx, then discard the temporary output.

Usage:
    python test_reallocation.py <report_csv> [<reference_reallocation.xlsx>]

Defaults:
    reference = data/reallocation.xlsx

Example:
    python test_reallocation.py data/report.csv
    python test_reallocation.py data/report.csv data/reallocation.xlsx
"""
import os
import sys
import tempfile
import time

import pandas as pd

from seat_reallocator.config import OCCUPIED
from seat_reallocator.io import load_tickets
from seat_reallocator.engine import detect_non_consecutive_orders
from seat_reallocator.engine import process_event, detect_collateral
from seat_reallocator.reporter import write_full_report


def run_reallocation(csv_path: str, out_path: str) -> None:
    tickets = load_tickets(csv_path)
    orders_by_event = detect_non_consecutive_orders(tickets)

    all_moves:      list = []
    all_infeasible: list = []

    for event_date, event_df in tickets.groupby('Data evento'):
        problematic = orders_by_event.get(event_date, set())
        if not problematic:
            continue
        moves, infeasible = process_event(event_df, problematic)
        for m in moves:
            m['Data evento'] = event_date
        all_moves.extend(moves)
        all_infeasible.extend((event_date, oid) for oid in infeasible)

    active         = tickets[tickets['Stato posto'].isin(OCCUPIED)]
    infeasible_set = {(ed, oid) for ed, oid in all_infeasible}
    collateral     = detect_collateral(active, all_moves, infeasible_set)

    write_full_report(csv_path, all_moves, infeasible_set, collateral, path=out_path)


def read_xlsx_sheets(path: str, skip: set[str] | None = None) -> pd.DataFrame:
    skip = skip or set()
    frames = []
    with pd.ExcelFile(path) as xl:
        for sheet in xl.sheet_names:
            if sheet in skip:
                continue
            df = xl.parse(sheet, dtype=str)
            df['_sheet'] = sheet
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def strip_strings(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.select_dtypes(include='object').columns:
        df[col] = df[col].str.strip()
    return df


def show(df: pd.DataFrame, label: str, cols: list[str]) -> None:
    if df.empty:
        return
    print(f'\n{label} ({len(df)}):')
    for _, row in df.head(10).iterrows():
        print(f'  sheet={row["_sheet"]}  order={row["Codice ordine"]}  seat={row["_orig_seat"]}  ' +
              '  '.join(f'{c}={row[c]!r}' for c in cols if c in row.index))
    if len(df) > 10:
        print(f'  ...and {len(df) - 10} more')


def main():
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print(__doc__)
        sys.exit(1)

    csv_path = sys.argv[1]
    ref_path = sys.argv[2] if len(sys.argv) == 3 else 'data/reallocation.xlsx'

    # Run reallocation into a temp file
    tmp = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
    tmp.close()
    try:
        print(f'Running reallocation on {csv_path}...')
        t0 = time.time()
        run_reallocation(csv_path, tmp.name)
        print(f'  Done in {time.time() - t0:.1f}s')

        # Read reference (old summary format: Posto originale / Posto nuovo)
        print(f'\nReading reference: {ref_path}')
        ref = strip_strings(read_xlsx_sheets(ref_path, skip={'COLLATERALE'}))
        ref = ref.rename(columns={'Posto originale': '_orig_seat', 'Posto nuovo': '_new_seat'})
        print(f'  {len(ref):,} rows across {ref["_sheet"].nunique()} sheets')

        # Read new output (full format: Posto / Nuovo posto), filter to non-NON_COINVOLTO
        new_all = strip_strings(read_xlsx_sheets(tmp.name, skip={'COLLATERALE'}))
        new_all = new_all.rename(columns={'Posto': '_orig_seat', 'Nuovo posto': '_new_seat'})
        new = new_all[new_all['Stato'] != 'NON COINVOLTO'].copy()
        print(f'  {len(new):,} relevant rows from new output (excl. NON COINVOLTO)')

    finally:
        os.unlink(tmp.name)

    key = ['_sheet', 'Codice ordine', '_orig_seat']
    merged = ref.merge(new, on=key, how='outer', suffixes=('_ref', '_new'), indicator=True)

    only_ref     = merged[merged['_merge'] == 'left_only']
    only_new     = merged[merged['_merge'] == 'right_only']
    both         = merged[merged['_merge'] == 'both']
    stato_diff   = both[both['Stato_ref'] != both['Stato_new']]
    seat_diff    = both[both['_new_seat_ref'] != both['_new_seat_new']]

    show(only_ref,   'Only in REFERENCE (not produced by new run)', ['Stato_ref',  '_new_seat_ref'])
    show(only_new,   'Only in NEW RUN (not in reference)',          ['Stato_new',  '_new_seat_new'])
    show(stato_diff, 'Stato mismatch',                             ['Stato_ref',  'Stato_new'])
    show(seat_diff,  'New-seat mismatch',                          ['_new_seat_ref', '_new_seat_new'])

    total = len(only_ref) + len(only_new) + len(stato_diff) + len(seat_diff)
    print()
    if total == 0:
        print('PASS — reallocation decisions match reference exactly.')
    else:
        print(f'FAIL — {total} differences found.')
        sys.exit(1)


if __name__ == '__main__':
    main()
