"""接印子系统测试：证据筛选、3VL 身份推理、盒数量 CSP、两晚排班。

运行：PYTHONPATH=src .venv/bin/python -m pytest tests/test_press.py -q
"""
from __future__ import annotations

import json
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pqscan.cli import main as cli_main
from pqscan.press.accept import _marks_from_evidence
from pqscan.press.evidence import admit_proofs
from pqscan.press.identity import aggregate_marks, build_identity_report
from pqscan.press.model import (
    BoxCount,
    Mark,
    Order,
    Position,
    PressError,
    load_box_counts,
    load_job,
    load_orders,
)
from pqscan.press.scheduler import (
    REASON_BOX_CLOSED,
    REASON_BOX_INFEASIBLE,
    REASON_MACHINE_BUSY,
    REASON_NO_PIECE,
    REASON_NO_TABLE_SLOT,
    REASON_PIECE_BUSY,
    Scheduler,
)

PRESS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "press")


def _job(name: str):
    return load_job(os.path.join(PRESS, name))


def _row_offsets(path: str) -> list[int]:
    meta = pq.ParquetFile(path).metadata
    offsets: list[int] = []
    cumulative = 0
    for rgi in range(meta.num_row_groups):
        offsets.append(cumulative)
        cumulative += meta.row_group(rgi).num_rows
    return offsets


def _report(job, tmp_path=None):
    """直接用任务夹具跑出身份报告（证据筛选 + 推理）。"""
    out = os.path.join(str(tmp_path) if tmp_path else "output/pytest", job.name)
    evidence = admit_proofs(job, out)
    offsets = {path: _row_offsets(path) for path in job.proof_files}
    marks = _marks_from_evidence(evidence.rows, offsets)
    return build_identity_report(marks, load_box_counts(job), job)


def _decide(job_name: str, tmp_path):
    from pqscan.press.accept import accept_job

    out = str(tmp_path / job_name)
    doc = accept_job(os.path.join(PRESS, job_name), out)
    return doc, out


# ---------------------------------------------------------------------------
# 证据筛选：复用原查询，迟到压样被挡、来源行保留、独立核对通过
# ---------------------------------------------------------------------------

def test_evidence_uses_pqscan_and_excludes_late_proofs(tmp_path):
    job = _job("job_small_busy.json")
    out = str(tmp_path / "ev")
    from pqscan.press.evidence import admit_proofs

    evidence = admit_proofs(job, out)
    assert evidence.verification_passed is True
    # 22 行标记里两行 taken_at=140/150 在第一晚起点 100 之后，必须被排除。
    assert evidence.excluded_count == 2
    assert len(evidence.rows) == 20
    assert all(r["taken_at"] < 100 for r in evidence.rows)
    # 溯源三列随结果落盘。
    got = pq.read_table(evidence.result_path).to_pylist()
    assert {"_source_file", "_source_row_group", "_source_row_in_group"} <= set(got[0])
    plan = json.load(open(evidence.plan_path, encoding="utf-8"))
    assert plan["predicate"] == {"lt": ["taken_at", 100]}


def test_late_contradicting_proof_cannot_poison_identity(tmp_path):
    """迟到压样若被采信会让 P3 自相矛盾；时间谓词挡住后 P3 仍是干净的甲。"""
    job = _job("job_lock.json")
    report = _report(job)
    p3 = report.pieces["P3"]
    assert not p3.contradicted
    assert report.committable_face("P3") == "甲"


# ---------------------------------------------------------------------------
# 3VL 观察聚合
# ---------------------------------------------------------------------------

def _mark(piece, feature, present, hint=None, box="B1", proof="pr", at=10):
    return Mark(proof, piece, box, at, feature, present, hint, "f", 0)


def test_unknown_mark_is_incomplete_observation():
    faces = {"甲": frozenset({"竖", "横"}), "乙": frozenset({"竖"})}
    job = _job("job_small_busy.json")
    marks = [_mark("PX", "竖", None), _mark("PX", "横", True)]
    obs = aggregate_marks(marks, job)["PX"]
    ev = obs.features["竖"]
    assert ev.verdict is None and ev.unknown_marks and not ev.contradicted
    # 横看清为有 -> 乙（无横）被排除，甲保留；UNKNOWN 的竖不否定任何字面。
    assert obs.candidate_faces(faces) == {"甲"}


