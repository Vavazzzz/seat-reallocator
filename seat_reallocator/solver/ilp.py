from collections import defaultdict

from ..config import INFEASIBLE_PENALTY, COLL_PENALTY, ILP_TIME_LIMIT
from ..geometry import contiguous_runs, is_adjacent, pair_seats
from .base import SegmentSolver
from .backtrack import BacktrackSolver


class ILPSolver(SegmentSolver):
    """
    Primary solver using Integer Linear Programming (pulp / CBC).

    Jointly optimises all orders in the segment in a single pass.
    Objective hierarchy (strictly ordered by penalty magnitude):
      1. Fix all problematic orders              (INFEASIBLE_PENALTY if skipped)
      2. Leave already-adjacent non-prob orders  (COLL_PENALTY if displaced)
      3. Minimise total seat displacement        (integer tiebreaker)

    Falls back to BacktrackSolver when pulp is unavailable or the ILP
    returns no solution within ILP_TIME_LIMIT seconds.
    """

    def solve(self, seats: dict, free_postos: set, problematic_set: set) -> tuple:
        try:
            import pulp
        except ImportError:
            print(
                'WARNING: pulp not available, falling back to backtracking solver'
                ' (may be slow and suboptimal)',
                flush=True,
            )
            return BacktrackSolver().solve(seats, free_postos, problematic_set)

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

        # Build candidates for every order (prob and non-prob alike)
        candidates: dict = {}
        for oid, postos in order_postos.items():
            blocks = get_blocks(len(postos))
            if oid in to_fix:
                orig = tuple(postos)
                if orig not in blocks:
                    # Dummy "infeasibility escape" block — chosen only when no
                    # contiguous block fits, paying INFEASIBLE_PENALTY
                    blocks = blocks + [orig]
            candidates[oid] = blocks

        mdl = pulp.LpProblem('seats', pulp.LpMinimize)
        oi  = {oid: i for i, oid in enumerate(order_postos)}

        x: dict = {
            (oid, b): pulp.LpVariable(f'x{oi[oid]}_{bi}', cat='Binary')
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

        # Three-tier objective
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

        mdl.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=ILP_TIME_LIMIT))

        if mdl.sol_status not in (1, 2):    # 1=Optimal, 2=IntegerFeasible (time limit)
            return BacktrackSolver().solve(seats, free_postos, problematic_set)

        # Extract the chosen block for each order
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
                for old, new in pair_seats(old_p, new_p):
                    moves.append((oid, old, new))

        return moves, infeasible
