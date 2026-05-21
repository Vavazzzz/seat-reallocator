import ast

import pandas as pd

from .config import VALID, OCCUPIED


def parse_orders(path: str) -> dict:
    """Parse orders.txt -> {event_date_str: set(order_id_str)}"""
    result = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            date_str, list_str = line.split(': ', 1)
            result[date_str] = set(ast.literal_eval(list_str))
    return result


def load_tickets(path: str) -> pd.DataFrame:
    with open(path, 'r', encoding='utf-8-sig') as f:
        first = f.readline()
    sep = ';' if ';' in first else ','
    df = pd.read_csv(
        path,
        sep=sep,
        low_memory=False,
        dtype={'Codice ordine': str, 'Data evento': str},
    )
    if 'Selezione in mappa' in df.columns:
        df = df[df['Selezione in mappa'].astype(str).str.lower() != 'true']
    df = df[df['Fila'].astype(str).str.upper() != 'GA']
    df = df[df['Stato posto'].isin(VALID)].copy()
    df['Posto'] = pd.to_numeric(df['Posto'], errors='coerce')
    df = df.dropna(subset=['Posto', 'Data evento', 'Settore', 'Fila', 'Settore prezzi'])
    df['Posto'] = df['Posto'].astype(int)
    return df


def detect_non_consecutive_orders(df: pd.DataFrame) -> dict[str, set[str]]:
    """Detect orders with non-consecutive seats -> {event_date_str: set(order_id_str)}"""
    from .geometry import is_adjacent

    active = df[df['Stato posto'].isin(OCCUPIED)]
    result: dict[str, set[str]] = {}
    for event_date, ev_df in active.groupby('Data evento'):
        non_consec: set[str] = set()
        for order_id, ord_df in ev_df.groupby('Codice ordine'):
            for _, seg_df in ord_df.groupby('Settore prezzi'):
                if seg_df['Settore'].nunique() > 1 or seg_df['Fila'].nunique() > 1:
                    non_consec.add(str(order_id))
                    break
                if not is_adjacent(seg_df['Posto'].tolist()):
                    non_consec.add(str(order_id))
                    break
        result[str(event_date)] = non_consec
    return result
