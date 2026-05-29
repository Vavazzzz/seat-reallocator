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

### Workflow scripts

```
reallocate.py          Step 1 — Standard reallocation pass (ILP / backtrack solver)
reallocate_capofila.py Step 2 — Capofila chain-shift pass (run on output of step 1)
export_flat_report.py  Step 3 — Flat per-seat report (Codice ordine, names, seat cols, Stato)
export_post_report.py  Step 4 — DF1+DF2+DF3 merge with updated ticket data
export_swap_map.py     Extra  — Per-seat physical swap map (old order → new order)
export_swap.py         Extra  — Per-order public cards (wide format, one file per event)
gui.py                 Desktop GUI wrapping all of the above
```

Steps 1 and 2 both produce an annotated `.xlsx`; step 3 reads that output and emits a focused reallocation summary; step 4 consumes the annotated output together with a fresh ticket export.

### Main execution flow (reallocate.py)

```
load_tickets()              → filtered DataFrame
detect_non_consecutive_orders() → {event_date: {order_ids}}
                              ↓
for each event_date:
  process_event()
    resolve_seats()           → occupied + free seat maps
    build_segments()          → independent (settore, fila, sp) sub-problems
    cross-segment check       → immediately flag cross-segment orders
    for each relevant segment:
      solve_segment()         [ILP path via ILPSolver]
        build binary vars x[order, block]
        add assignment + conflict constraints
        build tiered objective (INFEASIBLE_PENALTY / COLL_PENALTY / displacement)
        solve with CBC (ILP_TIME_LIMIT seconds)
        → on failure: BacktrackSolver [backtracking fallback]
        extract assignment, emit moves
                              ↓
detect_collateral()         → flag orders adjacent before but non-adjacent after
                              ↓
write_full_report()         → annotated xlsx (one sheet per event)
```

---

## 2. Package structure

