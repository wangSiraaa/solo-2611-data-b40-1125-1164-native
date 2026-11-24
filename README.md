# extcontract — exact finite-type compatibility, adaptation, and revision-aware ingestion

A self-contained Python 3.11 library for the 745 external files. It implements three
deliberately separated concerns:

1. **Contract compatibility** (`check_compatibility`) — a *row-free* proof that every
   legal value of each writer column is exactly representable in its paired reader
   column. It emits per-column safe/unsafe reasons, a concrete witness for each
   unsafe pair, and a public adaptation plan when the whole pair is safe.
2. **File adaptation** (`adapt_rows` / `adapt_file_document`) — validates every row
   against the *declared* writer domain, then applies the actual plan to emit
   `reader-records/v1` for the supplied `materials/745/reader_client.py`.
3. **Stateful ingestion** (`IngestionLibrary`) — owns the target row state under a
   fixed reader contract, resolves row revisions, and accepts or rejects each file
   **atomically**.

The supplied `reader_client.py` is never imported or modified. It is exercised
as a real subprocess against our output in the tests and demo.

---

## Quick start

```bash
# row-free compatibility report (per-column reasons + plan)
PYTHONPATH=. python3.11 -m extcontract compat \
    materials/745/writer-contract.json materials/745/reader-contract.json

# adapt the supplied rows and read them with the real client
PYTHONPATH=. python3.11 - <<'PY'
import json
from extcontract import load_contract, adapt_rows, write_records
w = load_contract("materials/745/writer-contract.json", role="writer")
r = load_contract("materials/745/reader-contract.json", role="reader")
rows = json.load(open("materials/745/input.json"))["rows"]
write_records(adapt_rows(w, r, rows).records, "/tmp/records.json")
PY
python3.11 materials/745/reader_client.py \
    materials/745/reader-contract.json /tmp/records.json
# -> {"numeric_totals": {"amount": "-1"}, "row_count": 2}
```

Full mandated scenario (F-BAD / F-NEW, narrowed readers, INT16, DECIMAL, nullable):

```bash
PYTHONPATH=. python3.11 examples/run_745_demo.py
```

Tests (pytest):

```bash
PYTHONPATH=. python3.11 -m pytest tests/ -q
```

---

## Type model (`extcontract/types.py`)

| Kind | JSON transport (writer input / reader output) | Domain |
|------|----------------------------------------------|--------|
| `INT(bits)` | JSON integer / minimal rational string | `[-2**(bits-1), 2**(bits-1)-1]`, `bits ∈ {8,16,32,64}` |
| `DECIMAL(p,s)` | JSON **fraction string** / minimal rational string | `c / 10**s`, `|c| ≤ 10**p-1`, `1≤p≤18`, `0≤s≤p` |
| `TEXT` | JSON string / JSON string | any string; never coerced to a number |

All types carry `nullable`. Numeric values are held internally as exact
`int`/`Fraction`; nothing is ever rounded, truncated, clamped, or defaulted from
`null`.

* Reader output uses **minimal rational strings** (`Fraction.__str__`), e.g. `"1"`,
  `"-2"`, `"1/10"`, which is what `reader_client.py` validates with
  `str(Fraction(value)) == value`.
* Writer `DECIMAL` input must be a fraction string (integers/floats/exponents are
  rejected — exact transport, no inference).
* Schema `default` values may be an integer or exact fraction string; they are
  normalised and range-checked against the reader type.

---

## 1. Compatibility service (`extcontract/compat.py`)

Pure function of the two contracts — **no rows, no target `row_id`**:

```python
report = check_compatibility(writer, reader)
report.safe               # bool over the whole declared domains
report.columns[i].safe    # per reader column
report.columns[i].reasons # human-readable proof / counterexample
report.columns[i].witness # a legal writer value the reader cannot hold (unsafe)
report.adaptation_plan()  # executable plan when report.safe
```

### Safety rules (exact, no samples)

