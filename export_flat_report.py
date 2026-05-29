#!/usr/bin/env python3
import argparse
from pathlib import Path

from seat_reallocator.reports.flat_report import build_reallocation_report

_DEFAULT_OUT = 'data/reallocation_report.xlsx'


def main():
    parser = argparse.ArgumentParser(
        description='Build flat reallocation report from annotated xlsx',
    )
    parser.add_argument(
        'input',
        help='Path to annotated xlsx (output of reallocate.py or reallocate_capofila.py)',
    )
    parser.add_argument(
        '--out', metavar='PATH', default=_DEFAULT_OUT,
        help=f'Output path (default: {_DEFAULT_OUT})',
    )
    args = parser.parse_args()

    n = build_reallocation_report(Path(args.input), Path(args.out))
    print(f'{n:,} rows written -> {args.out}')


if __name__ == '__main__':
    main()
