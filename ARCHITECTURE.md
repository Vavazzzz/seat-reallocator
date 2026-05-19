# Seat Reallocator — Architectural Walkthrough

> Complete internal documentation of `reallocate.py` for developers who need to understand or modify the system.
> Reflects the ILP-based implementation.

---

## 1. High-level strategy

### The problem in plain terms

Each ticket order can cover multiple seats. The ticketing system sometimes assigns those seats non-consecutively (e.g., seats 3, 7, 12 in the same row), which causes physical seating problems at the venue. The tool must rearrange who sits where so that every "problematic" order ends up in a tight consecutive block — without changing anyone's sector, row, or price category.

### Overall approach

The primary solver is an **Integer Linear Program (ILP)** that jointly optimizes seat assignments for every order in a segment in a single shot. The old greedy-warm-start + backtracking search is retained as a fallback (`_solve_segment_bt`) used only when the `pulp` library is unavailable or the ILP finds no feasible solution.

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
      solve_segment()   [ILP path]
        build binary variables x[order, block]
        add assignment constraints
        add seat-conflict constraints
        build tiered objective (INFEASIBLE_PENALTY / COLL_PENALTY / displacement)
        solve with CBC (10-second limit)
        → on failure: _solve_segment_bt() [backtracking fallback]
        extract assignment, emit moves
                       ↓
collateral detection  → identify orders adjacent before but not after
                       ↓
write output:
  default mode    → data/reallocation.xlsx   (moved + infeasible seats only)
  --full-report   → data/report_annotated.xlsx (every row annotated)
```

### Why ILP over backtracking

The backtracking solver had two fundamental limitations:

1. **Non-problematic orders were handled by a separate greedy repair** after the search placed the problematic orders. The greedy repair could scatter previously-adjacent non-problematic orders (collateral damage) in ways the backtracker never saw or optimized.

2. **The objective was local**: cost was `displacement + 100 × evictions` per problematic order, which did not directly penalize collateral damage to non-problematic orders.

The ILP addresses both by placing **every order in the model simultaneously**. The optimizer sees the full picture and can trade off between fixing problematic orders and preserving non-problematic ones in a single pass, with an explicit cost hierarchy enforced by penalty tiers.

---

## 2. File/module breakdown

The entire codebase is **one file**: `reallocate.py`. Structured by comment banners:

| Section | Lines | Responsibility |
|---|---|---|
| Constants | 23–25 | `OCCUPIED`, `VALID`, `MAX_BRANCHES` |
| I/O | 32–59 | Reading inputs, auto-detecting separator |
| Seat resolution | 66–109 | Building clean seat-state maps |
| 1-D geometry | 116–133 | Helpers for consecutive-run logic |
| Seat pairing | 140–149 | Smart old/new seat matching |
| ILP solver | 156–316 | Primary solver (`solve_segment`) |
| Backtracking fallback | 319–545 | Secondary solver (`_solve_segment_bt`) |
| Event processing | 552–592 | Per-event orchestration |
| Entry point | 599–841 | Argument parsing, collateral detection, two output modes |

**Dependency graph** (call direction →):

```
main()
  ├── load_tickets()
  ├── parse_orders()
  └── process_event()
        ├── resolve_seats()
        ├── build_segments()
        └── solve_segment()            [ILP primary]
              ├── is_adjacent()
              ├── contiguous_runs()    ← via get_blocks() inner fn
              ├── _pair_seats()
              └── _solve_segment_bt() [fallback]
                    ├── is_adjacent()
                    ├── contiguous_runs()
                    ├── simulate_collateral()
                    ├── record_if_better()
                    └── _pair_seats()
