# Seat Reallocator — Architectural Walkthrough

> Complete internal documentation of `reallocate.py` for developers who need to understand or modify the system.

---

## 1. High-level strategy

### The problem in plain terms

Each ticket order can cover multiple seats. The ticketing system sometimes assigns those seats non-consecutively (e.g., seats 3, 7, 12 in the same row), which causes physical seating problems at the venue. The tool must rearrange who sits where so that every "problematic" order ends up in a tight consecutive block — without changing anyone's sector, row, or price category.

### Overall approach

The algorithm is a **greedy warm-start + branch-and-bound backtracking search** operating on independent 1-D segments.

The key insight is that the seating problem decomposes naturally: a seat in `(Settore B, Row 7, price PRIMO)` can only be swapped with another seat in the exact same `(Settore, Fila, Settore prezzi)` triple. This means every such triple is a completely independent sub-problem. Once you decompose into these segments, each segment is a 1-D packing problem: given a list of integer positions, find an assignment of consecutive blocks to problematic orders.

### Main execution flow

```
parse_orders()   → {event_date: {order_ids}}
load_tickets()   → filtered DataFrame
                    ↓
for each event_date:
  process_event()
    resolve_seats()    → occupied + free seat maps
    build_segments()   → independent (settore, fila, sp) sub-problems
    cross-segment check → immediately flag orders spanning >1 segment
    for each relevant segment:
      solve_segment()  → branch-and-bound over contiguous block placements
        greedy warm-start → seeds upper bound
        backtrack()       → explores tree, prunes early
        repair phase      → assigns evicted non-problematic orders to nearest free slots
        emit moves        → (order, old_posto, new_posto) per seat changed
                    ↓
write reallocation.xlsx (one sheet per event)
```

### Why this approach

- **Decomposition** is exact: swapping seats across sectors/rows/price-categories is forbidden by hard constraint, so segments are truly independent.
- **Branch-and-bound** is exact within the 1-second time limit and the `MAX_BRANCHES=25` candidate cap. It guarantees the globally minimum-cost placement for a segment if it finishes in time.
- **Greedy warm-start** is a practical necessity: it gives the backtracker a strong upper bound from the very first call, enabling it to prune most branches immediately instead of exploring blindly.
- **Eviction penalty of 100** in the cost function makes displacing a non-problematic occupant roughly equivalent to moving an order 100 seats — so the algorithm strongly prefers solutions that use free slots, resorting to eviction only when necessary.

---

## 2. File/module breakdown

The entire codebase is **one file**: `reallocate.py`. There are no imports of internal modules and no sub-packages. The file is segmented by comment banners:

| Section | Lines | Responsibility |
|---|---|---|
| Constants | 23–26 | `OCCUPIED`, `VALID`, `MAX_BRANCHES` |
| I/O | 32–55 | Reading inputs |
| Seat resolution | 62–105 | Building clean seat-state maps |
| 1-D geometry | 112–129 | Helpers for consecutive-run logic |
| Segment solver | 136–286 | Core algorithm |
| Event processing | 293–333 | Per-event orchestration |
| Entry point | 340–414 | Top-level flow and output |

**Dependency graph** (call direction →):

```
main()
  ├── load_tickets()
  ├── parse_orders()
  └── process_event()
        ├── resolve_seats()
        ├── build_segments()
        └── solve_segment()
              ├── is_adjacent()
              ├── contiguous_runs()          ← via candidate_blocks() inner fn
              ├── step_cost()                ← inner fn
              └── backtrack()               ← inner fn (recursive)
```

No external dependencies except `pandas` for CSV loading and `ast`/`time` from the standard library.

---

## 3. Function-by-function explanation

### `parse_orders(path)` — line 32

**Purpose**: Parse `orders.txt` into a structured mapping.

**Input**: file path string.

**Output**: `dict[str, set[str]]` — `{event_date_str: set_of_order_id_strings}`

**Logic**: Each line has the format `"2026-06-06 20:30:00.0: ['3406711', '3407635', ...]"`. The function splits on `': '` (with a limit of 1 to handle any colons in the date string), then uses `ast.literal_eval` to safely parse the bracketed Python list literal on the right side. The result is converted to a `set` for O(1) membership tests later.

