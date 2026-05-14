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

The entire algorithm lives in one file: [reallocate.py](reallocate.py). See [ARCHITECTURE.md](ARCHITECTURE.md) for a deep function-by-function walkthrough.

### Problem

Concert ticket orders sometimes have seats scattered across a row (e.g., seats 3, 7, 12). The tool rearranges who sits where so every problematic order ends up in a consecutive block, without changing anyone's sector, row (`Fila`), or price category (`Settore prezzi`).

### Decomposition

Each `(Settore, Fila, Settore prezzi)` triple is an independent 1-D subproblem. Orders whose seats span multiple triples are immediately flagged `NON RISOLVIBILE` — they cannot be fixed under the constraints.

### Solver (`solve_segment`)

The core is a **greedy warm-start + branch-and-bound** over contiguous block placements:

1. **Greedy warm-start** — places each problematic order at the nearest non-overlapping contiguous block (by centroid distance). Seeds `best` with an initial upper bound.
2. **Backtracking** — explores up to `MAX_BRANCHES=25` candidate blocks per order per level, pruning on cost. Hard time limit: **1 second per segment**.
3. **Objective** — lexicographic `(collateral, displacement)`: the search minimizes the number of previously-adjacent non-prob orders that become non-adjacent first, then minimizes total seat displacement.
4. **`simulate_collateral`** — called at each backtracking leaf to count collateral damage for a candidate placement, enabling the solver to prefer chain-displacement solutions over ones that scatter innocent orders.
5. **Non-prob assignment** — after prob orders are placed, all non-prob orders share a pool. Displaced orders (those with any seat in `prob_taken`) are assigned first (largest first), then intact orders reclaim their original seats.
6. **Fallback** — if backtracking finds no solution (timeout, no valid blocks), a greedy fallback assigns individual orders independently.

### Output (`reallocation.xlsx`)

One sheet per event date (colon→dot, slash→dash in sheet name), plus an optional `COLLATERALE` sheet.

| `Stato` value | Meaning |
|---|---|
| `SPOSTATO` | Seat successfully moved |
| `NON RISOLVIBILE` | Order infeasible (cross-segment seats, or no valid contiguous block found) |
| `COLLATERALE` | Order was adjacent before but non-adjacent after (collateral from dense segment) |

### Key constants

| Constant | Location | Effect |
|---|---|---|
| `MAX_BRANCHES = 25` | top of file | Candidate blocks tried per order per backtrack level — raise to improve quality at cost of speed |
| Time limit `1.0s` | `solve_segment` | Per-segment backtracking budget — raise if segments time out too early |
| `OCCUPIED` / `VALID` | top of file | Seat statuses treated as taken / kept after CSV filter |

### Input data format

**`data/report_cleaned.csv`** — columns: `Codice ordine, Stato ordine, Stato posto, Data evento, Item, Settore, Fila, Posto, Settore prezzi`. Separator auto-detected (`,` or `;`). Rows where `Stato posto ∉ {CONFIRMED, RESALE, CANCELLED}` are filtered out. A seat with any CONFIRMED/RESALE row is treated as occupied; one with only CANCELLED rows is treated as free.

**`data/orders.txt`** — one line per event, format: `"2026-05-30 21:00:00.0: ['order_id_1', 'order_id_2', ...]"` (Python list literal). Only orders listed here are targeted for fixing, but chain displacements may move non-listed adjacent orders to make room.
