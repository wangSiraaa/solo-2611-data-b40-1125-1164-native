"""两套对板制度（声音边条 vs 现场追板同步机）与交付判定。

题面要求“选一套制度给两卷小片开单”，并“用一次现场才出现的板响说明锁定
边界怎样影响补救”。本模块把两套制度显式实现为可对比的策略：

1. **印死声画边条（``EdgeStripRegime``）** —— 传统“声响边条”/声音边条：
   在一卷上把声画偏移一次性印死。一旦印定，这卷的 (gear, offset) 被**锁死**，
   不能逐句挪动；换卷时才可独立校下一条。因此：
     - 锁定后，每条台词在该卷解释下只有一个落点；若该落点被院方静音帧/
       已锁片头排除，则无法用“换卷内另一种解释”补救。
     - 补救只能靠**补无对白帧**（在卷界/空帧处插入），或更换卷的起始候选
       （即“换卷时重新校下一条”）。

2. **现场追板同步机（``LiveSyncRegime``）** —— 现场随片追板：
    碰到未验证的卷界时，必须真的拿一帧空白做校正，随后才能重新锁声。
    校正帧会从“可补无对白帧”里消耗掉一帧，并把它变成该卷新的验证起点；
    与边条不同的是，只要卷内还有其它相容解释且尚未开始放映，同步机可以
    **在卷界改档/改起点**，因此对一次“现场才出现的新板响”更灵活，但代价
    是必须实打实消耗一个校正帧。

两套制度共用同一个求解/投影内核（:mod:`audiomix.sync`），区别只在于
“锁定边界后，允许在什么粒度上重解”。

交付判定（`:func:`deliverable_under`）：
    给定制度与一次“现场才出现的板响”（追加的检测），返回
    每条台词是否在**全部剩余解释**下都落在允许帧内（可交付），以及为补救
    需要消耗哪些可补无对白帧、哪些卷界需要重新校正。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .film import FilmProject
from .sync import (
    Detections, Projection, ReelSolution, is_globally_consistent, project,
)


@dataclass(frozen=True)
class LockBoundary:
    """锁定边界：某卷在给定档位下被印死/校验的 (gear_index, offset)。"""

    reel_id: str
    gear_index: int
    offset: int

    def to_json(self) -> dict[str, Any]:
        return {"reel_id": self.reel_id, "gear_index": self.gear_index,
                "offset": self.offset}


class Regime:
    """制度基类：给定工程、当前检测、锁定边界与一次新板响，给出交付判定。"""

    name = "base"

    def __init__(self, locked: dict[str, LockBoundary] | None = None):
        self.locked = dict(locked or {})

    # 子类实现：约束哪些卷的哪些自由度被冻结
    def constrain(self, per_reel: dict[str, list[ReelSolution]]
                  ) -> dict[str, list[ReelSolution]]:
        raise NotImplementedError

    def projected(self, proj: FilmProject, dets: Detections) -> Projection:
        return project(proj, dets)

    def assess(self, proj: FilmProject, dets: Detections) -> dict[str, Any]:
        raise NotImplementedError


class EdgeStripRegime(Regime):
    """印死声画边条：锁定卷的 (gear,offset) 绝对固定，卷内不可再挪。"""

    name = "edge_strip"

    def constrain(self, per_reel):
        out = {}
        for rid, sols in per_reel.items():
            lb = self.locked.get(rid)
            if lb is None:
                out[rid] = sols
            else:
                out[rid] = [s for s in sols
                            if s.gear_index == lb.gear_index and s.offset == lb.offset]
        return out

    def assess(self, proj: FilmProject, dets: Detections) -> dict[str, Any]:
        base = self.projected(proj, dets)
        cons = self.constrain(base.explanations)
        consistent = is_globally_consistent(cons)
        # 重算投影的可交付性：以约束后的解释集合为准
        return _assess_with_explanations(proj, cons, consistent, self.name)


class LiveSyncRegime(Regime):
    """现场追板同步机：未验证卷界须真拿空白帧校正后才能重锁。

    交付判定与边条相同，但报告“需要校正的卷界”（锁定边界仍存在、且卷尚未
    验证的卷）。校正本身消耗一帧可补无对白帧 —— 见 :func:`plan_remedy`。
    """

    name = "live_sync"

    def constrain(self, per_reel):
        # 同步机：锁定边界只约束“已开始放映/已锁定”的卷；未锁卷保持全部解释
        out = {}
        for rid, sols in per_reel.items():
            lb = self.locked.get(rid)
            if lb is None:
                out[rid] = sols
            else:
                kept = [s for s in sols
                        if s.gear_index == lb.gear_index and s.offset == lb.offset]
                out[rid] = kept if kept else sols
        return out

    def assess(self, proj: FilmProject, dets: Detections) -> dict[str, Any]:
        base = self.projected(proj, dets)
        cons = self.constrain(base.explanations)
        consistent = is_globally_consistent(cons)
        res = _assess_with_explanations(proj, cons, consistent, self.name)
        # 需要实际校正的卷界：卷仍有不止一种解释（未验证），或锁定解释已被
        # 新板响排除（冲突需重新锁声）。
        need_calib = []
        for rid, sols in base.explanations.items():
            if len(sols) > 1:
                need_calib.append(rid)
                continue
            lb = self.locked.get(rid)
            if lb is not None and not any(
                    s.gear_index == lb.gear_index and s.offset == lb.offset
                    for s in sols):
                need_calib.append(rid)
        res["needs_calibration_reels"] = need_calib
        return res


def _assess_with_explanations(proj: FilmProject,
                              per_reel: dict[str, list[ReelSolution]],
                              consistent: bool, regime_name: str) -> dict[str, Any]:
    """按给定解释集合逐台词判定“全部剩余解释下是否落在允许帧内”。"""
    lines_out: dict[str, Any] = {}
    for ln in proj.lines:
        rc = proj.reel_of_cell(ln.cell)
        deliverable = False
        possible: set[int] = set()
        if consistent and rc is not None:
            reel, local_cell = rc
            base = proj.frame_base(reel.reel_id)
            admissible = {
                f for f in range(ln.allowed_lo, ln.allowed_hi + 1)
                if f not in proj.locked_head_frames
                and f not in proj.silent_locked_frames
            }
            for s in per_reel.get(reel.reel_id, []):
                f = s.local_frame_of_cell(local_cell)
                if f is None or not (0 <= f < reel.frames):
                    possible = set()
                    break
                possible.add(base + f)
            deliverable = bool(possible) and possible <= admissible
        lines_out[ln.line_id] = {
            "possible_frames": sorted(possible),
            "deliverable": deliverable,
        }
    return {
        "regime": regime_name,
        "consistent": consistent,
        "n_explanations": _product(per_reel),
        "lines": lines_out,
        "deliverable_lines": sorted(k for k, v in lines_out.items() if v["deliverable"]),
        "undeliverable_lines": sorted(k for k, v in lines_out.items()
                                      if not v["deliverable"]),
    }


def _product(per_reel: dict[str, list[ReelSolution]]) -> int:
    n = 1
    for v in per_reel.values():
        n *= max(1, len(v))
    return n


def detect_with_new_bang(dets: Detections, pattern: str, cell: int) -> Detections:
    """追加一次“现场才出现的板响”后的检测。"""
    out = {k: list(v) for k, v in dets.items()}
    if cell not in out.setdefault(pattern, []):
        out[pattern].append(cell)
        out[pattern].sort()
    return out


def _blank_in_reel(proj: FilmProject, reel_id: str, exclude: set[int]) -> int | None:
    """该卷内可用于校正的空白帧（可补、非锁头/静音/已占用）。"""
    base = proj.frame_base(reel_id)
    reel = proj.reel(reel_id)
    for f in sorted(proj.fillable_frames):
        if base <= f < base + reel.frames and f not in exclude:
            return f
    return None


def plan_remedy(proj: FilmProject, dets: Detections, regime: Regime,
                projection_json: dict[str, Any],
                played: set[int] | None = None) -> dict[str, Any]:
    """给出补救方案：需补哪些无对白帧、需在哪些卷界重新校正。

    - **现场追板同步机**：每个“需重新校正”的卷界必须真的消耗一帧可补空白
      帧做校正（``filler_frames``）；校正后可在卷界重新锁声。
    - **印死声画边条**：已印死的卷不能在本卷内重解；补救需等到下一卷界
      重新开印（``reprint_reels`` 表示须重印、且不可用本卷内补帧替代）。
    """
    und = projection_json.get("undeliverable_lines", [])
    fillable = sorted(proj.fillable_frames)
    need_calib = projection_json.get("needs_calibration_reels", [])
    exclude = set(played or ()) | set(proj.locked_head_frames) | \
        set(proj.silent_locked_frames)

    result: dict[str, Any] = {
        "regime": regime.name,
        "undeliverable_lines": und,
        "fillable_available": fillable,
    }
    if regime.name == "live_sync":
        fills: list[int] = []
        for rid in need_calib:
            f = _blank_in_reel(proj, rid, exclude | set(fills))
            if f is not None:
                fills.append(f)
        result["filler_frames"] = sorted(fills)
        result["recalibrate_reels"] = list(need_calib)
        result["reprint_reels"] = []
        result["calibration_cost"] = len(fills)
    else:  # edge_strip
        # 已印死卷内不可重解：补救须重印（等到该卷的卷界/下一卷）
        result["filler_frames"] = []
        result["recalibrate_reels"] = []
        result["reprint_reels"] = sorted(regime.locked)
        result["calibration_cost"] = 0
    return result
