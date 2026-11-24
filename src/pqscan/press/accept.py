"""接印入口：证据筛选 -> 身份推理 -> 两晚排班 -> 落盘领班凭据。

输出（均在 --out 下）：
- proofs/proofs_before_nights.plan.json ：复用 pqscan 的行组剪枝计划；
- proofs/proofs_before_nights.parquet   ：采信的压样行（含三列溯源）；
- proofs/verification.json              ：无剪枝全扫描独立核对结论；
- identity.json                         ：每粒字的观察聚合、候选字面、整盒数量分配；
- decision.json                         ：每笔订单接/拒结论、拿哪盒、何时动哪块版、台账。

整个流程只从命令行读一个任务 JSON，可任意重跑；决策确定性来自固定的枚举顺序。
"""
from __future__ import annotations

import json
import os
from typing import Any

import pyarrow.parquet as pq

from .evidence import admit_proofs
from .identity import IdentityReport, build_identity_report
from .model import (
    Job,
    Mark,
    SRC_FILE,
    SRC_ROW,
    load_box_counts,
    load_job,
    load_orders,
    validate_orders_against_job,
)
from .scheduler import Scheduler


def _marks_from_evidence(rows: list[dict[str, Any]], row_offsets: dict[str, list[int]]) -> list[Mark]:
    """把查询命中的溯源行转回 Mark（来源文件/行组/组内行号都不丢）。"""
    marks: list[Mark] = []
    for r in rows:
        source_file = r["_source_file"]
        row_group = r["_source_row_group"]
        row_in_group = r["_source_row_in_group"]
        marks.append(
            Mark(
                proof_id=str(r["proof_id"]),
                piece_id=str(r["piece_id"]),
                box_id=None if r["box_id"] is None else str(r["box_id"]),
                taken_at=r["taken_at"],
                feature=str(r["feature"]),
                present=r["present"],
                face_hint=None if r["face_hint"] is None else str(r["face_hint"]),
                source_file=source_file,
                # 扁平文件内的物理行号 = 前面行组累计行数 + 组内行号。
                source_row=row_offsets[source_file][row_group] + row_in_group,
            )
        )
    return marks


def _identity_to_dict(report: IdentityReport) -> dict[str, Any]:
    pieces_out = []
    for pid in sorted(report.pieces):
        ob = report.pieces[pid]
        box = report.boxes[ob.box_id]
        pieces_out.append(
            {
                "piece_id": pid,
                "box_id": ob.box_id,
                "usable": not ob.contradicted,
                "contradicted": ob.contradicted,
                "candidate_faces": sorted(box.candidate_faces.get(pid, set())),
                "committable_face": box.forced_face.get(pid),
                "face_hints": sorted(ob.face_hints),
                "features": [
                    {
                        "feature": feat,
                        "verdict": ev.verdict,
                        "contradicted": ev.contradicted,
                        "seen_true_rows": [m.source_row for m in ev.seen_true],
                        "seen_false_rows": [m.source_row for m in ev.seen_false],
                        "unknown_rows": [m.source_row for m in ev.unknown_marks],
                    }
                    for feat, ev in sorted(ob.features.items())
                ],
                "mark_sources": ob.mark_refs,
            }
        )
    boxes_out = []
    for box_id in sorted(report.boxes):
        ledger = report.boxes[box_id]
        boxes_out.append(
            {
                "box_id": box_id,
                "pieces": list(ledger.pieces),
                "cast_counts": ledger.cast_counts,
                "feasible": ledger.feasible,
                "reason": ledger.reason,
                "witness_assignment": ledger.witness,
                "forced_faces": [
                    {"piece_id": pid, "face": face}
                    for pid, face in sorted(ledger.forced_face.items())
                ],
            }
        )
    return {"pieces": pieces_out, "boxes": boxes_out}


