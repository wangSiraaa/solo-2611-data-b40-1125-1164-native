"""周末回放：在基线上各做一次单点突变，证明原节目单会被哪条规则拒绝。

四类突变（每类只改一处）：
1. mutate_reading：单改一次鼓面读数/签条归属，使某段与行程表区间矛盾或分支失稳；
2. mutate_tag    ：单改一次模糊签条的候选，把唯一可行分支删成空集；
3. mutate_spare  ：单改一次库房备轮（移除一套备轮），封存路线凑不齐独立轴对；
4. mutate_lift   ：单改一次举升台安排（撤掉换轮展台的举升台），共用路线无法换轮。

每个场景都重新完整跑一遍履历复原 + 额度汇总 + 两条路线排程，便于交叉对照。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from ..errors import WheelsError
from .history import ItinerarySegment, Observation, reconstruct
from .quota import build_quota
from .schedule import Program, plan_weekend


@dataclass
class Scenario:
    name: str
    observations: list[Observation]
    itinerary: list[ItinerarySegment]
    tag_candidates: dict[str, list[str]]
    limits: dict[str, int]
    programs: list[Program]
    initial_mount: dict[str, str]
    spare_axles: list[str]              # 库房可借出的备轮（含车上现挂之外的库存）
    mutation: str = "baseline"
    mutation_detail: str = ""


@dataclass
class ScenarioResult:
    scenario: str
    mutation: str
    mutation_detail: str
    history_error: str | None
    branches: list[dict[str, Any]] = field(default_factory=list)
    quota: list[dict[str, Any]] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "mutation": self.mutation,
            "mutation_detail": self.mutation_detail,
            "history_error": self.history_error,
            "branches": self.branches,
            "quota": self.quota,
            "plans": self.plans,
        }


def _evaluate(sc: Scenario) -> ScenarioResult:
    res = ScenarioResult(
        scenario=sc.name, mutation=sc.mutation, mutation_detail=sc.mutation_detail,
        history_error=None,
    )
    try:
        branches = reconstruct(sc.observations, sc.itinerary, sc.tag_candidates)
        res.branches = [b.to_dict() for b in branches]
        quota = build_quota(branches, sc.limits, sc.spare_axles)
        res.quota = [q.to_dict() for q in sorted(quota.values(), key=lambda q: q.axle)]
    except WheelsError as exc:
        # 履历本身被突变破坏（读数/行程表矛盾、可行分支清零）：两条路线都不可排。
        res.history_error = str(exc)
        return res

    for route in ("sealed", "shared"):
        plan = plan_weekend(sc.programs, quota, sc.initial_mount, route)
        res.plans.append(plan.to_dict())
    return res


def run_replay(scenarios: list[Scenario]) -> list[ScenarioResult]:
    return [_evaluate(sc) for sc in scenarios]


# ---------------------------------------------------------------------------
# 单点突变构造器：深拷贝基线，只动一个字段。
# ---------------------------------------------------------------------------

def mutate_reading(sc: Scenario, car: str, ts: int, *, drum: int | None = None,
                   tag: str | None = None) -> Scenario:
    out = copy.deepcopy(sc)
    out.mutation, out.mutation_detail = "mutate_reading", f"车 {car} t={ts}"
    for i, obs in enumerate(out.observations):
        if obs.car == car and obs.ts == ts:
            out.observations[i] = Observation(
                car=obs.car, ts=obs.ts,
                drum=drum if drum is not None else obs.drum,
                axle=obs.axle, tag=tag if tag is not None else obs.tag,
                seg_id=obs.seg_id,
            )
            return out
    raise WheelsError(f"突变目标观测不存在：车 {car} t={ts}")


def mutate_tag(sc: Scenario, tag: str, removed: str) -> Scenario:
    out = copy.deepcopy(sc)
    out.mutation, out.mutation_detail = "mutate_tag", f"签条 {tag} 删除候选 {removed}"
    out.tag_candidates[tag] = [a for a in out.tag_candidates[tag] if a != removed]
    return out


def mutate_spare(sc: Scenario, removed: str) -> Scenario:
    out = copy.deepcopy(sc)
    out.mutation, out.mutation_detail = "mutate_spare", f"库房移除备轮 {removed}"
    out.spare_axles = [a for a in out.spare_axles if a != removed]
    out.limits = {a: lim for a, lim in out.limits.items() if a != removed}
    return out


def mutate_lift(sc: Scenario, stage: str) -> Scenario:
    out = copy.deepcopy(sc)
    out.mutation, out.mutation_detail = "mutate_lift", f"展台 {stage} 撤掉举升台"
    out.programs = [
        Program(
            program_id=p.program_id, car=p.car,
            stage=p.stage, has_lift=False if p.stage == stage else p.has_lift,
            ticks=p.ticks, departs_before_close=p.departs_before_close,
        )
        for p in out.programs
    ]
    return out
