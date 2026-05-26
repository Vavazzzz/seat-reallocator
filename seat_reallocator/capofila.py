import pandas as pd

from .config import OCCUPIED

_CAPOFILA_PATTERN = 'capofila'


def build_occupied_current(event_df: pd.DataFrame) -> dict:
    """
    Build an occupied dict keyed on the CURRENT seat position (Nuovo posto when
    present, Posto otherwise), so the chain shift operates on the actual venue
    layout after any previous reallocation pass.

    Value: (order_id, settore_prezzi, original_posto)
    The original_posto is used as 'Posto originale' in move records so that the
    reporter can match back to the source file row.
    """
    active = event_df[event_df['Stato posto'].isin(OCCUPIED)].copy()

    if 'Nuovo posto' in active.columns:
        cur = pd.to_numeric(active['Nuovo posto'], errors='coerce')
        active['_cur'] = cur.fillna(active['Posto']).astype(int)
    else:
        active['_cur'] = active['Posto'].astype(int)

    active_dedup = active.drop_duplicates(subset=['Settore', 'Fila', '_cur'])
    return {
        (r['Settore'], r['Fila'], int(r['_cur'])):
            (str(r['Codice ordine']), r['Settore prezzi'], int(r['Posto']))
        for r in active_dedup.to_dict('records')
    }


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
    3-seat aisle order: move the isolated seat adjacent to the pair via in-row shift.

    Three strategies are assembled and tried in ascending order of estimated moves;
    the first one that passes chain-validation wins:

    1. Relay — a single-seat order X jumps to the vacated isolated position; only
       the seats between T and X need to shift (often far fewer than the full gap).
       Cost = |X - T| + 1.  X is scanned nearest-first so the cheapest relay is
       found quickly; scanning stops once relay cost would exceed standard cost.

    2. Standard cascade — shift [T, F-1] toward F (nearest free slot from T toward
       isolated_pos, guaranteed to exist because isolated_pos itself is free).
       Cost = |F - T|.

    3. Secondary cascade — shift the pair outward (away from isolated seat), freeing
       the pair's inner aisle seat for the isolated seat to fill.  Valid only when
       the two pair seats are consecutive (L2 = L1+1 or R1 = R2-1).
       Cost = distance to the nearest free slot on the outer side.

    occupied_local values: (order_id, settore_prezzi, original_posto)
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
        isolated_pos   = next(iter(right_seats))
        T_pri, dir_pri = L2 + 1, +1
        T_sec, dir_sec = sorted_cap[1], -1     # free L2 by leftward cascade
        pair_consec    = (sorted_cap[1] == sorted_cap[0] + 1)
    else:
        if len(left_seats) != 1:
            return None
        isolated_pos   = next(iter(left_seats))
        T_pri, dir_pri = R1 - 1, -1
        T_sec, dir_sec = sorted_cap[2], +1     # free R1 by rightward cascade
        pair_consec    = (sorted_cap[3] == sorted_cap[2] + 1)

    # Remove isolated seat — its vacated position is available as a relay landing spot.
    del occupied_local[(settore, fila, isolated_pos)]

    # Per-order seat count in this row (snapshot after isolated removal).
    row_index: dict = {}
    for (s, f, p), entry in occupied_local.items():
        if s == settore and f == fila:
            row_index.setdefault(str(entry[0]), []).append(p)

    # --- Standard primary cascade ---
    range_pri = (range(T_pri, isolated_pos + 1) if dir_pri == +1
                 else range(T_pri, isolated_pos - 1, -1))
    F_pri = next((p for p in range_pri if (settore, fila, p) not in occupied_local), None)
    d_pri = abs(F_pri - T_pri) if F_pri is not None else float('inf')

    # candidates: (cost, T, F_or_X, direction, relay_entry_or_None)
    candidates = []
    if F_pri is not None:
        candidates.append((d_pri, T_pri, F_pri, dir_pri, None))

    # --- Relay candidates (primary direction only) ---
    relay_scan = (range(T_pri, isolated_pos) if dir_pri == +1
                  else range(T_pri, isolated_pos, -1))
    for X in relay_scan:
        entry_x = occupied_local.get((settore, fila, X))
        if entry_x is None:
            continue                               # free slot — standard handles it
        if len(row_index.get(str(entry_x[0]), [])) != 1:
            continue                               # multi-seat order, skip
        d_relay = abs(X - T_pri) + 1
        if d_relay > d_pri:
            break                                  # further relays only get more expensive
        candidates.append((d_relay, T_pri, X, dir_pri, entry_x))

    # --- Secondary cascade (pair shifts outward) ---
    if pair_consec:
        p = T_sec + dir_sec
        while 1 <= p <= 9999:
            if (settore, fila, p) not in occupied_local:
                candidates.append((abs(p - T_sec), T_sec, p, dir_sec, None))
                break
            p += dir_sec

    candidates.sort(key=lambda c: c[0])

    for _, T, F_or_X, direction, relay_entry in candidates:
        snapshot = dict(occupied_local)

        if relay_entry is not None:
            # Relay: jump single-seat order from X to isolated_pos, then cascade [T, X).
            X = F_or_X
            del occupied_local[(settore, fila, X)]
            occupied_local[(settore, fila, isolated_pos)] = relay_entry
            relay_move = _inrow_move(
                event_date, relay_entry[0], settore, fila,
                relay_entry[1], relay_entry[2], isolated_pos,
            )
            # X is now free — use it as the cascade sink.
            chain_moves = _execute_shift_chain(
                occupied_local, settore, fila, T, X, direction, event_date,
            )
            if chain_moves is not None:
                occupied_local[(settore, fila, T)] = (oid_str, sp, isolated_pos)
                return (
                    [_inrow_move(event_date, oid_str, settore, fila, sp, isolated_pos, T)],
                    chain_moves + [relay_move],
                )
        else:
            # Standard / secondary cascade.
            if F_or_X == T:
                occupied_local[(settore, fila, T)] = (oid_str, sp, isolated_pos)
                return [_inrow_move(event_date, oid_str, settore, fila, sp, isolated_pos, T)], []
            chain_moves = _execute_shift_chain(
                occupied_local, settore, fila, T, F_or_X, direction, event_date,
            )
            if chain_moves is not None:
                occupied_local[(settore, fila, T)] = (oid_str, sp, isolated_pos)
                return (
                    [_inrow_move(event_date, oid_str, settore, fila, sp, isolated_pos, T)],
                    chain_moves,
                )

        occupied_local.clear()
        occupied_local.update(snapshot)

    return None


