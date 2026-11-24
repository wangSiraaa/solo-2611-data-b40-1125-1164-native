"""展牌签发与台账。

馆员可反复调用的署年流程（每次调用 = 一份署年申请 = 一条台账记录）：

1. 按申请中的桥段构造 pqscan 查询，调用 pqscan.runner.run 查地方案卷
   （计划/扫描/独立核对全走一遍；核对不通过则拒绝签发）；
2. 读 results 中真正命中的 Parquet，把梁号、读数和来源行串成梁片证据：
   同树断片接回（漏读一环以 "?" 占位）、与逐年宽窄参照带交叉对齐、
   按工单与许可决定被榫盖住的外圈是否可引用；
3. 将候选年与工单安装先后相核（solve.solve_event_years）：
   目标梁的安装年份唯一 -> 签出标年；不唯一或矛盾 -> 暂缓并写明缺什么；
4. 台账追加一条记录（展牌单号 PB-xxxx），馆员凭单号查回具体梁片、
   采用的安装证据与当时可公开的结论。台账只增不改：日后再办（哪怕证据
   已变）不影响已签记录的“当时结论”。

工艺（application.craft）：
- noninvasive 不动实物：只用外露年轮与接合先后；被榫盖住的外圈须有查看权
  才可引用；
- microcore 微芯取样：在许可部位取一枚跨外圈的微芯，直接获得该梁被盖外圈
  的可引用读数。代价：该处榫痕的后续观察机会永久损失；全桥只许可开一处，
  台账登记后后续申请不得再开。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from pqscan.runner import run as pqscan_run

from . import PlaqueError
from .band import ReferenceBand, load_band
from .beams import (BeamRecord, build_beam_records, candidate_last_ring_years,
                    load_hit_fragments)
from .solve import BeamDating, possibilities_for, solve_event_years
from .workorder import FRESH, STOCK, WorkOrder, load_workorder

CRAFT_NONINVASIVE = "noninvasive"     # 不动实物
CRAFT_MICROCORE = "microcore"         # 微芯取样
CRAFT_LABELS = {CRAFT_NONINVASIVE: "不动实物", CRAFT_MICROCORE: "微芯取样"}

QUERY_ID = "plaque_segment"
PROJECTION = ["segment", "beam_id", "fragment", "start_pos", "widths", "bark_edge"]


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------

class Ledger:
    """展牌台账：JSON 文件、只增不改，凭展牌单号查回。"""

    def __init__(self, path: str):
        self.path = path
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self._doc = json.load(fh)
        else:
            self._doc = {"records": []}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self._doc, fh, ensure_ascii=False, indent=2)

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        record = dict(record)
        record["plaque_id"] = f"PB-{len(self._doc['records']) + 1:04d}"
        self._doc["records"].append(record)
        self._save()
        return record

    def lookup(self, plaque_id: str) -> dict[str, Any]:
        for rec in self._doc["records"]:
            if rec["plaque_id"] == plaque_id:
                return rec
        raise PlaqueError(f"台账中没有展牌单号 {plaque_id}")

    def records(self) -> list[dict[str, Any]]:
        return list(self._doc["records"])

    def core_consumed_by(self) -> str | None:
        """微芯已用掉的展牌单号（全桥只许可开一处）；未用返回 None。"""
        for rec in self._doc["records"]:
            if rec.get("core"):
                return rec["plaque_id"]
        return None


# ---------------------------------------------------------------------------
# 署年申请
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Application:
    segment: str          # 桥段
    beam_id: str          # 梁号（本次署年的目标梁）
    craft: str            # CRAFT_NONINVASIVE | CRAFT_MICROCORE


@dataclass
class BeamEvidence:
    """一根梁进入求解前的完整证据链（可序列化进台账）。"""

    record: BeamRecord
    covered_blocked: set[int]         # 被榫盖住且无权引用的环位
    citable_pattern: str
    candidates: list[int]             # 可引用末环的候选年
    bark_citable: bool
    dating: BeamDating
    note: str                         # 人读证据句


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_archive_scan(fixtures_dir: str, out_dir: str, segment: str) -> str:
    """按桥段调 pqscan 查案卷，返回真正命中的结果 Parquet 路径。"""
    files_json = os.path.join(fixtures_dir, "files.json")
    queries = [{"id": QUERY_ID, "where": {"eq": ["segment", segment]}, "columns": PROJECTION}]
    queries_dir = os.path.join(out_dir, "queries")
    os.makedirs(queries_dir, exist_ok=True)
    queries_json = os.path.join(queries_dir, f"{QUERY_ID}.json")
    with open(queries_json, "w", encoding="utf-8") as fh:
        json.dump(queries, fh, ensure_ascii=False, indent=2)

    doc = pqscan_run(files_json, queries_json, os.path.join(out_dir, "pqscan"))
    for q in doc["verification"]["queries"]:
        if not q["passed"]:
            raise PlaqueError(f"pqscan 独立核对未通过（{q['query_id']}），拒绝签发: {q['detail']}")
    return os.path.join(out_dir, "pqscan", "results", f"{QUERY_ID}.parquet")


def _beam_note(record: BeamRecord, dating: BeamDating, material: str) -> str:
    frags = "/".join(f.fragment for f in record.fragments)
    if len(record.fragments) > 1:
        joined = f"{frags}同树接回（断口漏读一环以?占位，计一份佐证）"
    else:
        joined = f"{frags}单片取样"
    if dating.exact_years is not None:
        reading = f"带皮末轮可引用，对齐候选{sorted(dating.exact_years)}"
    else:
        reading = f"缺最外圈可引用，只能给下界（砍伐不早于{dating.lower_bound}年）"
    material_note = "现砍现装，安装年即砍伐年" if material == FRESH else \
        "库存旧材，只压安装下界，不能据末轮提前"
    return f"{record.beam_id}梁{material_note}；{joined}，{reading}"


def _build_evidence(records: dict[str, BeamRecord], workorder: WorkOrder,
                    band: ReferenceBand, app: Application,
                    cored_joint: str | None) -> dict[str, BeamEvidence]:
    """对每根命中梁：算可引用读数、候选年、砍伐年约束。"""
    evidence: dict[str, BeamEvidence] = {}
    for beam_id, record in records.items():
        if beam_id not in workorder.beams:
            raise PlaqueError(f"案卷命中梁 {beam_id}，但工单没有其安装记录")
        order = workorder.beams[beam_id]

        covered_all: set[int] = set()
        citable: set[int] = set()
        for joint_id, positions in workorder.covered_positions(beam_id).items():
            covered_all |= positions
            if app.craft == CRAFT_MICROCORE and joint_id == cored_joint and beam_id == app.beam_id:
                citable |= positions            # 微芯直接读到该梁被盖外圈
            elif workorder.view_permits.get(joint_id):
                citable |= positions            # 有查看权，可引用案卷中的被盖读数
        blocked = covered_all - citable

        pattern = record.citable_pattern(blocked)
        candidates = candidate_last_ring_years(record, band, blocked)
        bark_citable = record.bark_edge and not blocked
        events = tuple(workorder.allowed_events(beam_id))

        if bark_citable:
            dating = BeamDating(beam_id, order.material, events,
                                frozenset(candidates), None)
        else:
            lower = min(candidates) + 1 if candidates else band.end_year + 1
            dating = BeamDating(beam_id, order.material, events, None, lower)
        evidence[beam_id] = BeamEvidence(
            record=record, covered_blocked=blocked, citable_pattern=pattern,
            candidates=candidates, bark_citable=bark_citable, dating=dating,
            note=_beam_note(record, dating, order.material),
        )
    return evidence


def _resolve_core(workorder: WorkOrder, app: Application, ledger: Ledger,
                  problems: list[str]) -> str | None:
    """微芯工艺：核定许可部位。返回取芯节点 id；不可取芯时登记原因并返回 None。"""
    if app.craft != CRAFT_MICROCORE:
        return None
    permitted = [j for j, ok in workorder.sample_permits.items() if ok]
    if not permitted:
        problems.append("无微芯取样许可，不能开芯")
        return None
    if len(permitted) > 1:
        raise PlaqueError(f"取样许可部位超过一处 {permitted}，与“只可开一处”冲突")
    used_by = ledger.core_consumed_by()
    if used_by is not None:
        problems.append(f"全桥只许可开一处微芯，已用于 {used_by}，不得再开")
        return None
    joint_id = permitted[0]
    joint = next((j for j in workorder.joints if j.id == joint_id), None)
    if joint is None or app.beam_id not in joint.covers:
        problems.append(f"许可部位 {joint_id} 不在{app.beam_id}梁上，取芯无法补证该梁外圈")
        return None
    return joint_id


def _exclusion_notes(workorder: WorkOrder, app: Application,
                     evidence: dict[str, BeamEvidence], band: ReferenceBand,
                     final: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """对照“撤掉封榫”的反事实解，说明候选年里的另一次安装是怎样被排除的。"""
    target = evidence[app.beam_id]
    relaxed_dating = BeamDating(target.dating.beam_id, target.dating.material,
                                tuple(workorder.beams[app.beam_id].events),
                                target.dating.exact_years, target.dating.lower_bound)
    relaxed_datings = [relaxed_dating if b == app.beam_id else ev.dating
                       for b, ev in evidence.items()]
    relaxed = possibilities_for(
        solve_event_years(workorder, relaxed_datings, band.start_year, band.end_year),
        app.beam_id,
    )
    notes: list[dict[str, Any]] = []
    sealed = [j for j in workorder.joints if j.tenon_beam == app.beam_id]
    for event, year in relaxed:
        if (event, year) in final:
            continue
        if sealed:
            reason = "；".join(
                f"封榫记录{j.id}：{j.sealed_after}后封存，"
                f"{app.beam_id}梁未在其后拆装" for j in sealed
            )
        else:
            reason = "与工单事件次序及他梁证据冲突"
        notes.append({"excluded": {"event": event, "year": year}, "reason": reason})
    return notes


def _public_conclusion(app: Application, status: str, year: int | None,
                       event: str | None, evidence: dict[str, BeamEvidence],
                       exclusions: list[dict[str, Any]], problems: list[str]) -> str:
    lines: list[str] = []
    if status == "issued":
        lines.append(f"{app.segment}{app.beam_id}梁：{event}{year}年。")
    else:
        lines.append(f"{app.segment}{app.beam_id}梁：暂缓署年。")
    lines.append("证据：" + "；".join(ev.note for ev in evidence.values()) + "。")
    for ex in exclusions:
        lines.append(f"候选{ex['excluded']['year']}年（{ex['excluded']['event']}）已排除：{ex['reason']}。")
    if status == "issued":
        # 旧材梁与目标梁同次装入而末轮更早：只能压下界，不能据它把安装提前。
        for beam_id, ev in evidence.items():
            if beam_id == app.beam_id or ev.dating.material != STOCK:
                continue
            if ev.dating.exact_years is None or event not in ev.dating.events:
                continue
            early = min(ev.dating.exact_years)
            if early < year:
                lines.append(
                    f"{beam_id}梁末轮{early}年系库存旧材，与{app.beam_id}梁同次装入"
                    f"（{event}），不能据{beam_id}梁把桥提前{year - early}年。"
                )
    if problems:
        lines.append("待补：" + "；".join(problems) + "。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 签发入口（馆员每次调用办一份申请）
# ---------------------------------------------------------------------------

def issue_plaque(fixtures_dir: str, out_dir: str, app: Application,
                 ledger_path: str | None = None,
                 clock: Callable[[], str] = _default_clock) -> dict[str, Any]:
    """办一份署年申请：查案卷 -> 拼梁对齐 -> 与工单相核 -> 签标年或暂缓并登记台账。"""
    if app.craft not in CRAFT_LABELS:
        raise PlaqueError(f"未知工艺 {app.craft!r}（可选 {sorted(CRAFT_LABELS)}）")
    ledger_path = ledger_path or os.path.join(out_dir, "plaque_ledger.json")
    ledger = Ledger(ledger_path)
    band = load_band(os.path.join(fixtures_dir, "band.json"))
    workorder = load_workorder(os.path.join(fixtures_dir, "workorder.json"))

    results_parquet = _run_archive_scan(fixtures_dir, out_dir, app.segment)
    records = build_beam_records(load_hit_fragments(results_parquet))
    if app.beam_id not in records:
        raise PlaqueError(
            f"案卷在桥段 {app.segment} 未命中梁 {app.beam_id} 的片段"
            f"（命中梁号：{sorted(records)}）"
        )

    problems: list[str] = []
    cored_joint = _resolve_core(workorder, app, ledger, problems)
    evidence = _build_evidence(records, workorder, band, app, cored_joint)

    datings = [ev.dating for ev in evidence.values()]
    solutions = solve_event_years(workorder, datings, band.start_year, band.end_year)
    final = possibilities_for(solutions, app.beam_id)
    exclusions = _exclusion_notes(workorder, app, evidence, band, final)

    if not solutions:
        status, event, year = "deferred", None, None
        problems.append("年轮证据与工单约束自相矛盾，无仍成立的安装年")
    elif len(final) == 1:
        status, (event, year) = "issued", final[0]
    else:
        status, event, year = "deferred", None, None
        problems.append(
            "安装年不唯一：" + "、".join(f"{e}{y}年" for e, y in final)
        )
        blocked_joints = [j.id for j in workorder.joints
                          if app.beam_id in j.covers and not workorder.view_permits.get(j.id)]
        if blocked_joints and app.craft == CRAFT_NONINVASIVE:
            problems.append(
                f"被榫盖住的外圈（{'、'.join(blocked_joints)}）无查看权，"
                "可申请查看权或改微芯取样工艺"
            )

    record: dict[str, Any] = {
        "issued_at": clock(),
        "application": {"segment": app.segment, "beam_id": app.beam_id,
                        "craft": app.craft, "craft_label": CRAFT_LABELS[app.craft]},
        "status": status,
        "dating": {"event": event, "year": year} if status == "issued" else None,
        "possibilities": [{"event": e, "year": y} for e, y in final],
        "beam_evidence": [
            {
                "beam_id": ev.record.beam_id,
                "fragments": [f.fragment for f in ev.record.fragments],
                "same_tree_single_evidence": True,
                "joined_pattern": ev.record.pattern,
                "citable_pattern": ev.citable_pattern,
                "covered_blocked_positions": sorted(ev.covered_blocked),
                "last_ring_candidates": ev.candidates,
                "bark_edge": ev.record.bark_edge,
                "bark_citable": ev.bark_citable,
                "felling_constraint": (
                    {"kind": "exact", "years": sorted(ev.dating.exact_years)}
                    if ev.dating.exact_years is not None
                    else {"kind": "lower_bound", "year": ev.dating.lower_bound}
                ),
                "candidate_events": list(ev.dating.events),
                "source_rows": [s.to_dict() for s in ev.record.source_rows],
            }
            for ev in evidence.values()
        ],
        "installation_evidence": [ev.note for ev in evidence.values()],
        "excluded_candidates": exclusions,
        "public_conclusion": _public_conclusion(
            app, status, year, event, evidence, exclusions, problems),
        "problems": problems,
        "results_parquet": results_parquet,
        "core": (
            {"joint": cored_joint,
             "cost": "该处榫痕后续观察机会已永久损失；全桥只许可开一处，本次已用"}
            if cored_joint else None
        ),
    }
    return ledger.append(record)


def lookup_plaque(ledger_path: str, plaque_id: str) -> dict[str, Any]:
    """凭展牌单号查回：具体梁片、采用的安装证据、当时可公开的结论。"""
    return Ledger(ledger_path).lookup(plaque_id)
