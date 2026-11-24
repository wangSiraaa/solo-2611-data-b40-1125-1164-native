"""Order intake and the sealed-box registry for the loose-piece desk.

This is the station's single order entry point.  A call supplies the board
(样图), the order's accepted edition, the rotation policy and the box weight
limit (counted in pieces).  The desk:

1. reads the current loose-piece ledger through the project's
   ``IngestionLibrary.publish()`` - never a private copy;
2. runs the exclusion / surviving-interpretation analysis in
   :mod:`extcontract.puzzle.reasoning`;
3. under the one implemented strategy (sealing paper left on, a minimal set of
   spares covering every still-possible edition goes into the same box),
   decides whether a box can be promised;
4. enforces the hard physical rule that one piece cannot appear in two boxes:
   pieces already sealed (or reserved by a promised box) are removed from
   available stock before reasoning.

A sealed box records the exact pieces physically in it and, per piece, the
publication basis (revision, photograph edition, back-code status, full ingest
``sources`` provenance), so the buyer's goods can be reconciled against the
post-seal physical ledger by box number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import ContractError
from ..ingest import IngestionLibrary
from .board import MAX_PIECES, BoardSpec, parse_board
from .pieces import Piece, pieces_from_publish
from .reasoning import ReasoningReport, candidate_placements, minimum_cover, reason


@dataclass
class OrderRequest:
    order_id: str
    board: BoardSpec
    edition: str                       # the edition the buyer accepts
    allow_rotation: bool
    weight_limit_pieces: int           # box capacity counted in pieces

    @classmethod
    def parse(cls, raw: Any) -> "OrderRequest":
        if not isinstance(raw, dict):
            raise ContractError("order request must be an object")
        extra = set(raw) - {
            "order_id",
            "board",
            "edition",
            "allow_rotation",
            "weight_limit_pieces",
        }
        if extra:
            raise ContractError(f"order request: unexpected keys {sorted(extra)}")
        order_id = raw.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            raise ContractError("order request: order_id must be a non-empty string")
        board = parse_board(raw.get("board"))
        edition = raw.get("edition")
        if edition not in board.editions:
            raise ContractError(
                f"order {order_id!r}: edition {edition!r} is not one of "
                f"{list(board.editions)} declared by the board"
            )
        allow_rotation = raw.get("allow_rotation")
        if not isinstance(allow_rotation, bool):
            raise ContractError("order request: allow_rotation must be a bool")
        limit = raw.get("weight_limit_pieces")
        if type(limit) is not int or limit < board.cell_count:
            raise ContractError(
                f"order {order_id!r}: weight_limit_pieces must be an integer >= "
                f"the {board.cell_count} cells of the board"
            )
        if limit > MAX_PIECES:
            raise ContractError(
                f"order {order_id!r}: no box may carry more than {MAX_PIECES} pieces"
            )
        return cls(
            order_id=order_id,
            board=board,
            edition=edition,
            allow_rotation=allow_rotation,
            weight_limit_pieces=limit,
        )


@dataclass
class BoxRecord:
    box_no: str
    order_id: str
    board_id: str
    edition: str
    sealed: bool
    packed_pieces: tuple[str, ...]
    spare_pieces: tuple[str, ...]
    weight_pieces: int
    weight_limit_pieces: int
    instructions: list[dict]
    interpretations: list[dict]
    # piece_id -> publication basis captured at packing time
    piece_basis: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "box_no": self.box_no,
            "order_id": self.order_id,
            "board_id": self.board_id,
            "edition": self.edition,
            "sealed": self.sealed,
            "packed_pieces": list(self.packed_pieces),
            "spare_pieces": list(self.spare_pieces),
            "weight_pieces": self.weight_pieces,
            "weight_limit_pieces": self.weight_limit_pieces,
            "instructions": self.instructions,
            "interpretations": self.interpretations,
            "piece_basis": self.piece_basis,
        }


@dataclass
class OrderResult:
    status: str                        # "PROMISED" | "SHORT"
    order_id: str
    reasons: list[str] = field(default_factory=list)
    report: ReasoningReport | None = None
    box: BoxRecord | None = None

    @property
    def promised(self) -> bool:
        return self.status == "PROMISED"

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "order_id": self.order_id,
            "reasons": list(self.reasons),
            "reasoning": self.report.to_json() if self.report else None,
            "box": self.box.to_json() if self.box else None,
        }


class PackingDesk:
    """Owns the box registry on top of a published loose-piece ledger."""

    def __init__(
        self,
        library: IngestionLibrary,
        *,
        box_prefix: str = "BOX",
        edition_alphabet: tuple[str, ...] = ("X", "Y"),
    ):
        if library.reader.role != "reader":
            raise ContractError("PackingDesk requires a reader-fixed ingestion library")
        self.library = library
        self.box_prefix = box_prefix
        self.edition_alphabet = tuple(edition_alphabet)
        self._boxes: dict[str, BoxRecord] = {}
        self._order_boxes: dict[str, str] = {}
        self._box_seq = 0

    # -- ledger / stock views ----------------------------------------------

    def current_pieces(self, board: BoardSpec) -> list[Piece]:
        """Reconstruct every piece from a fresh publish() of the real ledger."""
        return pieces_from_publish(
            self.library.publish(), editions=board.editions
        )

    def _all_pieces(self) -> list[Piece]:
        return pieces_from_publish(
            self.library.publish(), editions=self.edition_alphabet
        )

    def reserved_piece_ids(self) -> set[str]:
        """Pieces physically in a promised box (sealed or awaiting sealing)."""
        return {
            pid
            for box in self._boxes.values()
            for pid in box.packed_pieces
        }

    def sealed_piece_ids(self) -> set[str]:
        return {
            pid
            for box in self._boxes.values()
            if box.sealed
            for pid in box.packed_pieces
        }

    def available_pieces(self, board: BoardSpec) -> list[Piece]:
        reserved = self.reserved_piece_ids()
        return [p for p in self.current_pieces(board) if p.piece_id not in reserved]

    # -- order entry --------------------------------------------------------

    def take_order(self, request: OrderRequest) -> OrderResult:
        if request.order_id in self._order_boxes:
            raise ContractError(
                f"order {request.order_id!r} already has box "
                f"{self._order_boxes[request.order_id]!r}"
            )
        board = request.board
        pieces = self.available_pieces(board)
        if len(pieces) > MAX_PIECES:
            return OrderResult(
                status="SHORT",
                order_id=request.order_id,
                reasons=[
                    f"ledger holds {len(pieces)} available pieces; limit is {MAX_PIECES}"
                ],
            )

        report = reason(board, pieces, allow_rotation=request.allow_rotation)
        if not report.feasible:
            return OrderResult(
                status="SHORT",
                order_id=request.order_id,
                reasons=self._shortage_reasons(board, pieces, report),
                report=report,
            )

        # The order names one accepted edition.  A tiling that only exists
        # under another edition does not rescue it: the buyer's edition must
        # itself be among the surviving whole-plate interpretations.
        surviving_editions = {i.edition for i in report.interpretations}
        if request.edition not in surviving_editions:
            return OrderResult(
                status="SHORT",
                order_id=request.order_id,
                reasons=[
                    f"order accepts only edition {request.edition!r}, but no complete "
                    f"tiling survives under it; surviving editions: "
                    f"{sorted(surviving_editions) or ['<none>']}"
                ],
                report=report,
            )

        available_ids = {p.piece_id for p in pieces}
        cover = minimum_cover(
            board,
            pieces,
            report,
            available_piece_ids=available_ids,
            required_edition=request.edition,
        )
        if cover is None:
            return OrderResult(
                status="SHORT",
                order_id=request.order_id,
                reasons=[
                    "the ordered edition cannot be covered by available stock under "
                    "every still-possible piece edition outcome"
                ],
                report=report,
            )
        packed, instructions = cover
        if len(packed) > request.weight_limit_pieces:
            return OrderResult(
                status="SHORT",
                order_id=request.order_id,
                reasons=[
                    f"cover needs {len(packed)} pieces ({sorted(packed)}) but the box "
                    f"weight limit is {request.weight_limit_pieces} pieces (spares "
                    f"included)"
                ],
                report=report,
            )

        piece_by_id = {p.piece_id: p for p in pieces}
        core = self._core_for_edition(
            board, pieces, report, packed, request.edition
        )
        spares = tuple(sorted(set(packed) - set(core)))
        self._box_seq += 1
        box_no = f"{self.box_prefix}-{self._box_seq:04d}"
        box = BoxRecord(
            box_no=box_no,
            order_id=request.order_id,
            board_id=board.board_id,
            edition=request.edition,
            sealed=False,
            packed_pieces=tuple(sorted(packed)),
            spare_pieces=spares,
            weight_pieces=len(packed),
            weight_limit_pieces=request.weight_limit_pieces,
            instructions=instructions,
            interpretations=[i.to_json() for i in report.interpretations],
            piece_basis={
                pid: piece_by_id[pid].publication_basis()
                for pid in sorted(packed)
            },
        )
        # An unsealed box still reserves its pieces: a competing order cannot
        # promise the same physical piece while the first box is on the bench.
        self._boxes[box_no] = box
        self._order_boxes[request.order_id] = box_no
        return OrderResult(
            status="PROMISED", order_id=request.order_id, report=report, box=box
        )

    def seal_box(self, box_no: str) -> BoxRecord:
        """Mark a promised box sealed.  The captured publication basis is the
        packing-time snapshot and is deliberately not rewritten: the physical
        ledger reconciliation must compare against what was actually boxed."""
        box = self._require_box(box_no)
        sealed = BoxRecord(**{**box.__dict__, "sealed": True})
        self._boxes[box_no] = sealed
        return sealed

    # -- reconciliation -----------------------------------------------------

    def box(self, box_no: str) -> BoxRecord:
        return self._require_box(box_no)

    def box_for_order(self, order_id: str) -> BoxRecord | None:
        box_no = self._order_boxes.get(order_id)
        return self._boxes.get(box_no) if box_no else None

    def physical_ledger(self) -> dict:
        """The post-allocation physical account: sealed vs still-available."""
        reserved = self.reserved_piece_ids()
        all_pieces = self._all_pieces()
        return {
            "total_pieces": len(all_pieces),
            "reserved_pieces": sorted(reserved),
            "sealed_pieces": sorted(self.sealed_piece_ids()),
            "available_pieces": sorted(
                p.piece_id for p in all_pieces if p.piece_id not in reserved
            ),
            "boxes": [
                b.to_json() for b in sorted(self._boxes.values(), key=lambda b: b.box_no)
            ],
        }

    # -- internals ----------------------------------------------------------

    def _require_box(self, box_no: str) -> BoxRecord:
        box = self._boxes.get(box_no)
        if box is None:
            raise ContractError(f"unknown box number {box_no!r}")
        return box

    def _core_for_edition(
        self,
        board: BoardSpec,
        pieces: list[Piece],
        report: ReasoningReport,
        packed: frozenset[str],
        edition: str,
    ) -> list[str]:
        """The pieces of one minimum tiling under the buyer's accepted edition.

        Everything else in the cover rides along as a spare for an alternative
        edition outcome.
        """
        candidates, _ = candidate_placements(
            board, pieces, allow_rotation=report.allow_rotation
        )
        in_edition = [p for p in pieces if edition in p.editions]
        assignment = {p.piece_id: edition for p in in_edition}
        from .reasoning import _all_tiling_covers

        tilings = _all_tiling_covers(
            board,
            in_edition,
            assignment,
            candidates,
            set(packed),
        )
        fitting = [t for t in tilings if t <= frozenset(packed)]
        if not fitting:
            # The accepted edition cannot be built from the cover alone; every
            # packed piece is then needed conditionally.
            return sorted(packed)
        smallest = min(fitting, key=lambda s: (len(s), sorted(s)))
        return sorted(smallest)

    def _shortage_reasons(
        self, board: BoardSpec, pieces: list[Piece], report: ReasoningReport
    ) -> list[str]:
        reasons: list[str] = []
        reserved = self.reserved_piece_ids()
        for cell in board.cells:
            if not report.candidate_pieces_for_cell(cell.cell_id):
                if reserved:
                    reasons.append(
                        f"cell {cell.cell_id!r}: no *available* piece survives the "
                        f"face/edge/orientation exclusions; "
                        f"{sorted(reserved)} are already held by promised/sealed boxes"
                    )
                else:
                    reasons.append(
                        f"cell {cell.cell_id!r}: no piece survives the face/edge/"
                        f"orientation exclusions in any still-possible edition"
                    )
        for piece in pieces:
            live_editions = report.placements_by_piece.get(piece.piece_id, {})
            if not any(live_editions.values()):
                reasons.append(
                    f"piece {piece.piece_id!r}: excluded from every cell "
                    f"(photo edition {piece.photo_edition!r}, back code "
                    f"{piece.back_code!r})"
                )
        if not reasons:
            reasons.append(
                "the surviving edition interpretations cannot all be tiled with "
                "distinct available pieces"
            )
        return reasons
