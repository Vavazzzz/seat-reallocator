import pandas as pd

from seat_reallocator.engine import detect_non_consecutive_orders


def _df(rows):
    """Build a minimal tickets DataFrame."""
    return pd.DataFrame(
        rows,
        columns=['Data evento', 'Codice ordine', 'Settore prezzi', 'Settore', 'Fila', 'Posto', 'Stato posto'],
    )


EVENT = '2026-01-01'


# ---------------------------------------------------------------------------
# Single-seat orders: never problematic
# ---------------------------------------------------------------------------

def test_single_seat_not_detected():
    df = _df([(EVENT, 'O1', 'PRIMO', 'A', '1', 3, 'CONFIRMED')])
    assert 'O1' not in detect_non_consecutive_orders(df).get(EVENT, set())


# ---------------------------------------------------------------------------
# Adjacent multi-seat orders: not problematic
# ---------------------------------------------------------------------------

def test_two_consecutive_seats_not_detected():
    df = _df([
        (EVENT, 'O1', 'PRIMO', 'A', '1', 3, 'CONFIRMED'),
        (EVENT, 'O1', 'PRIMO', 'A', '1', 4, 'CONFIRMED'),
    ])
    assert 'O1' not in detect_non_consecutive_orders(df).get(EVENT, set())

def test_three_consecutive_seats_not_detected():
    df = _df([
        (EVENT, 'O1', 'PRIMO', 'A', '1', 5, 'CONFIRMED'),
        (EVENT, 'O1', 'PRIMO', 'A', '1', 6, 'CONFIRMED'),
        (EVENT, 'O1', 'PRIMO', 'A', '1', 7, 'CONFIRMED'),
    ])
    assert 'O1' not in detect_non_consecutive_orders(df).get(EVENT, set())


# ---------------------------------------------------------------------------
# Non-adjacent seats: problematic
# ---------------------------------------------------------------------------

def test_gap_in_seats_detected():
    df = _df([
        (EVENT, 'O1', 'PRIMO', 'A', '1', 3, 'CONFIRMED'),
        (EVENT, 'O1', 'PRIMO', 'A', '1', 5, 'CONFIRMED'),
    ])
    assert 'O1' in detect_non_consecutive_orders(df).get(EVENT, set())

def test_three_scattered_seats_detected():
    df = _df([
        (EVENT, 'O1', 'PRIMO', 'A', '1', 1, 'CONFIRMED'),
        (EVENT, 'O1', 'PRIMO', 'A', '1', 4, 'CONFIRMED'),
        (EVENT, 'O1', 'PRIMO', 'A', '1', 9, 'CONFIRMED'),
    ])
    assert 'O1' in detect_non_consecutive_orders(df).get(EVENT, set())


# ---------------------------------------------------------------------------
# Cross-settore and cross-fila: always problematic
# ---------------------------------------------------------------------------

def test_multiple_settore_detected():
    df = _df([
        (EVENT, 'O1', 'PRIMO', 'A', '1', 3, 'CONFIRMED'),
        (EVENT, 'O1', 'PRIMO', 'B', '1', 4, 'CONFIRMED'),  # different Settore
    ])
    assert 'O1' in detect_non_consecutive_orders(df).get(EVENT, set())

def test_multiple_fila_detected():
    df = _df([
        (EVENT, 'O1', 'PRIMO', 'A', '1', 3, 'CONFIRMED'),
        (EVENT, 'O1', 'PRIMO', 'A', '2', 4, 'CONFIRMED'),  # different Fila
    ])
    assert 'O1' in detect_non_consecutive_orders(df).get(EVENT, set())


# ---------------------------------------------------------------------------
# Selezione in mappa: excluded from detection
# ---------------------------------------------------------------------------

def test_selezione_in_mappa_excluded():
    df = _df([
        (EVENT, 'O1', 'PRIMO', 'A', '1', 3, 'CONFIRMED'),
        (EVENT, 'O1', 'PRIMO', 'A', '1', 5, 'CONFIRMED'),
    ])
    df['Selezione in mappa'] = 'true'
    result = detect_non_consecutive_orders(df)
    # All rows excluded — event may not appear or set is empty
    assert 'O1' not in result.get(EVENT, set())

def test_selezione_in_mappa_partial():
    # One non-consecutive order is excluded; a normal one is still detected
    df = _df([
        (EVENT, 'O1', 'PRIMO', 'A', '1', 1, 'CONFIRMED'),
        (EVENT, 'O1', 'PRIMO', 'A', '1', 3, 'CONFIRMED'),
        (EVENT, 'O2', 'PRIMO', 'A', '1', 5, 'CONFIRMED'),
        (EVENT, 'O2', 'PRIMO', 'A', '1', 7, 'CONFIRMED'),
    ])
    df['Selezione in mappa'] = 'false'
    df.loc[df['Codice ordine'] == 'O1', 'Selezione in mappa'] = 'true'
    result = detect_non_consecutive_orders(df)
    assert 'O1' not in result.get(EVENT, set())
    assert 'O2' in result.get(EVENT, set())


# ---------------------------------------------------------------------------
# Multiple events: results are isolated per event
# ---------------------------------------------------------------------------

def test_multiple_events_isolated():
    df = _df([
        ('2026-01-01', 'O1', 'PRIMO', 'A', '1', 1, 'CONFIRMED'),
        ('2026-01-01', 'O1', 'PRIMO', 'A', '1', 3, 'CONFIRMED'),  # non-consecutive
        ('2026-01-02', 'O2', 'PRIMO', 'A', '1', 5, 'CONFIRMED'),
        ('2026-01-02', 'O2', 'PRIMO', 'A', '1', 6, 'CONFIRMED'),  # consecutive
    ])
    result = detect_non_consecutive_orders(df)
    assert 'O1' in result.get('2026-01-01', set())
    assert 'O2' not in result.get('2026-01-02', set())