```

**External dependencies**: `pandas` (data loading), `pulp` (ILP solver — optional, graceful fallback if absent), `openpyxl` (Excel output). `ast`, `time`, `collections`, `argparse` from the standard library.

---

## 3. Function-by-function explanation

### `parse_orders(path)` — line 32

**Purpose**: Parse `orders.txt` into a structured mapping.

**Input**: file path string.

**Output**: `dict[str, set[str]]` — `{event_date_str: set_of_order_id_strings}`

**Logic**: Each line has the format `"2026-06-06 20:30:00.0: ['3406711', '3407635', ...]"`. Split on `': '` (limit 1 to handle colons in the date), then `ast.literal_eval` safely parses the Python list literal. Result is converted to a `set` for O(1) membership tests.

**Why `ast.literal_eval`**: The file uses Python list syntax with single-quoted strings — `json.loads` would fail. `eval` works but is a security risk. `ast.literal_eval` evaluates only Python literals, not arbitrary expressions.

**Called by**: `main()`.

---

### `load_tickets(path)` — line 45

**Purpose**: Load and clean a CSV (either `report_cleaned.csv` or the full report in `--full-report` mode).

**Input**: CSV path.

**Output**: cleaned `pd.DataFrame` with `Posto` as `int`.

**Changes from old version**: Auto-detects the separator by reading the first line and checking for `;`. This handles both the cleaned CSV (comma-separated) and the raw full report (semicolon-separated).

**Logic**:
1. Peek at the first line to detect `;` vs `,`.
2. `pd.read_csv` with `dtype={'Codice ordine': str, 'Data evento': str}` — prevents coercion of IDs to numbers.
3. Filter to rows where `Stato posto ∈ {'CONFIRMED', 'RESALE', 'CANCELLED'}`.
4. `pd.to_numeric(df['Posto'], errors='coerce')` — non-numeric seat numbers become `NaN`.
5. `dropna` on key columns, then cast `Posto` to `int`.

**Called by**: `main()`.

---

### `resolve_seats(event_df)` — line 66

**Purpose**: Determine the true status of every physical seat for one event.

**Input**: `event_df` — all ticket rows for one event.

**Output**:
- `occupied: {(settore, fila, posto): (order_id, settore_prezzi)}` — seats with any CONFIRMED/RESALE row.
- `free: {(settore, fila, posto): settore_prezzi}` — seats with only CANCELLED rows.

**Why needed**: The CSV may have multiple rows per physical seat (e.g., a CANCELLED row from a return plus a CONFIRMED row from a repurchase). The rule: if any CONFIRMED or RESALE row exists for a seat, the seat is occupied regardless of any CANCELLED rows.

**Logic**:
1. `active_keys` = set of `(settore, fila, posto)` triples with at least one CONFIRMED/RESALE row.
2. Deduplicate active rows by seat — first row's order ID is used (assumes all active rows for a seat belong to the same order).
3. For CANCELLED rows, only include seats **not** in `active_keys` as free.

**Called by**: `process_event()`.

---

### `build_segments(occupied, free)` — line 97

**Purpose**: Partition all seats into independent 1-D sub-problems.

**Input**: `occupied` and `free` dicts from `resolve_seats`.

**Output**: `dict[(settore, fila, sp), {'seats': {posto: order_id}, 'free': set[posto]}]`

**Logic**: Iterates both dicts, routing each seat to the segment keyed by its `(settore, fila, settore_prezzi)` triple.

**Why `settore_prezzi` in the key**: A row can contain seats at different price levels. Swapping across price levels changes the buyer's price category — forbidden. Including `settore_prezzi` enforces this constraint structurally.

**Called by**: `process_event()`.

---

### `contiguous_runs(positions)` — line 116

**Purpose**: Decompose a sorted list of integers into maximal consecutive runs.

**Input**: sorted `list[int]`.

**Output**: `list[list[int]]` — each inner list is a consecutive run.

**Example**: `[1, 2, 3, 5, 6, 10]` → `[[1, 2, 3], [5, 6], [10]]`

**Logic**: Linear scan — extend current run if next position = `prev + 1`, otherwise start a new run.

**Why needed**: Both the ILP and backtracking solvers enumerate candidate blocks as windows within runs. A window can only span within a run because positions from different runs are not consecutive.

**Called by**: `solve_segment()` via `get_blocks()` inner function; `_solve_segment_bt()` via `candidate_blocks()` and `simulate_collateral()`.

---

### `is_adjacent(postos)` — line 131

**Purpose**: Check whether a list of seat numbers is consecutive.

**Input**: `list[int]` (unsorted).

**Output**: `bool`

**Logic**: Sort, then verify every adjacent pair differs by exactly 1. Single-seat lists return `True`.

**Called by**: `solve_segment()` and `_solve_segment_bt()` to build `to_fix`; `simulate_collateral()` to detect collateral damage; collateral detection in `main()`.

---

### `_pair_seats(old_p, new_p)` — line 140

**Purpose**: Match old seat positions to new seat positions minimising the number of seats that actually move.

**Input**: `old_p: list[int]`, `new_p: list[int]` — same-length lists of sorted positions.

**Output**: `list[(old_seat, new_seat)]`

**Logic**:
1. `kept = set(old_p) & set(new_p)` — seats present in both lists stay fixed (paired with themselves).
2. `remaining_old` and `remaining_new` = positions not in `kept`, sorted.
3. Pair remaining by sorted-index zip.

**Why this matters**: The old code used `zip(old_p, new_p)` unconditionally — a sorted positional pairing. That could generate spurious moves. Example: an order at seats [5, 6] reassigned to [5, 7] would previously emit `(5→5, 6→7)`; `_pair_seats` gives the same result here. But for [5, 6] → [4, 6], the old code emits `(5→4, 6→6)` while `_pair_seats` emits `[(6,6), (5,4)]` — seat 6 is recognised as staying in place and only seat 5 moves. This reduces noise in the output.

**Called by**: `solve_segment()` and `_solve_segment_bt()` in their move-emission phases.

---

### `solve_segment(seats, free_postos, problematic_set)` — line 156

This is the primary solver. It formulates and solves an ILP, then falls back to `_solve_segment_bt` if `pulp` is unavailable or the ILP finds no feasible solution.

**Purpose**: Find the globally minimum-cost seat assignment for the entire segment such that every problematic order ends up in a contiguous block.

**Inputs**:
- `seats: dict[posto, order_id]` — current occupied assignment.
- `free_postos: set[posto]` — free positions in the segment.
- `problematic_set: set[order_id]` — orders to fix.

**Output**: `(moves, infeasible)`.

#### Step 1 — Setup (identical to backtracking path)

```python
order_postos = {oid: sorted(postos)}   # inverted index
to_fix = {oid for oid in prob. orders that are not adjacent}
all_pos = sorted(occupied ∪ free)
runs = contiguous_runs(all_pos)
```

`get_blocks(k)` inner function: same as old `candidate_blocks(k)` — returns all k-length windows across all runs.

#### Step 2 — Build candidate blocks for every order

```python
candidates: dict = {}
for oid, postos in order_postos.items():
    blocks = get_blocks(len(postos))
    if oid in to_fix:
        orig = tuple(postos)
        if orig not in blocks:
            blocks = blocks + [orig]   # dummy option
    candidates[oid] = blocks
