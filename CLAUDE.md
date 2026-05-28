# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the workflow

```bash
# Activate the virtual environment (Python 3.14)
.venv\Scripts\activate

# Step 1: Standard reallocation pass
python reallocate.py data/report.csv
# With manual orders override:
python reallocate.py data/report.csv --orders data/orders.txt
# Custom output path (default: data/report_annotated.xlsx):
python reallocate.py data/report.csv --out data/my_output.xlsx

# Step 2: Capofila aisle-seat pass (run on the annotated output from step 1)
python reallocate_capofila.py data/report_annotated.xlsx
# Custom output path (default: data/report_capofila.xlsx):
python reallocate_capofila.py data/report_annotated.xlsx --out data/capofila_out.xlsx

# Step 3: Build flat reallocation report (one row per affected seat)
python build_reallocation_report.py data/report_capofila.xlsx
# Custom output path (default: data/reallocation_report.xlsx):
python build_reallocation_report.py data/report_capofila.xlsx --out data/my_realloc_report.xlsx

# Step 4: Build final post-reallocation report
python build_post_report.py data/report_capofila.xlsx data/updated_report.csv data/extra.csv --annullo-from 2026-05-01
# Custom output path (default: data/post_report.xlsx):
python build_post_report.py data/report_capofila.xlsx data/updated_report.csv data/extra.csv --annullo-from 2026-05-01 --out data/final.xlsx
```

All scripts must be run from the project root (`c:\dev\seat-reallocator\`).

## Installing dependencies

```bash
pip install -r requirements.txt
```

## Running tests

```bash
python -m pytest tests/ -v
```

## Architecture

All domain logic lives in the `seat_reallocator/` package. Entry-point scripts at the root are thin shells. See [ARCHITECTURE.md](ARCHITECTURE.md) for a full walkthrough.

```
seat_reallocator/
    config.py       all constants and tuning parameters
    io.py           load_csv, load_tickets, parse_orders
    geometry.py     contiguous_runs, is_adjacent, pair_seats
    seats.py        resolve_seats, build_segments
    engine.py       detect_non_consecutive_orders, process_event, detect_collateral
    capofila.py     build_occupied_current, fix_capofila_orders (chain-shift)
    reporter.py     write_full_report
    post_report.py  build (DF1 + DF2 + DF3 merge), main()
    cli.py          main() for reallocate.py
    exporter.py     export_swap_files
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

### Capofila pass

Capofila orders (those whose `Settore prezzi` contains `'capofila'`) have a fixed L/R aisle structure the ILP cannot resolve. `capofila.py` uses a chain-shift approach: for 3-seat orders, it tries relay, primary cascade, and secondary cascade strategies in ascending cost order.

### Output status values

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

**`data/report.csv`** — raw report export. Required columns: `Codice ordine, Stato ordine, Stato posto, Data evento, Item, Settore, Fila, Posto, Settore prezzi`. Optional: `Selezione in mappa` (rows with value `true` are excluded). Separator auto-detected (`,` or `;`). Rows where `Fila == "GA"` (General Admission) or `Stato posto ∉ {CONFIRMED, RESALE, CANCELLED}` are filtered out. A seat with any CONFIRMED/RESALE row is treated as occupied; one with only CANCELLED rows is treated as free.

Non-consecutive orders are auto-detected: an order is problematic if, within any `Settore prezzi` group, its seats span multiple `Settore`, multiple `Fila`, or non-consecutive `Posto` values.

**`data/orders.txt`** (optional override) — one line per event, format: `"2026-05-30 21:00:00.0: ['order_id_1', 'order_id_2', ...]"` (Python list literal). Pass via `--orders` to bypass auto-detection.
