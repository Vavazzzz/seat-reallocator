import pandas as pd

from .config import OCCUPIED

_CAPOFILA_PATTERN = 'capofila'


def _detect_sides(event_df: pd.DataFrame) -> dict:
    """
    Per (Settore, Settore prezzi) detect aisle seat positions from data.
    Splits sorted distinct Posto values: lower half → left, upper half → right.
    Returns: {(settore, sp): (left_frozenset, right_frozenset, all_frozenset)}
    """
    result = {}
    cap_df = event_df[
        event_df['Settore prezzi'].str.contains(_CAPOFILA_PATTERN, case=False, na=False)
    ]
    for (settore, sp), grp in cap_df.groupby(['Settore', 'Settore prezzi']):
        postos = sorted(set(grp['Posto'].astype(int)))
        if len(postos) < 2:
            continue
        mid   = len(postos) // 2
        left  = frozenset(postos[:mid])
        right = frozenset(postos[mid:])
        result[(settore, sp)] = (left, right, frozenset(postos))
    return result


def _resolve_3seat(
    occupied_local, oid_str, settore, fila, sp,
    seats, left_side, right_side, all_cap, event_date,
):
    """
    3-seat aisle order: move the isolated aisle seat next to the pair via in-row chain shift.

    The isolated seat (minority side) is relocated to the position immediately adjacent
    to the pair. Any occupant at the target position cascades one step along the row,
    propagating until a free slot absorbs the displacement. If the entire middle section
    is occupied, the isolated seat's own vacated position serves as the cascade sink.

    Left-pair {L1,L2,Rx}: move Rx → L2+1, shifting chain rightward.
    Right-pair {Lx,R1,R2}: move Lx → R1-1, shifting chain leftward.

    Price-category constraints are ignored for chain members (regular middle-row orders).

    Returns (order_moves, chain_moves) or None if infeasible.
    """
    sorted_cap = sorted(all_cap)
    L2, R1 = sorted_cap[1], sorted_cap[2]

    left_seats  = seats & left_side
    right_seats = seats & right_side

    if not left_seats or not right_seats:
        return None

    if len(left_seats) >= len(right_seats):
        if len(right_seats) != 1:
            return None
        isolated_pos = next(iter(right_seats))
        T         = L2 + 1
        direction = +1
    else:
        if len(left_seats) != 1:
            return None
        isolated_pos = next(iter(left_seats))
        T         = R1 - 1
        direction = -1

    # Remove the isolated seat first so its vacated position can absorb the cascade
    # if no free seat exists in the middle section.
    del occupied_local[(settore, fila, isolated_pos)]

    # Extended search includes iso_pos (now free) as the last-resort sink.
    if direction == +1:
        search_range = range(T, isolated_pos + 1)
    else:
        search_range = range(T, isolated_pos - 1, -1)

    F = next(
        (p for p in search_range if (settore, fila, p) not in occupied_local),
        None,
    )
    if F is None:
        return None

    if F == T:
        occupied_local[(settore, fila, T)] = (oid_str, sp)
        return [_inrow_move(event_date, oid_str, settore, fila, sp, isolated_pos, T)], []

    chain_moves = _execute_shift_chain(
        occupied_local, settore, fila, T, F, direction, event_date,
    )
    if chain_moves is None:
        return None

    occupied_local[(settore, fila, T)] = (oid_str, sp)
    return [_inrow_move(event_date, oid_str, settore, fila, sp, isolated_pos, T)], chain_moves


