from collections import defaultdict

import pandas as pd

from .geometry import is_adjacent
from .seats import resolve_seats, build_segments
from .solver import solve_segment


def process_event(event_df: pd.DataFrame, problematic: set) -> tuple:
    """
    Orchestrate per-event seat reallocation.

    Returns:
        all_moves:      list of move dicts (without 'Data evento' key — caller adds it)
        all_infeasible: list of order_ids that could not be fixed
    """
    occupied, free = resolve_seats(event_df)
    segments       = build_segments(occupied, free)

    # Map each problematic order to the segment(s) its seats appear in
    order_segments: dict = defaultdict(set)
    for (settore, fila, sp), seg in segments.items():
        for oid in seg['seats'].values():
            if oid in problematic:
                order_segments[oid].add((settore, fila, sp))

    # Orders spanning multiple segments cannot be fixed under the constraints
    globally_infeasible = {
        oid for oid, segs in order_segments.items() if len(segs) > 1
    }

    fixable = problematic - globally_infeasible

    all_moves:      list = []
    all_infeasible: list = list(globally_infeasible)

    for (settore, fila, sp), seg in segments.items():
        if not any(oid in fixable for oid in seg['seats'].values()):
            continue

        moves, infeasible = solve_segment(seg['seats'], seg['free'], fixable)
        all_infeasible.extend(infeasible)

        for oid, old_p, new_p in moves:
            all_moves.append({
                'Settore':         settore,
                'Fila':            fila,
                'Settore prezzi':  sp,
                'Codice ordine':   oid,
                'Posto originale': old_p,
                'Posto nuovo':     new_p,
                'Stato':           'SPOSTATO' if old_p != new_p else 'COINVOLTO',
            })

    return all_moves, all_infeasible


def detect_collateral(
    active_df: pd.DataFrame,
    all_moves: list,
    infeasible_set: set,
) -> list:
    """
    Identify orders that were adjacent before reallocation but non-adjacent after.

    Args:
        active_df:      DataFrame of all occupied seats (CONFIRMED/RESALE rows).
        all_moves:      All move dicts produced by process_event (with 'Data evento').
        infeasible_set: {(event_date, order_id)} pairs that were flagged infeasible.

    Returns list of collateral row dicts.
    """
    collateral_rows: list = []
    moves_by_event: dict  = defaultdict(list)
    for m in all_moves:
        moves_by_event[m['Data evento']].append(m)

    for event_date, event_active in active_df.groupby('Data evento'):
        if event_date not in moves_by_event:
            continue

        orig: dict = defaultdict(list)
        for _, row in event_active.iterrows():
            orig[row['Codice ordine']].append(row['Posto'])

        final: dict = {oid: list(ps) for oid, ps in orig.items()}
        for m in moves_by_event[event_date]:
            oid = m['Codice ordine']
            op, np_ = m['Posto originale'], m['Posto nuovo']
            if op in final[oid]:
                final[oid].remove(op)
            if np_ not in final[oid]:
                final[oid].append(np_)

        for oid, orig_ps in orig.items():
            if (event_date, oid) in infeasible_set:
                continue
            if not is_adjacent(orig_ps) or is_adjacent(final[oid]):
                continue
            order_info = event_active[event_active['Codice ordine'] == oid].iloc[0]
            collateral_rows.append({
                'Data evento':     event_date,
                'Codice ordine':   oid,
                'Settore':         order_info['Settore'],
                'Fila':            order_info['Fila'],
                'Settore prezzi':  order_info['Settore prezzi'],
                'Posto originale': ', '.join(str(p) for p in sorted(orig_ps)),
                'Posto nuovo':     ', '.join(str(p) for p in sorted(final[oid])),
                'Stato':           'COLLATERALE',
            })

    return collateral_rows
