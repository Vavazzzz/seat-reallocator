# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the tool

```bash
# Activate the virtual environment (Python 3.14, dependencies: pandas, openpyxl, numpy)
.venv\Scripts\activate

# Run the seat reallocator
python reallocate.py

# Prepare a fresh report_cleaned.csv from a raw report.csv export
python clean.py
```

`reallocate.py` must be run from the project root (`c:\dev\seat-reallocator\`). It reads from `data/report_cleaned.csv` and `data/orders.txt`, and writes `reallocation.xlsx` to the project root. `clean.py` reads `report.csv` and writes `report_cleaned.csv` at the root — if you use it, move the output to `data/` before running `reallocate.py`.

There are no tests and no linter configuration.

## Architecture

The algorithm lives in the `seat_reallocator/` package. `reallocate.py` is a thin entry point. See [ARCHITECTURE.md](ARCHITECTURE.md) for a full walkthrough.

```
seat_reallocator/
    config.py       all constants and tuning parameters
    io.py           CSV loading, orders-file parsing
    geometry.py     contiguous_runs, is_adjacent, pair_seats
    seats.py        resolve_seats, build_segments
    engine.py       process_event, detect_collateral
    reporter.py     write_reallocation_report, write_full_report
    cli.py          main() + argparse
    solver/
        base.py     SegmentSolver ABC
        ilp.py      ILPSolver (primary — pulp/CBC)
        backtrack.py BacktrackSolver (fallback)
```

### Problem

Concert ticket orders sometimes have seats scattered across a row (e.g., seats 3, 7, 12). The tool rearranges who sits where so every problematic order ends up in a consecutive block, without changing anyone's sector, row (`Fila`), or price category (`Settore prezzi`).

### Decomposition

Each `(Settore, Fila, Settore prezzi)` triple is an independent 1-D subproblem. Orders whose seats span multiple triples are immediately flagged `NON RISOLVIBILE`.

### Primary solver (`ILPSolver`)

An Integer Linear Program that jointly optimises all orders in a segment. One binary variable per `(order, candidate-block)` pair. Three-tier objective (penalty magnitudes enforce strict priority):
1. Fix all problematic orders (`INFEASIBLE_PENALTY = 1 000 000` if skipped).
2. Leave already-adjacent non-prob orders undisturbed (`COLL_PENALTY = 10 000` if displaced).
3. Minimise total seat displacement (integer tiebreaker).

Falls back to `BacktrackSolver` if `pulp` is unavailable or the ILP finds no solution.

### Fallback solver (`BacktrackSolver`)

Greedy warm-start + branch-and-bound, `MAX_BRANCHES = 25` per level, `BT_TIME_LIMIT = 1.0 s` budget. Minimises `(collateral, displacement)` lexicographically.

### Output

One sheet per event date (colon→dot, slash→dash in sheet name), plus an optional `COLLATERALE` sheet.

| `Stato` value | Meaning |
|---|---|
| `SPOSTATO` | Seat successfully moved |
| `COINVOLTO` | Order in optimizer's assignment but seat number unchanged |
| `NON RISOLVIBILE` | Order infeasible (cross-segment seats, or no valid contiguous block found) |
| `COLLATERALE` | Order was adjacent before but non-adjacent after (collateral from dense segment) |
| `NON COINVOLTO` | Seat unaffected (full-report mode only) |

### Key constants

All in [`seat_reallocator/config.py`](seat_reallocator/config.py). Tuning any solver parameter requires editing only that file.

| Constant | Default | Effect |
|---|---|---|
| `MAX_BRANCHES` | 25 | Candidate blocks per order per backtrack level (BT only) |
| `ILP_TIME_LIMIT` | 10 s | Per-segment CBC budget |
| `BT_TIME_LIMIT` | 1.0 s | Per-segment backtracking budget |
| `INFEASIBLE_PENALTY` | 1 000 000 | ILP cost for leaving a prob order non-adjacent |
| `COLL_PENALTY` | 10 000 | ILP cost for displacing an already-adjacent non-prob order |

### Input data format

**`data/report_cleaned.csv`** — columns: `Codice ordine, Stato ordine, Stato posto, Data evento, Item, Settore, Fila, Posto, Settore prezzi`. Separator auto-detected (`,` or `;`). Rows where `Stato posto ∉ {CONFIRMED, RESALE, CANCELLED}` are filtered out. A seat with any CONFIRMED/RESALE row is treated as occupied; one with only CANCELLED rows is treated as free.

**`data/orders.txt`** — one line per event, format: `"2026-05-30 21:00:00.0: ['order_id_1', 'order_id_2', ...]"` (Python list literal). Only orders listed here are targeted for fixing, but chain displacements may move non-listed adjacent orders to make room.
