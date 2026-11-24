"""把弹簧检验入口接到 pqscan 的真实产物 ``results/<batch>.parquet``。

``runner.run`` 在每条查询的 Parquet 写出并完成独立核对后调用
:func:`run_inspection`。这里绝不回头读 fixtures 原始文件来冒充“选定批次”：
输入必须是本次 run 落到 ``<out_dir>/results`` 下的结果 Parquet，因此每行自带
``_source_file/_source_row_group/_source_row_in_group`` 物理溯源。
"""

from __future__ import annotations

import json
import os
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .springdesk import (
    InstallationDesk,
    SpringDeskError,
    build_curves,
)

INSPECTION_FILENAME = "spring_inspection.json"
ORDERS_FILENAME = "installation_orders.json"
CURVES_FILENAME = "spring_curves.parquet"


def result_parquet_path(out_dir: str, config: dict[str, Any]) -> str:
    explicit = config.get("result_parquet") or config.get("parquet_path")
    if explicit:
        return os.path.abspath(explicit)
    result_id = (
        config.get("result")
        or config.get("result_id")
        or config.get("selected_batch")
        or config.get("batch_result")
        or config.get("query_id")
        or config.get("batch")
        or "spring_batch"
    )
    return os.path.join(os.path.abspath(out_dir), "results", f"{result_id}.parquet")


def _state_path(out_dir: str, config: dict[str, Any]) -> str:
    path = config.get("desk_state_path") or config.get("state_path") or config.get("ledger_path")
    if path:
        return os.path.abspath(path)
    return os.path.join(os.path.abspath(out_dir), "spring_desk_state.json")


