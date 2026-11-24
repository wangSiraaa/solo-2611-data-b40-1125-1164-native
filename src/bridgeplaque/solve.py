"""安装年求解：把梁片年轮证据与工单安装先后放在同一组约束下枚举。

每根梁给出一条砍伐年约束（由 bridgeplaque.plaque 从对齐结果与许可算出）：
- exact_years：带皮末轮可引用 -> 砍伐年精确等于末轮候选年之一；
- lower_bound：缺最外圈（或外圈被盖且无权引用）-> 只给砍伐年下界。

安装年约束：
- 现砍现装：安装事件年 == 砍伐年；
- 库存旧材：安装事件年 >= 砍伐年（旧材只能压下界，不能把安装提前）；
- 榫序：卯方梁的安装不晚于榫方梁（榫切穿卯，卯先就位）；
- 事件按工单顺序严格递增（初建早于重装）。

枚举空间极小（事件数 <= 工单长度，年份限于参照带范围），直接穷举所有
(梁->事件归属, 事件->年份) 组合，输出全部仍成立的解；馆员看到的“候选年”
就是这些解在目标梁上的投影，一个解都不剩即证据自相矛盾。
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from .workorder import FRESH, WorkOrder


@dataclass(frozen=True)
class BeamDating:
    """一根梁进入求解的砍伐年证据。"""

    beam_id: str
    material: str                       # FRESH | STOCK
    events: tuple[str, ...]             # 封榫收敛后的候选安装事件
    exact_years: frozenset[int] | None  # 带皮末轮可引用：砍伐年 ∈ 该集合
    lower_bound: int | None             # 否则：砍伐年 >= 下界


@dataclass(frozen=True)
class Solution:
    assignment: dict[str, str]          # 梁号 -> 安装事件
    years: dict[str, int]               # 事件 -> 公历年


def _beam_ok(dating: BeamDating, event_year: int) -> bool:
    if dating.exact_years is not None:
        if dating.material == FRESH:
            return event_year in dating.exact_years
        return event_year >= min(dating.exact_years)      # 旧材：只压下界
    assert dating.lower_bound is not None
    return event_year >= dating.lower_bound


def solve_event_years(workorder: WorkOrder, datings: list[BeamDating],
                      year_min: int, year_max: int) -> list[Solution]:
    """枚举全部仍成立的 (归属, 年份) 解。"""
    events = workorder.events
    by_beam = {d.beam_id: d for d in datings}
    beam_ids = list(by_beam)

    solutions: list[Solution] = []
    # 梁 -> 事件的全部归属组合（多数梁只有一个候选事件，组合数很小）。
    event_choices = [by_beam[b].events for b in beam_ids]
    for assignment_tuple in product(*event_choices):
        assignment = dict(zip(beam_ids, assignment_tuple))
        # 事件年份：按工单顺序严格递增。
        for years_tuple in _increasing_tuples(len(events), year_min, year_max):
            years = dict(zip(events, years_tuple))
            if not all(_beam_ok(by_beam[b], years[assignment[b]]) for b in beam_ids):
                continue
            # 榫序：卯方先就位（同年不违反）。
            if any(
                years[assignment[j.mortise_beam]] > years[assignment[j.tenon_beam]]
                for j in workorder.joints
                if j.mortise_beam in by_beam and j.tenon_beam in by_beam
            ):
                continue
            solutions.append(Solution(assignment=assignment, years=years))
    return solutions


def _increasing_tuples(n: int, lo: int, hi: int):
    if n == 0:
        yield ()
        return
    if n == 1:
        for y in range(lo, hi + 1):
            yield (y,)
        return
    # 事件少、年份范围小，直接递归。
    def rec(prefix, start):
        if len(prefix) == n:
            yield tuple(prefix)
            return
        for y in range(start, hi + 1):
            yield from rec(prefix + [y], y + 1)
    yield from rec([], lo)


def possibilities_for(solutions: list[Solution], beam_id: str) -> list[tuple[str, int]]:
    """目标梁在所有仍成立的解中的 (安装事件, 年份) 投影，去重排序。"""
    pairs = {(s.assignment[beam_id], s.years[s.assignment[beam_id]]) for s in solutions}
    return sorted(pairs, key=lambda p: (p[1], p[0]))
