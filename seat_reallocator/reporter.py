import pandas as pd

from .config import OCCUPIED

_MOVE_COLS = [
    'Codice ordine', 'Settore', 'Fila', 'Settore prezzi',
    'Posto originale', 'Posto nuovo', 'Stato',
]

_DEFAULT_ANNOTATED_PATH = 'data/report_annotated.xlsx'


def _sheet_name(event_date: str) -> str:
    return str(event_date).replace(':', '.').replace('/', '-')[:31]


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
    if source_path.lower().endswith(('.xlsx', '.xls')):
        sheets = pd.read_excel(
            source_path,
            sheet_name=None,
            dtype={'Codice ordine': str, 'Data evento': str},
        )
        big_df = pd.concat(sheets.values(), ignore_index=True)
    else:
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
    has_cross_row = any('Fila nuovo' in m for m in all_moves)
    if all_moves:
        moves_raw = pd.DataFrame(all_moves)
        keep_cols = ['Data evento', 'Codice ordine', 'Posto originale', 'Posto nuovo', 'Stato']
        if has_cross_row:
            moves_raw['Fila nuovo'] = moves_raw.get('Fila nuovo', pd.NA)
            keep_cols.append('Fila nuovo')
        move_df = moves_raw[keep_cols].copy()
        rename_map = {
            'Posto originale': '_posto_num',
            'Posto nuovo':     '_nuovo_posto',
            'Stato':           '_stato',
        }
        if has_cross_row:
            rename_map['Fila nuovo'] = '_fila_nuova'
        move_df.rename(columns=rename_map, inplace=True)
        move_df['Codice ordine'] = move_df['Codice ordine'].astype(str)
    else:
        move_df = pd.DataFrame(
            columns=['Data evento', 'Codice ordine', '_posto_num', '_nuovo_posto', '_stato']
        )
        has_cross_row = False

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

    if 'Nuovo posto' not in big_df.columns:
        big_df['Nuovo posto'] = big_df['_posto_num']
    if 'Stato' not in big_df.columns:
        big_df['Stato'] = 'NON COINVOLTO'
    if has_cross_row and 'Nuova fila' not in big_df.columns:
        big_df['Nuova fila'] = big_df['Fila']

    inf_mask = occupied_mask & big_df['_is_inf']
    big_df.loc[inf_mask, 'Stato'] = 'NON RISOLVIBILE'

    if has_move.any():
        big_df.loc[has_move, 'Nuovo posto'] = big_df.loc[has_move, '_nuovo_posto']
        big_df.loc[has_move, 'Stato']       = big_df.loc[has_move, '_stato']
        if has_cross_row and '_fila_nuova' in big_df.columns:
            cross_mask = has_move & big_df['_fila_nuova'].notna()
            if cross_mask.any():
                big_df.loc[cross_mask, 'Nuova fila'] = big_df.loc[cross_mask, '_fila_nuova']

    # CANCELLED / non-occupied rows are always NON COINVOLTO
    big_df.loc[~occupied_mask, 'Stato']       = 'NON COINVOLTO'
    big_df.loc[~occupied_mask, 'Nuovo posto'] = big_df.loc[~occupied_mask, '_posto_num']

    big_df['Nuovo posto'] = pd.to_numeric(big_df['Nuovo posto'], errors='coerce').astype('Int64')
    if has_cross_row:
        big_df['Nuova fila'] = pd.to_numeric(big_df['Nuova fila'], errors='coerce').astype('Int64')

    # Drop temp columns, reinsert Nuovo posto + Stato (+ Nuova fila if needed) right after Posto
    drop_cols = ['_posto_num', '_nuovo_posto', '_stato', '_is_inf']
    if has_cross_row:
        drop_cols.append('_fila_nuova')
    big_df.drop(columns=drop_cols, inplace=True, errors='ignore')
    cols = list(big_df.columns)
    insert_cols = ['Nuovo posto', 'Stato']
    if has_cross_row:
        insert_cols.append('Nuova fila')
    for c in insert_cols:
        if c in cols:
            cols.remove(c)
    posto_idx = cols.index('Posto') + 1
    for i, c in enumerate(insert_cols):
        cols.insert(posto_idx + i, c)
    big_df = big_df[cols]

    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        for event_date, group in big_df.groupby('Data evento'):
            group.to_excel(writer, sheet_name=_sheet_name(event_date), index=False)
        if collateral_rows:
            df_coll = pd.DataFrame(collateral_rows)
            df_coll[_MOVE_COLS].to_excel(writer, sheet_name='COLLATERALE', index=False)

    return big_df
