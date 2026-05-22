import pandas as pd

from .config import OCCUPIED
from .geometry import is_adjacent

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


def _capofila_slice_valid(seats) -> bool:
    """A capofila row-slice is valid if empty, single, or a contiguous run."""
    return is_adjacent(sorted(seats))


def _donor_slice_valid_after_swap(
    occupied_local, donor_oid, settore, all_cap,
    lose_row, lose_pos,
    gain_row=None, gain_pos=None,
) -> bool:
    """
    Check that donor_oid's capofila row-slices remain valid after the proposed swap:
    donor loses (settore, lose_row, lose_pos) and optionally gains (settore, gain_row, gain_pos).
    """
    def _cap_seats_at(row):
        return frozenset(
            p for p in all_cap
            if str(occupied_local.get((settore, row, p), (None,))[0]) == donor_oid
        )

    new_lose_slice = _cap_seats_at(lose_row) - {lose_pos}
    if not _capofila_slice_valid(new_lose_slice):
        return False

    if gain_row is None:
        return True

    new_gain_slice = (
        new_lose_slice | {gain_pos}
        if gain_row == lose_row
        else _cap_seats_at(gain_row) | {gain_pos}
    )
    return _capofila_slice_valid(new_gain_slice)


def _try_full_side_move(
    occupied_local, infeasible_set, oid_str,
    settore, fila, target_row, sp, side_seats, all_cap, event_date,
):
    """
    Move `side_seats` from (settore, fila) to (settore, target_row) at the same positions.
    Each occupied target position's holder swaps back to fila at the same position.

    Validates atomically via a proposed-state copy; mutates occupied_local only on success.
    Returns (order_moves, donor_moves) or None.
    """
    side_sorted = sorted(side_seats)

    # Gather donors (one per seat position)
    seat_donors = {}
    for pos in side_sorted:
        key = (settore, target_row, pos)
        if key in occupied_local:
            d_oid = str(occupied_local[key][0])
            d_sp  = occupied_local[key][1]
            if d_oid == oid_str or d_oid in infeasible_set or d_sp != sp:
                return None
            seat_donors[pos] = (d_oid, d_sp)

    # Build proposed state for atomic validation
    proposed = dict(occupied_local)
    for pos in side_sorted:
        del proposed[(settore, fila, pos)]
        if pos in seat_donors:
            del proposed[(settore, target_row, pos)]
            proposed[(settore, fila, pos)] = seat_donors[pos]
        proposed[(settore, target_row, pos)] = (oid_str, sp)

    # Validate every donor's slices in the proposed state
    checked = set()
    for pos, (d_oid, _) in seat_donors.items():
        if d_oid in checked:
            continue
        checked.add(d_oid)
        for row in (fila, target_row):
            slc = frozenset(
                p for p in all_cap
                if str(proposed.get((settore, row, p), (None,))[0]) == d_oid
            )
            if not _capofila_slice_valid(slc):
                return None

    # Apply
    occupied_local.clear()
    occupied_local.update(proposed)

    order_moves = [
        _cross_move(event_date, oid_str, settore, fila, target_row, sp, pos, pos)
        for pos in side_sorted
    ]
    donor_moves = [
        _cross_move(event_date, d_oid, settore, target_row, fila, d_sp, pos, pos)
        for pos, (d_oid, d_sp) in seat_donors.items()
    ]
    return order_moves, donor_moves


def _try_single_seat_move(
    occupied_local, infeasible_set, oid_str,
    settore, fila, target_row, sp, src_pos, all_cap, event_date,
):
    """
    Move one seat from (fila, src_pos) to (target_row, src_pos).
    If occupied: donor swaps back to (fila, src_pos).
    Returns (order_moves, donor_moves) or None.
    """
    key = (settore, target_row, src_pos)
    donor_info = None

    if key in occupied_local:
        d_oid = str(occupied_local[key][0])
        d_sp  = occupied_local[key][1]
        if d_oid == oid_str or d_oid in infeasible_set or d_sp != sp:
            return None
        if not _donor_slice_valid_after_swap(
            occupied_local, d_oid, settore, all_cap,
            lose_row=target_row, lose_pos=src_pos,
            gain_row=fila,       gain_pos=src_pos,
        ):
            return None
        donor_info = (d_oid, d_sp)

    # Apply
    del occupied_local[(settore, fila, src_pos)]
    if donor_info:
        del occupied_local[(settore, target_row, src_pos)]
        occupied_local[(settore, fila, src_pos)] = (donor_info[0], donor_info[1])
    occupied_local[(settore, target_row, src_pos)] = (oid_str, sp)

    order_move = _cross_move(
        event_date, oid_str, settore, fila, target_row, sp, src_pos, src_pos,
    )
    donor_moves = (
        [_cross_move(event_date, donor_info[0], settore, target_row, fila,
                     donor_info[1], src_pos, src_pos)]
        if donor_info else []
    )
    return [order_move], donor_moves