```

**Critical difference from old approach**: Every order — problematic and non-problematic alike — gets a candidate list. Non-problematic orders' candidates are all contiguous blocks of their size, including their current positions. This is what makes the ILP a joint optimizer: it decides placement for all orders simultaneously.

For problematic orders, the dummy block `orig` (their current non-contiguous seats) is added to the candidate list if it isn't already there. This acts as an "infeasibility escape hatch" — the solver can choose it, but paying `INFEASIBLE_PENALTY`.

#### Step 3 — Define penalty constants

```python
INFEASIBLE_PENALTY = 1_000_000   # cost of leaving a prob order non-adjacent
COLL_PENALTY       = 10_000      # cost of moving an already-adjacent non-prob order
```

The hierarchy ensures the solver prefers, in strict order:
1. Fix all fixable problematic orders (`INFEASIBLE_PENALTY` avoided).
2. Leave already-adjacent non-problematic orders undisturbed (`COLL_PENALTY` avoided).
3. Minimise seat displacement (tiebreaker, integer values).

#### Step 4 — Build ILP variables

```python
x: dict = {
    (oid, b): pulp.LpVariable(f"x{oi[oid]}_{bi}", cat="Binary")
    for oid, blocks in candidates.items()
    for bi, b in enumerate(blocks)
}
```

One binary variable per `(order, candidate_block)` pair. `x[oid, b] = 1` means "order `oid` is assigned to block `b`."

Variable naming uses integer indices (`oi[oid]`, `bi`) rather than raw IDs to avoid character-limit issues in the solver's variable-name handling.

#### Step 5 — Add constraints

**Assignment constraint** (one block per order):
```python
for oid, blocks in candidates.items():
    mdl += pulp.lpSum(x[oid, b] for b in blocks) == 1
```

**Seat conflict constraint** (at most one order per seat):
```python
seat_vars: dict = defaultdict(list)
for (oid, b), var in x.items():
    for seat in b:
        seat_vars[seat].append(var)
for seat, var_list in seat_vars.items():
    if len(var_list) > 1:
        mdl += pulp.lpSum(var_list) <= 1
```

These two constraint families define the feasible region. The assignment constraint ensures no order is left unplaced. The conflict constraint ensures no physical seat is claimed by two orders simultaneously.

Note: the conflict constraint is only added where `len(var_list) > 1` — seats that appear in only one block's variable are trivially conflict-free.

#### Step 6 — Build objective

The objective has three tiers, applied depending on whether the order is problematic, already-adjacent non-problematic, or non-adjacent non-problematic:

```python
# Prob orders
if oid in to_fix:
    for b in blocks:
        if b == orig:
            obj.append(INFEASIBLE_PENALTY * x[oid, b])    # dummy = stay non-adjacent
        else:
            disp = sum(abs(nb - ob) for nb, ob in zip(b, postos))
            obj.append(disp * x[oid, b])                   # displacement only

# Already-adjacent non-prob orders
elif is_adjacent(postos) and orig in blocks:
    for b in blocks:
        if b != orig:
            disp = sum(abs(nb - ob) for nb, ob in zip(b, postos))
            obj.append((COLL_PENALTY + disp) * x[oid, b])  # collateral + displacement

