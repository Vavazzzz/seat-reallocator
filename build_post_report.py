#!/usr/bin/env python3
"""
Combine reallocation output (DF1) with updated ticket report (DF2) and
supplementary data (DF3) to produce a post-reallocation report.

    python build_post_report.py <annotated.xlsx> <updated_report.csv> <extra.csv> --annullo-from YYYY-MM-DD

DF1  report_annotated.xlsx produced by reallocate.py or reallocate_capofila.py.
DF2  Updated full ticket-report CSV (same format as reallocate.py input, latest movements).
DF3  Supplementary CSV; joined on Codice ordine + Posto.

Merge logic:
  1. Filter DF1 for Stato in {SPOSTATO, COINVOLTO}.
  2. Inner-join DF2 on Sigillo fiscale → DF2 becomes the base (latest data),
     enriched with Nuovo posto / Stato (/ Nuova fila) from DF1.
  3. Left-join DF3 on Codice ordine + Posto to append supplementary columns.
"""
import argparse
import sys

import pandas as pd


_REALLOC_STATES = {'SPOSTATO', 'COINVOLTO'}


def _load_csv(path: str, **kwargs) -> pd.DataFrame:
    with open(path, 'r', encoding='utf-8-sig') as f:
        first = f.readline()
    sep = ';' if ';' in first else ','
    return pd.read_csv(path, sep=sep, low_memory=False, **kwargs)


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
        '--out', metavar='PATH', default='data/post_report.xlsx',
        help='Output xlsx path (default: data/post_report.xlsx)',
    )
    args = parser.parse_args()

    # --- DF1: annotated reallocation report ---
    print('Loading annotated report (DF1)...', flush=True)
    sheets = pd.read_excel(
        args.annotated, sheet_name=None,
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
    df2 = _load_csv(
        args.updated_report,
        dtype={'Codice ordine': str, 'Sigillo fiscale': str},
    )
    if 'Sigillo fiscale' not in df2.columns:
        sys.exit("DF2 is missing the 'Sigillo fiscale' column.")
    df2['Posto'] = pd.to_numeric(df2['Posto'], errors='coerce').astype('Int64')
    print(f'  {len(df2):,} rows.', flush=True)

    # Inner join: only keep DF2 rows that have a SPOSTATO/COINVOLTO match in DF1
    merged = df2.merge(df1_slim, on='Sigillo fiscale', how='inner')
    print(f'  After DF1+DF2 merge: {len(merged):,} rows.', flush=True)
    if merged.empty:
        sys.exit('No matching rows after merging DF1 and DF2 on Sigillo fiscale.')

    # Filter on data annullo
    try:
        cutoff = pd.Timestamp(args.annullo_from)
    except Exception:
        sys.exit(f'Invalid --annullo-from date: {args.annullo_from!r}. Use YYYY-MM-DD.')
    if 'data annullo' not in merged.columns:
        sys.exit("DF2 is missing the 'data annullo' column.")
    merged['data annullo'] = pd.to_datetime(merged['data annullo'], errors='coerce')
    merged = merged[merged['data annullo'] > cutoff]
    print(f'  After data annullo > {args.annullo_from}: {len(merged):,} rows.', flush=True)
    if merged.empty:
        sys.exit('No rows remain after applying the data annullo filter.')

    # --- DF3: supplementary data ---
    print('Loading supplementary data (DF3)...', flush=True)
    df3 = _load_csv(args.extra, dtype={'Codice ordine': str})
    df3['Posto'] = pd.to_numeric(df3['Posto'], errors='coerce').astype('Int64')
    print(f'  {len(df3):,} rows.', flush=True)

    result = merged.merge(df3, on=['Codice ordine', 'Posto'], how='left')
    matched = result['Codice ordine'].notna().sum()
    print(f'  After DF3 join: {len(result):,} rows ({matched:,} matched DF3).', flush=True)

    # --- Output ---
    result.to_excel(args.out, index=False)
    print(f'\nPost-reallocation report written to {args.out}', flush=True)


if __name__ == '__main__':
    main()