def _resolve_cross_side(
    occupied_local, infeasible_set, oid_str, settore, fila, sp,
    majority_seats_sorted, target_side, all_cap, event_date, search_rows,
):
    """
    Move each majority seat to any free or swap-valid position in target_side.
    Processes seats one at a time, updating state incrementally.
    Donor swap: donor at (target_row, target_pos) receives (fila, src_pos).
    Rolls back fully on failure.
    Returns (order_moves, donor_moves) or None.
    """
    snapshot = dict(occupied_local)
    order_moves: list = []
    donor_moves: list = []

    for src_pos in majority_seats_sorted:
        placed = False
        for target_row in search_rows:
            for target_pos in sorted(target_side):
                # Skip positions this order already holds in target_row
                oid_in_target = frozenset(
                    p for p in all_cap
                    if str(occupied_local.get((settore, target_row, p), (None,))[0]) == oid_str
                )
                if target_pos in oid_in_target:
                    continue

                key = (settore, target_row, target_pos)
                donor_info = None

                if key in occupied_local:
                    d_oid = str(occupied_local[key][0])
                    d_sp  = occupied_local[key][1]
                    if d_oid == oid_str or d_oid in infeasible_set or d_sp != sp:
                        continue
                    if not _donor_slice_valid_after_swap(
                        occupied_local, d_oid, settore, all_cap,
                        lose_row=target_row, lose_pos=target_pos,
                        gain_row=fila,       gain_pos=src_pos,
                    ):
                        continue
                    donor_info = (d_oid, d_sp)

                # Apply step
                del occupied_local[(settore, fila, src_pos)]
                if donor_info:
                    del occupied_local[(settore, target_row, target_pos)]
                    occupied_local[(settore, fila, src_pos)] = (donor_info[0], donor_info[1])
                occupied_local[(settore, target_row, target_pos)] = (oid_str, sp)

                if target_row == fila:
                    order_moves.append(_inrow_move(
                        event_date, oid_str, settore, fila, sp, src_pos, target_pos,
                    ))
                    if donor_info:
                        donor_moves.append(_inrow_move(
                            event_date, donor_info[0], settore, fila, donor_info[1],
                            target_pos, src_pos,
                        ))
                else:
                    order_moves.append(_cross_move(
                        event_date, oid_str, settore, fila, target_row, sp,
                        src_pos, target_pos,
                    ))
                    if donor_info:
                        donor_moves.append(_cross_move(
                            event_date, donor_info[0], settore, target_row, fila,
                            donor_info[1], target_pos, src_pos,
                        ))
                placed = True
                break
            if placed:
                break

        if not placed:
            occupied_local.clear()
            occupied_local.update(snapshot)
            return None

    return order_moves, donor_moves


def _resolve_4seat(
    occupied_local, infeasible_set, oid_str, settore, fila, sp,
    left_side, right_side, all_cap, event_date, candidate_rows,
):
    """
    4-seat order: try moving one full side (left or right) to each adjacent row.
    Returns (order_moves, donor_moves) or None.
    """
    for move_side in (left_side, right_side):
        for target_row in candidate_rows:
            result = _try_full_side_move(
                occupied_local, infeasible_set, oid_str,
                settore, fila, target_row, sp, move_side, all_cap, event_date,
            )
            if result is not None:
                return result
    return None