**Why `ast.literal_eval`**: The file uses Python list syntax. `json.loads` would fail because the order IDs are quoted with single quotes (not JSON-legal). `eval` would work but is a security risk. `ast.literal_eval` is the safe middle ground — it only evaluates Python literals, not arbitrary expressions.

**Called by**: `main()` once at startup.

---

### `load_tickets(path)` — line 45

**Purpose**: Load and clean `report_cleaned.csv`.

**Input**: CSV path string.

**Output**: `pd.DataFrame` with only valid rows and `Posto` as an integer column.

**Logic step by step**:
1. `pd.read_csv` with `dtype={'Codice ordine': str, 'Data evento': str}` — prevents pandas from coercing order IDs or timestamps to numeric/datetime types.
2. Filter to rows where `Stato posto ∈ {'CONFIRMED', 'RESALE', 'CANCELLED'}` — this drops ~288 garbage rows that have other status values.
3. `pd.to_numeric(df['Posto'], errors='coerce')` — some seat numbers may be non-numeric strings; they become `NaN`.
4. `dropna` on `['Posto', 'Data evento', 'Settore', 'Fila', 'Settore prezzi']` — drops rows with missing critical fields.
5. Cast `Posto` to `int`.

**Called by**: `main()`.

---

### `resolve_seats(event_df)` — line 62

**Purpose**: From a per-event DataFrame (which may have multiple rows per physical seat), determine the true status of every seat.

**Input**: `event_df` — subset of `tickets` for one event date.

**Output**: Two dicts:
- `occupied: {(settore, fila, posto): (order_id, settore_prezzi)}` — seats with at least one CONFIRMED or RESALE row.
- `free: {(settore, fila, posto): settore_prezzi}` — seats with only CANCELLED rows (truly unoccupied).

**Why this is needed**: The CSV can contain multiple rows per physical seat — for example, a CANCELLED row (from a returned ticket) plus a CONFIRMED row (from the repurchase). The rule is: if any CONFIRMED or RESALE row exists for `(settore, fila, posto)`, the seat is occupied, regardless of CANCELLED rows. Only a seat with exclusively CANCELLED rows is truly free.

**Logic**:
1. `active` = rows where `Stato posto ∈ OCCUPIED`.
2. `active_keys` = set of `(settore, fila, posto)` tuples from `active` — the "has any active booking" fingerprint.
3. `act_dedup` = deduplicate active rows per physical seat (drop_duplicates on the three key columns). For each unique physical seat, take the first row's order ID. This assumes all CONFIRMED/RESALE rows for the same seat belong to the same order — which is the business-rule expectation.
4. Build `occupied` dict from `act_dedup`.
5. `canc` = rows where `Stato posto == 'CANCELLED'`.
6. `canc_dedup` = deduplicate, then keep only those seats whose `(s, f, p)` is **not** in `active_keys`. These are the truly empty seats.
7. Build `free` dict from the filtered cancelled rows.

**Called by**: `process_event()`.

---

### `build_segments(occupied, free)` — line 93

**Purpose**: Partition seats into independent sub-problems.

**Input**: `occupied` and `free` dicts from `resolve_seats`.

**Output**: `dict[(settore, fila, sp), {'seats': {posto: order_id}, 'free': set[posto]}]`

**Logic**: Iterates both input dicts. For each occupied seat `(s, f, p)` with order `(oid, sp)`, it adds `posto → oid` to `segs[(s, f, sp)]['seats']`. For each free seat `(s, f, p)` with `sp`, it adds `posto` to `segs[(s, f, sp)]['free']`. `defaultdict` handles first-access initialization.

**Why `(settore, fila, settore_prezzi)` not `(settore, fila)` alone**: A single row may contain seats at different price levels. Swapping a seat across price levels would change the buyer's price category — forbidden. Adding `settore_prezzi` to the segment key enforces this constraint structurally.

**Called by**: `process_event()`.

---

### `contiguous_runs(positions)` — line 112

**Purpose**: Decompose a sorted list of integers into maximal consecutive runs.

