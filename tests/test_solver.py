"""
Unit tests for ILPSolver and BacktrackSolver.

Each scenario is tested against both solvers. The helper `_seats_after`
reconstructs the final seat assignment by applying moves so tests can assert
on adjacency rather than on specific seat numbers (which may vary between
solvers when multiple equally-good solutions exist).
"""
from collections import defaultdict

import pytest

from seat_reallocator.geometry import is_adjacent
from seat_reallocator.solver.backtrack import BacktrackSolver
from seat_reallocator.solver.ilp import ILPSolver

try:
    import pulp
    HAS_PULP = True
except ImportError:
    HAS_PULP = False

SOLVERS = [
    pytest.param(BacktrackSolver, id='backtrack'),
    pytest.param(ILPSolver,       id='ilp', marks=pytest.mark.skipif(not HAS_PULP, reason='pulp not installed')),
]


def _seats_after(original_seats: dict, moves: list) -> dict:
    """
    Reconstruct {order_id: sorted_seat_list} after applying moves.

    original_seats: {posto: order_id}
    moves: [(order_id, old_posto, new_posto), ...]
    """
    assignment: dict = defaultdict(list)
    for posto, oid in original_seats.items():
        assignment[oid].append(posto)

    for oid, old_p, new_p in moves:
        if old_p in assignment[oid]:
            assignment[oid].remove(old_p)
        if new_p not in assignment[oid]:
            assignment[oid].append(new_p)

    return {oid: sorted(ps) for oid, ps in assignment.items()}


# ---------------------------------------------------------------------------
# Already adjacent: nothing to do
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('solver_cls', SOLVERS)
def test_already_adjacent_no_moves(solver_cls):
    seats = {1: 'A', 2: 'A', 4: 'B', 5: 'B'}
    moves, inf = solver_cls().solve(seats, set(), {'A', 'B'})
    assert moves == []
    assert inf == []

@pytest.mark.parametrize('solver_cls', SOLVERS)
def test_single_seat_no_moves(solver_cls):
    seats = {3: 'A'}
    moves, inf = solver_cls().solve(seats, set(), {'A'})
    assert moves == []
    assert inf == []


# ---------------------------------------------------------------------------
# Fixable with free seats
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('solver_cls', SOLVERS)
def test_one_order_fixed_with_free_seat(solver_cls):
    # A at [1,3], free seat at 2
    seats = {1: 'A', 3: 'A'}
    moves, inf = solver_cls().solve(seats, {2}, {'A'})
    assert inf == []
    final = _seats_after(seats, moves)
    assert is_adjacent(final['A'])

@pytest.mark.parametrize('solver_cls', SOLVERS)
def test_three_seat_order_fixed(solver_cls):
    # A at [1,3,5] — all isolated, 2 free seats
    seats = {1: 'A', 3: 'A', 5: 'A'}
    moves, inf = solver_cls().solve(seats, {2, 4}, {'A'})
    assert inf == []
    final = _seats_after(seats, moves)
    assert is_adjacent(final['A'])


# ---------------------------------------------------------------------------
# Fixable only by swapping — no free seats
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('solver_cls', SOLVERS)
def test_two_orders_swap_to_fix(solver_cls):
    # A=[1,3], B=[2,4]. No free seats.
    # Only valid fix: A=[1,2]+B=[3,4] or A=[3,4]+B=[1,2]
    seats = {1: 'A', 3: 'A', 2: 'B', 4: 'B'}
    moves, inf = solver_cls().solve(seats, set(), {'A', 'B'})
    assert inf == []
    final = _seats_after(seats, moves)
    assert is_adjacent(final['A'])
    assert is_adjacent(final['B'])


# ---------------------------------------------------------------------------
# Non-problematic order should not be displaced unless necessary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('solver_cls', SOLVERS)
def test_nonprob_order_not_moved_when_avoidable(solver_cls):
    # A=[1,3] (non-adjacent, prob), C=[5] (single seat, not prob), free={2}
    # A can go to [1,2] or [2,3] without touching C
    seats = {1: 'A', 3: 'A', 5: 'C'}
    moves, inf = solver_cls().solve(seats, {2}, {'A'})
    assert inf == []
    assert not any(oid == 'C' for oid, _, _ in moves), 'C should not have been moved'


# ---------------------------------------------------------------------------
# Infeasible: no contiguous block of the required size exists
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('solver_cls', SOLVERS)
def test_infeasible_isolated_seats(solver_cls):
    # A at [1,3,5] — runs are [1],[3],[5], no 3-length block anywhere
    seats = {1: 'A', 3: 'A', 5: 'A'}
    moves, inf = solver_cls().solve(seats, set(), {'A'})
    assert 'A' in inf

@pytest.mark.parametrize('solver_cls', SOLVERS)
def test_infeasible_does_not_prevent_other_fixes(solver_cls):
    # A needs 4 consecutive seats but all 4 positions are isolated — no 4-length
    # contiguous run exists anywhere in the segment, so A is genuinely infeasible
    # for both solvers. B=[9,11] is independently fixable with free seat at 10.
    seats = {1: 'A', 3: 'A', 5: 'A', 7: 'A', 9: 'B', 11: 'B'}
    moves, inf = solver_cls().solve(seats, {10}, {'A', 'B'})
    assert 'A' in inf
    final = _seats_after(seats, moves)
    assert is_adjacent(final['B'])


# ---------------------------------------------------------------------------
# Empty segment: no orders in problematic_set that need fixing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('solver_cls', SOLVERS)
def test_problematic_set_not_in_seats(solver_cls):
    seats = {1: 'A', 2: 'A'}
    # 'X' is in problematic_set but not present in seats at all
    moves, inf = solver_cls().solve(seats, set(), {'X'})
    assert moves == []
    assert inf == []
