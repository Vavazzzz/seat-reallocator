from abc import ABC, abstractmethod


class SegmentSolver(ABC):
    """
    Abstract base for seat-segment solvers.

    All implementations receive the same inputs and return the same output,
    making strategies fully interchangeable.
    """

    @abstractmethod
    def solve(self, seats: dict, free_postos: set, problematic_set: set) -> tuple:
        """
        Assign seats within a single (settore, fila, settore_prezzi) segment so
        that every problematic order ends up in a contiguous block.

        Args:
            seats:          {posto: order_id} — currently occupied seats.
            free_postos:    set of unoccupied seat numbers in the segment.
            problematic_set: order IDs targeted for fixing.

        Returns:
            moves:      [(order_id, old_posto, new_posto), ...]
            infeasible: [order_id, ...]  — orders that could not be made adjacent.
        """
        ...
