# Seat Reallocator — Architectural Walkthrough

> Complete internal documentation for developers who need to understand or modify the system.
> Reflects the modular package architecture.

---

## 1. High-level strategy

### The problem in plain terms

Each ticket order can cover multiple seats. The ticketing system sometimes assigns those seats non-consecutively (e.g., seats 3, 7, 12 in the same row), which causes physical seating problems at the venue. The tool must rearrange who sits where so that every "problematic" order ends up in a tight consecutive block — without changing anyone's sector, row, or price category.

### Overall approach

The primary solver is an **Integer Linear Program (ILP)** that jointly optimises seat assignments for every order in a segment in a single shot. The backtracking search is retained as a fallback (`BacktrackSolver`) used only when the `pulp` library is unavailable or the ILP finds no feasible solution.

The key insight driving decomposition is unchanged: a seat in `(Settore B, Row 7, price PRIMO)` can only be swapped with another seat in the exact same `(Settore, Fila, Settore prezzi)` triple. Every such triple is an independent 1-D sub-problem, solved separately.

### Main execution flow

```
parse_orders()      → {event_date: {order_ids}}
load_tickets()      → filtered DataFrame (auto-detects ; or , separator)
                       ↓
for each event_date:
  process_event()
    resolve_seats()     → occupied + free seat maps
    build_segments()    → independent (settore, fila, sp) sub-problems
    cross-segment check → immediately flag orders spanning >1 segment
    for each relevant segment:
      solve_segment()   [ILP path via ILPSolver]
        build binary variables x[order, block]
        add assignment constraints
        add seat-conflict constraints
        build tiered objective (INFEASIBLE_PENALTY / COLL_PENALTY / displacement)
        solve with CBC (ILP_TIME_LIMIT seconds)
        → on failure: BacktrackSolver [backtracking fallback]
        extract assignment, emit moves
                       ↓
detect_collateral()   → identify orders adjacent before but not after
                       ↓
write output:
  default mode    → data/reallocation.xlsx   (moved + infeasible seats only)
  --full-report   → data/report_annotated.xlsx (every row annotated)
```

---

## 2. Package structure

```
seat_reallocator/
    __init__.py         package marker
    config.py           all constants and tuning parameters
    io.py               CSV loading and orders-file parsing
    geometry.py         pure 1-D geometry helpers (no I/O, no pandas)
    seats.py            seat-state resolution and segment partitioning
    engine.py           per-event orchestration and collateral detection
    reporter.py         Excel output for both write modes
    cli.py              CLI entry point (argparse + main loop)
    solver/
        __init__.py     public solve_segment() entry point
        base.py         SegmentSolver abstract base class
        backtrack.py    BacktrackSolver — greedy warm-start + branch-and-bound
        ilp.py          ILPSolver — primary ILP solver (delegates to BT on failure)
reallocate.py           thin shell: `from seat_reallocator.cli import main; main()`
```

---

## 3. Module responsibilities

### `config.py`

All constants and tuning parameters in one place. Changing solver behaviour
(time budgets, penalty magnitudes, branch cap) requires editing only this file.

| Constant | Value | Meaning |
|---|---|---|
| `OCCUPIED` | `{'CONFIRMED', 'RESALE'}` | Seat statuses that count as taken |
| `VALID` | `{'CONFIRMED', 'RESALE', 'CANCELLED'}` | Statuses kept after CSV filter |
| `MAX_BRANCHES` | 25 | Candidate blocks per prob order per backtrack level (BT only) |
| `ILP_TIME_LIMIT` | 10 s | Per-segment CBC budget |
| `BT_TIME_LIMIT` | 1.0 s | Per-segment backtracking budget |
| `INFEASIBLE_PENALTY` | 1 000 000 | ILP cost for leaving a prob order non-adjacent |
| `COLL_PENALTY` | 10 000 | ILP cost for displacing an already-adjacent non-prob order |

---

### `io.py`

**Public interface**: `parse_orders(path)`, `load_tickets(path)`

Responsible for all file I/O. Auto-detects `;` vs `,` separator by peeking at
the first line of the CSV. Uses `ast.literal_eval` to safely parse the Python
list literal in `orders.txt`.

No business logic — pure data ingestion.

---

### `geometry.py`

**Public interface**: `contiguous_runs(positions)`, `is_adjacent(postos)`, `pair_seats(old_p, new_p)`

Pure 1-D integer geometry. No imports beyond builtins. Reusable without any
other part of the package.