def _decision_to_dict(job: Job, schedule, identity: IdentityReport, evidence) -> dict[str, Any]:
    orders_out = []
    for d in schedule.decisions:
        orders_out.append(
            {
                "order_id": d.order_id,
                "accepted": d.accepted,
                "night": d.night,
                "machine": d.machine,
                "route": d.route,
                "reason_code": d.reason_code,
                "reason": d.reason,
                "source": {SRC_FILE: d.source_file, SRC_ROW: d.source_row},
                "positions": [
                    {
                        "slot": pp.slot,
                        "required_face": pp.face,
                        "piece_id": pp.piece_id,
                        "box_id": pp.box_id,
                        "from_old_form": pp.from_old_form,
                    }
                    for pp in d.positions
                ],
            }
        )
    infeasible_boxes = [b for b in identity.boxes.values() if not b.feasible]
    return {
        "job": job.name,
        "summary": {
            "total_orders": len(schedule.decisions),
            "accepted": sum(1 for d in schedule.decisions if d.accepted),
            "refused": sum(1 for d in schedule.decisions if not d.accepted),
            "infeasible_boxes": [b.box_id for b in infeasible_boxes],
        },
        "evidence": {
            "admitted_proof_rows": len(evidence.rows),
            "excluded_proof_rows": evidence.excluded_count,
            "verification_passed": evidence.verification_passed,
            "verification_detail": evidence.verification_detail,
            "proof_result_parquet": evidence.result_path,
            "proof_plan_json": evidence.plan_path,
        },
        "global_warnings": [
            f"字盒 {b.box_id} 的数量记录无法兑现：{b.reason}，该盒身份不可作为排除依据"
            for b in infeasible_boxes
        ],
        "sealed_forms": [
            {
                "form_id": sf.form_id,
                "pieces": list(sf.pieces),
                "machine": sf.machine,
                "print_night": sf.print_night,
                "note": "封住的字两个夜班都借不出",
            }
            for sf in schedule.sealed_forms
        ],
        "orders": orders_out,
        "night_ledger": schedule.ledger,
    }


def accept_job(job_json: str, out_dir: str) -> dict[str, Any]:
    """命令行可重跑的接印主流程，返回落盘的决策文档。"""
    os.makedirs(out_dir, exist_ok=True)
    job = load_job(job_json)

    # 1) 原查询机器筛两晚开始前的压样（计划/扫描/独立核对，来源行全保留）。
    evidence = admit_proofs(job, out_dir)
    if not evidence.verification_passed:
        raise RuntimeError(evidence.verification_detail)
    with open(os.path.join(out_dir, "proofs", "verification.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "query_id": evidence.query.id,
                "passed": evidence.verification_passed,
                "detail": evidence.verification_detail,
                "result_rows": len(evidence.rows),
                "excluded_rows": evidence.excluded_count,
            },
            fh, ensure_ascii=False, indent=2,
        )

    # 2) 压样行 -> 字粒身份（3VL 聚合 + 盒数量 CSP）。
    row_offsets: dict[str, list[int]] = {}
    for path in job.proof_files:
        meta = pq.ParquetFile(path).metadata
        offsets: list[int] = []
        cumulative = 0
        for rgi in range(meta.num_row_groups):
            offsets.append(cumulative)
            cumulative += meta.row_group(rgi).num_rows
        row_offsets[path] = offsets
    marks = _marks_from_evidence(evidence.rows, row_offsets)
    box_counts = load_box_counts(job)
    identity = build_identity_report(marks, box_counts, job)
    with open(os.path.join(out_dir, "identity.json"), "w", encoding="utf-8") as fh:
        json.dump(_identity_to_dict(identity), fh, ensure_ascii=False, indent=2)

    # 3) 订单排班：封版/拆版两条路径下的可行做法或拒接凭据。
    orders = load_orders(job)
    validate_orders_against_job(orders, job)
    schedule = Scheduler(job, identity, orders).solve()
    decision = _decision_to_dict(job, schedule, identity, evidence)
    with open(os.path.join(out_dir, "decision.json"), "w", encoding="utf-8") as fh:
        json.dump(decision, fh, ensure_ascii=False, indent=2)
    return decision