def _resolve_3seat(
    occupied_local, infeasible_set, oid_str, settore, fila, sp,
    seats, left_side, right_side, all_cap, event_date, candidate_rows,
):
    """
    3-seat order — three strategies in priority order:

    S1 — move each minority seat to the same position in an adjacent row.
         Simplest fix: minority seat vacates current row, lands in neighbour.

    S2 — move the full majority side to an adjacent row (same positions).
         Valid only when order holds the complete majority side.

    S3 — cross-side: move majority seats into the opposite side's positions
         (same row first, then adjacent), using single-seat-accepting donors.

    Returns (order_moves, donor_moves) or None.
    """
    left_seats  = seats & left_side
    right_seats = seats & right_side

    if len(left_seats) >= len(right_seats):
        majority_side, majority_seats = left_side,  left_seats
        minority_side, minority_seats = right_side, right_seats
    else:
        majority_side, majority_seats = right_side, right_seats
        minority_side, minority_seats = left_side,  left_seats

    # S1: move each minority seat to same position in adjacent row
    for minority_pos in sorted(minority_seats):
        for target_row in candidate_rows:
            result = _try_single_seat_move(
                occupied_local, infeasible_set, oid_str,
                settore, fila, target_row, sp, minority_pos, all_cap, event_date,
            )
            if result is not None:
                return result

    # S2: move the full majority side to adjacent row
    if majority_seats == majority_side:
        for target_row in candidate_rows:
            result = _try_full_side_move(
                occupied_local, infeasible_set, oid_str,
                settore, fila, target_row, sp, majority_side, all_cap, event_date,
            )
            if result is not None:
                return result

    # S3: cross-side — move majority seats into minority-side positions
    return _resolve_cross_side(
        occupied_local, infeasible_set, oid_str, settore, fila, sp,
        sorted(majority_seats), minority_side, all_cap, event_date,
        [fila] + list(candidate_rows),
    )


def fix_capofila_orders(
    event_df: pd.DataFrame,
    infeasible_ids: list,
    occupied: dict,
    event_date: str,
) -> tuple:
    """
    Post-process NON RISOLVIBILE Capofila orders via cross-row reallocation.

    Success condition: after the fix each row-slice of the resolved order is a
    contiguous run (single seat, left pair, or right pair). The order may span
    multiple rows; that is intentional and acceptable.

    4-seat orders ({L1,L2,R1,R2}):
      Move one complete side to an adjacent row. Donors at target positions swap
      back to the vacated side in the problem row. Validated atomically.

    3-seat orders ({L1,L2,Rm} or {Lm,R1,R2}):
      S1 — move minority seat to same position in adjacent row
      S2 — move full majority side to adjacent row
      S3 — cross-side: relocate majority seats into the opposite side

    Returns:
        capofila_moves:   list of move dicts
        still_infeasible: list of order IDs that could not be resolved
    """
    capofila_moves:   list = []
    still_infeasible: list = []
    occupied_local   = dict(occupied)
    sides_cache      = _detect_sides(event_df)
    infeasible_set   = {str(oid) for oid in infeasible_ids}

    # Pre-cache valid capofila rows per settore
    rows_in_sector: dict = {}
    for (settore, _) in sides_cache:
        if settore not in rows_in_sector:
            rows_in_sector[settore] = set(
                event_df[
                    (event_df['Settore'] == settore) &
                    event_df['Settore prezzi'].str.contains(
                        _CAPOFILA_PATTERN, case=False, na=False,
                    )
                ]['Fila'].astype(int)
            )

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

        # Verify all order seats are present in occupied_local
        if not all((settore, fila, s) in occupied_local for s in seats):
            still_infeasible.append(oid)
            continue

        valid_rows     = rows_in_sector.get(settore, set())
        candidate_rows = [r for r in (fila - 1, fila + 1) if r in valid_rows]

        snapshot = dict(occupied_local)

        if len(seats) == 4:
            result = _resolve_4seat(
                occupied_local, infeasible_set, oid_str, settore, fila, sp,
                left_side, right_side, all_cap, event_date, candidate_rows,
            )
        else:
            result = _resolve_3seat(
                occupied_local, infeasible_set, oid_str, settore, fila, sp,
                seats, left_side, right_side, all_cap, event_date, candidate_rows,
            )

        if result is None:
            occupied_local.clear()
            occupied_local.update(snapshot)
            still_infeasible.append(oid)
            continue

        order_moves, donor_moves = result

        # COINVOLTO for seats that stayed at their original position
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

        capofila_moves.extend(donor_moves)
        capofila_moves.extend(order_moves)

    return capofila_moves, still_infeasible


def _cross_move(event_date, oid, settore, fila, fila_nuovo, sp, posto_orig, posto_nuovo):
    return {
        'Data evento':     event_date,
        'Codice ordine':   oid,
        'Settore':         settore,
        'Fila':            fila,
        'Fila nuovo':      fila_nuovo,
        'Settore prezzi':  sp,
        'Posto originale': posto_orig,
        'Posto nuovo':     posto_nuovo,
        'Stato':           'SPOSTATO',
    }


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
