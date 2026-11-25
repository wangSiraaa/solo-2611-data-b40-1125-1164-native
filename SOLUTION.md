# Basket quote service and after-sales ledger

A standalone, Python 3.11 solution for the brief in `README.md`:

* an **optimal basket quote service** with immutable published snapshots,
  deterministic two-level tie-breaking and proportional per-unit discount
  allocation;
* an **after-sales ledger** that binds each sale to a complete published quote
  and refunds exactly the per-unit amount that sale paid, with idempotent
  requests, all-or-nothing overlap rejection, and transactional stock moves.

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

### Error codes

`INVALID_BASKET`, `INVALID_COUPON`, `INVALID_INPUT`, `INVALID_GRADE`,
`INVALID_QUOTE`, `UNKNOWN_SNAPSHOT`, `SNAPSHOT_CONFLICT`, `REQUEST_CONFLICT`,
`QUOTE_CONFLICT`, `UNKNOWN_QUOTE`, `UNKNOWN_SALE`, `SALE_CONFLICT`,
`QUOTE_MISMATCH`, `UNIT_INELIGIBLE`, `UNIT_ALREADY_RETURNED`,
`OVERLAPPING_RETURN`, `INVENTORY_CONFLICT`.

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
  optics.py        telescope/eyepiece handover, focus math, scheduling
  optics_demo.py   optics demo driver (18 checks)
  planner.py       JSON plan runner
  demo.py          demo driver + CLI (demo / optics-demo / run-plan / self-check)
tests/             optimizer, quote-service, ledger, optics, reference tests
data/              real SQLite databases (created by the demo)
results/           JSON plans and result records
```

## Verification

`python3 -m basket_service demo` runs 19 checks, all computed against
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
* reopening the database preserves everything.

## Optics handover subsystem

The astronomy club's borrowed-eyepiece scenario lives in
`basket_service/optics.py`, wired onto the same SQLite database and surfaced
through `Service.optics` (and as `optics-*` ops in the JSON planner).  It
composes with the ledger: a piece is only accepted when a lender signs it onto
a **real, unreturned sale unit**, so two identical-looking eyepieces remain
distinct physical items and cannot impersonate each other by model number.

```bash
python3 -m basket_service optics-demo   # 18 checks; writes
                                        # data/optics.db,
                                        # results/optics_results.json,
                                        # results/optics_plan.json
```

Chosen delivery strategy: **pre-locked verified barrel groups** (not loose
pieces with a live trial that consumes an observing window).  A group is a
telescope + eyepiece + ordered adapter chain; it may be handed to a duty
officer only after a signed focus trial, and while sealed no member may be lent
alone.  Unsealing forces re-verification.

### Focus model

* **Net demand** = eyepiece focal position − total optical path of all inserted
  pieces.  The focus wheel travels an **inclusive** integer interval
  `[travel_lo, travel_hi]` (both endpoints reachable, adjustable on site).
* An **adapter** has an integer optical path and a directed interface
  `in_iface -> out_iface`; a chain must match head-to-tail
  (scope output → adapters → eyepiece input) and may not reuse a piece.
* An **eyepiece** has a shoulder measurement interval plus the zero-point bias
  of its **barrel batch**.  The technician recovers that bias from a two-end
  scale and known test-focus cards as an inclusive interval of *all compatible
  calibrations*.  Each batch keeps its own bias — never shared across batches.
* A delivery is legal only if the **whole net interval** fits the wheel travel
  under **every** compatible calibration (a robust check), never a point
  estimate.  Calibration can be re-opened; placement and delivery recompute the
  verdict from live evidence, so a stale OK can never ship.

Worked example (the brief's positive case): focal 10, eyepiece+adapter path 6,
travel 3..4 ⇒ net 4, inclusive ⇒ deliverable.  Changing the shoulder evidence
to path 5 ⇒ net 5, needing travel 5 ⇒ not deliverable and the group cannot
lock.

### Scheduling and exclusivity

* Sessions are time intervals; two consecutive sessions may overlap.  Pieces
  are exclusively booked over their session interval (touching endpoints do not
  conflict).  A checked-in piece is held until returned: a later session cannot
  take it even if its window does not overlap.  A telescope likewise serves one
  overlapping/active order at a time.
* `switch_telescope` re-mounts an open order onto another scope only if the
  chain stays compatible and the net interval still fits — the "continues after
  a temporary swap" case; otherwise it must be refused and the order cancelled
  (`cancel_order`), matching the "one continues, one must retract" pair.

Limits: at most 3 telescopes, 6 pieces and 3 sessions (enforced with
`OPTIC_LIMIT`).  All ops are transactional and survive reopen; the demo
re-checks state before/after a close/reopen.

## Verification
