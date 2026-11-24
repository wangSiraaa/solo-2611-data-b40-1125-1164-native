"""触读地图展的人工检查命令（复用主目录库与任务行）。

示例
====
::

    python -m confcat.tactile.cli publish data/tactile/catalog.db \\
        tactile-dot samples/tactile/dot_product_v1.yaml \\
        tactile-base samples/tactile/base_product_v1.yaml
    python -m confcat.tactile.cli audit data/tactile/catalog.db \\
        samples/tactile/exhibition.yaml \\
        data/tactile/dot_tasks.csv data/tactile/base_tasks.csv
    python -m confcat.tactile.cli audit ... --policy none
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..catalog import Catalog
from .domain import AnchorEndPolicy, NoMarkerPolicy
from .exhibition import load_exhibition
from .placement import evaluate, plan_exhibition
from .plates import load_base_bundle, load_plate_bundle


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _policy(name: str, anchor_end: str):
    if name == "none":
        return NoMarkerPolicy()
    if name == "anchor-end":
        return AnchorEndPolicy(anchor_end=anchor_end)
    raise SystemExit(f"未知方向标记策略 {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="confcat-tactile", description="触读地图展制牌/布牌工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pub = sub.add_parser("publish", help="把两份归一任务配置发布为目录产品版本")
    p_pub.add_argument("db")
    p_pub.add_argument("dot_product")
    p_pub.add_argument("dot_yaml")
    p_pub.add_argument("base_product")
    p_pub.add_argument("base_yaml")

    p_audit = sub.add_parser("audit", help="布牌员：触读唯一性审计与布展规划")
    p_audit.add_argument("db")
    p_audit.add_argument("exhibition_yaml")
    p_audit.add_argument("dot_csv")
    p_audit.add_argument("base_csv")
    p_audit.add_argument("--dot-product", default="tactile-dot")
    p_audit.add_argument("--base-product", default="tactile-base")
    p_audit.add_argument("--policy", choices=["none", "anchor-end"], default=None,
                         help="覆盖布展 YAML 的方向标记策略")
    p_audit.add_argument("--anchor-end", default="R")
    p_audit.add_argument("--allow-reengrave", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "publish":
        with Catalog(args.db) as cat:
            d = cat.publish_file(args.dot_product, Path(args.dot_yaml))
            b = cat.publish_file(args.base_product, Path(args.base_yaml))
        _print({
            "dot": {"product": args.dot_product, "version": d.version},
            "base": {"product": args.base_product, "version": b.version},
        })
        return 0

    with Catalog(args.db) as cat:
        plates = load_plate_bundle(cat, args.dot_product, "doc-0", args.dot_csv)
        bases = load_base_bundle(cat, args.base_product, "doc-0", args.base_csv)
        ex = load_exhibition(args.exhibition_yaml, plates, bases)
        policy = _policy(args.policy, args.anchor_end) if args.policy else ex.marker_policy

        confusions = evaluate(ex.booths)
        plan = plan_exhibition(ex.booths, ex.inventory, policy,
                               args.allow_reengrave or ex.reengrave_allowed)
        report = {
            "exhibition": ex.name,
            "marker_policy": policy.describe(),
            "reengrave_allowed": plan.reengrave_allowed,
            "confusion_count": len(confusions),
            "confusions": [
                {
                    "felt": c.first.render(),
                    "first": c.first.describe(),
                    "second": c.second.describe(),
                }
                for c in confusions
            ],
            "booths": [
                {
                    "booth": bp.booth_id,
                    "openable": bp.openable,
                    "blocked_reasons": bp.blocked_reasons,
                    "entrances": [
                        {"entrance": ep.entrance_id, "base": ep.assigned_base, "openable": ep.openable}
                        for ep in bp.entrances
                    ],
                    "reengrave_proposals": [p.describe() for p in bp.proposals],
                }
                for bp in plan.booth_plans
            ],
            "base_usage": {k: v for k, v in plan.base_usage.items() if v},
            "leftover_bases": list(plan.leftover_bases),
        }
        _print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
