"""本机返券柜台入口（可在本机办理新申请，并模拟两站断网）。

每个 SQLite 文件就是一个独立柜台：中心一个库，两个分站各一个库，分站之间
默认 *不共享* 私重与领取流水（断网）。跨站领券权通过显式的交出/接收消息
在本机模拟，消息带幂等 ``msg_id``，原站补送同一消息不会恢复已交出的权。

常用::

    python3 -m rebate.cli init --db demo/out/center.db --mode UNIFIED
    python3 -m rebate.cli scenario --db demo/out/center.db     # 题面四判 + 跨站 + 纠正
    python3 -m rebate.cli apply --db demo/out/center.db --period P1 --household 甲
    python3 -m rebate.cli receipt --db demo/out/center.db --receipt 1
    python3 -m rebate.cli reconcile --db demo/out/center.db

也可在 Python 里直接用 :class:`rebate.Counter` 办理任意新申请。
"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Optional

from .counter import (
    CENTER_STATION,
    Counter,
    MODE_STATION,
    MODE_UNIFIED,
)
from .network import NODE_BAG, ORIGIN_CENTER, ORIGIN_STATION, PartialWeigh


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# ------------------------------------------------------------- 场景构造
def build_two_bag_world(c: Counter, budget: int = 2) -> None:
    """题面标准世界：甲乙各交一袋，车净重 10，每袋纸重 4 或 6，返券线 4。"""
    c.register_station("S1")
    c.register_station("S2")
    c.register_household("甲")
    c.register_household("乙")
    c.open_period(
        "P1", reduction_line=4, claim_deadline=100,
        central_budget=budget if c.mode == MODE_UNIFIED else None,
    )
    if c.mode == MODE_STATION:
        c.allocate_budget("P1", budget, "S1")
        c.allocate_budget("P1", budget, "S2")
    c.add_box("BX", "TRIP1")
    c.add_bag("B甲", "甲", "BX", gross=6, tare_lo=0, tare_hi=2,
              allowed_paper=(4, 6))
    c.add_bag("B乙", "乙", "BX", gross=6, tare_lo=0, tare_hi=2,
              allowed_paper=(4, 6))
    c.add_trip_version("TRIP1", 10, 10)
    c.ensure_right("P1", "甲", "S1")
    c.ensure_right("P1", "乙", "S1")


def run_scenario(db: str) -> dict:
    """把题面要求的判定在同一个核算期依次演完，返回结构化结果。"""
    path = pathlib.Path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    c = Counter(str(path), mode=MODE_UNIFIED)
    out: dict = {"steps": []}

    def log(title: str, decision) -> None:
        out["steps"].append({
            "title": title,
            "receipt_id": decision.receipt_id,
            "result": decision.result,
            "reason": decision.reason,
            "proven": decision.proven,
            "worlds": [w.household_paper for w in decision.solve.worlds],
            "budget_remaining": decision.budget_remaining,
        })

    # 1) 仅有总量：两户都不能保证可领。
    build_two_bag_world(c, budget=2)
    log("1 仅车净重10、无个人称量 -> 两户均挂起", c.apply("P1", "甲"))
    log("1 同上 乙", c.apply("P1", "乙"))

    # 2) 甲自己出示 4：足额且有领券权 -> 签。
    c.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                               ORIGIN_CENTER, household_id="甲"))
    log("2 甲出示4kg -> 签券", c.apply("P1", "甲"))

    # 3) 预算改为零的独立库：同样证据不能签。
    zero_path = path.with_name(path.stem + "-zero-budget.db")
    if zero_path.exists():
        zero_path.unlink()
    cz = Counter(str(zero_path), mode=MODE_UNIFIED)
    build_two_bag_world(cz, budget=0)
    cz.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                                ORIGIN_CENTER, household_id="甲"))
    log("3 预算为0、甲出示4kg -> 挂起不签", cz.apply("P1", "甲"))

    # 4) 回到仅总量，只有乙向中心出示 6：统一封账能据此签给甲。
    c4 = Counter(":memory:", mode=MODE_UNIFIED)
    build_two_bag_world(c4, budget=2)
    c4.add_partial(PartialWeigh("W乙", NODE_BAG, "B乙", 6, 6,
                                ORIGIN_CENTER, household_id="乙"))
    log("4 仅乙出示6kg、统一封账 -> 反推甲=4 签给甲", c4.apply("P1", "甲"))

    # 5) 分站断网：S1 看不到 S2 的乙私重，甲留单；甲自己凭条到了再办。
    cs = Counter(":memory:", mode=MODE_STATION)
    build_two_bag_world(cs, budget=2)
    cs.add_partial(PartialWeigh("W乙", NODE_BAG, "B乙", 6, 6,
                                ORIGIN_STATION, station_id="S2",
                                household_id="乙"))
    log("5 分站S1看不到S2私重 -> 甲留单", cs.apply("P1", "甲", station_id="S1"))
    cs.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                                ORIGIN_STATION, station_id="S1",
                                household_id="甲"))
    log("5 甲本人凭条到达S1 -> 签券", cs.apply("P1", "甲", station_id="S1"))

    # 6) 跨站领券权：交出后两站不能花；接收后原站补送不恢复。
    cr = Counter(":memory:", mode=MODE_STATION)
    build_two_bag_world(cr, budget=2)
    cr.hand_over("P1", "甲", "S1", "S2", msg_id="M1")
    log("6 领券权交出在途 -> 两站均不能花",
        cr.apply("P1", "甲", station_id="S1"))
    cr.receive_right("P1", "甲", "S2", msg_id="M2")
    cr.hand_over("P1", "甲", "S1", "S2", msg_id="M1")  # 补送，幂等不恢复
    cr.add_partial(PartialWeigh("W甲2", NODE_BAG, "B甲", 4, 4,
                                ORIGIN_STATION, station_id="S2",
                                household_id="甲"))
    log("6 S2接收后签券、原站补送不恢复",
        cr.apply("P1", "甲", station_id="S2"))
    out["right_state_after_resend"] = cr.right_state("P1", "甲")["state"]

    # 7) 最后额度：预算1，甲签出后乙只能挂起。
    cl = Counter(":memory:", mode=MODE_UNIFIED)
    build_two_bag_world(cl, budget=1)
    cl.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                                ORIGIN_CENTER, household_id="甲"))
    cl.add_partial(PartialWeigh("W乙", NODE_BAG, "B乙", 6, 6,
                                ORIGIN_CENTER, household_id="乙"))
    log("7 最后额度 -> 甲签出", cl.apply("P1", "甲"))
    log("7 最后额度 -> 乙无预算挂起", cl.apply("P1", "乙"))

    # 8) 封账后称量纠正：只开差额争议，不另兑；版本链可回查。
    cd = Counter(":memory:", mode=MODE_UNIFIED)
    build_two_bag_world(cd, budget=5)
    cd.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                                ORIGIN_CENTER, household_id="甲"))
    issued = cd.apply("P1", "甲")
    cd.close_period("P1", at_time=90)
    cd.add_trip_version("TRIP1", 12, 12, source="weighing-correction")
    cd.correct_partial("W甲", "W甲.v2", 6, 6)
    dispute = cd.open_dispute("P1", "甲", original_receipt_id=issued.receipt_id)
    out["steps"].append({
        "title": "8 封账后纠正 净重10->12、甲4->6 -> 仅差额争议",
        "receipt_id": dispute.receipt_id,
        "result": dispute.result,
        "weight_delta": dispute.evidence["weight_delta"],
        "coupon_value": dispute.coupon_value,
        "trip_versions": cd.trip_versions("TRIP1"),
    })
    out["reconcile"] = cd.reconcile()
    c.close()
    return out


# ------------------------------------------------------------- CLI
def _open(args: argparse.Namespace) -> Counter:
    # 经营模式在建库时落盘，重开时自动恢复，子命令无需重复指定。
    return Counter(args.db)


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="社区废纸返券柜台")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="新建柜台库并开一个核算期")
    p_init.add_argument("--db", required=True)
    p_init.add_argument("--mode", choices=(MODE_UNIFIED, MODE_STATION),
                        default=MODE_UNIFIED)
    p_init.add_argument("--period", default="P1")
    p_init.add_argument("--line", type=int, default=4)
    p_init.add_argument("--deadline", type=int, default=100)
    p_init.add_argument("--budget", type=int, default=2)

    p_scenario = sub.add_parser("scenario", help="演完题面全部判定")
    p_scenario.add_argument("--db", required=True)

    p_apply = sub.add_parser("apply", help="办理一户新申请")
    p_apply.add_argument("--db", required=True)
    p_apply.add_argument("--period", required=True)
    p_apply.add_argument("--household", required=True)
    p_apply.add_argument("--station", default=CENTER_STATION)
    p_apply.add_argument("--time", type=int, default=None)
    p_apply.add_argument("--weigh-node", default=None,
                         help="现场自愿出示的局部称量节点（如袋号 B甲）")
    p_apply.add_argument("--weigh-value", type=int, default=None)
    p_apply.add_argument("--weigh-origin", choices=(ORIGIN_CENTER, ORIGIN_STATION),
                         default=ORIGIN_CENTER)

    p_rec = sub.add_parser("receipt", help="按回执号查签发/挂起原因与已兑额度")
    p_rec.add_argument("--db", required=True)
    p_rec.add_argument("--receipt", type=int, required=True)
    p_rec.add_argument("--role", choices=(ORIGIN_CENTER, ORIGIN_STATION, "HOUSEHOLD"),
                       default=ORIGIN_CENTER)
    p_rec.add_argument("--station", default=None)

    p_rec = sub.add_parser("reconcile", help="可回查核对")
    p_rec.add_argument("--db", required=True)

    p_tot = sub.add_parser("totals", help="公开车次净重总量")
    p_tot.add_argument("--db", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "init":
        path = pathlib.Path(args.db)
        path.parent.mkdir(parents=True, exist_ok=True)
        c = Counter(str(path), mode=args.mode)
        c.open_period(args.period, args.line, args.deadline,
                      central_budget=args.budget if args.mode == MODE_UNIFIED else None)
        if args.mode == MODE_STATION:
            c.register_station("S1")
            c.register_station("S2")
            c.allocate_budget(args.period, args.budget, "S1")
            c.allocate_budget(args.period, args.budget, "S2")
        _print({"opened": args.db, "mode": args.mode, "period": args.period})
        c.close()
        return

    if args.cmd == "scenario":
        _print(run_scenario(args.db))
        return

    if args.cmd == "apply":
        c = _open(args)
        client = None
        if args.weigh_node is not None and args.weigh_value is not None:
            client = PartialWeigh(
                weigh_id=f"CLI-{args.household}-{args.weigh_node}",
                node_kind=NODE_BAG, node_id=args.weigh_node,
                lo=args.weigh_value, hi=args.weigh_value,
                origin=args.weigh_origin,
                station_id=args.station if args.weigh_origin == ORIGIN_STATION else None,
                household_id=args.household,
            )
        d = c.apply(args.period, args.household, station_id=args.station,
                    at_time=args.time, client_partial=client)
        _print({
            "receipt_id": d.receipt_id, "result": d.result, "reason": d.reason,
            "proven": d.proven, "coupon_value": d.coupon_value,
            "reduction_line": d.reduction_line,
            "right_state": d.right_state,
            "budget_remaining": d.budget_remaining,
            "worlds": [w.household_paper for w in d.solve.worlds],
        })
        c.close()
        return

    if args.cmd == "receipt":
        c = _open(args)
        _print(c.receipt(args.receipt, viewer_role=args.role,
                         viewer_station=args.station))
        c.close()
        return

    if args.cmd == "reconcile":
        c = _open(args)
        errs = c.reconcile()
        _print({"ok": not errs, "errors": errs})
        c.close()
        return

    if args.cmd == "totals":
        c = _open(args)
        _print(c.public_totals())
        c.close()
        return


if __name__ == "__main__":
    main()