def test_contradictory_clear_marks_make_piece_unusable():
    job = _job("job_small_busy.json")
    marks = [
        _mark("PX", "缺口J", True),
        _mark("PX", "缺口J", False, proof="pr2", at=30),
    ]
    obs = aggregate_marks(marks, job)["PX"]
    assert obs.contradicted and obs.candidate_faces(job.faces) == set()


def test_same_piece_marks_must_explain_one_face_including_hint():
    job = _job("job_small_busy.json")
    # 看清的笔画指向甲，人工 hint 却说是乙：无法解释为同一字面。
    marks = [
        _mark("PX", "缺口J", True),
        _mark("PX", "缺口Y", False, hint="乙"),
    ]
    obs = aggregate_marks(marks, job)["PX"]
    assert obs.hint_contradiction and obs.contradicted


# ---------------------------------------------------------------------------
# 盒数量 CSP
# ---------------------------------------------------------------------------

def test_box_count_forces_unidentifiable_single_piece():
    """P4 单粒候选 {甲,乙}，但整盒数量唯一把它逼成甲（数量排除分配）。"""
    job = _job("job_proof_flip_clear.json")
    report = _report(job)
    b2 = report.boxes["B2"]
    assert b2.feasible
    assert report.pieces["P4"].candidate_faces(job.faces) == {"甲", "乙"}
    assert b2.forced_face["P4"] == "甲"
    assert report.committable_face("P4") == "甲"


def test_box_count_cannot_force_when_multiple_allocations():
    """vague 版三粒共享 {甲,乙} 候选，三种分配并存，没有甲能被唯一逼出。"""
    job = _job("job_proof_flip_vague.json")
    report = _report(job)
    b2 = report.boxes["B2"]
    assert b2.feasible
    assert "P3" not in b2.forced_face and "P4" not in b2.forced_face
    assert report.committable_face("P3") is None
    assert report.committable_face("P4") is None
    assert report.committable_face("P6") == "乙"


def test_infeasible_box_counts(tmp_path):
    """铸字数量与盒内字粒数对不上时，盒台账不可兑现，不能作为排除依据。"""
    job = _job("job_small_busy.json")
    bad_counts = [
        BoxCount("B1", "甲", 5, "f", 0),
        BoxCount("B2", "甲", 2, "f", 1),
        BoxCount("B2", "乙", 2, "f", 2),
    ]
    # B1 两粒 + B2 四粒都给看清标记，使不可兑现只来自 B1 的数量记录。
    marks = [
        _mark("P1", "缺口J", True, box="B1"), _mark("P1", "缺口Y", False, box="B1"),
        _mark("P2", "缺口Y", True, box="B1"), _mark("P2", "缺口J", False, box="B1"),
        _mark("P3", "缺口J", True, box="B2"), _mark("P3", "缺口Y", False, box="B2"),
        _mark("P4", "缺口J", True, box="B2"), _mark("P4", "缺口Y", False, box="B2"),
        _mark("P5", "缺口Y", True, box="B2"), _mark("P5", "缺口J", False, box="B2"),
        _mark("P6", "缺口Y", True, box="B2"), _mark("P6", "缺口J", False, box="B2"),
    ]
    report = build_identity_report(marks, bad_counts, job)
    assert report.boxes["B1"].feasible is False
    assert report.boxes["B2"].feasible is True
    # 不可兑现的盒不产出任何可交付身份。
    assert report.committable_face("P1") is None
    # 排班器登记 box_ledger_infeasible（优先级低于 no_eligible_piece，
    # 此处 B1 本就无身份可交付，故最终拒因为 no_eligible_piece，但盒告警同时在案）。
    orders = load_orders(job)
    result = Scheduler(job, report, orders).solve()
    assert all(not d.accepted for d in result.decisions)
    # 构造一张 B2 可满足、B1 不可兑现的混盒订单，盒告警仍应登记在 blockers 中。
    mixed = Order(
        order_id="OM", night=0, machine="M9",
        positions=(Position("s1", "甲"),), source_box=None,
        allowed_routes=("recompose", "seal"), source_file="x", source_row=0,
    )
    feasible, blockers = Scheduler(job, report, [mixed])._try_order(
        mixed, Scheduler(job, report, [mixed])._initial_state()
    )
    assert REASON_BOX_INFEASIBLE in blockers
    assert feasible  # B2 的甲仍可正常交付，坏盒不株连好盒