```
seat_reallocator/
    __init__.py             package marker
    config.py               all constants and tuning parameters
    io.py                   CSV/Excel loading and orders-file parsing
    geometry.py             pure 1-D geometry helpers (no I/O, no pandas)
    segments.py             seat-state resolution and segment partitioning
    engine.py               per-event orchestration, detection, collateral
    capofila.py             chain-shift fixer for Capofila aisle orders
    cli.py                  CLI entry point for reallocate.py
    solver/
        __init__.py         public solve_segment() entry point
        base.py             SegmentSolver abstract base class
        backtrack.py        BacktrackSolver — greedy warm-start + branch-and-bound
        ilp.py              ILPSolver — primary ILP solver (delegates to BT on failure)
    reports/
        __init__.py         package marker
        annotator.py        annotate source file with move outcomes (write_full_report)
        flat_report.py      flat per-seat summary (build_reallocation_report)
        swap_map.py         per-seat physical swap map: old order → new order (build)
        post_report.py      DF1+DF2+DF3 merge report (build, main)
        exporter.py         per-order public cards, wide format (export_swap_files)

reallocate.py               thin shell → seat_reallocator.cli.main()
reallocate_capofila.py      thin shell → capofila workflow
export_flat_report.py       thin shell → seat_reallocator.reports.flat_report
export_post_report.py       thin shell → seat_reallocator.reports.post_report.main()
export_swap_map.py          thin shell → seat_reallocator.reports.swap_map.main()
export_swap.py              thin shell → seat_reallocator.reports.exporter.export_swap_files()
gui.py                      desktop GUI (customtkinter)
tools/
    test_detection.py       CLI: compare detect_non_consecutive_orders vs reference output
    test_reallocation.py    CLI: run full pipeline and diff against reference xlsx
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

**Public interface**: `load_csv(path, **kwargs)`, `parse_orders(path)`, `load_tickets(path)`

Responsible for all file I/O. `load_csv` auto-detects `;` vs `,` by peeking at
the first line; it is the shared low-level loader used by `load_tickets`,
`reporter.py`, and `post_report.py`. `load_tickets` additionally filters out
GA rows, non-VALID statuses, and coerces seat/row numbers to integers.

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

### `segments.py`

**Public interface**: `resolve_seats(event_df)`, `build_segments(occupied, free)`

Translates a raw pandas DataFrame for one event into the data structures used
by the solver.

- `resolve_seats` — determines the true status of each physical seat: OCCUPIED
  if any CONFIRMED/RESALE row exists, FREE if only CANCELLED rows exist.
- `build_segments` — partitions all seats into independent `(settore, fila,
  settore_prezzi)` sub-problems. Including `settore_prezzi` in the key
  structurally prevents swapping across price categories.

---

### `engine.py`

**Public interface**: `detect_non_consecutive_orders(df)`, `process_event(event_df, problematic)`, `detect_collateral(active_df, all_moves, infeasible_set)`

Orchestrates per-event and post-processing work.

- `detect_non_consecutive_orders` — scans all orders in the loaded DataFrame and
  returns `{event_date: set(order_ids)}` for orders whose seats span multiple
  sectors, multiple rows, or non-consecutive seat numbers within a price group.
- `process_event` — resolves seats, builds segments, flags cross-segment
  infeasible orders, dispatches each relevant segment to `solve_segment`, and
  assembles move dicts. Returns `(all_moves, all_infeasible)`.
- `detect_collateral` — post-hoc pass that identifies orders that were adjacent
  before reallocation and non-adjacent after by replaying all moves on the
  original seat state.

---

### `capofila.py`

**Public interface**: `build_occupied_current(event_df)`, `fix_capofila_orders(event_df, order_ids, occupied, event_date)`

Handles "Capofila" aisle-seat orders — orders whose `Settore prezzi` contains
the string `'capofila'`. These orders have a special L/R aisle structure that
the standard ILP solver cannot fix; instead a chain-shift approach is used.

- `build_occupied_current` — builds `{(settore, fila, posto): (order_id, sp, original_posto)}`
  keyed on `Nuovo posto` when present (so chain shifts operate on post-reallocation
  positions, not original positions).
- `_detect_sides` — infers which seat positions are left-aisle vs right-aisle by
  splitting the sorted distinct `Posto` values for each `(Settore, Settore prezzi)`
  capofila group at the midpoint.
- `fix_capofila_orders` — for each non-consecutive 3-seat capofila order, tries
  three strategies (relay, primary cascade, secondary cascade) in ascending cost
  order. 4-seat orders are left unchanged. Returns `(capofila_moves, still_infeasible)`.

---

### `annotator.py`

**Public interface**: `write_full_report(source_path, all_moves, infeasible_set, collateral_rows, path=...)`

Reads the full source file again, left-merges the move DataFrame on
`(Data evento, Codice ordine, posto_num)`, annotates every row with `Nuovo posto`
and `Stato`, drops temp columns, reorders so the new columns follow `Posto`,
writes one sheet per event. Returns the annotated DataFrame for stat reporting.

---

### `flat_report.py`

**Public interface**: `build_reallocation_report(input_path, output_path)`

Reads all event sheets from an annotated xlsx, drops `NON COINVOLTO` rows,
selects a fixed column subset (`Codice ordine`, `Cognome`, `Nome`, `Settore`,
`Fila`, `Posto`, `Nuovo posto`, `Stato`, `Settore prezzi`, participant names),
and writes a single sorted sheet. Returns the row count written.

---

### `swap_map.py`

**Public interface**: `build(source_path, out_path)`, `main()`

Builds a per-seat physical swap map from an annotated xlsx: for each physical
seat touched by a `SPOSTATO` move, emits one row with the old occupant and the
new occupant side by side. Keyed on `(Settore, Fila, Posto)` — useful for
venue staff who need to know what changed at each seat position.

---

### `post_report.py`

**Public interface**: `build(annotated_path, updated_report_path, extra_path, annullo_from, out_path)`, `main()`

Produces the final post-reallocation report by combining three data sources:

1. **DF1** — `report_annotated.xlsx` (output of `reallocate.py` or `reallocate_capofila.py`).
   Filtered to rows with `Stato` in `{SPOSTATO, COINVOLTO}`.
2. **DF2** — Updated full ticket-report CSV (latest export from the ticketing system).
   Inner-joined to DF1 on `Sigillo fiscale`; DF2 becomes the base with reallocation
   columns (`Nuovo posto`, `Stato`, optionally `Nuova fila`) merged in from DF1.
3. **DF3** — Supplementary data CSV. Left-joined on `(Codice ordine, Posto)` to
   append extra columns (e.g., contact details).

A `--annullo-from DATE` filter is applied to keep only rows where `data annullo > DATE`.

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

### `cli.py`

**Public interface**: `main()`

Argument parsing, the main event loop, progress printing, and coordination
between engine and reporter. No business logic — pure orchestration.

---

### `exporter.py`

**Public interface**: `export_swap_files(input_path, output_dir)`

Reads an annotated `.xlsx`, filters to `SPOSTATO` rows, and writes one CSV per
order containing the seat-swap mapping. Skips the `COLLATERALE` sheet.

---

## 4. Dependency graph

```
cli.py
  ├── io.py              (load_tickets, parse_orders)
  ├── engine.py          (detect_non_consecutive_orders, process_event, detect_collateral)
  │     ├── geometry.py  (is_adjacent)
  │     ├── segments.py  (resolve_seats, build_segments)
  │     │     └── config.py
  │     └── solver/
  │           ├── __init__.py   → ILPSolver
  │           ├── ilp.py        → BacktrackSolver (fallback)
  │           │     ├── geometry.py
  │           │     └── config.py
  │           └── backtrack.py
  │                 ├── geometry.py
  │                 └── config.py
  └── reports/annotator.py  (write_full_report)
        ├── io.py            (load_csv)
        └── config.py        (OCCUPIED)

