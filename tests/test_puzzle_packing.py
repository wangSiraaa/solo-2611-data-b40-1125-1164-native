"""End-to-end tests of the order intake and sealed-box registry.

Covers the mandated two-cell scenarios:

1. left cell settled, one back-confirmed X matching right piece, two-piece
   limit -> deliver; a competing order cannot reuse either piece;
2. correcting the right piece's publication to confirmed Y blocks an X order,
   restoring it reopens delivery, and an order accepting only Y stays blocked;
3. two unseen back-paper right pieces (one X, one Y physically) -> a three-
   piece box with one spare and edge-mark selection notes; a two-piece limit
   reports the weight shortage with the exact reason.
"""

from __future__ import annotations

import pytest

from extcontract import IngestionLibrary
from extcontract.errors import ContractError
from extcontract.puzzle import (
    UNREAD,
    OrderRequest,
    PackingDesk,
    photo_file,
    photo_reader_contract,
    photo_row,
    photo_writer_contract,
)

RIGHT_EDGES = {"n": "F", "e": "F", "s": "F", "w": "B"}
LEFT_EDGES = {"n": "F", "e": "T", "s": "F", "w": "F"}

BOARD = {
    "board_id": "two-cell",
    "rows": 1,
    "cols": 2,
    "editions": ["X", "Y"],
    "cells": [
        {"cell_id": "L", "row": 0, "col": 0,
         "zones": {"X": "ZL", "Y": "ZL"}},
        {"cell_id": "R", "row": 0, "col": 1,
         "zones": {"X": "ZR", "Y": "ZR"}},
    ],
}


def _order(order_id, *, edition="X", limit=2, rotation=False):
    return OrderRequest.parse(
        {
            "order_id": order_id,
            "board": BOARD,
            "edition": edition,
            "allow_rotation": rotation,
            "weight_limit_pieces": limit,
        }
    )


def _desk(rows):
    writer = photo_writer_contract()
    library = IngestionLibrary(photo_reader_contract())
    result = library.ingest(writer, photo_file("photo-batch", rows))
    assert result.accepted, result.errors
    return library, PackingDesk(library)


def _left(pid="L", back=UNREAD):
    return photo_row(
        pid, face_zone="ZL", edges=LEFT_EDGES, orientations=[0],
        photo_edition="X/Y", back_code=back,
    )


def _right(pid, *, back=UNREAD, photo="X/Y"):
    return photo_row(
        pid, face_zone="ZR", edges=RIGHT_EDGES, orientations=[0],
        photo_edition=photo, back_code=back,
    )


# ---------------------------------------------------------------------------
# Scenario 1: deliver, contention, seal and reconcile by box number
# ---------------------------------------------------------------------------

def test_scenario_one_delivers_and_seals_two_piece_box():
    library, desk = _desk([_left(), _right("RX", back="X")])
    result = desk.take_order(_order("O1"))
    assert result.promised, result.reasons
    box = result.box
    assert list(box.packed_pieces) == ["L", "RX"]
    assert box.spare_pieces == ()
    assert box.weight_pieces == 2
    assert box.weight_limit_pieces == 2
    sealed = desk.seal_box(box.box_no)
    assert sealed.sealed is True


def test_competing_order_cannot_reuse_a_promised_piece():
    _, desk = _desk([_left(), _right("RX", back="X")])
    first = desk.take_order(_order("O1"))
    assert first.promised
    second = desk.take_order(_order("O2"))
    assert not second.promised
    # The shortage names the cells that lost their only stock.
    joined = " ".join(second.reasons)
    assert "L" in joined and "R" in joined
    # Only the first box exists and holds both physical pieces.
    assert len(desk.physical_ledger()["boxes"]) == 1
    assert desk.physical_ledger()["available_pieces"] == []


