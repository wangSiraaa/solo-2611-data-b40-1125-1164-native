"""Demo driver: builds a real persistent database and a JSON result record.

Run ``python3 -m basket_service demo`` to (re)generate::

    data/demo.db            real SQLite database
    results/demo_plan.json  the JSON plan that was executed for the main run
    results/demo_results.json  every quote, sale, return and independent check

All *expectations* in this file are produced by ``basket_service.reference``
(brute-force enumeration and Fraction arithmetic) and by reading the raw
ledger records, never by calling the production optimizer/refund functions to
compute the expected answer.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

from . import open_service
from .errors import BasketError
from .reference import exhaustive_best, expected_allocation, expected_return
from .validation import validate_basket, validate_coupons

ROOT = Path(__file__).resolve().parent.parent


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _brute(basket, coupons):
    units = validate_basket(basket)
    canonical = validate_coupons(coupons, {u["sku"] for u in units})
    total, used, unit_lists, mapping = exhaustive_best(units, canonical)
    allocation = expected_allocation(units, mapping, {c["coupon_id"]: c["D"] for c in canonical})
    return total, mapping, allocation


def _case_material() -> Dict[str, Any]:
    case = json.loads((ROOT / "case.json").read_text(encoding="utf-8"))
    basket = case["basket"]
    before = case["before_coupons"]
    added = case["added_coupons"]
    return {"basket": basket, "before": before, "added": added, "return_request": case["return_request"]}


def _run_troupe_scenarios(svc) -> Dict[str, Any]:
    """Drive the brief's fitting / cabinet / delivery narrative.

    Most scenarios run on their own in-memory service so physical shoes from one
    scenario never leak into another.  The final scenario runs on the shared
    ``svc`` so the database-reopen check can assert troupe persistence.  Every
    quoted pair count is also cross-checked against the independent
    ``max_pairs_bruteforce`` reference, never against the planner's own output.
    """

    from .reference import max_pairs_bruteforce

    out: Dict[str, Any] = {"scenarios": {}, "checks": []}
    checks = out["checks"]

    def add_check(name, ok, expected, actual, **extra):
        checks.append({"check": name, "ok": bool(ok), "expected": expected, "actual": actual, **extra})

    products = [{"sku": "DANCE", "price": 100}]

    def new_sale(service, prefix, unit_ids):
        snap = service.publish_snapshot(products, [], snapshot_id=f"troupe-snap-{prefix}")
        basket = [{"unit_id": u, "sku": "DANCE", "price": 100} for u in unit_ids]
        quote = service.quote(basket, [], f"troupe-q-{prefix}", snap)
        return service.register_sale(f"troupe-sale-{prefix}", quote=quote)

    def seed(service, physical_id, side, *, width, stretch_to, length=5, sale, origin="owner"):
        service.recover_from_sale(physical_id, side, sale, physical_id, width=width, length=length, stretch_to=stretch_to)
        service.consign(physical_id, origin, f"consent-{physical_id}")
        service.publish_fitting_slip(f"troupe-slip-{physical_id}", physical_id)

    def shoes_view(service, ids):
        return [
            {
                "physical_id": p["physical_id"],
                "side": p["side"],
                "width": p["width"],
                "stretch_to": p["stretch_to"],
                "directions": p["directions"],
                "length": p["length"],
                "pad_step": 1,
            }
            for p in service.list_physicals()
            if p["physical_id"] in ids
        ]

    # -- Pair scenario: 甲乙 lefts are dedicated; right feet suit widths 2 & 3.
    pair = open_service(":memory:")
    new_sale(pair, "pair", ["LJ", "LY", "R1", "R2"])
    seed(pair, "LJ", "L", width=2, stretch_to=2, sale="troupe-sale-pair")
    seed(pair, "LY", "L", width=2, stretch_to=2, sale="troupe-sale-pair")
    seed(pair, "R1", "R", width=2, stretch_to=2, sale="troupe-sale-pair")
    seed(pair, "R2", "R", width=2, stretch_to=3, sale="troupe-sale-pair")
    pair_ids = {"LJ", "LY", "R1", "R2"}
    needs = [
        {"dancer_id": "jia", "left": {"width": 2, "length": 5}, "right": {"width": 2, "length": 5}},
        {"dancer_id": "yi", "left": {"width": 2, "length": 5}, "right": {"width": 3, "length": 5}},
    ]

    with_stretch = pair.allocate("troupe-pair-1", needs, run="pair-run", stretch_budget=1)
    independent_with = max_pairs_bruteforce(shoes_view(pair, pair_ids), needs, stretch_budget=1)
    out["scenarios"]["pair_with_stretch"] = with_stretch
    add_check("troupe.pair.one_stretch_two_pairs", with_stretch["count"] == 2 == independent_with, 2, with_stretch["count"])

    # "只把 R2 的试楦结果改为不能再撑" -> only one pair.
    pair.update_fitting("R2", stretch_to=2)
    pair.publish_fitting_slip("troupe-slip-R2", "R2")
    r2_blocked = pair.allocate("troupe-pair-2", needs, run="pair-run-2", stretch_budget=1)
    independent_blocked = max_pairs_bruteforce(shoes_view(pair, pair_ids), needs, stretch_budget=1)
    out["scenarios"]["pair_r2_cannot_stretch"] = r2_blocked
    add_check("troupe.pair.r2_cannot_stretch_one_pair", r2_blocked["count"] == 1 == independent_blocked, 1, r2_blocked["count"])

    # Restore the fitting, then hand R2's run occupancy to the prior shift.
    pair.update_fitting("R2", stretch_to=3)
    pair.publish_fitting_slip("troupe-slip-R2", "R2")
    pair.assign_occupancy("R2", "prior-shift")
    occupied = pair.allocate("troupe-pair-3", needs, run="current-shift", stretch_budget=1)
    out["scenarios"]["pair_r2_occupied_by_prior_shift"] = occupied
    add_check("troupe.pair.occupancy_handoff_one_pair", occupied["count"] == 1, 1, occupied["count"])
    add_check(
        "troupe.pair.occupancy_excludes_r2",
        {"physical_id": "R2", "reason": "occupied by another run"} in occupied["excluded"],
        True,
        occupied["excluded"],
    )
    # Bookkeeping occupancy is not a width: clear it, R2 is still physically width 2.
    pair.release_occupancy("R2", "prior-shift")
    cleared = pair.allocate("troupe-pair-4", needs, run="current-shift", stretch_budget=0)
    r2_width_before = pair.get_physical("R2")["width"]
    out["scenarios"]["pair_after_clearing_occupancy_no_stretch"] = cleared
    add_check(
        "troupe.pair.clearing_occupancy_does_not_change_width",
        r2_width_before == 2 and cleared["count"] == 1,
        2,
        r2_width_before,
        pair_count=cleared["count"],
    )
    # Stretch R2 to 3 for real; that width is irreversible.
    pair.stretch("R2", 3)
    try:
        pair.stretch("R2", 2)
        shrink_blocked = False
    except BasketError as exc:
        shrink_blocked = exc.code == "WIDTH_IRREVERSIBLE"
    add_check("troupe.width.irreversible", pair.get_physical("R2")["width"] == 3 and shrink_blocked, True, shrink_blocked)
    # "不能靠清空账面占用把宽3恢复成宽2": clearing occupancy again still won't shrink it.
    pair.assign_occupancy("R2", "x")
    pair.release_occupancy("R2", "x")
    try:
        pair.stretch("R2", 2)
        still_blocked = False
    except BasketError as exc:
        still_blocked = exc.code == "WIDTH_IRREVERSIBLE"
    add_check("troupe.width.occupancy_cannot_unstretch", still_blocked and pair.get_physical("R2")["width"] == 3, True, still_blocked)

    # Deliver the two pairs; binding + laces persist; held shoes are not re-promised.
    delivered = pair.confirm_delivery("troupe-pair-1", run_until="pair-run-end")
    out["scenarios"]["pair_delivery"] = delivered
    add_check("troupe.delivery.two_pairs", delivered["pairs_delivered"] == 2, 2, delivered["pairs_delivered"])
    add_check("troupe.delivery.laces_tied", all(b["tied_laces"] for b in delivered["bindings"]), True, [b["tied_laces"] for b in delivered["bindings"]])
    realloc = pair.allocate("troupe-pair-realloc", needs, run="pair-run", stretch_budget=1)
    add_check("troupe.binding.held_shoe_not_reallocated", realloc["count"] == 0, 0, realloc["count"])
    pair.return_delivery(delivered["bindings"][0]["binding_id"], on_time=True, receipt_id="pair-return-1")
    del pair

    # -- Three-dancer pad scenario (甲乙丙) on its own service.
    show2_needs = [
        {"dancer_id": "yi", "left": None, "right": {"width": 2, "length": 5}},
        {"dancer_id": "bing", "left": None, "right": {"width": 2, "length": 5}},
    ]

    def seed_pad_world(prefix, mode, pad_holder):
        world = open_service(":memory:")
        world.set_shop_mode(mode)
        ids = {"JL": f"{prefix}-JL", "YL": f"{prefix}-YL", "BL": f"{prefix}-BL", "R": f"{prefix}-R", "BR": f"{prefix}-BR", "PAD": f"{prefix}-PAD"}
        new_sale(world, prefix, list(ids.values())[:5])
        seed(world, ids["JL"], "L", width=2, stretch_to=2, sale=f"troupe-sale-{prefix}")
        seed(world, ids["YL"], "L", width=2, stretch_to=2, sale=f"troupe-sale-{prefix}")
        seed(world, ids["BL"], "L", width=2, stretch_to=2, sale=f"troupe-sale-{prefix}")
        seed(world, ids["R"], "R", width=2, stretch_to=3, sale=f"troupe-sale-{prefix}")
        world.stretch(ids["R"], 3)
        seed(world, ids["BR"], "R", width=3, stretch_to=3, sale=f"troupe-sale-{prefix}")
        world.register_pad(ids["PAD"], holder=pad_holder)
        return world, ids

    # PRESTRETCH counter holds its own pad.
    world, ids = seed_pad_world("padown", "PRESTRETCH", "shop")
    own = world.allocate("troupe-pad-own", show2_needs, run="show-2", stretch_budget=1)
    out["scenarios"]["pad_prestretch_own"] = own
    pad_positions = [p["dancer_id"] for p in own["pairs"] if p.get("R", {}).get("pad_id")]
    add_check("troupe.pad.prestretch_own_one_pair", own["count"] == 1, 1, own["count"])
    add_check("troupe.pad.single_pad_used_once", len(pad_positions) == 1, 1, pad_positions)
    del world

    # 丙 already borrowed the pad from the troupe; counter receives the receipt.
    world, ids = seed_pad_world("padborrow", "PERF_TRANSFER", "bing")
    try:
        world.acquire_borrowable_pad("troupe-acq-blocked", ids["PAD"], "EXT-RECEIPT-0")
        blocked = None
    except BasketError as exc:
        blocked = exc.code
    add_check("troupe.pad.cannot_acquire_while_borrowed", blocked == "INVALID_RECEIPT", "INVALID_RECEIPT", blocked)
    # Replay: 丙 returns it to the troupe before 乙 signs.
    world.record_pad_receipt("troupe-pad-return", ids["PAD"], "bing", "troupe", "RET-EXT-1")
    world.acquire_borrowable_pad("troupe-acq", ids["PAD"], "EXT-RECEIPT-1")
    ledger = world.pad_ledger(ids["PAD"])
    transfer_ok = world.allocate("troupe-pad-transfer", show2_needs, run="show-2", stretch_budget=1)
    out["scenarios"]["pad_transfer_replay"] = {"pad_ledger": ledger, "allocation": transfer_ok}
    add_check(
        "troupe.pad.return_then_acquire_enables_one_pair",
        transfer_ok["count"] == 1
        and [t["direction"] for t in ledger][-2:] == ["MOVE", "ACQUIRE"]
        and ledger[-1]["receipt_id"] == "EXT-RECEIPT-1",
        1,
        transfer_ok["count"],
    )
    del world

    # PRESTRETCH counter refuses the troupe pad business.
    world, ids = seed_pad_world("padrefuse", "PRESTRETCH", "troupe")
    try:
        world.acquire_borrowable_pad("troupe-acq-refused", ids["PAD"], "EXT")
        refused = None
    except BasketError as exc:
        refused = exc.code
    refused_alloc = world.allocate("troupe-pad-refused", show2_needs, run="show-2", stretch_budget=1)
    out["scenarios"]["pad_prestretch_refuses_transfer"] = {"error": refused, "allocation": refused_alloc}
    add_check("troupe.pad.prestretch_refuses_transfer", refused == "INVALID_SHOP_MODE" and refused_alloc["count"] == 0, "INVALID_SHOP_MODE", refused)
    del world

    # -- 甲's first show: sign out the pre-stretched width-3 right and return it
    #    on time; the physical width remains 3 forever after.
    jia = open_service(":memory:")
    new_sale(jia, "jia", ["JIA-R"])
    seed(jia, "JIA-R", "R", width=2, stretch_to=3, sale="troupe-sale-jia")
    jia.stretch("JIA-R", 3)
    jia_alloc = jia.allocate("troupe-jia-r", [{"dancer_id": "jia", "left": None, "right": {"width": 3, "length": 5}}], run="show-1", stretch_budget=0)
    jia_delivery = jia.confirm_delivery("troupe-jia-r", run_until="show-1-end")
    jia_return = jia.return_delivery(jia_delivery["bindings"][0]["binding_id"], on_time=True, receipt_id="JIA-RETURN")
    out["scenarios"]["jia_first_show"] = {"allocation": jia_alloc, "delivery": jia_delivery, "return": jia_return}
    add_check(
        "troupe.jia.width3_signed_and_returned_kept",
        jia_alloc["count"] == 1 and jia_return["on_time"] and jia.get_physical("JIA-R")["width"] == 3,
        True,
        {"count": jia_alloc["count"], "width": jia.get_physical("JIA-R")["width"]},
    )
    del jia

    # -- Late after-sales return of an original order, walked through the real
    #    sale -> fitting -> confirmed delivery -> after-sales entries, with a
    #    small (<3 dancer) order reopened.
    late = open_service(":memory:")
    late.set_shop_mode("PRESTRETCH")
    late_sale = new_sale(late, "late", ["LZ1", "LZ2", "RZ1", "RZ2"])
    seed(late, "LZ1", "L", width=2, stretch_to=2, sale="troupe-sale-late")
    seed(late, "LZ2", "L", width=2, stretch_to=2, sale="troupe-sale-late")
    seed(late, "RZ1", "R", width=2, stretch_to=2, sale="troupe-sale-late")
    seed(late, "RZ2", "R", width=2, stretch_to=3, sale="troupe-sale-late")
    late_needs = [
        {"dancer_id": "z1", "left": {"width": 2, "length": 5}, "right": {"width": 2, "length": 5}},
        {"dancer_id": "z2", "left": {"width": 2, "length": 5}, "right": {"width": 3, "length": 5}},
    ]
    before_late = late.allocate("troupe-late-before", late_needs, run="late-run", stretch_budget=1)
    late_delivery = late.confirm_delivery("troupe-late-before", run_until="late-run-end")
    # The troupe reopens the original order late and returns unit RZ2 (乙's shoe).
    ret_late = late.return_units("troupe-late-return", "troupe-sale-late", ["RZ2"], {"RZ2": "RESTOCK"})
    conf_late = late.confirm_return("troupe-late-conf", "troupe-late-return", "troupe-sale-late")
    # A fresh shift after the return cannot source that shoe for a new dancer.
    after_late = late.allocate(
        "troupe-late-after",
        [{"dancer_id": "z3", "left": None, "right": {"width": 3, "length": 5}}],
        run="late-run-2",
        stretch_budget=0,
    )
    out["scenarios"]["late_after_sales_return"] = {
        "ledger_paid": {u["unit_id"]: u["paid"] for u in late_sale["units"]},
        "before": before_late,
        "delivery": late_delivery,
        "ledger_return": ret_late,
        "confirmation": conf_late,
        "after": after_late,
    }
    add_check(
        "troupe.aftersales.late_return_stops_new_issuance",
        before_late["count"] == 2
        and late_delivery["pairs_delivered"] == 2
        and after_late["count"] == 0
        and conf_late["affected_physicals"] == [{"physical_id": "RZ2", "unit_id": "RZ2"}]
        and ret_late["refund_total"] == next(u["paid"] for u in late_sale["units"] if u["unit_id"] == "RZ2"),
        {"before": 2, "delivered": 2, "after": 0},
        {"before": before_late["count"], "delivered": late_delivery["pairs_delivered"], "after": after_late["count"]},
    )
    del late

    # -- Integrated scenario on the shared session DB (persistence / reopen).
    #    Three dancers, two deliverable complete pairs from the shared cabinet.
    new_sale(svc, "integrated", ["ILJ", "ILY", "IR1", "IR2"])
    seed(svc, "ILJ", "L", width=2, stretch_to=2, sale="troupe-sale-integrated")
    seed(svc, "ILY", "L", width=2, stretch_to=2, sale="troupe-sale-integrated")
    seed(svc, "IR1", "R", width=2, stretch_to=2, sale="troupe-sale-integrated")
    seed(svc, "IR2", "R", width=2, stretch_to=3, sale="troupe-sale-integrated")
    svc.set_shop_mode("PRESTRETCH")
    integ_needs = [
        {"dancer_id": "jia", "left": {"width": 2, "length": 5}, "right": {"width": 2, "length": 5}},
        {"dancer_id": "yi", "left": {"width": 2, "length": 5}, "right": {"width": 3, "length": 5}},
    ]
    integ = svc.allocate("troupe-integrated", integ_needs, run="integrated-run", stretch_budget=1)
    integ_delivery = svc.confirm_delivery("troupe-integrated", run_until="integrated-run-end")
    out["scenarios"]["integrated_persistence"] = {"allocation": integ, "delivery": integ_delivery}
    add_check(
        "troupe.integrated.two_pairs_delivered",
        integ["count"] == 2 and integ_delivery["pairs_delivered"] == 2,
        2,
        integ_delivery["pairs_delivered"],
    )

    out["scenarios"]["final_cabinet"] = svc.cabinet_view()
    return out


def run_demo(db_path: str, out_path: str, plan_path: str) -> Dict[str, Any]:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(db_path + suffix)
        if candidate.exists():
            candidate.unlink()

    svc = open_service(db_path)
    results: Dict[str, Any] = {"database": str(db_path), "scenarios": {}, "checks": []}

    # ------------------------------------------------------------------
    # 1. Case material: two snapshots, quote under both, bind two sales.
    # ------------------------------------------------------------------
    material = _case_material()
    basket = material["basket"]
    snap_before = svc.publish_snapshot(
        [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}],
        material["before"],
        snapshot_id="case-before",
    )
    snap_added = svc.publish_snapshot(
        [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}],
        material["before"] + material["added"],
        snapshot_id="case-added",
    )
    quote_before = svc.quote(basket, [], "case-q-before", snap_before)
    quote_added = svc.quote(basket, [], "case-q-added", snap_added)
    brute_before = _brute(basket, material["before"])
    brute_added = _brute(basket, material["before"] + material["added"])
    results["scenarios"]["case_material"] = {
        "snapshots": {"before": snap_before, "added": snap_added},
        "quote_before": quote_before,
        "quote_added": quote_added,
        "independent_before": {"total": brute_before[0], "assignments": brute_before[1], "allocation": brute_before[2]},
        "independent_added": {"total": brute_added[0], "assignments": brute_added[1], "allocation": brute_added[2]},
    }
    results["checks"].append({
        "check": "case.before.total_matches_bruteforce",
        "ok": quote_before["total"] == brute_before[0],
        "expected": brute_before[0],
        "actual": quote_before["total"],
    })
    results["checks"].append({
        "check": "case.added.total_matches_bruteforce",
        "ok": quote_added["total"] == brute_added[0],
        "expected": brute_added[0],
        "actual": quote_added["total"],
    })
    results["checks"].append({
        "check": "old_quote_unchanged_after_new_snapshot",
        "ok": svc.get_quote("case-q-before") == quote_before,
        "expected": True,
        "actual": svc.get_quote("case-q-before") == quote_before,
    })

    # Two independent legal sales, different promotions -> different paid.
    sale_before = svc.register_sale("S1", quote=quote_before)
    sale_added = svc.register_sale("S2", quote=quote_added)
    results["scenarios"]["two_sales"] = {"S1": sale_before, "S2": sale_added}

    # Returns follow their own bound quote, not the newest promotion.
    ret_before_b = svc.return_units(material["return_request"]["request_id"], "S1", ["b"], {"b": "RESTOCK"})
    ret_added_b = svc.return_units("ret-added-b", "S2", ["b"], {"b": "SCRAP"})
    paid_before = {u["unit_id"]: u["paid"] for u in sale_before["units"]}
    paid_added = {u["unit_id"]: u["paid"] for u in sale_added["units"]}
    results["scenarios"]["bound_quote_refunds"] = {
        "case-return-request": ret_before_b,
        "ret-added-b": ret_added_b,
    }
    results["checks"].append({
        "check": "old_sale_refund_uses_old_quote",
        "ok": ret_before_b["refund_total"] == expected_return(paid_before, ["b"])[0],
        "expected": expected_return(paid_before, ["b"])[0],
        "actual": ret_before_b["refund_total"],
    })
    results["checks"].append({
        "check": "new_sale_refund_uses_new_quote",
        "ok": ret_added_b["refund_total"] == expected_return(paid_added, ["b"])[0],
        "expected": expected_return(paid_added, ["b"])[0],
        "actual": ret_added_b["refund_total"],
    })

    # Same sale_id with the other quote must be rejected; the old identity is
    # still a legitimate, different sale rather than a retry.
    try:
        svc.register_sale("S1", quote=quote_added)
        rebind_ok = False
        rebind_error = None
    except BasketError as exc:
        rebind_ok = True
        rebind_error = exc.code
    results["scenarios"]["sale_conflict"] = {"rejected": rebind_ok, "error": rebind_error}

    # Promotion switch: publish a third, stronger snapshot and quote again; the
    # old sale still refunds from its own binding.
    third_coupons = material["before"] + material["added"] + [
        {"coupon_id": "ABC", "D": 200, "requires": {"A": 1, "B": 1, "C": 1}}
    ]
    snap_third = svc.publish_snapshot(
        [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}],
        third_coupons,
        snapshot_id="case-third",
    )
    quote_third = svc.quote(basket, [], "case-q-third", snap_third)
    ret_before_a = svc.return_units("ret-before-a", "S1", ["a"], {"a": "SCRAP"})
    results["scenarios"]["promotion_switch"] = {
        "snapshot": snap_third,
        "quote": quote_third,
        "old_sale_refund_a_after_switch": ret_before_a,
    }
    results["checks"].append({
        "check": "post_switch_old_sale_refund_follows_old_paid",
        "ok": ret_before_a["refund_total"] == paid_before["a"],
        "expected": paid_before["a"],
        "actual": ret_before_a["refund_total"],
    })

    # ------------------------------------------------------------------
    # 2. Optimizer trap and tie-break examples.
    # ------------------------------------------------------------------
    greedy_basket = [
        {"unit_id": "a", "sku": "A", "price": 100},
        {"unit_id": "b", "sku": "B", "price": 100},
        {"unit_id": "c", "sku": "C", "price": 100},
        {"unit_id": "d", "sku": "D", "price": 100},
    ]
    greedy_coupons = [
        {"coupon_id": "COMBINED", "D": 190, "requires": {"A": 1, "B": 1, "C": 1, "D": 1}},
        {"coupon_id": "AB", "D": 120, "requires": {"A": 1, "B": 1}},
        {"coupon_id": "CD", "D": 120, "requires": {"C": 1, "D": 1}},
    ]
    gsnap = svc.publish_snapshot(None, greedy_coupons)
    greedy_quote = svc.quote(greedy_basket, [], "greedy-quote", gsnap)
    greedy_brute = _brute(greedy_basket, greedy_coupons)
    results["scenarios"]["greedy_trap"] = {
        "basket": greedy_basket,
        "coupons": greedy_coupons,
        "largest_D_first_total": 400 - 190,
        "optimal_quote": greedy_quote,
        "independent": {"total": greedy_brute[0], "assignments": greedy_brute[1], "allocation": greedy_brute[2]},
    }
    results["checks"].append({
        "check": "greedy_largest_d_is_not_optimal",
        "ok": greedy_quote["total"] < 400 - 190 and greedy_quote["total"] == greedy_brute[0],
        "expected": greedy_brute[0],
        "actual": greedy_quote["total"],
        "greedy_total": 400 - 190,
    })

    contention_basket = [
        {"unit_id": "a", "sku": "A", "price": 100},
        {"unit_id": "b", "sku": "B", "price": 100},
    ]
    contention_coupons = [
        {"coupon_id": "AB1", "D": 120, "requires": {"A": 1, "B": 1}},
        {"coupon_id": "AB2", "D": 110, "requires": {"A": 1, "B": 1}},
    ]
    csnap = svc.publish_snapshot(None, contention_coupons)
    contention_quote = svc.quote(contention_basket, [], "contention-quote", csnap)
    results["scenarios"]["contention"] = {
        "quote": contention_quote,
        "independent": _brute(contention_basket, contention_coupons)[:2],
    }

    price_basket = [
        {"unit_id": "cheap", "sku": "A", "price": 30},
        {"unit_id": "dear", "sku": "A", "price": 300},
        {"unit_id": "mid", "sku": "A", "price": 120},
    ]
    price_coupons = [
        {"coupon_id": "big", "D": 250, "requires": {"A": 1}},
        {"coupon_id": "small1", "D": 60, "requires": {"A": 1}},
        {"coupon_id": "small2", "D": 50, "requires": {"A": 1}},
    ]
    psnap = svc.publish_snapshot(None, price_coupons)
    price_quote = svc.quote(price_basket, [], "price-quote", psnap)
    results["scenarios"]["same_sku_different_prices"] = {
        "quote": price_quote,
        "independent": _brute(price_basket, price_coupons)[:2],
    }

    tie_basket = [
        {"unit_id": "a", "sku": "A", "price": 100},
        {"unit_id": "b", "sku": "A", "price": 100},
        {"unit_id": "c", "sku": "A", "price": 100},
    ]
    tie_coupons = [
        {"coupon_id": "z", "D": 50, "requires": {"A": 1}},
        {"coupon_id": "y", "D": 50, "requires": {"A": 1}},
        {"coupon_id": "x", "D": 50, "requires": {"A": 1}},
    ]
    tsnap = svc.publish_snapshot(None, tie_coupons)
    tie_quote = svc.quote(tie_basket, [], "tie-quote", tsnap)
    results["scenarios"]["two_level_tie_break"] = {
        "quote": tie_quote,
        "independent": _brute(tie_basket, tie_coupons)[:2],
    }
    results["checks"].append({
        "check": "tie_break_matches_bruteforce",
        "ok": tie_quote["assignments"] == _brute(tie_basket, tie_coupons)[1],
        "expected": _brute(tie_basket, tie_coupons)[1],
        "actual": tie_quote["assignments"],
    })

    remainder_basket = [
        {"unit_id": "z", "sku": "A", "price": 100},
        {"unit_id": "a", "sku": "A", "price": 100},
        {"unit_id": "m", "sku": "A", "price": 100},
        {"unit_id": "low", "sku": "A", "price": 1},
    ]
    remainder_coupons = [{"coupon_id": "third", "D": 100, "requires": {"A": 3}}]
    rsnap = svc.publish_snapshot(None, remainder_coupons)
    remainder_quote = svc.quote(remainder_basket, [], "remainder-quote", rsnap)
    results["scenarios"]["proportional_remainder"] = {
        "quote": remainder_quote,
        "independent": _brute(remainder_basket, remainder_coupons),
    }
    results["checks"].append({
        "check": "proportional_allocation_matches_fraction_reference",
        "ok": remainder_quote["allocation"] == _brute(remainder_basket, remainder_coupons)[2],
        "expected": _brute(remainder_basket, remainder_coupons)[2],
        "actual": remainder_quote["allocation"],
    })

    # ------------------------------------------------------------------
    # 3. Ten-unit basket: independent full enumeration + ledger audit.
    # ------------------------------------------------------------------
    ten_basket = [
        {"unit_id": "u0", "sku": "A", "price": 100},
        {"unit_id": "u1", "sku": "A", "price": 30},
        {"unit_id": "u2", "sku": "B", "price": 70},
        {"unit_id": "u3", "sku": "B", "price": 45},
        {"unit_id": "u4", "sku": "C", "price": 90},
        {"unit_id": "u5", "sku": "C", "price": 20},
        {"unit_id": "u6", "sku": "D", "price": 60},
        {"unit_id": "u7", "sku": "D", "price": 10},
        {"unit_id": "u8", "sku": "E", "price": 55},
        {"unit_id": "u9", "sku": "F", "price": 5},
    ]
    ten_coupons = [
        {"coupon_id": "c0", "D": 40, "requires": {"A": 1}},
        {"coupon_id": "c1", "D": 50, "requires": {"B": 1, "C": 1}},
        {"coupon_id": "c2", "D": 35, "requires": {"D": 2}},
        {"coupon_id": "c3", "D": 60, "requires": {"A": 2}},
        {"coupon_id": "c4", "D": 70, "requires": {"C": 1, "D": 1, "E": 1}},
        {"coupon_id": "c5", "D": 25, "requires": {"F": 1}},
        {"coupon_id": "c6", "D": 45, "requires": {"B": 2}},
        {"coupon_id": "c7", "D": 30, "requires": {"E": 1, "F": 1}},
    ]
    ten_snap = svc.publish_snapshot(None, ten_coupons)
    ten_quote = svc.quote(ten_basket, [], "ten-quote", ten_snap)
    ten_brute = _brute(ten_basket, ten_coupons)
    ten_sale = svc.register_sale("S-ten", quote=ten_quote)
    returned_units = ["u1", "u5", "u9", "u3"]
    grades = {"u1": "RESTOCK", "u5": "SCRAP", "u9": "RESTOCK", "u3": "RESTOCK"}
    ten_return = svc.return_units("ret-ten", "S-ten", returned_units, grades)
    paid = {row["unit_id"]: row["paid"] for row in ten_brute[2]}
    expected_refund, expected_per_unit = expected_return(paid, returned_units)
    results["scenarios"]["ten_unit"] = {
        "basket": ten_basket,
        "coupons": ten_coupons,
        "quote": ten_quote,
        "independent": {"total": ten_brute[0], "assignments": ten_brute[1], "allocation": ten_brute[2]},
        "sale": ten_sale,
        "return": ten_return,
        "independent_refund": {"total": expected_refund, "per_unit": expected_per_unit},
    }
    results["checks"].append({
        "check": "ten_unit_total_matches_full_enumeration",
        "ok": ten_quote["total"] == ten_brute[0] and ten_quote["assignments"] == ten_brute[1],
        "expected": ten_brute[0],
        "actual": ten_quote["total"],
    })
    results["checks"].append({
        "check": "ten_unit_return_matches_original_paid",
        "ok": ten_return["refund_total"] == expected_refund,
        "expected": expected_refund,
        "actual": ten_return["refund_total"],
    })

    # Independent ledger audit: reconstruct from sale_units + return_items, not
    # from the service's refund function.
    raw = svc.db
    with raw.connection() as conn:
        unit_rows = conn.execute(
            "SELECT unit_id, sku, paid, returned, grade FROM sale_units WHERE sale_id = ? ORDER BY unit_id", ("S-ten",)
        ).fetchall()
        return_rows = conn.execute(
            "SELECT ri.unit_id, ri.grade, ri.refund FROM return_items ri JOIN return_requests rr ON rr.request_id = ri.request_id WHERE rr.sale_id = ? ORDER BY ri.unit_id",
            ("S-ten",),
        ).fetchall()
        inv_rows = conn.execute("SELECT sku, bucket, SUM(delta) AS qty FROM inventory_events GROUP BY sku, bucket").fetchall()
    independent_ledger = {
        "sale_units": [dict(r) for r in unit_rows],
        "return_items": [dict(r) for r in return_rows],
        "inventory_from_raw_events": [dict(r) for r in inv_rows],
        "per_unit_paid_sum": sum(r["paid"] for r in unit_rows),
        "returned_sum": sum(r["refund"] for r in return_rows),
        "remaining": [r["unit_id"] for r in unit_rows if not r["returned"]],
    }
    results["scenarios"]["ten_unit"]["independent_ledger_audit"] = independent_ledger
    results["checks"].append({
        "check": "ten_unit_ledger_audit_per_unit_paid_sums_to_quote",
        "ok": independent_ledger["per_unit_paid_sum"] == ten_quote["total"],
        "expected": ten_quote["total"],
        "actual": independent_ledger["per_unit_paid_sum"],
    })
    results["checks"].append({
        "check": "ten_unit_ledger_audit_refund_sums_to_return",
        "ok": independent_ledger["returned_sum"] == ten_return["refund_total"],
        "expected": ten_return["refund_total"],
        "actual": independent_ledger["returned_sum"],
    })

    # ------------------------------------------------------------------
    # 4. Retry, overlap and independent stock movements.
    # ------------------------------------------------------------------
    lost_response_retry = svc.return_units("ret-ten", "S-ten", returned_units, grades)
    results["scenarios"]["lost_response_retry"] = lost_response_retry
    results["checks"].append({
        "check": "retry_replays_first_result",
        "ok": lost_response_retry == ten_return,
        "expected": ten_return,
        "actual": lost_response_retry,
    })

    try:
        svc.return_units("overlap", "S-ten", ["u0", "u1"], {"u0": "RESTOCK", "u1": "RESTOCK"})
        overlap = {"rejected": False}
    except BasketError as exc:
        overlap = {"rejected": True, "error": exc.code}
    results["scenarios"]["overlap_rejected"] = overlap
    results["checks"].append({
        "check": "overlap_rejects_whole_request_and_keeps_u0_refundable",
        "ok": overlap["rejected"] and "u0" in svc.get_sale("S-ten")["remaining_refundable"],
        "expected": True,
        "actual": overlap["rejected"],
    })

    adj = svc.adjust_inventory("adj-A-1", "A", 4, "RESTOCK")
    adj_scrap = svc.adjust_inventory("adj-B-1", "B", 2, "SCRAP")
    results["scenarios"]["adjustments"] = {"restock": adj, "scrap": adj_scrap}
    results["checks"].append({
        "check": "adjustment_does_not_change_refundable",
        "ok": "u0" in svc.get_sale("S-ten")["remaining_refundable"],
        "expected": True,
        "actual": "u0" in svc.get_sale("S-ten")["remaining_refundable"],
    })

    # ------------------------------------------------------------------
    # 4b. Troupe fitting, cabinet and paired delivery.
    # ------------------------------------------------------------------
    troupe = _run_troupe_scenarios(svc)
    results["scenarios"]["troupe"] = troupe
    for check in troupe["checks"]:
        results["checks"].append(check)

    # ------------------------------------------------------------------
    # 5. Reopen the database and keep all state.
    # ------------------------------------------------------------------
    results["scenarios"]["before_reopen"] = {
        "inventory": svc.inventory(),
        "sale_ten": svc.get_sale("S-ten"),
        "audit_sales": svc.audit_sales(),
        "audit_inventory": svc.audit_inventory(),
        "cabinet": svc.cabinet_view(),
    }
    del svc
    reopened = open_service(db_path)
    results["scenarios"]["after_reopen"] = {
        "get_quote_case": reopened.get_quote("case-q-before"),
        "get_sale_ten": reopened.get_sale("S-ten"),
        "get_return_ten": reopened.get_return("ret-ten"),
        "inventory": reopened.inventory(),
        "audit_sales": reopened.audit_sales(),
        "audit_inventory": reopened.audit_inventory(),
        "cabinet": reopened.cabinet_view(),
    }
    results["checks"].append({
        "check": "reopen_preserves_quote_sale_return_inventory",
        "ok": (
            reopened.get_quote("case-q-before") == quote_before
            and reopened.get_sale("S-ten")["remaining_refundable"] == ["u0", "u2", "u4", "u6", "u7", "u8"]
            and reopened.get_return("ret-ten")["refund_total"] == ten_return["refund_total"]
            and reopened.inventory() == results["scenarios"]["before_reopen"]["inventory"]
        ),
        "expected": "identical",
        "actual": "identical"
        if (
            reopened.get_quote("case-q-before") == quote_before
            and reopened.get_sale("S-ten")["remaining_refundable"] == ["u0", "u2", "u4", "u6", "u7", "u8"]
            and reopened.get_return("ret-ten")["refund_total"] == ten_return["refund_total"]
            and reopened.inventory() == results["scenarios"]["before_reopen"]["inventory"]
        )
        else "differs",
    })
    before_cabinet = results["scenarios"]["before_reopen"]["cabinet"]
    after_cabinet = results["scenarios"]["after_reopen"]["cabinet"]
    # Compare the durable facts (ignore ordering-stable dict identity of full blobs).
    reopen_bindings_ok = [(b["binding_id"], b["width_delivered"], b["tied_laces"], b["pad_id"]) for b in before_cabinet["bindings"]] == [
        (b["binding_id"], b["width_delivered"], b["tied_laces"], b["pad_id"]) for b in after_cabinet["bindings"]
    ]
    reopen_widths_ok = [(p["physical_id"], p["width"]) for p in before_cabinet["physicals"]] == [
        (p["physical_id"], p["width"]) for p in after_cabinet["physicals"]
    ]
    results["checks"].append({
        "check": "reopen_preserves_cabinet_widths_laces_bindings_pads",
        "ok": reopen_bindings_ok and reopen_widths_ok,
        "expected": True,
        "actual": reopen_bindings_ok and reopen_widths_ok,
    })

    # ------------------------------------------------------------------
    # 6. Independent quote issuer -> ledger-only consumer.
    # ------------------------------------------------------------------
    ledger_path = str(Path(db_path).with_suffix(".ledger.db"))
    for suffix in ("", "-wal", "-shm"):
        stale = Path(ledger_path + suffix)
        if stale.exists():
            stale.unlink()
    ledger_only = open_service(ledger_path)
    consumer_sale = ledger_only.register_sale("S-independent", quote=quote_added)
    consumer_return = ledger_only.return_units("ret-independent", "S-independent", ["c"], {"c": "RESTOCK"})
    results["scenarios"]["independent_quote_consumer"] = {
        "sale": consumer_sale,
        "return": consumer_return,
        "quote_came_from": "case-q-added (validated payload; ledger never re-optimised)",
    }
    results["checks"].append({
        "check": "ledger_consumes_complete_external_quote",
        "ok": consumer_sale["total"] == quote_added["total"]
        and consumer_return["refund_total"] == expected_return(paid_added, ["c"])[0],
        "expected": expected_return(paid_added, ["c"])[0],
        "actual": consumer_return["refund_total"],
    })
    del ledger_only

    # ------------------------------------------------------------------
    # 7. Concurrent overlapping returns against the real file database.
    # ------------------------------------------------------------------
    race_svc = open_service(db_path)
    concurrent_quote = race_svc.quote(basket, [], "race-q", snap_before)
    race_svc.register_sale("S-race", quote=concurrent_quote)
    restock_b_before_race = race_svc.inventory()["RESTOCK"].get("B", 0)
    del race_svc
    barrier = threading.Barrier(2)
    outcomes: List[Dict[str, Any]] = []
    failures: List[str] = []
    lock = threading.Lock()

    def worker(request_id: str) -> None:
        client = open_service(db_path)
        try:
            barrier.wait()
            result = client.return_units(request_id, "S-race", ["b"], {"b": "RESTOCK"})
            with lock:
                outcomes.append({"request_id": request_id, "refund_total": result["refund_total"]})
        except BasketError as exc:
            with lock:
                failures.append(exc.code)

    threads = [threading.Thread(target=worker, args=(f"race-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    final = open_service(db_path)
    restock_b_after_race = final.inventory()["RESTOCK"].get("B", 0)
    race_state = {
        "successes": outcomes,
        "failures": failures,
        "inventory_B_restock": restock_b_after_race,
        "inventory_B_restock_delta": restock_b_after_race - restock_b_before_race,
        "remaining": final.get_sale("S-race")["remaining_refundable"],
    }
    results["scenarios"]["concurrent_returns"] = race_state
    results["checks"].append({
        "check": "concurrent_overlap_one_success_one_rejection_no_double_stock",
        "ok": len(outcomes) == 1
        and failures == ["UNIT_ALREADY_RETURNED"]
        and race_state["inventory_B_restock_delta"] == 1,
        "expected": {"successes": 1, "rejections": ["UNIT_ALREADY_RETURNED"], "restock_B_delta": 1},
        "actual": race_state,
    })
    del final

    results["summary"] = {
        "checks": len(results["checks"]),
        "passed": sum(1 for c in results["checks"] if c["ok"]),
        "failed": sum(1 for c in results["checks"] if not c["ok"]),
    }
    results["plan"] = {
        "note": "The structured operations executed above, replayable with `python -m basket_service run-plan`.",
        "steps": [
            {"op": "publish_snapshot", "products": [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}], "coupons": material["before"], "snapshot_id": "case-before"},
            {"op": "publish_snapshot", "products": [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}], "coupons": material["before"] + material["added"], "snapshot_id": "case-added"},
            {"name": "q_before", "op": "quote", "basket": basket, "coupons": [], "request_id": "case-q-before", "snapshot_id": "case-before"},
            {"name": "q_added", "op": "quote", "basket": basket, "coupons": [], "request_id": "case-q-added", "snapshot_id": "case-added"},
            {"name": "s_before", "op": "register_sale", "sale_id": "S1", "quote": "$q_before"},
            {"name": "s_added", "op": "register_sale", "sale_id": "S2", "quote": "$q_added"},
            {"op": "return", "request_id": "r1", "sale_id": "S1", "unit_ids": ["b"], "grade": {"b": "RESTOCK"}},
            {"op": "return", "request_id": "ret-added-b", "sale_id": "S2", "unit_ids": ["b"], "grade": {"b": "SCRAP"}},
            {"op": "register_sale", "sale_id": "S1", "quote": "$q_added", "expect_error": "SALE_CONFLICT"},
            # --- troupe cabinet (self-contained order + two pairs) ---------
            {"op": "set_shop_mode", "mode": "PRESTRETCH"},
            {"op": "publish_snapshot", "products": [{"sku": "DANCE", "price": 100}], "coupons": [], "snapshot_id": "troupe-plan-snap"},
            {"name": "tq", "op": "quote", "basket": [{"unit_id": u, "sku": "DANCE", "price": 100} for u in ("tLJ", "tLY", "tR1", "tR2")], "coupons": [], "request_id": "troupe-plan-q", "snapshot_id": "troupe-plan-snap"},
            {"op": "register_sale", "sale_id": "troupe-plan-sale", "quote_request_id": "troupe-plan-q"},
            {"op": "recover_from_sale", "physical_id": "tLJ", "side": "L", "sale_id": "troupe-plan-sale", "unit_id": "tLJ", "width": 2, "length": 5, "stretch_to": 2},
            {"op": "recover_from_sale", "physical_id": "tLY", "side": "L", "sale_id": "troupe-plan-sale", "unit_id": "tLY", "width": 2, "length": 5, "stretch_to": 2},
            {"op": "recover_from_sale", "physical_id": "tR1", "side": "R", "sale_id": "troupe-plan-sale", "unit_id": "tR1", "width": 2, "length": 5, "stretch_to": 2},
            {"op": "recover_from_sale", "physical_id": "tR2", "side": "R", "sale_id": "troupe-plan-sale", "unit_id": "tR2", "width": 2, "length": 5, "stretch_to": 3},
            {"op": "consign", "physical_id": "tLJ", "dancer_id": "owner", "receipt_id": "c"},
            {"op": "consign", "physical_id": "tLY", "dancer_id": "owner", "receipt_id": "c"},
            {"op": "consign", "physical_id": "tR1", "dancer_id": "owner", "receipt_id": "c"},
            {"op": "consign", "physical_id": "tR2", "dancer_id": "owner", "receipt_id": "c"},
            {"op": "publish_fitting_slip", "slip_id": "t-slip-tLJ", "physical_id": "tLJ"},
            {"op": "publish_fitting_slip", "slip_id": "t-slip-tLY", "physical_id": "tLY"},
            {"op": "publish_fitting_slip", "slip_id": "t-slip-tR1", "physical_id": "tR1"},
            {"op": "publish_fitting_slip", "slip_id": "t-slip-tR2", "physical_id": "tR2"},
            {"name": "talloc", "op": "allocate", "allocation_id": "troupe-plan-alloc", "needs": [
                {"dancer_id": "jia", "left": {"width": 2, "length": 5}, "right": {"width": 2, "length": 5}},
                {"dancer_id": "yi", "left": {"width": 2, "length": 5}, "right": {"width": 3, "length": 5}},
            ], "stretch_budget": 1},
            {"op": "confirm_delivery", "allocation_id": "troupe-plan-alloc", "run_until": "run-end"},
            {"op": "cabinet_view"},
        ],
    }

    _write_json(Path(out_path), results)
    _write_json(Path(plan_path), results["plan"])
    return results


def _run_plan_file(db_path: str, plan_file: str, out_file: str | None) -> Dict[str, Any]:
    svc = open_service(db_path)
    plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    result = svc.run(plan)
    if out_file:
        _write_json(Path(out_file), result)
    return result


def main(argv: List[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m basket_service", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the full demo and write database/results")
    demo.add_argument("--db", default=str(ROOT / "data" / "demo.db"))
    demo.add_argument("--out", default=str(ROOT / "results" / "demo_results.json"))
    demo.add_argument("--plan", default=str(ROOT / "results" / "demo_plan.json"))

    run_plan = sub.add_parser("run-plan", help="execute a JSON plan against a database")
    run_plan.add_argument("plan")
    run_plan.add_argument("--db", required=True)
    run_plan.add_argument("--out")

    selfcheck = sub.add_parser("self-check", help="run the unit test suite")
    selfcheck.add_argument("--db", default=str(ROOT / "data" / "selfcheck.db"))
    selfcheck.add_argument("--out", default=str(ROOT / "results" / "self_check.json"))

    args = parser.parse_args(argv)
    if args.command == "demo":
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
        results = run_demo(args.db, args.out, args.plan)
        print(json.dumps({"database": args.db, "results": args.out, "summary": results["summary"]}, indent=2))
        return 0 if results["summary"]["failed"] == 0 else 1
    if args.command == "run-plan":
        result = _run_plan_file(args.db, args.plan, args.out)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.command == "self-check":
        import unittest

        loader = unittest.TestLoader()
        suite = loader.discover(str(ROOT / "tests"))
        runner = unittest.TextTestRunner(verbosity=2)
        outcome = runner.run(suite)
        payload = {
            "ok": outcome.wasSuccessful(),
            "tests": outcome.testsRun,
            "failures": [str(t) for t, _ in outcome.failures],
            "errors": [str(t) for t, _ in outcome.errors],
        }
        _write_json(Path(args.out), payload)
        return 0 if outcome.wasSuccessful() else 1
    return 2