def test_broken_form_ledger_only_claims_broken_night(tmp_path):
    """旧版拆字只在拆字当班（第二晚）留台账；第一晚没有任何拆字记录。"""
    doc, _ = _decide("job_table_flip_full.json", tmp_path)
    night1, night2 = doc["night_ledger"]
    assert night1["broken_pieces"] == [] and night1["old_forms_broken"] == []
    assert {p["piece_id"] for p in night2["broken_pieces"]} == {"P3", "P4"}
    assert all(p["from_old_form"] == "OF1" for p in night2["broken_pieces"])


# ---------------------------------------------------------------------------
# 排班：小单主线
# ---------------------------------------------------------------------------

def test_small_order_two_machines_same_face_refused(tmp_path):
    doc, _ = _decide("job_small_busy.json", tmp_path)
    by_id = {o["order_id"]: o for o in doc["orders"]}
    assert by_id["O1"]["accepted"] and by_id["O1"]["positions"][0]["piece_id"] == "P1"
    assert not by_id["O2"]["accepted"]
    assert by_id["O2"]["reason_code"] == REASON_PIECE_BUSY


def test_small_order_switch_second_machine_to_other_face_accepted(tmp_path):
    doc, _ = _decide("job_small_switch.json", tmp_path)
    assert all(o["accepted"] for o in doc["orders"])
    pieces = {o["order_id"]: o["positions"][0]["piece_id"] for o in doc["orders"]}
    assert pieces == {"O1": "P1", "O2": "P2"}


# ---------------------------------------------------------------------------
# 排班：只改压样 / 只改台面占用 的两次翻转
# ---------------------------------------------------------------------------

def test_proof_only_change_flips_decision(tmp_path):
    clear, _ = _decide("job_proof_flip_clear.json", tmp_path)
    vague, _ = _decide("job_proof_flip_vague.json", tmp_path)
    assert clear["orders"][0]["accepted"] is True
    assert vague["orders"][0]["accepted"] is False
    assert vague["orders"][0]["reason_code"] == REASON_NO_PIECE
    # 两份任务只有压样文件不同：订单 Parquet 同一份，台面/盒配置完全一致。
    jc = load_job(os.path.join(PRESS, "job_proof_flip_clear.json"))
    jv = load_job(os.path.join(PRESS, "job_proof_flip_vague.json"))
    assert jc.order_file == jv.order_file
    assert jc.table_slots == jv.table_slots and jc.open_boxes == jv.open_boxes
    assert jc.proof_files != jv.proof_files


def test_table_slot_only_change_flips_decision(tmp_path):
    tight, _ = _decide("job_table_flip_tight.json", tmp_path)
    full, _ = _decide("job_table_flip_full.json", tmp_path)
    t = {o["order_id"]: o for o in tight["orders"]}
    f = {o["order_id"]: o for o in full["orders"]}
    assert t["Ta"]["accepted"] and not t["Tb"]["accepted"]
    assert t["Tb"]["reason_code"] == REASON_NO_TABLE_SLOT
    assert f["Ta"]["accepted"] and f["Tb"]["accepted"]
    # 两张票都从旧版 OF1 拆字，未开任何盒。
    assert all(p["from_old_form"] == "OF1" for p in f["Tb"]["positions"])
    assert full["night_ledger"][1]["table_slots_used"] == 2
    # 两份任务只有台面时段数不同：压样、订单、盒权限逐字段一致。
    jt = load_job(os.path.join(PRESS, "job_table_flip_tight.json"))
    jf = load_job(os.path.join(PRESS, "job_table_flip_full.json"))
    assert jt.proof_files == jf.proof_files and jt.order_file == jf.order_file
    assert jt.open_boxes == jf.open_boxes and jt.old_forms == jf.old_forms
    assert jt.table_slots != jf.table_slots


# ---------------------------------------------------------------------------
# 排班：封版、机器、开盒约束
# ---------------------------------------------------------------------------

