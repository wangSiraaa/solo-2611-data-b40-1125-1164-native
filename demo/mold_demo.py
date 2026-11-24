"""销模核销可复跑案例：一甲两乙、两副共用底座的模。

运行：
    python3 -m demo.mold_demo
    python3 -m demo.mold_demo --json
"""

from __future__ import annotations

import argparse
import copy
import json
from typing import Any

from weighbridge import Catalog, Warehouse
from weighbridge.molds import (
    ISSUED,
    ROUTE_ON_SITE_WITNESS,
    ROUTE_WORKSHOP_SEALED,
    MoldWriteOffLedger,
)

WORK = "STATUE-01"


def _warehouse_trip(*, out_gross: int = 9575, net_delta: int | None = None) -> Warehouse:
    c = Catalog()
    c.define_mass("tractor", "T1", 7000, 7000)
    c.define_mass("trailer", "R1", 2000, 2000)
    seq = c.apply_attach("T1", "R1")
    w = Warehouse(c)
    w.add_ticket(dict(
        ticket_id="WB-IN", tractor="T1", catalog_seq=seq, ts=1,
        direction="IN", gross=9000,
    ))
    gross_out = out_gross if net_delta is None else 9000 + net_delta
    w.add_ticket(dict(
        ticket_id="WB-OUT", tractor="T1", catalog_seq=seq, ts=2,
        direction="OUT", gross=gross_out,
    ))
    ev = w.preview("LOAD", "WB-IN", "WB-OUT")
    w.confirm(ev.trip_id)
    return w


def build_sealed_case(mutation: str | None = None) -> tuple[Warehouse, MoldWriteOffLedger, dict[str, Any]]:
    """构造工场拆瓣、封号集中发破碎台的基线/单变量反事实案例。

    mutation 可为：
    * ``seal``：把甲箱封号交接改成错号；
    * ``weigh_ticket``：只改 OUT 磅单毛重；
    * ``workshop_piece``：把唯一甲改成不可在工场封运。
    """
    out_gross = 9900 if mutation == "weigh_ticket" else 9575
    w = _warehouse_trip(out_gross=out_gross)
    ledger = w.mold_writeoffs()

    a_sealable = mutation != "workshop_piece"
    ledger.add_piece("A1", WORK, "A", weight_lo=298, weight_hi=302,
                     labels=["甲瓣"], workshop_sealable=a_sealable)
    ledger.add_piece("B1", WORK, "B", weight_lo=225, weight_hi=238, labels=["左瓣"])
    ledger.add_piece("B2", WORK, "B", weight_lo=232, weight_hi=245, labels=["左瓣"])
    ledger.add_piece("BASE1", WORK, "BASE", weight=80, labels=["共用底座"])

    ledger.add_mold("M1", WORK, required_piece_ids=["A1", "B1", "BASE1"], note="第一副")
    ledger.add_mold("M2", WORK, required_piece_ids=["A1", "B2", "BASE1"], note="第二副，共用甲和底座")

    ledger.add_witness("PACKER-1", "装箱见证人", kind="packing")
    ledger.seal_box("BOX-A", "SEAL-A-1", tare_lo=18, tare_hi=22,
                    packing_witness_id="PACKER-1", packed_at=10)
    ledger.seal_box("BOX-B", "SEAL-B-2", tare_lo=13, tare_hi=17,
                    packing_witness_id="PACKER-1", packed_at=11)
    # 注意：B 行只有“左瓣”标签，没有 identified_piece_id；不能把它当成 B1 或 B2 的序列号。
    ledger.pack_line("BOX-A", label="甲瓣", role="A", work_id=WORK)
    ledger.pack_line("BOX-B", label="左瓣", role="B", work_id=WORK)

    ledger.attach_shipment("TRIP0001", ["BOX-A", "BOX-B"])

    seal_a = "WRONG-SEAL" if mutation == "seal" else "SEAL-A-1"
    ledger.add_crush_batch(
        "CRUSH-1", crushed_at=100, box_ids=["BOX-A", "BOX-B"],
        seal_readings={
            "BOX-A": {"seal_id": seal_a, "intact": True},
            "BOX-B": {"seal_id": "SEAL-B-2", "intact": True},
        },
        slag_weight=(500, 560), yield_lo="0.9", yield_hi="1",
    )
    context = {"baseline_receipt": "RCPT-1", "mutation": mutation}
    return w, ledger, context