| Writer → Reader | Safe iff |
|-----------------|----------|
| nullable → non-nullable | **never** (null is a legal writer value, witness = null) |
| `INT(bw) → INT(br)` | `br ≥ bw` |
| `INT(bw) → DECIMAL(p,s)` | `2**(bw-1) * 10**s ≤ 10**p - 1` |
| `DECIMAL(pw,sw) → INT(br)` | `sw == 0` and `10**pw - 1 ≤ 2**(br-1) - 1` |
| `DECIMAL(pw,sw) → DECIMAL(pr,sr)` | `sr ≥ sw` and `(10**pw-1)·10**(sr-sw) ≤ 10**pr-1` |
| `TEXT → TEXT` | always |
| anything else | **never** (no implicit coercion; TEXT is never parsed as numeric) |

Examples the brief calls out, all covered by tests:

* `INT8 → DECIMAL(3,0)` **SAFE**; `INT8 → DECIMAL(2,0)` **UNSAFE** even though the
  samples `1, -2` fit — the witness is a legal INT8 value (e.g. `-128`) outside
  `[-99, 99]`.
* `INT16 → DECIMAL(3,0)` **UNSAFE** for the same reason.
* `DECIMAL(3,1) → DECIMAL(4,2)` **SAFE** (`1/10` converts exactly); lowering scale
  is **UNSAFE** (witness `1/10` at scale 0).
* nullable writer → non-nullable reader **UNSAFE**.

### Missing / extra columns

* A reader column is safe when the writer supplies it and the whole-domain proof
  holds, **or** when the writer omits it **and** the reader declares an explicit,
  in-domain `default`.
* A reader column absent from the writer with **no** default is **UNSAFE** —
  nullable does not imply a supplied value.
* Extra writer columns are listed under `ignored_writer_fields` (ignored).
* Pairing is **by `field_id` only**; a changed display `name` is noted but harmless.
* Duplicate `field_id` in a contract is rejected at parse time.
* A `default` never rescues a present-but-null writer value: if the writer declares
  the column, the row-level `null` passes through (legal only if both nullable).
  This is enforced both in the proof and at row adaptation.

### Contract revisions

`replace_field_type(contract, field_id=..., new_type_json=...)` models the
`reader_revision` / `writer_revision` operations in `input.json`; re-running
`check_compatibility` yields `INCOMPATIBLE` for both supplied revisions.

---

## 2. File adaptation (`extcontract/adapter.py`)

```python
result = adapt_rows(writer, reader, rows)          # -> AdaptationResult
result.status    # "ADAPTED" | "INCOMPATIBLE" | "INVALID"
result.records   # reader-records/v1 (only when ADAPTED)
result.errors    # per-row/field messages (never silently drops a row)
```

Pipeline, in order:

1. Check contract compatibility. If unsafe → `INCOMPATIBLE`, no records.
2. Parse each row envelope (`row_id`, positive-integer `revision` defaulting to `1`,
   `values` object).
3. Validate every value against the **declared writer domain** (range, scale,
   nullability, JSON encoding, unknown fields). All row errors are collected and the
   **whole file** is rejected; no partial output.
4. Apply the actual plan column-by-column:
   * `writer` column → exact conversion (`int_identity`, `int_widen`,
     `int_to_decimal_exact`, `decimal_identity`, `decimal_exact_rescale`,
     `decimal_to_int_exact`, `text_identity`) or `null_passthrough`;
   * missing column with default → `fill_default`;
   * guards re-assert reader-domain membership before encoding.
5. Emit `reader-records/v1`:

```json
{
  "format": "reader-records/v1",
  "reader_version": "r1",
  "rows": [
    {"row_id": "one", "revision": 1,
     "values": {"amount": "1", "status": "ok"},
     "sources": {
       "amount": {"field_id": "amount", "writer_field_id": "amount",
                  "writer_type": {"kind":"INT","bits":8},
                  "reader_type": {"kind":"DECIMAL","precision":3,"scale":0},
                  "conversion": "int_to_decimal_exact", "input": 1},
       "status": {"default": "ok", "conversion": "fill_default",
                  "reader_type": {"kind":"TEXT"}}}}
  ]
}
```

`sources` preserves the original field id, the conversion basis, the original
(writer) input value, and the default basis, satisfying the provenance requirement.

`adapt_file_document` additionally pins `writer_version`/`reader_version` declared by
the file to the fixed contracts.

---

## 3. Ingestion library (`extcontract/ingest.py`)

