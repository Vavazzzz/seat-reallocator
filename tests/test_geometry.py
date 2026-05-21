from seat_reallocator.geometry import contiguous_runs, is_adjacent, pair_seats


# ---------------------------------------------------------------------------
# contiguous_runs
# ---------------------------------------------------------------------------

def test_contiguous_runs_empty():
    assert contiguous_runs([]) == []

def test_contiguous_runs_single():
    assert contiguous_runs([5]) == [[5]]

def test_contiguous_runs_single_run():
    assert contiguous_runs([1, 2, 3]) == [[1, 2, 3]]

def test_contiguous_runs_multiple_runs():
    assert contiguous_runs([1, 2, 3, 5, 6, 10]) == [[1, 2, 3], [5, 6], [10]]

def test_contiguous_runs_all_isolated():
    assert contiguous_runs([1, 3, 5]) == [[1], [3], [5]]

def test_contiguous_runs_gap_of_two():
    assert contiguous_runs([1, 2, 4, 5]) == [[1, 2], [4, 5]]


# ---------------------------------------------------------------------------
# is_adjacent
# ---------------------------------------------------------------------------

def test_is_adjacent_empty():
    assert is_adjacent([]) is True

def test_is_adjacent_single():
    assert is_adjacent([7]) is True

def test_is_adjacent_two_consecutive():
    assert is_adjacent([3, 4]) is True

def test_is_adjacent_unsorted_consecutive():
    assert is_adjacent([5, 3, 4]) is True

def test_is_adjacent_three_consecutive():
    assert is_adjacent([3, 4, 5]) is True

def test_is_adjacent_gap():
    assert is_adjacent([3, 5]) is False

def test_is_adjacent_multi_gap():
    assert is_adjacent([1, 3, 5]) is False

def test_is_adjacent_duplicates_not_adjacent():
    # [2, 2, 3] sorted → [2, 2, 3]; 2+1 == 2 is False → not adjacent
    assert is_adjacent([2, 2, 3]) is False


# ---------------------------------------------------------------------------
# pair_seats
# ---------------------------------------------------------------------------

def test_pair_seats_identical():
    assert pair_seats([1, 2, 3], [1, 2, 3]) == [(1, 1), (2, 2), (3, 3)]

def test_pair_seats_full_shift():
    # Nothing in common → zip sorted remaining
    result = pair_seats([1, 2], [3, 4])
    assert set(result) == {(1, 3), (2, 4)}

def test_pair_seats_keeps_common():
    # 5 stays, 6 moves to 7
    result = pair_seats([5, 6], [5, 7])
    assert (5, 5) in result
    assert (6, 7) in result

def test_pair_seats_keeps_right_common():
    # [5,6] → [4,6]: 6 stays, 5 moves to 4
    result = pair_seats([5, 6], [4, 6])
    assert (6, 6) in result
    assert (5, 4) in result

def test_pair_seats_all_common():
    result = pair_seats([10, 11, 12], [10, 11, 12])
    assert result == [(10, 10), (11, 11), (12, 12)]

def test_pair_seats_length_preserved():
    for old, new in [([1, 2, 3], [3, 4, 5]), ([5], [9]), ([1, 2], [2, 3])]:
        assert len(pair_seats(old, new)) == len(old)
