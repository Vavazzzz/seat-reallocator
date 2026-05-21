import argparse
import time

from seat_reallocator.io import load_tickets
from seat_reallocator.engine import detect_non_consecutive_orders
from seat_reallocator.seats import resolve_seats
from seat_reallocator.capofila import fix_capofila_orders
from seat_reallocator.reporter import write_full_report

_DEFAULT_OUT = 'data/report_capofila.xlsx'


def main():
    parser = argparse.ArgumentParser(description='Capofila aisle seat fixer')
    parser.add_argument('input', help='Path to the report XLSX or CSV')
    parser.add_argument(
        '--out', metavar='PATH', default=_DEFAULT_OUT,
        help=f'Output path (default: {_DEFAULT_OUT})',
    )
    args = parser.parse_args()

    print('Loading tickets...', flush=True)
    tickets = load_tickets(args.input)
    print(f'  {len(tickets):,} valid rows across {tickets["Data evento"].nunique()} events.', flush=True)

    print('Detecting non-consecutive orders...', flush=True)
    orders_by_event = detect_non_consecutive_orders(tickets)

    all_moves:      list = []
    all_infeasible: list = []

    for event_date, event_df in tickets.groupby('Data evento'):
        problematic = orders_by_event.get(event_date, set())
        if not problematic:
            continue

        # Keep only orders that have at least one Capofila ticket
        capofila_orders = [
            oid for oid in problematic
            if not event_df[
                (event_df['Codice ordine'] == str(oid)) &
                event_df['Settore prezzi'].str.contains('capofila', case=False, na=False)
            ].empty
        ]
        if not capofila_orders:
            continue

        print(f'\nEvent {event_date}: {len(capofila_orders)} Capofila orders', flush=True)
        t0 = time.time()

        occupied, _ = resolve_seats(event_df)
        moves, still_infeasible = fix_capofila_orders(
            event_df, capofila_orders, occupied, event_date,
        )

        all_moves.extend(moves)
        all_infeasible.extend((event_date, oid) for oid in still_infeasible)

        elapsed = time.time() - t0
        fixed = len({m['Codice ordine'] for m in moves})
        print(
            f'  Fixed: {fixed}  |  Moves: {len(moves)}  |  Infeasible: {len(still_infeasible)}'
            f'  |  {elapsed:.1f}s',
            flush=True,
        )
        if still_infeasible:
            print(f'  Could not fix: {still_infeasible}', flush=True)

    infeasible_set = {(ed, oid) for ed, oid in all_infeasible}
    big_df = write_full_report(args.input, all_moves, infeasible_set, [], path=args.out)
    spostato = (big_df['Stato'] == 'SPOSTATO').sum()
    non_ris  = (big_df['Stato'] == 'NON RISOLVIBILE').sum()
    print(f'\nAnnotated {len(big_df):,} rows -> {args.out}', flush=True)
    print(f'  SPOSTATO: {spostato}  NON RISOLVIBILE: {non_ris}', flush=True)

    if all_infeasible:
        print(f'\nWARNING: {len(infeasible_set)} Capofila orders could not be fixed.', flush=True)


if __name__ == '__main__':
    main()