- `contiguous_runs` — split a sorted int list into maximal consecutive runs.
- `is_adjacent` — check whether a list of seat numbers forms a consecutive block.
- `pair_seats` — match old seats to new seats minimising moves (keeps seats
  present in both lists fixed; pairs the rest in sorted order).

---

### `seats.py`

**Public interface**: `resolve_seats(event_df)`, `build_segments(occupied, free)`

Translates a raw pandas DataFrame for one event into the data structures used
by the solver.

- `resolve_seats` — determines the true status of each physical seat: OCCUPIED
  if any CONFIRMED/RESALE row exists, FREE if only CANCELLED rows exist.
- `build_segments` — partitions all seats into independent `(settore, fila,
  settore_prezzi)` sub-problems. Including `settore_prezzi` in the key
  structurally prevents swapping across price categories.

---

### `solver/base.py`

**Public interface**: `SegmentSolver` (ABC)

Defines the contract every solver must implement:

```python
def solve(self, seats: dict, free_postos: set, problematic_set: set) -> tuple:
    # returns (moves, infeasible)
```

Solvers are interchangeable — swap `ILPSolver` for `BacktrackSolver` (or a
future custom solver) by changing only `solver/__init__.py`.

---

### `solver/backtrack.py`

**Public interface**: `BacktrackSolver(SegmentSolver)`

Greedy warm-start + branch-and-bound search.

1. **Greedy warm-start** — places each problematic order at the nearest
   non-overlapping contiguous block (by centroid distance). Seeds `best`.
2. **Backtracking** — explores up to `MAX_BRANCHES` candidate blocks per order
   per level, pruning on cost. Hard time limit: `BT_TIME_LIMIT` seconds.
3. **Objective** — lexicographic `(collateral, displacement)`. `simulate_collateral`
   is called at each leaf to evaluate collateral damage for a candidate placement.
4. **Non-prob assignment** — after prob orders are placed, displaced orders get
   priority, then intact orders reclaim their original seats where possible.
5. **Fallback within fallback** — if backtracking finds nothing, a plain greedy
   pass runs independently.

---

### `solver/ilp.py`

**Public interface**: `ILPSolver(SegmentSolver)`

Primary solver. Formulates an Integer Linear Program over the full segment and
solves it with CBC (via `pulp`). Falls back to `BacktrackSolver` if `pulp` is
unavailable or the ILP returns no solution.

**Model structure**:
- One binary variable `x[order, block]` per `(order, candidate-block)` pair.
- Assignment constraint: each order gets exactly one block.
- Conflict constraint: each seat is claimed by at most one block.
- Three-tier objective (penalty magnitudes ensure strict priority ordering):

| Tier | Condition | Cost |
|---|---|---|
| 1st | Prob order cannot be fixed (dummy block chosen) | `INFEASIBLE_PENALTY` |
| 2nd | Adjacent non-prob order is displaced | `COLL_PENALTY + displacement` |
| 3rd | Prob order displacement (tiebreaker) | `displacement` |

---

### `solver/__init__.py`

**Public interface**: `solve_segment(seats, free_postos, problematic_set)`

Single public entry point for the solver subsystem. Currently delegates to
`ILPSolver`. To swap the primary strategy, change only this file.

---

### `engine.py`

**Public interface**: `process_event(event_df, problematic)`, `detect_collateral(active_df, all_moves, infeasible_set)`

Orchestrates per-event and post-processing work.

- `process_event` — resolves seats, builds segments, flags cross-segment
  infeasible orders, dispatches each relevant segment to `solve_segment`, and
  assembles move dicts. Returns `(all_moves, all_infeasible)`.
- `detect_collateral` — post-hoc pass that identifies orders that were adjacent
  before reallocation and non-adjacent after by replaying all moves on the
  original seat state.

---

### `reporter.py`

**Public interface**: `write_reallocation_report(all_rows, collateral_rows, path=...)`,
`write_full_report(source_path, all_moves, infeasible_set, collateral_rows, path=...)`

All Excel I/O in one place.

- `write_reallocation_report` — summary mode: one sheet per event with only
  moved/infeasible seats, plus optional `COLLATERALE` sheet.
- `write_full_report` — annotated mode: reads the full source CSV again, joins
  moves on `(Data evento, Codice ordine, posto_num)`, annotates every row with
  `Nuovo posto` and `Stato`, writes one sheet per event. Returns the annotated
  DataFrame for stat reporting in the CLI.