def test_box_lookup_returns_packed_pieces_and_publication_basis():
    library, desk = _desk([_left(), _right("RX", back="X")])
    result = desk.take_order(_order("O1"))
    desk.seal_box(result.box.box_no)

    box = desk.box(result.box.box_no)
    assert set(box.packed_pieces) == {"L", "RX"}
    basis = box.piece_basis["RX"]
    assert basis["back_code"] == "X"
    assert basis["revision"] == 1
    # Full ingest provenance travels with the piece, so the buyer's goods can
    # be reconciled against the ledger after sealing.
    assert basis["sources"]["back_code"]["input"] == "X"
    assert basis["sources"]["piece_id"]["conversion"] == "text_identity"

    ledger = desk.physical_ledger()
    assert ledger["sealed_pieces"] == ["L", "RX"]
    assert ledger["total_pieces"] == 2


def test_unknown_box_number_raises():
    _, desk = _desk([_left(), _right("RX", back="X")])
    with pytest.raises(ContractError, match="unknown box number"):
        desk.box("BOX-9999")


# ---------------------------------------------------------------------------
# Scenario 2: back-code correction flips deliverability; restore; Y-only order
# ---------------------------------------------------------------------------

def test_correcting_right_piece_to_y_blocks_x_order():
    library, desk = _desk([_left(), _right("RX", back="X")])
    writer = photo_writer_contract()
    previous = _right("RX", back="X")
    correction = {
        "row_id": "RX",
        "revision": 2,
        "values": {**previous["values"], "back_code": "Y"},
    }
    accepted = library.ingest(writer, photo_file("back-code-read", [correction]))
    assert accepted.accepted, accepted.errors

    result = desk.take_order(_order("O3"))
    assert not result.promised
    assert any("edition 'X'" in reason for reason in result.reasons)
    # The reasoning snapshot still explains why: only edition Y survives.
    assert {i.edition for i in result.report.interpretations} == {"Y"}


def test_restore_correction_reopens_delivery_and_keeps_piece_number():
    library, desk = _desk([_left(), _right("RX", back="X")])
    writer = photo_writer_contract()
    previous = _right("RX", back="X")
    library.ingest(
        writer,
        photo_file("corr", [
            {"row_id": "RX", "revision": 2,
             "values": {**previous["values"], "back_code": "Y"}},
        ]),
    )
    library.ingest(
        writer,
        photo_file("restore", [
            {"row_id": "RX", "revision": 3,
             "values": {**previous["values"], "back_code": "X"}},
        ]),
    )
    row = library.get_row("RX")
    assert row.row_id == "RX"
    assert row.revision == 3 and row.encoded["back_code"] == "X"

    result = desk.take_order(_order("O4"))
    assert result.promised, result.reasons
    assert "RX" in result.box.packed_pieces


def test_order_accepting_only_y_cannot_deliver_x_confirmed_stock():
    _, desk = _desk([_left(), _right("RX", back="X")])
    result = desk.take_order(_order("O5", edition="Y"))
    assert not result.promised
    assert any("edition 'Y'" in reason for reason in result.reasons)


# ---------------------------------------------------------------------------
# Scenario 3: unseen back paper, two right pieces, spare in the same box
# ---------------------------------------------------------------------------

def test_two_unseen_right_pieces_pack_three_piece_box_with_one_spare():
    _, desk = _desk([_left(), _right("RA"), _right("RB")])
    result = desk.take_order(_order("O6", limit=3))
    assert result.promised, result.reasons
    box = result.box
    assert set(box.packed_pieces) == {"L", "RA", "RB"}
    assert box.weight_pieces == 3
    # Exactly one right piece is core under the buyer's X edition; the other
    # is the trial-fit spare.
    assert len(box.spare_pieces) == 1
    assert box.spare_pieces[0] in {"RA", "RB"}
    # Selection notes are delivered with the box and mention edge-mark fitting.
    note_text = " ".join(n["instruction"] for n in box.instructions)
    assert "边纹" in note_text
    right_note = next(n for n in box.instructions if n["cell_id"] == "R")
    assert set(right_note["candidate_pieces"]) == {"RA", "RB"}


def test_three_piece_cover_rejected_by_two_piece_weight_limit():
    # A fresh desk with no prior reservation: the cover itself weighs three
    # pieces, so the two-piece limit must be reported as a weight shortage.
    _, desk = _desk([_left(), _right("RA"), _right("RB")])
    result = desk.take_order(_order("O7", limit=2))
    assert not result.promised
    assert any("weight limit is 2" in reason for reason in result.reasons)


