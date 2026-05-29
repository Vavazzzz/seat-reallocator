import pandas as pd

from seat_reallocator.segments import resolve_seats, build_segments


def _df(rows):
    """Build a minimal tickets DataFrame from (Stato posto, Settore, Fila, Posto, Codice ordine, Settore prezzi)."""
    return pd.DataFrame(rows, columns=['Stato posto', 'Settore', 'Fila', 'Posto', 'Codice ordine', 'Settore prezzi'])


# ---------------------------------------------------------------------------
# resolve_seats
# ---------------------------------------------------------------------------

def test_resolve_confirmed_is_occupied():
    df = _df([('CONFIRMED', 'A', '1', 5, 'O1', 'PRIMO')])
    occupied, free = resolve_seats(df)
    assert ('A', '1', 5) in occupied
    assert occupied[('A', '1', 5)] == ('O1', 'PRIMO')
    assert free == {}

def test_resolve_resale_is_occupied():
    df = _df([('RESALE', 'A', '1', 5, 'O1', 'PRIMO')])
    occupied, free = resolve_seats(df)
    assert ('A', '1', 5) in occupied

def test_resolve_cancelled_is_free():
    df = _df([('CANCELLED', 'A', '1', 5, 'O1', 'PRIMO')])
    occupied, free = resolve_seats(df)
    assert occupied == {}
    assert ('A', '1', 5) in free
    assert free[('A', '1', 5)] == 'PRIMO'

def test_resolve_confirmed_overrides_cancelled():
    df = _df([
        ('CANCELLED', 'A', '1', 5, 'O1', 'PRIMO'),
        ('CONFIRMED', 'A', '1', 5, 'O1', 'PRIMO'),
    ])
    occupied, free = resolve_seats(df)
    assert ('A', '1', 5) in occupied
    assert ('A', '1', 5) not in free

def test_resolve_multiple_seats():
    df = _df([
        ('CONFIRMED', 'A', '1', 3, 'O1', 'PRIMO'),
        ('CONFIRMED', 'A', '1', 4, 'O1', 'PRIMO'),
        ('CANCELLED', 'A', '1', 5, 'O2', 'PRIMO'),
    ])
    occupied, free = resolve_seats(df)
    assert len(occupied) == 2
    assert len(free) == 1
    assert ('A', '1', 5) in free


# ---------------------------------------------------------------------------
# build_segments
# ---------------------------------------------------------------------------

def test_build_segments_single():
    occupied = {('A', '1', 5): ('O1', 'PRIMO')}
    segs = build_segments(occupied, {})
    assert ('A', '1', 'PRIMO') in segs
    assert segs[('A', '1', 'PRIMO')]['seats'] == {5: 'O1'}
    assert segs[('A', '1', 'PRIMO')]['free'] == set()

def test_build_segments_groups_by_triple():
    occupied = {
        ('A', '1', 5): ('O1', 'PRIMO'),
        ('A', '1', 6): ('O1', 'PRIMO'),
        ('B', '1', 1): ('O2', 'SECONDO'),
    }
    segs = build_segments(occupied, {})
    assert len(segs) == 2
    assert set(segs[('A', '1', 'PRIMO')]['seats'].keys()) == {5, 6}
    assert set(segs[('B', '1', 'SECONDO')]['seats'].keys()) == {1}

def test_build_segments_free_seats():
    occupied = {('A', '1', 5): ('O1', 'PRIMO')}
    free     = {('A', '1', 6): 'PRIMO', ('A', '1', 7): 'PRIMO'}
    segs = build_segments(occupied, free)
    assert segs[('A', '1', 'PRIMO')]['free'] == {6, 7}

def test_build_segments_different_price_same_row():
    # Same (Settore, Fila) but different Settore prezzi → two separate segments
    occupied = {
        ('A', '1', 5): ('O1', 'PRIMO'),
        ('A', '1', 6): ('O2', 'SECONDO'),
    }
    segs = build_segments(occupied, {})
    assert len(segs) == 2
    assert ('A', '1', 'PRIMO') in segs
    assert ('A', '1', 'SECONDO') in segs
