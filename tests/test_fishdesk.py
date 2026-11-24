"""Tests for the ornamental-fish boarding desk.

The acceptance arc follows the task statement:

* named tail-spot evidence identifies the white-tailed JIA fish in A;
* replacing that record with "unseen which fish entered A" makes pickup wait;
* restoring the record but revoking the A mandate still prevents handover;
* ambiguous same-spot scoops retain all possible group histories;
* finite net slots or a missing observation window change which orders can be
  accepted;
* receipts are unique per fish and handover is a sealed opaque box.
"""

from __future__ import annotations

import unittest

from solo.codec import encode_text
from solo.fishdesk import (
    MAX_FISH,
    MAX_TANKS,
    MAX_TRANSFERS,
    DuplicateReceiptFish,
    FrontDesk,
    LedgerBuilder,
    LedgerParseError,
    NetRack,
    FishDeskError,
    PickupCase,
    PickupBlocked,
    ReceiptBook,
    RevisionLedger,
    canonical_ledger_text,
    parse_ledger,
    slot_pressure_ledger_text,
    slot_report,
)


def canonical_desk(*, revoke: bool = False, used: bool = False) -> FrontDesk:
    desk = FrontDesk(available_lots={"A", "B"})
    desk.receipts.issue("R-JIA", "F1", "JIA")
    desk.receipts.issue("R-YI", "F2", "YI")
    desk.receipts.return_receipt("R-JIA")
    desk.receipts.return_receipt("R-YI")
    if used:
        desk.receipts.mark_used("R-JIA")
    desk.mandates.grant("M-A", "JIA", "WANG", "A")
    if revoke:
        desk.mandates.revoke("M-A")
    return desk


def jia_case() -> PickupCase:
    return PickupCase("O-JIA", "JIA", ("R-JIA",), "A", holder="WANG", mandate_id="M-A")


class CanonicalAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = canonical_ledger_text()
        self.ledger = RevisionLedger.from_text(self.text)

    def test_pa_note_is_real_pair_but_ascii_records_span_to_bytes(self):
        version = self.ledger.version(0)
        doc = version.decode()
        self.assertIn("一丁丂", doc.text)
        encoded = encode_text(self.text).data
        self.assertIn(b"\xff\x01", encoded)  # switched into PAIR for annotation

        parsed = self.ledger.parse(0)
        witness = parsed.find_line_containing("WITNESS E-SPLIT")
        self.assertEqual(doc.text[witness.start_char : witness.end_char], witness.text)
        self.assertEqual(
            version.body[witness.start_byte : witness.end_byte],
            witness.text.encode("ascii"),
        )
        self.assertGreaterEqual(witness.end_byte, witness.start_byte)

    def test_named_white_tail_record_makes_jia_pickup_a_deliverable(self):
        replay = self.ledger.replay(0)
        self.assertTrue(replay.viable)
        self.assertEqual(len(replay.worlds), 1)
        report = replay.identity_report()
        self.assertEqual(report.certain_locations["F1"].tank, "A")
        self.assertIsNone(report.certain_locations["F1"].unit)
        self.assertEqual(report.certain_locations["F2"].tank, "B")

        desk = canonical_desk()
        decision = desk.attempt(jia_case(), replay)
        self.assertTrue(decision.allowed, decision.reason)
        self.assertEqual(decision.household, "JIA")
        self.assertEqual(decision.fish_ids, ("F1",))
        self.assertTrue(decision.box_id)
        self.assertIn("R-JIA", {r.receipt_id for r in desk.receipts.all() if r.used})
        self.assertIn("F1", desk.delivered_fish)

        handover = desk.deliveries[0]
        self.assertTrue(handover.box.sealed)
        with self.assertRaises(Exception):
            handover.box.open_at_front_desk()

    def test_two_receipts_cannot_point_to_same_fish(self):
        receipts = ReceiptBook()
        receipts.issue("R1", "F1", "JIA")
        with self.assertRaises(DuplicateReceiptFish):
            receipts.issue("R2", "F1", "JIA")

    def test_used_receipt_cannot_write_off_twice(self):
        replay = self.ledger.replay(0)
        desk = canonical_desk(used=True)
        decision = desk.evaluate(jia_case(), replay)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "RECEIPT_ALREADY_WRITTEN_OFF")
        with self.assertRaises(PickupBlocked):
            desk.settle(jia_case(), replay)

    def test_unseen_erratum_keeps_two_identity_histories_and_blocks_pickup(self):
        old_witness = self.ledger.parse(0).find_line_containing("WITNESS E-SPLIT")
        erratum = "# ERRATUM_UNSEEN which fish entered A"
        new_version, changed_line = self.ledger.replace_line_containing(
            "WITNESS E-SPLIT", erratum
        )
        self.assertEqual(changed_line.text, old_witness.text)
        self.assertEqual(new_version.parent_version, 0)

        # The corrected version is independently decoded through Version.decode.
        corrected = self.ledger.parse(new_version.version_id)
        self.assertIn("ERRATUM_UNSEEN", corrected.text)
        replay = self.ledger.replay(new_version.version_id)
        self.assertEqual(len(replay.worlds), 2)
        report = replay.identity_report()
        self.assertNotIn("F1", report.certain_locations)
        self.assertEqual(len(report.fog_groups), 1)
        self.assertEqual(set(report.fog_groups[0].fish_ids), {"F1", "F2"})
        self.assertEqual(len(report.fog_groups[0].assignments), 2)

        desk = canonical_desk()
        decision = desk.evaluate(jia_case(), replay)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "IDENTITY_MIXED")
        self.assertEqual(desk.deliveries, [])

    def test_inverse_erratum_restores_record_and_identity(self):
        erratum = "# ERRATUM_UNSEEN which fish entered A"
        original_line = self.ledger.parse(0).find_line_containing("WITNESS E-SPLIT").text
        v1, _ = self.ledger.replace_line_containing("WITNESS E-SPLIT", erratum)
        self.assertEqual(len(self.ledger.replay(v1.version_id).worlds), 2)

        v2, patched = self.ledger.replace_line_containing("ERRATUM_UNSEEN", original_line)
        self.assertIn("ERRATUM_UNSEEN", patched.text)
        self.assertEqual(v2.parent_version, v1.version_id)
        self.assertEqual(len(self.ledger.replay(v2.version_id).worlds), 1)

    def test_restored_ledger_but_revoked_mandate_still_blocks(self):
        # Original immutable version remains available and replays uniquely.
        replay = self.ledger.replay(0)
        self.assertEqual(len(replay.worlds), 1)
        desk = canonical_desk(revoke=True)
        decision = desk.evaluate(jia_case(), replay)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "MANDATE_REVOKED")


class AmbiguousGroupHistoryTests(unittest.TestCase):
    def test_converging_merge_keeps_distinct_earlier_group_paths(self):
        b = LedgerBuilder("CONVERGE")
        b.fish("F1", "O1", "WHITE", "A", "09:00")
        b.fish("F2", "O2", "WHITE", "A", "09:01")
        b.scoop("E1", 1, "A", "B", "10:00", spot="WHITE")
        # Both possible worlds converge when A and B are merged into C, but
        # the ledger must retain which fish could have travelled through B.
        b.merge("E2", ["A", "B"], "C", "11:00")
        b.count("C", 2, "11:05")
        replay = RevisionLedger.from_text(b.render()).replay()
        self.assertEqual(len(replay.worlds), 2)
        first_scoop = {
            next(iter(w.find_move("E1").fish_ids)) for w in replay.worlds
        }
        self.assertEqual(first_scoop, {"F1", "F2"})
        self.assertTrue(all(len(w.moves) == 2 for w in replay.worlds))

    def test_two_of_three_same_spot_scoop_keeps_all_group_histories(self):
        b = LedgerBuilder("AMBIG")
        for i in range(1, 4):
            b.fish(f"F{i}", f"O{i}", "WHITE", "A", f"09:0{i}")
        # Exactly two same-spot fish are scooped; only destination B is written.
        b.scoop("E-SCOOP", 2, "A", "B", "10:00", spot="WHITE")
        b.count("B", 2, "10:05")
        ledger = RevisionLedger.from_text(b.render())
        replay = ledger.replay()
        self.assertEqual(len(replay.worlds), 3)
        report = replay.identity_report()
        self.assertGreater(len(report.possible_locations["F1"]), 1)

        # A clearly read fish number at the source excludes two histories.
        b.see(["F1"], "A", "10:06")
        narrowed = RevisionLedger.from_text(b.render()).replay()
        self.assertEqual(len(narrowed.worlds), 1)
        final = narrowed.worlds[0]
        self.assertEqual(final.loc("F1").tank, "A")
        self.assertEqual({final.loc("F2").tank, final.loc("F3").tank}, {"B"})

        # A wrong point count excludes every remaining history.
        b.count("B", 3, "10:07")
        contradictory = RevisionLedger.from_text(b.render()).replay()
        self.assertFalse(contradictory.viable)
        self.assertTrue(any("COUNT B=3" in reason for reason in contradictory.contradictions))


