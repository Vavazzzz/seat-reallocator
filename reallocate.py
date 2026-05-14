#!/usr/bin/env python3
"""
Seat Reallocator

For each problematic order (seats in the same row but not consecutive),
finds a rearrangement within the same (Settore, Fila, Settore prezzi) segment
that makes all its seats adjacent, displacing as few other orders as possible.

Input:
  report_cleaned.csv  — all sold tickets
  orders.txt          — problematic order IDs grouped by event date

Output:
  reallocation.csv    — seat moves: (order, old_posto, new_posto) per event
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
# Segment solver
# ---------------------------------------------------------------------------

def solve_segment(seats: dict, free_postos: set, problematic_set: set) -> tuple:
    """
    Rearrange orders within a single (settore, fila, settore_prezzi) segment so
    that every problematic order occupies a contiguous run of seat numbers.

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
        for old, new in zip(old_p, new_p):
            if old != new:
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
                'Stato':           'SPOSTATO',
            })

    return all_moves, all_infeasible


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print('Loading tickets...', flush=True)
    tickets = load_tickets('report_cleaned.csv')
    print(f'  {len(tickets):,} valid rows across {tickets["Data evento"].nunique()} events.', flush=True)

    print('Loading problematic orders...', flush=True)
    orders_by_event = parse_orders('orders.txt')

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

    active = tickets[tickets['Stato posto'].isin(OCCUPIED)]

    # Build infeasible rows: one row per seat of each infeasible order
    infeasible_rows: list = []
    for event_date, oid in all_infeasible:
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

    # Detect collateral damage: orders that were adjacent but are now non-adjacent
    # after moves were applied (displaced as a side-effect of fixing prob orders).
    collateral_rows: list = []
    infeasible_set  = {(ed, oid) for ed, oid in all_infeasible}

    for event_date, event_active in active.groupby('Data evento'):
        if event_date not in {m['Data evento'] for m in all_moves}:
            continue

        # Original positions per order
        orig: dict = defaultdict(list)
        for _, row in event_active.iterrows():
            orig[row['Codice ordine']].append(row['Posto'])

        # Apply this event's moves to get final positions
        final: dict = {oid: list(ps) for oid, ps in orig.items()}
        for m in all_moves:
            if m['Data evento'] != event_date or m['Stato'] != 'SPOSTATO':
                continue
            oid = m['Codice ordine']
            op, np_ = m['Posto originale'], m['Posto nuovo']
            if op in final[oid]:
                final[oid].remove(op)
            final[oid].append(np_)

        for oid, orig_ps in orig.items():
            if (event_date, oid) in infeasible_set:
                continue
            if not is_adjacent(orig_ps):
                continue  # already non-adjacent before our tool ran
            if is_adjacent(final[oid]):
                continue  # still adjacent — fine

            # This order was adjacent before and non-adjacent after: collateral damage
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

    cols = [
        'Codice ordine', 'Settore', 'Fila', 'Settore prezzi',
        'Posto originale', 'Posto nuovo', 'Stato',
    ]
    all_rows = all_moves + infeasible_rows
    if all_rows:
        df_all = pd.DataFrame(all_rows)
        # Add collateral sheet separately (different column shape)
        with pd.ExcelWriter('reallocation.xlsx', engine='openpyxl') as writer:
            for event_date, group in df_all.groupby('Data evento'):
                sheet_name = str(event_date).replace(':', '.').replace('/', '-')[:31]
                group[cols].to_excel(writer, sheet_name=sheet_name, index=False)
            if collateral_rows:
                df_coll = pd.DataFrame(collateral_rows)
                df_coll[cols].to_excel(writer, sheet_name='COLLATERALE', index=False)
        total_events = df_all['Data evento'].nunique()
        print(f'\nTotal rows: {len(all_moves)} moves + {len(infeasible_rows)} infeasible'
              f' across {total_events} event sheets -> reallocation.xlsx', flush=True)
        if collateral_rows:
            print(f'  Collateral damage: {len(collateral_rows)} orders broken by displacement'
                  f' -> sheet COLLATERALE', flush=True)
    else:
        print('\nNo output generated.', flush=True)

    if all_infeasible:
        print(f'\nWARNING: {len(all_infeasible)} orders could not be fixed.')
    if collateral_rows:
        print(f'WARNING: {len(collateral_rows)} previously-adjacent orders became'
              f' non-adjacent as collateral displacement (dense segments, no free seats).',
              flush=True)


if __name__ == '__main__':
    main()