def _execute_shift_chain(
    occupied_local, settore, fila, T, F, direction, event_date,
):
    """
    Shift every occupied group in the chain between T and F by one position toward F,
    leaving T free.

    direction=+1: chain [T, F-1] shifts right — process F-1 down to T.
    direction=-1: chain [F+1, T] shifts left  — process F+1 up to T.

    A group is all seats in this row owned by the same order. Infeasible when a chain
    order also holds row-seats outside the chain (shifting would break its contiguity).

    Returns list of move dicts, or None.
    """
    chain_positions = (set(range(T, F)) if direction == +1
                       else set(range(F + 1, T + 1)))

    # Build per-order index of all positions held in this row
    row_index: dict = {}
    for (s, f, p), (o, _) in occupied_local.items():
        if s == settore and f == fila:
            row_index.setdefault(str(o), set()).add(p)

    # Collect chain orders
    chain_orders: dict = {}
    for p in chain_positions:
        entry = occupied_local.get((settore, fila, p))
        if entry is None:
            continue
        oid_d = str(entry[0])
        chain_orders.setdefault(oid_d, []).append(p)

    # Validate: no chain order has row-seats outside the chain (would create a gap)
    for oid_d in chain_orders:
        if not row_index.get(oid_d, set()).issubset(chain_positions):
            return None

    # Execute from F-end toward T so each position is vacated before its neighbour moves in
    moves: list = []
    processed: set = set()
    scan = (range(F - 1, T - 1, -1) if direction == +1 else range(F + 1, T + 1))

    for p in scan:
        entry = occupied_local.get((settore, fila, p))
        if entry is None:
            continue
        oid_d, sp_d = entry
        if oid_d in processed:
            continue
        processed.add(oid_d)

        positions = sorted(chain_orders[oid_d])
        shift_seq = reversed(positions) if direction == +1 else iter(positions)
        for pos in shift_seq:
            new_pos = pos + direction
            del occupied_local[(settore, fila, pos)]
            occupied_local[(settore, fila, new_pos)] = (oid_d, sp_d)
            moves.append(_inrow_move(event_date, oid_d, settore, fila, sp_d, pos, new_pos))

    return moves


def fix_capofila_orders(
    event_df: pd.DataFrame,
    infeasible_ids: list,
    occupied: dict,
    event_date: str,
) -> tuple:
    """
    Post-process NON RISOLVIBILE Capofila orders.

    3-seat orders ({L1,L2,Rx} or {Lx,R1,R2}): move isolated seat next to pair
        via in-row chain shift (minimises total disruption, no cross-row moves).
    4-seat orders: left unchanged (not handled here).

    Returns:
        capofila_moves:   list of move dicts
        still_infeasible: list of order IDs that could not be resolved
    """
    capofila_moves:   list = []
    still_infeasible: list = []
    occupied_local         = dict(occupied)
    sides_cache            = _detect_sides(event_df)

    for oid in infeasible_ids:
        oid_str = str(oid)

        order_rows = event_df[
            (event_df['Codice ordine'] == oid_str) &
            event_df['Settore prezzi'].str.contains(_CAPOFILA_PATTERN, case=False, na=False) &
            event_df['Stato posto'].isin(OCCUPIED)
        ]
        if order_rows.empty:
            still_infeasible.append(oid)
            continue
        if order_rows['Fila'].nunique() > 1 or order_rows['Settore'].nunique() > 1:
            still_infeasible.append(oid)
            continue

        settore = order_rows['Settore'].iloc[0]
        fila    = int(order_rows['Fila'].iloc[0])
        sp      = order_rows['Settore prezzi'].iloc[0]
        seats   = set(order_rows['Posto'].astype(int))

        sides_key = (settore, sp)
        if sides_key not in sides_cache:
            still_infeasible.append(oid)
            continue

        left_side, right_side, all_cap = sides_cache[sides_key]

        if not all((settore, fila, s) in occupied_local for s in seats):
            still_infeasible.append(oid)
            continue

        # 4-seat orders are left unchanged
        if len(seats) == 4:
            still_infeasible.append(oid)
            continue

        snapshot = dict(occupied_local)

        result = _resolve_3seat(
            occupied_local, oid_str, settore, fila, sp,
            seats, left_side, right_side, all_cap, event_date,
        )

        if result is None:
            occupied_local.clear()
            occupied_local.update(snapshot)
            still_infeasible.append(oid)
            continue

        order_moves, chain_moves = result

        # COINVOLTO for capofila seats that stayed at their original position
        moved_orig = {
            m['Posto originale'] for m in order_moves
            if m['Stato'] == 'SPOSTATO' and m['Codice ordine'] == oid_str
        }
        for s in seats - moved_orig:
            order_moves.append({
                'Data evento':     event_date,
                'Codice ordine':   oid_str,
                'Settore':         settore,
                'Fila':            fila,
                'Settore prezzi':  sp,
                'Posto originale': s,
                'Posto nuovo':     s,
                'Stato':           'COINVOLTO',
            })

        capofila_moves.extend(chain_moves)
        capofila_moves.extend(order_moves)

    return capofila_moves, still_infeasible


def _inrow_move(event_date, oid, settore, fila, sp, posto_orig, posto_nuovo):
    return {
        'Data evento':     event_date,
        'Codice ordine':   oid,
        'Settore':         settore,
        'Fila':            fila,
        'Settore prezzi':  sp,
        'Posto originale': posto_orig,
        'Posto nuovo':     posto_nuovo,
        'Stato':           'SPOSTATO',
    }
