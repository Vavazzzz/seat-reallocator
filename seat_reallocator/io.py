import ast

import pandas as pd

from .config import VALID


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
    if path.lower().endswith(('.xlsx', '.xls')):
        sheets = pd.read_excel(
            path,
            sheet_name=None,
            dtype={'Codice ordine': str, 'Data evento': str},
        )
        df = pd.concat(sheets.values(), ignore_index=True)
    else:
        with open(path, 'r', encoding='utf-8-sig') as f:
            first = f.readline()
        sep = ';' if ';' in first else ','
        df = pd.read_csv(
            path,
            sep=sep,
            low_memory=False,
            dtype={'Codice ordine': str, 'Data evento': str},
        )
    df = df[df['Fila'].astype(str).str.upper() != 'GA']
    df = df[df['Stato posto'].isin(VALID)].copy()
    df['Posto'] = pd.to_numeric(df['Posto'], errors='coerce')
    df = df.dropna(subset=['Posto', 'Data evento', 'Settore', 'Fila', 'Settore prezzi'])
    df['Posto'] = df['Posto'].astype(int)
    return df
