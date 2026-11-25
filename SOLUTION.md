# Basket quote service and after-sales ledger

A standalone, Python 3.11 solution for the brief in `README.md`:

* an **optimal basket quote service** with immutable published snapshots,
  deterministic two-level tie-breaking and proportional per-unit discount
  allocation;
* an **after-sales ledger** that binds each sale to a complete published quote
  and refunds exactly the per-unit amount that sale paid, with idempotent
  requests, all-or-nothing overlap rejection, and transactional stock moves;
* a **nib-grinding workshop** (`basket_service/nib.py`) that reads sold, not
  refunded nibs by sale+unit, keeps per-piece try-write evidence and each
  piece's reachable shapes, schedules one grind slot per shift, and only signs
  orders it can guarantee to return to the owner.

No external dependencies are required — only the standard library and SQLite.

## Quick start

```bash
# Full demo: rebuilds data/demo.db and writes results/demo_plan.json +
# results/demo_results.json, including independent cross-checks.
python3 -m basket_service demo

# Unit / differential / concurrency tests (40 tests).
python3 -m basket_service self-check
# or: python3 -m unittest discover -s tests -v

# Run a JSON plan against a real database.
python3 -m basket_service run-plan results/demo_plan.json --db data/plan.db
```

Callable entry point:

```python
from basket_service import open_service

svc = open_service("data/shop.db")

snap = svc.publish_snapshot(products=[...], coupons=[...])   # immutable snapshot
quote = svc.quote(basket, coupons=[], request_id="q1", snapshot_id=snap)
sale = svc.register_sale("S1", quote=quote)                   # binds the full quote
result = svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
svc.adjust_inventory("adj1", "B", 3, "RESTOCK")               # independent stock event
```

`register_sale` also accepts `request_id=`/`quote_id=` when the ledger shares
the quote database; or pass `quote=` with any complete quote payload (for
example one issued by an independent quote service) — the ledger validates the
payload structurally and **never re-runs the optimizer** for sales or refunds.

## Contract summary

### Quote

* A basket is a list of physical units with unique `unit_id`, a `sku` and a
  positive integer `price` (booleans rejected). Input order never affects the
  result.
* A coupon has a stable `coupon_id`, a positive integer `D`, and
  `requires: sku -> count`. One use consumes exactly those units, which must
  have total original price at least `D`; coupons share no physical units; each
  coupon is used at most once; untouched units stay at full price.
* The total is the minimum possible `sum(price) - sum(D)`.
* Tie-break: smaller sorted tuple of used `coupon_id`s first; if equal, compare
  the per-coupon sorted `unit_id` lists in coupon-id order.
* Each coupon's `D` is split over its units proportionally to original price:
  floor of the exact share, then one cent each to the largest fractional
  remainders, ties by `unit_id` ascending. Per-unit discount never exceeds the
  unit price and the paid amounts sum to the total.
* The service stores quotes by `request_id`: same request + canonical basket +
  snapshot replays the identical full quote; different content is
  `REQUEST_CONFLICT`. New snapshots never change old quotes.

### Ledger

* A `sale_id` binds one complete quote; the same body replays, different content
  is `SALE_CONFLICT`. Per-unit original paid amounts are persisted.
* `return(request_id, sale_id, unit_ids, grade)` refunds each unit's stored
  original paid amount, regardless of current prices/promotions. `grade` is
  `RESTOCK` or `SCRAP` per unit and drives the stock bucket.
* A unit returns at most once. A request is all-or-nothing: any already-returned
  or ineligible unit rejects the whole request, processing nothing.
* Same `request_id` + same body replays the first result (even after other
  units were returned); same id with a different body is `REQUEST_CONFLICT`.
* Refund and inventory receipt/scrap are written in one `BEGIN IMMEDIATE`
  transaction, so a failure or concurrent request never leaves a refund
  without its stock move or double-counts stock.
* `adjust_inventory(event_id, sku, quantity, bucket)` records independent
  restock/scrap events (idempotent by event id); it never changes any sale's
  refundable amounts and never resurrects returned units.
* Sales do not imply stock-out; after-sales stock starts at zero and is
  reconciled only from its own events.

### Nib-grinding workshop

A nib is registered against a *sold, not refunded* unit (`register_nib`), with
the catalogue of **hypotheses** its model admits plus the owner, allowed width
band and return deadline. Each piece is a separate physical unit: two units of
the same model keep independent try-write records.

**Width model** (`shape_of`). Each hypothesis is `(kind, base, side_wide)`:

* straight writes its base width in both directions — `(b, b)`;
* side writes one wide (`b+1`) and one narrow (`b-1`); the wide direction is a
  property of the piece and **swaps when the hand swaps** (`换手对调两向`);
* every width is an integer, and a grind subtracts one from the base for the
  whole piece. A straight nib survives to base ≥ 1, a side nib to base ≥ 2;
  metal is never recovered, so the grind count only rises and is shared by every
  order on the piece.

**Try-writes** (`record_trial`) are directional readings attached to one piece;
a reading retains exactly the hypotheses that explain it *at that grind level*.
Adding a contradictory reading is rejected and rolled back. Because evidence
only ever removes hypotheses, a guarantee computed on the survivors remains
valid as more readings arrive.

**Delivery guarantee** (`open_nib_order`). The counter signs only when the
customer's two-direction interval, deadline and a one-grind-slot-per-shift
schedule are simultaneously satisfiable **for every surviving hypothesis** — it
never has to know the nib type to promise the result. Two strategies:

* `PREGRIND` — grind now within the range guaranteeable without identifying the
  type (for urgent, same-shift orders). If no such grind exists it suspends the
  order (`HUNG`).
