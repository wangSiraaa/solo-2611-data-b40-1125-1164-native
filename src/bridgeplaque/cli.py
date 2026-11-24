"""命令行入口：馆员办理署年申请、凭单号查回、演示翻转。

用法：
    python -m bridgeplaque.cli issue  --fixtures fixtures_bridge --out out \
        --segment 南桥段 --beam 甲 --craft noninvasive
    python -m bridgeplaque.cli issue  ... --beam 乙            # 换一根梁再办一次
    python -m bridgeplaque.cli lookup --ledger out/plaque_ledger.json --plaque-id PB-0001
    python -m bridgeplaque.cli demo   --fixtures fixtures_bridge --out out_demo
"""
from __future__ import annotations

import argparse
import json
import sys

from . import PlaqueError
from .demo import run_demo
from .plaque import (Application, CRAFT_MICROCORE, CRAFT_NONINVASIVE,
                     issue_plaque, lookup_plaque)


def _print_record(rec: dict) -> None:
    head = f"[{rec['plaque_id']}] {rec['application']['segment']}" \
           f"{rec['application']['beam_id']}梁 工艺={rec['application']['craft_label']}"
    if rec["status"] == "issued":
        d = rec["dating"]
        print(f"{head} -> 签发：{d['event']}{d['year']}年")
    else:
        print(f"{head} -> 暂缓")
    print(rec["public_conclusion"])


def _cmd_issue(args: argparse.Namespace) -> int:
    app = Application(segment=args.segment, beam_id=args.beam, craft=args.craft)
    rec = issue_plaque(args.fixtures, args.out, app, ledger_path=args.ledger)
    _print_record(rec)
    return 0 if rec["status"] == "issued" else 3


def _cmd_lookup(args: argparse.Namespace) -> int:
    rec = lookup_plaque(args.ledger, args.plaque_id)
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    steps = run_demo(args.fixtures, args.out)
    print(f"{'演示':<16}{'单项改动':<28}{'结果'}")
    for s in steps:
        if s["status"] == "issued":
            d = s["dating"]
            outcome = f"签发 {d['event']}{d['year']}年"
        else:
            outcome = "暂缓"
        print(f"{s['demo']:<16}{s['change']:<28}{s['plaque_id']} {outcome}")
    print(f"\n各步台账与 pqscan 产物见 {args.out}/ 下对应子目录")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bridgeplaque", description="旧木桥展牌签发")
    sub = parser.add_subparsers(dest="command", required=True)

    p_issue = sub.add_parser("issue", help="办一份署年申请（查案卷+相核+登记台账）")
    p_issue.add_argument("--fixtures", required=True, help="夹具目录（files.json/band.json/workorder.json）")
    p_issue.add_argument("--out", required=True, help="本次办理的工作目录（pqscan 产物与默认台账）")
    p_issue.add_argument("--segment", required=True, help="桥段")
    p_issue.add_argument("--beam", required=True, help="梁号")
    p_issue.add_argument("--craft", default=CRAFT_NONINVASIVE,
                         choices=[CRAFT_NONINVASIVE, CRAFT_MICROCORE],
                         help="工艺：noninvasive 不动实物 / microcore 微芯取样")
    p_issue.add_argument("--ledger", default=None, help="台账路径（默认 <out>/plaque_ledger.json）")
    p_issue.set_defaults(func=_cmd_issue)

    p_lookup = sub.add_parser("lookup", help="凭展牌单号查回记录")
    p_lookup.add_argument("--ledger", required=True)
    p_lookup.add_argument("--plaque-id", required=True)
    p_lookup.set_defaults(func=_cmd_lookup)

    p_demo = sub.add_parser("demo", help="演示单项证据/许可改动如何翻转同一次署年")
    p_demo.add_argument("--fixtures", required=True)
    p_demo.add_argument("--out", required=True)
    p_demo.set_defaults(func=_cmd_demo)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PlaqueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
