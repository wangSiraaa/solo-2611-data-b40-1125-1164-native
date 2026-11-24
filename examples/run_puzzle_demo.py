"""End-to-end demonstration of the loose-piece box-assembly service.

Run with:

    PYTHONPATH=. python3.11 examples/run_puzzle_demo.py

It walks the mandated two-cell story entirely through the project's existing
contract -> ingest -> publish machinery plus the puzzle packing desk:

  1. the workstation files photo interpretation rows under the pw1 writer
     contract; the pr1 ledger ingests them and publish() is the only view the
     packing desk ever reads;
  2. order O1 (edition X, no rotation, two-piece box limit) is promised and
     sealed; a competing order O2 for the same piece numbers is short;
  3. the sealed box is reconciled by box number: exact pieces and the
     publication basis (revision + full ingest sources) of each;
  4. the right piece's publication is corrected to confirmed Y (same piece id,
     higher revision): the X order can no longer be promised; restoring the X
     publication reopens it, while an order accepting only Y stays short;
  5. with two unseen back-paper right pieces (physically one X, one Y) an
     order is delivered as a three-piece box: one right piece rides as a
     trial-fit spare with edge-mark selection notes; a two-piece limit gives
     the exact shortage reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extcontract import IngestionLibrary  # noqa: E402
from extcontract.puzzle import (  # noqa: E402
    OrderRequest,
    PackingDesk,
    photo_file,
    photo_reader_contract,
    photo_row,
    photo_writer_contract,
)

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
LEFT_EDGES = {"n": "F", "e": "T", "s": "F", "w": "F"}
RIGHT_EDGES = {"n": "F", "e": "F", "s": "F", "w": "B"}


def banner(number, title):
    print(f"\n=== {number}. {title} ===")


def order(order_id, *, edition="X", limit=2):
    return OrderRequest.parse(
        {
            "order_id": order_id,
            "board": BOARD,
            "edition": edition,
            "allow_rotation": False,
            "weight_limit_pieces": limit,
        }
    )


def open_desk(rows):
    writer = photo_writer_contract()
    library = IngestionLibrary(photo_reader_contract())
    result = library.ingest(writer, photo_file("workstation-photos", rows))
    assert result.accepted, result.errors
    return writer, library, PackingDesk(library)


def main() -> int:
    # 1. First box: left cell settled, one back-confirmed X right piece ------
    banner(1, "first box promised, competing order refused, box sealed")
    writer, library, desk = open_desk(
        [
            photo_row("L", face_zone="ZL", edges=LEFT_EDGES, orientations=[0],
                      photo_edition="X/Y"),  # back paper stays on
            photo_row("RX", face_zone="ZR", edges=RIGHT_EDGES, orientations=[0],
                      photo_edition="X/Y", back_code="X"),
        ]
    )
    first = desk.take_order(order("O1"))
    assert first.promised, first.reasons
    print("O1:", first.status, "->", first.box.box_no,
          "packed", list(first.box.packed_pieces))
    desk.seal_box(first.box.box_no)

    competitor = desk.take_order(order("O2"))
    print("O2 (contends for the same piece numbers):", competitor.status)
    assert not competitor.promised
    print("shortage:", competitor.reasons)

    # 2. Reconcile by box number against the post-seal physical ledger -------
    banner(2, "reconcile sealed box by box number")
    box = desk.box(first.box.box_no)
    print("physical ledger:", json.dumps(desk.physical_ledger(), ensure_ascii=False))
    for pid in box.packed_pieces:
        basis = box.piece_basis[pid]
        print(
            f"  {pid}: revision {basis['revision']}, photo_edition "
            f"{basis['photo_edition']!r}, back_code {basis['back_code']!r}, "
            f"sources conversions "
            f"{sorted(v['conversion'] for v in basis['sources'].values())}"
        )

    # 3. Publication correction: right piece confirmed Y, then restored ------
    banner(3, "back-code correction flips deliverability; restore reopens it")
    writer2, library2, desk2 = open_desk(
        [
            photo_row("L", face_zone="ZL", edges=LEFT_EDGES, orientations=[0],
                      photo_edition="X/Y"),
            photo_row("RX", face_zone="ZR", edges=RIGHT_EDGES, orientations=[0],
                      photo_edition="X/Y", back_code="X"),
        ]
    )
    previous = photo_row("RX", face_zone="ZR", edges=RIGHT_EDGES, orientations=[0],
                         photo_edition="X/Y", back_code="X")
    correction = {
        "row_id": "RX",
        "revision": 2,
        "values": {**previous["values"], "back_code": "Y"},
    }
    assert library2.ingest(writer2, photo_file("back-code-read", [correction])).accepted
    corrected_order = desk2.take_order(order("O3"))
    print("O3 after RX publication corrected to Y:", corrected_order.status)
    print("  reasons:", corrected_order.reasons)
    assert not corrected_order.promised

    restore = {
        "row_id": "RX",
        "revision": 3,
        "values": {**previous["values"], "back_code": "X"},
    }
    assert library2.ingest(writer2, photo_file("back-code-restore", [restore])).accepted
    reopened = desk2.take_order(order("O4"))
    print("O4 after restoring the X publication (piece id RX kept):",
          reopened.status, list(reopened.box.packed_pieces))
    assert reopened.promised

    y_only = desk2.take_order(order("O5", edition="Y"))
    print("O5 accepting only Y:", y_only.status, "-", y_only.reasons[0])
    assert not y_only.promised

    # 4. Unseen back paper: two right pieces, one spare in a three-piece box --
    banner(4, "two unseen right pieces -> three-piece box with a trial-fit spare")
    _, _, desk3 = open_desk(
        [
            photo_row("L", face_zone="ZL", edges=LEFT_EDGES, orientations=[0],
                      photo_edition="X/Y"),
            photo_row("RA", face_zone="ZR", edges=RIGHT_EDGES, orientations=[0],
                      photo_edition="X/Y"),
            photo_row("RB", face_zone="ZR", edges=RIGHT_EDGES, orientations=[0],
                      photo_edition="X/Y"),
        ]
    )
    spare_order = desk3.take_order(order("O6", limit=3))
    assert spare_order.promised, spare_order.reasons
    sb = spare_order.box
    print("O6: PROMISED",
          "packed", list(sb.packed_pieces), "spare", list(sb.spare_pieces),
          "weight", f"{sb.weight_pieces}/{sb.weight_limit_pieces}")
    print("selection notes:")
    for note in sb.instructions:
        print(" ", json.dumps(note, ensure_ascii=False))

    too_heavy = desk3.take_order(order("O7", limit=2))
    print("O7 with a two-piece limit:", too_heavy.status)
    print("  reasons:", too_heavy.reasons)
    assert not too_heavy.promised

    print("\nAll demo assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