def test_sealed_form_locks_piece_both_nights(tmp_path):
    doc, _ = _decide("job_lock.json", tmp_path)
    order = doc["orders"][0]
    assert not order["accepted"] and order["reason_code"] == REASON_PIECE_BUSY
    ledger_busy = doc["night_ledger"][0]["pieces_busy"]
    assert "P1" in ledger_busy and "P1" in doc["night_ledger"][1]["pieces_busy"]


def test_machine_busy_reason(tmp_path):
    """同一晚同一机器塞两张票：第二张拒因是机器占用，而不是字粒。"""
    job = _job("job_small_switch.json")
    orders = load_orders(job)
    cloned = []
    for o in orders:
        cloned.append(o)
    # 手工构造一张与 O1 同机同晚、要乙的新票（乙 P2 空闲，排除字粒因素）。
    cloned.insert(1, Order(
        order_id="OZ", night=0, machine="M1",
        positions=(Position("s1", "乙"),), source_box="B1",
        allowed_routes=("recompose", "seal"), source_file="x", source_row=9,
    ))
    report = _report(job)
    result = Scheduler(job, report, cloned).solve()
    oz = next(d for d in result.decisions if d.order_id == "OZ")
    assert not oz.accepted and oz.reason_code == REASON_MACHINE_BUSY


def test_closed_box_blocks_recompose_but_seal_route_unaffected(tmp_path):
    """当班无权开 B2 时：recompose 被盒权限挡住，允许 seal 的票仍可提前封版。"""
    job = _job("job_proof_flip_clear.json")
    object.__setattr__(job, "open_boxes", {0: frozenset(), 1: frozenset()})
    object.__setattr__(job, "table_slots", {0: 0, 1: 0})  # 台面也封死，逼出路径差异
    orders = load_orders(job)
    report = _report(job)
    result = Scheduler(job, report, orders).solve()
    decision = result.decisions[0]
    assert decision.accepted and decision.route == "seal"

    # 同一张票若只允许 recompose，则只能拒接且拒因为盒未开/无台面。
    sealed_only = [Order(
        order_id=o.order_id, night=o.night, machine=o.machine, positions=o.positions,
        source_box=o.source_box, allowed_routes=("recompose",),
        source_file=o.source_file, source_row=o.source_row,
    ) for o in orders]
    result2 = Scheduler(job, report, sealed_only).solve()
    assert not result2.decisions[0].accepted
    assert result2.decisions[0].reason_code in (REASON_BOX_CLOSED, REASON_NO_TABLE_SLOT)


# ---------------------------------------------------------------------------
# 输入校验与 CLI
# ---------------------------------------------------------------------------

def test_nested_parquet_rejected(tmp_path):
    path = tmp_path / "nested.parquet"
    pq.write_table(
        pa.table({"a": pa.array([["x"]], type=pa.list_(pa.string()))}), path
    )
    job = _job("job_small_busy.json")
    object.__setattr__(job, "order_file", str(path))
    with pytest.raises(PressError):
        load_orders(job)


def test_order_validation_unknown_face_and_box(tmp_path):
    from pqscan.press.model import validate_orders_against_job

    job = _job("job_small_busy.json")
    bad_face = [Order("OX", 0, "M1", (Position("s1", "丙"),), None,
                      ("recompose",), "x", 0)]
    with pytest.raises(PressError):
        validate_orders_against_job(bad_face, job)
    bad_box = [Order("OX", 0, "M1", (Position("s1", "甲"),), "B9",
                     ("recompose",), "x", 0)]
    with pytest.raises(PressError):
        validate_orders_against_job(bad_box, job)


def test_cli_accept_rerunnable(tmp_path, capsys):
    out = str(tmp_path / "cli")
    rc1 = cli_main(["accept", "--job", os.path.join(PRESS, "job_small_switch.json"), "--out", out])
    rc2 = cli_main(["accept", "--job", os.path.join(PRESS, "job_small_switch.json"), "--out", out])
    assert rc1 == rc2 == 0
    assert os.path.exists(os.path.join(out, "decision.json"))
    assert os.path.exists(os.path.join(out, "identity.json"))
    assert os.path.exists(os.path.join(out, "proofs", "verification.json"))
    text = capsys.readouterr().out
    assert "接 2 / 拒 0" in text
