"""Loose-piece box-assembly service for a small puzzle factory.

This subpackage extends the exact contract toolkit with the factory's
workstation-to-packing flow while reusing every existing mechanism unchanged:

* :mod:`extcontract.puzzle.contracts` - photo-interpretation writer/reader
  contracts built from the project's :class:`~extcontract.types.TextType`
  fields;
* :mod:`extcontract.puzzle.workstation` - ingest-file builders (the same
  physical piece keeps its piece id across re-photographs and revisions);
* :mod:`extcontract.puzzle.board` / :mod:`extcontract.puzzle.pieces` - the
  board geometry and the physical pieces reconstructed from ``publish()``;
* :mod:`extcontract.puzzle.reasoning` - edge/face/orientation exclusions,
  surviving whole-plate interpretations and the no-peel spare-piece cover;
* :mod:`extcontract.puzzle.packing` - the order intake entry point and the
  sealed-box registry with publication-basis provenance for reconciliation.

The implemented delivery strategy is the *no-peel* one: the back paper is not
removed, and the smallest set of spare pieces that covers every still-possible
edition is packed into the same box together with edge-mark selection notes;
the spares count against the box weight limit.
"""

from __future__ import annotations

from .board import (
    BoardSpec,
    Cell,
    EDGES,
    MAX_CELLS,
    MAX_PIECES,
    ORIENTATIONS,
    edges_mate,
    parse_board,
    rotate_edges,
)
from .contracts import (
    PHOTO_FIELDS,
    READER_VERSION,
    WRITER_VERSION,
    photo_reader_contract,
    photo_writer_contract,
    reader_contract_json,
    writer_contract_json,
)
from .packing import BoxRecord, OrderRequest, OrderResult, PackingDesk
from .pieces import AMBIGUOUS, UNREAD, Piece, piece_from_row, pieces_from_publish
from .reasoning import (
    Interpretation,
    Placement,
    ReasoningReport,
    candidate_placements,
    minimum_cover,
    reason,
)
from .workstation import back_code_correction, photo_file, photo_row

__all__ = [
    # board
    "BoardSpec",
    "Cell",
    "EDGES",
    "MAX_CELLS",
    "MAX_PIECES",
    "ORIENTATIONS",
    "edges_mate",
    "parse_board",
    "rotate_edges",
    # contracts
    "PHOTO_FIELDS",
    "READER_VERSION",
    "WRITER_VERSION",
    "photo_reader_contract",
    "photo_writer_contract",
    "reader_contract_json",
    "writer_contract_json",
    # pieces
    "AMBIGUOUS",
    "UNREAD",
    "Piece",
    "piece_from_row",
    "pieces_from_publish",
    # reasoning
    "Interpretation",
    "Placement",
    "ReasoningReport",
    "candidate_placements",
    "minimum_cover",
    "reason",
    # packing
    "BoxRecord",
    "OrderRequest",
    "OrderResult",
    "PackingDesk",
    # workstation
    "back_code_correction",
    "photo_file",
    "photo_row",
]
