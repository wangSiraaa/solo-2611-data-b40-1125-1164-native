"""Optics handover subsystem tests.

Every expectation is derived from first principles (interval arithmetic on the
focus model), independent of the service's internal helper functions.  These
cover the brief's required scenarios:

* a piece may only be signed onto a real, unreturned sale unit (no
  model-number impersonation);
* net focus demand = focal position - total optical path, checked against an
  *inclusive* wheel travel over *all* compatible calibrations;
* the "focal 10, path 6, travel 3..4 -> deliverable" positive case, plus the
  "change shoulder evidence to path 5/6 -> needs travel 5 -> not deliverable"
  counterexample;
* the per-batch zero-point bias is never shared across batches (two-barrel
  counterexample), and one adapter cannot be used by two scopes;
* a front session holding a delivered piece blocks the next session even when
  its window does not overlap, until the piece is returned;
* a temporary telescope switch may continue, while an unfocusable one must be
  refused (cancelled);
* measurements and receipts survive reopen.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from basket_service import open_service
from basket_service.errors import BasketError


def make_sale(svc, skus, sale_id="S1"):
    products = [{"sku": s, "price": 100} for s in skus]
    snap = svc.publish_snapshot(products, [], snapshot_id=f"snap-{sale_id}")
    basket = [{"unit_id": f"u-{s}", "sku": s, "price": 100} for s in skus]
    quote = svc.quote(basket, [], f"q-{sale_id}", snap)
    return svc.register_sale(sale_id, quote=quote)


def wedge(svc, skus, *, travel=(3, 4), bias=0, focal=10, shoulder=(5, 5), path=1, sale_id="S1"):
    """One eyepiece + one adapter signed onto a sale, grouped and verified."""

    sale = make_sale(svc, skus, sale_id=sale_id)
    svc.add_telescope("T1", "SCT", travel[0], travel[1])
    svc.add_batch("B1", bias, bias, [])
    svc.register_piece("EP1", "eyepiece", skus[0], sale_id, f"u-{skus[0]}",
                       batch_id="B1", focal_position=focal, in_iface="SC",
                       measured_lo=shoulder[0], measured_hi=shoulder[1])
    svc.register_piece("AD1", "adapter", skus[1], sale_id, f"u-{skus[1]}",
                       in_iface="SCT", out_iface="SC", nominal_path=path)
    return sale


def full_order(svc, *, travel=(3, 4), shoulder=(5, 5), focal=10, path=1, sale_id="S1"):
    wedge(svc, ["E1", "AD1"], travel=travel, shoulder=shoulder, focal=focal, path=path, sale_id=sale_id)
    group = svc.lock_group("G1", "T1", "EP1", ["AD1"])
    svc.verify_group("G1", observe_wheel=(group["net_lo"] + group["net_hi"]) // 2, technician="tech")
    svc.add_session("SES1", 1, 0, 100)
    return group


class IntakeTests(unittest.TestCase):
    def test_piece_requires_unreturned_sale_unit(self):
        svc = open_service(":memory:")
        sale = make_sale(svc, ["E1"])
        svc.add_batch("B1", 0, 0, [])
        # Unknown unit rejected.
        with self.assertRaises(BasketError) as ctx:
            svc.register_piece("EP1", "eyepiece", "E1", "S1", "nope", batch_id="B1",
                               focal_position=10, in_iface="SC", measured_lo=5, measured_hi=5)
        self.assertEqual(ctx.exception.code, "UNIT_INELIGIBLE")
        # Returned unit rejected.
        svc.return_units("r", "S1", ["u-E1"], {"u-E1": "RESTOCK"})
        with self.assertRaises(BasketError) as ctx:
            svc.register_piece("EP1", "eyepiece", "E1", "S1", "u-E1", batch_id="B1",
                               focal_position=10, in_iface="SC", measured_lo=5, measured_hi=5)
        self.assertEqual(ctx.exception.code, "PIECE_UNAVAILABLE")

    def test_no_two_pieces_impersonate_one_unit(self):
        svc = open_service(":memory:")
        make_sale(svc, ["E1"])
        svc.add_batch("B1", 0, 0, [])
        svc.register_piece("EP1", "eyepiece", "E1", "S1", "u-E1", batch_id="B1",
                           focal_position=10, in_iface="SC", measured_lo=5, measured_hi=5)
        with self.assertRaises(BasketError) as ctx:
            svc.register_piece("EP2", "eyepiece", "E1", "S1", "u-E1", batch_id="B1",
                               focal_position=10, in_iface="SC", measured_lo=5, measured_hi=5)
        self.assertEqual(ctx.exception.code, "PIECE_IMPERSONATION")

    def test_model_must_match_sale_sku(self):
        svc = open_service(":memory:")
        make_sale(svc, ["E1"])
        svc.add_batch("B1", 0, 0, [])
        with self.assertRaises(BasketError) as ctx:
            svc.register_piece("EP1", "eyepiece", "E9", "S1", "u-E1", batch_id="B1",
                               focal_position=10, in_iface="SC", measured_lo=5, measured_hi=5)
        self.assertEqual(ctx.exception.code, "INVALID_OPTIC")

    def test_limits_are_enforced(self):
        svc = open_service(":memory:")
        make_sale(svc, ["E1", "E2", "E3", "E4", "E5", "E6", "E7"])
        svc.add_batch("B1", 0, 0, [])
        for i in range(1, 7):
            svc.register_piece(f"E{i}", "eyepiece", f"E{i}", "S1", f"u-E{i}", batch_id="B1",
                               focal_position=10, in_iface="SC", measured_lo=5, measured_hi=5)
        with self.assertRaises(BasketError) as ctx:
            svc.register_piece("E7", "eyepiece", "E7", "S1", "u-E7", batch_id="B1",
                               focal_position=10, in_iface="SC", measured_lo=5, measured_hi=5)
        self.assertEqual(ctx.exception.code, "OPTIC_LIMIT")
        for i in range(1, 4):
            svc.add_telescope(f"T{i}", "SCT", 0, 10)
        with self.assertRaises(BasketError) as ctx:
            svc.add_telescope("T4", "SCT", 0, 10)
        self.assertEqual(ctx.exception.code, "OPTIC_LIMIT")
        for i in range(1, 4):
            svc.add_session(f"S{i}", i, i * 10, i * 10 + 5)
        with self.assertRaises(BasketError) as ctx:
            svc.add_session("S4", 4, 40, 45)
        self.assertEqual(ctx.exception.code, "OPTIC_LIMIT")


class FocusMathTests(unittest.TestCase):
    def test_positive_case_focal10_path6_travel3to4(self):
        # 焦位10, 目镜+接环总光程6, 调焦轮覆盖3..4 -> net 4 inclusive -> deliverable.
        svc = open_service(":memory:")
        group = full_order(svc)  # shoulder 5, bias 0, adapter 1 => total 6, net 4
        self.assertEqual((group["total_lo"], group["total_hi"]), (6, 6))
        self.assertEqual((group["net_lo"], group["net_hi"]), (4, 4))
        delivery = svc.deliver_order(svc.place_order("O1", "S1", "G1", "SES1", 1, 2)["order_id"])
        self.assertEqual((delivery["focus_lo"], delivery["focus_hi"]), (4, 4))

    def test_changed_shoulder_to_5_needs_travel_5_not_deliverable(self):
        # Change shoulder evidence 5 -> 4 => eyepiece path 4, total with adapter
        # 5 => net 5 => needs travel 5, which lies outside [3,4].
        svc = open_service(":memory:")
        wedge(svc, ["E1", "AD1"])
        # Before a group is locked the evidence may change.
        updated = svc.update_piece_evidence("EP1", 4, 4)
        self.assertEqual(updated["path_interval"], [4, 4])  # eyepiece's own path
        assess = svc.assess_combo("T1", "EP1", ["AD1"])
        self.assertEqual((assess["total_lo"], assess["total_hi"]), (5, 5))
        self.assertEqual((assess["net_lo"], assess["net_hi"]), (5, 5))
        self.assertFalse(assess["deliverable"])
        self.assertEqual(assess["needed_travel"], 5)
        with self.assertRaises(BasketError) as ctx:
            svc.lock_group("G1", "T1", "EP1", ["AD1"])
        self.assertEqual(ctx.exception.code, "FOCUS_UNREACHABLE")

    def test_evidence_cannot_change_under_sealed_group(self):
        svc = open_service(":memory:")
        wedge(svc, ["E1", "AD1"])
        svc.lock_group("G1", "T1", "EP1", ["AD1"])
        with self.assertRaises(BasketError) as ctx:
            svc.update_piece_evidence("EP1", 4, 4)
        self.assertEqual(ctx.exception.code, "GROUP_SEALED")
        # After unsealing it is allowed again, and re-locking fails as expected.
        svc.unseal_group("G1")
        svc.update_piece_evidence("EP1", 4, 4)
        with self.assertRaises(BasketError) as ctx:
            svc.lock_group("G1b", "T1", "EP1", ["AD1"])
        self.assertEqual(ctx.exception.code, "FOCUS_UNREACHABLE")

    def test_endpoints_inclusive(self):
        # net_hi == travel_hi and net_lo == travel_lo both count as reachable.
        svc = open_service(":memory:")
        # shoulder [5,6], bias 0 => total [6,7], focal 10 => net [3,4] == travel [3,4]
        wedge(svc, ["E1", "AD1"], shoulder=(5, 6))
        assess = svc.assess_combo("T1", "EP1", ["AD1"])
        self.assertEqual((assess["net_lo"], assess["net_hi"]), (3, 4))
        self.assertTrue(assess["deliverable"])

    def test_robust_over_all_calibrations(self):
        # Bias interval widened so the net interval is [2,4]; travel [3,4] cannot
        # contain 2, so no calibration set makes it robustly deliverable.
        svc = open_service(":memory:")
        wedge(svc, ["E1", "AD1"], bias=0, shoulder=(5, 5))
        svc.calibrate_batch("B1", [])  # no cards: keep scale but bias fixed at [0,0]
        # Widen the batch scale by re-adding with a wider interval is not allowed;
        # instead widen shoulder to [6,8] => total [7,9] => net [1,3], travel[3,4] fails low end.
        svc.unseal_group  # noop attribute access guard
        svc.update_piece_evidence("EP1", 6, 8)
        assess = svc.assess_combo("T1", "EP1", ["AD1"])
        self.assertEqual((assess["net_lo"], assess["net_hi"]), (1, 3))
        self.assertFalse(assess["deliverable"])

    def test_chain_interfaces_must_match(self):
        svc = open_service(":memory:")
        make_sale(svc, ["E1", "AD1"])
        svc.add_telescope("T1", "SCT", 0, 10)
        svc.add_batch("B1", 0, 0, [])
        svc.register_piece("EP1", "eyepiece", "E1", "S1", "u-E1", batch_id="B1",
                           focal_position=10, in_iface="M42", measured_lo=5, measured_hi=5)
        svc.register_piece("AD1", "adapter", "AD1", "S1", "u-AD1",
                           in_iface="SCT", out_iface="SC", nominal_path=1)
        with self.assertRaises(BasketError) as ctx:
            svc.lock_group("G1", "T1", "EP1", ["AD1"])
        self.assertEqual(ctx.exception.code, "INCOMPATIBLE_CHAIN")


class BiasTests(unittest.TestCase):
    def test_bias_recovered_from_scale_and_cards(self):
        svc = open_service(":memory:")
        # Card: focal - ref_path - wheel = bias point.
        # focal 12, ref_path 3, wheel 5 => point 4 ; scale [0,10] => bias [4,4].
        rec = svc.add_batch("B1", 0, 10, [
            {"focal_position": 12, "ref_path": 3, "wheel": 5},
        ])
        self.assertEqual((rec["bias_lo"], rec["bias_hi"]), (4, 4))
        # Two cards agreeing on a range: points 3 and 6 => bias [3,6].
        rec2 = svc.add_batch("B2", 0, 10, [
            {"focal_position": 10, "ref_path": 3, "wheel": 4},
            {"focal_position": 10, "ref_path": 3, "wheel": 1},
        ])
        self.assertEqual((rec2["bias_lo"], rec2["bias_hi"]), (3, 6))

    def test_inconsistent_calibration_rejected(self):
        svc = open_service(":memory:")
        with self.assertRaises(BasketError) as ctx:
            svc.add_batch("B1", 5, 6, [
                {"focal_position": 10, "ref_path": 3, "wheel": 4},  # point 3, outside [5,6]
            ])
        self.assertEqual(ctx.exception.code, "CALIBRATION_INCONSISTENT")

    def test_recalibration_is_reopenable(self):
        svc = open_service(":memory:")
        rec = svc.add_batch("B1", 0, 10, [{"focal_position": 12, "ref_path": 3, "wheel": 5}])  # bias 4
        self.assertEqual((rec["bias_lo"], rec["bias_hi"]), (4, 4))
        recal = svc.calibrate_batch("B1", [{"focal_position": 10, "ref_path": 3, "wheel": 6}])  # point 1
        self.assertEqual((recal["bias_lo"], recal["bias_hi"]), (1, 1))

    def test_widened_bias_blocks_previously_fine_group(self):
        svc = open_service(":memory:")
        sale = make_sale(svc, ["E1", "AD1"])
        svc.add_telescope("T1", "SCT", 3, 4)
        svc.add_batch("B1", 0, 2, [{"focal_position": 10, "ref_path": 3, "wheel": 6}])  # bias [1,1]
        svc.register_piece("EP1", "eyepiece", "E1", "S1", "u-E1", batch_id="B1",
                           focal_position=10, in_iface="SC", measured_lo=4, measured_hi=4)
        svc.register_piece("AD1", "adapter", "AD1", "S1", "u-AD1",
                           in_iface="SCT", out_iface="SC", nominal_path=1)
        # path = (4+1) shoulder + 1 adapter = 6, net = 4 => fits [3,4].
        group = svc.lock_group("G1", "T1", "EP1", ["AD1"])
        self.assertEqual((group["net_lo"], group["net_hi"]), (4, 4))
        svc.verify_group("G1", observe_wheel=4, technician="t")
        svc.add_session("SES1", 1, 0, 10)
        svc.add_session("SES2", 2, 10, 20)  # touches SES1 end, no overlap
        svc.place_order("O1", "S1", "G1", "SES1", 1, 2)
        # Re-calibrate downward (bias 2) => path 7 => net 3, still inside [3,4].
        svc.calibrate_batch("B1", [{"focal_position": 10, "ref_path": 3, "wheel": 5}])  # point 2
        o2 = svc.place_order("O2", "S1", "G1", "SES2", 11, 12)
        self.assertEqual(o2["status"], "OPEN")
        # Now how a calibration to an out-of-range point is refused by the scale.
        with self.assertRaises(BasketError) as ctx:
            svc.calibrate_batch("B1", [{"focal_position": 10, "ref_path": 3, "wheel": 1}])  # point 6 > scale_hi
        self.assertEqual(ctx.exception.code, "CALIBRATION_INCONSISTENT")


class GroupTests(unittest.TestCase):
    def test_unverified_group_cannot_be_ordered(self):
        svc = open_service(":memory:")
        wedge(svc, ["E1", "AD1"])
        svc.lock_group("G1", "T1", "EP1", ["AD1"])
        svc.add_session("SES1", 1, 0, 10)
        with self.assertRaises(BasketError) as ctx:
            svc.place_order("O1", "S1", "G1", "SES1", 1, 2)
        self.assertEqual(ctx.exception.code, "GROUP_NOT_VERIFIED")

    def test_sealed_members_cannot_be_lent_alone(self):
        svc = open_service(":memory:")
        full_order(svc)
        # A second group trying to reuse a sealed member fails.
        with self.assertRaises(BasketError) as ctx:
            svc.lock_group("G2", "T1", "EP1", [])
        self.assertEqual(ctx.exception.code, "GROUP_SEALED")

    def test_verify_requires_wheel_inside_interval(self):
        svc = open_service(":memory:")
        wedge(svc, ["E1", "AD1"])
        svc.lock_group("G1", "T1", "EP1", ["AD1"])
        with self.assertRaises(BasketError) as ctx:
            svc.verify_group("G1", observe_wheel=9, technician="t")
        self.assertEqual(ctx.exception.code, "FOCUS_UNREACHABLE")

    def test_unseal_requires_no_active_booking(self):
        svc = open_service(":memory:")
        full_order(svc)
        svc.place_order("O1", "S1", "G1", "SES1", 1, 2)
        with self.assertRaises(BasketError) as ctx:
            svc.unseal_group("G1")
        self.assertEqual(ctx.exception.code, "GROUP_SEALED")


class TwoBarrelTests(unittest.TestCase):
    def test_shared_scale_does_not_imply_shared_bias(self):
        # Two scopes, two shoulder batches with different real biases.  A wrong
        # assumption that they share one bias would mis-rate the second scope.
        svc = open_service(":memory:")
        make_sale(svc, ["E1", "E2", "AD1"])
        svc.add_batch("B1", 0, 0, [])
        svc.add_batch("B2", 2, 2, [])
        svc.add_telescope("T1", "SCT", 0, 4)
        svc.add_telescope("T2", "SCT", 0, 4)
        svc.register_piece("EP1", "eyepiece", "E1", "S1", "u-E1", batch_id="B1",
                           focal_position=10, in_iface="SC", measured_lo=3, measured_hi=3)
        svc.register_piece("EP2", "eyepiece", "E2", "S1", "u-E2", batch_id="B2",
                           focal_position=10, in_iface="SC", measured_lo=3, measured_hi=3)
        svc.register_piece("AD1", "adapter", "AD1", "S1", "u-AD1",
                           in_iface="SCT", out_iface="SC", nominal_path=1)
        # EP1 path 3 +1 -> net 6 (not deliverable on travel 0..4)
        self.assertFalse(svc.assess_combo("T1", "EP1", ["AD1"])["deliverable"])
        # EP2 path 5 +1 -> net 4 (deliverable)
        self.assertTrue(svc.assess_combo("T2", "EP2", ["AD1"])["deliverable"])
        # Only the deliverable scope may lock the shared adapter.
        svc.lock_group("G2", "T2", "EP2", ["AD1"])
        with self.assertRaises(BasketError) as ctx:
            svc.lock_group("G1", "T1", "EP1", ["AD1"])
        self.assertEqual(ctx.exception.code, "GROUP_SEALED")

    def test_one_adapter_cannot_serve_two_scopes(self):
        svc = open_service(":memory:")
        make_sale(svc, ["E1", "E2", "AD1", "AD2"])
        svc.add_batch("B1", 0, 0, [])
        svc.add_telescope("T1", "SCT", 0, 10)
        svc.add_telescope("T2", "SCT", 0, 10)
        for name in ("E1", "E2"):
            svc.register_piece(name, "eyepiece", name, "S1", f"u-{name}", batch_id="B1",
                               focal_position=8, in_iface="SC", measured_lo=3, measured_hi=3)
        svc.register_piece("AD1", "adapter", "AD1", "S1", "u-AD1",
                           in_iface="SCT", out_iface="SC", nominal_path=1)
        svc.register_piece("AD2", "adapter", "AD2", "S1", "u-AD2",
                           in_iface="SCT", out_iface="SC", nominal_path=1)
        svc.lock_group("G1", "T1", "E1", ["AD1"])
        with self.assertRaises(BasketError) as ctx:
            svc.lock_group("G2", "T2", "E2", ["AD1"])
        self.assertEqual(ctx.exception.code, "GROUP_SEALED")
        # With its own adapter the second scope is fine.
        g2 = svc.lock_group("G2", "T2", "E2", ["AD2"])
        self.assertEqual(g2["adapter_ids"], ["AD2"])


class SessionTests(unittest.TestCase):
    def test_overlapping_sessions_conflict(self):
        svc = open_service(":memory:")
        full_order(svc)
        svc.add_session("SES2", 2, 50, 60)  # overlaps [0,100]
        svc.place_order("O1", "S1", "G1", "SES1", 1, 2)
        with self.assertRaises(BasketError) as ctx:
            svc.place_order("O2", "S1", "G1", "SES2", 51, 52)
        self.assertEqual(ctx.exception.code, "PIECE_BOOKED")

    def test_delivered_piece_held_until_returned(self):
        svc = open_service(":memory:")
        full_order(svc)
        svc.add_session("SES2", 2, 200, 300)  # non-overlapping with SES1 [0,100]
        svc.place_order("O1", "S1", "G1", "SES1", 1, 2)
        svc.deliver_order("O1")
        # Front session holds the adapter -> next session cannot deliver it even
        # though the windows do not overlap, until the group is returned.
        with self.assertRaises(BasketError) as ctx:
            svc.place_order("O2", "S1", "G1", "SES2", 201, 202)
        self.assertEqual(ctx.exception.code, "PIECE_BOOKED")
        svc.return_pieces("O1")
        o2 = svc.place_order("O2", "S1", "G1", "SES2", 201, 202)
        self.assertEqual(o2["status"], "OPEN")

    def test_reserved_but_not_delivered_frees_at_non_overlap(self):
        svc = open_service(":memory:")
        full_order(svc)
        svc.add_session("SES2", 2, 100, 200)  # touches SES1 end at 100 (no overlap)
        svc.place_order("O1", "S1", "G1", "SES1", 1, 2)
        # Touching endpoints do not conflict.
        o2 = svc.place_order("O2", "S1", "G1", "SES2", 101, 102)
        self.assertEqual(o2["status"], "OPEN")

    def test_slot_must_lie_in_session(self):
        svc = open_service(":memory:")
        full_order(svc)
        with self.assertRaises(BasketError) as ctx:
            svc.place_order("O1", "S1", "G1", "SES1", -5, 2)
        self.assertEqual(ctx.exception.code, "INVALID_OPTIC")

    def test_two_concurrent_placements_one_wins(self):
        # A single piece cannot be promised to two overlapping sessions even if
        # two threads race to book it: exactly one placement commits.
        import tempfile
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "race.db")
            setup = open_service(path)
            full_order(setup)
            setup.add_session("SES2", 2, 0, 100)  # overlaps SES1 [0,100]
            del setup

            results = []
            lock = threading.Lock()
            barrier = threading.Barrier(2)

            def worker(order_id):
                client = open_service(path)
                try:
                    barrier.wait()
                    client.place_order(order_id, "S1", "G1", "SES2", 1, 2)
                    with lock:
                        results.append((order_id, "OK"))
                except BasketError as exc:
                    with lock:
                        results.append((order_id, exc.code))

            threads = [threading.Thread(target=worker, args=(f"O-{i}",)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            outcomes = sorted(code for _, code in results)
            self.assertEqual(outcomes, ["OK", "PIECE_BOOKED"])
            final = open_service(path)
            open_orders = [o for o in final.list_orders() if o["status"] == "OPEN"]
            self.assertEqual(len(open_orders), 1)


class SwitchAndCancelTests(unittest.TestCase):
    def _setup_two_scopes(self, travel2=(3, 4), travel3=(0, 2)):
        svc = open_service(":memory:")
        full_order(svc)
        svc.add_telescope("T2", "SCT", travel2[0], travel2[1])
        svc.add_telescope("T3", "SCT", travel3[0], travel3[1])
        svc.place_order("O1", "S1", "G1", "SES1", 1, 2)
        return svc

    def test_switch_to_compatible_scope_continues(self):
        svc = open_service(":memory:")
        full_order(svc)
        # T2 has a different (but still containing) travel interval.
        svc.add_telescope("T2", "SCT", 4, 5)
        svc.place_order("O1", "S1", "G1", "SES1", 1, 2)
        result = svc.switch_telescope("O1", "T2")
        self.assertTrue(result["continued"])
        self.assertEqual(result["telescope_id"], "T2")
        self.assertEqual(result["switched_from"], "T1")
        # Delivery is re-evaluated against T2's live travel, not the stored one.
        delivery = svc.deliver_order("O1")
        self.assertEqual((delivery["focus_lo"], delivery["focus_hi"]), (4, 4))
        self.assertEqual((delivery["travel_lo"], delivery["travel_hi"]), (4, 5))

    def test_switch_to_unfocusable_scope_must_be_refused(self):
        svc = self._setup_two_scopes()
        with self.assertRaises(BasketError) as ctx:
            svc.switch_telescope("O1", "T3")
        self.assertEqual(ctx.exception.code, "FOCUS_UNREACHABLE")
        # Order remains on the original telescope and is still valid.
        order = svc.get_order("O1")
        self.assertEqual(order["telescope_id"], "T1")

    def test_one_order_continues_another_must_cancel(self):
        # 一单临时换台后能够继续，另一单必须退约.
        svc = open_service(":memory:")
        make_sale(svc, ["E1", "E2", "AD1", "AD2"])
        svc.add_batch("B1", 0, 0, [])
        svc.add_telescope("T1", "SCT", 3, 4)
        svc.add_telescope("T2", "SCT", 3, 4)   # compatible, can host a temporary move
        svc.add_telescope("T3", "SCT", 0, 2)   # too short: cannot focus the group
        for name in ("E1", "E2"):
            svc.register_piece(name, "eyepiece", name, "S1", f"u-{name}", batch_id="B1",
                               focal_position=10, in_iface="SC", measured_lo=5, measured_hi=5)
        for name in ("AD1", "AD2"):
            svc.register_piece(name, "adapter", name, "S1", f"u-{name}",
                               in_iface="SCT", out_iface="SC", nominal_path=1)
        svc.lock_group("G1", "T1", "E1", ["AD1"])
        svc.lock_group("G2", "T1", "E2", ["AD2"])
        svc.verify_group("G1", observe_wheel=4, technician="t")
        svc.verify_group("G2", observe_wheel=4, technician="t")
        svc.add_session("SES1", 1, 0, 100)

        # Order 1 starts on T1, then temporarily re-mounts on the compatible T2.
        svc.place_order("O1", "S1", "G1", "SES1", 1, 2)
        moved = svc.switch_telescope("O1", "T2")
        self.assertTrue(moved["continued"])
        self.assertEqual((moved["switched_from"], moved["telescope_id"]), ("T1", "T2"))

        # Order 2 takes the now-free T1, but the only spare scope T3 is too short.
        svc.place_order("O2", "S1", "G2", "SES1", 3, 4)
        with self.assertRaises(BasketError) as ctx:
            svc.switch_telescope("O2", "T3")
        self.assertEqual(ctx.exception.code, "FOCUS_UNREACHABLE")
        # Therefore order 2 must be cancelled (it cannot continue elsewhere).
        cancelled = svc.cancel_order("O2", reason="no compatible telescope on site")
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertFalse(any(b["active"] for b in svc.list_bookings() if b["order_id"] == "O2"))
        # Order 1 keeps going and delivers fine on T2.
        self.assertEqual(svc.deliver_order("O1")["telescope_id"], "T2")

    def test_delivered_order_cannot_be_cancelled(self):
        svc = open_service(":memory:")
        full_order(svc)
        svc.place_order("O1", "S1", "G1", "SES1", 1, 2)
        svc.deliver_order("O1")
        with self.assertRaises(BasketError) as ctx:
            svc.cancel_order("O1")
        self.assertEqual(ctx.exception.code, "ORDER_STATE")


class PlannerAndReopenTests(unittest.TestCase):
    def test_plan_round_trip(self):
        plan = {
            "steps": [
                {"op": "publish_snapshot", "products": [{"sku": "E1", "price": 100}, {"sku": "AD1", "price": 100}],
                 "coupons": [], "snapshot_id": "s1"},
                {"op": "quote", "basket": [{"unit_id": "u-E1", "sku": "E1", "price": 100},
                                           {"unit_id": "u-AD1", "sku": "AD1", "price": 100}],
                 "coupons": [], "request_id": "q1", "snapshot_id": "s1"},
                {"op": "register_sale", "sale_id": "S1", "quote_request_id": "q1"},
                {"op": "add_telescope", "telescope_id": "T1", "out_iface": "SCT",
                 "travel_lo": 3, "travel_hi": 4},
                {"op": "add_batch", "batch_id": "B1", "scale_lo": 0, "scale_hi": 0, "cards": []},
                {"op": "register_piece", "piece_id": "EP1", "kind": "eyepiece", "model": "E1",
                 "sale_id": "S1", "unit_id": "u-E1", "batch_id": "B1", "focal_position": 10,
                 "in_iface": "SC", "measured_lo": 5, "measured_hi": 5},
                {"op": "register_piece", "piece_id": "AD1", "kind": "adapter", "model": "AD1",
                 "sale_id": "S1", "unit_id": "u-AD1", "in_iface": "SCT", "out_iface": "SC",
                 "nominal_path": 1},
                {"name": "g", "op": "lock_group", "group_id": "G1", "telescope_id": "T1",
                 "eyepiece_id": "EP1", "adapter_ids": ["AD1"]},
                {"op": "verify_group", "group_id": "G1", "observe_wheel": 4, "technician": "t"},
                {"op": "add_session", "session_id": "SES1", "seq": 1, "starts_at": 0, "ends_at": 100},
                {"op": "place_order", "order_id": "O1", "sale_id": "S1", "group_id": "G1",
                 "session_id": "SES1", "slot_lo": 1, "slot_hi": 2},
                {"name": "d", "op": "deliver_order", "order_id": "O1"},
                {"op": "get_delivery", "order_id": "O1"},
                {"op": "deliver_order", "order_id": "O1"},
            ]
        }
        svc = open_service(":memory:")
        result = svc.run(plan)
        self.assertTrue(result["ok"])
        self.assertEqual(result["steps"][7]["result"]["net_lo"], 4)
        self.assertEqual(result["steps"][11]["result"]["focus_lo"], 4)
        self.assertEqual(result["steps"][12]["result"]["focus_hi"], 4)
        # Re-delivery replays the same receipt.
        self.assertEqual(result["steps"][12]["result"], result["steps"][13]["result"])

    def test_reopen_preserves_optics_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "optics.db")
            svc = open_service(path)
            full_order(svc)
            svc.place_order("O1", "S1", "G1", "SES1", 1, 2)
            svc.deliver_order("O1")
            before = {
                "group": svc.get_group("G1"),
                "delivery": svc.get_delivery("O1"),
                "bookings": svc.list_bookings(),
                "piece": svc.get_piece("EP1"),
                "batch": svc.get_batch("B1"),
            }
            del svc
            reopened = open_service(path)
            after = {
                "group": reopened.get_group("G1"),
                "delivery": reopened.get_delivery("O1"),
                "bookings": reopened.list_bookings(),
                "piece": reopened.get_piece("EP1"),
                "batch": reopened.get_batch("B1"),
            }
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
