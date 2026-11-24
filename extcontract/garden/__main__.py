"""Command line entry point: ``python -m extcontract.garden``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .scenario import run_story


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the spring garden hand-over scenario")
    parser.add_argument("--json", type=Path, help="write the full JSON report to this path")
    parser.add_argument(
        "--order",
        type=Path,
        help="write the next-shift hand-over order JSON to this path",
    )
    args = parser.parse_args()

    report = run_story()
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.order:
        args.order.parent.mkdir(parents=True, exist_ok=True)
        order = report["next_shift"]["h2_sign"].get("order")
        args.order.write_text(json.dumps(order, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    h1 = report["baseline"]["signed_order"]
    h2 = report["next_shift"]["h2_sign"]
    print(report["answer"])
    print(f"首户交单: {h1['order_id']} -> 地界 {h1['boundary']}")
    if h2.get("order"):
        print(f"次班交单: {h2['order']['order_id']} -> 真箱号 {h2['order']['box_ids']}")
    corr = report["corrected_B"]["eligibility"]
    print(
        "乙史更正后失格: "
        + ", ".join(lot for lot, item in corr.items() if not item["eligible"])
    )
    print(f"收窄过道: {report['restored_and_narrowed']['two_place_plan']['status']}")
    print(f"第三户: {report['next_shift']['h3_sign']['status']}")
    if args.json:
        print(f"完整报告: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
