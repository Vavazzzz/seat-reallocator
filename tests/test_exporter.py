import pandas as pd

from seat_reallocator.exporter import pivot_order, process_sheet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(**kwargs) -> dict:
    defaults = {
        'Codice ordine': 'O1', 'Posto': '5', 'Stato': 'NON COINVOLTO',
        'Nuovo posto': '5', 'Item': 'SectorA', 'Settore': 'A', 'Fila': '1',
        'Codice supporto': 'BC01', 'Cognome partecipante': '', 'Nome partecipante': '',
        'Cognome': 'Rossi', 'Nome': 'Mario', 'Email': 'm@example.com',
    }
    return {**defaults, **kwargs}

def _group(*rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


# ---------------------------------------------------------------------------
# pivot_order: field mapping
# ---------------------------------------------------------------------------

def test_pivot_order_spostato_uses_nuovo_posto():
    group = _group(_row(Posto='5', Stato='SPOSTATO', **{'Nuovo posto': '7'}))
    row = pivot_order(group)
    assert row['vecchio_posto_01'] == '5'
    assert row['nuovo_posto_01'] == '7'

def test_pivot_order_non_spostato_keeps_posto():
    group = _group(_row(Posto='5', Stato='COINVOLTO', **{'Nuovo posto': '5'}))
    row = pivot_order(group)
    assert row['vecchio_posto_01'] == '5'
    assert row['nuovo_posto_01'] == '5'

def test_pivot_order_stato_case_insensitive():
    group = _group(_row(Posto='5', Stato='spostato', **{'Nuovo posto': '9'}))
    row = pivot_order(group)
    assert row['nuovo_posto_01'] == '9'

def test_pivot_order_order_level_fields():
    group = _group(_row(Cognome='Bianchi', Nome='Luca', Email='luca@example.com'))
    row = pivot_order(group)
    assert row['cognome'] == 'Bianchi'
    assert row['nome'] == 'Luca'
    assert row['email'] == 'luca@example.com'
    assert row['ordine'] == 'O1'

def test_pivot_order_two_tickets_numbered():
    group = _group(
        _row(Posto='3', Stato='SPOSTATO', **{'Nuovo posto': '4'}),
        _row(Posto='6', Stato='NON COINVOLTO', **{'Nuovo posto': '6', 'Codice ordine': 'O1'}),
    )
    row = pivot_order(group)
    # Sorted by Posto: 3 → _01, 6 → _02
    assert row['vecchio_posto_01'] == '3'
    assert row['nuovo_posto_01'] == '4'
    assert row['vecchio_posto_02'] == '6'
    assert row['nuovo_posto_02'] == '6'

def test_pivot_order_missing_columns_default_empty():
    group = pd.DataFrame([{'Codice ordine': 'O1', 'Posto': '5',
                           'Stato': 'NON COINVOLTO', 'Nuovo posto': '5'}])
    row = pivot_order(group)
    assert row['cognome'] == ''
    assert row['barcode_01'] == ''

def test_pivot_order_ticket_count():
    group = _group(
        _row(Posto='1'), _row(Posto='2'), _row(Posto='3'),
    )
    row = pivot_order(group)
    assert sum(1 for k in row if k.startswith('vecchio_posto_')) == 3


# ---------------------------------------------------------------------------
# process_sheet: filtering and grouping
# ---------------------------------------------------------------------------

def test_process_sheet_empty_no_spostato():
    df = pd.DataFrame([_row(Stato='NON COINVOLTO'), _row(Stato='COINVOLTO')])
    assert process_sheet(df) == {}

def test_process_sheet_returns_spostato_order():
    df = pd.DataFrame([_row(Stato='SPOSTATO', **{'Nuovo posto': '7'})])
    result = process_sheet(df)
    assert 1 in result
    assert len(result[1]) == 1

def test_process_sheet_includes_all_tickets_of_spostato_order():
    # Order O1 has 2 tickets; one is SPOSTATO → whole order appears
    df = pd.DataFrame([
        _row(Codice_ordine='O1', Posto='3', Stato='SPOSTATO', **{'Codice ordine': 'O1', 'Nuovo posto': '4'}),
        _row(Codice_ordine='O1', Posto='4', Stato='COINVOLTO', **{'Codice ordine': 'O1', 'Nuovo posto': '4'}),
    ])
    result = process_sheet(df)
    assert 2 in result
    assert len(result[2]) == 1

def test_process_sheet_groups_by_ticket_count():
    # O1: 1 ticket (SPOSTATO), O2: 2 tickets (one SPOSTATO)
    df = pd.DataFrame([
        {**_row(), 'Codice ordine': 'O1', 'Posto': '1', 'Stato': 'SPOSTATO', 'Nuovo posto': '2'},
        {**_row(), 'Codice ordine': 'O2', 'Posto': '3', 'Stato': 'SPOSTATO', 'Nuovo posto': '5'},
        {**_row(), 'Codice ordine': 'O2', 'Posto': '4', 'Stato': 'NON COINVOLTO', 'Nuovo posto': '4'},
    ])
    result = process_sheet(df)
    assert 1 in result and len(result[1]) == 1
    assert 2 in result and len(result[2]) == 1

def test_process_sheet_excludes_non_spostato_only_orders():
    # O1 only has COINVOLTO → not included. O2 has SPOSTATO → included.
    df = pd.DataFrame([
        {**_row(), 'Codice ordine': 'O1', 'Stato': 'COINVOLTO'},
        {**_row(), 'Codice ordine': 'O2', 'Stato': 'SPOSTATO', 'Nuovo posto': '9'},
    ])
    result = process_sheet(df)
    all_orders = {row['ordine'] for bucket in result.values() for _, row in bucket.iterrows()}
    assert 'O1' not in all_orders
    assert 'O2' in all_orders
