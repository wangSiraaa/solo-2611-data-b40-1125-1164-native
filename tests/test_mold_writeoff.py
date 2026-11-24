from __future__ import annotations

import unittest

from weighbridge import Catalog, MoldWriteOffLedger, Warehouse
from weighbridge.molds import ISSUED, WITHHELD
from demo.mold_demo import WORK, build_on_site_case, build_sealed_case


class MoldScenarioTests(unittest.TestCase):
    def test_baseline_unique_a_issues_receipt_and_traces_to_evidence(self):
        w, ledger, _ = build_sealed_case()
        receipt = ledger.issue_receipt("RCPT-BASE", WORK)
        self.assertEqual(receipt.status, ISSUED)
        verdict = receipt.verdict
        self.assertEqual(verdict["certain_destroyed_ids"], ["A1"])
        self.assertEqual(verdict["remaining_molds"], [])
        self.assertGreaterEqual(verdict["feasible_worlds"], 2)

        trace = ledger.trace_receipt("RCPT-BASE")
        lookup = trace["reverse_lookup"]
        self.assertEqual(lookup["trip_ids"], ["TRIP0001"])
        self.assertIn("WB-IN", lookup["ticket_ids"])
        self.assertIn("WB-OUT", lookup["ticket_ids"])
        self.assertEqual(set(lookup["seal_ids"]), {"SEAL-A-1", "SEAL-B-2"})
        self.assertEqual(lookup["crush_batch_ids"], ["CRUSH-1"])
        self.assertIn("TRIP0001", trace["weighing_evidence"])
        self.assertIn("explain_trip", trace["weighing_evidence"]["TRIP0001"])
        w.close()

    def test_generic_left_label_is_not_a_serial_but_one_b_is_not_enough(self):
        _, ledger, _ = build_sealed_case()
        verdict = ledger.evaluate_work(WORK)
        # 两个重量都相容的可能世界：B1 或 B2 被封箱破碎，但不能把标签直接当序列号。
        self.assertIn("B1", verdict.possible_destroyed_ids)
        self.assertIn("B2", verdict.possible_destroyed_ids)
        self.assertNotIn("B1", verdict.certain_destroyed_ids)
        self.assertNotIn("B2", verdict.certain_destroyed_ids)

        # 只剩一个乙被确认破碎、唯一甲仍在时，绝不能销账。
        w = Warehouse(Catalog())
        lx = w.mold_writeoffs()
        lx.add_piece("A1", "WX", "A", weight=10)
        lx.add_piece("B1", "WX", "B", weight=10)
        lx.add_piece("B2", "WX", "B", weight=11)
        lx.add_mold("X1", "WX", slots={"A": {"role": "A"}, "B": {"role": "B"}})
        lx.seal_box("BB", "S1", tare=1, at_workshop=True)
        lx.pack_line("BB", label="左瓣", role="B", work_id="WX")
        lx.attach_shipment("T1", ["BB"], net=11)
        lx.add_crush_batch("C1", crushed_at=2, box_ids=["BB"],
                           seal_readings={"BB": "S1"}, slag_weight=(8, 12))
        r = lx.issue_receipt("R", "WX")
        self.assertEqual(r.status, WITHHELD)
        rem = r.verdict["remaining_molds"]
        self.assertTrue(rem)
        self.assertEqual(rem[0]["mold_id"], "X1")

    def test_shared_base_destruction_disables_both_molds(self):
        lx = MoldWriteOffLedger()
        for pid, role, weight in [
            ("A1", "A", 10), ("B1", "B", 10), ("B2", "B", 10), ("BASE", "BASE", 5)
        ]:
            lx.add_piece(pid, "W", role, weight=weight)
        lx.add_mold("M1", "W", required_piece_ids=["A1", "B1", "BASE"])
        lx.add_mold("M2", "W", required_piece_ids=["A1", "B2", "BASE"])
        lx.add_witness("PW", "装箱见证", kind="packing")
        lx.seal_box("BASE-BOX", "SEAL-BASE", tare=1, packing_witness_id="PW")
        lx.pack_line("BASE-BOX", identified_piece_id="BASE", identity_basis="serial")
        lx.attach_shipment("T", ["BASE-BOX"], net=6)
        lx.add_crush_batch("C", crushed_at=3, box_ids=["BASE-BOX"],
                           seal_readings={"BASE-BOX": "SEAL-BASE"}, slag_weight=(4, 6))
        verdict = lx.evaluate_work("W")
        self.assertTrue(verdict.issuable, verdict.issues)
        self.assertEqual(verdict.remaining_molds, ())

    def test_single_seal_evidence_change_flips_receipt(self):
        _, good, _ = build_sealed_case()
        self.assertEqual(good.issue_receipt("R1", WORK).status, ISSUED)
        _, bad, _ = build_sealed_case("seal")
        receipt = bad.issue_receipt("R2", WORK)
        self.assertEqual(receipt.status, WITHHELD)
        self.assertTrue(any("seal mismatch" in x for x in receipt.verdict["issues"]))
        self.assertEqual({x["mold_id"] for x in receipt.verdict["remaining_molds"]}, {"M1", "M2"})

    def test_single_weigh_ticket_change_flips_receipt(self):
        _, bad, _ = build_sealed_case("weigh_ticket")
        receipt = bad.issue_receipt("R", WORK)
        self.assertEqual(receipt.status, WITHHELD)
        self.assertTrue(any("does not intersect" in x for x in receipt.verdict["issues"]))

    def test_single_workshop_sealable_change_flips_receipt(self):
        _, bad, _ = build_sealed_case("workshop_piece")
        receipt = bad.issue_receipt("R", WORK)
        self.assertEqual(receipt.status, WITHHELD)
        self.assertTrue(any("no possible" in x for x in receipt.verdict["issues"]))

    def test_single_witness_window_change_flips_on_site_receipt(self):
        _, good, _ = build_on_site_case()
        self.assertEqual(good.issue_receipt("R1", WORK).status, ISSUED)
        _, bad, _ = build_on_site_case("witness_window")
        receipt = bad.issue_receipt("R2", WORK)
        self.assertEqual(receipt.status, WITHHELD)
        self.assertTrue(any("outside witness window" in x for x in receipt.verdict["issues"]))
        self.assertEqual(len(receipt.verdict["remaining_molds"]), 2)

    def test_identity_recorded_after_crush_cannot_prove_serial(self):
        w = Warehouse(Catalog())
        lx = w.mold_writeoffs()
        lx.add_piece("A1", "W", "A", weight=10)
        lx.add_mold("M", "W", required_piece_ids=["A1"])
        lx.seal_box("B", "S", tare=0)
        lx.add_witness("Z", valid_from=0, valid_to=100)
        lx.add_opening("O", "B", witness_id="Z", opened_at=80, piece_ids=["A1"], batch_id="C")
        lx.add_crush_batch("C", crushed_at=70, opening_ids=["O"])
        verdict = lx.evaluate_work("W")
        self.assertFalse(verdict.issuable)
        self.assertTrue(any("after crushing" in x for x in verdict.issues))

    def test_signature_is_limited_to_boxes_actually_seen(self):
        lx = MoldWriteOffLedger()
        lx.add_witness("W", valid_from=0, valid_to=10)
        lx.seal_box("B1", "S1", tare=0)
        lx.seal_box("B2", "S2", tare=0)
        with self.assertRaises(Exception):
            lx.sign_boxes("SIG", "W", ["B2"], signed_at=5)
        lx.witness_sees_box("W", "B1")
        sig = lx.sign_boxes("SIG", "W", ["B1"], signed_at=5)
        self.assertEqual(sig["box_ids"], ["B1"])

    def test_unconfirmed_trip_is_not_actual_shipment_evidence(self):
        c = Catalog()
        c.define_mass("tractor", "T", 10, 10)
        c.define_mass("trailer", "R", 1, 1)
        seq = c.apply_attach("T", "R")
        w = Warehouse(c)
        w.add_ticket(dict(ticket_id="IN", tractor="T", catalog_seq=seq, ts=1,
                          direction="IN", gross=11))
        w.add_ticket(dict(ticket_id="OUT", tractor="T", catalog_seq=seq, ts=2,
                          direction="OUT", gross=22))
        w.preview("LOAD", "IN", "OUT")  # DRAFT only
        lx = w.mold_writeoffs()
        lx.add_piece("A1", "W", "A", weight=10)
        lx.add_mold("M", "W", required_piece_ids=["A1"])
        lx.seal_box("B", "S", tare=1)
        lx.pack_line("B", identified_piece_id="A1")
        lx.attach_shipment("TRIP0001", ["B"])
        verdict = lx.evaluate_work("W")
        self.assertFalse(verdict.issuable)
        self.assertTrue(any("not CONFIRMED" in x for x in verdict.issues))

    def test_vehicle_weight_reference_cannot_be_used_as_box_tare_twice(self):
        w = Warehouse(Catalog())
        lx = w.mold_writeoffs()
        lx.add_piece("A1", "W", "A", weight=10)
        lx.add_mold("M", "W", required_piece_ids=["A1"])
        lx.seal_box("B1", "S1", tare=1, tare_ref="VEHICLE-T1")
        lx.attach_shipment("T1", ["B1"], net=11, vehicle_deduction_ref="VEHICLE-T1")
        verdict = lx.evaluate_work("W")
        self.assertFalse(verdict.issuable)
        self.assertTrue(any("deducted twice" in x for x in verdict.issues))


if __name__ == "__main__":
    unittest.main()