**Input**: a sorted `list[int]` of seat positions.

**Output**: `list[list[int]]` — each inner list is a run of consecutive integers.

**Example**: `[1, 2, 3, 5, 6, 10]` → `[[1, 2, 3], [5, 6], [10]]`

**Logic**: Linear pass. Maintain `cur` as the current run. If the next position equals `cur[-1] + 1`, extend `cur`. Otherwise, save `cur` and start a new one.

**Why it exists**: The solver needs to enumerate all length-k windows in the available positions as candidate blocks. These windows can only span within a run — you can't form a consecutive block `[5, 6]` from positions `[5, 10]`. `contiguous_runs` cleanly isolates the reachable sub-ranges.

**Called by**: `solve_segment()` once at its start, indirectly via the `candidate_blocks` inner function.

---

### `is_adjacent(postos)` — line 127

**Purpose**: Check whether a list of seat numbers is already consecutive.

**Input**: `list[int]` of seat positions (unsorted).

**Output**: `bool`

**Logic**: Sort the list. Return `True` if every adjacent pair differs by exactly 1. A list of length ≤ 1 is trivially adjacent.

**Example**: `is_adjacent([5, 7])` → `False`. `is_adjacent([5, 6, 7])` → `True`.

**Called by**: `solve_segment()` to build `to_fix` — only orders that are in `problematic_set` AND fail `is_adjacent` actually need work.

---

### `solve_segment(seats, free_postos, problematic_set)` — line 136

This is the heart of the algorithm. It is the most complex function and is broken down phase by phase below.

**Purpose**: Find the lowest-cost permutation of seats within one segment such that every problematic order's seats become consecutive.

**Inputs**:
- `seats: dict[posto, order_id]` — current occupied assignment within the segment.
- `free_postos: set[posto]` — currently unoccupied positions in the segment.
- `problematic_set: set[order_id]` — which orders need to be fixed.

**Output**: `(moves, infeasible)` where `moves = [(order_id, old_posto, new_posto), ...]` and `infeasible = [order_id, ...]`.

#### Phase 1 — Setup

```python
order_postos: dict = defaultdict(list)
for pos, oid in seats.items():
    order_postos[oid].append(pos)
for oid in order_postos:
    order_postos[oid].sort()
```

Inverts `seats` to produce `order_postos: {order_id: [sorted postos]}`. This is the working representation throughout.

```python
to_fix = {
    oid: postos
    for oid, postos in order_postos.items()
    if oid in problematic_set and not is_adjacent(postos)
}
```

`to_fix` is the subset of problematic orders in this segment that are genuinely non-adjacent. Orders already adjacent (or single-seat) are excluded. If `to_fix` is empty, the function returns immediately.

```python
all_pos = sorted(set(seats) | free_postos)
runs    = contiguous_runs(all_pos)
```

`all_pos` is the universe of available positions (occupied + free). `runs` splits it into consecutive stretches.

#### Phase 2 — Candidate block enumeration (inner function `candidate_blocks`)

```python
def candidate_blocks(k: int) -> list:
    blocks = []
    for run in runs:
        for i in range(len(run) - k + 1):
            blocks.append(tuple(run[i: i + k]))
    return blocks
```

Returns every contiguous window of length `k` across all runs. For a run `[10, 11, 12, 13]` with `k=2`, the blocks are `(10,11), (11,12), (12,13)`. A block must be entirely within one run because positions in different runs are not consecutive by definition.

#### Phase 3 — Sort problematic orders largest-first

```python
prob_list = sorted(to_fix.items(), key=lambda x: -len(x[1]))
```

Orders with more seats are harder to place (fewer valid k-length windows exist). Placing them first in the search tree causes infeasibility to be detected earlier, enabling aggressive pruning.

#### Phase 4 — Cost function (inner function `step_cost`)

```python
def step_cost(block: tuple, old_p: list) -> int:
    new_p  = sorted(block)
    disp   = sum(abs(n - o) for n, o in zip(new_p, old_p))
    evict  = sum(1 for p in block if p in seats and seats[p] not in to_fix)
    return disp + evict * 100
```

