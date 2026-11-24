#!/usr/bin/env python3
"""Deterministic local replay of the zipper sample-garment work order.

Run from the repository root:

    python3 examples/run_zipper_replay.py

It writes ``artifacts/zipper_replay.json`` and exits non-zero if any of the
brief's required outcomes changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from solo.zipper import (  # noqa: E402
    BOTH,
    OUTBOUND,
    RIVET,
    TRANSPLANT,
    CompletionReceipt,
    Garment,
    LooseTooth,
    MaterialCabinet,
    RepairCounter,
    RevisionEditor,
    DonorTape,
    build_standard_order_text,
    build_story_demo,
    longest_durable_segment,
    simulate_zipper,
)


def assert_story() -> dict:
    # -- Revision-library conditions ---------------------------------------
    editor = RevisionEditor(build_standard_order_text())
    assert simulate_zipper(editor.parsed()).complete

    changed = editor.change_boundary(2, OUTBOUND)
    one_way_order = editor.parsed(changed.version_id)
    one_way = one_way_order.simulate()
    assert not one_way.complete
    assert one_way.position == 2
    assert one_way.blocked_direction == "return"
    assert one_way.blocked_direction == "back"  # synonym accepted by reports
    assert one_way.blocked_cell == 2
    assert one_way.blocked_boundary == 2
    assert changed.old_byte_end - changed.old_byte_start == 1
    assert changed.condition_kind == "boundary_direction"
    assert longest_durable_segment(one_way_order).length == 1

    rejected = RepairCounter().estimate(one_way_order)
    assert rejected.status == "rejected"
    assert not rejected.can_accept

    restored = editor.change_boundary(2, BOTH)
    assert editor.parsed(restored.version_id).simulate().complete

    d_change = editor.change_tooth("right", 2, "D", physical=True)
    mismatch = editor.parsed(d_change.version_id).simulate()
    assert not mismatch.complete
    assert mismatch.blocked_direction == OUTBOUND
    assert mismatch.blocked_cell == 2

    restored_tooth = editor.change_tooth("right", 2, "B", physical=True)
    assert editor.parsed(restored_tooth.version_id).simulate().complete
    trip_change = editor.change_trip_cells(4)
    four = editor.parsed(trip_change.version_id).simulate()
    assert not four.complete
    assert four.blocked_direction == OUTBOUND
    assert four.blocked_cell == 4

    # -- Shared materials: loose-tooth/rivet route --------------------------
    def garment(gid: str, embroidery: str) -> Garment:
        return Garment(
            "ABC",
            "CXA",
            {1: "+", 2: "+", 3: "+"},
            3,
            garment_id=gid,
            embroidered_tapes=(embroidery,),
            allowed_seams=(0, 1, 2, 3),
            target_letter="B",
        )

    g1, g2 = garment("G1", "绣字·云雁"), garment("G2", "绣字·松烟")
    cabinet = RepairCounter(loose_teeth=[LooseTooth("B", "LOOSE-B-LAST")])
    first = cabinet.service(g1, craft=RIVET)
    assert isinstance(first, CompletionReceipt) and first.passed
    assert cabinet.audit(garment_id="G1").tooth_ids == ("LOOSE-B-LAST",)

    second_quote = cabinet.estimate(g2, craft=RIVET)
    assert not second_quote.can_accept
    cabinet.add_loose_tooth("B", serial="LOOSE-B-NEW")
    second = cabinet.service(g2, craft=RIVET)
    assert second.passed
    assert cabinet.audit().tooth_ids == ("LOOSE-B-LAST", "LOOSE-B-NEW")

    # -- Shared materials: numbered donor route -----------------------------
    h1, h2 = garment("H1", "绣字·青衿"), garment("H2", "绣字·白露")
    donor_cabinet = RepairCounter(donors=[DonorTape("DONOR-B-7", "BXY", "normal")])
    donor_first = donor_cabinet.service(h1, craft=TRANSPLANT)
    assert donor_first.passed
    audit1 = donor_cabinet.audit(garment_id="H1")
    assert audit1.donor_ids == ("DONOR-B-7",)
    assert audit1.cut_marks == (0, 1)

    donor_second = donor_cabinet.estimate(h2, craft=TRANSPLANT)
    assert not donor_second.can_accept
    # A loose tooth is not a whole donor segment, so same-craft transplant
    # remains unavailable until a compatible numbered segment is restocked.
    donor_cabinet.add_loose_tooth("B", serial="LOOSE-B-UNUSED")
    assert not donor_cabinet.estimate(h2, craft=TRANSPLANT).can_accept
    # The added loose B does enable the alternative rivet craft.
    assert donor_cabinet.service(h2, craft=RIVET).passed

    story = build_story_demo()
    story["replay_assertions"] = "passed"
    return story


def main() -> int:
    story = assert_story()
    out = ROOT / "artifacts" / "zipper_replay.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(story, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("zipper replay: all story assertions passed")
    print(f"wrote {out.relative_to(ROOT)}")
    print("canonical trip: complete")
    print("one-way boundary 2: return blocked at cell/boundary 2; counter rejects")
    print("right B->D: outbound blocked at cell 2")
    print("trip=4: outbound blocked at cell 4")
    print("rivet route: LOOSE-B-LAST then LOOSE-B-NEW delivered both garments")
    print("donor route: DONOR-B-7 cut [0,1); loose B alone does not restart transplant")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
