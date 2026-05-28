import ast

import pandas as pd

from .config import VALID


def load_csv(path: str, **kwargs) -> pd.DataFrame:
    """Read a CSV, auto-detecting ; vs , separator from the first line."""
    with open(path, 'r', encoding='utf-8-sig') as f:
        first = f.readline()
    sep = ';' if ';' in first else ','
    return pd.read_csv(path, sep=sep, low_memory=False, **kwargs)


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


def _parse_fila(val):
    """Convert Fila: numeric string → int, non-numeric non-null string (e.g. "-") → str, null → None."""
    if pd.isna(val):
        return None
    s = str(val).strip()
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return s if s else None


def load_tickets(path: str) -> pd.DataFrame:
    if path.lower().endswith(('.xlsx', '.xls')):
        sheets = pd.read_excel(
            path,
            sheet_name=None,
            dtype={'Codice ordine': str, 'Data evento': str},
        )
        df = pd.concat(sheets.values(), ignore_index=True)
    else:
        df = load_csv(path, dtype={'Codice ordine': str, 'Data evento': str})
    df = df[df['Fila'].astype(str).str.upper() != 'GA']
    df = df[df['Stato posto'].isin(VALID)].copy()
    df['Posto'] = pd.to_numeric(df['Posto'], errors='coerce')
    df['Fila']  = df['Fila'].apply(_parse_fila)
    df = df.dropna(subset=['Posto', 'Fila', 'Data evento', 'Settore', 'Settore prezzi'])
    df['Posto'] = df['Posto'].astype(int)
    return df
