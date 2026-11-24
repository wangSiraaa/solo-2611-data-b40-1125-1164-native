"""Read/write contracts for the embroidery needle-road registry.

The registry is deliberately a flat, all-nullable-TEXT table so that the
existing exact-type machinery (compatibility proof, adaptation, revision-aware
atomic ingestion) applies without any coercion: every domain value is an
abstract ``str``, and all embroidery-specific validation lives in
:mod:`extcontract.embroidery.records`, which reads the *published effective
rows* rather than raw writer input.

Four row kinds share the same columns:

================  =========================================================
``record_type``   ``THREAD`` / ``OBSERVATION`` / ``PROTECT`` / ``NOKNOT``
================  =========================================================

THREAD rows describe one physical thread, with the embroiderer's own id, the
physical thread colour, the ordered holes it pierces, the full-screen flow
sequence of every segment, and - when known - the face of its starting end.

OBSERVATION rows describe a side view: between two holes there is one segment
of a given colour.  Observations never name a thread; reconstruction assigns
each observation to exactly one matching segment, injectively.

PROTECT rows are collector circles of original stitches that must be kept.
NOKNOT rows are collector circles in which a fixing knot may not be exposed on
the given face.

Every row carries the embroidery-piece id (``piece_id``) so one contract can
serve several screens; rows of a single reconstruction always share one piece.
"""

from __future__ import annotations

from typing import Any

from ..contract import Contract, parse_contract

EMBROIDERY_WRITER_VERSION = "ew1"
EMBROIDERY_READER_VERSION = "er1"

#: The shared column set.  All columns are nullable TEXT because different row
#: kinds populate different subsets; semantic legality is checked after
#: publication, never by implicit parsing.
REGISTRY_FIELDS: tuple[str, ...] = (
    "piece_id",
    "record_type",
    "row_kind_id",
    "thread_id",
    "color",
    "holes_json",
    "start_face",
    "seq_json",
    "hole_a",
    "hole_b",
    "observed_face",
    "protected_face",
    "no_knot_face",
    "zone_holes_json",
    "tail_hole",
)

_CONTRACT_DOC = {
    "version": EMBROIDERY_READER_VERSION,
    "fields": [
        {
            "field_id": field_id,
            "name": field_id,
            "type": {"kind": "TEXT"},
            "nullable": True,
        }
        for field_id in REGISTRY_FIELDS
    ],
}


def embroidery_contract(*, role: str) -> Contract:
    """Parse the fixed embroidery writer (``ew1``) or reader (``er1``)."""
    doc = dict(_CONTRACT_DOC)
    if role == "writer":
        doc = {"version": EMBROIDERY_WRITER_VERSION, "fields": _CONTRACT_DOC["fields"]}
    elif role != "reader":
        raise ValueError("role must be 'writer' or 'reader'")
    return parse_contract(doc, role=role)


# ---------------------------------------------------------------------------
# row builders - one registry row per physical fact; revisions are expressed by
# re-ingesting a row with the same row_id and a higher revision.
# ---------------------------------------------------------------------------

def _base(row_id: str, piece_id: str, record_type: str, kind_id: str | None) -> dict:
    return {
        "row_id": row_id,
        "values": {
            "piece_id": piece_id,
            "record_type": record_type,
            "row_kind_id": kind_id if kind_id is not None else row_id,
        },
    }


def thread_row(
    row_id: str,
    piece_id: str,
    *,
    thread_id: str,
    color: str,
    holes: list[str],
    sequences: list[int],
    start_face: str | None = None,
    revision: int | None = None,
) -> dict:
    """Build a THREAD registry row.

    ``holes`` are the holes the thread pierces in embroiderer order; each
    segment between two consecutive holes has the full-screen flow sequence at
    the same index in ``sequences`` (so ``len(sequences) == len(holes) - 1``).
    ``start_face`` is ``"front"`` / ``"back"`` or ``None`` when the starting
    end's face is not recorded.
    """
    import json

    row = _base(row_id, piece_id, "THREAD", thread_id)
    values = row["values"]
    values.update(
        {
            "thread_id": thread_id,
            "color": color,
            "holes_json": json.dumps(list(holes), ensure_ascii=False),
            "seq_json": json.dumps([int(s) for s in sequences]),
            "start_face": start_face,
            "tail_hole": holes[-1] if holes else None,
        }
    )
    if revision is not None:
        row["revision"] = revision
    return row


def observation_row(
    row_id: str,
    piece_id: str,
    *,
    color: str,
    hole_a: str,
    hole_b: str,
    observed_face: str,
    revision: int | None = None,
) -> dict:
    """Build an OBSERVATION row: one coloured segment seen between two holes."""
    row = _base(row_id, piece_id, "OBSERVATION", None)
    row["values"].update(
        {
            "color": color,
            "hole_a": hole_a,
            "hole_b": hole_b,
            "observed_face": observed_face,
        }
    )
    if revision is not None:
        row["revision"] = revision
    return row


def protection_row(
    row_id: str,
    piece_id: str,
    *,
    thread_id: str,
    hole_a: str,
    hole_b: str,
    protected_face: str,
    revision: int | None = None,
) -> dict:
    """Build a PROTECT row: an original stitch the collector circled to keep."""
    row = _base(row_id, piece_id, "PROTECT", None)
    row["values"].update(
        {
            "thread_id": thread_id,
            "hole_a": hole_a,
            "hole_b": hole_b,
            "protected_face": protected_face,
        }
    )
    if revision is not None:
        row["revision"] = revision
    return row


def noknot_row(
    row_id: str,
    piece_id: str,
    *,
    no_knot_face: str,
    zone_holes: list[str],
    revision: int | None = None,
) -> dict:
    """Build a NOKNOT row: a face region on which no knot head may show."""
    import json

    row = _base(row_id, piece_id, "NOKNOT", None)
    row["values"].update(
        {
            "no_knot_face": no_knot_face,
            "zone_holes_json": json.dumps(list(zone_holes), ensure_ascii=False),
        }
    )
    if revision is not None:
        row["revision"] = revision
    return row


def registry_document(file_id: str, rows: list[dict]) -> dict[str, Any]:
    """Wrap registry rows in an embroidery ingestion file envelope."""
    return {
        "file_id": file_id,
        "writer_version": EMBROIDERY_WRITER_VERSION,
        "reader_version": EMBROIDERY_READER_VERSION,
        "rows": list(rows),
    }
