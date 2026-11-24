"""证据筛选：用原 pqscan 查询机器筛出“两晚开始前”的压样。

不新写一套过滤逻辑，而是把任务条件翻译成与 fixtures/queries.json 同格式的谓词树，
走与 `pqscan run` 完全相同的计划/扫描/核对代码路径：
- planner 只读 footer 统计做保守剪枝；
- scanner 两阶段读取，结果自带 _source_file/_source_row_group/_source_row_in_group；
- reference 无剪枝全扫描逐行核对，保证筛入的每一行都与 3VL 精确过滤一致。

“两晚开始前”定义为 taken_at < 第一晚起点 nights[0]：两晚期间新压的样不能作为
开印前的既有人工标记。任务 JSON 可再挂 proof_predicate 做附加限制（如限定盒号）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import pyarrow.parquet as pq

from ..errors import PlanError
from ..expr import Query, predicate_columns, validate, validate_projection
from ..planner import plan_file
from ..reference import reference_scan
from ..scanner import InstrumentedParquetFile, scan_query
from ..stats import read_footer_stats
from .model import Job

PROOF_COLUMNS = ("proof_id", "piece_id", "box_id", "taken_at", "feature", "present", "face_hint")


@dataclass(frozen=True)
class ProofEvidence:
    """一次接印所采信的压样证据。"""

    query: Query
    result_path: str
    plan_path: str
    rows: list[dict[str, Any]]
    """命中行（含 pqscan 三列溯源 + 扁平来源列）。"""
    verification_passed: bool
    verification_detail: str
    excluded_count: int
    """被时间/附加谓词排除的压样行数（全部输入文件口径）。"""


def build_proof_query(job: Job) -> Query:
    """构造压样筛选谓词：taken_at < nights[0] [AND 附加谓词]。"""
    before: dict[str, Any] = {"lt": ["taken_at", job.nights[0]]}
    predicate = {"and": [before, job.extra_predicate]} if job.extra_predicate else before
    return Query(id="proofs_before_nights", where=predicate, columns=list(PROOF_COLUMNS))


def admit_proofs(job: Job, out_dir: str) -> ProofEvidence:
    """跑原查询机器筛压样，并落盘计划/结果/核对结论。"""
    plans_dir = os.path.join(out_dir, "proofs")
    results_dir = os.path.join(out_dir, "proofs")
    os.makedirs(plans_dir, exist_ok=True)
    os.makedirs(results_dir, exist_ok=True)

    query = build_proof_query(job)
    files = list(job.proof_files)

    # 与 runner.run 一致：footer 打开一次，计划与 schema 校验共用。
    footer_pf = {path: pq.ParquetFile(path) for path in files}
    schemas = {path: pf.schema_arrow for path, pf in footer_pf.items()}
    for path in files:
        validate(query.where, schemas[path])
        validate_projection(query.columns, schemas[path])
    if set(predicate_columns(query.where)) & {"_source_file", "_source_row"}:
        raise PlanError("压样谓词引用了保留的来源列名")

    decisions_by_file = {
        path: plan_file(path, query.where, read_footer_stats(path, schemas[path]))
        for path in files
    }
    plan_doc = {
        "query_id": query.id,
        "predicate": query.where,
        "rule": "复用 pqscan：仅当证明 can_be_true=False 才跳过行组；"
                "只采信两晚开始前（taken_at < 第一晚起点）的压样，来源行全部保留",
        "files": [
            {
                "file": path,
                "row_groups": [
                    {
                        "row_group": d.row_group,
                        "num_rows": d.num_rows,
                        "action": d.action,
                        "reason": d.reason,
                        "can_be_true": d.can_be_true,
                        "can_be_false": d.can_be_false,
                        "missing_or_unreliable_stats": list(d.missing_stats),
                        "trace": d.trace.to_dict(),
                    }
                    for d in decisions
                ],
            }
            for path, decisions in decisions_by_file.items()
        ],
    }
    plan_path = os.path.join(plans_dir, f"{query.id}.plan.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan_doc, fh, ensure_ascii=False, indent=2)

    result_path = os.path.join(results_dir, f"{query.id}.parquet")
    scan_query(query, decisions_by_file, InstrumentedParquetFile, result_path)

    # 独立核对：无剪枝全扫描 + 纯 Python 3VL，与引擎结果逐行比对。
    ref_table = reference_scan(query, files)
    got_table = pq.read_table(result_path)
    passed = got_table.schema == ref_table.schema and got_table.to_pylist() == ref_table.to_pylist()
    detail = ("压样筛选结果与无剪枝全扫描逐行一致（含溯源列）" if passed
              else "压样筛选结果与独立核对不一致，禁止用于接印决策")

    rows = got_table.to_pylist()
    total = sum(pq.ParquetFile(p).metadata.num_rows for p in files)
    return ProofEvidence(
        query=query,
        result_path=result_path,
        plan_path=plan_path,
        rows=rows,
        verification_passed=passed,
        verification_detail=detail,
        excluded_count=total - len(rows),
    )