- `disp`: total displacement — sum of absolute position changes, using sorted-to-sorted pairing. By the rearrangement inequality, this pairing minimizes the sum of absolute differences for any given set of new positions.
- `evict`: how many non-problematic occupants this block would displace.
- The 100× weight makes evicting a single non-problematic occupant equivalent to moving an order 100 seats, so the algorithm strongly prefers to use free slots.

Note: `seats[p] not in to_fix` checks whether the current occupant at position `p` is a non-problematic order. Evicting a problematic order from its current slot costs nothing extra since we're already reassigning it.

#### Phase 5 — Greedy warm-start

```python
g_taken: set       = set()
g_placement: dict  = {}
for oid, old_p in prob_list:
    k = len(old_p)
    centroid = sum(old_p) / k
    for block in sorted(candidate_blocks(k), key=lambda b: abs(sum(b) / k - centroid)):
        if not any(p in g_taken for p in block):
            g_placement[oid] = block
            g_taken.update(block)
            break
if len(g_placement) == len(prob_list):
    g_cost = sum(step_cost(g_placement[oid], old_p) for oid, old_p in prob_list)
    best['cost']      = g_cost
    best['placement'] = g_placement
```

Iterates problematic orders largest-first. For each, picks the closest available block (by centroid-to-centroid distance) that doesn't overlap already-claimed positions. This is a pure greedy — first-fit by proximity. If all orders are placed, it records the solution in `best`.

**Why this matters**: The greedy solution seeds `best['cost']`. The branch-and-bound then prunes any branch where `partial_cost >= best['cost']`. A good warm-start eliminates vast portions of the search tree.