# Non-adjacent non-prob orders (shouldn't happen but tolerated)
else:
    centroid = sum(postos) / len(postos)
    for b in blocks:
        disp = abs(sum(b) / len(b) - centroid)
        if disp:
            obj.append(disp * x[oid, b])                   # centroid displacement only
```

The comment in the code notes that `COLL_PENALTY + disp` as the coefficient for non-original blocks eliminates degenerate circular permutations — every possible assignment gets a unique score, so the solver always picks the assignment that moves the non-prob order the minimum distance from its origin.

The third case (non-adjacent non-prob orders) uses centroid displacement because these orders don't have a well-defined "correct" position — the solver moves them as little as possible.

#### Step 7 — Solve

```python
mdl.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=10))
```

CBC (COIN-BC) is the open-source MIP solver bundled with `pulp`. `msg=0` suppresses solver output. `timeLimit=10` — ten seconds per segment (ten times more than the old 1-second backtracking budget, appropriate given the larger search space the ILP covers).

`mdl.sol_status` codes: `1` = Optimal, `2` = IntegerFeasible (time limit hit but a valid solution was found). Anything else (infeasible, unbounded, error) triggers the backtracking fallback.

#### Step 8 — Extract solution

```python
assignment: dict = {}
for oid, blocks in candidates.items():
    for b in blocks:
        if (pulp.value(x[oid, b]) or 0) > 0.5:
            assignment[oid] = b
            break
```

For each order, scan its blocks and pick the one whose variable is closest to 1. The `> 0.5` threshold handles floating-point noise in the solver's binary variable values.

#### Step 9 — Identify infeasible orders

```python
infeasible = [oid for oid in to_fix
              if assignment.get(oid) == tuple(order_postos[oid])]
```

A problematic order is infeasible if the solver assigned it to its own dummy block (original non-contiguous positions). This means the `INFEASIBLE_PENALTY` was unavoidable — no contiguous block could be found for it without violating seat-conflict constraints.

#### Step 10 — Build `new_asgn` and emit moves

```python
new_asgn: dict = {}
for oid, b in assignment.items():
    for p in b:
        new_asgn[p] = oid
for oid, postos in order_postos.items():    # safety fallback
    if oid not in assignment:
        for p in postos:
            new_asgn[p] = oid
```

Invert to `{order_id: [sorted new postos]}`, then compare old and new per order via `_pair_seats`:

```python
for oid, old_p in order_postos.items():
    new_p = inv.get(oid, old_p)
    if set(old_p) != set(new_p):
        for old, new in _pair_seats(old_p, new_p):
            moves.append((oid, old, new))
```

The `set(old_p) != set(new_p)` check skips orders whose seat-set didn't change (set comparison, not list comparison — order of seats doesn't matter).

**Called by**: `process_event()`.

---

### `_solve_segment_bt(seats, free_postos, problematic_set)` — line 319

This is the old backtracking solver, now a fallback. Its structure is similar to the original but with several important refinements.

**Purpose**: Place each problematic order in a contiguous block via branch-and-bound backtracking, then greedily reassign non-problematic orders. Used when `pulp` is absent or the ILP fails.

**Inputs/Output**: same interface as `solve_segment`.

#### Key differences from the original backtracking solver

**1. Lexicographic objective — collateral is the primary criterion**

```python
best: dict = {'collateral': float('inf'), 'displacement': float('inf'), 'placement': None}
```

The old solver minimised a combined `displacement + 100 * evictions` scalar. The backtracking fallback now minimises `(collateral, displacement)` lexicographically — collateral first, displacement as tiebreaker. "Collateral" means the number of previously-adjacent non-problematic orders that become non-adjacent after the reallocation.

**2. `simulate_collateral(placement)` — line 367**

```python
def simulate_collateral(placement: dict) -> int:
```

Called at every leaf of the search tree to evaluate a complete candidate placement. It simulates the full non-problematic order reassignment (the same greedy loop used in the final assignment phase) and counts how many originally-adjacent non-prob orders end up non-adjacent in that simulation.

This is expensive (O(N log N) per leaf) but necessary: collateral damage cannot be predicted from displacement alone because it depends on which non-prob orders get evicted and whether contiguous slots remain for them.

**3. `record_if_better(placement, displacement)` — line 412**

```python
def record_if_better(placement: dict, displacement: int):
    coll = simulate_collateral(placement)
    if (coll < best['collateral'] or
            (coll == best['collateral'] and displacement < best['displacement'])):
        best['collateral']  = coll
        best['displacement'] = displacement
        best['placement']   = dict(placement)
```

Updates `best` using the lexicographic comparison. Replaces the old direct cost comparison.

**4. Modified pruning condition**

```python
if best['collateral'] == 0 and partial_cost >= best['displacement']:
    return
