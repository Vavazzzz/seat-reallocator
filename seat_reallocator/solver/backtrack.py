import time
from collections import defaultdict

from ..config import MAX_BRANCHES, BT_TIME_LIMIT
from ..geometry import contiguous_runs, is_adjacent, pair_seats
from .base import SegmentSolver


class BacktrackSolver(SegmentSolver):
    """
    Greedy warm-start + branch-and-bound backtracking solver.

    Used as a fallback when the ILP solver is unavailable or finds no solution.
    Minimises (collateral, displacement) lexicographically within a 1-second budget.
    """

    def solve(self, seats: dict, free_postos: set, problematic_set: set) -> tuple:
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

        prob_list = sorted(to_fix.items(), key=lambda x: -len(x[1]))

        best: dict = {'collateral': float('inf'), 'displacement': float('inf'), 'placement': None}

        def simulate_collateral(placement: dict) -> int:
            prob_tk   = {p for postos in placement.values() for p in postos}
            remaining = set(p for p in all_pos if p not in prob_tk)
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
                    if is_adjacent(old_p):
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
                best['collateral']   = coll
                best['displacement'] = displacement
                best['placement']    = dict(placement)

        # Greedy warm start — seeds best with an initial upper bound
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
            record_if_better(
                g_placement,
                sum(step_cost(g_placement[oid], op) for oid, op in prob_list),
            )

        deadline = time.time() + BT_TIME_LIMIT

        def backtrack(idx: int, taken: set, placement: dict, partial_cost: int):
            if time.time() > deadline:
                return
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

        # Double fallback — if backtracking found nothing, run a plain greedy pass
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

        # 2. Infeasible prob orders stay in place so their seats are not claimed later
        for oid in infeasible:
            for p in to_fix[oid]:
                new_asgn[p] = oid

        # 3. Non-prob orders share a pool; displaced orders get priority
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
                for old, new in pair_seats(old_p, new_p):
                    moves.append((oid, old, new))

        return moves, infeasible
