# Basket quote service, after-sales ledger and troupe shoe cabinet

A standalone, Python 3.11 solution for the brief in `README.md`:

* an **optimal basket quote service** with immutable published snapshots,
  deterministic two-level tie-breaking and proportional per-unit discount
  allocation;
* an **after-sales ledger** that binds each sale to a complete published quote
  and refunds exactly the per-unit amount that sale paid, with idempotent
  requests, all-or-nothing overlap rejection, and transactional stock moves;
* a **troupe fitting & cabinet subsystem** that recovers real left/right shoes
  from original order units, runs fitting slips, resolves width/stretch/pad
  constraints, and delivers whole pairs bound to one dancer per run — with two
  shop modes (pre-stretch vs per-show transfer) and late-return gating.

No external dependencies are required — only the standard library and SQLite.

## Quick start

```bash
# Full demo: rebuilds data/demo.db and writes results/demo_plan.json +
# results/demo_results.json, including independent cross-checks.
python3 -m basket_service demo

# Unit / differential / concurrency tests.
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

# Troupe cabinet: recover real shoes, fit, allocate, deliver whole pairs.
svc.recover_from_sale("R2", "R", "S1", "b", width=2, length=5, stretch_to=3)
svc.consign("R2", "dancer-1", "consent-receipt-1")           # named deposit + consent
svc.publish_fitting_slip("slip-R2", "R2")                    # fitting master's slip
alloc = svc.allocate("A1", [{"dancer_id": "jia", "right": {"width": 3}}], stretch_budget=1)
svc.confirm_delivery("A1", run_until="run-1-end")            # binds pair, ties laces
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

### Troupe fitting & cabinet

Every physical shoe is a real left or right object with its own fitting data.
It is **recovered from a real sale unit** (`recover_from_sale` / `register_physical`),
which reads the unit's stored `paid` amount and its `returned` flag from
`sale_units`; a unit already returned yields an unusable physical.  Store stock
(`origin="STOCK"`) has no ledger origin.

* **Eligibility.** A physical enters the cabinet only when a *named* dancer has
  deposited it and signed troupe-adjustment consent (`consign`).  `revoke_consignment`
  and an after-sales return (`confirm_return`) both make a shoe unusable for
  *new* allocation and revoke consent.
* **Fitting master.** `update_fitting` / `stretch` record length, instep width and
  stretch directions.  Widths only grow: a stretch below the current width or
  beyond the fitting limit is `WIDTH_IRREVERSIBLE`.  "Cannot stretch further" is
  encoded by setting `stretch_to` equal to the current width.
* **Fitting slips.** `publish_fitting_slip` pins the shoe's current attributes
  and lists the named dancers' feet it can reach without a pad.  The cabinet only
  consumes *released* slips (`require_slip=True`, the default).
* **Dancers.** `register_dancer` stores left/right width (or widths) plus an
  optional pinned length and run label.
* **Allocation.** `allocate` runs an exhaustive deterministic maximiser over the
  available shoes and returns **the largest number of complete whole pairs**.  It
  respects side, pinned length, current width (reachable up to `stretch_to` when
  the shoe stretches along `WIDTH`), the per-call `stretch_budget`, and the
  available narrowing pads (each pad narrows by one step and can sit in only one
  shoe).  Equal pair counts may break ties arbitrarily; the planner prefers fewer
  pads then fewer stretches.  An occupancy from a *different* run, an active
  delivery binding, a returned origin unit, or missing consent/slip all exclude a
  shoe (reported in `excluded`).
* **Delivery.** `confirm_delivery` re-checks consent/usability in the write
  transaction, applies the irreversible stretch, places any pad, and binds both
  shoes of a pair to the dancer for the whole run.  Tied laces, delivered width
  and pad id are persisted.  `return_delivery` / `handover_return` free the shoe
  and pad and record a receipt; late returns are flagged, not rejected.
* **Two shop modes.** `set_shop_mode`:
  * `PRESTRETCH` — pre-stretch and lock pairs for the whole run; works from its
    own physical stock and refuses the troupe's pad-transfer business
    (`INVALID_SHOP_MODE`);
  * `PERF_TRANSFER` — hand shoes and a movable pad over per show with a return
    receipt.  A pad held by the troupe must be drawn with a receipt
    (`acquire_borrowable_pad`); a pad borrowed by a dancer can only return through
    `record_pad_receipt` before the counter can take it.
* **Occupancy.** `assign_occupancy` / `release_occupancy` model a performance
  period's claim on a shoe.  Occupancy is *bookkeeping*: clearing it never
  changes a physical width, so it cannot turn a stretched width 3 back into 2.
* **Late after-sales return.** `confirm_return(confirmation_id, return_request_id,
  sale_id)` requires the ledger return to exist, marks affected physicals
  unusable, and revokes their consent — stopping *new* issuance.

### Error codes

`INVALID_BASKET`, `INVALID_COUPON`, `INVALID_INPUT`, `INVALID_GRADE`,
`INVALID_QUOTE`, `UNKNOWN_SNAPSHOT`, `SNAPSHOT_CONFLICT`, `REQUEST_CONFLICT`,
`QUOTE_CONFLICT`, `UNKNOWN_QUOTE`, `UNKNOWN_SALE`, `SALE_CONFLICT`,
`QUOTE_MISMATCH`, `UNIT_INELIGIBLE`, `UNIT_ALREADY_RETURNED`,
`OVERLAPPING_RETURN`, `INVENTORY_CONFLICT`, plus the troupe codes
`UNKNOWN_PHYSICAL`, `PHYSICAL_CONFLICT`, `UNKNOWN_FITTING_SLIP`,
`FITTING_CONFLICT`, `UNKNOWN_DANCER`, `DANCER_CONFLICT`, `CONSENT_REQUIRED`,
`CONSENT_REVOKED`, `UNKNOWN_DEPOSIT`, `DEPOSIT_CONFLICT`, `UNKNOWN_TRANSFER`,
`TRANSFER_CONFLICT`, `INVALID_ALLOCATION`, `ALLOCATION_CONFLICT`,
`WIDTH_IRREVERSIBLE`, `INVALID_SHOP_MODE`, `INVALID_FOOT`,
`PHYSICAL_UNAVAILABLE`, `UNKNOWN_PAD`, `PAD_CONFLICT`, `PAD_IN_USE`,
`INVALID_RECEIPT`, `RETURN_CONFIRMATION_CONFLICT`.

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
* **Cabinet uses physical facts.** Allocation reads each physical's *current*
  width and fitting limits from SQLite; a stored width only ever grows
  (`WIDTH_IRREVERSIBLE`), and occupancy/binding bookkeeping never rewrites it.
  Delivery re-validates consent and usability inside the write transaction, so a
  return or revocation between plan and delivery still blocks it.
* **Exhaustive pair planner.** For the small sizes in the brief the planner
  enumerates feasible shoe-to-foot options (with optional pad and at most one
  stretch) and maximises complete pairs; an independent
  `reference.max_pairs_bruteforce` re-derives the count in tests and the demo.

## Layout

```
basket_service/
  __init__.py      Service facade / open_service
  errors.py        typed error codes
  validation.py    strict input + quote payload validation, allocation
  optimizer.py     exact bitmask optimizer + deterministic tie-break
  reference.py     independent brute-force / Fraction / pair-count reference (tests only)
  db.py            SQLite schema and transaction helpers
  quote_service.py snapshots and quote API
  ledger.py        sales, returns, inventory, audits
  troupe.py        fitting slips, pads, allocation, paired delivery, two shop modes
  planner.py       JSON plan runner (incl. troupe ops)
  demo.py          demo driver + CLI (demo / run-plan / self-check)
tests/             optimizer, quote-service, ledger, reference, troupe tests
data/              real SQLite databases (created by the demo)
results/           JSON plans and result records
```

## Verification

`python3 -m basket_service demo` runs 38 checks, all computed against
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
* troupe cabinet: one stretch delivering two pairs, "cannot stretch further"
  and prior-shift occupancy both reducing to one pair, occupancy bookkeeping
  never narrowing a width, two-pair delivery with tied laces and un-repromised
  held shoes, single-pad width-2 service, borrow/return/acquire pad replay, the
  pre-stretch counter refusing pad transfers, 甲's width-3 sign-out-and-return,
  and a late after-sales return stopping new issuance — pair counts cross-checked
  against `max_pairs_bruteforce`;
* reopening the database preserves quotes, sales, returns, inventory **and** the
  cabinet's physical widths, tied laces, bindings and pads.
