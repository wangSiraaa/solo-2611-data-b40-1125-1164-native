"""腾街单桌面回放：三段街、两趟车以内的两个题面案例。

运行::

    python3 -m demo.run_street_demo
    python3 -m demo.run_street_demo --json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from weighbridge.scenarios import (
    build_ban_case,
    build_fork_market_case,
    build_route_case,
    build_witness_case,
    replay_fork_worlds,
)


def line(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def status(case: dict[str, Any]) -> str:
    o = case["order"]
    method = o.evaluation.get("selected_method")
    return f"{o.order_id}: {o.status}" + (f" / {method}" if method else "")


def print_query_brief(office: Any, order_id: str) -> None:
    q = office.query_order(order_id)
    print(f"  单号 {order_id} -> {q['status']}")
    print(f"    可交路段: {q.get('deliverable_segments')}")
    print(f"    可交摊位: {q.get('deliverable_stalls')}")
    print(f"    引用车次: {[t['trip_id'] + ' net=' + str(t.get('net')) for t in q['trip_details']]}")
    if q.get("planned_trip_id"):
        print(f"    计划末车(尚未引用确认净重): {q['planned_trip_id']}")
    print(f"    许出堆位: {q.get('granted_slots')}")
    if q.get("reasons"):
        print(f"    复核理由: {q['reasons']}")
    if q.get("recommended_partial"):
        print(f"    建议缩段: {q['recommended_partial']}")


def run() -> dict[str, Any]:
    report: dict[str, Any] = {}

    line("案例一：甲乙原有各 5kg，一车确认卸雪净重 6kg")
    ok = build_witness_case(replaced=False)
    bad = build_witness_case(replaced=True)
    recon = ok["office"].reconstruct()
    print("有‘乙只装 1kg’见证：")
    print(f"  车次映射: {ok['trip_ids']}")
    print(f"  剩余雪量: {recon.remaining_range}")
    print(f"  车次分配: {recon.trip_allocations}")
    print_query_brief(ok["office"], "SR-见证")
    print("\n只把装车见证替换成‘乙装 5kg’：")
    bad_recon = bad["office"].reconstruct()
    print(f"  剩余雪量: {bad_recon.remaining_range}")
    print_query_brief(bad["office"], "SR-见证")
    report["witness"] = {
        "ok": ok["office"].query_order("SR-见证"),
        "replaced": bad["office"].query_order("SR-见证"),
    }

    line("同一承诺的禁倒分支：禁倒权必须换来可签收的丙位")
    ban_ok = build_ban_case()
    ban_bad = build_ban_case(revoked=True)
    print_query_brief(ban_ok["office"], "ND-禁倒甲")
    print_query_brief(ban_ok["office"], "SR-禁倒")
    print("\n只撤掉这一块禁倒权：")
    print_query_brief(ban_bad["office"], "SR-禁倒")
    report["ban"] = {
        "signed": ban_ok["office"].query_order("SR-禁倒"),
        "revoked": ban_bad["office"].query_order("SR-禁倒"),
    }

    line("同一承诺的岔口分支：可达装车点与过岔口前观测")
    route_ok = build_route_case()
    route_bad = build_route_case(point_removed=True)
    route_late = build_route_case(late=True)
    print_query_brief(route_ok["office"], "SR-岔口")
    print("\n只撤掉甲侧可达装车点：")
    print_query_brief(route_bad["office"], "SR-岔口")
    print("\n只把观测推迟到车过岔口：")
    print_query_brief(route_late["office"], "SR-岔口")
    report["route"] = {
        "ok": route_ok["office"].query_order("SR-岔口"),
        "point_removed": route_bad["office"].query_order("SR-岔口"),
        "observation_late": route_late["office"].query_order("SR-岔口"),
    }

    line("案例二：甲乙两段须同时无雪；邻队 1kg 暂存，末车容量 1kg")
    base = build_fork_market_case()
    print_query_brief(base["office"], "ST-邻队")
    print_query_brief(base["office"], "SR-早市")
    print("\n两种均符合此前磅单的落堆实况，现场班在岔口前可见：")
    replay_rows = []
    for r in replay_fork_worlds(base):
        row = {
            "邻队落点": r["landing_segment"],
            "决策依据": r["decision_basis"],
            "末车去向": r["target_segment"],
            "装车": r["loaded"],
            "最终雪量": r["final"],
            "甲乙可同时开市": r["requested_open"],
            "可开市连续块": r["open_blocks"],
        }
        replay_rows.append(row)
        print("  " + json.dumps(row, ensure_ascii=False))
    report["fork_base"] = {
        "order": base["office"].query_order("SR-早市"),
        "replays": replay_rows,
    }

    line("单点反事实 A：只增加‘丙位已先租给别人’的凭据")
    rented = build_fork_market_case(c_rented=True, sign_ban=True)
    if rented["ban_error"]:
        print(f"  禁倒单签发失败: {rented['ban_error']}")
    print_query_brief(rented["office"], "ND-前夜")
    print_query_brief(rented["office"], "SR-早市")
    rented_rows = []
    for r in replay_fork_worlds(rented):
        rented_rows.append({
            "邻队落点": r["landing_segment"], "末车去向": r["target_segment"],
            "甲乙可同时开市": r["requested_open"], "连续块": r["open_blocks"],
        })
    for row in rented_rows:
        print("  " + json.dumps(row, ensure_ascii=False))
    report["c_rented"] = {
        "ban": rented["office"].query_order("ND-前夜"),
        "release": rented["office"].query_order("SR-早市"),
        "replays": rented_rows,
    }

    line("单点反事实 B：只把现场观测推迟到末车已过单行岔口")
    late = build_fork_market_case(late_observation=True)
    print_query_brief(late["office"], "SR-早市")
    reduced = late["office"].submit_release_order(
        "SR-乙丙", segments=["乙", "丙"], snow_line=1,
        method="FIXED_ROUTE", permit_ids=["ST-邻队"],
        forecast_segment="乙", capacity=1,
        title="接班人按固定乙侧路线改开乙丙两段",
    )
    print_query_brief(late["office"], "SR-乙丙")
    lucky = late["office"].replay_morning(
        "SR-早市", landing_segment="乙", observed=False,
        observation_timing="AFTER_FORK", forecast_segment="乙",
    )
    wrong = late["office"].replay_morning(
        "SR-早市", landing_segment="甲", observed=False,
        observation_timing="AFTER_FORK", forecast_segment="乙",
    )
    late_rows = [
        {"实况": "旧预计表碰巧正确", "邻队落点": "乙", "末车去向": "乙",
         "甲乙同时开市": lucky["requested_open"], "连续块": lucky["open_blocks"]},
        {"实况": "预计表错误且无法返回", "邻队落点": "甲", "末车去向": "乙",
         "甲乙同时开市": wrong["requested_open"], "阻滞": wrong["blocker"],
         "最终雪量": wrong["final"], "可开市连续块": wrong["open_blocks"]},
    ]
    for row in late_rows:
        print("  " + json.dumps(row, ensure_ascii=False))
    report["late_observation"] = {
        "original": late["office"].query_order("SR-早市"),
        "reduced": late["office"].query_order("SR-乙丙"),
        "replays": late_rows,
    }

    line("若前夜丙位空闲时已签禁倒：观测迟到不影响甲乙承诺")
    banned = build_fork_market_case(late_observation=True, sign_ban=True)
    print_query_brief(banned["office"], "ND-前夜")
    print_query_brief(banned["office"], "SR-早市")
    report["signed_ban"] = banned["office"].query_order("SR-早市")

    print("\n结论：只有每个可行世界都合格的整段才签 ACCEPTED；"
          "失败单保留可复核理由，并给出可另开的连续缩段。")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="write JSON report to demo/out")
    args = parser.parse_args()
    report = run()
    if args.json:
        out = Path(__file__).resolve().parent / "out" / "street_report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
        print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