class CompartmentSlotTests(unittest.TestCase):
    def _desk(self, extra_nets: bool) -> FrontDesk:
        lots = {"N1", "N2"}
        if extra_nets:
            lots.update({"N3", "N4"})
        else:
            lots.add("OPEN:C")
        desk = FrontDesk(available_lots=lots)
        targets = [
            ("R1", "F1", "JIA"),
            ("R2", "F2", "YI"),
            ("R3", "F3", "BING"),
            ("R4", "F4", "DING"),
        ]
        for rid, fid, owner in targets:
            desk.receipts.issue(rid, fid, owner)
            desk.receipts.return_receipt(rid)
        return desk

    def test_tight_slots_accept_private_nets_but_hold_shared_pair(self):
        text = slot_pressure_ledger_text(extra_nets=False)
        ledger = RevisionLedger.from_text(text)
        replay = ledger.replay()
        self.assertTrue(replay.viable, replay.contradictions)

        slots = {s.tank: s for s in slot_report(ledger.parse())}
        self.assertEqual(slots["C"].capacity, 2)
        self.assertEqual(slots["C"].peak, 2)
        self.assertEqual(slots["C"].peak_free, 0)
        self.assertEqual(slots["C"].free, 2)
        self.assertEqual(slots["C"].violations, ())

        desk = self._desk(False)
        ok_jia = desk.evaluate(
            PickupCase("O1", "JIA", ("R1",), "N1", holder="JIA"), replay
        )
        ok_yi = desk.evaluate(
            PickupCase("O2", "YI", ("R2",), "N2", holder="YI"), replay
        )
        self.assertTrue(ok_jia.allowed, ok_jia.reason)
        self.assertTrue(ok_yi.allowed, ok_yi.reason)

        bing = desk.evaluate(
            PickupCase("O3", "BING", ("R3",), "OPEN:C", holder="BING"), replay
        )
        ding = desk.evaluate(
            PickupCase("O4", "DING", ("R4",), "OPEN:C", holder="DING"), replay
        )
        self.assertFalse(bing.allowed)
        self.assertEqual(bing.reason_code, "IDENTITY_MIXED")
        self.assertFalse(ding.allowed)
        self.assertEqual(ding.reason_code, "IDENTITY_MIXED")

    def test_extra_slots_make_all_four_orders_acceptable(self):
        text = slot_pressure_ledger_text(extra_nets=True)
        ledger = RevisionLedger.from_text(text)
        replay = ledger.replay()
        self.assertTrue(replay.viable, replay.contradictions)
        desk = self._desk(True)
        for i, (owner, net) in enumerate(
            [("JIA", "N1"), ("YI", "N2"), ("BING", "N3"), ("DING", "N4")], start=1
        ):
            decision = desk.evaluate(
                PickupCase(f"O{i}", owner, (f"R{i}",), net, holder=owner), replay
            )
            self.assertTrue(decision.allowed, decision.reason)

    def test_over_capacity_net_record_is_reported(self):
        b = LedgerBuilder("OVER-SLOT")
        b.tank("C", 1)
        b.net("N1", "C")
        b.net("N2", "C")
        b.fish("F1", "JIA", "WHITE", "C", "09:00", net="N1")
        b.fish("F2", "YI", "PLAIN", "C", "09:01", net="N2")
        parsed = parse_ledger(b.render())
        c_slot = next(s for s in slot_report(parsed) if s.tank == "C")
        self.assertTrue(any("CAP 1" in v for v in c_slot.violations))

    def test_live_net_rack_rejects_third_slot_and_atomic_shift_checks_destination(self):
        rack = NetRack({"C": 2, "A": 1})
        rack.reserve("N1", "C")
        rack.reserve("N2", "C")
        rack.reserve("N3", "A")
        with self.assertRaises(FishDeskError):
            rack.reserve("N4", "C")
        # Moving N1 to full A fails and leaves its location unchanged.
        with self.assertRaises(FishDeskError):
            rack.shift("N1", "A", expected_source="C")
        self.assertEqual(rack.net_tank["N1"], "C")
        rack.set_capacity("A", 2)
        rack.shift("N1", "A", expected_source="C")
        self.assertEqual(rack.net_tank["N1"], "A")
        self.assertEqual(rack.free("C"), 1)