reallocate_capofila.py
  ├── io.py                  (load_tickets)
  ├── engine.py              (detect_non_consecutive_orders)
  ├── capofila.py            (build_occupied_current, fix_capofila_orders)
  │     └── config.py       (OCCUPIED)
  └── reports/annotator.py  (write_full_report)

reports/post_report.py
  └── io.py                  (load_csv)
```

External dependencies: `pandas`, `openpyxl`, `pulp` (optional — graceful fallback), `numpy`.

---

## 5. Function-by-function reference

### `load_csv(path, **kwargs)` — `io.py`

**Output**: `pd.DataFrame`

Peeks at the first line to detect `;` vs `,`. Passes `**kwargs` directly to
`pd.read_csv`. Used by `load_tickets`, `reporter.write_full_report`, and
`post_report.build`.

---

### `parse_orders(path)` — `io.py`

**Output**: `dict[str, set[str]]` — `{event_date_str: set_of_order_ids}`

Each line in `orders.txt` has the format
`"2026-06-06 20:30:00.0: ['3406711', '3407635', ...]"`. Split on `': '` (limit
1), then `ast.literal_eval` safely parses the Python list literal.

---

### `load_tickets(path)` — `io.py`

**Output**: cleaned `pd.DataFrame` with `Posto` and `Fila` as `int`.

Accepts both CSV and Excel (multi-sheet). Filters out GA rows, non-VALID
statuses, and rows with missing key columns. Uses `load_csv` for the CSV path.

---

### `detect_non_consecutive_orders(df)` — `engine.py`

**Output**: `dict[str, set[str]]` — `{event_date: set(order_ids)}`

An order is problematic if, within any `Settore prezzi` group, its seats span
multiple `Settore`, multiple `Fila`, or non-consecutive `Posto` values. Respects
the optional `Selezione in mappa` column (rows with value `'true'` are excluded).

---

### `resolve_seats(event_df)` — `segments.py`

**Output**: `occupied: {(settore, fila, posto): (order_id, sp)}`, `free: {(settore, fila, posto): sp}`

A seat is OCCUPIED if any CONFIRMED/RESALE row exists regardless of any
CANCELLED rows for the same seat.

---

### `build_segments(occupied, free)` — `segments.py`

**Output**: `{(settore, fila, sp): {'seats': {posto: order_id}, 'free': set[posto]}}`

Routes each seat to the segment keyed by `(settore, fila, settore_prezzi)`.
Including `settore_prezzi` in the key structurally forbids cross-price swaps.

---

### `build_occupied_current(event_df)` — `capofila.py`

**Output**: `{(settore, fila, posto): (order_id, sp, original_posto)}`

Keyed on `Nuovo posto` when present, so chain shifts operate on the actual
current layout after any prior reallocation pass. The `original_posto` value is
retained as `Posto originale` in move records so the reporter can match rows.

---

### `fix_capofila_orders(event_df, order_ids, occupied, event_date)` — `capofila.py`

**Output**: `(capofila_moves: list, still_infeasible: list)`

For each 3-seat capofila order in `order_ids`, three strategies are tried in
ascending estimated cost:
1. **Relay** — a single-seat neighbouring order jumps to the vacated position;
   the shorter gap is cascaded.
2. **Primary cascade** — shift the block between the isolated seat and the pair
   toward the nearest free slot.
3. **Secondary cascade** — shift the pair outward, freeing its inner aisle seat
   for the isolated seat to fill.

4-seat orders are left unchanged (added to `still_infeasible`).

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

---

### `write_full_report(source_path, all_moves, infeasible_set, collateral_rows, path)` — `annotator.py`

Re-reads the source file, left-merges the move DataFrame on
`(Data evento, Codice ordine, posto_num)`, annotates each row with `Nuovo posto`
and `Stato`, drops temp columns, reorders so the new columns follow `Posto`,
writes one sheet per event. Returns the annotated DataFrame.

---

### `build(annotated_path, updated_report_path, extra_path, annullo_from, out_path)` — `post_report.py`

Three-way merge producing the final report. See section 3 (`post_report.py`) for
the full merge logic.

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
| Add a new post-processing step | Add a function in `post_report.py` or a new module |

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

### `data/report.csv`

```
Codice ordine,Stato ordine,Stato posto,Data evento,Item,Settore,Fila,Posto,Settore prezzi
3406628,REGULAR,CONFIRMED,2026-06-27 21:00:00.0,...,Prato Gold,GA,3,PRATO GOLD
```

Separator auto-detected (`;` or `,`). Rows where `Fila == "GA"` or
`Stato posto ∉ {CONFIRMED, RESALE, CANCELLED}` are filtered out.

### `data/orders.txt`

```
2026-05-30 21:00:00.0: ['3417618', '3419185', ...]
2026-06-06 20:30:00.0: ['3406711', '3407635', ...]
```

One line per event. Python list literal after the colon.

### `data/report_annotated.xlsx` (output of reallocate.py / reallocate_capofila.py)

One sheet per event containing every row of the source file, with added columns
`Nuovo posto` and `Stato` inserted after `Posto`. Optional `COLLATERALE` sheet.

### `data/post_report.xlsx` (output of build_post_report.py)

Single-sheet Excel file: DF2 rows matched to DF1 on `Sigillo fiscale`, enriched
with `Nuovo posto` / `Stato`, filtered by `data annullo`, and joined to DF3.

---

## 9. Known limitations

1. **ILP model size** grows quadratically with segment size — O(N²/k) variables for N seats and orders of size k.
2. **Silent fallback** — when the ILP fails, `BacktrackSolver` is invoked without logging which segment triggered it.
3. **`simulate_collateral` is O(N log N) per backtracking leaf** — can be slow for large segments in the BT fallback.
4. **Non-adjacent non-prob orders use centroid distance**, not exact displacement, in the ILP objective.
5. **Sheet name collision** — if two event dates produce the same 31-character truncated string, `openpyxl` will error.
6. **Full-report merge assumes unique `(event, order, seat)` rows** — duplicates in the source CSV will inflate counts.
7. **Capofila 4-seat orders** are always left as `NON RISOLVIBILE` — the chain-shift strategy is only defined for 3-seat orders.