If the greedy fails to place all orders (some couldn't find a non-overlapping block), `len(g_placement) < len(prob_list)`, the `if` doesn't trigger, and `best['cost']` stays `inf`. The backtracker then starts completely unguided.

#### Phase 6 — Branch-and-bound backtracking (inner function `backtrack`)

```python
def backtrack(idx: int, taken: set, placement: dict, partial_cost: int):
    if time.time() > deadline:
        return
    if partial_cost >= best['cost']:
        return

    if idx == len(prob_list):
        if partial_cost < best['cost']:
            best['cost']      = partial_cost
            best['placement'] = dict(placement)
        return

    oid, old_p = prob_list[idx]
    k          = len(old_p)
    centroid   = sum(old_p) / k

    blocks = sorted(
        candidate_blocks(k),
        key=lambda b: abs(sum(b) / k - centroid),
    )[:MAX_BRANCHES]

    for block in blocks:
        if any(p in taken for p in block):
            continue
        cost_here = step_cost(block, old_p)
        if partial_cost + cost_here >= best['cost']:
            continue
        placement[oid] = block
        backtrack(idx + 1, taken | set(block), placement, partial_cost + cost_here)
        del placement[oid]
```

The recursive function places one order per level (`idx` is the depth). Pruning happens at two points:
1. **Time limit**: `time.time() > deadline` — 1 second per segment.
2. **Cost bound**: `partial_cost >= best['cost']` — cuts branches that can't beat the current best.

For each level, it tries up to `MAX_BRANCHES=25` candidate blocks, sorted by proximity to the order's current centroid. Overlap with already-placed orders is checked via `taken`. When all orders are placed (`idx == len(prob_list)`), if the total cost is better than `best['cost']`, the solution is recorded.

`taken | set(block)` creates a new set each call (functional style), so backtracking is clean — when the recursive call returns, `taken` is unchanged. `placement` is mutated in-place and cleaned up with `del placement[oid]` after each recursive call.

**Called with**: `backtrack(0, set(), {}, 0)` — empty initial state.

#### Phase 7 — Build new full assignment

After search, `best['placement']` maps each problematic order to its chosen block.

```python
prob_taken = {p for postos in best['placement'].values() for p in postos}
```

All positions claimed by problematic orders.

**Step 1** — Problematic orders at their chosen new positions:
```python
for oid, postos in best['placement'].items():
    for p in postos:
        new_asgn[p] = oid
```

**Step 2** — Non-problematic orders whose seats were not touched stay in place:
```python
for pos, oid in seats.items():
    if oid not in to_fix and pos not in prob_taken:
        new_asgn[pos] = oid
```

**Step 3** — Non-problematic orders with evicted seats get the nearest remaining positions:
```python
remaining = sorted(p for p in all_pos if p not in new_asgn)
for oid, old_p in order_postos.items():
    if oid in to_fix:
        continue
    n_evicted = sum(1 for p in old_p if p in prob_taken)
    if n_evicted == 0:
        continue
    centroid = sum(old_p) / len(old_p)
    remaining.sort(key=lambda p: abs(p - centroid))
    for p in remaining[:n_evicted]:
        new_asgn[p] = oid
    remaining = remaining[n_evicted:]
```

For each evicted non-problematic order, sorts remaining open positions by distance from the order's centroid and takes the `n_evicted` closest ones. This is greedy — it processes orders one at a time without coordinating across them.

#### Phase 8 — Emit moves

```python
inv: dict = defaultdict(list)
for pos, oid in new_asgn.items():
    inv[oid].append(pos)
for oid in inv:
    inv[oid].sort()

moves = []
for oid, old_p in order_postos.items():
    new_p = inv.get(oid, old_p)
    for old, new in zip(old_p, new_p):
        if old != new:
            moves.append((oid, old, new))
```

Inverts `new_asgn` to get `{order_id: [sorted new postos]}`. Then for each order, pairs old sorted positions with new sorted positions via `zip` and emits a move for every changed seat.

---

### `process_event(event_df, problematic)` — line 293

**Purpose**: Orchestrate processing for one event: resolve seats, build segments, detect globally infeasible orders, invoke segment solver.

**Inputs**:
- `event_df`: all ticket rows for this event.
- `problematic: set[order_id]`: orders that need fixing for this event.

**Output**: `(all_moves, all_infeasible)` — combined results from all segments.

**Logic**:
1. `resolve_seats(event_df)` → `occupied`, `free`.
2. `build_segments(occupied, free)` → `segments`.
3. **Cross-segment detection**: For each segment, for each problematic order in that segment, record which segments that order appears in (`order_segments`). If an order appears in more than one segment key, it is `globally_infeasible` — its seats span different `(settore, fila, sp)` combinations, which cannot be made adjacent without violating constraints.
4. `fixable = problematic - globally_infeasible`.
5. For each segment: skip it if it contains no fixable orders. Otherwise call `solve_segment(seg['seats'], seg['free'], fixable)`.
6. Collect moves and infeasible lists. Moves are enriched with settore/fila/sp metadata here (the solver returns only order/old_posto/new_posto).

**Called by**: `main()`.

---

### `main()` — line 340

**Purpose**: Top-level entry point — load, process, write output.

**Logic**:
1. `load_tickets('report_cleaned.csv')` → `tickets` DataFrame. Print row count.
2. `parse_orders('orders.txt')` → `orders_by_event` dict.
3. `tickets.groupby('Data evento')` — iterate over events. For each event with problematic orders, call `process_event`. Stamp each move dict with `Data evento`.
4. After all events: build infeasible rows — for each infeasible `(event_date, order_id)`, look up the order's actual current seats in the `tickets` DataFrame and emit rows with `Stato = 'NON RISOLVIBILE'` and `Posto originale == Posto nuovo` (no move, just a marker).
5. Write `reallocation.xlsx` with `pd.ExcelWriter`, one sheet per event. Sheet names are sanitized (colons → dots, slashes → hyphens, truncated to 31 chars — Excel's limit).
6. Print final summary.

**Output columns**: `['Codice ordine', 'Settore', 'Fila', 'Settore prezzi', 'Posto originale', 'Posto nuovo', 'Stato']`. `Data evento` is excluded from columns because it is already encoded in the sheet name.

---

## 4. Execution flow trace

**Scenario**: Event `2026-05-30 21:00:00.0`. The output shows a 3-way rotation among orders `3410490`, `3417618`, and `3420777` in segment `(Settore B, -, PRIMO SETTORE NUMERATO)`:

```
3410490: seat 59 → 65
3417618: seat 55 → 59
3420777: seat 65 → 55
```

**Step 1 — `load_tickets`**

The CSV is read. Rows for the three orders above have `Stato posto = CONFIRMED` and survive the filter. `Posto` is cast to int.

**Step 2 — `parse_orders`**

Line 1 of `orders.txt`: `"2026-05-30 21:00:00.0: ['3417618', '3419185', ...]"` → `{'3417618', '3419185', ...}`. Order `3410490` is not in this set — it is a non-problematic order.

**Step 3 — `main` iterates events**

`tickets.groupby('Data evento')` yields the 2026-05-30 group. `problematic = {'3417618', ...}` — the 17 orders from the file.

**Step 4 — `process_event`**

`resolve_seats(event_df)`:
- Seat `(Settore B, -, 55)` → CONFIRMED by `3417618`.
- Seat `(Settore B, -, 65)` → CONFIRMED by `3420777` (hypothetically, its second seat alongside another).
- Seat `(Settore B, -, 59)` → CONFIRMED by `3410490`.

`build_segments`: All three land in `(Settore B, -, PRIMO SETTORE NUMERATO)` → `seats = {55: '3417618', 65: '3420777', 59: '3410490', ...}`.

Cross-segment check: `3417618` and `3420777` each appear in only one segment → `globally_infeasible = {}`. Both are `fixable`.

**Step 5 — `solve_segment`**

`order_postos`:
- `3417618`: [55, 65] (non-adjacent — `to_fix`)
- `3420777`: [65, ...] (non-adjacent — `to_fix`)
- `3410490`: [59] (single seat — adjacent, not in `to_fix`)

`all_pos` includes 55, 59, 65 and surrounding free positions. `prob_list` sorted by size.

**Greedy warm-start**: Places `3417618` at the closest available 2-block to centroid 60. Say `(59, 60)` — but 59 is occupied by `3410490` (eviction cost +100). Tries `(60, 61)` if free. Continues until a valid block is found.

**Backtracking**: Explores if placing `3417618` at `(55, 56)` (displacement=10+9=19, 0 evictions) and `3420777` at `(64, 65)` (displacement=1+0=1, 0 evictions) beats the greedy solution. Whichever placement minimizes total cost becomes `best`.

In the actual output, the solver chose to rotate the three orders (3-way swap), meaning placing `3417618` at `(59, 60)` or similar caused `3410490` to be evicted from 59 and reassigned to 65 (vacated by `3420777`'s move).

**Phase 7 — Assignment**:
- `3417618` → positions from `best['placement']`
- `3420777` → positions from `best['placement']`
- `3410490` (evicted from 59) → nearest remaining position: 65

**Phase 8 — Moves emitted**:
- `(3417618, 55, 59)`, `(3420777, 65, 55)`, `(3410490, 59, 65)`

**Step 6 — Back in `main`**

Moves get `Data evento = '2026-05-30 21:00:00.0'` stamped. After all events, `reallocation.xlsx` written with one sheet per event.

---

## 5. Decision logic

### How candidates are evaluated: `step_cost`

```
cost = Σ|new_pos_i - old_pos_i|  +  100 × (# non-problematic orders evicted)
```

- Displacement is measured by pairing sorted new positions with sorted old positions. By the rearrangement inequality, this pairing minimizes the sum of absolute differences.
- The 100× multiplier on evictions ensures: "moving an order even 99 positions is preferable to evicting one innocent bystander." In practice, segments rarely span 100 seats, so eviction is truly a last resort.

### How the algorithm chooses among alternatives

The backtracker explores candidate blocks sorted by `|centroid_of_block - centroid_of_order|` — proximity first. This means the search tree is ordered best-first at each level. Combined with branch-and-bound pruning (`partial_cost >= best['cost']`), the algorithm tends to find the optimal solution quickly without exhaustively exploring distant candidates.

### Constraints enforced

| Constraint | Enforcement mechanism |
|---|---|
| Same sector/row/price | Structural — segment decomposition makes cross-constraint moves impossible |
| Consecutive seats only | `candidate_blocks` only produces windows from contiguous runs |
| No position overlap | `taken` set in `backtrack` + `any(p in taken for p in block)` check |
| Cross-segment orders rejected | `globally_infeasible` check in `process_event` before any solver call |

### How conflicts are resolved

The branch-and-bound naturally resolves conflicts: if two problematic orders both want the same block, only one branch assigns it to the first order, and the other must pick another block. Both branches are explored (up to `MAX_BRANCHES`), and the winner is whichever minimizes total cost.

### Scoring and prioritization

- **Largest orders first** in `prob_list` — maximizes early pruning.
- **Closest-first block ordering** within the search — maximizes probability that the first valid complete assignment is near-optimal.
- **Greedy warm-start** seeds a strong initial upper bound — the backtracker typically only has to verify it can't do better, pruning most branches immediately.

---

## 6. Data structures

### `tickets: pd.DataFrame`

Flat, row-per-ticket structure. Columns: `Codice ordine, Stato ordine, Stato posto, Data evento, Item, Settore, Fila, Posto, Settore prezzi`. Used only for initial loading and filtering. After `groupby('Data evento')`, each group is passed to `process_event` and not touched directly again.

### `occupied: {(settore, fila, posto): (order_id, settore_prezzi)}`

Dict keyed by physical seat identity (3-tuple). Value carries the owner order and price category. Uniquely identifies every occupied seat in an event and who owns it.

### `free: {(settore, fila, posto): settore_prezzi}`

Same key structure as `occupied`. Contains only genuinely unoccupied seats (not blocked by any CONFIRMED/RESALE). Value is just `settore_prezzi` to allow correct segment assignment.

### `segments: {(settore, fila, sp): {'seats': {posto: order_id}, 'free': set[posto]}}`

The core decomposition. Each segment is self-contained. `seats` maps position (int) to order ID. `free` is a set of available positions. The solver operates exclusively on `seats` and `free` — no knowledge of other segments is required.

### `order_postos: {order_id: [sorted postos]}`

Inverted index within a segment. Built at the start of `solve_segment` by inverting `seats`. Allows O(1) lookup of where an order currently sits. Sorted to enable correct position pairing in move emission.

### `to_fix: {order_id: [postos]}`

Subset of `order_postos` — only orders that are both in `problematic_set` and not already adjacent. The target list for the solver.

### `best: {'cost': float, 'placement': dict}`

Shared mutable closure state for the branch-and-bound. Tracks the best complete solution found so far. Updated whenever `backtrack` finds a strictly better assignment. The `dict(placement)` copy on update is essential — without it, the mutable `placement` dict would corrupt the stored solution as the search continues.

### `placement: {order_id: tuple[posto, ...]}`

Partial assignment built up during backtracking. Mutated in-place (`placement[oid] = block` / `del placement[oid]`) for efficiency. The `del` is the explicit backtracking step.

### `taken: set[posto]`

Immutable per call — a new set is created via `taken | set(block)` at each level. This functional style ensures the parent call's `taken` is unchanged after a recursive call returns, making the backtracking clean without explicit undo.

### `new_asgn: {posto: order_id}`

Final seat assignment after solving. Built in three sequential passes: (1) problematic orders, (2) un-evicted non-problematic, (3) evicted non-problematic. Then inverted to `inv` for move computation.

### `remaining: list[posto]`

Mutable list of unassigned positions, re-sorted by centroid distance for each evicted non-problematic order. Shrinks as positions are assigned out.

---

## 7. Weak points and complexity hotspots

### 1. Evicted non-problematic orders may themselves become scattered

The repair phase assigns the nearest available positions to evicted orders. But "nearest" is per-order greedy and does not consider whether the evicted order's remaining (un-evicted) seats are still adjacent.

**Example**: Order A has seats [10, 11, 12]. The solver evicts seat 11 for a problematic order. Repair assigns seat 20 as the nearest available. Now order A sits at [10, 12, 20] — scattered. This is not caught or corrected. The order will not appear in future runs unless explicitly listed in `orders.txt`.

### 2. `MAX_BRANCHES = 25` is a hard approximation cap

If the optimal block placement for an order is the 26th closest block by centroid distance, it will never be found. In segments with many occupied seats and little free space, the globally optimal solution may be missed. The greedy warm-start helps by bounding cost, but the backtracker still cannot search beyond position 25 in the candidate list.

### 3. Greedy warm-start can fail silently

If the greedy cannot place all orders (first-fit conflicts), `len(g_placement) < len(prob_list)` and `best['cost']` stays `inf`. The backtracker then starts with no upper bound. Depending on the search space size and the 1-second limit, it may or may not find an optimal solution before timing out.

### 4. `candidate_blocks` is re-evaluated at every backtrack level

`candidate_blocks(k)` is called fresh at every level of `backtrack`. Since `runs` is a closure from the outer scope and never changes during search, the result is always the same for a given `k`. Caching it by `k` (e.g., with a dict or `lru_cache`) would be a trivial optimization. In practice this is fast because segment sizes are small, but it is a hidden redundancy.

### 5. `order_segments` detection is O(segments × seats_per_segment)

The loop in `process_event` that maps each problematic order to its set of segment keys iterates every segment and every seat within it. For a large event, this is O(S × N). In practice fine, but not immediately obvious.

### 6. Move emission uses positional pairing by sorted rank

In the final step, old and new positions for each order are paired by sorted index:
```python
for old, new in zip(old_p, new_p):
```
This is mathematically correct for minimizing total displacement (rearrangement inequality), but it means the move record `(oid, 55, 59)` says "the seat at position 55 in the old assignment becomes position 59 in the new assignment" — not necessarily that the specific physical ticket was swapped. Consumers of the output should understand this is a positional re-labeling, not a per-ticket tracking record.

### 7. Excel sheet name collision risk

Sheet names are derived as `str(event_date).replace(':', '.').replace('/', '-')[:31]`. Two distinct event date strings that produce identical sanitized names (within 31 chars) would silently collide. In practice, event dates are distinct timestamps, so this is unlikely but not guarded against.

### 8. `fixable` is event-level, passed to segment-level solver

`fixable` (the event-wide set of order IDs that are not globally infeasible) is passed directly to `solve_segment` as `problematic_set`. The solver narrows it correctly because `order_postos` is built from `seg['seats']`, which only contains orders present in that segment. So the cross-segment filtering is implicit rather than explicit — correct, but non-obvious to a reader.

### 9. Partially evicted non-problematic multi-seat orders become scattered

If an order has seats [5, 6, 7] and only seat 6 is evicted (`n_evicted=1`), the repair assigns one new position elsewhere. The order ends up with seats [5, new_pos, 7] — non-consecutive. The solver's eviction cost (`evict * 100`) tries to avoid this scenario, but when eviction is unavoidable, the greedy repair does not maintain adjacency for non-problematic orders and does not trigger any follow-up fix.

---

## Appendix: Key constants

| Constant | Value | Meaning |
|---|---|---|
| `OCCUPIED` | `{'CONFIRMED', 'RESALE'}` | Seat statuses that count as taken |
| `VALID` | `{'CONFIRMED', 'RESALE', 'CANCELLED'}` | Statuses kept after CSV filter |
| `MAX_BRANCHES` | `25` | Max candidate blocks tried per order per backtrack level |
| Time limit | `1.0` s | Per-segment backtracking budget (`deadline = time.time() + 1.0`) |
| Eviction weight | `100` | Penalty multiplier for displacing a non-problematic occupant |

## Appendix: Input/output formats

### `report_cleaned.csv`

```
Codice ordine, Stato ordine, Stato posto, Data evento, Item, Settore, Fila, Posto, Settore prezzi
3406628, REGULAR, CONFIRMED, 2026-06-27 21:00:00.0, ..., Prato Gold, GA, 3, PRATO GOLD
```

### `orders.txt`

```
2026-05-30 21:00:00.0: ['3417618', '3419185', ...]
2026-06-06 20:30:00.0: ['3406711', '3407635', ...]
```

One line per event. Format is a Python list literal after the colon.

### `reallocation.xlsx`

One sheet per event. Columns: `Codice ordine, Settore, Fila, Settore prezzi, Posto originale, Posto nuovo, Stato`.

- `Stato = 'SPOSTATO'` — seat was successfully moved.
- `Stato = 'NON RISOLVIBILE'` — order could not be fixed; row shows current seat with no change.
