"""保修接单命令行。

子命令：
  build   生成夹具（原始证据 Parquet + 经 pqscan 扫描的结果 + 保修配置 JSON）
  evidence  按炉号/锅号调取底釉与温区证据（读 pqscan 扫描结果 Parquet）
  decide  为一张订单出操作单（可选 method）
  run     按配置顺序办理多张订单，写全部回单、台账与库存/见证锅复用总表
  levers  在两架四只锅范围内演示三种单点改动
  demo    一键：建夹具 → 办两张单 → 演示杠杆 → 落实一次拆检 → 落盘所有 JSON

用法示例：
  PYTHONPATH=src python -m pqscan.warranty.cli demo --out output/warranty
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .decision import StockLedger, decide, resolve_after_cut
from .fixtures import build as build_fixtures
from .levers import demonstrate_levers
from .model import (
    InventoryItem, WarrantyError, WitnessPot,
    inventory_from_list, problem_config_from_dict, witnesses_from_list,
)
from .reconstruct import load_evidence, select_order_pots
from .runner import OrderRequest, _resolve, process_order, run_orders


def _load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["_base_dir"] = os.path.dirname(os.path.abspath(path))
    return cfg


def _objs(cfg: dict[str, Any]):
    return (
        problem_config_from_dict(cfg["problem"]),
        inventory_from_list(cfg["inventory"]),
        witnesses_from_list(cfg["witnesses"]),
    )


def _dump(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def _cmd_build(args: argparse.Namespace) -> int:
    info = build_fixtures(args.dir)
    print(json.dumps({"raw": info["raw"], "config": info["config"],
                      "scanned": info["scanned"]}, ensure_ascii=False, indent=2))
    return 0


def _cmd_evidence(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    base = cfg["_base_dir"]
    if args.order:
        # 配置内路径为相对配置文件目录的文件名。
        paths = [p if os.path.isabs(p) else os.path.join(base, p)
                 for p in cfg["evidence_for_order"][args.order]]
    elif args.file:
        paths = [args.file]  # 显式给出，按调用者当前工作目录解析
    else:
        print("错误: 需要 --order 或 --file 指定证据 Parquet", file=sys.stderr)
        return 1
    pots, hangers = load_evidence(paths)
    furnace_pots = [p for p in pots if p.furnace_id == args.furnace]
    selected = select_order_pots(furnace_pots, args.pot) if args.pot else furnace_pots
    out = {
        "furnace_id": args.furnace,
        "pots": [
            {
                "pot_id": p.pot_id,
                "hanger_id": p.hanger_id,
                "possible_groups": None if p.possible_groups is None
                else sorted(g.label() for g in p.possible_groups),
                "unknown_reason": p.unknown_reason,
                "source_records": p.source_records(),
            }
            for p in selected
        ],
        "hangers": [h.to_dict() for k, h in sorted(hangers.items()) if k[0] == args.furnace],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _cmd_decide(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    problem, inventory, witnesses = _objs(cfg)
    ledger = StockLedger()
    req = OrderRequest(args.order, args.furnace, args.pot, args.method)
    res = process_order(req, cfg["evidence_for_order"][args.order], problem, inventory, witnesses,
                        ledger, base_dir=cfg["_base_dir"])
    _dump(args.out, res)
    print(res["summary"])
    print(f"回单已写入 {args.out}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    problem, inventory, witnesses = _objs(cfg)
    orders = [
        OrderRequest(o["order_id"], o["furnace_id"], o["pot_ids"], o.get("method", "auto"))
        for o in cfg["orders"]
    ]
    doc = run_orders(orders, cfg["evidence_for_order"], problem, inventory, witnesses,
                     base_dir=cfg["_base_dir"])
    _dump(args.out, doc)
    for rc in doc["receipts"]:
        print(f"[{rc['order']['order_id']}] {rc['verdict']}: {rc['summary']}")
    print(f"全部回单、台账与复用总表已写入 {args.out}")
    return 0


def _cmd_levers(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    problem, inventory, witnesses = _objs(cfg)
    req = OrderRequest(args.order, args.furnace, args.pot, args.method)
    extra_inv = InventoryItem(args.extra_item_id, args.extra_item_glaze, args.extra_item_zone,
                              {"note": "演示用追加合格库存锅"})
    repl_wit = WitnessPot(args.replacement_witness_id, args.replacement_witness_glaze, "high",
                          {"note": "演示用替换见证锅"})
    out = demonstrate_levers(
        req, cfg["evidence_for_order"][args.order], problem, inventory, witnesses,
        extra_orientation={(args.furnace, args.orientation_hanger): args.orientation_hot_row},
        extra_inventory=extra_inv,
        replacement_witness=repl_wit,
        removed_witness_id=args.remove_witness,
        base_dir=cfg["_base_dir"],
    )
    _dump(args.out, out)
    print(f"baseline: {out['baseline']['verdict']}")
    for name, lever in out["levers"].items():
        print(f"  {name}: {lever['result']['verdict']} (changed={lever['changed']})")
    print(f"杠杆对比已写入 {args.out}")
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    out = args.out
    os.makedirs(out, exist_ok=True)
    info = build_fixtures(os.path.join(out, "fixtures"))
    cfg = _load_config(info["config"])
    base = cfg["_base_dir"]
    problem, inventory, witnesses = _objs(cfg)

    orders = [
        OrderRequest(o["order_id"], o["furnace_id"], o["pot_ids"], o.get("method", "auto"))
        for o in cfg["orders"]
    ]
    doc = run_orders(orders, cfg["evidence_for_order"], problem, inventory, witnesses, base_dir=base)
    _dump(os.path.join(out, "orders_receipts.json"), doc)

    # 落实一次拆检：仅在结果已知后调用 resolve_after_cut，且 decide() 从不预挑分支。
    first_order = next(o for o in orders if o.order_id == "ORD-A")
    ev_a = [_resolve(p, base) for p in cfg["evidence_for_order"]["ORD-A"]]
    all_pots, _ = load_evidence(ev_a)
    order_pots = select_order_pots(
        [p for p in all_pots if p.furnace_id == first_order.furnace_id],
        first_order.pot_ids, first_order.furnace_id)
    clean_sheet = decide("ORD-A", order_pots, problem, inventory, witnesses, method="auto")
    resolved_neg = resolve_after_cut(clean_sheet, {"GY/high": "negative"}, inventory)
    resolved_pos = resolve_after_cut(clean_sheet, {"GY/high": "positive"}, inventory)
    _dump(os.path.join(out, "ord_A_cut_resolution.json"), {
        "pending_sheet_verdict": clean_sheet.verdict,
        "branches": clean_sheet.branches,
        "if_GY_high_negative": resolved_neg,
        "if_GY_high_positive": resolved_pos,
    })

    lever_out = demonstrate_levers(
        first_order, cfg["evidence_for_order"]["ORD-A"], problem, inventory, witnesses,
        extra_orientation={("furnace-alpha", "H2"): 1},
        extra_inventory=InventoryItem("R3", "GY", "low", {"note": "演示用追加合格库存锅"}),
        replacement_witness=WitnessPot("W-GX-HIGH", "GX", "high", {"note": "演示用替换见证锅"}),
        removed_witness_id="W-GY-HIGH",
        base_dir=base,
    )
    _dump(os.path.join(out, "order_A_levers.json"), lever_out)

    print("== 两炉两单 ==")
    for rc in doc["receipts"]:
        print(f"[{rc['order']['order_id']}] {rc['verdict']}: {rc['summary']}")
    print("  库存复用:", {k: v["reusable"] for k, v in doc["reuse_report"]["inventory_items"].items()})
    print("  见证锅复用:", {k: v["reusable"] for k, v in doc["reuse_report"]["witness_pots"].items()})
    print("== 拆检分支（结果到手后） ==")
    print("  阴性:", resolved_neg["verdict"], resolved_neg.get("released"))
    print("  阳性:", resolved_pos["verdict"], resolved_pos.get("refused_after_positive"),
          resolved_pos.get("replaced_after_positive"))
    print("== 三种杠杆 ==")
    print("  baseline:", lever_out["baseline"]["verdict"])
    for name, lever in lever_out["levers"].items():
        print(f"  {name}: {lever['result']['verdict']} (changed={lever['changed']})")
    print(f"\n全部产物已写入 {out}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pqscan.warranty", description="搪瓷锅保修接单")
    sub = parser.add_subparsers(dest="command", required=True)

    p_b = sub.add_parser("build", help="生成保修夹具")
    p_b.add_argument("--dir", default="fixtures/warranty")
    p_b.set_defaults(func=_cmd_build)

    p_e = sub.add_parser("evidence", help="按炉号/锅号调取底釉与温区证据")
    p_e.add_argument("--config", required=True)
    p_e.add_argument("--furnace", required=True)
    p_e.add_argument("--order", default=None)
    p_e.add_argument("--file", default=None)
    p_e.add_argument("--pot", action="append", default=None)
    p_e.set_defaults(func=_cmd_evidence)

    p_d = sub.add_parser("decide", help="为一张订单出操作单")
    p_d.add_argument("--config", required=True)
    p_d.add_argument("--order", required=True)
    p_d.add_argument("--furnace", required=True)
    p_d.add_argument("--pot", action="append", required=True)
    p_d.add_argument("--method", default="auto", choices=["auto", "record", "replace", "cut"])
    p_d.add_argument("--out", default="output/warranty/order_sheet.json")
    p_d.set_defaults(func=_cmd_decide)

    p_r = sub.add_parser("run", help="按配置办理多张订单")
    p_r.add_argument("--config", required=True)
    p_r.add_argument("--out", default="output/warranty/orders_receipts.json")
    p_r.set_defaults(func=_cmd_run)

    p_l = sub.add_parser("levers", help="演示三种单点改动")
    p_l.add_argument("--config", required=True)
    p_l.add_argument("--order", required=True)
    p_l.add_argument("--furnace", required=True)
    p_l.add_argument("--pot", action="append", required=True)
    p_l.add_argument("--method", default="auto", choices=["auto", "record", "replace", "cut"])
    p_l.add_argument("--orientation-hanger", default="H2")
    p_l.add_argument("--orientation-hot-row", type=int, default=1)
    p_l.add_argument("--extra-item-id", default="R3")
    p_l.add_argument("--extra-item-glaze", default="GY")
    p_l.add_argument("--extra-item-zone", default="low")
    p_l.add_argument("--replacement-witness-id", default="W-GX-HIGH")
    p_l.add_argument("--replacement-witness-glaze", default="GX")
    p_l.add_argument("--remove-witness", default="W-GY-HIGH")
    p_l.add_argument("--out", default="output/warranty/order_levers.json")
    p_l.set_defaults(func=_cmd_levers)

    p_demo = sub.add_parser("demo", help="一键演示")
    p_demo.add_argument("--out", default="output/warranty")
    p_demo.set_defaults(func=_cmd_demo)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except WarrantyError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
