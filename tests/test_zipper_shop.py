from __future__ import annotations

import json
import unittest

from solo.revision import RevisionLibrary
from solo.zipper import (
    BOTH,
    OUTBOUND,
    REVERSED,
    RIVET,
    TRANSPLANT,
    DonorTape,
    Garment,
    LooseTooth,
    RepairCounter,
    RevisionEditor,
    ShiftPlanner,
    Stop,
    Tape,
    build_standard_order_text,
    change_one_character,
    longest_durable_segment,
    parse_work_order,
    simulate_zipper,
)


class TravelSemanticsTests(unittest.TestCase):
    def test_canonical_abc_cba_reverse_completes(self) -> None:
        report = simulate_zipper(Tape("ABC"), Tape("CBA", REVERSED))
        self.assertTrue(report.complete)
        self.assertTrue(report.deliverable)
        self.assertEqual(report.position, 0)
        self.assertEqual(len(report.events), 12)  # pair/cross + cross/unpair x3

    def test_boundary_two_out_only_blocks_return_at_two(self) -> None:
        order = Garment(
            "ABC",
            "CBA",
            {1: BOTH, 2: Stop(2, OUTBOUND), 3: BOTH},
            3,
            garment_id="one-way",
        )
        report = order.simulate()
        self.assertFalse(report.complete)
        self.assertTrue(report.outbound_complete)
        self.assertFalse(report.return_complete)
        self.assertEqual(report.position, 2)
        self.assertEqual(report.blocked_cell, 2)
        self.assertEqual(report.blocked_boundary, 2)
        self.assertEqual(report.blocked_direction, "return")
        self.assertEqual(report.blocked_direction, "back")
        self.assertEqual(report.code, "STOP_DIRECTION")
        # Cell 3 had already meshed outbound and been opened before the slider
        # reached boundary 2; it is the old-tooth segment cycled by this run.
        self.assertEqual(report.cycled_cells, (3,))
        self.assertEqual(report.completed_old_tooth_segment, (3, 3))

        durable = [(s.start_cell, s.end_cell) for s in order.durable_segments()]
        self.assertIn((1, 1), durable)
        self.assertIn((3, 3), durable)
        self.assertNotIn((2, 2), durable)
        self.assertNotIn((1, 3), durable)
        self.assertEqual(longest_durable_segment(order).length, 1)

    def test_right_middle_d_blocks_outbound_cell_two(self) -> None:
        report = simulate_zipper("ABC", "CDA", {1: "+", 2: "+", 3: "+"}, 3)
        self.assertFalse(report.complete)
        self.assertEqual(report.position, 1)
        self.assertEqual(report.blocked_direction, OUTBOUND)
        self.assertEqual(report.blocked_cell, 2)
        self.assertEqual(report.code, "TOOTH_MISMATCH")

    def test_four_cell_trip_on_three_cell_tapes_blocks_at_four(self) -> None:
        report = simulate_zipper("ABC", "CBA", required_cells=4)
        self.assertFalse(report.complete)
        self.assertEqual(report.position, 3)
        self.assertEqual(report.blocked_direction, "out")
        self.assertEqual(report.blocked_cell, 4)
        self.assertEqual(report.code, "BEYOND_TAPE")

    def test_reverse_tape_reads_tail_to_head(self) -> None:
        self.assertEqual(Tape("CBA", REVERSED).effective_letters(), ("A", "B", "C"))
        self.assertEqual(Tape("CBA", REVERSED).logical_to_physical_index(1), 2)  # 0-based index 2? no:
        # cell 1 -> physical position 3 -> zero index 2.
        self.assertEqual(Tape("CBA", REVERSED).logical_to_physical_index(2), 1)


class WorkOrderRevisionTests(unittest.TestCase):
    def test_one_character_change_reports_actual_decode_spans(self) -> None:
        editor = RevisionEditor(build_standard_order_text())
        base = editor.library.get(editor.version_id)
        doc = base.decode()
        self.assertIn("BOUNDARY 2 +", base.text)

        change = editor.change_boundary(2, OUTBOUND)
        self.assertEqual(change.old_char, "+")
        self.assertEqual(change.new_char, ">")
        self.assertEqual(change.condition_kind, "boundary_direction")
        self.assertEqual(change.boundary, 2)
        self.assertEqual((change.old_byte_start, change.old_byte_end), doc.byte_range(change.char_start))
        self.assertEqual(change.old_byte_end - change.old_byte_start, 1)
        self.assertTrue(editor.library.verify(change.version_id))

        parsed = editor.parsed(change.version_id)
        self.assertEqual(parsed.stops[2].mode, OUTBOUND)
        self.assertFalse(parsed.simulate().complete)

    def test_direct_library_apply_one_character(self) -> None:
        editor = RevisionEditor(build_standard_order_text())
        text = editor.version.text
        pos = text.index("TRIP 3") + len("TRIP ")
        change = change_one_character(editor.library, editor.version_id, pos, "4")
        self.assertEqual(change.condition_kind, "required_cells")
        self.assertEqual(editor.parsed(change.version_id).required_cells, 4)

    def test_parser_ignores_pair_annotation_and_supports_ascii(self) -> None:
        text = build_standard_order_text()
        order = parse_work_order(text)
        self.assertEqual(order.left.physical_text(), "ABC")
        self.assertEqual(order.right.effective_letters(), ("A", "B", "C"))
        self.assertEqual(order.required_cells, 3)
        # The generated note contains SOLO-representable PAIR Chinese.
        body = editor_body(text)
        self.assertIn(b"\xff\x01", body)