* `REMEASURE` — keep the original width, occupy one whole shift for the owner's
  two-direction try-write, then decide (`AWAITING_REMEASURE` → signed/refused at
  `remeasure_nib`). This is the "return it as it is" route and never removes
  metal until the type is known.

Each accepted order reserves its shifts (`nib_shifts`), so a slot cannot be
promised to two orders, and one piece cannot be signed to two customers while an
order on it is active. `deliver_nib` requires the planned grinds, checks the
actual measured pair against a surviving type and the signed interval, records
that evidence, and hands the physical piece to the claimant. A query
(`query_nib`) reports who holds the piece and whether the original width is
intact; a refunded or delivered piece cannot start a new order, and repeating a
delivery replays the identical original record.

For the brief's piece **N** (straight 2 → `(2,2)`, reverse side 3 → `(2,4)`, only
H=2 observed): the urgent `H=1, V∈1..3` same-shift order is signed after one
grind (both hypotheses land in band); the "return as-is (2,2)" order waits one
shift, then signs if N reads `(2,2)` and is refused if it reads `(2,4)`.

### Error codes

`INVALID_BASKET`, `INVALID_COUPON`, `INVALID_INPUT`, `INVALID_GRADE`,
`INVALID_QUOTE`, `UNKNOWN_SNAPSHOT`, `SNAPSHOT_CONFLICT`, `REQUEST_CONFLICT`,
`QUOTE_CONFLICT`, `UNKNOWN_QUOTE`, `UNKNOWN_SALE`, `SALE_CONFLICT`,
`QUOTE_MISMATCH`, `UNIT_INELIGIBLE`, `UNIT_ALREADY_RETURNED`,
`OVERLAPPING_RETURN`, `INVENTORY_CONFLICT`, and for nibs:
`NIB_UNKNOWN`, `NIB_UNIT_CONFLICT`, `NIB_UNIT_RETURNED`, `NIB_UNIT_DELIVERED`,
`NIB_UNIT_NOT_IN_SHOP`, `NIB_ORDER_UNKNOWN`, `NIB_ORDER_CONFLICT`,
`NIB_ORDER_STATE`, `NIB_NO_PLAN`, `NIB_EVIDENCE_MISMATCH`,
`NIB_REQUIREMENT_VIOLATED`, `NIB_DELIVERY_CONFLICT`, `NIB_ALREADY_DELIVERED`,
`NIB_SLOT_CONFLICT`, `NIB_INVALID_TRIAL`.

Every public operation either commits a complete result or raises before any
partial state is written.

## Design trade-offs

* **Exact optimization, no greedy.** A bitmask DP over ≤10 units computes the
  maximum discount; an ignored-coupon subset scan then finds the
  lexicographically smallest coupon-id set reaching it; a feasibility-guided
  prefix greedy fixes the lexicographically smallest unit-id lists. This is
  fast (milliseconds at the specified limits) and deterministic. Greedy
  "largest D first" is demonstrably wrong (see the `greedy_trap` scenario).
* **Snapshot-bound quotes.** Quotes carry the snapshot id, the canonical
  basket, the coupon catalogue, assignments, the full allocation and the total.
  Passing a different ad-hoc coupon list under an existing snapshot is a
  conflict; publishing a new snapshot never mutates an old quote.
* **Ledger uses stored facts.** Refund eligibility and amounts come from the
  sale's stored unit rows (protected by primary keys and conditional updates
  inside a SQLite write transaction). Nothing about historical returns,
  condition or stock counts is inferred from a quote.
* **Concurrency.** Writers use `BEGIN IMMEDIATE` with a busy timeout; the
  unit-level guard is a conditional `UPDATE ... WHERE returned = 0` re-checked
  in-transaction, so two connections that both read "returnable" still produce
  exactly one success. This is database locking, not an in-process set.
* **Reopen.** Snapshots, quotes, sales, return requests/items, adjustments,
  inventory events and totals all live in SQLite and survive process restart.

## Layout

```
basket_service/
  __init__.py      Service facade / open_service
  errors.py        typed error codes
  validation.py    strict input + quote payload validation, allocation
  optimizer.py     exact bitmask optimizer + deterministic tie-break
  reference.py     independent brute-force / Fraction reference (tests only)
  db.py            SQLite schema and transaction helpers
  quote_service.py snapshots and quote API
  ledger.py        sales, returns, inventory, audits
  planner.py       JSON plan runner
  nib.py           nib-grinding workshop (width model, evidence, orders, slots)
  demo.py          demo driver + CLI (demo / run-plan / self-check)
tests/             optimizer, quote-service, ledger, reference tests
data/              real SQLite databases (created by the demo)
results/           JSON plans and result records
```

## Verification

`python3 -m basket_service demo` runs 26 checks, all computed against
independent references:

* case-material quotes vs. exhaustive enumeration;
* two sales under different promotions, refunds following each sale's own
  bound quote (including after a promotion switch);
* greedy-largest-D counterexample and contention/tie-break/remainder examples;
* a 10-unit basket optimized by full enumeration and audited from raw ledger
  rows (per-unit paid sums, refund sums, remaining refundable, stock totals);
* lost-response retry replay, whole-request overlap rejection, independent
  stock adjustments, and a two-thread concurrent return race with no double
  stock;
* the nib workshop: H=2/H=3 discriminations, both-1 deliverability with a free
  slot, a stolen slot blocking delivery, two parallel replays of piece N
  (urgent PREGRIND delivered then refused to a second customer; REMEASURE as-is
  signed on read 2 and refused on read 4), and a full nib plan replayed on a
  fresh database;
* reopening the database preserves everything.
