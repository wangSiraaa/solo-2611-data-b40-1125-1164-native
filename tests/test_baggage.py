"""Tests for the baggage carousel catalog, ambiguity handoff and rack desk."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.baggage import (  # noqa: E402
    GroupTicket,
    RackBank,
    advance_ring,
    build_covered_pair_alternatives,
    encode_covered_read,
    make_handoff,
    make_product,
    parse_command,
    load_stored_frames,
    ingest_timed,
    run_demo,
    run_toothed_case,
    run_two_ring_rack_case,
    switch_bag,
    two_ring_topology,
    canonical_topology,
)
from bitcatalog.warehouse import ObservationWarehouse  # noqa: E402


class CatalogAndBranchBits(unittest.TestCase):
    def setUp(self):
        self.product = make_product()
        self.tmp = tempfile.TemporaryDirectory()
        self.wh = ObservationWarehouse(":memory:")

    def tearDown(self):
        self.wh.close()
        self.tmp.cleanup()

    def test_exactly_three_small_catalog_messages(self):
        self.assertEqual([m.frame_id for m in self.product.messages], [1, 2, 3])
        self.assertEqual(len(self.product.messages), 3)

    def test_dirty_inactive_branch_bits_are_not_bag_number(self):
        frame = encode_covered_read(
            self.product,
            "DIRTY-READ",
            beat=0,
            ring="A",
            cell=2,
            candidate_bags=(2, 3),
            dirty_inactive_hint=15,
        )
        ingest_timed(self.wh, self.product, [frame])
        stored = load_stored_frames(self.wh, self.product, [frame])[0]

        self.assertEqual(stored.selector, 2)
        # The dirty branch-1-only nibble is physically retained in the frame...
        self.assertEqual(stored.payload_hex[7], "f")
        # ...but the original warehouse exposes no inactive branch signals.
        self.assertNotIn("exact_bag_no", stored.raw)
        self.assertNotIn("branch1_hint", stored.raw)
        command = parse_command(stored)
        self.assertEqual(command["read_type"], "covered_pair")
        self.assertEqual(command["candidates"], (2, 3))
        self.assertFalse(command["inactive_branch_bits_used"])


class CanonicalTiming(unittest.TestCase):
    def setUp(self):
        self.product = make_product()
        self.wh = ObservationWarehouse(":memory:")

    def tearDown(self):
        self.wh.close()

    def test_actual_teeth_from_frame_control_advance(self):
        from bitcatalog.baggage import encode_step

        product = make_product()
        wh = ObservationWarehouse(":memory:")
        frame = encode_step(product, "TEETH-3", 0, "A", 3)
        ingest_timed(wh, product, [frame])
        stored = load_stored_frames(wh, product, [frame])
        command = parse_command(stored[0])
        layout = advance_ring({2: ("A", 2)}, canonical_topology(), command["ring"], command["teeth"])
        self.assertEqual(command["teeth"], 3)
        self.assertEqual(layout[2], ("A", 1))
        wh.close()

    def test_prompt_examples(self):
        default = run_toothed_case(
            self.wh, self.product, case_id="T-DEFAULT", title="default",
            right_cell=2, depart_beat=2,
        )
        shifted = run_toothed_case(
            self.wh, self.product, case_id="T-ONE", title="one",
            right_cell=1, depart_beat=3,
        )
        early = run_toothed_case(
            self.wh, self.product, case_id="T-EARLY", title="early",
            right_cell=2, depart_beat=1,
        )
        self.assertEqual((default.complete_beat, default.delivered), (2, True))
        self.assertEqual((shifted.complete_beat, shifted.delivered), (3, True))
        self.assertEqual((early.complete_beat, early.delivered), (2, False))


class SwitchAndHandoff(unittest.TestCase):
    def test_only_switch_cell_changes_ring(self):
        topo = two_ring_topology()
        layout = {4: ("B", 1)}
        moved_by_teeth = advance_ring(layout, topo, "B", 1)
        self.assertEqual(moved_by_teeth[4], ("B", 2))
        switched = switch_bag(moved_by_teeth, topo, 4, 0, "B", "A")
        self.assertEqual(switched[4], ("A", 2))

        with self.assertRaises(Exception) as at_switch:
            switch_bag(layout, topo, 4, 0, "B", "A")
        self.assertEqual(at_switch.exception.code, "not_at_switch")

    def test_two_alternatives_are_exclusive_whole_layouts(self):
        product = make_product()
        wh = ObservationWarehouse(":memory:")
        try:
            frame = encode_covered_read(product, "AMB", 0, "A", 2, (2, 3))
            ingest_timed(wh, product, [frame])
            stored = load_stored_frames(wh, product, [frame])
            command = parse_command(stored[0])
            topo = two_ring_topology()
            known = {1: ("A", 0), 4: ("B", 1), 5: ("B", 0), 6: ("B", 3)}
            alts = build_covered_pair_alternatives(known, topo, command)
            handoff = make_handoff(product, topo, alts, stored, horizon=1)
        finally:
            wh.close()

        self.assertEqual([a.alt_id for a in alts], ["AMB-1-L1", "AMB-1-L2"])
        for alt in alts:
            self.assertEqual(len(alt.placements), 6)
        self.assertNotEqual(alts[0].placements, alts[1].placements)
        self.assertEqual(handoff.port_options[1], ("PA",))
        self.assertEqual(handoff.port_options[5], ("PB",))

    def test_full_two_ring_delivery_and_no_double_count(self):
        product = make_product()
        wh = ObservationWarehouse(":memory:")
        try:
            result = run_two_ring_rack_case(wh, product)
            serialized = result.as_dict()
        finally:
            wh.close()

        self.assertTrue(result.delivered)
        self.assertEqual(result.complete_beat, 3)
        self.assertIsNotNone(result.handoff)
        self.assertEqual(len(result.handoff.alternatives), 2)
        # JSON-safe report contains the ambiguity and per-beat rows.
        self.assertEqual(len(serialized["rows"]), 4)
        statuses = {event["status"] for event in result.events}
        self.assertIn("delivered", statuses)
        self.assertIn("misclaim_blocked", statuses)
        self.assertIn("duplicate_delivery_blocked", statuses)


class RackInvariants(unittest.TestCase):
    def test_occupied_slot_cannot_be_promised_twice(self):
        rack = RackBank({"PA": ["PA-01", "PA-02"]})
        first = rack.arrive(0, "PA", 1, "G1")
        self.assertEqual((first.slot, first.status), ("PA-01", "racked"))
        denied = rack.reserve(0, "PA-01", 9, "G9", 0)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.status, "slot_occupied_denied")

        winner, losers = rack.contest_same_slot(
            1, "PA-02", [(2, "LATE", 5), (3, "URGENT", 1)]
        )
        self.assertEqual(winner.bag, 3)
        self.assertEqual(len(losers), 1)
        self.assertFalse(losers[0].allowed)

    def test_ticket_rejects_duplicate_bag_number(self):
        with self.assertRaises(Exception) as ctx:
            GroupTicket("BAD", "someone", (1, 1, 2), depart_beat=0)
        self.assertEqual(ctx.exception.code, "duplicate_ticket_bag")


class EndToEndReport(unittest.TestCase):
    def test_run_demo_creates_rerun_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_demo(tmp)
            root = Path(tmp)
            self.assertTrue((root / "baggage_catalog.json").exists())
            self.assertTrue((root / "baggage_report.md").exists())
            self.assertTrue((root / "baggage_report.json").exists())
            self.assertTrue((root / "baggage.db").exists())
            self.assertEqual(report["warehouse_counts"]["total"], 28)
            self.assertEqual(report["warehouse_counts"]["rejected"], 0)
            self.assertEqual(len(report["stored_frames"]), 28)
            self.assertTrue(all(frame["product_sha256"] == report["product_sha256"]
                                for frame in report["stored_frames"]))
            by_id = {frame["frame_id"] for frame in report["stored_frames"]}
            self.assertEqual(by_id, {1, 2, 3})


if __name__ == "__main__":
    unittest.main()
