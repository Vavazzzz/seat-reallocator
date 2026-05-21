import pandas as pd

_LEFT  = frozenset({1, 2})
_RIGHT = frozenset({19, 20})


def fix_capofila_orders(
    event_df: pd.DataFrame,
    infeasible_ids: list,
    occupied: dict,
    event_date: str,
) -> tuple:
    """
    Post-process NON RISOLVIBILE Capofila orders by moving minority-side seats
    to free majority-side positions in adjacent rows (same Settore + Settore prezzi).

    Capofila rows have aisle seats only: positions {1,2} (left) and {19,20} (right).
    An order is a Capofila order if its Settore prezzi contains 'capofila'.
    Cross-row moves land on the majority side to keep all seats on one aisle.

    Returns:
        capofila_moves:   list of move dicts (Data evento already set)
        still_infeasible: list of order IDs that could not be resolved
    """
    capofila_moves:   list = []
    still_infeasible: list = []
    occupied_local = dict(occupied)

    for oid in infeasible_ids:
        oid_str = str(oid)
        order_rows = event_df[
            (event_df['Codice ordine'] == oid_str) &
            event_df['Settore prezzi'].str.contains('capofila', case=False, na=False)
        ]
        if order_rows.empty:
            still_infeasible.append(oid)
            continue

        # Only handle single-row, single-sector Capofila orders; others stay infeasible
        if order_rows['Fila'].nunique() > 1 or order_rows['Settore'].nunique() > 1:
            still_infeasible.append(oid)
            continue

        settore = order_rows['Settore'].iloc[0]
        fila    = int(order_rows['Fila'].iloc[0])
        sp      = order_rows['Settore prezzi'].iloc[0]
        seats   = set(order_rows['Posto'].astype(int))

        left_seats  = seats & _LEFT
        right_seats = seats & _RIGHT

        if len(left_seats) >= len(right_seats):
            keep_side = _LEFT
            to_move   = sorted(right_seats)
        else:
            keep_side = _RIGHT
            to_move   = sorted(left_seats)

        if not to_move:
            still_infeasible.append(oid)
            continue

        # Rows that exist in this (Settore, Settore prezzi), sorted by proximity
        rows_in_sp = sorted(
            event_df[
                (event_df['Settore'] == settore) &
                event_df['Settore prezzi'].str.contains('capofila', case=False, na=False)
            ]['Fila'].astype(int).unique()
        )
        candidate_rows = sorted(
            (r for r in rows_in_sp if r != fila),
            key=lambda r: abs(r - fila),
        )

        moves_for_order: list = []
        failed = False

        for seat in to_move:
            placed = False
            for target_row in candidate_rows:
                for target_pos in sorted(keep_side):
                    key = (settore, target_row, target_pos)
                    if key not in occupied_local:
                        moves_for_order.append({
                            'Data evento':     event_date,
                            'Codice ordine':   oid_str,
                            'Settore':         settore,
                            'Fila':            fila,
                            'Fila nuovo':      target_row,
                            'Settore prezzi':  sp,
                            'Posto originale': seat,
                            'Posto nuovo':     target_pos,
                            'Stato':           'SPOSTATO',
                        })
                        occupied_local[key] = (oid_str, sp)
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                failed = True
                break

        if failed:
            still_infeasible.append(oid)
        else:
            capofila_moves.extend(moves_for_order)

    return capofila_moves, still_infeasible