def test_sealed_first_box_makes_second_order_short_on_same_piece_numbers():
    _, desk = _desk([_left(), _right("RA"), _right("RB")])
    first = desk.take_order(_order("O8", limit=3))
    assert first.promised
    desk.seal_box(first.box.box_no)
    second = desk.take_order(_order("O9", limit=3))
    assert not second.promised
    # Every piece of the first physical box is absent from available stock.
    assert set(first.box.packed_pieces).isdisjoint(
        desk.physical_ledger()["available_pieces"]
    )


# ---------------------------------------------------------------------------
# Request validation and rotation policy
# ---------------------------------------------------------------------------

def test_order_request_validates_weight_limit_below_cell_count():
    with pytest.raises(ContractError, match="weight_limit_pieces"):
        OrderRequest.parse(
            {"order_id": "BAD", "board": BOARD, "edition": "X",
             "allow_rotation": False, "weight_limit_pieces": 1}
        )


def test_order_request_rejects_undeclared_edition():
    with pytest.raises(ContractError, match="not one of"):
        OrderRequest.parse(
            {"order_id": "BAD", "board": BOARD, "edition": "Z",
             "allow_rotation": False, "weight_limit_pieces": 2}
        )


def test_duplicate_order_id_is_rejected():
    _, desk = _desk([_left(), _right("RX", back="X")])
    assert desk.take_order(_order("DUP")).promised
    with pytest.raises(ContractError, match="already has box"):
        desk.take_order(_order("DUP"))


# ---------------------------------------------------------------------------
# Four-cell board where every piece must be rotated
# ---------------------------------------------------------------------------

FOUR_CELL_BOARD = {
    "board_id": "four-cell",
    "rows": 2,
    "cols": 2,
    "editions": ["X"],
    "cells": [
        {"cell_id": "A", "row": 0, "col": 0, "zones": {"X": "ZA"}},
        {"cell_id": "B", "row": 0, "col": 1, "zones": {"X": "ZB"}},
        {"cell_id": "C", "row": 1, "col": 0, "zones": {"X": "ZC"}},
        {"cell_id": "D", "row": 1, "col": 1, "zones": {"X": "ZD"}},
    ],
}

# Orientation-0 edge sets of a consistent 2x2; each photographed piece is that
# shape rotated 180 degrees, so no rotation means every flat points inward.
_NEEDED = {
    "A": {"n": "F", "e": "T", "s": "B", "w": "F"},
    "B": {"n": "F", "e": "F", "s": "T", "w": "B"},
    "C": {"n": "T", "e": "B", "s": "F", "w": "F"},
    "D": {"n": "B", "e": "F", "s": "F", "w": "T"},
}
_ZONES = {"A": "ZA", "B": "ZB", "C": "ZC", "D": "ZD"}


def _four_cell_desk():
    from extcontract.puzzle import rotate_edges

    rows = [
        photo_row(
            f"P{pid}",
            face_zone=_ZONES[pid],
            edges=rotate_edges(_NEEDED[pid], 180),
            orientations=[0, 180],
            photo_edition="X",
            back_code="X",
        )
        for pid in "ABCD"
    ]
    return _desk(rows)


def test_rotation_required_four_cell_board():
    _, desk = _four_cell_desk()
    blocked = desk.take_order(
        OrderRequest.parse(
            {"order_id": "NR", "board": FOUR_CELL_BOARD, "edition": "X",
             "allow_rotation": False, "weight_limit_pieces": 4}
        )
    )
    assert not blocked.promised

    allowed = desk.take_order(
        OrderRequest.parse(
            {"order_id": "R", "board": FOUR_CELL_BOARD, "edition": "X",
             "allow_rotation": True, "weight_limit_pieces": 4}
        )
    )
    assert allowed.promised, allowed.reasons
    assert len(allowed.box.packed_pieces) == 4
    tiling = allowed.report.interpretations[0].placement_order()
    assert {p.cell_id for p in tiling} == {"A", "B", "C", "D"}
    assert all(p.orientation == 180 for p in tiling)
