"""Nib-grinding workshop tests.

The width model is re-derived independently through ``basket_service.reference``
so the production geometry helpers are never the source of the expected answer.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from basket_service import open_service
from basket_service.errors import BasketError
from basket_service.nib import H, V, SIDE, STRAIGHT, shape_of
from basket_service.reference import nib_best_kind_for_pairs, nib_shape

CAND_N = [{"kind": STRAIGHT, "base": 2}, {"kind": SIDE, "base": 3, "side_wide": V}]
CAND_KNOWN = [
    {"kind": STRAIGHT, "base": 2},
    {"kind": SIDE, "base": 2, "side_wide": H},
    {"kind": SIDE, "base": 2, "side_wide": V},
]


def make_sale(svc, sale_id, unit_ids):
    basket = [{"unit_id": uid, "sku": "NIB", "price": 100} for uid in unit_ids]
    snap = svc.publish_snapshot([{"sku": "NIB", "price": 100}], [], snapshot_id=f"snap-{sale_id}")
    quote = svc.quote(basket, [], f"q-{sale_id}", snap)
    return svc.register_sale(sale_id, quote=quote)


def setup(unit_ids=("u",), sale_id="S", cands=CAND_KNOWN, hand="RIGHT", **nib_kwargs):
    svc = open_service(":memory:")
    make_sale(svc, sale_id, unit_ids)
    kw = dict(owner_id="owner", candidates=cands, hand=hand, allowed_h=[1, 5], allowed_v=[1, 5], return_deadline=5)
    kw.update(nib_kwargs)
    svc.register_nib(sale_id, unit_ids[0], **kw)
    return svc


class GeometryTests(unittest.TestCase):
    def test_reference_and_service_agree(self):
        for kind, base, side_wide in [
            (STRAIGHT, 2, None),
            (STRAIGHT, 4, None),
            (SIDE, 3, H),
            (SIDE, 3, V),
            (SIDE, 2, H),
        ]:
            cand = {"kind": kind, "base": base, "side_wide": side_wide}
            for hand in ("RIGHT", "LEFT"):
                for grinds in range(0, 4):
                    try:
                        ref = nib_shape(kind, base, hand, side_wide, grinds)
                    except ValueError:
                        ref = None
                    self.assertEqual(shape_of(cand, hand, grinds), ref, (kind, base, side_wide, hand, grinds))

    def test_straight_same_both_directions(self):
        self.assertEqual(shape_of({"kind": STRAIGHT, "base": 2}, "RIGHT", 0), {"H": 2, "V": 2})
        self.assertEqual(shape_of({"kind": STRAIGHT, "base": 2}, "LEFT", 0), {"H": 2, "V": 2})

    def test_side_is_wide_one_narrow_one_and_swaps_with_hand(self):
        right = shape_of({"kind": SIDE, "base": 2, "side_wide": H}, "RIGHT", 0)
        self.assertEqual(right, {"H": 3, "V": 1})
        left = shape_of({"kind": SIDE, "base": 2, "side_wide": H}, "LEFT", 0)
        self.assertEqual(left, {"H": 1, "V": 3})

    def test_grind_reduces_base_for_whole_piece(self):
        self.assertEqual(shape_of({"kind": STRAIGHT, "base": 3}, "RIGHT", 1), {"H": 2, "V": 2})
        # Side base-4 V-wide after two grinds: base 2 -> V=3, H=1 (both dropped).
        self.assertEqual(shape_of({"kind": SIDE, "base": 4, "side_wide": V}, "RIGHT", 2), {"V": 3, "H": 1})

    def test_grind_cannot_go_below_one_or_side_below_two(self):
        self.assertIsNone(shape_of({"kind": STRAIGHT, "base": 1}, "RIGHT", 1))
        self.assertIsNone(shape_of({"kind": SIDE, "base": 2}, "RIGHT", 1))

    def test_n_piece_both_shapes_drop_by_one_each_grind(self):
        # (2,2) and (2,4) at grind 0 -> (1,1) and (1,3) at grind 1, per the brief.
        straight = {"kind": STRAIGHT, "base": 2}
        side = {"kind": SIDE, "base": 3, "side_wide": V}
        self.assertEqual((shape_of(straight, "RIGHT", 0)["H"], shape_of(straight, "RIGHT", 0)["V"]), (2, 2))
        self.assertEqual((shape_of(side, "RIGHT", 0)["H"], shape_of(side, "RIGHT", 0)["V"]), (2, 4))
        self.assertEqual((shape_of(straight, "RIGHT", 1)["H"], shape_of(straight, "RIGHT", 1)["V"]), (1, 1))
        self.assertEqual((shape_of(side, "RIGHT", 1)["H"], shape_of(side, "RIGHT", 1)["V"]), (1, 3))

    def test_min_grinds_is_per_hypothesis(self):
        # For "both directions 1": straight needs 1 grind, the reverse side nib
        # can never reach V=1 (its V stays wide), which is why an urgent
        # cross-type guarantee must rely on the intersection of shapes.
        from basket_service.nib import min_grinds_for_candidate, guarantee_grinds

        straight = {"kind": STRAIGHT, "base": 2}
        side = {"kind": SIDE, "base": 3, "side_wide": V}
        req = {"H": (1, 1), "V": (1, 1)}
        self.assertEqual(min_grinds_for_candidate(straight, "RIGHT", 0, req, 5), 1)
        self.assertIsNone(min_grinds_for_candidate(side, "RIGHT", 0, req, 5))
        self.assertIsNone(guarantee_grinds([straight, side], "RIGHT", 0, req, 5))
        # But the wider vertical band (1..3) is guaranteeable for both at 1 grind.
        req2 = {"H": (1, 1), "V": (1, 3)}
        self.assertEqual(guarantee_grinds([straight, side], "RIGHT", 0, req2, 5), 1)


class EvidenceTests(unittest.TestCase):
    def test_n_candidates_shapes_are_2_2_and_2_4(self):
        shapes = sorted((shape_of(c, "RIGHT", 0)["H"], shape_of(c, "RIGHT", 0)["V"]) for c in CAND_N)
        self.assertEqual(shapes, [(2, 2), (2, 4)])

    def test_known_base2_h2_leaves_only_straight(self):
        svc = setup()
        result = svc.record_trial("S", "u", hand="RIGHT", direction=H, width=2)
        expected = nib_best_kind_for_pairs([("RIGHT", H, 2, 0)], [(c["kind"], c["base"], c.get("side_wide")) for c in CAND_KNOWN])
        self.assertEqual(result["surviving"], expected)
        self.assertEqual([c["kind"] for c in result["surviving"]], ["STRAIGHT"])

    def test_known_base2_h3_leaves_only_side(self):
        svc = setup()
        result = svc.record_trial("S", "u", hand="RIGHT", direction=H, width=3)
        self.assertEqual([c["kind"] for c in result["surviving"]], ["SIDE"])
        self.assertEqual(result["surviving"][0]["side_wide"], H)

    def test_contradictory_trial_rolls_back(self):
        svc = setup(initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        before = svc.get_nib("S", "u")
        with self.assertRaises(BasketError) as ctx:
            svc.record_trial("S", "u", hand="RIGHT", direction=H, width=3)
        self.assertEqual(ctx.exception.code, "NIB_EVIDENCE_MISMATCH")
        self.assertEqual(svc.get_nib("S", "u")["surviving"], before["surviving"])

    def test_trial_tracks_grind_level(self):
        svc = setup(cands=CAND_N, allowed_h=[1, 4], allowed_v=[1, 5])
        # Side base-3 V-wide at grind 0 reads V=4; after one grind it reads V=3.
        svc.record_trial("S", "u", hand="RIGHT", direction=V, width=4)
        self.assertEqual([c["kind"] for c in svc.get_nib("S", "u")["surviving"]], [SIDE])
        # Grinding the piece changes what an old measurement means; we only add
        # new measurements at the *current* grind level, so survivors stay sound.


class PregrindOrderTests(unittest.TestCase):
    def test_urgent_guarantee_across_types(self):
        svc = setup(cands=CAND_N, allowed_h=[1, 3], allowed_v=[1, 4],
                    initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        order = svc.open_nib_order(
            "O", "S", "u", strategy="PREGRIND",
            requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 3]},
            deadline_shift=1, opened_shift=1, claimant_id="owner",
        )
        self.assertEqual(order["status"], "SIGNED")
        self.assertEqual(order["decision"]["plan"]["extra_grinds"], 1)
        self.assertTrue(order["decision"]["plan"]["guaranteed_shapes"])

    def test_known_straight_both1_one_slot_delivered(self):
        svc = setup(initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        svc.open_nib_order("O", "S", "u", strategy="PREGRIND",
                           requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 1]},
                           deadline_shift=1, opened_shift=1, claimant_id="owner")
        svc.grind_nib("O", shift_no=1)
        delivered = svc.deliver_nib("O", shift_no=1, measured_h=1, measured_v=1)
        self.assertEqual(delivered["status"], "DELIVERED")
        self.assertEqual((delivered["delivery"]["measured_h"], delivered["delivery"]["measured_v"]), (1, 1))
        self.assertFalse(delivered["delivery"]["evidence"]["original_width_available"])

    def test_known_side_cannot_reach_both1(self):
        svc = setup(initial_trials=[{"hand": "RIGHT", "direction": H, "width": 3}])
        order = svc.open_nib_order("O", "S", "u", strategy="PREGRIND",
                                   requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 1]},
                                   deadline_shift=2, opened_shift=1, claimant_id="owner")
        self.assertEqual(order["status"], "HUNG")

    def test_slot_taken_blocks_otherwise_possible_order(self):
        svc = setup(initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        svc.reserve_slot(1, "LOCKED", "GRIND")
        order = svc.open_nib_order("O", "S", "u", strategy="PREGRIND",
                                   requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 1]},
                                   deadline_shift=1, opened_shift=1, claimant_id="owner")
        self.assertEqual(order["status"], "HUNG")

    def test_grind_is_irreversible_and_persisted(self):
        svc = setup(cands=CAND_N, allowed_h=[1, 3], allowed_v=[1, 4],
                    initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        svc.open_nib_order("O", "S", "u", strategy="PREGRIND",
                           requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 3]},
                           deadline_shift=1, opened_shift=1, claimant_id="owner")
        svc.grind_nib("O", shift_no=1)
        nib = svc.get_nib("S", "u")
        self.assertEqual(nib["grinds_done"], 1)
        self.assertFalse(nib["original_width_available"])
        with self.assertRaises(BasketError):
            svc.grind_nib("O", shift_no=2)  # only one planned grind; also slot 2 conflicts


class RemeasureOrderTests(unittest.TestCase):
    def _open(self, svc, deadline=2):
        return svc.open_nib_order(
            "O", "S", "u", strategy="REMEASURE",
            requirement={"hand": "RIGHT", "h": [2, 2], "v": [2, 2]},
            deadline_shift=deadline, opened_shift=1, claimant_id="owner",
        )

    def test_ambiguous_requires_remeasure(self):
        svc = setup(cands=CAND_N, allowed_h=[1, 4], allowed_v=[1, 5],
                    initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        order = self._open(svc)
        self.assertEqual(order["status"], "AWAITING_REMEASURE")

    def test_read2_signs_and_keeps_original_width(self):
        svc = setup(cands=CAND_N, allowed_h=[1, 4], allowed_v=[1, 5],
                    initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        self._open(svc)
        result = svc.remeasure_nib("O", shift_no=1, h_width=2, v_width=2)
        self.assertEqual(result["status"], "SIGNED")
        delivered = svc.deliver_nib("O", shift_no=2, measured_h=2, measured_v=2)
        self.assertEqual(delivered["status"], "DELIVERED")
        self.assertTrue(delivered["delivery"]["evidence"]["original_width_available"])
        self.assertEqual(svc.get_nib("S", "u")["grinds_done"], 0)

    def test_read4_refused_for_as_is_request(self):
        svc = setup(cands=CAND_N, allowed_h=[1, 4], allowed_v=[1, 5],
                    initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        self._open(svc)
        result = svc.remeasure_nib("O", shift_no=1, h_width=2, v_width=4)
        self.assertEqual(result["status"], "REFUSED")

    def test_remeasure_takes_a_shift(self):
        svc = setup(cands=CAND_N, allowed_h=[1, 4], allowed_v=[1, 5],
                    initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        # Another locked order holds shift 1; the remeasure order takes shift 2.
        svc.reserve_slot(1, "OTHER", "GRIND")
        order = self._open(svc, deadline=2)
        self.assertEqual(order["decision"]["plan"]["remeasure_shift"], 2)
        # Trying to remeasure in the occupied shift must be rejected.
        with self.assertRaises(BasketError) as ctx:
            svc.remeasure_nib("O", shift_no=1, h_width=2, v_width=2)
        self.assertEqual(ctx.exception.code, "NIB_SLOT_CONFLICT")


class OwnershipTests(unittest.TestCase):
    def test_same_piece_cannot_be_signed_to_two_customers(self):
        svc = setup(unit_ids=("u",), sale_id="S", cands=CAND_N, allowed_h=[1, 3], allowed_v=[1, 4],
                    initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        first = svc.open_nib_order("O1", "S", "u", strategy="PREGRIND",
                                   requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 3]},
                                   deadline_shift=1, opened_shift=1, claimant_id="alice")
        self.assertTrue(first["signed"])
        second = svc.open_nib_order("O2", "S", "u", strategy="PREGRIND",
                                    requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 3]},
                                    deadline_shift=1, opened_shift=1, claimant_id="bob")
        self.assertEqual(second["status"], "REFUSED")
        self.assertIn("committed", second["decision"]["reason"])

    def test_delivered_piece_holder_and_original_after_query(self):
        svc = setup(cands=CAND_N, allowed_h=[1, 3], allowed_v=[1, 4],
                    initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        svc.open_nib_order("O", "S", "u", strategy="PREGRIND",
                           requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 3]},
                           deadline_shift=1, opened_shift=1, claimant_id="alice")
        svc.grind_nib("O", shift_no=1)
        svc.deliver_nib("O", shift_no=1, measured_h=1, measured_v=1)
        query = svc.query_nib("S", "u")
        self.assertEqual(query["holder"], "alice")
        self.assertFalse(query["original_width_available"])
        self.assertEqual(query["grinds_done"], 1)
        # Delivering again must replay the identical original delivery.
        replay = svc.deliver_nib("O", shift_no=1, measured_h=1, measured_v=1)
        self.assertTrue(replay["repeated"])
        self.assertEqual(replay["delivery"], svc.get_nib_order("O")["delivery"])
        # A different width at the same order is a conflict, not a new delivery.
        with self.assertRaises(BasketError) as ctx:
            svc.deliver_nib("O", shift_no=1, measured_h=2, measured_v=2)
        self.assertEqual(ctx.exception.code, "NIB_UNIT_CONFLICT")

    def test_refunded_unit_cannot_host_an_order(self):
        svc = open_service(":memory:")
        make_sale(svc, "S", ["u"])
        svc.register_nib("S", "u", owner_id="alice", candidates=CAND_KNOWN, return_deadline=3)
        # Refund the physical unit after registration.
        svc.return_units("r1", "S", ["u"], {"u": "RESTOCK"})
        order = svc.open_nib_order("O", "S", "u", strategy="PREGRIND",
                                   requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 1]},
                                   deadline_shift=1, opened_shift=1, claimant_id="alice")
        self.assertEqual(order["status"], "REFUSED")
        self.assertIn("refunded", order["decision"]["reason"])

    def test_refund_after_signing_blocks_delivery_and_grind(self):
        svc = setup(initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        svc.open_nib_order("O", "S", "u", strategy="PREGRIND",
                           requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 1]},
                           deadline_shift=1, opened_shift=1, claimant_id="alice")
        svc.return_units("r1", "S", ["u"], {"u": "RESTOCK"})
        with self.assertRaises(BasketError) as ctx:
            svc.grind_nib("O", shift_no=1)
        self.assertEqual(ctx.exception.code, "NIB_UNIT_RETURNED")
        # A freshly registered signed order also cannot be ground/delivered post-refund.
        svc2 = setup(initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        svc2.open_nib_order("O", "S", "u", strategy="PREGRIND",
                            requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 1]},
                            deadline_shift=1, opened_shift=1, claimant_id="alice")
        svc2.grind_nib("O", shift_no=1)
        svc2.return_units("r1", "S", ["u"], {"u": "RESTOCK"})
        with self.assertRaises(BasketError) as ctx:
            svc2.deliver_nib("O", shift_no=1, measured_h=1, measured_v=1)
        self.assertEqual(ctx.exception.code, "NIB_UNIT_RETURNED")

    def test_registration_rejected_for_unknown_or_returned_unit(self):
        svc = open_service(":memory:")
        make_sale(svc, "S", ["u"])
        with self.assertRaises(BasketError) as ctx:
            svc.register_nib("S", "nope", owner_id="a", candidates=CAND_KNOWN)
        self.assertEqual(ctx.exception.code, "NIB_UNKNOWN")
        svc.return_units("r1", "S", ["u"], {"u": "SCRAP"})
        with self.assertRaises(BasketError) as ctx:
            svc.register_nib("S", "u", owner_id="a", candidates=CAND_KNOWN)
        self.assertEqual(ctx.exception.code, "NIB_UNIT_RETURNED")

    def test_same_model_units_keep_independent_evidence(self):
        # Two units of the same model: one trial on u1 must not narrow u2, and
        # the second order binds to a different physical unit.
        svc = open_service(":memory:")
        make_sale(svc, "S", ["u1", "u2"])
        for uid, trial in (("u1", 2), ("u2", 3)):
            svc.register_nib("S", uid, owner_id=f"owner-{uid}", candidates=CAND_KNOWN, hand="RIGHT",
                             return_deadline=3, initial_trials=[{"hand": "RIGHT", "direction": H, "width": trial}])
        self.assertEqual([c["kind"] for c in svc.get_nib("S", "u1")["surviving"]], ["STRAIGHT"])
        self.assertEqual([c["kind"] for c in svc.get_nib("S", "u2")["surviving"]], ["SIDE"])
        # Each unit can carry a signed order concurrently; they do not collide.
        o1 = svc.open_nib_order("O1", "S", "u1", strategy="PREGRIND",
                                requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 1]},
                                deadline_shift=1, opened_shift=1, claimant_id="owner-u1")
        o2 = svc.open_nib_order("O2", "S", "u2", strategy="PREGRIND",
                                requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 1]},
                                deadline_shift=1, opened_shift=1, claimant_id="owner-u2")
        self.assertEqual(o1["status"], "SIGNED")
        # u2 is the side nib: 3,1 after one grind, so both=1 is impossible.
        self.assertEqual(o2["status"], "HUNG")

    def test_cross_shift_acceptance_and_delivery(self):
        # Order opened in shift 1, remeasured in shift 1, delivered in shift 2.
        svc = setup(cands=CAND_N, allowed_h=[1, 4], allowed_v=[1, 5],
                    initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
        order = svc.open_nib_order("O", "S", "u", strategy="REMEASURE",
                                   requirement={"hand": "RIGHT", "h": [2, 2], "v": [2, 2]},
                                   deadline_shift=2, opened_shift=1, claimant_id="mallory")
        self.assertEqual(order["status"], "AWAITING_REMEASURE")
        signed = svc.remeasure_nib("O", shift_no=1, h_width=2, v_width=2)
        self.assertEqual(signed["status"], "SIGNED")
        delivered = svc.deliver_nib("O", shift_no=2, measured_h=2, measured_v=2)
        self.assertEqual(delivered["status"], "DELIVERED")
        self.assertEqual(delivered["delivery"]["shift_no"], 2)
        self.assertTrue(delivered["delivery"]["evidence"]["original_width_available"])


class PersistenceTests(unittest.TestCase):
    def test_reopen_preserves_evidence_orders_and_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "nib.db")
            svc = open_service(path)
            make_sale(svc, "S", ["u"])
            svc.register_nib("S", "u", owner_id="alice", candidates=CAND_N,
                             allowed_h=[1, 3], allowed_v=[1, 4], return_deadline=3,
                             initial_trials=[{"hand": "RIGHT", "direction": H, "width": 2}])
            svc.open_nib_order("O", "S", "u", strategy="PREGRIND",
                               requirement={"hand": "RIGHT", "h": [1, 1], "v": [1, 3]},
                               deadline_shift=1, opened_shift=1, claimant_id="alice")
            svc.grind_nib("O", shift_no=1)
            delivered = svc.deliver_nib("O", shift_no=1, measured_h=1, measured_v=1)
            del svc
            reopened = open_service(path)
            self.assertEqual(reopened.get_nib("S", "u")["grinds_done"], 1)
            self.assertEqual(reopened.get_nib_order("O")["delivery"], delivered["delivery"])
            self.assertEqual(reopened.query_nib("S", "u")["holder"], "alice")
            self.assertEqual(reopened.nib_availability(1, 2)["slots"][1]["order_id"], "O")