```

The old solver pruned any branch where `partial_cost >= best['cost']`. The new solver only prunes on displacement when the current best has zero collateral — otherwise it would incorrectly discard branches that might reduce collateral even at higher displacement.

**5. Non-problematic order reassignment tries contiguous blocks first**

```python
best_block = None
best_dist  = float('inf')
for run in contiguous_runs(rem_list):
    for i in range(len(run) - k + 1):
        block = tuple(run[i: i + k])
        d = abs(sum(block) / k - centroid)
        if d < best_dist:
            best_dist  = d
            best_block = block

if best_block is None:
    # No contiguous block available — take k nearest individual seats
    rem_list.sort(key=lambda p: abs(p - centroid))
    best_block = tuple(rem_list[:k]) if len(rem_list) >= k else tuple(rem_list)
```

The old solver just sorted by centroid distance and took the `n_evicted` nearest individual seats. The fallback now explicitly searches for the nearest **contiguous** block, falling back to individual seat assignment only when no contiguous block exists. This directly reduces the collateral damage the solver produces.

**6. Displaced non-prob orders get priority**

```python
displaced_np = sorted(
    [(oid, op) for oid, op in order_postos.items()
     if oid not in to_fix and any(p in prob_taken for p in op)],
    key=lambda x: -len(x[1]),    # largest displaced first
)
intact_np = sorted(
    [(oid, op) for oid, op in order_postos.items()
     if oid not in to_fix and all(p not in prob_taken for p in op)],
    key=lambda x: (-len(x[1]), min(x[1])),    # largest first, then by position
)
for oid, old_p in displaced_np + intact_np:
    ...
```

Orders that lost seats to the prob placement are processed first — they have lost seats and urgently need a new contiguous block. Intact orders follow and will naturally reclaim their original positions if still available.

**7. Infeasible orders stay in place explicitly**

```python
for oid in infeasible:
    for p in to_fix[oid]:
        new_asgn[p] = oid
```

In the old code, infeasible orders' seats could be claimed by the non-prob repair loop. Now they are locked into `new_asgn` before the repair loop runs.

**8. Double fallback**

```python
if best['placement'] is None:
    taken_fb: set = set()
    best['placement'] = {}
    for oid, old_p in prob_list:
        ...greedy...
```

If backtracking finds no placement at all (very rare — would require the greedy warm-start to also fail), a second greedy pass runs independently. This prevents `best['placement']` from being `None` downstream.

---

### `process_event(event_df, problematic)` — line 552

**Purpose**: Orchestrate per-event processing.

**Logic**: Unchanged from the old version except one small addition: moves where `old_p == new_p` get `Stato = 'COINVOLTO'` instead of `'SPOSTATO'`.

```python
'Stato': 'SPOSTATO' if old_p != new_p else 'COINVOLTO',
```

`'COINVOLTO'` marks orders that were included in the optimizer's assignment (and thus appear in the output) but whose seat did not actually change number. This can happen when the ILP re-assigns an order to a block that happens to contain its original seat.

**Called by**: `main()`.

---

### `main()` — line 599

**Purpose**: Entry point with two output modes.

#### New: argument parsing

```python
parser.add_argument('--full-report', metavar='PATH', ...)
```

Without `--full-report`: reads `data/report_cleaned.csv`, writes `data/reallocation.xlsx` (moved/infeasible seats only, plus optional `COLLATERALE` sheet).

With `--full-report PATH`: reads the full raw report at `PATH`, annotates every row with `Nuovo posto` and `Stato`, writes `data/report_annotated.xlsx`.

#### New: collateral detection (lines 647–685)

After all events are processed, a post-processing pass identifies collateral orders — those that were originally adjacent but became non-adjacent after reallocation. This works across both output modes.

```python
for event_date, event_active in active.groupby('Data evento'):
    # Reconstruct final seat state by applying all moves
    orig: dict = defaultdict(list)     # original seats per order
    final: dict = {oid: list(ps) ...}  # copy that gets moves applied

    for m in moves_by_event[event_date]:
        oid = m['Codice ordine']
        # Remove old seat, add new seat
        if op in final[oid]: final[oid].remove(op)
        if np_ not in final[oid]: final[oid].append(np_)

    for oid, orig_ps in orig.items():
        if (event_date, oid) in infeasible_set: continue
        if not is_adjacent(orig_ps) or is_adjacent(final[oid]): continue
        # was adjacent, now isn't → collateral
        collateral_rows.append({..., 'Stato': 'COLLATERALE'})
