"""Writer/reader contracts for the small puzzle factory's loose-piece service.

The factory's workstation reads a photograph of each loose physical piece and
files one *photo interpretation record* per piece.  A later photograph (or a
back-code read under the sealing paper) revises the same record.  These two
:class:`~extcontract.contract.Contract` documents give that record its fields
through the project's existing contract mechanism, so the records then flow
through the unchanged :class:`~extcontract.ingest.IngestionLibrary` (revision
rules, atomicity, idempotent receipts) and are read back through
``publish()``.

Writer side (the workstation app, version ``pw1``) reports what the
photograph establishes; the workstation never invents a back code, so
``back_code`` defaults to ``UNREAD`` at the ledger rather than being typed at
the bench.  Reader side (version ``pr1``) is the loose-piece ledger the
packing desk reasons over.  Reader-only columns declare an in-domain TEXT
default, which keeps the contract pair SAFE when a writer revision omits a
column that the ledger knows how to fill.

Field meaning
-------------
``piece_id``        stable physical-piece number; re-photographing the *same*
                    piece reuses this row id (highest revision then wins).
``face_zone``       pattern zone printed on the face ("A".."F").
``edge_n/s/e/w``    tab/blank/flat edge mark as seen on the photo, or "U" when
                    the photograph cannot read that edge.
``orientations``    orientations the piece physically allows, as an unordered
                    set string, e.g. ``"0"`` or ``"0|90|180|270"``.
``photo_edition``   what the photograph establishes about the plate edition:
                    ``"X"`` / ``"Y"`` when the print settles it, or ``"X/Y"``
                    when only "one of the two plates" is knowable.
``back_code``       back paper read: ``"X"`` / ``"Y"`` once the code is read
                    clear, or ``"UNREAD"`` while the sealing paper stays on.
``photo_id``        identifier of the photograph the row was read from.
``note``            free-text workstation note.
"""

from __future__ import annotations

from ..contract import Contract, Field
from ..types import TextType

WRITER_VERSION = "pw1"
READER_VERSION = "pr1"

# Ordered field ids; shared by both contracts.  The reader mirrors every
# writer field_id, which is the only identity the compatibility layer pairs on.
PHOTO_FIELDS: tuple[tuple[str, str], ...] = (
    ("piece_id", "piece number"),
    ("face_zone", "face pattern zone"),
    ("edge_n", "north edge mark"),
    ("edge_s", "south edge mark"),
    ("edge_e", "east edge mark"),
    ("edge_w", "west edge mark"),
    ("orientations", "allowed orientations"),
    ("photo_edition", "edition established by the photograph"),
    ("back_code", "back paper code"),
    ("photo_id", "source photograph id"),
    ("note", "workstation note"),
)

# Columns the workstation writer does not type.  Each is reader-side only and
# therefore needs an in-domain default for the pair to be SAFE.  A default
# fills only an entirely omitted column, never a row-level value.
_READER_ONLY: dict[str, str] = {
    "photo_id": "unknown-photo",
    "note": "",
}

# The writer carries back_code itself (UNREAD while the paper is on, X/Y once
# read clear); it is not a reader-only fill.
_WRITER_FIELDS = tuple(
    (fid, name) for fid, name in PHOTO_FIELDS if fid not in _READER_ONLY
)


def _text_type() -> TextType:
    return TextType(nullable=False)


def photo_writer_contract() -> Contract:
    """Contract under which the workstation files photo interpretation rows."""
    return Contract(
        version=WRITER_VERSION,
        fields=tuple(
            Field(field_id=fid, name=name, type=_text_type())
            for fid, name in _WRITER_FIELDS
        ),
        role="writer",
    )


def photo_reader_contract() -> Contract:
    """Contract of the loose-piece ledger consumed by the packing desk."""
    fields: list[Field] = []
    for fid, name in PHOTO_FIELDS:
        if fid in _READER_ONLY:
            fields.append(
                Field(
                    field_id=fid,
                    name=name,
                    type=_text_type(),
                    default=_READER_ONLY[fid],
                    has_default=True,
                )
            )
        else:
            fields.append(Field(field_id=fid, name=name, type=_text_type()))
    return Contract(version=READER_VERSION, fields=tuple(fields), role="reader")


# ---------------------------------------------------------------------------
# JSON documents (kept under materials/puzzle for parity with materials/745)
# ---------------------------------------------------------------------------

def _text_field_json(field_id: str, name: str) -> dict:
    return {
        "field_id": field_id,
        "name": name,
        "type": {"kind": "TEXT"},
        "nullable": False,
    }


def writer_contract_json() -> dict:
    return {
        "version": WRITER_VERSION,
        "fields": [_text_field_json(fid, name) for fid, name in _WRITER_FIELDS],
    }


def reader_contract_json() -> dict:
    return {
        "version": READER_VERSION,
        "fields": [
            {
                **_text_field_json(fid, name),
                **({"default": _READER_ONLY[fid]} if fid in _READER_ONLY else {}),
            }
            for fid, name in PHOTO_FIELDS
        ],
    }
