from .ilp import ILPSolver


def solve_segment(seats: dict, free_postos: set, problematic_set: set) -> tuple:
    """Solve one segment. Uses ILP with automatic backtracking fallback."""
    return ILPSolver().solve(seats, free_postos, problematic_set)
