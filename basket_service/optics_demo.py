"""Optics handover demo: the astronomy club's borrowed-eyepiece scenario.

Run ``python3 -m basket_service optics-demo`` to (re)generate::

    data/optics.db               real SQLite database
    results/optics_results.json  every check, computed independently
    results/optics_plan.json     a replayable JSON plan

The scenario follows the brief literally:

1. two visually identical eyepieces from two different barrel batches, every
   piece signed onto a real, unreturned sale unit, no model impersonation;
2. the two-barrel shared-scale-bias counterexample: two scopes must not share
   one adapter, and a batch bias is never assumed for another batch;
3. the positive delivery (focal 10, path 6, travel 3..4 -> net 4, inclusive);
4. the counterexample: changing shoulder evidence to path 5 pushes the demand
   to 5, outside the wheel, so it can no longer be guaranteed;
5. restoring the evidence, then a front session holding the adapter: the next
   session cannot be delivered even when its window does not overlap;
6. one order that continues after a temporary re-mount, and one that must be
   cancelled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from . import open_service
from .errors import BasketError

ROOT = Path(__file__).resolve().parent.parent


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _sale(svc, skus, sale_id):
    products = [{"sku": s, "price": 100} for s in skus]
    snap = svc.publish_snapshot(products, [], snapshot_id=f"snap-{sale_id}")
    basket = [{"unit_id": f"u-{s}", "sku": s, "price": 100} for s in skus]
    quote = svc.quote(basket, [], f"q-{sale_id}", snap)
    return svc.register_sale(sale_id, quote=quote)


def _err_code(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except BasketError as exc:
        return exc.code
    return "NO_ERROR"


def run_optics_demo(db_path: str, out_path: str, plan_path: str) -> Dict[str, Any]:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(db_path + suffix)
        if candidate.exists():
            candidate.unlink()

    svc = open_service(db_path)
    results: Dict[str, Any] = {"database": str(db_path), "checks": [], "scenarios": {}}

    def check(name: str, ok: bool, expected: Any, actual: Any) -> None:
        results["checks"].append({"check": name, "ok": bool(ok), "expected": expected, "actual": actual})

    # ------------------------------------------------------------------
    # 1. Intake: two identical-looking eyepieces, two batches, real units.
    # ------------------------------------------------------------------
    _sale(svc, ["E-A", "AD-1", "AD-2"], "S1")
    _sale(svc, ["E-B"], "S2")
    svc.add_batch("B1", 0, 0, [])
    svc.add_batch("B2", 2, 2, [])
    svc.add_telescope("T-A", "SCT", 3, 4)
    svc.add_telescope("T-B", "SCT", 3, 4)
    svc.add_telescope("T-C", "SCT", 0, 2)  # too short for a 6-unit path
    svc.register_piece("EP-A", "eyepiece", "E-A", "S1", "u-E-A", batch_id="B1",
                       focal_position=10, in_iface="SC", measured_lo=5, measured_hi=5,
                       return_deadline=100, repair_deadline=120, lend_return_deadline=140)
    svc.register_piece("EP-B", "eyepiece", "E-B", "S2", "u-E-B", batch_id="B2",
                       focal_position=10, in_iface="SC", measured_lo=3, measured_hi=3)
    svc.register_piece("AD-1", "adapter", "AD-1", "S1", "u-AD-1",
                       in_iface="SCT", out_iface="SC", nominal_path=1)
    svc.register_piece("AD-2", "adapter", "AD-2", "S1", "u-AD-2",
                       in_iface="SCT", out_iface="SC", nominal_path=1)
    results["scenarios"]["intake"] = {
        "EP-A": svc.get_piece("EP-A"),
        "EP-B": svc.get_piece("EP-B"),
        "EP-A_path": list(svc.piece_path_interval("EP-A")),
        "EP-B_path": list(svc.piece_path_interval("EP-B")),
    }
    impersonation = _err_code(svc.register_piece, "EP-A-guest", "eyepiece", "E-A", "S1", "u-E-A",
                              batch_id="B1", focal_position=10, in_iface="SC",
                              measured_lo=5, measured_hi=5)
    results["scenarios"]["impersonation_error"] = impersonation
    check("intake.same_look_distinct_units",
          svc.get_piece("EP-A")["sale_id"] != svc.get_piece("EP-B")["sale_id"],
          "distinct sales", "distinct sales")
    check("intake.no_model_impersonation", impersonation == "PIECE_IMPERSONATION",
          "PIECE_IMPERSONATION", impersonation)

    # ------------------------------------------------------------------
    # 2. Two-barrel shared-scale-bias counterexample.
    # ------------------------------------------------------------------
    gprobe = svc.lock_group("G-probe", "T-B", "EP-B", ["AD-1"])
    shared_err = _err_code(svc.lock_group, "G-A-pre", "T-A", "EP-A", ["AD-1"])
    svc.unseal_group("G-probe")  # no bookings yet; frees EP-B and AD-1
    # What a naive "both barrels share one bias" would compute for EP-B:
    # measured shoulder 3 + B1's bias 0 + adapter 1 = path 4 => net 6, so it would
    # wrongly reject a piece that with its own batch bias (2) reaches net 4.
    naive_path = 3 + svc.get_batch("B1")["bias_hi"] + 1
    naive_net = 10 - naive_path
    naive_deliverable = 3 <= naive_net <= 4
    real_net = svc.assess_combo("T-B", "EP-B", ["AD-1"])
    results["scenarios"]["two_barrel"] = {
        "probe": gprobe,
        "shared_adapter_error": shared_err,
        "batch_B1": svc.get_batch("B1"),
        "batch_B2": svc.get_batch("B2"),
        "naive_shared_bias": {"path": naive_path, "net": naive_net,
                              "deliverable": naive_deliverable},
        "correct_bias": {"net": real_net["net_hi"], "deliverable": real_net["deliverable"]},
    }
    check("two_barrel.bias_is_per_batch",
          svc.get_batch("B1")["bias_hi"] != svc.get_batch("B2")["bias_hi"],
          "different biases", {"B1": 0, "B2": 2})
    check("two_barrel.shared_bias_flips_verdict",
          naive_deliverable != real_net["deliverable"] and real_net["deliverable"],
          {"naive_deliverable": False, "correct_deliverable": True},
          {"naive_deliverable": naive_deliverable, "correct_deliverable": real_net["deliverable"]})
    check("two_barrel.one_adapter_not_shared", shared_err == "GROUP_SEALED",
          "GROUP_SEALED", shared_err)

    # ------------------------------------------------------------------
    # 3. Positive delivery: focal 10, path 6, travel 3..4 -> net 4 inclusive.
    # ------------------------------------------------------------------
    svc.add_session("SES1", 1, 0, 100)
    svc.add_session("SES2", 2, 80, 200)   # overlaps SES1 on [80,100]
    svc.add_session("SES3", 3, 300, 400)  # a later night, no overlap
    ga1 = svc.lock_group("GA1", "T-A", "EP-A", ["AD-1"])
    svc.verify_group("GA1", observe_wheel=4, technician="tech-A")
    order1 = svc.place_order("ORDER-1", "S1", "GA1", "SES1", 1, 2)
    overlap_err = _err_code(svc.place_order, "ORDER-2", "S1", "GA1", "SES2", 81, 82)
    delivery1 = svc.deliver_order("ORDER-1")
    results["scenarios"]["positive"] = {"group": ga1, "order": order1, "delivery": delivery1,
                                        "overlap_error": overlap_err}
    check("focus.positive_total_is_6", (ga1["total_lo"], ga1["total_hi"]) == (6, 6), [6, 6],
          [ga1["total_lo"], ga1["total_hi"]])
    check("focus.positive_net_is_4", (ga1["net_lo"], ga1["net_hi"]) == (4, 4), [4, 4],
          [ga1["net_lo"], ga1["net_hi"]])
    check("focus.positive_delivers", delivery1["telescope_id"] == "T-A"
          and delivery1["focus_hi"] == 4, {"telescope": "T-A", "focus_hi": 4},
          {"telescope": delivery1["telescope_id"], "focus_hi": delivery1["focus_hi"]})
    check("sessions.reserved_overlap_conflicts", overlap_err == "PIECE_BOOKED",
          "PIECE_BOOKED", overlap_err)

    # ------------------------------------------------------------------
    # 4. Counterexample: change shoulder evidence -> path 5 -> needs travel 5.
    # ------------------------------------------------------------------
    svc.return_pieces("ORDER-1")
    svc.unseal_group("GA1")
    svc.update_piece_evidence("EP-A", 4, 4)  # eyepiece path 4 -> total 5 with adapter
    changed = svc.assess_combo("T-A", "EP-A", ["AD-1"])
    relock_err = _err_code(svc.lock_group, "GA1-bad", "T-A", "EP-A", ["AD-1"])
    results["scenarios"]["changed_evidence"] = {"assess": changed, "relock_error": relock_err}
    check("counterexample.changed_path_needs_5",
          changed["needed_travel"] == 5 and not changed["deliverable"],
          {"needed_travel": 5, "deliverable": False},
          {"needed_travel": changed["needed_travel"], "deliverable": changed["deliverable"]})
    check("counterexample.changed_cannot_lock", relock_err == "FOCUS_UNREACHABLE",
          "FOCUS_UNREACHABLE", relock_err)

    # Restore original evidence and re-lock / verify.
    svc.update_piece_evidence("EP-A", 5, 5)
    ga1b = svc.lock_group("GA1b", "T-A", "EP-A", ["AD-1"])
    svc.verify_group("GA1b", observe_wheel=4, technician="tech-A")
    results["scenarios"]["restored_group"] = ga1b
    check("counterexample.restore_evidence_delivers",
          (ga1b["net_lo"], ga1b["net_hi"]) == (4, 4), [4, 4], [ga1b["net_lo"], ga1b["net_hi"]])

    # ------------------------------------------------------------------
    # 5. Front session holds the adapter past the next session's start.
    # ------------------------------------------------------------------
    svc.place_order("ORDER-1b", "S1", "GA1b", "SES1", 1, 2)
    svc.deliver_order("ORDER-1b")
    held_err = _err_code(svc.place_order, "ORDER-4", "S1", "GA1b", "SES3", 301, 302)
    svc.return_pieces("ORDER-1b")
    order3 = svc.place_order("ORDER-3", "S1", "GA1b", "SES3", 301, 302)
    results["scenarios"]["held_forward"] = {"next_session_error": held_err, "after_return": order3}
    check("sessions.delivered_front_blocks_next", held_err == "PIECE_BOOKED",
          "PIECE_BOOKED", held_err)
    check("sessions.after_return_next_ok", order3["status"] == "OPEN", "OPEN", order3["status"])

    # ------------------------------------------------------------------
    # 6. Temporary re-mount: one order continues, another must be cancelled.
    # ------------------------------------------------------------------
    # Continue ORDER-3 by moving it from T-A to the compatible T-B.
    continued = svc.switch_telescope("ORDER-3", "T-B")
    # Build a second group on the now-free T-A and try to move it to T-C (too short).
    gb1 = svc.lock_group("GB1", "T-A", "EP-B", ["AD-2"])
    svc.verify_group("GB1", observe_wheel=4, technician="tech-B")
    svc.place_order("ORDER-5", "S2", "GB1", "SES3", 305, 306)
    move_err = _err_code(svc.switch_telescope, "ORDER-5", "T-C")
    cancelled = svc.cancel_order("ORDER-5", reason="no compatible telescope on site")
    results["scenarios"]["switch"] = {"continued": continued, "move_error": move_err,
                                      "cancelled": cancelled}
    check("switch.compatible_continues",
          continued["continued"] and continued["telescope_id"] == "T-B"
          and continued["switched_from"] == "T-A", True, continued["continued"])
    check("switch.unfocusable_refused", move_err == "FOCUS_UNREACHABLE",
          "FOCUS_UNREACHABLE", move_err)
    check("switch.must_cancel", cancelled["status"] == "CANCELLED", "CANCELLED",
          cancelled["status"])

    # ------------------------------------------------------------------
    # 7. Persistence: reopen keeps every fact.
    # ------------------------------------------------------------------
    results["scenarios"]["before_reopen"] = {
        "orders": svc.list_orders(),
        "bookings": svc.list_bookings(),
        "batches": [svc.get_batch(b) for b in ("B1", "B2")],
        "pieces": [svc.get_piece(p) for p in ("EP-A", "EP-B", "AD-1")],
    }
    del svc
    reopened = open_service(db_path)
    after = {
        "orders": reopened.list_orders(),
        "bookings": reopened.list_bookings(),
        "batches": [reopened.get_batch(b) for b in ("B1", "B2")],
        "pieces": [reopened.get_piece(p) for p in ("EP-A", "EP-B", "AD-1")],
    }
    results["scenarios"]["after_reopen"] = after
    check("reopen.preserves_optics", results["scenarios"]["before_reopen"] == after,
          "identical", "identical" if results["scenarios"]["before_reopen"] == after else "differs")

    results["summary"] = {
        "checks": len(results["checks"]),
        "passed": sum(1 for c in results["checks"] if c["ok"]),
        "failed": sum(1 for c in results["checks"] if not c["ok"]),
    }
    results["plan"] = {
        "note": "Replayable optics handover transcript (focal 10, path 6, travel 3..4).",
        "steps": [
            {"op": "publish_snapshot", "products": [{"sku": "E-A", "price": 100},
                                                    {"sku": "AD-1", "price": 100}],
             "coupons": [], "snapshot_id": "snap-S1"},
            {"op": "quote", "basket": [{"unit_id": "u-E-A", "sku": "E-A", "price": 100},
                                       {"unit_id": "u-AD-1", "sku": "AD-1", "price": 100}],
             "coupons": [], "request_id": "q-S1", "snapshot_id": "snap-S1"},
            {"op": "register_sale", "sale_id": "S1", "quote_request_id": "q-S1"},
            {"op": "add_telescope", "telescope_id": "T-A", "out_iface": "SCT",
             "travel_lo": 3, "travel_hi": 4},
            {"op": "add_batch", "batch_id": "B1", "scale_lo": 0, "scale_hi": 0, "cards": []},
            {"op": "register_piece", "piece_id": "EP-A", "kind": "eyepiece", "model": "E-A",
             "sale_id": "S1", "unit_id": "u-E-A", "batch_id": "B1", "focal_position": 10,
             "in_iface": "SC", "measured_lo": 5, "measured_hi": 5},
            {"op": "register_piece", "piece_id": "AD-1", "kind": "adapter", "model": "AD-1",
             "sale_id": "S1", "unit_id": "u-AD-1", "in_iface": "SCT", "out_iface": "SC",
             "nominal_path": 1},
            {"op": "lock_group", "group_id": "GA1", "telescope_id": "T-A",
             "eyepiece_id": "EP-A", "adapter_ids": ["AD-1"]},
            {"op": "verify_group", "group_id": "GA1", "observe_wheel": 4, "technician": "tech-A"},
            {"op": "add_session", "session_id": "SES1", "seq": 1, "starts_at": 0, "ends_at": 100},
            {"op": "place_order", "order_id": "ORDER-1", "sale_id": "S1", "group_id": "GA1",
             "session_id": "SES1", "slot_lo": 1, "slot_hi": 2},
            {"op": "deliver_order", "order_id": "ORDER-1"},
        ],
    }

    _write_json(Path(out_path), results)
    _write_json(Path(plan_path), results["plan"])
    return results
