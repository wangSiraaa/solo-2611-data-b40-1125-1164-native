"""证据 Parquet 的写出与「经 pqscan 扫描」的辅助。

- write_evidence_raw：按行组写一份原始证据 Parquet（列见 EVIDENCE_SCHEMA）。
- scan_furnace：用 pqscan 的 footer 计划 + 两阶段扫描，对某炉号做过滤，
  产出带溯源列（_source_file/_source_row_group/_source_row_in_group）的结果 Parquet，
  正是售后接单时要读的「刚扫出的 Parquet 结果」。
"""
from __future__ import annotations

import os
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from ..expr import Query
from ..planner import plan_file
from ..scanner import InstrumentedParquetFile, scan_query
from ..stats import read_footer_stats
from .reconstruct import EVIDENCE_COLUMNS

EVIDENCE_SCHEMA = pa.schema([
    pa.field("furnace_id", pa.string()),
    pa.field("hanger_id", pa.string()),
    pa.field("pot_id", pa.string()),
    pa.field("row", pa.int32()),
    pa.field("slot", pa.int32()),
    pa.field("glaze_batch", pa.string()),
    pa.field("hot_row", pa.int32()),
    pa.field("witness_plate_id", pa.string()),
])


def write_evidence_raw(path: str, row_groups: list[list[dict[str, Any]]]) -> None:
    """把若干行组写成一份 Parquet（write_statistics=True，可被 pqscan 剪枝）。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    writer = pq.ParquetWriter(path, EVIDENCE_SCHEMA, write_statistics=True)
    try:
        for group in row_groups:
            cols = {f.name: [row.get(f.name) for row in group] for f in EVIDENCE_SCHEMA}
            writer.write_table(pa.table(cols, schema=EVIDENCE_SCHEMA))
    finally:
        writer.close()


def scan_furnace(raw_path: str, furnace_id: str, out_path: str) -> dict[str, Any]:
    """用 pqscan 对某炉号扫描，写出带溯源的结果 Parquet；返回计划/读取摘要。"""
    schema = pq.ParquetFile(raw_path).schema_arrow
    where = {"eq": ["furnace_id", furnace_id]}
    query = Query(id=f"evidence_{furnace_id}", where=where, columns=EVIDENCE_COLUMNS)
    groups = read_footer_stats(raw_path, schema)
    decisions = {raw_path: plan_file(raw_path, where, groups)}
    run = scan_query(query, decisions, InstrumentedParquetFile, out_path)
    return {
        "query_id": query.id,
        "output_path": out_path,
        "result_rows": run.result_rows,
        "groups": [g.to_dict() for g in run.group_records],
    }
