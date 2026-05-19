from collections import defaultdict

import pandas as pd

from .config import OCCUPIED


def resolve_seats(event_df: pd.DataFrame):
    """
    Determine the effective status of every physical seat for one event.

    A seat is OCCUPIED if any CONFIRMED/RESALE row exists for it.
    A seat is FREE    if only CANCELLED rows exist (truly unoccupied).

    Returns:
        occupied: {(settore, fila, posto): (order_id, settore_prezzi)}
        free:     {(settore, fila, posto): settore_prezzi}
    """
    active = event_df[event_df['Stato posto'].isin(OCCUPIED)]
    active_keys = set(zip(active['Settore'], active['Fila'], active['Posto']))

    act_dedup = active.drop_duplicates(['Settore', 'Fila', 'Posto'])
    occupied = {
        (r['Settore'], r['Fila'], r['Posto']): (r['Codice ordine'], r['Settore prezzi'])
        for r in act_dedup.to_dict('records')
    }

    canc = event_df[event_df['Stato posto'] == 'CANCELLED']
    canc_dedup = canc.drop_duplicates(['Settore', 'Fila', 'Posto'])
    free = {
        (r['Settore'], r['Fila'], r['Posto']): r['Settore prezzi']
        for r in canc_dedup.to_dict('records')
        if (r['Settore'], r['Fila'], r['Posto']) not in active_keys
    }

    return occupied, free


def build_segments(occupied: dict, free: dict) -> dict:
    """
    Group seats into independent segments keyed by (settore, fila, settore_prezzi).

    Returns:
        {(settore, fila, sp): {'seats': {posto: order_id}, 'free': {posto, ...}}}
    """
    segs = defaultdict(lambda: {'seats': {}, 'free': set()})
    for (s, f, p), (oid, sp) in occupied.items():
        segs[(s, f, sp)]['seats'][p] = oid
    for (s, f, p), sp in free.items():
        segs[(s, f, sp)]['free'].add(p)
    return dict(segs)