def build_on_site_case(mutation: str | None = None) -> tuple[Warehouse, MoldWriteOffLedger, dict[str, Any]]:
    """混箱运输 + 见证人到场开箱识别的替代路径。

    mutation=``witness_window`` 时只把见证时段改窄。
    """
    w = _warehouse_trip(net_delta=320)
    ledger = w.mold_writeoffs()
    ledger.add_piece("A1", WORK, "A", weight_lo=298, weight_hi=302, labels=["甲瓣"])
    ledger.add_piece("B1", WORK, "B", weight_lo=225, weight_hi=238, labels=["左瓣"])
    ledger.add_piece("B2", WORK, "B", weight_lo=232, weight_hi=245, labels=["左瓣"])
    ledger.add_piece("BASE1", WORK, "BASE", weight=80)
    ledger.add_mold("M1", WORK, required_piece_ids=["A1", "B1", "BASE1"])
    ledger.add_mold("M2", WORK, required_piece_ids=["A1", "B2", "BASE1"])

    ledger.seal_box("MIX-BOX", "SEAL-MIX", tare_lo=18, tare_hi=22, at_workshop=False)
    # 混箱里仍可留着只写角色的装箱条；它能参与重量枚举，但在现场指认前不能单独销身份。
    ledger.pack_line("MIX-BOX", label="甲瓣", role="A", work_id=WORK)
    ledger.attach_shipment("TRIP0001", ["MIX-BOX"])

    valid_to = 45 if mutation == "witness_window" else 60
    ledger.add_witness("WIT-1", "现场见证人", valid_from=40, valid_to=valid_to)
    ledger.add_opening(
        "OPEN-1", "MIX-BOX", witness_id="WIT-1", opened_at=50,
        piece_ids=["A1"], batch_id="CRUSH-ON",
    )
    ledger.add_crush_batch(
        "CRUSH-ON", crushed_at=70, box_ids=["MIX-BOX"], opening_ids=["OPEN-1"],
        seal_readings={"MIX-BOX": "SEAL-MIX"},
        slag_weight=(285, 305), yield_lo="0.95", yield_hi="1",
    )
    return w, ledger, {"mutation": mutation, "route": ROUTE_ON_SITE_WITNESS}


def run(mutate: str | None = None) -> dict[str, Any]:
    if mutate in {"witness_window"}:
        _, ledger, ctx = build_on_site_case(mutate)
        route = ROUTE_ON_SITE_WITNESS
    else:
        _, ledger, ctx = build_sealed_case(mutate)
        route = ROUTE_WORKSHOP_SEALED
    receipt = ledger.issue_receipt(
        "RCPT-1", WORK, client="委托人", signatory="铸像厂值班厂长",
        issued_at=101, route=route,
    )
    trace = ledger.trace_receipt("RCPT-1")
    return {"context": ctx, "receipt": receipt.to_dict(), "reverse_lookup": trace["reverse_lookup"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="print full JSON report")
    args = parser.parse_args()

    reports = {
        "baseline": run(None),
        "mutate_seal": run("seal"),
        "mutate_weigh_ticket": run("weigh_ticket"),
        "mutate_workshop_piece": run("workshop_piece"),
        "mutate_witness_window": run("witness_window"),
    }
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return
    for name, report in reports.items():
        r = report["receipt"]
        v = r["verdict"]
        print(f"== {name}: {r['status']} ==")
        print("  certain:", ", ".join(v["certain_destroyed_ids"]) or "(none)")
        for b in v["batch_reports"]:
            print("  batch", b["batch_id"], "combinations=", b.get("inferred_combinations"))
        print("  remaining:", [x["mold_id"] + ":" + "+".join(x["chosen_piece_ids"]) for x in v["remaining_molds"]])
        if v["issues"]:
            print("  issues:", " | ".join(v["issues"][:3]))
        print("  trace:", report["reverse_lookup"])


if __name__ == "__main__":
    main()
