"""对板求解（联合同期声）与放映排片投影。

模型（与 :mod:`audiomix.film`、:mod:`audiomix.clapper` 对齐）：

- **每卷一个卷内线性映射**：卷内局部帧 ``f`` 落在卷内局部录音格
  ``offset + f * gear``。``gear``（每帧推进多少录音格）取自工程给定的
  走片速度（每胶片帧推进一格 = ``1``）。因此 ``gear`` 只能是卷界处改档，
  ``offset`` 是卷的待校起点（取自该卷 ``start_candidates``）。
- **板响依据**：对板只能从“实际区间”读出的板响（检测到的格与花纹）出发。
  每个视觉板标（``FrameMark``）必须对到**唯一可用的真实板响**：在同一花纹
  的检测里，存在一个格 ``k`` 满足 ``k = offset + f*gear``，其中 ``f`` 是板标
  帧。
- **漏一声 / 跳一格**：同一花纹相邻两个真实板响之间，若帧距为 ``Δ``，则夹着
  ``Δ-1`` 个“未见于录音的板响”，计入合并容差
  ``max_missing_in_recording + max_missing_in_film``（题面各一，共 2）。
  这一容差只放宽“同花纹相邻板响之间可缺几个”，**不放宽板标本身**：每条视觉
  板标仍必须对到同花纹的真实板响——题面试例正是靠这一点把起点从 {0,1} 收敛
  到唯一 1。卷内映射单调（``gear>0`` 保证帧随格递增），即“每段内单调的时间关系”。
- **相容解释**：一组（每卷 gear 与 offset）只要板响可解释、且所有具名台词
  都能落到整数帧，就是一条**仍相容的声画对应**。求解器发布全部相容解释。

投影（`:func:`project`）在这些剩余解释上给出每条台词的“可能帧集合”和
“全部解释下必落的帧集合”，并据已锁片头 / 院方静音帧 / 允许帧区间判定该
台词是否**在全部剩余解释下都落在允许帧内**（可交付）。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable

from .film import FilmProject, Reel
from .rationals import dump_r

# 检测结果：{pattern: [global_cell, ...]}
Detections = dict[str, list[int]]


@dataclass(frozen=True)
class ReelSolution:
    """一卷的一种相容解释（一条声画对应）。"""

    reel_id: str
    gear_index: int
    gear: Fraction
    offset: int
    unseen_bangs: int              # 合并容差里用掉的“未见板响”数
    real_bangs: tuple[tuple[int, int, str], ...]  # (局部帧, 局部格, 花纹)

    def local_frame_of_cell(self, local_cell: int) -> int | None:
        """局部格 -> 局部帧；不落在整数帧上则 None。"""
        num = local_cell - self.offset
        den = self.gear
        # num / den 是否整数
        scaled = num * den.denominator
        if scaled % den.numerator != 0:
            return None
        f = scaled // den.numerator
        return int(f)

    def cell_of_local_frame(self, local_frame: int) -> Fraction:
        return Fraction(self.offset) + local_frame * self.gear

    def to_json(self) -> dict[str, Any]:
        return {
            "reel_id": self.reel_id,
            "gear_index": self.gear_index,
            "gear": dump_r(self.gear),
            "offset": self.offset,
            "unseen_bangs": self.unseen_bangs,
            "real_bangs": [
                {"local_frame": m, "local_cell": k, "pattern": p}
                for (m, k, p) in self.real_bangs
            ],
        }


def _local_detections(project: FilmProject, reel: Reel,
                      detections: Detections) -> dict[str, list[int]]:
    """把全局格检测裁剪成本卷局部格。"""
    tb = project.tape_base(reel.reel_id)
    out: dict[str, list[int]] = {}
    for pat, cells in detections.items():
        loc = sorted(c - tb for c in cells)
        out[pat] = [c for c in loc if 0 <= c < reel.tape_cells]
    return out


def _solve_reel(project: FilmProject, reel: Reel,
                local_dets: dict[str, list[int]],
                lines: Iterable, rng) -> list[ReelSolution]:
    """一卷的全部相容 (gear, offset)。"""
    marks_by_pattern: dict[str, list] = {}
    for m in project.marks_of(reel.reel_id):
        marks_by_pattern.setdefault(m.pattern, []).append(m)

    line_cells = [ln.cell - project.tape_base(reel.reel_id) for ln in lines]

    budget = project.max_missing_in_recording + project.max_missing_in_film
    solutions: list[ReelSolution] = []

    # 板标出现的花纹 ∪ 检测到的花纹：板标花纹即使一个检测都没有，也必须判定
    # （否则“板标对到真实板响”会被静默跳过）。
    all_patterns = sorted(set(marks_by_pattern) | set(local_dets))

    for gi, gear in enumerate(project.gears):
        for offset in reel.start_candidates:
            ok = True
            unseen = 0
            bangs: list[tuple[int, int, str]] = []
            for pattern in all_patterns:
                cells = local_dets.get(pattern, [])
                matched: list[tuple[int, int]] = []
                for k in sorted(cells):
                    num = k - offset
                    scaled = num * gear.denominator
                    if scaled % gear.numerator != 0:
                        continue  # 不在帧边界上的检测（走带毛刺/边缘），略过
                    m = scaled // gear.numerator
                    if 0 <= m < reel.frames:
                        matched.append((int(m), k))
                marks = marks_by_pattern.get(pattern, [])
                # 每个板标都必须对到同花纹的真实板响（唯一板响格由 offset 决定）
                for mk in marks:
                    if not any(m == mk.frame for m, _ in matched):
                        ok = False
                        break
                if not ok:
                    break
                # 同花纹相邻真实板响之间的“未见”板响计入合并容差
                # （录音带拼接漏一个 + 胶片同花纹跳一格）
                ms = [m for m, _ in matched]
                for a, b in zip(ms, ms[1:]):
                    gap_unseen = b - a - 1
                    if gap_unseen > budget:
                        ok = False
                        break
                    unseen += gap_unseen
                if not ok:
                    break
                bangs.extend((m, k, pattern) for m, k in matched)
            if not ok or unseen > budget:
                continue
            # 本卷所有台词必须能落到整数帧且在卷内
            for lc in line_cells:
                num = lc - offset
                scaled = num * gear.denominator
                if scaled % gear.numerator != 0:
                    ok = False
                    break
                f = scaled // gear.numerator
                if not (0 <= f < reel.frames):
                    ok = False
                    break
            if not ok:
                continue
            solutions.append(ReelSolution(
                reel_id=reel.reel_id, gear_index=gi, gear=gear, offset=offset,
                unseen_bangs=unseen, real_bangs=tuple(sorted(bangs)),
            ))
    return solutions


def solve(project: FilmProject, detections: Detections) -> dict[str, list[ReelSolution]]:
    """每卷求全部相容解释；返回 ``reel_id -> [ReelSolution]``。

    若某卷无任何相容解释，则整部片无相容声画对应（该卷对应空表）。
    """
    per_reel: dict[str, list[ReelSolution]] = {}
    for reel in project.reels:
        lines = [ln for ln in project.lines
                 if (project.reel_of_cell(ln.cell) or (None,))[0] is reel]
        loc = _local_detections(project, reel, detections)
        per_reel[reel.reel_id] = _solve_reel(project, reel, loc, lines,
                                             project.gears)
    return per_reel


def is_globally_consistent(per_reel: dict[str, list[ReelSolution]]) -> bool:
    return all(len(v) > 0 for v in per_reel.values())


# ============================== 放映投影 ============================== #

@dataclass(frozen=True)
class LineProjection:
    line_id: str
    cell: int
    allowed_lo: int
    allowed_hi: int
    possible_frames: tuple[int, ...]     # 剩余解释下可能落到的帧（升序）
    guaranteed_frames: tuple[int, ...]   # 全部解释下都落的帧（交集）
    admissible_frames: tuple[int, ...]   # 允许∩非锁头∩非静音，且在可能集合内
    deliverable: bool                     # 全部剩余解释都落在允许帧内

    def to_json(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id, "cell": self.cell,
            "allowed_lo": self.allowed_lo, "allowed_hi": self.allowed_hi,
            "possible_frames": list(self.possible_frames),
            "guaranteed_frames": list(self.guaranteed_frames),
            "admissible_frames": list(self.admissible_frames),
            "deliverable": self.deliverable,
        }


@dataclass(frozen=True)
class Projection:
    """一次排片投影的结果（纯函数产物，可整份存盘/复查）。"""

    film_id: str
    consistent: bool
    explanations: dict[str, list[ReelSolution]]
    n_explanations: int
    lines: dict[str, LineProjection]

    def deliverable_lines(self) -> list[str]:
        return sorted(k for k, v in self.lines.items() if v.deliverable)

    def undeliverable_lines(self) -> list[str]:
        return sorted(k for k, v in self.lines.items() if not v.deliverable)

    def to_json(self) -> dict[str, Any]:
        return {
            "format": "audiomix.projection/1",
            "film_id": self.film_id,
            "consistent": self.consistent,
            "n_explanations": self.n_explanations,
            "explanations": {
                rid: [s.to_json() for s in sols]
                for rid, sols in self.explanations.items()
            },
            "lines": {k: v.to_json() for k, v in self.lines.items()},
            "deliverable_lines": self.deliverable_lines(),
            "undeliverable_lines": self.undeliverable_lines(),
        }


def _admissible_set(project: FilmProject, ln) -> set[int]:
    """允许帧区间内、非已锁片头、非院方静音帧的帧集合。"""
    return {
        f for f in range(ln.allowed_lo, ln.allowed_hi + 1)
        if f not in project.locked_head_frames
        and f not in project.silent_locked_frames
    }


def project(project: FilmProject, detections: Detections) -> Projection:
    """在全部相容解释上投影每条具名台词的可交付性。"""
    per_reel = solve(project, detections)
    consistent = is_globally_consistent(per_reel)
    n_expl = 1
    for sols in per_reel.values():
        n_expl *= len(sols)

    line_projs: dict[str, LineProjection] = {}
    for ln in project.lines:
        rc = project.reel_of_cell(ln.cell)
        if rc is None or not consistent:
            line_projs[ln.line_id] = LineProjection(
                ln.line_id, ln.cell, ln.allowed_lo, ln.allowed_hi,
                (), (), (), False)
            continue
        reel, local_cell = rc
        sols = per_reel.get(reel.reel_id, [])
        possible: set[int] = set()
        frame_sets: list[set[int]] = []
        base = project.frame_base(reel.reel_id)
        placed_frames_this_solution: list[int] = []
        for s in sols:
            f = s.local_frame_of_cell(local_cell)
            if f is None or not (0 <= f < reel.frames):
                # 该解释无法安置这条真实台词 -> 这条解释不可作为剩余解释
                # （我们在 solve 已排除，这里保守跳过）
                continue
            gf = base + f
            possible.add(gf)
            placed_frames_this_solution.append(gf)
            frame_sets.append({gf})
        # 全部解释下必落的帧 = 每个解释贡献单点集的交
        if frame_sets:
            guaranteed = set.intersection(*frame_sets)
        else:
            guaranteed = set()
        admissible = _admissible_set(project, ln)
        # 可交付：可能集合非空且完全落在可允许集合内
        deliverable = bool(possible) and possible <= admissible
        line_projs[ln.line_id] = LineProjection(
            ln.line_id, ln.cell, ln.allowed_lo, ln.allowed_hi,
            tuple(sorted(possible)), tuple(sorted(guaranteed)),
            tuple(sorted(possible & admissible)), deliverable,
        )
    return Projection(project.film_id, consistent, per_reel, n_expl, line_projs)
