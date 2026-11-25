"""车站报站词库/出声台命令行入口（乘务员可再次调用）。

工作区状态落盘在 ``--root/state.json``，所有命令可重复执行、跨进程续办。

子命令：
  station-init        初始化题面词库、接头表、班次、双店并首发
  station-catalog     看同一份资产目录与发版
  station-release     在当前工作态上再发一版
  station-evaluate    办理一个班次（整词或拼接），输出签播/拒播依据
  station-replay      旧播次按原音回听
  station-rejections  列出全部拒播依据（缺录档/误听/独占/窗不足…）
  station-ledger      查两店代价台账
  station-budget      查真实录音采样预算占用
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .contracts import ContractKind
from .hearing import HearingReport
from .render import SplicePlan, WholeWordPlan
from .schedule import PlayOrder, Rejection


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _hearing_json(h: HearingReport | None) -> dict | None:
    if h is None:
        return None
    return {
        "target": h.target, "neighbors": list(h.neighbors),
        "window_samples": h.window_samples,
        "rendered_samples": h.rendered_samples,
        "station_hits": list(h.station_hits),
        "checked": [{"station": c[0], "strict_match": c[1]} for c in h.checked],
        "window_ok": h.window_ok, "can_air": h.can_air,
        "reason": h.reason, "rendered_pcm": list(h.rendered_pcm),
    }


def _order_json(o: PlayOrder) -> dict:
    return {
        "trip_no": o.trip_no, "direction": o.direction, "target": o.target,
        "contract": o.contract, "release_version": o.release_version,
        "plan_kind": o.plan_kind, "asset_refs": list(o.asset_refs),
        "pcm_snapshot": list(o.pcm_snapshot),
        "hearing": _hearing_json(o.hearing),
    }


def _rejection_json(r: Rejection) -> dict:
    return {
        "trip_no": r.trip_no, "target": r.target, "contract": r.contract,
        "reason": r.reason, "detail": r.detail, "plan_kind": r.plan_kind,
        "asset_refs": list(r.asset_refs), "release_version": r.release_version,
        "hearing": _hearing_json(r.hearing),
    }


def _load(root: str):
    from .store import WorkspaceStore

    store = WorkspaceStore(root)
    if not store.exists():
        raise SystemExit(f"工作区尚未初始化：先执行 station-init --root {root}")
    return store, store.load()


def _save(store, state) -> None:
    store.save(
        state["lexicon"], state["splice_table"], state["schedule"],
        state["booking"], state["rental_ledger"], state["buyout_ledger"],
        state["rental_desk"], state["buyout_desk"],
    )


# ------------------------------- 子命令 ------------------------------- #

def cmd_init(p) -> None:
    # 延迟导入：init 复用 demo 的标准题面装配，保证 CLI 与叙事完全同源
    from demo.station_scenario import (
        build_lexicon, build_splice_table, build_schedule, build_desks,
    )
    from .store import WorkspaceStore

    store = WorkspaceStore(p.root)
    if store.exists() and not p.force:
        raise SystemExit(f"{store.path} 已存在；加 --force 覆盖重建")
    lex = build_lexicon()
    lex.release("CLI 首发：题面词库")
    splice = build_splice_table()
    schedule, booking, calendar = build_schedule()
    rental, buyout, rl, bl = build_desks(schedule, splice, booking, calendar)
    store.save(lex, splice, schedule, booking, rl, bl, rental, buyout)
    _print({"initialized": store.path,
            "assets": lex.catalog(),
            "recorded_samples": lex.recorded_samples,
            "releases": list(lex.releases)})


def cmd_catalog(p) -> None:
    _, state = _load(p.root)
    lex = state["lexicon"]
    _print({
        "releases": [
            {"version": r.version, "note": r.note,
             "asset_ids": sorted(r.assets),
             "station_templates": {
                 t.station: list(t.reference) for t in r.station_templates},
             "character_templates": [
                 {"character": t.character, "speaker": t.speaker,
                  "gear": t.gear, "reference": list(t.reference),
                  "tolerance": t.tolerance}
                 for t in r.character_templates]}
            for r in lex.releases.values()
        ],
        "catalog": lex.catalog(),
    })


def cmd_release(p) -> None:
    store, state = _load(p.root)
    rel = state["lexicon"].release(p.note)
    _save(store, state)
    _print({"released": rel.version, "note": rel.note,
            "asset_ids": sorted(rel.assets)})


def cmd_evaluate(p) -> None:
    store, state = _load(p.root)
    lex = state["lexicon"]
    rel = lex.get_release(p.release) if p.release else lex.latest_release()
    desk = state["rental_desk"] if p.contract == "rental" else state["buyout_desk"]
    if p.plan == "whole":
        decision = desk.evaluate_whole_word(rel, p.trip, p.asset)
    else:
        head, tail = p.splice
        decision = desk.evaluate_splice(rel, p.trip, head, tail)
    _save(store, state)
    payload = {
        "signed": decision.signed, "trip_no": decision.trip_no,
        "target": decision.target, "contract": decision.contract,
        "plan_kind": decision.plan_kind, "asset_refs": list(decision.asset_refs),
        "release_version": decision.release_version,
        "reason": decision.reason, "detail": decision.detail,
        "hearing": _hearing_json(decision.hearing),
        "deferred_slot": decision.deferred_slot,
        "order": _order_json(decision.order) if decision.order else None,
    }
    _print(payload)
    if not decision.signed and decision.deferred_slot is None:
        sys.exit(2)  # 拒播退出码，便于脚本编排


def cmd_replay(p) -> None:
    _, state = _load(p.root)
    desk = state["rental_desk"] if p.contract == "rental" else state["buyout_desk"]
    _print(_order_json(desk.replay(p.trip)))


def cmd_rejections(p) -> None:
    _, state = _load(p.root)
    _print([_rejection_json(r) for r in state["schedule"].rejections])


def cmd_ledger(p) -> None:
    _, state = _load(p.root)
    out = {}
    for key in ("rental_ledger", "buyout_ledger"):
        ledger = state[key]
        out[ledger.name] = {
            "total": ledger.total(),
            "entries": [
                {"trip_no": e.trip_no, "contract": e.contract,
                 "action": e.action, "asset_refs": list(e.asset_refs),
                 "unit_price": e.unit_price, "amount": e.amount, "note": e.note}
                for e in ledger.entries
            ],
        }
    _print(out)


def cmd_budget(p) -> None:
    _, state = _load(p.root)
    lex = state["lexicon"]
    _print({"recorded_samples": lex.recorded_samples,
            "sample_budget": lex.sample_budget,
            "within_budget": lex.recorded_samples <= lex.sample_budget})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m audiomix.announce.cli",
        description="车站报站词库发版、排播与试听入口",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("station-init", help="初始化题面工作区")
    sp.add_argument("--root", required=True)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("station-catalog", help="查资产目录与发版")
    sp.add_argument("--root", required=True)
    sp.set_defaults(func=cmd_catalog)

    sp = sub.add_parser("station-release", help="当前工作态再发一版")
    sp.add_argument("--root", required=True)
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_release)

    sp = sub.add_parser("station-evaluate", help="办理班次（签播/拒播）")
    sp.add_argument("--root", required=True)
    sp.add_argument("--contract", choices=[c.value for c in ContractKind],
                    required=True)
    sp.add_argument("--trip", type=int, required=True)
    sp.add_argument("--plan", choices=["whole", "splice"], required=True)
    sp.add_argument("--asset", help="整词计划的资产 id")
    sp.add_argument("--splice", nargs=2, metavar=("HEAD", "TAIL"),
                    help="拼接计划的词头/尾字资产 id")
    sp.add_argument("--release", help="指定发版版本，默认最新")
    sp.set_defaults(func=cmd_evaluate)

    sp = sub.add_parser("station-replay", help="旧播次按原音回听")
    sp.add_argument("--root", required=True)
    sp.add_argument("--contract", choices=[c.value for c in ContractKind],
                    required=True)
    sp.add_argument("--trip", type=int, required=True)
    sp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("station-rejections", help="全部拒播依据")
    sp.add_argument("--root", required=True)
    sp.set_defaults(func=cmd_rejections)

    sp = sub.add_parser("station-ledger", help="两店代价台账")
    sp.add_argument("--root", required=True)
    sp.set_defaults(func=cmd_ledger)

    sp = sub.add_parser("station-budget", help="录音采样预算")
    sp.add_argument("--root", required=True)
    sp.set_defaults(func=cmd_budget)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd == "station-evaluate":
        if args.plan == "whole" and not args.asset:
            raise SystemExit("整词计划必须提供 --asset")
        if args.plan == "splice" and not args.splice:
            raise SystemExit("拼接计划必须提供 --splice HEAD TAIL")
    args.func(args)


if __name__ == "__main__":
    main()