---

### `cli.py`

**Public interface**: `main()`

Argument parsing, the main event loop, progress printing, and coordination
between engine and reporter. No business logic — pure orchestration.

---

## 4. Dependency graph

```
cli.py
  ├── io.py              (load_tickets, parse_orders)
  ├── engine.py          (process_event, detect_collateral)
  │     ├── geometry.py  (is_adjacent)
  │     ├── seats.py     (resolve_seats, build_segments)
  │     │     └── config.py
  │     └── solver/
  │           ├── __init__.py   → ILPSolver
  │           ├── ilp.py        → BacktrackSolver (fallback)
  │           │     ├── geometry.py
  │           │     └── config.py
  │           └── backtrack.py
  │                 ├── geometry.py
  │                 └── config.py
  └── reporter.py        (write_reallocation_report, write_full_report)
        └── config.py    (OCCUPIED)
```

External dependencies: `pandas` (data loading/grouping), `pulp` (ILP solver —
optional, graceful fallback if absent), `openpyxl` (Excel output). `ast`,
`time`, `collections`, `argparse` from the standard library.

---

## 5. Function-by-function reference

### `parse_orders(path)` — `io.py`

**Output**: `dict[str, set[str]]` — `{event_date_str: set_of_order_ids}`

Each line in `orders.txt` has the format
`"2026-06-06 20:30:00.0: ['3406711', '3407635', ...]"`. Split on `': '` (limit
1), then `ast.literal_eval` safely parses the Python list literal.

---

### `load_tickets(path)` — `io.py`

**Output**: cleaned `pd.DataFrame` with `Posto` as `int`.

Peeks at the first line to detect `;` vs `,`. Filters to `VALID` statuses,
coerces `Posto` to numeric, drops rows with missing key columns.

---

### `resolve_seats(event_df)` — `seats.py`

**Output**: `occupied: {(settore, fila, posto): (order_id, sp)}`, `free: {(settore, fila, posto): sp}`

A seat is OCCUPIED if any CONFIRMED/RESALE row exists regardless of any
CANCELLED rows for the same seat.

---

### `build_segments(occupied, free)` — `seats.py`

**Output**: `{(settore, fila, sp): {'seats': {posto: order_id}, 'free': set[posto]}}`

Routes each seat to the segment keyed by `(settore, fila, settore_prezzi)`.
Including `settore_prezzi` in the key structurally forbids cross-price swaps.

---

### `contiguous_runs(positions)` — `geometry.py`

**Example**: `[1, 2, 3, 5, 6, 10]` → `[[1, 2, 3], [5, 6], [10]]`

Linear scan — extend current run if next position = `prev + 1`, otherwise open
a new run. Used by both solvers to enumerate candidate blocks.

---

### `is_adjacent(postos)` — `geometry.py`

Sort, then verify every adjacent pair differs by exactly 1.

---

### `pair_seats(old_p, new_p)` — `geometry.py`

Keeps seats present in both lists fixed (no move emitted), then zips remaining
positions in sorted order. Reduces noise in the output vs. naive `zip`.

---

### `ILPSolver.solve(seats, free_postos, problematic_set)` — `solver/ilp.py`

See section 3 for the full model description. Key steps:

1. Build `candidates` — all contiguous k-blocks for every order, plus a dummy
   "original" block for prob orders (the infeasibility escape hatch).
2. Create binary variables `x[oid, block]`.
3. Add assignment and conflict constraints.
4. Build three-tier objective.
5. Solve with `PULP_CBC_CMD(msg=0, timeLimit=ILP_TIME_LIMIT)`.
6. If `sol_status not in (1, 2)` → delegate to `BacktrackSolver`.
7. Extract assignment, identify infeasible orders (those assigned their dummy),
   invert to seat → order, emit moves via `pair_seats`.

---

### `BacktrackSolver.solve(seats, free_postos, problematic_set)` — `solver/backtrack.py`

1. Greedy warm-start seeds `best` with an initial `(collateral, displacement)`.
2. `backtrack(idx, taken, placement, partial_cost)` — recursive DFS. Prunes on
   displacement only once a zero-collateral solution exists.
3. `simulate_collateral(placement)` — called at every leaf; simulates the full
   non-prob greedy reassignment and counts formerly-adjacent orders that become
   non-adjacent.
