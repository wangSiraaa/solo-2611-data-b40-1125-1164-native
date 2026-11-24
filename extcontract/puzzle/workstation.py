"""Workstation helpers: build photo interpretation files for the ingest path.

The desk never writes ledger rows directly.  Every piece fact enters through
the project's unchanged :class:`~extcontract.ingest.IngestionLibrary` as an
ingest file under the workstation writer contract; the packing desk only ever
reads ``publish()``.  Re-photographing the *same* physical piece reuses its
``piece_id`` and bumps the revision, which is exactly what makes the original
piece number persist across corrections (e.g. a back code read clear after
sealing-paper removal).
"""

from __future__ import annotations

from ..errors import ContractError
from .board import DIRECTIONS, EDGES
from .contracts import WRITER_VERSION, READER_VERSION
from .pieces import AMBIGUOUS, UNREAD


def photo_row(
    piece_id: str,
    *,
    face_zone: str,
    edges: dict[str, str],
    orientations: list[int] | tuple[int, ...],
    photo_edition: str,
    back_code: str = UNREAD,
    revision: int = 1,
) -> dict:
    """Build one writer row (a single piece's photograph interpretation)."""
    missing = [d for d in DIRECTIONS if d not in edges]
    if missing:
        raise ContractError(f"piece {piece_id!r}: edges missing directions {missing}")
    bad = [d for d in DIRECTIONS if edges[d] not in EDGES]
    if bad:
        raise ContractError(
            f"piece {piece_id!r}: edge marks must be one of {list(EDGES)}; "
            f"bad directions {bad}"
        )
    if photo_edition not in ("X", "Y", AMBIGUOUS):
        raise ContractError(
            f"piece {piece_id!r}: photo_edition must be X, Y or {AMBIGUOUS}"
        )
    if back_code not in ("X", "Y", UNREAD):
        raise ContractError(f"piece {piece_id!r}: back_code must be X, Y or UNREAD")
    if type(revision) is not int or revision < 1:
        raise ContractError(f"piece {piece_id!r}: revision must be a positive integer")
    if not orientations:
        raise ContractError(f"piece {piece_id!r}: at least one orientation is required")
    return {
        "row_id": piece_id,
        "revision": revision,
        "values": {
            "piece_id": piece_id,
            "face_zone": face_zone,
            "edge_n": edges["n"],
            "edge_e": edges["e"],
            "edge_s": edges["s"],
            "edge_w": edges["w"],
            "orientations": "|".join(str(o) for o in sorted(orientations)),
            "photo_edition": photo_edition,
            "back_code": back_code,
        },
    }


def photo_file(file_id: str, rows: list[dict]) -> dict:
    """Wrap workstation rows in the ingest envelope pinned to the pw1/pr1 pair."""
    if not isinstance(rows, list) or not rows:
        raise ContractError("photo file must contain at least one row")
    return {
        "file_id": file_id,
        "writer_version": WRITER_VERSION,
        "reader_version": READER_VERSION,
        "rows": rows,
    }


def back_code_correction(
    piece_id: str,
    *,
    revision: int,
    back_code: str,
    previous: dict,
) -> dict:
    """Build the higher-revision row that reads a back code clear.

    ``previous`` is the piece's current writer row; every photograph fact is
    carried forward unchanged and only the back code (and revision) changes, so
    the physical piece keeps the same piece number through the correction.
    """
    if back_code not in ("X", "Y"):
        raise ContractError("a back-code correction must settle X or Y")
    values = dict(previous["values"])
    values["back_code"] = back_code
    row = {"row_id": piece_id, "revision": revision, "values": values}
    if row["row_id"] != piece_id:  # pragma: no cover - defensive
        raise ContractError("back-code correction must keep the original piece id")
    return row
