"""Unit tests for board geometry, edge exclusions and interpretation logic."""

from __future__ import annotations

import pytest

from extcontract.errors import ContractError
from extcontract.puzzle import (
    AMBIGUOUS,
    UNREAD,
    candidate_placements,
    parse_board,
    photo_row,
    reason,
    rotate_edges,
)
from extcontract.puzzle.pieces import piece_from_row


def _board():
    return parse_board(
        {
            "board_id": "grid",
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
    )


def _piece(pid, zone, edges, orientations=(0,), photo="X/Y", back=UNREAD):
    row = {
        "row_id": pid,
        "revision": 1,
        "values": {
            "piece_id": pid,
            "face_zone": zone,
            "edge_n": edges["n"],
            "edge_e": edges["e"],
            "edge_s": edges["s"],
            "edge_w": edges["w"],
            "orientations": "|".join(str(o) for o in orientations),
            "photo_edition": photo,
            "back_code": back,
            "photo_id": "ph",
            "note": "",
        },
        "sources": {},
    }
    return piece_from_row(row, editions=("X", "Y"))


def test_rotation_turns_edge_tuple():
    base = {"n": "T", "e": "F", "s": "B", "w": "F"}
    assert rotate_edges(base, 90) == {"n": "F", "e": "T", "s": "F", "w": "B"}
    assert rotate_edges(base, 180) == {"n": "B", "e": "F", "s": "T", "w": "F"}


def test_board_rejects_more_than_six_cells():
    raw = {
        "board_id": "big",
        "rows": 2,
        "cols": 4,
        "editions": ["X"],
        "cells": [
            {"cell_id": f"C{i}", "row": i // 4, "col": i % 4,
             "zones": {"X": f"Z{i}"}}
            for i in range(8)
        ],
    }
    with pytest.raises(ContractError, match="at most 6 cells"):
        parse_board(raw)


def test_face_zone_exclusion():
    board = _board()
    wrong = _piece("P", "Z-OTHER", {"n": "F", "e": "T", "s": "F", "w": "F"})
    report = reason(board, [wrong], allow_rotation=False)
    assert not report.feasible
    assert any(e["rule"] == "face" for e in report.exclusions)


def test_border_requires_flat_edge():
    board = _board()
    # North side of L is a border but carries a tab -> must be excluded.
    bad_border = _piece("P", "ZL", {"n": "T", "e": "T", "s": "F", "w": "F"})
    candidates, exclusions = candidate_placements(
        board, [bad_border], allow_rotation=False
    )
    assert candidates["P"] == {"X": [], "Y": []}
    assert any(e["rule"] == "borders" for e in exclusions)


def test_internal_flat_edge_is_excluded():
    board = _board()
    # East side of L is internal (touches R); a flat edge there cannot mate.
    bad_internal = _piece("P", "ZL", {"n": "F", "e": "F", "s": "F", "w": "F"})
    candidates, _ = candidate_placements(board, [bad_internal], allow_rotation=False)
    assert candidates["P"] == {"X": [], "Y": []}


def test_adjacent_edges_must_be_tab_blank_opposites():
    board = _board()
    left = _piece("L", "ZL", {"n": "F", "e": "T", "s": "F", "w": "F"})
    right_ok = _piece("R", "ZR", {"n": "F", "e": "F", "s": "F", "w": "B"})
    right_bad = _piece("R", "ZR", {"n": "F", "e": "F", "s": "F", "w": "T"})
    assert reason(board, [left, right_ok], allow_rotation=False).feasible
    bad_report = reason(board, [left, right_bad], allow_rotation=False)
    assert not bad_report.feasible


def test_no_rotation_policy_drops_unrotatable_piece():
    board = _board()
    # Piece only allows 90-degree use; as-photographed (0) it is unusable.
    sideways = _piece(
        "P", "ZL", {"n": "F", "e": "F", "s": "F", "w": "T"},
        orientations=(90,),
    )
    report = reason(board, [sideways], allow_rotation=False)
    assert any(e["rule"] == "rotation" for e in report.exclusions)
    assert not report.feasible
    # With rotation permitted the same piece enters candidates.
    assert reason(board, [sideways], allow_rotation=True).feasible is False  # still incomplete board, but no rotation exclusion
    rotated_report = reason(board, [sideways], allow_rotation=True)
    assert not any(e["rule"] == "rotation" for e in rotated_report.exclusions)


def test_back_code_pins_edition_and_removes_interpretation():
    board = _board()
    left = _piece("L", "ZL", {"n": "F", "e": "T", "s": "F", "w": "F"})
    right_x = _piece(
        "R", "ZR", {"n": "F", "e": "F", "s": "F", "w": "B"}, back="X"
    )
    report = reason(board, [left, right_x], allow_rotation=False)
    assert {i.edition for i in report.interpretations} == {"X"}
    assert right_x.editions == ("X",)


def test_ambiguous_piece_keeps_both_editions():
    piece = _piece("P", "ZR", {"n": "F", "e": "F", "s": "F", "w": "B"})
    assert piece.editions == ("X", "Y")
    assert piece.back_paper_on is True


def test_surviving_interpretation_requires_full_bijection():
    board = _board()
    # Two pieces that both only fit the left cell: the right cell stays empty.
    left_a = _piece("LA", "ZL", {"n": "F", "e": "T", "s": "F", "w": "F"})
    left_b = _piece("LB", "ZL", {"n": "F", "e": "T", "s": "F", "w": "F"})
    report = reason(board, [left_a, left_b], allow_rotation=False)
    assert not report.feasible
    assert report.candidate_pieces_for_cell("R") == []


def test_known_neighbour_evidence_excludes_non_mating_candidate():
    board = _board()
    left = _piece("L", "ZL", {"n": "F", "e": "T", "s": "F", "w": "F"})
    right = _piece("R", "ZR", {"n": "F", "e": "F", "s": "F", "w": "T"})  # tab vs tab
    candidates, exclusions = candidate_placements(
        board,
        [left, right],
        allow_rotation=False,
        known_neighbors={("R", "w"): ("L", "T")},
    )
    assert candidates["R"] == {"X": [], "Y": []}
    assert any(e["rule"] == "adjacency" for e in exclusions)


def test_photo_row_helper_rejects_bad_edge_mark():
    with pytest.raises(ContractError):
        photo_row(
            "P", face_zone="Z", edges={"n": "X", "e": "F", "s": "F", "w": "F"},
            orientations=[0], photo_edition=AMBIGUOUS,
        )