def editor_body(text: str) -> bytes:
    from solo.codec import encode_text_bytes

    return encode_text_bytes(text)


class CounterStoryTests(unittest.TestCase):
    def garment(self, gid: str) -> Garment:
        return Garment(
            "ABC",
            "CXA",
            {1: "+", 2: "+", 3: "+"},
            3,
            garment_id=gid,
            embroidered_tapes=(f"named-{gid}",),
            allowed_seams=(0, 1, 2, 3),
            target_letter="B",
        )

    def test_unauthorized_stop_is_rejected_not_modified(self) -> None:
        order = self.garment("stop")
        order.stops[2] = Stop(2, OUTBOUND)
        counter = RepairCounter()
        estimate = counter.estimate(order)
        self.assertEqual(estimate.status, "rejected")
        self.assertFalse(estimate.can_accept)
        rejected = counter.accept(estimate)
        self.assertFalse(rejected.accepted)
        receipt = rejected.complete()
        self.assertFalse(receipt.passed)
        self.assertEqual(order.stops[2].mode, OUTBOUND)

    def test_rivet_inventory_and_restock_supports_second_order(self) -> None:
        g1, g2 = self.garment("G1"), self.garment("G2")
        counter = RepairCounter(loose_teeth=[LooseTooth("B", "B-LAST")])

        first = counter.service(g1, craft=RIVET)
        self.assertTrue(first.passed, first.errors)
        self.assertEqual(counter.audit("G1").tooth_ids, ("B-LAST",))

        quote = counter.estimate(g2, craft=RIVET)
        self.assertFalse(quote.can_accept)
        counter.add_loose_tooth("B", serial="B-NEW")
        second = counter.service(g2, craft=RIVET)
        self.assertTrue(second.passed, second.errors)
        self.assertEqual(counter.audit().tooth_ids, ("B-LAST", "B-NEW"))
        self.assertEqual(second.preserved_embroidery, ("named-G2",))

    def test_donor_cut_audit_and_loose_b_does_not_replace_segment(self) -> None:
        g1, g2 = self.garment("H1"), self.garment("H2")
        counter = RepairCounter(donors=[DonorTape("D7", "BXY", "normal")])

        first = counter.service(g1, craft=TRANSPLANT)
        self.assertTrue(first.passed, first.errors)
        audit = counter.audit("H1")
        self.assertEqual(audit.donor_ids, ("D7",))
        self.assertEqual(audit.cut_marks, (0, 1))
        self.assertEqual(counter.donors[0].available_ranges, [(1, 3)])
        self.assertEqual(counter.donors[0].donor_id, "D7")

        self.assertFalse(counter.estimate(g2, craft=TRANSPLANT).can_accept)
        counter.add_loose_tooth("B", serial="LOOSE-UNUSED")
        self.assertFalse(counter.estimate(g2, craft=TRANSPLANT).can_accept)
        # Same added material is enough if the shop chooses riveting instead.
        self.assertTrue(counter.service(g2, craft=RIVET).passed)

    def test_shift_separation(self) -> None:
        planner = ShiftPlanner()
        bad = {1: 1, 2: 2}
        good = {1: 1, 2: 3}
        self.assertTrue(planner.violations(bad))
        self.assertFalse(planner.violations(good))
        scheduled = planner.schedule([1, 2])
        self.assertGreaterEqual(abs(scheduled[1] - scheduled[2]), 2)

    def test_report_is_json_like(self) -> None:
        report = simulate_zipper("ABC", "CDA", required_cells=3)
        payload = json.dumps(report.to_dict(), ensure_ascii=False)
        self.assertIn("TOOTH_MISMATCH", payload)
        self.assertEqual(report["blocked_cell"], 2)
        position, direction, cell = report
        self.assertEqual((position, cell), (1, 2))
        self.assertEqual(direction, OUTBOUND)


if __name__ == "__main__":
    unittest.main()
