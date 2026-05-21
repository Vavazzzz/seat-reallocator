#!/usr/bin/env python3
"""
Transform report_annotated.xlsx into per-event, per-ticket-count swap files.

Usage:
    python export_swap.py [input.xlsx] [output_dir]

Defaults:
    input      : data/report_annotated.xlsx
    output_dir : swap_output/
"""
import sys
from pathlib import Path

from seat_reallocator.exporter import export_swap_files

INPUT_DEFAULT  = Path('data/report_annotated.xlsx')
OUTPUT_DEFAULT = Path('swap_output')

if __name__ == '__main__':
    args = sys.argv[1:]
    inp  = Path(args[0]) if len(args) >= 1 else INPUT_DEFAULT
    out  = Path(args[1]) if len(args) >= 2 else OUTPUT_DEFAULT

    if not inp.exists():
        sys.exit(f'Input file not found: {inp}')

    print(f'Reading {inp} …')
    export_swap_files(inp, out)