4. `record_if_better` — lexicographic update of `best`.
5. After search: prob orders placed, infeasible locked in place, non-prob orders
   assigned from the remaining pool (displaced first, then intact).

---

### `process_event(event_df, problematic)` — `engine.py`

1. `resolve_seats` + `build_segments`.
2. Cross-segment check → `globally_infeasible`.
3. Per-segment loop → `solve_segment` → collect moves and infeasible.
4. Returns `(all_moves, all_infeasible)` without `'Data evento'` key; the
   caller (CLI) adds it.

---

### `detect_collateral(active_df, all_moves, infeasible_set)` — `engine.py`

Replays all moves on the original seat state to reconstruct the final seat
assignment, then flags any order that was adjacent before but non-adjacent after.
Detection condition: `not is_adjacent(orig_ps) or is_adjacent(final[oid])` —
skip if originally non-adjacent (not collateral) or still adjacent (no damage).

---

### `write_reallocation_report(...)` — `reporter.py`

Groups `all_rows` by `'Data evento'`, writes each group (minus `'Data evento'`
column) as one sheet. Appends `COLLATERALE` sheet if any.

---

### `write_full_report(...)` — `reporter.py`

Re-reads the source CSV, left-merges the move DataFrame on
`(Data evento, Codice ordine, posto_num)`, annotates each row with `Nuovo posto`
and `Stato`, drops temp columns, reorders so the two new columns follow `Posto`,
writes one sheet per event. Returns the annotated DataFrame.

---

### `main()` — `cli.py`

Argument parsing → ticket loading → event loop → collateral detection →
report writing. Pure orchestration; no domain logic.

---

## 6. Extension points

| Goal | Where to change |
|---|---|
| Add a new reallocation strategy | Subclass `SegmentSolver` in `solver/`, update `solver/__init__.py` |
| Change scoring rules | Edit penalty constants in `config.py` or override the objective build in `ILPSolver` |
| Support different venue layouts | Extend `build_segments` in `seats.py` with new decomposition keys |
| Add a validation pass | Insert a new function in `engine.py` between `resolve_seats` and `build_segments` |
| Extend reporting | Add a new function in `reporter.py` |
| Change output paths | Update defaults in `reporter.py` or pass `path=` from `cli.py` |

---

## 7. Output status values

| `Stato` value | Meaning |
|---|---|
| `SPOSTATO` | Seat was moved to a new number |
| `COINVOLTO` | Order was in the optimizer's assignment but seat number did not change |
| `NON RISOLVIBILE` | Order could not be made adjacent (cross-segment or no valid block) |
| `COLLATERALE` | Order was adjacent before but non-adjacent after (post-hoc detection) |
| `NON COINVOLTO` | Seat was not affected by any reallocation (full-report mode only) |

---

## 8. Input/output formats

### `data/report_cleaned.csv`

```
Codice ordine,Stato ordine,Stato posto,Data evento,Item,Settore,Fila,Posto,Settore prezzi
3406628,REGULAR,CONFIRMED,2026-06-27 21:00:00.0,...,Prato Gold,GA,3,PRATO GOLD
```

Separator auto-detected (`;` or `,`).

### `data/orders.txt`

```
2026-05-30 21:00:00.0: ['3417618', '3419185', ...]
2026-06-06 20:30:00.0: ['3406711', '3407635', ...]
```

One line per event. Python list literal after the colon.

### `data/reallocation.xlsx` (default mode)

One sheet per event. Columns: `Codice ordine, Settore, Fila, Settore prezzi, Posto originale, Posto nuovo, Stato`. Optional `COLLATERALE` sheet.

### `data/report_annotated.xlsx` (`--full-report` mode)

One sheet per event containing every row of the source file, with two added columns (`Nuovo posto`, `Stato`) inserted after `Posto`. Optional `COLLATERALE` sheet.

---

## 9. Known limitations

1. **ILP model size** grows quadratically with segment size — O(N²/k) variables for N seats and orders of size k.
2. **Silent fallback** — when the ILP fails, `BacktrackSolver` is invoked without logging which segment triggered it.
3. **`simulate_collateral` is O(N log N) per backtracking leaf** — can be slow for large segments in the BT fallback.
4. **Non-adjacent non-prob orders use centroid distance**, not exact displacement, in the ILP objective.
5. **Sheet name collision** — if two event dates produce the same 31-character truncated string, `openpyxl` will error.
6. **Full-report merge assumes unique `(event, order, seat)` rows** — duplicates in the source CSV will inflate counts.
