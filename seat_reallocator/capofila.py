import pandas as pd

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


def _bilateral_donor(occupied_local, settore, target_row, keep_side, all_cap, infeasible_set):
    """
    For Type A: check that every position in keep_side at target_row belongs to
    the same single order, and that order holds *exactly* keep_side seats in that row.
    Returns (donor_oid, donor_sp) or None.
    """
    donor_oid = donor_sp = None
    for pos in keep_side:
        entry = occupied_local.get((settore, target_row, pos))
        if entry is None:
            return None
        oid_val, sp_val = str(entry[0]), entry[1]
        if donor_oid is None:
            donor_oid, donor_sp = oid_val, sp_val
        elif oid_val != donor_oid:
            return None

    if donor_oid in infeasible_set:
        return None

    donor_seats_in_row = {
        p for p in all_cap
        if str(occupied_local.get((settore, target_row, p), (None,))[0]) == donor_oid
    }
    if donor_seats_in_row != set(keep_side):
        return None

    return donor_oid, donor_sp


def fix_capofila_orders(
    event_df: pd.DataFrame,
    infeasible_ids: list,
    occupied: dict,
    event_date: str,
) -> tuple:
    """
    Post-process NON RISOLVIBILE Capofila orders.

    Aisle seat positions are detected dynamically from data (lower-half = left,
    upper-half = right within each Settore prezzi group).

    Two strategies tried per order, in order:

    1. SEAT-BY-SEAT — each minority seat placed individually in an adjacent row (±1):
       a. Target slot free → place directly.
       b. Target occupied, donor relocates to a free slot within the same row
          without breaking its own adjacency.
       c. Target occupied, donor is a single-seat order → donor takes the
          problem order's vacated seat (cross-row swap of one seat pair).

    2. BILATERAL GROUP SWAP (only when minority and majority are equal size) —
       The entire minority group moves to the majority-side positions of an
       adjacent row, while the order occupying those positions (which must be a
       single order holding exactly that set of seats) swaps into the now-freed
       minority positions in the problem row.

    Only rows immediately adjacent (±1) are considered. occupied_local is rolled
    back on failure so subsequent orders see clean state.

    Returns:
        capofila_moves:   list of move dicts (Data evento already set)
        still_infeasible: list of order IDs that could not be resolved
    """
    capofila_moves:   list = []
    still_infeasible: list = []
    occupied_local = dict(occupied)
    sides_cache = _detect_sides(event_df)
    infeasible_set = {str(oid) for oid in infeasible_ids}

    for oid in infeasible_ids:
        oid_str = str(oid)
        order_rows = event_df[
            (event_df['Codice ordine'] == oid_str) &
            event_df['Settore prezzi'].str.contains(_CAPOFILA_PATTERN, case=False, na=False)
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
        left_seats  = seats & left_side
        right_seats = seats & right_side

        if len(left_seats) >= len(right_seats):
            keep_side = left_side
            to_move   = sorted(right_seats)
        else:
            keep_side = right_side
            to_move   = sorted(left_seats)

        if not to_move:
            still_infeasible.append(oid)
            continue

        rows_in_sp = set(
            event_df[
                (event_df['Settore'] == settore) &
                event_df['Settore prezzi'].str.contains(_CAPOFILA_PATTERN, case=False, na=False)
            ]['Fila'].astype(int)
        )
        candidate_rows = [r for r in [fila - 1, fila + 1] if r in rows_in_sp]

        snapshot = dict(occupied_local)

        # ── Attempt 1: seat-by-seat ───────────────────────────────────────────
        moves_sw:  list = []
        donors_sw: list = []
        failed = False

        for seat in to_move:
            placed = False
            for target_row in candidate_rows:
                for target_pos in sorted(keep_side):
                    key = (settore, target_row, target_pos)

                    if key not in occupied_local:
                        # Case a: free slot
                        moves_sw.append(_cross_move(
                            event_date, oid_str, settore, fila, target_row, sp, seat, target_pos,
                        ))
                        occupied_local[key] = (oid_str, sp)
                        placed = True
                        break

                    else:
                        donor_oid = str(occupied_local[key][0])
                        donor_sp  = occupied_local[key][1]
                        if donor_oid == oid_str or donor_oid in infeasible_set:
                            continue

                        donor_seats_in_row = {
                            p for p in all_cap
                            if str(occupied_local.get(
                                (settore, target_row, p), (None,)
                            )[0]) == donor_oid
                        }

                        # Case b: donor relocates within same row
                        for alt_pos in sorted(all_cap):
                            if (settore, target_row, alt_pos) in occupied_local:
                                continue
                            new_donor = (donor_seats_in_row - {target_pos}) | {alt_pos}
                            if is_adjacent(sorted(new_donor)):
                                donors_sw.append(_inrow_move(
                                    event_date, donor_oid, settore, target_row,
                                    donor_sp, target_pos, alt_pos,
                                ))
                                del occupied_local[(settore, target_row, target_pos)]
                                occupied_local[(settore, target_row, alt_pos)] = (donor_oid, donor_sp)
                                moves_sw.append(_cross_move(
                                    event_date, oid_str, settore, fila, target_row,
                                    sp, seat, target_pos,
                                ))
                                occupied_local[(settore, target_row, target_pos)] = (oid_str, sp)
                                placed = True
                                break
                        if placed:
                            break

                        # Case c: single-seat donor takes the vacated problem seat
                        if len(donor_seats_in_row) == 1:
                            donors_sw.append(_cross_move(
                                event_date, donor_oid, settore, target_row, fila,
                                donor_sp, target_pos, seat,
                            ))
                            del occupied_local[(settore, target_row, target_pos)]
                            del occupied_local[(settore, fila, seat)]
                            occupied_local[(settore, target_row, target_pos)] = (oid_str, sp)
                            occupied_local[(settore, fila, seat)] = (donor_oid, donor_sp)
                            moves_sw.append(_cross_move(
                                event_date, oid_str, settore, fila, target_row,
                                sp, seat, target_pos,
                            ))
                            placed = True
                            break

                if placed:
                    break
            if not placed:
                failed = True
                break

        if failed:
            occupied_local.clear()
            occupied_local.update(snapshot)

        # ── Attempt 2: bilateral group swap (equal-sized sides only) ─────────
        if failed and len(to_move) == len(keep_side):
            moves_bi:  list = []
            donors_bi: list = []
            failed = True

            to_move_sorted = sorted(to_move)
            keep_sorted    = sorted(keep_side)

            for target_row in candidate_rows:
                donor_info = _bilateral_donor(
                    occupied_local, settore, target_row, keep_side, all_cap, infeasible_set,
                )
                if donor_info is None:
                    continue
                donor_oid, donor_sp = donor_info

                # Problem order: minority seats → majority positions in target_row
                for orig, dest in zip(to_move_sorted, keep_sorted):
                    moves_bi.append(_cross_move(
                        event_date, oid_str, settore, fila, target_row, sp, orig, dest,
                    ))
                # Donor: majority positions in target_row → freed minority positions in fila
                for orig, dest in zip(keep_sorted, to_move_sorted):
                    donors_bi.append(_cross_move(
                        event_date, donor_oid, settore, target_row, fila, donor_sp, orig, dest,
                    ))

                # Atomic occupied_local update
                for pos in to_move_sorted:
                    del occupied_local[(settore, fila, pos)]
                for pos in keep_sorted:
                    del occupied_local[(settore, target_row, pos)]
                for dest in keep_sorted:
                    occupied_local[(settore, target_row, dest)] = (oid_str, sp)
                for dest in to_move_sorted:
                    occupied_local[(settore, fila, dest)] = (donor_oid, donor_sp)

                failed = False
                break

            if failed:
                occupied_local.clear()
                occupied_local.update(snapshot)
            else:
                moves_sw, donors_sw = moves_bi, donors_bi

        # ── Attempt 3: cross-side swap ────────────────────────────────────────
        # Move each majority seat to the minority side using single-seat donors
        # on that side (same row first, then ±1). Each donor swaps back into
        # the vacated majority position.
        if failed:
            moves_cs:  list = []
            donors_cs: list = []
            majority_seats = sorted(seats & keep_side)
            target_side    = right_side if keep_side == left_side else left_side
            cs_rows = [fila] + candidate_rows   # same row first, then ±1

            for s in majority_seats:
                placed = False
                for target_row in cs_rows:
                    for target_pos in sorted(target_side):
                        key = (settore, target_row, target_pos)

                        if key not in occupied_local:
                            if target_row == fila:
                                moves_cs.append(_inrow_move(
                                    event_date, oid_str, settore, fila, sp, s, target_pos,
                                ))
                            else:
                                moves_cs.append(_cross_move(
                                    event_date, oid_str, settore, fila, target_row,
                                    sp, s, target_pos,
                                ))
                            del occupied_local[(settore, fila, s)]
                            occupied_local[key] = (oid_str, sp)
                            placed = True
                            break

                        else:
                            donor_oid = str(occupied_local[key][0])
                            donor_sp  = occupied_local[key][1]
                            if donor_oid == oid_str or donor_oid in infeasible_set:
                                continue

                            # Donor must hold exactly one capofila seat in this settore
                            donor_cap_count = sum(
                                1 for k in occupied_local
                                if k[0] == settore
                                and str(occupied_local[k][0]) == donor_oid
                            )
                            if donor_cap_count != 1:
                                continue

                            if target_row == fila:
                                moves_cs.append(_inrow_move(
                                    event_date, oid_str, settore, fila, sp, s, target_pos,
                                ))
                                donors_cs.append(_inrow_move(
                                    event_date, donor_oid, settore, fila, donor_sp,
                                    target_pos, s,
                                ))
                            else:
                                moves_cs.append(_cross_move(
                                    event_date, oid_str, settore, fila, target_row,
                                    sp, s, target_pos,
                                ))
                                donors_cs.append(_cross_move(
                                    event_date, donor_oid, settore, target_row, fila,
                                    donor_sp, target_pos, s,
                                ))

                            del occupied_local[(settore, fila, s)]
                            del occupied_local[key]
                            occupied_local[key] = (oid_str, sp)
                            occupied_local[(settore, fila, s)] = (donor_oid, donor_sp)
                            placed = True
                            break

                    if placed:
                        break
                if not placed:
                    break
            else:
                failed = False

            if failed:
                occupied_local.clear()
                occupied_local.update(snapshot)
            else:
                moves_sw  = moves_cs
                donors_sw = donors_cs

        if failed:
            still_infeasible.append(oid)
            continue

        # COINVOLTO for seats that stayed in place (not covered by a SPOSTATO for this order)
        moved_orig = {
            m['Posto originale'] for m in moves_sw
            if m['Stato'] == 'SPOSTATO' and m['Codice ordine'] == oid_str
        }
        for s in seats - moved_orig:
            moves_sw.append({
                'Data evento':     event_date,
                'Codice ordine':   oid_str,
                'Settore':         settore,
                'Fila':            fila,
                'Settore prezzi':  sp,
                'Posto originale': s,
                'Posto nuovo':     s,
                'Stato':           'COINVOLTO',
            })

        capofila_moves.extend(donors_sw)
        capofila_moves.extend(moves_sw)

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
