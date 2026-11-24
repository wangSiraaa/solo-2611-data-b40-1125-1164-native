"""命令行入口。

用法：
    python -m pqscan.cli run --files fixtures/files.json --queries fixtures/queries.json \
        --out output
    python -m pqscan.cli verify --files ... --queries ... --out output   # 只跑独立核对
"""
from __future__ import annotations

import argparse
import json
import sys

from .errors import PqScanError
from .expr import parse_queries, validate, validate_projection
from .reference import reference_scan
from .runner import run
from .inspection import run_inspection
from .springdesk import SpringDeskError


def _cmd_run(args: argparse.Namespace) -> int:
    doc = run(args.files, args.queries, args.out, inspection=args.inspection)
    all_ok = all(q["passed"] for q in doc["verification"]["queries"])
    for q in doc["verification"]["queries"]:
        flag = "PASS" if q["passed"] else "FAIL"
        print(f"[verify:{flag}] {q['query_id']}: {q['detail']} "
              f"(engine={q['engine_rows']}, reference={q['reference_rows']})")
    inspection = doc.get("spring_inspection")
    if inspection:
        print(f"[spring] 受理 {len(inspection.get('orders', []))} 单；"
              f"接受 {inspection.get('accepted_order_ids')}，"
              f"拒绝 {inspection.get('rejected_order_ids')}；"
              f"报告 {args.out}/spring_inspection.json")
    print(f"\n计划与结果已写入 {args.out}/ (plans/、results/、run_record.json、verification.json)")
    return 0 if all_ok else 2


def _cmd_inspect(args: argparse.Namespace) -> int:
    with open(args.config, encoding="utf-8") as fh:
        config = json.load(fh)
    report = run_inspection(args.out, config)
    print(f"[spring] 接受 {report['accepted_order_ids']}；拒绝 {report['rejected_order_ids']}")
    print(f"[spring] 报告 {args.out}/spring_inspection.json")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    import os
    import pyarrow.parquet as pq

    fixture_dir = os.path.dirname(os.path.abspath(args.files))
    with open(args.files, encoding="utf-8") as fh:
        files = [os.path.join(fixture_dir, n) for n in json.load(fh)]
    with open(args.queries, encoding="utf-8") as fh:
        queries = parse_queries(json.load(fh))

    failed = 0
    for query in queries:
        for path in files:
            validate(query.where, pq.ParquetFile(path).schema_arrow)
            validate_projection(query.columns, pq.ParquetFile(path).schema_arrow)
        ref = reference_scan(query, files)
        result_path = os.path.join(args.out, "results", f"{query.id}.parquet")
        got = pq.read_table(result_path)
        same = got.schema == ref.schema and got.to_pylist() == ref.to_pylist()
        print(f"[{'PASS' if same else 'FAIL'}] {query.id}: {got.num_rows} 行")
        failed += 0 if same else 1
    return 0 if failed == 0 else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pqscan", description="Parquet footer 统计驱动的保守 plan/scan")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="计划 + 扫描 + 独立核对")
    p_run.add_argument("--files", required=True)
    p_run.add_argument("--queries", required=True)
    p_run.add_argument("--out", default="output")
    p_run.add_argument("--inspection", default=None,
                       help="弹簧检验/装机单配置 JSON；也可在 queries 文档中放 spring_inspection")
    p_run.set_defaults(func=_cmd_run)

    p_ver = sub.add_parser("verify", help="对已产出的结果再跑一次无剪枝全扫描核对")
    p_ver.add_argument("--files", required=True)
    p_ver.add_argument("--queries", required=True)
    p_ver.add_argument("--out", default="output")
    p_ver.set_defaults(func=_cmd_verify)

    p_inspect = sub.add_parser("inspect", help="从 output/results 的选定 Parquet 执行弹簧检验")
    p_inspect.add_argument("--out", default="output")
    p_inspect.add_argument("--config", required=True, help="弹簧检验/装机单 JSON 配置")
    p_inspect.set_defaults(func=_cmd_inspect)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (PqScanError, SpringDeskError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