def read_selected_result(out_dir: str, config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = result_parquet_path(out_dir, config)
    if not os.path.exists(path):
        raise SpringDeskError(
            f"弹簧检验选定批次尚未由 runner 写到 results: {path}"
        )
    table = pq.read_table(path)
    rows = table.to_pylist()
    metadata = {
        "result_parquet": path,
        "result_rows": table.num_rows,
        "schema_names": table.schema.names,
        "physical_provenance_preserved": all(
            c in table.schema.names
            for c in ("_source_file", "_source_row_group", "_source_row_in_group")
        ),
    }
    return rows, metadata


def _write_curves_parquet(path: str, curves: dict[str, dict[str, Any]]) -> int:
    flat: list[dict[str, Any]] = []
    for curve in curves.values():
        for point in curve.get("points", []):
            source = point.get("source") or {}
            flat.append({
                "spring_id": point["spring_id"],
                "trial_id": point["trial_id"],
                "batch_id": point.get("batch_id"),
                "direction": point["direction"],
                "force_level": str(point["force_level"]),
                "raw_displacement": point["raw_displacement"],
                "zero_correction": point["zero_correction"],
                "displacement": point["displacement"],
                "observation_ids": ",".join(
                    c.get("observation_id") or "" for c in point.get("applied_corrections", [])
                ).strip(","),
                "_source_file": source.get("_source_file"),
                "_source_row_group": source.get("_source_row_group"),
                "_source_row_in_group": source.get("_source_row_in_group"),
                "result_row_index": source.get("result_row_index"),
            })
    if flat:
        table = pa.Table.from_pylist(flat)
    else:
        table = pa.table({
            "spring_id": pa.array([], pa.string()),
            "trial_id": pa.array([], pa.string()),
            "direction": pa.array([], pa.string()),
            "force_level": pa.array([], pa.string()),
            "displacement": pa.array([], pa.float64()),
            "_source_file": pa.array([], pa.string()),
            "_source_row_group": pa.array([], pa.int32()),
            "_source_row_in_group": pa.array([], pa.int64()),
        })
    pq.write_table(table, path)
    return table.num_rows


def run_inspection(out_dir: str, config: dict[str, Any]) -> dict[str, Any]:
    """读取 results 中的选定 Parquet，恢复曲线并受理一张或多张装机单。"""
    if not isinstance(config, dict):
        raise SpringDeskError(f"spring inspection 配置必须是对象: {config!r}")

    rows, input_meta = read_selected_result(out_dir, config)
    state_path = _state_path(out_dir, config)
    if config.get("fresh_state") and os.path.exists(state_path):
        os.unlink(state_path)

    desk = InstallationDesk(state_path)
    if config.get("springs") or config.get("inventory"):
        desk.receive_springs(config.get("springs") or config.get("inventory"))
    for obs in config.get("start_observations", []):
        desk.record_start_observation(**obs)
    for obs in config.get(
        "observations",
        config.get("corrections", config.get("supplements", [])),
    ):
        desk.record_observation(obs)

    corrections = desk.active_corrections()
    curves = build_curves(
        rows,
        corrections,
        trial_id=config.get("trial_id"),
        batch_id=config.get("batch_id"),
    )

    curves_path = os.path.join(os.path.abspath(out_dir), CURVES_FILENAME)
    curve_rows = _write_curves_parquet(curves_path, curves)

    raw_orders = config.get("orders")
    if raw_orders is None and (config.get("order") or config.get("order_id")):
        raw_orders = [config.get("order") if isinstance(config.get("order"), dict) else config]
    raw_orders = raw_orders or []

    order_reports: list[dict[str, Any]] = []
    reopen_between = bool(config.get("reopen_between_orders", True))
    for index, order_req in enumerate(raw_orders):
        # 允许单张单改读另一批 result Parquet；仍然必须来自 results。
        order_config = dict(config)
        order_config.update(order_req if isinstance(order_req, dict) else {})
        order_rows = rows
        if order_config.get("result") or order_config.get("result_id"):
            order_rows, _ = read_selected_result(out_dir, order_config)

        if reopen_between and index > 0:
            desk.close()
            desk = InstallationDesk.reopen(state_path)

        report = desk.submit_order(
            order_req,
            order_rows,
            corrections=[],  # 补录已在可重开台账中，避免重复追加。
            raise_on_reject=bool(config.get("raise_on_reject", False)),
        )
        order_reports.append(report)

    desk.close()

    accepted = [r["order_id"] for r in order_reports if r.get("accepted")]
    rejected = [r["order_id"] for r in order_reports if not r.get("accepted")]
    chosen = next((r for r in reversed(order_reports) if r.get("accepted")), None)
    report_doc = {
        "entry": "pqscan.runner.run -> run_spring_inspection",
        "selected_batch": config.get("selected_batch", config.get("result", config.get("result_id"))),
        **input_meta,
        "curves_parquet": curves_path,
        "curve_point_rows": curve_rows,
        "curve_spring_ids": sorted(curves),
        "curves": curves,
        "corrections": corrections,
        "desk_state_path": state_path,
        "orders": order_reports,
        "accepted_order_ids": accepted,
        "rejected_order_ids": rejected,
        "chosen_order_id": None if chosen is None else chosen["order_id"],
        "business_choice": config.get(
            "business_choice",
            "按工单 mode 执行；direct 占用实簧，cam 使用指定在机簧对与空拍",
        ),
    }

    out_path = os.path.join(os.path.abspath(out_dir), INSPECTION_FILENAME)
    orders_dir = os.path.join(os.path.abspath(out_dir), "orders")
    os.makedirs(orders_dir, exist_ok=True)
    for index, order in enumerate(order_reports, start=1):
        safe_id = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_"
                          for ch in str(order.get("order_id", index)))
        ticket_path = os.path.join(orders_dir, f"{index:02d}-{safe_id}.json")
        with open(ticket_path, "w", encoding="utf-8") as fh:
            json.dump(order, fh, ensure_ascii=False, indent=2, default=str)
        order["ticket_path"] = ticket_path

    report_doc["orders_dir"] = orders_dir
    report_doc["report_path"] = out_path
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report_doc, fh, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(os.path.abspath(out_dir), ORDERS_FILENAME), "w", encoding="utf-8") as fh:
        json.dump(order_reports, fh, ensure_ascii=False, indent=2, default=str)
    return report_doc


# 近义入口名。
inspect_spring_batch = run_inspection
run_spring_inspection = run_inspection
inspect = run_inspection
load_selected_batch = read_selected_result


class SpringInspector:
    """面向对象别名：绑定 out_dir 后重复受理检验。"""

    def __init__(self, out_dir: str):
        self.out_dir = out_dir

    def read_batch(self, config: dict[str, Any]):
        return read_selected_result(self.out_dir, config)

    def run(self, config: dict[str, Any]) -> dict[str, Any]:
        return run_inspection(self.out_dir, config)

    inspect = run
