"""
Combine reallocation output (DF1) with updated ticket report (DF2) and
supplementary data (DF3) to produce a post-reallocation report.

Merge logic:
  1. Filter DF1 for Stato in {SPOSTATO, COINVOLTO}.
  2. Inner-join DF2 on Sigillo fiscale — DF2 becomes the base (latest data),
     enriched with Nuovo posto / Stato (/ Nuova fila) from DF1.
  3. Filter merged result where "data annullo" > cutoff date.
  4. Left-join DF3 on (Codice ordine, Posto) to append supplementary columns.
"""
import argparse
import sys

import pandas as pd

from ..io import load_csv

_REALLOC_STATES = {'SPOSTATO', 'COINVOLTO'}
_DEFAULT_OUT = 'data/post_report.xlsx'


def build(
    annotated_path: str,
    updated_report_path: str,
    extra_path: str,
    annullo_from: str,
    out_path: str = _DEFAULT_OUT,
) -> None:
    # --- DF1: annotated reallocation report ---
    print('Loading annotated report (DF1)...', flush=True)
    sheets = pd.read_excel(
        annotated_path,
        sheet_name=None,
        dtype={'Codice ordine': str, 'Sigillo fiscale': str},
    )
    df1 = pd.concat(sheets.values(), ignore_index=True)
    df1_filtered = df1[df1['Stato'].isin(_REALLOC_STATES)].copy()
    if df1_filtered.empty:
        sys.exit('No SPOSTATO/COINVOLTO rows found in the annotated report.')

    realloc_cols = ['Sigillo fiscale', 'Nuovo posto', 'Stato']
    if 'Nuova fila' in df1_filtered.columns:
        realloc_cols.append('Nuova fila')
    missing = [c for c in realloc_cols if c not in df1_filtered.columns]
    if missing:
        sys.exit(f'DF1 is missing required columns: {missing}')

    df1_slim = df1_filtered[realloc_cols].drop_duplicates(subset=['Sigillo fiscale'])
    print(f'  {len(df1_slim):,} SPOSTATO/COINVOLTO seats.', flush=True)

    # --- DF2: updated full ticket report ---
    print('Loading updated ticket report (DF2)...', flush=True)
    df2 = load_csv(
        updated_report_path,
        dtype={'Codice ordine': str, 'Sigillo fiscale': str},
    )
    if 'Sigillo fiscale' not in df2.columns:
        sys.exit("DF2 is missing the 'Sigillo fiscale' column.")
    df2['Posto'] = pd.to_numeric(df2['Posto'], errors='coerce').astype('Int64')
    print(f'  {len(df2):,} rows.', flush=True)

    # Inner join: only rows that have a SPOSTATO/COINVOLTO match in DF1
    merged = df2.merge(df1_slim, on='Sigillo fiscale', how='inner')
    print(f'  After DF1+DF2 merge: {len(merged):,} rows.', flush=True)
    if merged.empty:
        sys.exit('No matching rows after merging DF1 and DF2 on Sigillo fiscale.')

    # Filter on data annullo
    try:
        cutoff = pd.Timestamp(annullo_from)
    except Exception:
        sys.exit(f'Invalid --annullo-from date: {annullo_from!r}. Use YYYY-MM-DD.')
    if 'data annullo' not in merged.columns:
        sys.exit("DF2 is missing the 'data annullo' column.")
    merged['data annullo'] = pd.to_datetime(merged['data annullo'], errors='coerce')
    merged = merged[merged['data annullo'] > cutoff]
    print(f'  After data annullo > {annullo_from}: {len(merged):,} rows.', flush=True)
    if merged.empty:
        sys.exit('No rows remain after applying the data annullo filter.')

    # --- DF3: supplementary data ---
    print('Loading supplementary data (DF3)...', flush=True)
    df3 = load_csv(extra_path, dtype={'Codice ordine': str})
    df3['Posto'] = pd.to_numeric(df3['Posto'], errors='coerce').astype('Int64')
    print(f'  {len(df3):,} rows.', flush=True)

    result = merged.merge(df3, on=['Codice ordine', 'Posto'], how='left')
    matched = result['Codice ordine'].notna().sum()
    print(f'  After DF3 join: {len(result):,} rows ({matched:,} matched DF3).', flush=True)

    result.to_excel(out_path, index=False)
    print(f'\nPost-reallocation report written to {out_path}', flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Build post-reallocation report from three data sources',
    )
    parser.add_argument('annotated', help='report_annotated.xlsx (DF1)')
    parser.add_argument('updated_report', help='Updated ticket-report CSV (DF2)')
    parser.add_argument('extra', help='Supplementary data CSV (DF3)')
    parser.add_argument(
        '--annullo-from', metavar='DATE', required=True,
        help='Keep only rows where "data annullo" > DATE (format: YYYY-MM-DD)',
    )
    parser.add_argument(
        '--out', metavar='PATH', default=_DEFAULT_OUT,
        help=f'Output xlsx path (default: {_DEFAULT_OUT})',
    )
    args = parser.parse_args()
    build(args.annotated, args.updated_report, args.extra, args.annullo_from, args.out)
