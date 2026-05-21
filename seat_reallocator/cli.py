import argparse
import time

from .config import OCCUPIED
from .io import load_tickets, parse_orders
from .engine import process_event, detect_collateral, detect_non_consecutive_orders
from .reporter import write_full_report


def main():
    parser = argparse.ArgumentParser(description='Concert seat reallocator')
    parser.add_argument(
        'input',
        help='Path to the report CSV (raw export or cleaned)',
    )
    parser.add_argument(
        '--orders', metavar='PATH',
        help='Optional orders.txt to override auto-detection of non-consecutive orders',
    )
    parser.add_argument(
        '--out', metavar='PATH', default='data/report_annotated.xlsx',
        help='Output path (default: data/report_annotated.xlsx)',
    )
    args = parser.parse_args()

    print('Loading tickets...', flush=True)
    tickets = load_tickets(args.input)
    print(f'  {len(tickets):,} valid rows across {tickets["Data evento"].nunique()} events.', flush=True)

    if args.orders:
        print('Loading problematic orders from file...', flush=True)
        orders_by_event = parse_orders(args.orders)
    else:
        print('Detecting non-consecutive orders...', flush=True)
        orders_by_event = detect_non_consecutive_orders(tickets)
        total = sum(len(v) for v in orders_by_event.values())
        print(f'  {total} problematic orders detected across {tickets["Data evento"].nunique()} events.', flush=True)

    all_moves:      list = []
    all_infeasible: list = []

    for event_date, event_df in tickets.groupby('Data evento'):
        problematic = orders_by_event.get(event_date, set())
        if not problematic:
            continue

        print(f'\nEvent {event_date}: {len(problematic)} problematic orders', flush=True)
        t0 = time.time()
        moves, infeasible = process_event(event_df, problematic)

        for m in moves:
            m['Data evento'] = event_date
        all_moves.extend(moves)
        all_infeasible.extend((event_date, oid) for oid in infeasible)

        fixed   = len({m['Codice ordine'] for m in moves if m['Data evento'] == event_date})
        elapsed = time.time() - t0
        print(
            f'  Fixed: {fixed}  |  Moves: {len(moves)}  |  Infeasible: {len(infeasible)}'
            f'  |  {elapsed:.1f}s',
            flush=True,
        )
        if infeasible:
            print(f'  Could not fix: {infeasible}', flush=True)

    active        = tickets[tickets['Stato posto'].isin(OCCUPIED)]
    infeasible_set = {(ed, oid) for ed, oid in all_infeasible}

    collateral_rows = detect_collateral(active, all_moves, infeasible_set)

    big_df = write_full_report(args.input, all_moves, infeasible_set, collateral_rows, path=args.out)
    spostato  = (big_df['Stato'] == 'SPOSTATO').sum()
    coinvolto = (big_df['Stato'] == 'COINVOLTO').sum()
    non_ris   = (big_df['Stato'] == 'NON RISOLVIBILE').sum()
    print(f'\nAnnotated {len(big_df):,} rows -> {args.out}', flush=True)
    print(f'  SPOSTATO: {spostato}  COINVOLTO: {coinvolto}  NON RISOLVIBILE: {non_ris}', flush=True)
    if collateral_rows:
        print(f'  COLLATERALE: {len(collateral_rows)} orders -> sheet COLLATERALE', flush=True)

    if all_infeasible:
        seen_inf = {(ed, oid) for ed, oid in all_infeasible}
        print(f'\nWARNING: {len(seen_inf)} orders could not be fixed.', flush=True)
    if collateral_rows:
        print(
            f'WARNING: {len(collateral_rows)} previously-adjacent orders became'
            f' non-adjacent as collateral displacement (dense segments, no free seats).',
            flush=True,
        )
