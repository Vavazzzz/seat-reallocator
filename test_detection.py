"""
Quick comparison: detect_non_consecutive_orders vs a reference output.txt.
Fully-cancelled orders (no CONFIRMED/RESALE seats) are excluded from the
reference before comparing, since they should never be reallocated.

Usage:
    python test_detection.py <report_csv> <reference_output_txt>

Example:
    python test_detection.py data/report.csv ../check-consecutive-seats/data/output.txt
"""
import ast
import sys

from seat_reallocator.config import OCCUPIED
from seat_reallocator.io import load_tickets
from seat_reallocator.engine import detect_non_consecutive_orders


def parse_reference(path: str) -> dict[str, set[str]]:
    result = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            date_str, list_str = line.split(': ', 1)
            result[date_str] = set(str(x) for x in ast.literal_eval(list_str))
    return result


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    csv_path, ref_path = sys.argv[1], sys.argv[2]

    print(f'Loading tickets from {csv_path}...')
    tickets = load_tickets(csv_path)
    print(f'  {len(tickets):,} valid rows across {tickets["Data evento"].nunique()} events.')

    # Build set of orders that have at least one occupied seat, per event
    active = tickets[tickets['Stato posto'].isin(OCCUPIED)]
    occupied_orders: dict[str, set[str]] = {
        str(date): set(grp['Codice ordine'].astype(str))
        for date, grp in active.groupby('Data evento')
    }

    print('Running detect_non_consecutive_orders...')
    detected = detect_non_consecutive_orders(tickets)

    print(f'Parsing reference from {ref_path}...')
    reference = parse_reference(ref_path)

    # Filter reference to only orders with at least one occupied seat
    reference = {
        date: orders & occupied_orders.get(date, set())
        for date, orders in reference.items()
    }

    all_dates = sorted(set(detected) | set(reference))
    total_missing = 0
    total_extra = 0

    for date in all_dates:
        det = detected.get(date, set())
        ref = reference.get(date, set())
        missing = ref - det   # in reference but not detected
        extra   = det - ref   # detected but not in reference
        if missing or extra:
            print(f'\n  {date}:')
            if missing:
                print(f'    MISSING  ({len(missing):3d}): {sorted(missing)[:10]}{"..." if len(missing) > 10 else ""}')
            if extra:
                print(f'    EXTRA    ({len(extra):3d}): {sorted(extra)[:10]}{"..." if len(extra) > 10 else ""}')
            total_missing += len(missing)
            total_extra   += len(extra)
        else:
            print(f'  {date}: OK ({len(det)} orders)')

    print()
    if total_missing == 0 and total_extra == 0:
        print('PASS — detected orders match reference exactly (cancelled-only orders excluded).')
    else:
        print(f'FAIL — {total_missing} missing, {total_extra} extra across all events.')
        sys.exit(1)


if __name__ == '__main__':
    main()
