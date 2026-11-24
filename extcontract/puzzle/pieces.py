"""Physical pieces reconstructed from the published loose-piece ledger.

The ledger itself is the ordinary ``reader-records/v1`` output of the project's
:class:`~extcontract.ingest.IngestionLibrary`; every value is a TEXT string.
This module turns those rows into validated :class:`Piece` objects and derives
the two facts the reasoning engine needs:

* ``editions`` - the plate editions the single physical piece could still be.
  A read-clear back code is authoritative; otherwise the photograph's
  ``photo_edition`` is all that is known (``"X/Y"`` keeps both open).
* ``orientations`` - the rotations the piece physically allows.

Re-photographing a piece reuses its ``piece_id`` and arrives as a higher
revision; by the time the packing desk calls ``publish()`` each piece id
already denotes exactly one current physical interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import ContractError
from .board import DIRECTIONS, EDGES, ORIENTATIONS, rotate_edges

AMBIGUOUS = "X/Y"
UNREAD = "UNREAD"


@dataclass(frozen=True)
class Piece:
    piece_id: str
    face_zone: str
    edges: dict[str, str]          # orientation-0 edge marks, keys n/e/s/w
    orientations: tuple[int, ...]
    photo_edition: str             # "X" | "Y" | "X/Y"
    back_code: str                 # "X" | "Y" | "UNREAD"
    photo_id: str
    note: str
    revision: int
    sources: dict = field(default_factory=dict)

    # -- derived facts -----------------------------------------------------

    @property
    def editions(self) -> tuple[str, ...]:
        """Editions this one physical piece could still be."""
        if self.back_code != UNREAD:
            return (self.back_code,)
        if self.photo_edition == AMBIGUOUS:
            return ("X", "Y")
        return (self.photo_edition,)

    @property
    def back_paper_on(self) -> bool:
        return self.back_code == UNREAD

    def edges_at(self, orientation: int) -> dict[str, str]:
        return rotate_edges(self.edges, orientation)

    def publication_basis(self) -> dict:
        """What ``publish()`` currently holds for this piece, with provenance."""
        return {
            "piece_id": self.piece_id,
            "revision": self.revision,
            "editions": list(self.editions),
            "photo_edition": self.photo_edition,
            "back_code": self.back_code,
            "photo_id": self.photo_id,
            "sources": self.sources,
        }


def _parse_orientations(raw: str, piece_id: str) -> tuple[int, ...]:
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    if not parts:
        raise ContractError(f"piece {piece_id!r}: orientations must list at least one value")
    values: list[int] = []
    for part in parts:
        try:
            value = int(part)
        except ValueError:
            raise ContractError(
                f"piece {piece_id!r}: orientation {part!r} is not an integer"
            ) from None
        if value not in ORIENTATIONS:
            raise ContractError(
                f"piece {piece_id!r}: orientation must be one of {list(ORIENTATIONS)}"
            )
        values.append(value)
    if len(set(values)) != len(values):
        raise ContractError(f"piece {piece_id!r}: duplicate orientations in {raw!r}")
    return tuple(sorted(values))


def piece_from_row(row: dict, *, editions: tuple[str, ...]) -> Piece:
    """Build a :class:`Piece` from one published ledger row.

    ``editions`` is the board's edition alphabet, used to validate the
    edition strings the workstation reported.
    """
    values = row.get("values")
    if not isinstance(values, dict):
        raise ContractError(f"row {row.get('row_id')!r}: missing values object")
    piece_id = row["row_id"]
    required = (
        "piece_id",
        "face_zone",
        "orientations",
        "photo_edition",
        "back_code",
        "photo_id",
    )
    missing = [k for k in required if k not in values]
    if missing:
        raise ContractError(f"piece {piece_id!r}: ledger row missing fields {missing}")
    if values["piece_id"] != piece_id:
        raise ContractError(
            f"piece {piece_id!r}: row id and piece_id field disagree "
            f"({values['piece_id']!r})"
        )

    edges = {}
    for direction in DIRECTIONS:
        key = f"edge_{direction}"
        mark = values.get(key)
        if mark not in EDGES:
            raise ContractError(
                f"piece {piece_id!r}: {key} must be one of {list(EDGES)}, got {mark!r}"
            )
        edges[direction] = mark

    orientations = _parse_orientations(values["orientations"], piece_id)
    photo_edition = values["photo_edition"]
    valid_edition_tokens = set(editions) | {AMBIGUOUS}
    if photo_edition not in valid_edition_tokens:
        raise ContractError(
            f"piece {piece_id!r}: photo_edition {photo_edition!r} not in "
            f"{sorted(valid_edition_tokens)}"
        )
    back_code = values["back_code"]
    if back_code not in (*editions, UNREAD):
        raise ContractError(
            f"piece {piece_id!r}: back_code must be one of "
            f"{[*editions, UNREAD]}, got {back_code!r}"
        )
    # A read-clear back code is the strongest fact; it must not contradict an
    # edition the photograph had already pinned down.
    if back_code != UNREAD and photo_edition != AMBIGUOUS and photo_edition != back_code:
        raise ContractError(
            f"piece {piece_id!r}: back code {back_code!r} contradicts photograph "
            f"edition {photo_edition!r}"
        )

    return Piece(
        piece_id=piece_id,
        face_zone=values["face_zone"],
        edges=edges,
        orientations=orientations,
        photo_edition=photo_edition,
        back_code=back_code,
        photo_id=values["photo_id"],
        note=values.get("note", ""),
        revision=row.get("revision", 1),
        sources=row.get("sources", {}),
    )


def pieces_from_publish(published: dict, *, editions: tuple[str, ...]) -> list[Piece]:
    """Parse every row of a ``publish()`` snapshot into pieces."""
    if published.get("format") != "reader-records/v1":
        raise ContractError("puzzle ledger expects a reader-records/v1 snapshot")
    rows = published.get("rows", [])
    if len(rows) > 12:
        raise ContractError("at most 12 physical pieces are supported per ledger")
    pieces = [piece_from_row(row, editions=editions) for row in rows]
    ids = [p.piece_id for p in pieces]
    if len(set(ids)) != len(ids):
        raise ContractError("ledger contains duplicate piece ids")
    return pieces
