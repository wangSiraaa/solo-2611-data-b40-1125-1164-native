"""Tests for the troupe fitting / cabinet / paired-delivery subsystem."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from basket_service import open_service
from basket_service.errors import BasketError
from basket_service.reference import max_pairs_bruteforce
from basket_service.troupe import plan_pairs, shoe_options

PRODUCTS = [{"sku": "DANCE", "price": 100}]


def make_sale(svc, unit_ids, sale_id="S-shoes"):
    snap = svc.publish_snapshot(PRODUCTS, [], snapshot_id=f"snap-{sale_id}")
    basket = [{"unit_id": u, "sku": "DANCE", "price": 100} for u in unit_ids]
    quote = svc.quote(basket, [], f"q-{sale_id}", snap)
    return svc.register_sale(sale_id, quote=quote)


def seed_shoe(svc, physical_id, side, *, width, stretch_to=None, length=5, origin_dancer="owner", sale_id="S-shoes"):
    svc.recover_from_sale(physical_id, side, sale_id, physical_id, width=width, length=length, stretch_to=stretch_to)
    svc.consign(physical_id, origin_dancer, f"consent-{physical_id}")
    svc.publish_fitting_slip(f"slip-{physical_id}", physical_id)


def shoe_dicts(svc):
    out = {}
    for view in svc.list_physicals():
        out[view["physical_id"]] = {
            "physical_id": view["physical_id"],
            "side": view["side"],
            "width": view["width"],
            "stretch_to": view["stretch_to"],
            "directions": view["directions"],
            "length": view["length"],
            "pad_step": 1,
        }
    return out


class PlanPairsTests(unittest.TestCase):
    def test_special_lefts_and_rights_two_pairs_with_one_stretch(self):
        shoes = {
            "LJ": {"physical_id": "LJ", "side": "L", "width": 2, "stretch_to": 2, "directions": ["WIDTH"], "length": 5},
            "LY": {"physical_id": "LY", "side": "L", "width": 2, "stretch_to": 2, "directions": ["WIDTH"], "length": 5},
            "R1": {"physical_id": "R1", "side": "R", "width": 2, "stretch_to": 2, "directions": ["WIDTH"], "length": 5},
            "R2": {"physical_id": "R2", "side": "R", "width": 2, "stretch_to": 3, "directions": ["WIDTH"], "length": 5},
        }
        needs = [
            {"dancer_id": "jia", "left": {"widths": [2], "length": 5}, "right": {"widths": [2], "length": 5}},
            {"dancer_id": "yi", "left": {"widths": [2], "length": 5}, "right": {"widths": [3], "length": 5}},
        ]
        plan = plan_pairs(list(shoes.values()), needs, stretch_budget=1)
        self.assertEqual(plan["count"], 2)
        self.assertEqual(plan["stretch_used"], 1)
        # Matches the independent brute-force maximum.
        self.assertEqual(plan["count"], max_pairs_bruteforce(list(shoes.values()), needs, stretch_budget=1))

    def test_same_setup_but_r2_cannot_stretch_yields_one(self):
        shoes = {
            "LJ": {"physical_id": "LJ", "side": "L", "width": 2, "stretch_to": 2, "directions": ["WIDTH"], "length": 5},
            "LY": {"physical_id": "LY", "side": "L", "width": 2, "stretch_to": 2, "directions": ["WIDTH"], "length": 5},
            "R1": {"physical_id": "R1", "side": "R", "width": 2, "stretch_to": 2, "directions": ["WIDTH"], "length": 5},
            "R2": {"physical_id": "R2", "side": "R", "width": 2, "stretch_to": 2, "directions": ["WIDTH"], "length": 5},
        }
        needs = [
            {"dancer_id": "jia", "left": {"widths": [2], "length": 5}, "right": {"widths": [2], "length": 5}},
            {"dancer_id": "yi", "left": {"widths": [2], "length": 5}, "right": {"widths": [3], "length": 5}},
        ]
        self.assertEqual(plan_pairs(list(shoes.values()), needs, stretch_budget=1)["count"], 1)

    def test_pad_serves_one_width2_among_two_width3_shoes(self):
        shoes = {
            "R": {"physical_id": "R", "side": "R", "width": 3, "stretch_to": 3, "directions": ["WIDTH"], "length": 5},
            "B": {"physical_id": "B", "side": "R", "width": 3, "stretch_to": 3, "directions": ["WIDTH"], "length": 5},
        }
        needs = [
            {"dancer_id": "yi", "left": None, "right": {"widths": [2], "length": 5}},
            {"dancer_id": "bing", "left": None, "right": {"widths": [2], "length": 5}},
        ]
        # One pad -> only one width-2 foot can be served.
        self.assertEqual(plan_pairs(list(shoes.values()), needs, pads_available=1, stretch_budget=0)["count"], 1)
        # No pad -> zero.
        self.assertEqual(plan_pairs(list(shoes.values()), needs, pads_available=0, stretch_budget=0)["count"], 0)

    def test_pad_bruteforce_agrees(self):
        shoes = [
            {"physical_id": "R", "side": "R", "width": 3, "stretch_to": 3, "directions": ["WIDTH"], "length": 5},
            {"physical_id": "B", "side": "R", "width": 3, "stretch_to": 3, "directions": ["WIDTH"], "length": 5},
        ]
        needs = [
            {"dancer_id": "yi", "left": None, "right": {"widths": [2], "length": 5}},
            {"dancer_id": "bing", "left": None, "right": {"widths": [2], "length": 5}},
        ]
        for pads in range(0, 3):
            self.assertEqual(
                plan_pairs(shoes, needs, pads_available=pads)["count"],
                max_pairs_bruteforce(shoes, needs, pads_available=pads),
            )

    def test_length_pin_and_side_restrict(self):
        self.assertEqual(shoe_options(
            {"physical_id": "x", "side": "R", "width": 2, "stretch_to": 3, "directions": ["WIDTH"], "length": 5},
            {"widths": [2], "length": 6}, pad_available=False, stretch_remaining=1), [])
        self.assertTrue(shoe_options(
            {"physical_id": "x", "side": "R", "width": 2, "stretch_to": 3, "directions": ["WIDTH"], "length": 5},
            {"widths": [3], "length": 5}, pad_available=False, stretch_remaining=1))


class PhysicalAndConsentTests(unittest.TestCase):
    def setUp(self):
        self.svc = open_service(":memory:")
        make_sale(self.svc, ["L1", "R1"])

    def test_recover_uses_real_unit_and_paid(self):
        phys = self.svc.recover_from_sale("R1", "R", "S-shoes", "R1", width=2, length=5, stretch_to=3)
        self.assertEqual(phys["paid"], 100)
        self.assertEqual(phys["unit_id"], "R1")
        self.assertTrue(phys["usable"])

    def test_returned_unit_unusable_on_recovery(self):
        self.svc.return_units("ret-r1", "S-shoes", ["R1"], {"R1": "RESTOCK"})
        phys = self.svc.recover_from_sale("R1", "R", "S-shoes", "R1", width=2, length=5)
        self.assertFalse(phys["usable"])

    def test_unknown_origin_unit(self):
        with self.assertRaises(BasketError) as ctx:
            self.svc.recover_from_sale("nope", "R", "S-shoes", "nope", width=2)
        self.assertEqual(ctx.exception.code, "PHYSICAL_CONFLICT")

    def test_consent_required_for_eligibility(self):
        self.svc.recover_from_sale("R1", "R", "S-shoes", "R1", width=2, length=5, stretch_to=2)
        self.assertFalse(self.svc.get_physical("R1")["consigned"])
        self.svc.consign("R1", "dancer", "receipt-1")
        self.assertTrue(self.svc.get_physical("R1")["consigned"])
        self.svc.revoke_consignment("R1")
        self.assertFalse(self.svc.get_physical("R1")["consigned"])

    def test_consign_conflict(self):
        self.svc.recover_from_sale("R1", "R", "S-shoes", "R1", width=2, length=5)
        self.svc.consign("R1", "a", "r1")
        with self.assertRaises(BasketError) as ctx:
            self.svc.consign("R1", "b", "r2")
        self.assertEqual(ctx.exception.code, "DEPOSIT_CONFLICT")


class StretchAndWidthTests(unittest.TestCase):
    def setUp(self):
        self.svc = open_service(":memory:")
        make_sale(self.svc, ["R1"])

    def test_stretch_cannot_shrink_and_respects_limit(self):
        self.svc.recover_from_sale("R1", "R", "S-shoes", "R1", width=2, length=5, stretch_to=3)
        self.svc.stretch("R1", 3)
        self.assertEqual(self.svc.get_physical("R1")["width"], 3)
        with self.assertRaises(BasketError) as ctx:
            self.svc.stretch("R1", 2)
        self.assertEqual(ctx.exception.code, "WIDTH_IRREVERSIBLE")
        with self.assertRaises(BasketError) as ctx2:
            self.svc.stretch("R1", 4)
        self.assertEqual(ctx2.exception.code, "WIDTH_IRREVERSIBLE")

    def test_cannot_clear_occupancy_to_restore_width(self):
        # "Cannot restore width 3 to width 2 by clearing bookkeeping occupancy."
        self.svc.recover_from_sale("R1", "R", "S-shoes", "R1", width=2, length=5, stretch_to=3)
        self.svc.stretch("R1", 3)
        self.svc.assign_occupancy("R1", "prev-show")
        self.svc.release_occupancy("R1", "prev-show")
        self.assertEqual(self.svc.get_physical("R1")["width"], 3)
        with self.assertRaises(BasketError):
            self.svc.stretch("R1", 2)


class AllocationServiceTests(unittest.TestCase):
    def _svc(self):
        svc = open_service(":memory:")
        make_sale(svc, ["LJ", "LY", "R1", "R2"])
        seed_shoe(svc, "LJ", "L", width=2, stretch_to=2)
        seed_shoe(svc, "LY", "L", width=2, stretch_to=2)
        seed_shoe(svc, "R1", "R", width=2, stretch_to=2)
        seed_shoe(svc, "R2", "R", width=2, stretch_to=3)
        return svc

    def test_two_pairs_then_restrictions(self):
        needs = [
            {"dancer_id": "jia", "left": {"width": 2, "length": 5}, "right": {"width": 2, "length": 5}},
            {"dancer_id": "yi", "left": {"width": 2, "length": 5}, "right": {"width": 3, "length": 5}},
        ]
        svc = self._svc()
        self.assertEqual(svc.allocate("a", needs, stretch_budget=1)["count"], 2)

        svc.update_fitting("R2", stretch_to=2)
        self.assertEqual(svc.allocate("b", needs, stretch_budget=1)["count"], 1)

        svc2 = self._svc()
        svc2.assign_occupancy("R2", "prev-show")
        self.assertEqual(svc2.allocate("c", needs, run="current-show", stretch_budget=1)["count"], 1)

    def test_occupancy_of_same_run_is_allowed(self):
        needs = [
            {"dancer_id": "jia", "left": {"width": 2, "length": 5}, "right": {"width": 2, "length": 5}},
            {"dancer_id": "yi", "left": {"width": 2, "length": 5}, "right": {"width": 3, "length": 5}},
        ]
        svc = self._svc()
        svc.assign_occupancy("R2", "run-x")
        self.assertEqual(svc.allocate("c", needs, run="run-x", stretch_budget=1)["count"], 2)

    def test_consent_revocation_blocks_allocation(self):
        needs = [
            {"dancer_id": "jia", "left": {"width": 2, "length": 5}, "right": {"width": 2, "length": 5}},
            {"dancer_id": "yi", "left": {"width": 2, "length": 5}, "right": {"width": 3, "length": 5}},
        ]
        svc = self._svc()
        svc.revoke_consignment("R2")
        result = svc.allocate("a", needs, stretch_budget=1)
        self.assertEqual(result["count"], 1)
        self.assertIn({"physical_id": "R2", "reason": "no active troupe consent"}, result["excluded"])

    def test_slip_required(self):
        needs = [{"dancer_id": "jia", "left": {"width": 2, "length": 5}, "right": {"width": 2, "length": 5}}]
        svc = self._svc()
        # Both lefts lose their slip, so no complete pair can be planned.
        svc.retract_fitting_slip("slip-LJ")
        svc.retract_fitting_slip("slip-LY")
        result = svc.allocate("a", needs, stretch_budget=0)
        self.assertEqual(result["count"], 0)

    def test_allocation_idempotent_and_conflict(self):
        needs = [{"dancer_id": "jia", "left": {"width": 2, "length": 5}, "right": {"width": 2, "length": 5}}]
        svc = self._svc()
        first = svc.allocate("a", needs, stretch_budget=0)
        replay = svc.allocate("a", needs, stretch_budget=0)
        self.assertTrue(replay["repeated"])
        with self.assertRaises(BasketError) as ctx:
            svc.allocate("a", needs, stretch_budget=1)
        self.assertEqual(ctx.exception.code, "ALLOCATION_CONFLICT")


class DeliveryTests(unittest.TestCase):
    def _svc(self, path=":memory:"):
        svc = open_service(path)
        make_sale(svc, ["LJ", "LY", "R1", "R2"])
        seed_shoe(svc, "LJ", "L", width=2, stretch_to=2)
        seed_shoe(svc, "LY", "L", width=2, stretch_to=2)
        seed_shoe(svc, "R1", "R", width=2, stretch_to=2)
        seed_shoe(svc, "R2", "R", width=2, stretch_to=3)
        return svc

    NEEDS = [
        {"dancer_id": "jia", "left": {"width": 2, "length": 5}, "right": {"width": 2, "length": 5}},
        {"dancer_id": "yi", "left": {"width": 2, "length": 5}, "right": {"width": 3, "length": 5}},
    ]

    def test_delivery_binds_pair_and_persists_after_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "shop.db")
            svc = self._svc(path)
            svc.allocate("alloc", self.NEEDS, run="run-1", stretch_budget=1)
            delivered = svc.confirm_delivery("alloc", run_until="run-1-end")
            self.assertEqual(delivered["pairs_delivered"], 2)
            # Each dancer's two shoes share a binding (pair) and tied laces.
            for b in delivered["bindings"]:
                self.assertTrue(b["tied_laces"])
            yi_width = svc.get_physical("R2")["width"]
            self.assertEqual(yi_width, 3)
            del svc
            reopened = open_service(path)
            view = reopened.cabinet_view()
            self.assertEqual(len(view["bindings"]), 4)
            self.assertEqual(reopened.get_physical("R2")["width"], 3)
            # Widths never shrink even after reopening.
            with self.assertRaises(BasketError):
                reopened.stretch("R2", 2)

    def test_bound_shoe_cannot_be_reallocated_while_held(self):
        svc = self._svc()
        svc.allocate("alloc", self.NEEDS, run="run-1", stretch_budget=1)
        svc.confirm_delivery("alloc")
        again = svc.allocate("alloc2", self.NEEDS, run="run-1", stretch_budget=1)
        # All four shoes are held, so no fresh delivery can be planned.
        self.assertEqual(again["count"], 0)

    def test_return_between_plan_and_delivery_blocks(self):
        svc = self._svc()
        svc.allocate("alloc", self.NEEDS, stretch_budget=1)
        svc.return_units("ret-r2", "S-shoes", ["R2"], {"R2": "RESTOCK"})
        svc.confirm_return("conf", "ret-r2", "S-shoes")
        with self.assertRaises(BasketError) as ctx:
            svc.confirm_delivery("alloc")
        self.assertEqual(ctx.exception.code, "PHYSICAL_UNAVAILABLE")

    def test_delivery_idempotent(self):
        svc = self._svc()
        svc.allocate("alloc", self.NEEDS, stretch_budget=1)
        first = svc.confirm_delivery("alloc")
        second = svc.confirm_delivery("alloc")
        self.assertTrue(second["repeated"])
        self.assertEqual(len(svc.list_bindings()), 4)

    def test_late_return_flagged(self):
        svc = self._svc()
        svc.allocate("alloc", self.NEEDS, stretch_budget=1)
        delivered = svc.confirm_delivery("alloc")
        binding = delivered["bindings"][0]["binding_id"]
        late = svc.return_delivery(binding, on_time=False, receipt_id="R-LATE")
        self.assertTrue(late["late"])
        self.assertEqual(late["receipt_id"], "R-LATE")


class PadAndModeTests(unittest.TestCase):
    def _svc(self, mode, pad_holder):
        svc = open_service(":memory:")
        svc.set_shop_mode(mode)
        make_sale(svc, ["R", "B"])
        seed_shoe(svc, "R", "R", width=3, stretch_to=3)
        seed_shoe(svc, "B", "R", width=3, stretch_to=3)
        svc.register_pad("PAD1", holder=pad_holder)
        return svc

    NEEDS = [
        {"dancer_id": "yi", "left": None, "right": {"width": 2, "length": 5}},
        {"dancer_id": "bing", "left": None, "right": {"width": 2, "length": 5}},
    ]

    def test_prestretch_uses_own_pad(self):
        svc = self._svc("PRESTRETCH", "shop")
        self.assertEqual(svc.allocate("a", self.NEEDS, stretch_budget=0)["count"], 1)

    def test_pad_cannot_be_in_two_shoes(self):
        svc = self._svc("PERF_TRANSFER", "shop")
        svc.allocate("a", self.NEEDS, stretch_budget=0)
        svc.confirm_delivery("a")
        # Pad now lives in one shoe; a fresh allocation has no spare pad.
        svc.return_delivery(svc.list_bindings()[0]["binding_id"])  # frees one shoe+pad
        self.assertEqual(svc.get_pad("PAD1")["physical_id"], None)

    def test_perf_transfer_requires_troupe_pad_acquisition(self):
        svc = self._svc("PERF_TRANSFER", "troupe")
        self.assertEqual(svc.allocate("a", self.NEEDS, stretch_budget=0)["count"], 0)
        svc.acquire_borrowable_pad("acq", "PAD1", "EXT-RECEIPT")
        self.assertEqual(svc.allocate("b", self.NEEDS, stretch_budget=0)["count"], 1)
        ledger = svc.pad_ledger("PAD1")
        self.assertEqual(ledger[-1]["direction"], "ACQUIRE")
        self.assertEqual(ledger[-1]["receipt_id"], "EXT-RECEIPT")

    def test_prestretch_refuses_troupe_transfer(self):
        svc = self._svc("PRESTRETCH", "troupe")
        with self.assertRaises(BasketError) as ctx:
            svc.acquire_borrowable_pad("acq", "PAD1", "EXT")
        self.assertEqual(ctx.exception.code, "INVALID_SHOP_MODE")

    def test_dancer_borrow_and_return_replay(self):
        svc = self._svc("PERF_TRANSFER", "bing")  # 丙 already borrowed it from the troupe
        with self.assertRaises(BasketError) as ctx:
            svc.acquire_borrowable_pad("acq", "PAD1", "EXT")
        self.assertEqual(ctx.exception.code, "INVALID_RECEIPT")
        # Replay: 丙 returns it to the troupe before 乙 signs.
        svc.record_pad_receipt("ret", "PAD1", "bing", "troupe", "RET-1")
        svc.acquire_borrowable_pad("acq", "PAD1", "EXT")
        self.assertEqual(svc.get_pad("PAD1")["holder"], "shop")
        self.assertEqual(svc.allocate("b", self.NEEDS, stretch_budget=0)["count"], 1)

    def test_handover_return_frees_pads(self):
        svc = self._svc("PERF_TRANSFER", "shop")
        svc.allocate("a", self.NEEDS, stretch_budget=0)
        delivered = svc.confirm_delivery("a")
        receipt = svc.handover_return("show-1", receipt_id="HR-1")
        self.assertTrue(receipt["on_time"])
        self.assertEqual(len(receipt["released"]), delivered["pairs_delivered"])
        self.assertEqual(svc.get_pad("PAD1")["physical_id"], None)


class AfterSalesConfirmationTests(unittest.TestCase):
    def test_confirm_return_stops_new_issuance(self):
        svc = open_service(":memory:")
        make_sale(svc, ["R1"])
        seed_shoe(svc, "R1", "R", width=2, stretch_to=3)
        svc.return_units("ret", "S-shoes", ["R1"], {"R1": "RESTOCK"})
        result = svc.confirm_return("conf", "ret", "S-shoes")
        self.assertEqual(result["affected_physicals"], [{"physical_id": "R1", "unit_id": "R1"}])
        self.assertFalse(svc.get_physical("R1")["usable"])
        self.assertFalse(svc.get_physical("R1")["consigned"])
        needs = [{"dancer_id": "x", "right": {"width": 2, "length": 5}}]
        self.assertEqual(svc.allocate("a", needs, stretch_budget=0)["count"], 0)

    def test_confirm_return_unknown_request(self):
        svc = open_service(":memory:")
        with self.assertRaises(BasketError) as ctx:
            svc.confirm_return("conf", "missing", "S")
        self.assertEqual(ctx.exception.code, "INVALID_RECEIPT")


class DancerTests(unittest.TestCase):
    def test_register_and_conflict(self):
        svc = open_service(":memory:")
        svc.register_dancer("jia", name="甲", left={"width": 2}, right={"width": 2, "length": 5}, run="r1")
        self.assertEqual(svc.get_dancer("jia")["name"], "甲")
        with self.assertRaises(BasketError) as ctx:
            svc.register_dancer("jia", left={"width": 3})
        self.assertEqual(ctx.exception.code, "DANCER_CONFLICT")

    def test_unknown_dancer(self):
        svc = open_service(":memory:")
        with self.assertRaises(BasketError) as ctx:
            svc.get_dancer("ghost")
        self.assertEqual(ctx.exception.code, "UNKNOWN_DANCER")


if __name__ == "__main__":
    unittest.main()


class PlannerIntegrationTests(unittest.TestCase):
    def test_troupe_plan_replay(self):
        svc = open_service(":memory:")
        steps = [
            {"op": "set_shop_mode", "mode": "PRESTRETCH"},
            {"op": "publish_snapshot", "products": PRODUCTS, "coupons": [], "snapshot_id": "s"},
            {
                "name": "q",
                "op": "quote",
                "basket": [{"unit_id": u, "sku": "DANCE", "price": 100} for u in ("LJ", "LY", "R1", "R2")],
                "coupons": [],
                "request_id": "q1",
                "snapshot_id": "s",
            },
            {"op": "register_sale", "sale_id": "S", "quote_request_id": "q1"},
            {"op": "recover_from_sale", "physical_id": "LJ", "side": "L", "sale_id": "S", "unit_id": "LJ", "width": 2, "length": 5, "stretch_to": 2},
            {"op": "recover_from_sale", "physical_id": "LY", "side": "L", "sale_id": "S", "unit_id": "LY", "width": 2, "length": 5, "stretch_to": 2},
            {"op": "recover_from_sale", "physical_id": "R1", "side": "R", "sale_id": "S", "unit_id": "R1", "width": 2, "length": 5, "stretch_to": 2},
            {"op": "recover_from_sale", "physical_id": "R2", "side": "R", "sale_id": "S", "unit_id": "R2", "width": 2, "length": 5, "stretch_to": 3},
            {"op": "consign", "physical_id": "LJ", "dancer_id": "o", "receipt_id": "c"},
            {"op": "consign", "physical_id": "LY", "dancer_id": "o", "receipt_id": "c"},
            {"op": "consign", "physical_id": "R1", "dancer_id": "o", "receipt_id": "c"},
            {"op": "consign", "physical_id": "R2", "dancer_id": "o", "receipt_id": "c"},
            {"op": "publish_fitting_slip", "slip_id": "sLJ", "physical_id": "LJ"},
            {"op": "publish_fitting_slip", "slip_id": "sLY", "physical_id": "LY"},
            {"op": "publish_fitting_slip", "slip_id": "sR1", "physical_id": "R1"},
            {"op": "publish_fitting_slip", "slip_id": "sR2", "physical_id": "R2"},
            {
                "name": "alloc",
                "op": "allocate",
                "allocation_id": "a1",
                "needs": [
                    {"dancer_id": "jia", "left": {"width": 2, "length": 5}, "right": {"width": 2, "length": 5}},
                    {"dancer_id": "yi", "left": {"width": 2, "length": 5}, "right": {"width": 3, "length": 5}},
                ],
                "stretch_budget": 1,
            },
            {"op": "confirm_delivery", "allocation_id": "a1", "run_until": "r-end"},
            {"op": "cabinet_view"},
        ]
        result = svc.run({"steps": steps})
        self.assertTrue(result["ok"])
        alloc = next(s for s in result["steps"] if s.get("name") == "alloc")
        self.assertEqual(alloc["result"]["count"], 2)
        view = result["steps"][-1]["result"]
        self.assertEqual(len(view["bindings"]), 4)
