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


def pair_seats(old_p: list, new_p: list) -> list:
    """
    Match old seats to new seats minimising the number that move.
    Seats present in both lists stay fixed; the rest are paired in sorted order.
    Returns [(old_seat, new_seat), ...].
    """
    kept = set(old_p) & set(new_p)
    remaining_old = sorted(set(old_p) - kept)
    remaining_new = sorted(set(new_p) - kept)
    return [(s, s) for s in sorted(kept)] + list(zip(remaining_old, remaining_new))
