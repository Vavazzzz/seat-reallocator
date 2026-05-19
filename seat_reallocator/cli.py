import argparse
import time

import pandas as pd

from .config import OCCUPIED
from .io import load_tickets, parse_orders
from .engine import process_event, detect_collateral
from .reporter import write_reallocation_report, write_full_report


def main():
    parser = argparse.ArgumentParser(description='Concert seat reallocator')
    parser.add_argument(
        '--full-report', metavar='PATH',
        help='Path to the full-columns report CSV. When given, the output is '
             'data/report_annotated.xlsx with every row annotated with '
             'Nuovo posto and Stato. Without it, only moved/infeasible seats '
             'are written to data/reallocation.xlsx.',
    )
    args = parser.parse_args()
    full_report_path = args.full_report
    tickets_path = full_report_path or 'data/report_cleaned.csv'

    print('Loading tickets...', flush=True)
    tickets = load_tickets(tickets_path)
    print(f'  {len(tickets):,} valid rows across {tickets["Data evento"].nunique()} events.', flush=True)

    print('Loading problematic orders...', flush=True)
    orders_by_event = parse_orders('data/orders.txt')

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

    if full_report_path:
        out_path = 'data/report_annotated.xlsx'
        big_df = write_full_report(full_report_path, all_moves, infeasible_set, collateral_rows,
                                   path=out_path)
        spostato  = (big_df['Stato'] == 'SPOSTATO').sum()
        coinvolto = (big_df['Stato'] == 'COINVOLTO').sum()
        non_ris   = (big_df['Stato'] == 'NON RISOLVIBILE').sum()
        print(f'\nAnnotated {len(big_df):,} rows -> {out_path}', flush=True)
        print(f'  SPOSTATO: {spostato}  COINVOLTO: {coinvolto}  NON RISOLVIBILE: {non_ris}', flush=True)
        if collateral_rows:
            print(f'  COLLATERALE: {len(collateral_rows)} orders -> sheet COLLATERALE', flush=True)

    else:
        seen_inf: set     = set()
        infeasible_rows: list = []
        for event_date, oid in all_infeasible:
            if (event_date, oid) in seen_inf:
                continue
            seen_inf.add((event_date, oid))
            order_seats = active[
                (active['Data evento'] == event_date) &
                (active['Codice ordine'] == oid)
            ]
            for _, row in order_seats.iterrows():
                infeasible_rows.append({
                    'Data evento':     event_date,
                    'Codice ordine':   oid,
                    'Settore':         row['Settore'],
                    'Fila':            row['Fila'],
                    'Settore prezzi':  row['Settore prezzi'],
                    'Posto originale': row['Posto'],
                    'Posto nuovo':     row['Posto'],
                    'Stato':           'NON RISOLVIBILE',
                })

        all_rows = all_moves + infeasible_rows
        write_reallocation_report(all_rows, collateral_rows)

        if all_rows:
            total_events = pd.DataFrame(all_rows)['Data evento'].nunique()
            print(
                f'\nTotal rows: {len(all_moves)} moves + {len(infeasible_rows)} infeasible'
                f' across {total_events} event sheets -> data/reallocation.xlsx',
                flush=True,
            )
            if collateral_rows:
                print(f'  Collateral: {len(collateral_rows)} orders -> sheet COLLATERALE', flush=True)
        else:
            print('\nNo output generated.', flush=True)

        if all_infeasible:
            print(f'\nWARNING: {len(seen_inf)} orders could not be fixed.')
        if collateral_rows:
            print(
                f'WARNING: {len(collateral_rows)} previously-adjacent orders became'
                f' non-adjacent as collateral displacement (dense segments, no free seats).',
                flush=True,
            )
