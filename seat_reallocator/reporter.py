from collections import defaultdict

import pandas as pd

from .config import OCCUPIED

_MOVE_COLS = [
    'Codice ordine', 'Settore', 'Fila', 'Settore prezzi',
    'Posto originale', 'Posto nuovo', 'Stato',
]

_DEFAULT_REALLOCATION_PATH = 'data/reallocation.xlsx'
_DEFAULT_ANNOTATED_PATH    = 'data/report_annotated.xlsx'


def _sheet_name(event_date: str) -> str:
    return str(event_date).replace(':', '.').replace('/', '-')[:31]


def write_reallocation_report(
    all_rows: list,
    collateral_rows: list,
    path: str = _DEFAULT_REALLOCATION_PATH,
) -> None:
    """
    Write the summary report (moved + infeasible seats only), one sheet per event.
    Writes nothing if all_rows is empty.
    """
    if not all_rows:
        return
    df_all = pd.DataFrame(all_rows)
    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        for event_date, group in df_all.groupby('Data evento'):
            group[_MOVE_COLS].to_excel(writer, sheet_name=_sheet_name(event_date), index=False)
        if collateral_rows:
            df_coll = pd.DataFrame(collateral_rows)
            df_coll[_MOVE_COLS].to_excel(writer, sheet_name='COLLATERALE', index=False)


def write_full_report(
    source_path: str,
    all_moves: list,
    infeasible_set: set,
    collateral_rows: list,
    path: str = _DEFAULT_ANNOTATED_PATH,
) -> pd.DataFrame:
    """
    Annotate every row of the source CSV with 'Nuovo posto' and 'Stato', then
    write one sheet per event to an xlsx file.

    Returns the annotated DataFrame (for stat reporting in the CLI).
    """
    with open(source_path, 'r', encoding='utf-8-sig') as f:
        first_line = f.readline()
    sep = ';' if ';' in first_line else ','
    big_df = pd.read_csv(
        source_path,
        sep=sep,
        low_memory=False,
        dtype={'Codice ordine': str, 'Data evento': str},
    )
    big_df['Codice ordine'] = big_df['Codice ordine'].astype(str)
    big_df['_posto_num'] = pd.to_numeric(big_df['Posto'], errors='coerce')

    # Build move lookup
    if all_moves:
        move_df = pd.DataFrame(all_moves)[
            ['Data evento', 'Codice ordine', 'Posto originale', 'Posto nuovo', 'Stato']
        ].copy()
        move_df.rename(columns={
            'Posto originale': '_posto_num',
            'Posto nuovo':     '_nuovo_posto',
            'Stato':           '_stato',
        }, inplace=True)
        move_df['Codice ordine'] = move_df['Codice ordine'].astype(str)
    else:
        move_df = pd.DataFrame(
            columns=['Data evento', 'Codice ordine', '_posto_num', '_nuovo_posto', '_stato']
        )

    big_df = big_df.merge(
        move_df,
        on=['Data evento', 'Codice ordine', '_posto_num'],
        how='left',
    )

    big_df['_is_inf'] = [
        (ev, oid) in infeasible_set
        for ev, oid in zip(big_df['Data evento'], big_df['Codice ordine'])
    ]

    occupied_mask = big_df['Stato posto'].isin(OCCUPIED)
    has_move      = big_df['_stato'].notna()

    big_df['Nuovo posto'] = big_df['_posto_num']
    big_df['Stato']       = 'NON COINVOLTO'

    inf_mask = occupied_mask & big_df['_is_inf']
    big_df.loc[inf_mask, 'Stato'] = 'NON RISOLVIBILE'

    big_df.loc[has_move, 'Nuovo posto'] = big_df.loc[has_move, '_nuovo_posto']
    big_df.loc[has_move, 'Stato']       = big_df.loc[has_move, '_stato']

    # CANCELLED / non-occupied rows are always NON COINVOLTO
    big_df.loc[~occupied_mask, 'Stato']       = 'NON COINVOLTO'
    big_df.loc[~occupied_mask, 'Nuovo posto'] = big_df.loc[~occupied_mask, '_posto_num']

    big_df['Nuovo posto'] = pd.to_numeric(big_df['Nuovo posto'], errors='coerce').astype('Int64')

    # Drop temp columns, reinsert Nuovo posto + Stato right after Posto
    big_df.drop(columns=['_posto_num', '_nuovo_posto', '_stato', '_is_inf'],
                inplace=True, errors='ignore')
    cols = list(big_df.columns)
    for c in ('Nuovo posto', 'Stato'):
        if c in cols:
            cols.remove(c)
    posto_idx = cols.index('Posto') + 1
    cols.insert(posto_idx,     'Nuovo posto')
    cols.insert(posto_idx + 1, 'Stato')
    big_df = big_df[cols]

    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        for event_date, group in big_df.groupby('Data evento'):
            group.to_excel(writer, sheet_name=_sheet_name(event_date), index=False)
        if collateral_rows:
            df_coll = pd.DataFrame(collateral_rows)
            df_coll[_MOVE_COLS].to_excel(writer, sheet_name='COLLATERALE', index=False)

    return big_df
