"""命令行入口。

用法：
    python -m pqscan.cli run --files fixtures/files.json --queries fixtures/queries.json \
        --out output
    python -m pqscan.cli verify --files ... --queries ... --out output   # 只跑独立核对
    python -m pqscan.cli accept --job fixtures/press/<job>.json --out output/press/<job>
"""
from __future__ import annotations

import argparse
import json
import sys

from .errors import PqScanError
from .expr import parse_queries, validate, validate_projection
from .reference import reference_scan
from .runner import run


def _cmd_run(args: argparse.Namespace) -> int:
    doc = run(args.files, args.queries, args.out)
    all_ok = all(q["passed"] for q in doc["verification"]["queries"])
    for q in doc["verification"]["queries"]:
        flag = "PASS" if q["passed"] else "FAIL"
        print(f"[verify:{flag}] {q['query_id']}: {q['detail']} "
              f"(engine={q['engine_rows']}, reference={q['reference_rows']})")
    print(f"\n计划与结果已写入 {args.out}/ (plans/、results/、run_record.json、verification.json)")
    return 0 if all_ok else 2


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


def _cmd_accept(args: argparse.Namespace) -> int:
    """接印入口：一个任务 JSON 重跑两晚领物票的接/拒决策。"""
    from .press.accept import accept_job

    doc = accept_job(args.job, args.out)
    for order in doc["orders"]:
        flag = "接单" if order["accepted"] else "拒接"
        print(f"[{flag}] {order['order_id']}（第 {order['night'] + 1} 晚/机器 {order['machine']}）"
              f"{('/' + order['route']) if order['route'] else ''}: {order['reason']}")
    print(f"\n汇总：接 {doc['summary']['accepted']} / 拒 {doc['summary']['refused']}；"
          f"采信压样 {doc['evidence']['admitted_proof_rows']} 行、"
          f"排除 {doc['evidence']['excluded_proof_rows']} 行、"
          f"独立核对 {'PASS' if doc['evidence']['verification_passed'] else 'FAIL'}")
    print(f"凭据已写入 {args.out}/ (proofs/、identity.json、decision.json)")
    # 拒接是正常业务结论，不是工具错误；只有输入不合法才会走异常分支返回 1。
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pqscan", description="Parquet footer 统计驱动的保守 plan/scan")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="计划 + 扫描 + 独立核对")
    p_run.add_argument("--files", required=True)
    p_run.add_argument("--queries", required=True)
    p_run.add_argument("--out", default="output")
    p_run.set_defaults(func=_cmd_run)

    p_ver = sub.add_parser("verify", help="对已产出的结果再跑一次无剪枝全扫描核对")
    p_ver.add_argument("--files", required=True)
    p_ver.add_argument("--queries", required=True)
    p_ver.add_argument("--out", default="output")
    p_ver.set_defaults(func=_cmd_verify)

    p_acc = sub.add_parser("accept", help="接印：压样证据 -> 字粒身份 -> 两晚排班决策")
    p_acc.add_argument("--job", required=True, help="接印任务 JSON（见 fixtures/press/）")
    p_acc.add_argument("--out", default="output/press")
    p_acc.set_defaults(func=_cmd_accept)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PqScanError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
