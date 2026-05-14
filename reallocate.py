#!/usr/bin/env python3
"""
Seat Reallocator

For each problematic order (seats in the same row but not consecutive),
finds a rearrangement within the same (Settore, Fila, Settore prezzi) segment
that makes all its seats adjacent, displacing as few other orders as possible.

Input:
  data/report_cleaned.csv  — all sold tickets
  data/orders.txt          — problematic order IDs grouped by event date

Output:
  data/reallocation.csv    — seat moves: (order, old_posto, new_posto) per event
"""

import ast
import time
from collections import defaultdict

import pandas as pd

OCCUPIED     = {'CONFIRMED', 'RESALE'}
VALID        = {'CONFIRMED', 'RESALE', 'CANCELLED'}
MAX_BRANCHES = 25   # candidate placements tried per problematic order during backtracking


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

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
    df = df[df['Stato posto'].isin(VALID)].copy()
    df['Posto'] = pd.to_numeric(df['Posto'], errors='coerce')
    df = df.dropna(subset=['Posto', 'Data evento', 'Settore', 'Fila', 'Settore prezzi'])
    df['Posto'] = df['Posto'].astype(int)
    return df


# ---------------------------------------------------------------------------
# Seat resolution
# ---------------------------------------------------------------------------

def resolve_seats(event_df: pd.DataFrame):
    """
    For each physical seat in the event determine its effective status.

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


# ---------------------------------------------------------------------------
# 1-D geometry helpers
# ---------------------------------------------------------------------------

def contiguous_runs(positions: list) -> list:
    """Split a sorted list of integers into maximal consecutive runs."""
    if not positions:
        return []
    runs, cur = [], [positions[0]]
    for p in positions[1:]:
        if p == cur[-1] + 1:
            cur.append(p)
        else:
            runs.append(cur)
            cur = [p]
    runs.append(cur)
    return runs


def is_adjacent(postos: list) -> bool:
    s = sorted(postos)
    return len(s) <= 1 or all(s[i + 1] == s[i] + 1 for i in range(len(s) - 1))


# ---------------------------------------------------------------------------
# Seat pairing
# ---------------------------------------------------------------------------

def _pair_seats(old_p: list, new_p: list) -> list:
    """
    Match old seats to new seats minimising the number that move.
    Seats present in both lists stay fixed; the rest are paired in sorted order.
    Returns [(old_seat, new_seat), ...].
    """
    kept = set(old_p) & set(new_p)
    remaining_old = sorted(set(old_p) - kept)
    remaining_new = sorted(set(new_p) - kept)
    return [(s, s) for s in sorted(kept)] + list(zip(remaining_old, remaining_new))


# ---------------------------------------------------------------------------
# Segment solver
# ---------------------------------------------------------------------------

def solve_segment(seats: dict, free_postos: set, problematic_set: set) -> tuple:
    """
    ILP-based seat reallocator. Falls back to backtracking (_solve_segment_bt)
    if pulp is unavailable or the solver finds no feasible solution.

    Model: one binary variable x[order, block] per (order, contiguous-block) pair.
      - Each order gets exactly one block.
      - No seat is claimed by two orders.
    Objective: minimise COLL_PENALTY * collateral + prob_displacement.
      - Non-prob originally-adjacent orders pay COLL_PENALTY if moved elsewhere.
      - Prob orders that cannot be fixed pay INFEASIBLE_PENALTY (stay in place).

    Returns:
        moves:      [(order_id, old_posto, new_posto), ...]
        infeasible: [order_id, ...]   — orders that could not be made adjacent
    """
    try:
        import pulp
    except ImportError:
        return _solve_segment_bt(seats, free_postos, problematic_set)

    order_postos: dict = defaultdict(list)
    for pos, oid in seats.items():
        order_postos[oid].append(pos)
    for oid in order_postos:
        order_postos[oid].sort()

    to_fix = {
        oid: postos
        for oid, postos in order_postos.items()
        if oid in problematic_set and not is_adjacent(postos)
    }
    if not to_fix:
        return [], []

    all_pos = sorted(set(seats) | free_postos)
    runs    = contiguous_runs(all_pos)

    def get_blocks(k: int) -> list:
        return [tuple(run[i:i + k]) for run in runs for i in range(len(run) - k + 1)]

    # All orders are in the model so the ILP jointly optimises the full segment.
    # Prob orders also get their original (non-contiguous) seats as a dummy option
    # carrying INFEASIBLE_PENALTY — chosen only when no contiguous block fits.
    candidates: dict = {}
    for oid, postos in order_postos.items():
        blocks = get_blocks(len(postos))
        if oid in to_fix:
            orig = tuple(postos)
            if orig not in blocks:
                blocks = blocks + [orig]
        candidates[oid] = blocks

    INFEASIBLE_PENALTY = 1_000_000
    COLL_PENALTY       = 10_000

    mdl = pulp.LpProblem("seats", pulp.LpMinimize)
    oi  = {oid: i for i, oid in enumerate(order_postos)}

    x: dict = {
        (oid, b): pulp.LpVariable(f"x{oi[oid]}_{bi}", cat="Binary")
        for oid, blocks in candidates.items()
        for bi, b in enumerate(blocks)
    }

    # Each order → exactly one block
    for oid, blocks in candidates.items():
        mdl += pulp.lpSum(x[oid, b] for b in blocks) == 1

    # Each seat → at most one block
    seat_vars: dict = defaultdict(list)
    for (oid, b), var in x.items():
        for seat in b:
            seat_vars[seat].append(var)
    for seat, var_list in seat_vars.items():
        if len(var_list) > 1:
            mdl += pulp.lpSum(var_list) <= 1

    # Objective
    #
    # Prob orders:
    #   dummy block (original non-contiguous seats) → INFEASIBLE_PENALTY
    #   any contiguous block                        → seat displacement
    #
    # Non-prob originally-adjacent orders:
    #   original block                              → 0
    #   any other block                             → COLL_PENALTY + displacement
    #   (displacement tiebreaker eliminates degenerate circular permutations
    #    — every possible assignment gets a unique score, so the solver always
    #    picks the one that moves the order the least distance from its origin)
    #
    # Non-prob originally-non-adjacent orders:
    #   any block                                   → centroid displacement
    #   (keeps them close to where they were without forcing collateral cost)
    obj = []
    for oid, blocks in candidates.items():
        postos = order_postos[oid]
        orig   = tuple(postos)
        if oid in to_fix:
            for b in blocks:
                if b == orig:
                    obj.append(INFEASIBLE_PENALTY * x[oid, b])
                else:
                    disp = sum(abs(nb - ob) for nb, ob in zip(b, postos))
                    obj.append(disp * x[oid, b])
        elif is_adjacent(postos) and orig in blocks:
            for b in blocks:
                if b != orig:
                    disp = sum(abs(nb - ob) for nb, ob in zip(b, postos))
                    obj.append((COLL_PENALTY + disp) * x[oid, b])
        else:
            centroid = sum(postos) / len(postos)
            for b in blocks:
                disp = abs(sum(b) / len(b) - centroid)
                if disp:
                    obj.append(disp * x[oid, b])

    mdl += pulp.lpSum(obj)

    mdl.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=10))

    if mdl.sol_status not in (1, 2):    # 1=Optimal, 2=IntegerFeasible (time limit)
        return _solve_segment_bt(seats, free_postos, problematic_set)

    # Extract: find the chosen block for each order
    assignment: dict = {}
    for oid, blocks in candidates.items():
        for b in blocks:
            if (pulp.value(x[oid, b]) or 0) > 0.5:
                assignment[oid] = b
                break

    # Infeasible = prob orders that chose their dummy (original non-contiguous) block
    infeasible = [oid for oid in to_fix
                  if assignment.get(oid) == tuple(order_postos[oid])]

    # Build seat → order map
    new_asgn: dict = {}
    for oid, b in assignment.items():
        for p in b:
            new_asgn[p] = oid
    for oid, postos in order_postos.items():    # safety: any unassigned order stays
        if oid not in assignment:
            for p in postos:
                new_asgn[p] = oid

    # Emit seat-level moves
    inv: dict = defaultdict(list)
    for pos, oid in new_asgn.items():
        inv[oid].append(pos)
    for oid in inv:
        inv[oid].sort()

    moves = []
    for oid, old_p in order_postos.items():
        new_p = inv.get(oid, old_p)
        if set(old_p) != set(new_p):
            for old, new in _pair_seats(old_p, new_p):
                moves.append((oid, old, new))

    return moves, infeasible


def _solve_segment_bt(seats: dict, free_postos: set, problematic_set: set) -> tuple:
    """
    Backtracking fallback used when pulp is unavailable or the ILP has no solution.

    Strategy:
      1. Backtracking search places each prob order in the best available contiguous
         block (sorted by proximity, capped at MAX_BRANCHES). Evictions are allowed
         because chain displacements can leave all non-prob orders adjacent.
      2. All non-prob orders are reassigned from a shared pool: displaced orders
         (those that lost seats to prob placement) are processed first so they get
         priority, then intact orders reclaim their original seats where possible.

    Returns:
        moves:      [(order_id, old_posto, new_posto), ...]
        infeasible: [order_id, ...]   — orders that could not be fixed
    """
    order_postos: dict = defaultdict(list)
    for pos, oid in seats.items():
        order_postos[oid].append(pos)
    for oid in order_postos:
        order_postos[oid].sort()

    to_fix = {
        oid: postos
        for oid, postos in order_postos.items()
        if oid in problematic_set and not is_adjacent(postos)
    }
    if not to_fix:
        return [], []

    all_pos = sorted(set(seats) | free_postos)
    runs    = contiguous_runs(all_pos)

    def candidate_blocks(k: int) -> list:
        blocks = []
        for run in runs:
            for i in range(len(run) - k + 1):
                blocks.append(tuple(run[i: i + k]))
        return blocks

    def step_cost(block: tuple, old_p: list) -> int:
        return sum(abs(n - o) for n, o in zip(sorted(block), old_p))

    prob_list = sorted(to_fix.items(), key=lambda x: -len(x[1]))  # largest first

    # best tracks (collateral, displacement) — collateral is primary objective.
    best: dict = {'collateral': float('inf'), 'displacement': float('inf'), 'placement': None}

    def simulate_collateral(placement: dict) -> int:
        """
        Simulate the non-prob greedy assignment for a given prob placement and
        count how many originally-adjacent non-prob orders would end up non-adjacent.
        """
        prob_tk   = {p for postos in placement.values() for p in postos}
        remaining = set(p for p in all_pos if p not in prob_tk)
        # Remove seats of prob orders not in placement (they stay in place)
        for oid, old_p in prob_list:
            if oid not in placement:
                for p in old_p:
                    remaining.discard(p)

        displaced = sorted(
            [(oid, op) for oid, op in order_postos.items()
             if oid not in to_fix and any(p in prob_tk for p in op)],
            key=lambda x: -len(x[1]),
        )
        intact = sorted(
            [(oid, op) for oid, op in order_postos.items()
             if oid not in to_fix and all(p not in prob_tk for p in op)],
            key=lambda x: (-len(x[1]), min(x[1])),
        )
        count = 0
        for oid, old_p in displaced + intact:
            k        = len(old_p)
            centroid = sum(old_p) / k
            rem_list = sorted(remaining)
            blk      = None
            bd       = float('inf')
            for run in contiguous_runs(rem_list):
                for i in range(len(run) - k + 1):
                    b = tuple(run[i: i + k])
                    d = abs(sum(b) / k - centroid)
                    if d < bd:
                        bd, blk = d, b
            if blk is None:
                if is_adjacent(old_p):  # was adjacent, now can't be placed contiguously
                    count += 1
                rem_list.sort(key=lambda p: abs(p - centroid))
                blk = tuple(rem_list[:k]) if len(rem_list) >= k else tuple(rem_list)
            for p in blk:
                remaining.discard(p)
        return count

    def record_if_better(placement: dict, displacement: int):
        coll = simulate_collateral(placement)
        if (coll < best['collateral'] or
                (coll == best['collateral'] and displacement < best['displacement'])):
            best['collateral']  = coll
            best['displacement'] = displacement
            best['placement']   = dict(placement)

    # Greedy warm start
    g_taken: set      = set()
    g_placement: dict = {}
    for oid, old_p in prob_list:
        k        = len(old_p)
        centroid = sum(old_p) / k
        for block in sorted(candidate_blocks(k), key=lambda b: abs(sum(b) / k - centroid)):
            if not any(p in g_taken for p in block):
                g_placement[oid] = block
                g_taken.update(block)
                break
    if len(g_placement) == len(prob_list):
        record_if_better(g_placement, sum(step_cost(g_placement[oid], op) for oid, op in prob_list))

    deadline = time.time() + 1.0

    def backtrack(idx: int, taken: set, placement: dict, partial_cost: int):
        if time.time() > deadline:
            return
        # Only prune on displacement once we have a zero-collateral solution
        if best['collateral'] == 0 and partial_cost >= best['displacement']:
            return
        if idx == len(prob_list):
            record_if_better(placement, partial_cost)
            return
        oid, old_p = prob_list[idx]
        k          = len(old_p)
        centroid   = sum(old_p) / k
        blocks = sorted(candidate_blocks(k), key=lambda b: abs(sum(b) / k - centroid))[:MAX_BRANCHES]
        for block in blocks:
            if any(p in taken for p in block):
                continue
            cost_here = step_cost(block, old_p)
            if best['collateral'] == 0 and partial_cost + cost_here >= best['displacement']:
                continue
            placement[oid] = block
            backtrack(idx + 1, taken | set(block), placement, partial_cost + cost_here)
            del placement[oid]

    backtrack(0, set(), {}, 0)

    if best['placement'] is None:
        taken_fb: set = set()
        best['placement'] = {}
        for oid, old_p in prob_list:
            k        = len(old_p)
            centroid = sum(old_p) / k
            for block in sorted(candidate_blocks(k), key=lambda b: abs(sum(b) / k - centroid)):
                if not any(p in taken_fb for p in block):
                    best['placement'][oid] = block
                    taken_fb.update(block)
                    break

    infeasible = [oid for oid, _ in prob_list if oid not in best['placement']]
    prob_taken = {p for postos in best['placement'].values() for p in postos}

    # --- Build new full assignment ---

    new_asgn: dict = {}

    # 1. Prob orders at their chosen blocks
    for oid, postos in best['placement'].items():
        for p in postos:
            new_asgn[p] = oid

    # 2. Infeasible prob orders stay in place so their seats aren't taken by others
    for oid in infeasible:
        for p in to_fix[oid]:
            new_asgn[p] = oid

    # 3. All non-prob orders share a pool (all seats not claimed by prob placement
    #    or infeasible orders). Displaced orders (with any seat in prob_taken) are
    #    processed first — they need a new block urgently. Intact orders follow and
    #    will naturally reclaim their original seats if available.
    remaining = set(p for p in all_pos if p not in new_asgn)

    displaced_np = sorted(
        [(oid, op) for oid, op in order_postos.items()
         if oid not in to_fix and any(p in prob_taken for p in op)],
        key=lambda x: -len(x[1]),
    )
    intact_np = sorted(
        [(oid, op) for oid, op in order_postos.items()
         if oid not in to_fix and all(p not in prob_taken for p in op)],
        key=lambda x: (-len(x[1]), min(x[1])),
    )

    for oid, old_p in displaced_np + intact_np:
        k        = len(old_p)
        centroid = sum(old_p) / k
        rem_list = sorted(remaining)

        best_block = None
        best_dist  = float('inf')
        for run in contiguous_runs(rem_list):
            for i in range(len(run) - k + 1):
                block = tuple(run[i: i + k])
                d = abs(sum(block) / k - centroid)
                if d < best_dist:
                    best_dist  = d
                    best_block = block

        if best_block is None:
            # No contiguous block available — take k nearest individual seats
            rem_list.sort(key=lambda p: abs(p - centroid))
            best_block = tuple(rem_list[:k]) if len(rem_list) >= k else tuple(rem_list)

        for p in best_block:
            new_asgn[p] = oid
            remaining.discard(p)

    # --- Emit seat-level moves ---
    inv: dict = defaultdict(list)
    for pos, oid in new_asgn.items():
        inv[oid].append(pos)
    for oid in inv:
        inv[oid].sort()

    moves = []
    for oid, old_p in order_postos.items():
        new_p = inv.get(oid, old_p)
        if set(old_p) != set(new_p):
            for old, new in _pair_seats(old_p, new_p):
                moves.append((oid, old, new))

    return moves, infeasible


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------

def process_event(event_df: pd.DataFrame, problematic: set) -> tuple:
    occupied, free = resolve_seats(event_df)
    segments       = build_segments(occupied, free)

    # Map each order to the set of segments its seats appear in
    order_segments: dict = defaultdict(set)
    for (settore, fila, sp), seg in segments.items():
        for oid in seg['seats'].values():
            if oid in problematic:
                order_segments[oid].add((settore, fila, sp))

    # Orders whose seats span multiple segments cannot be fixed within our constraints
    globally_infeasible = {
        oid for oid, segs in order_segments.items() if len(segs) > 1
    }

    # Only attempt to fix orders fully contained in a single segment
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Concert seat reallocator')
    parser.add_argument(
        '--full-report', metavar='PATH',
        help='Path to the full-columns report CSV. When given, the output is '
             'data/report_annotated.xlsx with every row annotated with '
             'Nuovo posto and Stato. Without it, only moved/infeasible seats '
             'are written to data/reallocation.xlsx.',
    )
    args = parser.parse_args()
    full_report_path = args.full_report
    tickets_path = full_report_path or 'data/report_cleaned.csv'

    print('Loading tickets...', flush=True)
    tickets = load_tickets(tickets_path)
    print(f'  {len(tickets):,} valid rows across {tickets["Data evento"].nunique()} events.', flush=True)

    print('Loading problematic orders...', flush=True)
    orders_by_event = parse_orders('data/orders.txt')

    all_moves:      list = []
    all_infeasible: list = []   # list of (event_date, order_id)

    for event_date, event_df in tickets.groupby('Data evento'):
        problematic = orders_by_event.get(event_date, set())
        if not problematic:
            continue

        print(f'\nEvent {event_date}: {len(problematic)} problematic orders', flush=True)
        t0 = __import__('time').time()
        moves, infeasible = process_event(event_df, problematic)

        for m in moves:
            m['Data evento'] = event_date
        all_moves.extend(moves)
        all_infeasible.extend((event_date, oid) for oid in infeasible)

        fixed = len({m['Codice ordine'] for m in moves
                     if m['Data evento'] == event_date})
        elapsed = __import__('time').time() - t0
        print(f'  Fixed: {fixed}  |  Moves: {len(moves)}  |  Infeasible: {len(infeasible)}  |  {elapsed:.1f}s', flush=True)
        if infeasible:
            print(f'  Could not fix: {infeasible}', flush=True)

    active         = tickets[tickets['Stato posto'].isin(OCCUPIED)]
    infeasible_set = {(ed, oid) for ed, oid in all_infeasible}

    # --- Collateral detection (shared by both output modes) ---
    collateral_rows: list = []
    moves_by_event: dict  = defaultdict(list)
    for m in all_moves:
        moves_by_event[m['Data evento']].append(m)

    for event_date, event_active in active.groupby('Data evento'):
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

    if full_report_path:
        # ---------------------------------------------------------------
        # Full-report mode: annotate every row of the big file
        # ---------------------------------------------------------------
        with open(full_report_path, 'r', encoding='utf-8-sig') as f:
            first_line = f.readline()
        sep = ';' if ';' in first_line else ','
        big_df = pd.read_csv(
            full_report_path,
            sep=sep,
            low_memory=False,
            dtype={'Codice ordine': str, 'Data evento': str},
        )
        big_df['Codice ordine'] = big_df['Codice ordine'].astype(str)
        big_df['_posto_num'] = pd.to_numeric(big_df['Posto'], errors='coerce')

        # Build move lookup DataFrame
        if all_moves:
            move_df = pd.DataFrame(all_moves)[
                ['Data evento', 'Codice ordine', 'Posto originale', 'Posto nuovo', 'Stato']
            ].copy()
            move_df.rename(columns={'Posto originale': '_posto_num',
                                    'Posto nuovo':     '_nuovo_posto',
                                    'Stato':           '_stato'}, inplace=True)
            move_df['Codice ordine'] = move_df['Codice ordine'].astype(str)
        else:
            move_df = pd.DataFrame(
                columns=['Data evento', 'Codice ordine', '_posto_num', '_nuovo_posto', '_stato']
            )

        big_df = big_df.merge(
            move_df,
            on=['Data evento', 'Codice ordine', '_posto_num'],
            how='left',
        )

        # Infeasible mask (vectorised)
        big_df['_is_inf'] = [
            (ev, oid) in infeasible_set
            for ev, oid in zip(big_df['Data evento'], big_df['Codice ordine'])
        ]

        occupied_mask = big_df['Stato posto'].isin(OCCUPIED)
        has_move      = big_df['_stato'].notna()

        # Defaults
        big_df['Nuovo posto'] = big_df['_posto_num']
        big_df['Stato']       = 'NON COINVOLTO'

        # Infeasible occupied seats
        inf_mask = occupied_mask & big_df['_is_inf']
        big_df.loc[inf_mask, 'Stato'] = 'NON RISOLVIBILE'

        # Seats with an explicit move entry (SPOSTATO or COINVOLTO)
        big_df.loc[has_move, 'Nuovo posto'] = big_df.loc[has_move, '_nuovo_posto']
        big_df.loc[has_move, 'Stato']       = big_df.loc[has_move, '_stato']

        # CANCELLED / non-occupied always NON COINVOLTO
        big_df.loc[~occupied_mask, 'Stato']       = 'NON COINVOLTO'
        big_df.loc[~occupied_mask, 'Nuovo posto'] = big_df.loc[~occupied_mask, '_posto_num']

        # Convert Nuovo posto to nullable int
        big_df['Nuovo posto'] = pd.to_numeric(big_df['Nuovo posto'], errors='coerce').astype('Int64')

        # Drop temp columns, then insert Nuovo posto + Stato after Posto
        big_df.drop(columns=['_posto_num', '_nuovo_posto', '_stato', '_is_inf'],
                    inplace=True, errors='ignore')
        cols = list(big_df.columns)
        for c in ('Nuovo posto', 'Stato'):
            if c in cols:
                cols.remove(c)
        posto_idx = cols.index('Posto') + 1
        cols.insert(posto_idx,     'Nuovo posto')
        cols.insert(posto_idx + 1, 'Stato')
        big_df = big_df[cols]

        coll_cols = [
            'Codice ordine', 'Settore', 'Fila', 'Settore prezzi',
            'Posto originale', 'Posto nuovo', 'Stato',
        ]
        out_path = 'data/report_annotated.xlsx'
        with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
            for event_date, group in big_df.groupby('Data evento'):
                sheet_name = str(event_date).replace(':', '.').replace('/', '-')[:31]
                group.to_excel(writer, sheet_name=sheet_name, index=False)
            if collateral_rows:
                df_coll = pd.DataFrame(collateral_rows)
                df_coll[coll_cols].to_excel(writer, sheet_name='COLLATERALE', index=False)

        spostato  = (big_df['Stato'] == 'SPOSTATO').sum()
        coinvolto = (big_df['Stato'] == 'COINVOLTO').sum()
        non_ris   = (big_df['Stato'] == 'NON RISOLVIBILE').sum()
        print(f'\nAnnotated {len(big_df):,} rows -> {out_path}', flush=True)
        print(f'  SPOSTATO: {spostato}  COINVOLTO: {coinvolto}  NON RISOLVIBILE: {non_ris}', flush=True)
        if collateral_rows:
            print(f'  COLLATERALE: {len(collateral_rows)} orders -> sheet COLLATERALE', flush=True)

    else:
        # ---------------------------------------------------------------
        # Summary mode (default): only moved / infeasible seats
        # ---------------------------------------------------------------
        seen_inf: set = set()
        infeasible_rows: list = []
        for event_date, oid in all_infeasible:
            if (event_date, oid) in seen_inf:
                continue
            seen_inf.add((event_date, oid))
            order_seats = active[
                (active['Data evento'] == event_date) &
                (active['Codice ordine'] == oid)
            ]
            for _, row in order_seats.iterrows():
                infeasible_rows.append({
                    'Data evento':     event_date,
                    'Codice ordine':   oid,
                    'Settore':         row['Settore'],
                    'Fila':            row['Fila'],
                    'Settore prezzi':  row['Settore prezzi'],
                    'Posto originale': row['Posto'],
                    'Posto nuovo':     row['Posto'],
                    'Stato':           'NON RISOLVIBILE',
                })

        cols = [
            'Codice ordine', 'Settore', 'Fila', 'Settore prezzi',
            'Posto originale', 'Posto nuovo', 'Stato',
        ]
        all_rows = all_moves + infeasible_rows
        if all_rows:
            df_all = pd.DataFrame(all_rows)
            with pd.ExcelWriter('data/reallocation.xlsx', engine='openpyxl') as writer:
                for event_date, group in df_all.groupby('Data evento'):
                    sheet_name = str(event_date).replace(':', '.').replace('/', '-')[:31]
                    group[cols].to_excel(writer, sheet_name=sheet_name, index=False)
                if collateral_rows:
                    df_coll = pd.DataFrame(collateral_rows)
                    df_coll[cols].to_excel(writer, sheet_name='COLLATERALE', index=False)
            total_events = df_all['Data evento'].nunique()
            print(f'\nTotal rows: {len(all_moves)} moves + {len(infeasible_rows)} infeasible'
                  f' across {total_events} event sheets -> data/reallocation.xlsx', flush=True)
            if collateral_rows:
                print(f'  Collateral: {len(collateral_rows)} orders -> sheet COLLATERALE', flush=True)
        else:
            print('\nNo output generated.', flush=True)

        if all_infeasible:
            print(f'\nWARNING: {len(seen_inf)} orders could not be fixed.')
        if collateral_rows:
            print(f'WARNING: {len(collateral_rows)} previously-adjacent orders became'
                  f' non-adjacent as collateral displacement (dense segments, no free seats).',
                  flush=True)


if __name__ == '__main__':
    main()