```python
library = IngestionLibrary(reader)                 # fixed reader contract
res = library.ingest(writer, file_document)        # atomic accept/reject
library.publish()                                  # reader-records/v1 snapshot
library.numeric_totals()                           # exact per-column totals
```

### Separation of concerns

`check_compatibility` (row-free) decides whether the *contract pair* is safe. Its
`SAFE` result is a **precondition**, never acceptance. The library then independently
validates rows, resolves revisions, and commits atomically. It never hard-codes a
target `row_id`.

### State model & canonical equality

Each row stores its **canonical reader value** (`int`/`Fraction`/`str`/`None`) plus
the adapted `encoded` values and `sources` provenance. Business identity is the
canonical value, so:

* different input representations of the same number (`"1"` vs `"2/2"` for
  `DECIMAL(_,0)`) do **not** create a conflict;
* changing a display `name` while keeping `field_id` changes nothing;
* `null` and the string `"null"` are distinct; `TEXT` values are never parsed to
  numbers.

### Revision rules (per file, per `row_id`)

* Missing `revision` ⇒ `1`; revisions must be positive integers.
* The **highest revision in the file wins**.
* **Every** occurrence is first validated for writer-domain legality — an illegal
  low-revision row rejects the file (it is not silently ignored).
* Lower revision than the stored one ⇒ validated, then **ignored**.
* Same revision + same canonical value ⇒ **replay** (no change).
* Same revision + different canonical value ⇒ **whole file rejected**, even if a
  higher revision for the same row also appears in the file.

### Atomicity

Validation, adaptation, revision resolution, and conflict detection happen entirely
on staged copies. State and receipt are published in one guarded assignment. A
rejected file leaves **no new rows and no receipt**.

### File identity / idempotency

Receipts are keyed by `(reader_version, file_id)` and store a canonical,
representation-independent fingerprint over the `(row_id, revision, values)` content
and contract versions:

* identical resubmission ⇒ original receipt, `"replay": true`;
* same `file_id`, different content ⇒ rejected.

### Reader-version isolation

Each `IngestionLibrary` is fixed to one reader contract; its rows live in an
isolated map keyed by `reader_version`. Distinct reader versions never rescan each
other's rows; cross-version ingress into a library is refused.

### Publication

`publish()` returns `reader-records/v1` from the **real target row slice**, retaining
each row's `revision`, adapted `values`, and `sources` provenance. It is directly
consumable by `reader_client.py`; totals recompute from the canonical values.

### Costs (the documented envelope: ≤20 columns, ≤5000 rows, ≤50 versions)

* Ingest: `O(m·k)` for validation/adaptation plus `O(m·k log m)` for sorting/
  fingerprinting (`m` rows, `k` columns); conflict checks are dict lookups.
* Publish: `O(n·k)` copy + `O(n log n)` sort. No versioned-view recomputation is
  needed because every accepted row already holds its adapted values and provenance.
* 50 reader versions are independent maps; ingesting under each does not touch the
  others.
* Measured in `tests/test_scale.py` (20 cols × 5000 rows, 50 versions): plan ≪ 1 ms,
  ingest ≈ 0.6 s, publish ≈ 0.17 s, all 50 versions ≈ 8 ms.

No arbitrary type inference, no Arrow Union, no online database migration.

---

## 4. Loose-piece box assembly (`extcontract.puzzle`)

A second business on the same contract machinery: a small puzzle factory that
receives loose pieces from different plate editions and must decide whether a
station can hand over a complete box. Nothing here bypasses the three layers
above — the workstation's photo interpretation records are ordinary writer
rows ingested into an ordinary `IngestionLibrary`, and the packing desk only
ever reads `publish()`.

### Contracts and the piece-number rule

`extcontract/puzzle/contracts.py` (with mirrored JSON in
`materials/puzzle/`) declares the workstation writer contract `pw1` and the
loose-piece ledger reader contract `pr1`, both over `TEXT` fields:

`piece_id`, `face_zone`, `edge_n/s/e/w` (`T` tab / `B` blank / `F` flat),
`orientations`, `photo_edition` (`X`, `Y`, or `X/Y` when the photograph can
only establish "one of the two plates"), `back_code` (`X`/`Y`/`UNREAD`), plus
reader-only `photo_id` / `note` with in-domain defaults. Re-photographing the
*same physical piece* reuses its `piece_id` and arrives as a higher revision,
so a back-code correction keeps the original piece number and the revision
history / `sources` provenance stays attached.