def _execute_shift_chain(
    occupied_local, settore, fila, T, F, direction, event_date,
):
    """
    Shift every occupied group in the chain between T and F by one position toward F,
    leaving T free. Operates in current-position (Nuovo posto) space.

    direction=+1: chain [T, F-1] shifts right — process F-1 down to T.
    direction=-1: chain [F+1, T] shifts left  — process F+1 up to T.

    occupied_local values are (order_id, sp, original_posto) 3-tuples.
    Move records use original_posto as Posto originale.

    Infeasible when a chain order also holds row-seats outside the chain range.
    Returns list of move dicts, or None.
    """
    chain_positions = (set(range(T, F)) if direction == +1
                       else set(range(F + 1, T + 1)))

    # Build per-order index of all current positions held in this row
    row_index: dict = {}
    for (s, f, p), entry in occupied_local.items():
        if s == settore and f == fila:
            row_index.setdefault(str(entry[0]), set()).add(p)

    # Collect chain orders
    chain_orders: dict = {}
    for p in chain_positions:
        entry = occupied_local.get((settore, fila, p))
        if entry is None:
            continue
        oid_d = str(entry[0])
        chain_orders.setdefault(oid_d, []).append(p)

    # Validate: no chain order has row-seats outside the chain (would break contiguity)
    for oid_d in chain_orders:
        if not row_index.get(oid_d, set()).issubset(chain_positions):
            return None

    # Execute from F-end toward T
    moves: list = []
    processed: set = set()
    scan = (range(F - 1, T - 1, -1) if direction == +1 else range(F + 1, T + 1))

    for p in scan:
        entry = occupied_local.get((settore, fila, p))
        if entry is None:
            continue
        oid_d = entry[0]
        if oid_d in processed:
            continue
        processed.add(oid_d)

        positions = sorted(chain_orders[oid_d])
        is_consec = (len(positions) == 1 or
                     positions[-1] - positions[0] == len(positions) - 1)
        if is_consec:
            # Consecutive block: only the boundary seat crosses a new threshold;
            # all interior seats stay in place (no unnecessary moves).
            move_from = positions[0] if direction == +1 else positions[-1]
            move_to   = positions[-1] + 1 if direction == +1 else positions[0] - 1
            _, sp_pos, orig_pos = occupied_local[(settore, fila, move_from)]
            del occupied_local[(settore, fila, move_from)]
            occupied_local[(settore, fila, move_to)] = (oid_d, sp_pos, orig_pos)
            moves.append(_inrow_move(event_date, oid_d, settore, fila, sp_pos, orig_pos, move_to))
        else:
            # Non-consecutive seats (defensive fallback): shift each seat individually.
            shift_seq = reversed(positions) if direction == +1 else iter(positions)
            for pos in shift_seq:
                _, sp_pos, orig_pos = occupied_local[(settore, fila, pos)]
                new_pos = pos + direction
                del occupied_local[(settore, fila, pos)]
                occupied_local[(settore, fila, new_pos)] = (oid_d, sp_pos, orig_pos)
                moves.append(_inrow_move(event_date, oid_d, settore, fila, sp_pos, orig_pos, new_pos))

    return moves


def fix_capofila_orders(
    event_df: pd.DataFrame,
    infeasible_ids: list,
    occupied: dict,
    event_date: str,
) -> tuple:
    """
    Post-process NON RISOLVIBILE Capofila orders via in-row chain shift.

    occupied must be built with build_occupied_current() so that keys reflect
    the actual current seat positions (Nuovo posto after any prior reallocation).

    3-seat orders ({L1,L2,Rx} or {Lx,R1,R2}): move isolated seat next to pair.
    4-seat orders: left unchanged.

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

        # Capofila orders were NON RISOLVIBILE in the prior pass, so their
        # current position equals their original Posto. Use Posto for the lookup.
        if not all((settore, fila, s) in occupied_local for s in seats):
            still_infeasible.append(oid)
            continue

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

        # COINVOLTO for capofila seats that neither moved directly nor via chain
        # (Option B cascades the pair via chain_moves, so exclude those too).
        chain_moved_orig = {
            m['Posto originale'] for m in chain_moves
            if str(m['Codice ordine']) == oid_str
        }
        moved_orig = {
            m['Posto originale'] for m in order_moves
            if m['Stato'] == 'SPOSTATO' and m['Codice ordine'] == oid_str
        }
        for s in seats - moved_orig - chain_moved_orig:
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
