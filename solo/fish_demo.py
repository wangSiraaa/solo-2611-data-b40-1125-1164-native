"""End-to-end fish boarding demonstration.

Run with::

    python3 -m solo.fish_demo

It prints the three required decision turns and writes a machine-readable
report to ``artifacts/fish_demo_report.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .codec import encode_text
from .fishdesk import (
    FrontDesk,
    LedgerBuilder,
    PickupCase,
    RevisionLedger,
    canonical_ledger_text,
    slot_pressure_ledger_text,
    slot_report,
)

ARTIFACT_DIR = Path(__file__).resolve().parent.parent / "artifacts"
ARTIFACT_PATH = ARTIFACT_DIR / "fish_demo_report.json"


def _canonical_desk(*, revoke_mandate: bool = False) -> FrontDesk:
    desk = FrontDesk(available_lots={"A", "B"})
    desk.receipts.issue("R-JIA", "F1", "JIA")
    desk.receipts.issue("R-YI", "F2", "YI")
    # Only JIA's receipt has been returned this weekend.
    desk.receipts.return_receipt("R-JIA")
    desk.mandates.grant("M-A", "JIA", "TRUSTEE-LIN", "A")
    if revoke_mandate:
        desk.mandates.revoke("M-A")
    return desk


def _jia_case() -> PickupCase:
    return PickupCase(
        "ORDER-JIA-A",
        "JIA",
        ("R-JIA",),
        "A",
        holder="TRUSTEE-LIN",
        mandate_id="M-A",
        day="SATURDAY",
    )


def canonical_story() -> dict[str, Any]:
    ledger = RevisionLedger.from_text(canonical_ledger_text())
    case = _jia_case()

    original_replay = ledger.replay(0)
    original_desk = _canonical_desk()
    written_off = original_desk.attempt(case, original_replay)

    erratum = "# ERRATUM_UNSEEN which fish entered A"
    corrected_version, old_line = ledger.replace_line_containing(
        "WITNESS E-SPLIT", erratum
    )
    ambiguous_replay = ledger.replay(corrected_version.version_id)
    ambiguous_desk = _canonical_desk()
    ambiguous_decision = ambiguous_desk.evaluate(case, ambiguous_replay)

    restored_desk = _canonical_desk(revoke_mandate=True)
    revoked_decision = restored_desk.evaluate(case, ledger.replay(0))

    return {
        "question": "After merge/split, which original household can be completed this weekend?",
        "identity": original_replay.identity_report().summary(),
        "original_record": {
            "version": 0,
            "surviving_histories": len(original_replay.worlds),
            "decision": written_off.summary(),
            "sealed_box": {
                "box_id": original_desk.deliveries[0].box.box_id,
                "sealed": original_desk.deliveries[0].box.sealed,
                "fish_ids": sorted(original_desk.deliveries[0].box.fish_ids),
            },
            "answer": "可交甲户（JIA）：具名记录证明白尾 F1 入 A；受托人凭 A 委托和已交回回单封遮光盒交付并销账。",
        },
        "erratum_unseen_a": {
            "new_version": corrected_version.version_id,
            "replaced_char_span": [old_line.start_char, old_line.end_char],
            "replaced_byte_span": [old_line.start_byte, old_line.end_byte],
            "surviving_histories": len(ambiguous_replay.worlds),
            "fog": ambiguous_replay.identity_report().summary()["fog_groups"],
            "decision": ambiguous_decision.summary(),
            "answer": "暂缓：A 可能是 F1 或 F2；又无独立隔位记录，前台不能在提领时开盒分辨。",
        },
        "restored_but_mandate_revoked": {
            "surviving_histories": 1,
            "decision": revoked_decision.summary(),
            "answer": "暂缓：身份虽已重新确定，但受托人提 A 的委托已撤销。",
        },
    }


def _slot_desk(extra_nets: bool) -> FrontDesk:
    lots = {"N1", "N2"}
    if extra_nets:
        lots.update({"N3", "N4"})
    else:
        lots.add("OPEN:C")
    desk = FrontDesk(available_lots=lots)
    for rid, fid, owner in (
        ("R1", "F1", "JIA"),
        ("R2", "F2", "YI"),
        ("R3", "F3", "BING"),
        ("R4", "F4", "DING"),
    ):
        desk.receipts.issue(rid, fid, owner)
        desk.receipts.return_receipt(rid)
    return desk


def slot_pressure_story() -> dict[str, Any]:
    outcomes: dict[str, Any] = {}
    for extra_nets in (False, True):
        key = "relief_four_private_nets" if extra_nets else "tight_two_slots_in_C"
        ledger = RevisionLedger.from_text(slot_pressure_ledger_text(extra_nets=extra_nets))
        replay = ledger.replay()
        desk = _slot_desk(extra_nets)
        targets = (
            [("JIA", "R1", "N1"), ("YI", "R2", "N2"), ("BING", "R3", "N3"), ("DING", "R4", "N4")]
            if extra_nets
            else [
                ("JIA", "R1", "N1"),
                ("YI", "R2", "N2"),
                ("BING", "R3", "OPEN:C"),
                ("DING", "R4", "OPEN:C"),
            ]
        )
        decisions = []
        for i, (owner, receipt, target) in enumerate(targets, 1):
            case = PickupCase(f"SLOT-{key}-{i}", owner, (receipt,), target, holder=owner)
            decisions.append(desk.evaluate(case, replay).summary())
        outcomes[key] = {
            "surviving_histories": len(replay.worlds),
            "slot_use": [
                {
                    "tank": s.tank,
                    "capacity": s.capacity,
                    "final_nets": list(s.nets),
                    "peak": s.peak,
                    "peak_free": s.peak_free,
                    "violations": list(s.violations),
                }
                for s in slot_report(ledger.parse())
            ],
            "decisions": decisions,
            "accepted_orders": [d["case_id"] for d in decisions if d["allowed"]],
            "held_orders": [
                {"case_id": d["case_id"], "reason_code": d["reason_code"]}
                for d in decisions
                if not d["allowed"]
            ],
        }
    outcomes["lesson"] = (
        "A whole-net transfer keeps household identity even when slots are tight; "
        "without the last two slots, BING and DING stay as a same-spot shared pair "
        "and their separate orders disappear from the acceptable pickup list."
    )
    return outcomes


def window_gap_story() -> dict[str, Any]:
    def ledger(gap: bool) -> RevisionLedger:
        b = LedgerBuilder("WINDOW-GAP-DEMO")
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

    result: dict[str, Any] = {}
    for name, gap in (("complete_window", False), ("missed_one_window_segment", True)):
        rev = ledger(gap)
        replay = rev.replay()
        desk = FrontDesk(available_lots={"A"})
        desk.receipts.issue("R1", "F1", "JIA")
        desk.receipts.return_receipt("R1")
        case = PickupCase("WINDOW-JIA", "JIA", ("R1",), "A", holder="JIA")
        result[name] = {
            "surviving_histories": len(replay.worlds),
            "fog": replay.identity_report().summary()["fog_groups"],
            "decision": desk.evaluate(case, replay).summary(),
        }
    return result


def ambiguous_scoop_story() -> dict[str, Any]:
    b = LedgerBuilder("GROUP-HISTORY-DEMO")
    for i in range(1, 4):
        b.fish(f"F{i}", f"O{i}", "WHITE", "A", f"09:0{i}")
    b.scoop("E1", 2, "A", "B", "10:00", spot="WHITE")
    # The same two-fish group is scooped onward; every retained world carries
    # both transfers rather than collapsing to one guessed fish number.
    b.scoop("E2", 2, "B", "C", "11:00", spot="WHITE")
    b.count("C", 2, "11:05")
    replay = RevisionLedger.from_text(b.render()).replay()
    trace_counts = sorted({len(w.moves) for w in replay.worlds})
    return {
        "surviving_group_histories": len(replay.worlds),
        "transfer_traces_per_history": trace_counts,
        "possible_F1_locations": [loc.label for loc in replay.identity_report().possible_locations["F1"]],
        "rule": "All combinations remain until a count or clearly read fish ID excludes them.",
    }


def build_report() -> dict[str, Any]:
    return {
        "title": "SOLO duty-book ornamental-fish boarding desk",
        "limits": {"max_fish": 4, "max_tanks": 3, "max_transfers": 6},
        "canonical": canonical_story(),
        "slot_pressure_new_boarding": slot_pressure_story(),
        "window_gap_new_boarding": window_gap_story(),
        "ambiguous_group_transfers": ambiguous_scoop_story(),
    }


def main() -> None:
    report = build_report()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    canonical = RevisionLedger.from_text(canonical_ledger_text())
    (ARTIFACT_DIR / "fish_duty_book_v0.solo").write_bytes(canonical.version(0).body)
    erratum_version, _ = canonical.replace_line_containing(
        "WITNESS E-SPLIT", "# ERRATUM_UNSEEN which fish entered A"
    )
    (ARTIFACT_DIR / "fish_duty_book_unseen_v1.solo").write_bytes(erratum_version.body)

    canonical = report["canonical"]
    print("== 观赏鱼寄养值班簿 ==")
    print("原始具名记录:", canonical["original_record"]["answer"])
    print("  decision:", canonical["original_record"]["decision"]["reason_code"],
          canonical["original_record"]["decision"]["box_id"])
    print("改作未看清:", canonical["erratum_unseen_a"]["answer"])
    print("  decision:", canonical["erratum_unseen_a"]["decision"]["reason_code"])
    print("还原但撤委托:", canonical["restored_but_mandate_revoked"]["answer"])
    print("  decision:", canonical["restored_but_mandate_revoked"]["decision"]["reason_code"])

    slots = report["slot_pressure_new_boarding"]
    tight = slots["tight_two_slots_in_C"]
    relief = slots["relief_four_private_nets"]
    print("\n== 隔位紧张新寄养 ==")
    print("紧张时可接:", tight["accepted_orders"], "暂缓:", tight["held_orders"])
    print("加隔位后可接:", relief["accepted_orders"])

    windows = report["window_gap_new_boarding"]
    print("\n== 观察窗对照 ==")
    print("完整窗前:", windows["complete_window"]["decision"]["reason_code"])
    print("漏看一段:", windows["missed_one_window_segment"]["decision"]["reason_code"])

    amb = report["ambiguous_group_transfers"]
    print("\n== 同尾斑成组转移 ==")
    print("保留经过数:", amb["surviving_group_histories"],
          "每段经过的转移 trace 数:", amb["transfer_traces_per_history"])
    print("\nreport:", ARTIFACT_PATH)


if __name__ == "__main__":
    main()