```

The detection condition `not is_adjacent(orig_ps) or is_adjacent(final[oid])` reads: "skip if originally non-adjacent (already broken, not collateral) OR if still adjacent after (no damage)." Only orders that were adjacent before and non-adjacent after are flagged.

#### Full-report mode (lines 687–782)

Reads the full CSV again, joins the move results on `(Data evento, Codice ordine, posto_num)`, and annotates each row with `Nuovo posto` (the seat's new number) and `Stato` (`SPOSTATO`, `COINVOLTO`, `NON RISOLVIBILE`, or `NON COINVOLTO`).

Uses a pandas left-merge rather than a row loop, so even very large CSVs are handled efficiently. Drops temporary `_` columns before writing.

#### Summary mode (lines 784–833, default)

Same as the old behaviour: writes only moved and infeasible seats to `data/reallocation.xlsx`, plus the `COLLATERALE` sheet if any collateral orders were found.

---

## 4. Execution flow trace

**Scenario**: Event `2026-05-30 21:00:00.0`. Two problematic orders in segment `(Settore B, -, PRIMO SETTORE NUMERATO)`:

- Order `3417618`: seats [55, 65] — non-adjacent.
- Order `3420777`: seats [63, 68] — non-adjacent (hypothetical).
- Order `3410490`: seats [59] — single seat, non-problematic.
- Free positions in the segment: 56, 57, 60, 61, 64, 66, 67.

**Step 1 — `main` calls `process_event`**

`resolve_seats` maps each physical seat to its occupant. `build_segments` puts all seats in `(Settore B, -, PRIMO SETTORE NUMERATO)`.

Cross-segment check: both problematic orders appear in only this one segment → `globally_infeasible = {}`, `fixable = {3417618, 3420777}`.

**Step 2 — `solve_segment` is called**

`order_postos`:
```
3417618 → [55, 65]   (in to_fix: non-adjacent)
3420777 → [63, 68]   (in to_fix: non-adjacent)
3410490 → [59]       (not in to_fix: single seat, already adjacent)
```

`all_pos = [55, 56, 57, 59, 60, 61, 63, 64, 65, 66, 67, 68]`
`runs = [[55,56,57], [59,60,61], [63,64,65,66,67,68]]`

`candidates` built:
- `3417618`: all 2-length blocks + dummy `(55,65)`. Blocks include `(55,56)`, `(56,57)`, `(59,60)`, `(60,61)`, `(63,64)`, ..., plus dummy `(55,65)`.
- `3420777`: all 2-length blocks + dummy `(63,68)`.
- `3410490`: all 1-length blocks = `[(55,), (56,), ..., (68,)]`.

**Step 3 — ILP variables and constraints**

~30 variables created (one per `(order, block)` pair). Three assignment constraints (one per order). Seat conflict constraints for positions like 55, 56, 63, 64, 65 etc. where multiple blocks overlap.

**Step 4 — Objective**

For `3417618`:
- `(55,65)` dummy → coefficient `1_000_000`
- `(55,56)` → displacement `|55-55| + |56-65| = 9`
- `(56,57)` → displacement `|56-55| + |57-65| = 9`
- `(59,60)` → displacement `|59-55| + |60-65| = 9` (evicts `3410490` from 59, but that's not directly penalized in the ILP — `3410490`'s `COLL_PENALTY` handles it)
- ...

For `3410490` (single seat, adjacent):
- `(59,)` → 0 (no cost for staying in place)
- Any other position → `COLL_PENALTY + |new - 59|`

**Step 5 — CBC solves**

CBC finds the optimal assignment: perhaps `3417618 → (63,64)`, `3420777 → (65,66)`, `3410490 → (59,)` — zero displacement for `3410490`, and both problematic orders fixed.

**Step 6 — Assignment extracted**

Both problematic orders get contiguous blocks. `3410490` stays at 59.

**Step 7 — Move emission**

```
3417618: old=[55,65], new=[63,64] → _pair_seats → [(55→63), (65→64)]
3420777: old=[63,68], new=[65,66] → _pair_seats → [(63→65), (68→66)]
3410490: old=[59], new=[59] → set unchanged, no move emitted
```

**Step 8 — Back in `main`**, collateral detection runs, no collateral found, output written.

---

## 5. Decision logic

### ILP objective hierarchy

The three-tier cost structure enforces a strict priority ordering:

| Priority | Condition | Cost assigned |
|---|---|---|
| 1st | Prob order cannot be fixed → dummy block chosen | `INFEASIBLE_PENALTY = 1,000,000` |
| 2nd | Adjacent non-prob order is moved | `COLL_PENALTY + displacement = 10,000+` |
| 3rd | Prob order displacement | `displacement` (integer, bounded by segment size) |
| 3rd | Non-adjacent non-prob order centroid shift | `centroid distance` (float, secondary tiebreaker) |

Because penalties are strictly larger than any realistic displacement value (segments are hundreds of seats at most), the solver never trades a collateral damage for displacement savings. The hierarchy is mathematically enforced, not just heuristic.

### How candidate blocks are enumerated

`get_blocks(k)` / `candidate_blocks(k)` enumerate all k-length windows within contiguous runs. For the ILP, **all** blocks are added to the model (no `MAX_BRANCHES` cap). This is exact: the ILP considers every possible contiguous placement, not just the 25 closest by centroid as the backtracking did.

### Constraints enforced

| Constraint | Enforcement |
|---|---|
| Same sector/row/price | Structural — segment decomposition |
| Consecutive seats only | `get_blocks`/`candidate_blocks` only produces windows from runs |
| No two orders share a seat | ILP seat-conflict constraint (`lpSum(seat_vars[seat]) <= 1`) |
| Cross-segment orders rejected | `globally_infeasible` check in `process_event` |
| Infeasible orders stay in place | ILP: dummy block with original seats; BT: explicit lock in `new_asgn` |

### How the ILP handles infeasibility

The ILP is always **feasible by construction** — even if no contiguous block exists for a problematic order, the dummy block `orig` is always available. The solver never returns "infeasible"; it returns a solution where some prob orders chose their dummy block (paying `INFEASIBLE_PENALTY`). Infeasibility detection is post-hoc:

```python
infeasible = [oid for oid in to_fix
              if assignment.get(oid) == tuple(order_postos[oid])]