class ObservationWindowTests(unittest.TestCase):
    def _ledger(self, gap: bool) -> RevisionLedger:
        b = LedgerBuilder("WINDOW")
        b.fish("F1", "JIA", "WHITE", "A", "09:00")
        b.fish("F2", "YI", "PLAIN", "B", "09:01")
        b.fish("F3", "BING", "PLAIN", "B", "09:02")
        b.merge("M1", ["A", "B"], "C", "10:00")
        b.split("S1", "C", {"A": 1, "B": 2}, "11:00")
        if gap:
            b.window_gap("S1", "IN", "11:01", dst="A")
        else:
            b.window("S1", "IN", ["WHITE"], "11:01", dst="A", complete=True)
        return RevisionLedger.from_text(b.render())

    def test_complete_window_resolves_and_gap_holds_order(self):
        complete = self._ledger(False).replay()
        self.assertEqual(len(complete.worlds), 1)
        self.assertEqual(complete.identity_report().certain_locations["F1"].tank, "A")

        gap_ledger = self._ledger(True)
        gap = gap_ledger.replay()
        self.assertEqual(len(gap.worlds), 3)
        desk = FrontDesk(available_lots={"A"})
        desk.receipts.issue("R1", "F1", "JIA")
        desk.receipts.return_receipt("R1")
        allowed = desk.evaluate(
            PickupCase("O1", "JIA", ("R1",), "A", holder="JIA"), complete
        )
        blocked = desk.evaluate(
            PickupCase("O1", "JIA", ("R1",), "A", holder="JIA"), gap
        )
        self.assertTrue(allowed.allowed, allowed.reason)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason_code, "IDENTITY_MIXED")


class LimitTests(unittest.TestCase):
    def test_max_fish_tanks_and_transfers_are_enforced(self):
        b = LedgerBuilder("TOO-MANY-FISH")
        for i in range(MAX_FISH + 1):
            b.fish(f"F{i}", f"O{i}", "WHITE", "A", f"09:{i:02d}")
        with self.assertRaises(LedgerParseError):
            parse_ledger(b.render())

        b = LedgerBuilder("TOO-MANY-TANKS")
        for i, tank in enumerate(["A", "B", "C", "D"][: MAX_TANKS + 1]):
            b.fish(f"F{i}", f"O{i}", "WHITE", tank, f"09:{i:02d}")
        with self.assertRaises(LedgerParseError):
            parse_ledger(b.render())

        b = LedgerBuilder("TOO-MANY-MOVES")
        b.fish("F1", "JIA", "WHITE", "A", "09:00")
        tanks = ["A", "B", "C"]
        for i in range(MAX_TRANSFERS + 1):
            src = tanks[i % 2]
            dst = tanks[(i + 1) % 2]
            b.raw(f"MOVE F1 FROM {src} TO {dst} ID M{i} AT {10+i:02d}:00")
        with self.assertRaises(LedgerParseError):
            parse_ledger(b.render())


if __name__ == "__main__":
    unittest.main()