### Reasoning: exclusions and surviving whole-plate interpretations

`reason(board, pieces, allow_rotation=...)` keeps, per physical piece, the
cells it can occupy in each edition it could still be. Each candidate passes
four exclusion rules: **face zone**, **rotation policy**, **flat-edges-on-
borders**, and **adjacent tab/blank mating** (optionally seeded with
already-recognised neighbour evidence via `known_neighbors`). An
*interpretation* is one board edition plus a complete bijection tiling; a box
is only promised when the buyer's named edition itself survives, and the
no-peel cover below works for every still-possible physical outcome.

### The one implemented delivery strategy: no peel, minimal spares

The factory does **not** spend inspection quota reading back codes. Instead
`minimum_cover` enumerates the joint outcomes of every back-paper-on piece and
takes the union of the smallest ordered-edition tiling across outcomes: pieces
that only rescue an alternative outcome ride in the same box as spares, with
per-cell edge-mark selection notes, and every piece (spares included) counts
against `weight_limit_pieces`. If no cover exists, or it exceeds the weight
limit, `take_order` returns `SHORT` with an executable reason.

### Order entry, contention and reconciliation

```python
from extcontract import IngestionLibrary
from extcontract.puzzle import (
    PackingDesk, OrderRequest, IngestionLibrary,
    photo_writer_contract, photo_reader_contract, photo_file, photo_row,
)

library = IngestionLibrary(photo_reader_contract())
library.ingest(photo_writer_contract(), photo_file("batch", [ ... rows ... ]))
desk = PackingDesk(library)
result = desk.take_order(OrderRequest.parse({
    "order_id": "O1", "board": {...}, "edition": "X",
    "allow_rotation": False, "weight_limit_pieces": 2,
}))
result.status            # "PROMISED" | "SHORT"
desk.seal_box(result.box.box_no)
desk.box(box_no).piece_basis   # per packed piece: revision + full sources
desk.physical_ledger()         # sealed/reserved/available piece account
```

One physical piece cannot appear in two boxes: promised (and sealed) boxes
reserve their pieces, so a competing order that needs the same piece numbers is
decided against the remaining stock and comes back `SHORT`. Bounded to six
cells and twelve physical pieces. The mandated two-cell scenarios and a
rotation-required 2×2 board are covered by `tests/test_puzzle_*.py`; run the
story with `python3.11 examples/run_puzzle_demo.py`.

---

## Templates for the mandated scenario

`fixtures/bootstrap.json`, `fixtures/F-BAD.json`, `fixtures/F-NEW.json` are the
exact documents from the brief:

1. Bootstrap `one=1@r1`, `two=-2@r1` → client amount total **-1**, `status="ok"`.
2. `F-BAD` (`one=2@r1` conflicts with accepted `1`; `three=1@r1`) → **whole file
   rejected**; `three` remains invisible; total stays **-1**.
3. `F-NEW` (`one=3@r2`, `two=4@r2`) → **accepted atomically**; published slice totals
   **7** and the real client reads it; the pre-`F-NEW` slice still totals **-1**.

---

## Package layout

```
extcontract/
  errors.py     exceptions (business rejections are results, not exceptions)
  types.py      INT/DECIMAL/TEXT domain model, exact encode/decode
  contract.py   writer/reader contract parsing, defaults, duplicate checks
  compat.py     row-free whole-domain proof + adaptation plan + witnesses
  adapter.py    per-row validation + plan application -> reader-records/v1
  ingest.py     revision-aware, atomic, idempotent target state
  cli.py        compat / adapt / ingest commands
  puzzle/       loose-piece box-assembly service (contracts, board, pieces,
                reasoning, packing desk, workstation ingest helpers)
examples/run_745_demo.py     end-to-end scenario against the real client
examples/run_puzzle_demo.py  loose-piece order, contention, correction story
fixtures/                    bootstrap, F-BAD, F-NEW documents
materials/745/               original 745 contracts, input and reader client
materials/puzzle/            pw1/pr1 photo-interpretation contracts
tests/                       compat, adapter, ingest, fixtures, scale, puzzle
```
