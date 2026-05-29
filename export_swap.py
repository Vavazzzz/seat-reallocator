#!/usr/bin/env python3
import argparse
from pathlib import Path

from seat_reallocator.reports.exporter import export_swap_files

_DEFAULT_INPUT  = 'data/report_annotated.xlsx'
_DEFAULT_OUTPUT = 'swap_output'


def main():
    parser = argparse.ArgumentParser(
        description='Export per-order public swap cards from an annotated xlsx',
    )
    parser.add_argument(
        'input', nargs='?', default=_DEFAULT_INPUT,
        help=f'Annotated xlsx (default: {_DEFAULT_INPUT})',
    )
    parser.add_argument(
        '--out', metavar='DIR', default=_DEFAULT_OUTPUT,
        help=f'Output directory (default: {_DEFAULT_OUTPUT})',
    )
    args = parser.parse_args()

    inp = Path(args.input)
    if not inp.exists():
        parser.error(f'Input file not found: {inp}')

    print(f'Reading {inp} …')
    export_swap_files(inp, Path(args.out))


if __name__ == '__main__':
    main()