```

### Backtracking fallback pruning

The fallback prunes only when `best['collateral'] == 0` — meaning we have found at least one zero-collateral solution. Until then, all branches continue (we can't discard branches that might reduce collateral). Once a zero-collateral solution exists, displacement pruning (`partial_cost >= best['displacement']`) activates. This is a weaker pruning condition than the old flat cost prune, meaning the fallback explores more of the tree — acceptable given it's only invoked when the ILP fails.

---

## 6. Data structures

### `tickets: pd.DataFrame`

Flat row-per-ticket DataFrame. After `load_tickets`, filtered to valid statuses with `Posto` as `int`. Grouped by `Data evento` in `main`.

### `occupied / free` dicts

```
occupied: {(settore, fila, posto): (order_id, settore_prezzi)}
free:     {(settore, fila, posto): settore_prezzi}
```

Physical seat → ownership/availability mapping. Tuple keys uniquely identify a physical seat. Used only within `process_event` to build segments.

### `segments: {(settore, fila, sp): {'seats': {posto: order_id}, 'free': set[posto]}}`

The problem decomposition. Each segment is fully self-contained. The solver sees only `seats` and `free` — no other segment's data.

### `candidates: {order_id: list[tuple[posto, ...]]}`

Built in `solve_segment`. For each order (prob and non-prob), all contiguous k-length blocks in the segment, plus the dummy block for prob orders. This is the full decision set for the ILP.

### `x: {(order_id, block_tuple): LpVariable}`

The ILP variables. Binary — 1 if the order is assigned to that block, 0 otherwise. After solving, exactly one variable per order will be 1.

### `seat_vars: {posto: list[LpVariable]}`

Index for building seat-conflict constraints. Maps each position to all `x` variables whose block contains that position.

### `best: dict` (ILP path)

Not used in the ILP path — the ILP directly produces the optimal `assignment` dict. `best` is only used in `_solve_segment_bt`.

### `best: dict` (backtracking fallback)

```python
{'collateral': float, 'displacement': float, 'placement': dict}
```

Tracks the best complete solution found. Updated via `record_if_better` using lexicographic comparison. `placement: {order_id: block_tuple}` is deep-copied (`dict(placement)`) on every update.

### `new_asgn: {posto: order_id}`

Final seat assignment after the solver. Built in three phases in the backtracking path (prob orders, infeasible orders, non-prob orders), or directly from `assignment` in the ILP path. Inverted to emit moves.

### `collateral_rows: list[dict]`

Post-processing result from `main`. Each entry describes one order that was adjacent before reallocation and non-adjacent after. Written to the `COLLATERALE` sheet in the output.

---

## 7. Weak points and complexity hotspots

### 1. ILP model size grows quadratically with segment size

The number of variables is `Σ_orders (number_of_k_blocks_for_order)`. For a dense segment with N seats and many orders of size k, `get_blocks(k)` can return O(N) blocks per order, and there are O(N/k) orders, giving O(N²/k) variables. For large events (hundreds of seats per row, many problematic orders), this can produce models with thousands of variables, which may approach or exceed the 10-second time limit.

### 2. Fallback to `_solve_segment_bt` is silent

When the ILP fails (`mdl.sol_status not in (1, 2)`), the code silently calls `_solve_segment_bt`. There is no logging of which segments fell back, making it hard to diagnose quality differences or detect segments that are consistently forcing the fallback.

### 3. `simulate_collateral` is O(N log N) per backtracking leaf

In `_solve_segment_bt`, `simulate_collateral` is called at every complete placement found by `backtrack`. It simulates the entire non-prob assignment loop (sorting, run-finding, block selection). For a segment with many non-prob orders, this can be slow. The 1-second time limit mitigates this, but the fallback may produce fewer explored leaves than the old solver.

### 4. Non-prob orders in the ILP use centroid-distance cost, not block-level displacement

For non-adjacent non-prob orders (third tier in the objective), the cost coefficient is `abs(sum(b)/len(b) - centroid)` — the distance between the centroid of block `b` and the centroid of the original seats. This is a proxy for displacement, not the exact sum of absolute differences. It can mismatch: two blocks with the same centroid distance but different individual seat displacements receive the same cost. This is a deliberate simplification (centroid is cheap and good enough for the tiebreaker role), but it means the ILP may not find the absolute minimum displacement for non-adjacent non-prob orders.

### 5. `_pair_seats` pairing is still an approximation for non-prob orders

`_pair_seats` keeps seats that appear in both old and new lists, then zips remaining positions by sorted index. For an order that moves from [5, 6, 7] to [5, 8, 9], it returns `[(5,5), (6,8), (7,9)]`. This is correct. But for [5, 6] → [6, 7], it keeps 6 in place and maps 5→7 — which is the minimum-moves interpretation but not necessarily the physical swap intention (the person at seat 5 could move to seat 7 while the person at seat 6 stays, or both could slide right by one). The output represents one valid permutation, not necessarily the physically simplest one.

### 6. Collateral detection is a post-hoc check, not an optimization input for the ILP

The ILP objective penalizes moving already-adjacent non-prob orders via `COLL_PENALTY`. But `COLL_PENALTY` is charged per order moved, while collateral means an order that ends up non-adjacent — which can happen even if the order moves as a whole intact block (if it's moved to a position where it's surrounded by other moved orders). In very dense segments, an order might be moved while still ending up non-adjacent even in its new position, and the ILP would charge `COLL_PENALTY` without preventing this. Collateral detection catches these cases after the fact.

### 7. The `COLL_PENALTY + disp` coefficient is applied per-order, not per-seat

The ILP charges `COLL_PENALTY + disp` as the cost coefficient on `x[oid, b]` for non-prob adjacent orders when `b != orig`. But `disp` here is `sum(abs(nb - ob) for nb, ob in zip(b, postos))` — total displacement for the entire order. For a large order (e.g., 6 seats), the displacement component is larger, which could cause the solver to prefer moving large adjacent non-prob orders less than small ones, even if the total seat-count disruption is the same. This is unlikely to matter in practice but is a non-obvious asymmetry.

### 8. Excel sheet name collision risk

Sheet names derived as `str(event_date).replace(':', '.').replace('/', '-')[:31]` could collide if two event dates differ only in characters that map to the same replacement and share the same 31-character prefix. Unguarded — a collision would cause a runtime error from `openpyxl`.

### 9. Full-report merge on `(Data evento, Codice ordine, posto_num)` assumes unique rows

In full-report mode, the move DataFrame is left-merged onto the big DataFrame on three columns. If the original report has duplicate rows for the same `(event, order, seat)` combination, the merge will produce multiple rows per move, inflating counts. The deduplication in `resolve_seats` prevents the solver from seeing duplicates, but the full-report annotation operates on the raw file directly.

---

## Appendix: Key constants

| Constant | Value | Meaning |
|---|---|---|
| `OCCUPIED` | `{'CONFIRMED', 'RESALE'}` | Seat statuses that count as taken |
| `VALID` | `{'CONFIRMED', 'RESALE', 'CANCELLED'}` | Statuses kept after CSV filter |
| `MAX_BRANCHES` | `25` | Max candidate blocks per order per backtrack level (fallback only) |
| ILP time limit | `10` s | Per-segment CBC budget (`timeLimit=10`) |
| BT time limit | `1.0` s | Per-segment backtracking budget (`deadline = time.time() + 1.0`) |
| `INFEASIBLE_PENALTY` | `1,000,000` | ILP cost for leaving a prob order non-adjacent (unfixable) |
| `COLL_PENALTY` | `10,000` | ILP cost for displacing an already-adjacent non-prob order |

## Appendix: Output status values

| `Stato` value | Meaning |
|---|---|
| `SPOSTATO` | Seat was moved to a new number |
| `COINVOLTO` | Order was in the optimizer's assignment but seat number did not change |
| `NON RISOLVIBILE` | Order could not be made adjacent (cross-segment or no valid block) |
| `COLLATERALE` | Order was adjacent before but non-adjacent after (post-hoc detection) |
| `NON COINVOLTO` | Seat was not affected by any reallocation (full-report mode only) |

## Appendix: Input/output formats

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
