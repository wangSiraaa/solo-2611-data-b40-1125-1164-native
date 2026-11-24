"""周末展演排程：封存（route A）与共用复读（route B）两条路线。

节目按给定顺序逐个分配轴对，额度校验统一走 quota 的"恰好到限可收车"语义：
历史最大已耗 + 本场已用 + 本段格数 <= limit 才允许，等号合法。

route=sealed（封存路线）：开馆前为每个节目封存一整套轴对，整个周末不再借出，
因此不同节目必须用不同轴对；封存发生在开馆前，不依赖展台举升台。

route=shared（共用路线）：轴对可在节目间复读鼓面后再换，但
- 只有带举升台（has_lift）的展台能换轴对，无举升台只能沿用车上现挂轴对；
- 散场前已经出发（departs_before_close）的列车叫不回来：它挂着的轴对随之锁死，
  后续节目不能再借用这套轮（车回库后可在带举升台的展台换另一套轮再上场）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import WheelsError
from .quota import AxleQuota


@dataclass(frozen=True)
class Program:
    program_id: str
    car: str
    stage: str
    has_lift: bool
    ticks: int
    departs_before_close: bool = False


@dataclass
class Assignment:
    program_id: str
    car: str
    stage: str
    axle: str | None
    swapped: bool
    lift_used: bool
    ticks: int
    headroom_after: int | None
    status: str                      # "accepted" | "rejected"
    reject_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {
            "program_id": self.program_id,
            "car": self.car,
            "stage": self.stage,
            "axle": self.axle,
            "swapped": self.swapped,
            "lift_used": self.lift_used,
            "ticks": self.ticks,
            "headroom_after": self.headroom_after,
            "status": self.status,
        }
        if self.reject_reasons:
            out["reject_reasons"] = list(self.reject_reasons)
        return out


@dataclass
class WeekendPlan:
    route: str
    feasible: bool
    assignments: list[Assignment]
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "feasible": self.feasible,
            "note": self.note,
            "assignments": [a.to_dict() for a in self.assignments],
        }


class _Runtime:
    """回溯搜索中的可变状态。"""

    def __init__(self, quota: dict[str, AxleQuota], route: str):
        self.quota = quota
        self.route = route
        self.weekend_used: dict[str, int] = {axle: 0 for axle in quota}
        self.locked: set[str] = set()          # 随散场前出发列车锁死的轴对（叫不回）

    def headroom(self, axle: str) -> int:
        # 用最保守历史口径 consumed_max，保证任何可行履历分支下都不超期。
        return self.quota[axle].headroom_min - self.weekend_used[axle]

    def candidates(self, program: Program, current: str | None) -> list[str]:
        if self.route == "sealed":
            # 封存：开馆前整套备好，不受举升台限制，但已封存/锁死的不能再选。
            pool = [a for a in sorted(self.quota) if a not in self.locked]
        elif not program.has_lift:
            # 无举升台只能沿用车上现挂；现挂轮已被移除/锁死则无轮可用。
            pool = [current] if current is not None and current in self.quota else []
        else:
            pool = [a for a in sorted(self.quota) if a not in self.locked]
        return [a for a in pool if self.headroom(a) >= program.ticks]

    def take(self, program: Program, axle: str) -> None:
        self.weekend_used[axle] += program.ticks
        if self.route == "sealed":
            self.locked.add(axle)              # 封存后整个周末不借出
        if program.departs_before_close:
            self.locked.add(axle)              # 车散场前出发，轴对随车锁死、叫不回

    def release(self, program: Program, axle: str, was_locked: set[str]) -> None:
        self.weekend_used[axle] -= program.ticks
        self.locked = set(was_locked)


def _reject_reasons(rt: _Runtime, program: Program, current: str | None) -> list[str]:
    reasons: list[str] = []
    if rt.route == "shared" and not program.has_lift:
        if current is None or current not in rt.quota:
            reasons.append(f"展台 {program.stage} 无举升台且车上没有可用现挂轴对，无法换轮")
        elif current in rt.locked:
            reasons.append(
                f"展台 {program.stage} 无举升台不能换轮，现挂轴对 {current} "
                f"已随散场前出发的列车锁死、叫不回来"
            )
        elif rt.headroom(current) < program.ticks:
            reasons.append(
                f"展台 {program.stage} 无举升台不能换轮，现挂轴对 {current} "
                f"剩余 {rt.headroom(current)} 格 < 本段 {program.ticks} 格"
            )
        return reasons
    blocked = [a for a in sorted(rt.quota) if a not in rt.locked]
    short = [a for a in blocked if rt.headroom(a) < program.ticks]
    if not blocked:
        reasons.append("没有可借出的轴对（均已封存或随出发列车锁死）")
    elif len(short) == len(blocked):
        detail = ", ".join(f"{a} 剩 {rt.headroom(a)}" for a in short)
        reasons.append(f"全部可用轴对额度不足（本段需 {program.ticks} 格）：{detail}")
    return reasons


def plan_weekend(
    programs: list[Program],
    quota: dict[str, AxleQuota],
    initial_mount: dict[str, str],
    route: str,
) -> WeekendPlan:
    if route not in ("sealed", "shared"):
        raise WheelsError(f"未知路线 {route!r}，只支持 sealed / shared")
    rt = _Runtime(quota, route)
    mount = dict(initial_mount)

    def backtrack(idx: int, picks: list[Assignment]) -> list[Assignment] | None:
        if idx == len(programs):
            return picks
        p = programs[idx]
        current = mount.get(p.car)
        options = rt.candidates(p, current)
        # 优先沿用现挂轴对，减少不必要的举升台换轮。
        if current in options:
            options = [current] + [a for a in options if a != current]
        for axle in options:
            swapped = axle != current
            locked_before = set(rt.locked)
            rt.take(p, axle)
            mount[p.car] = axle
            picks.append(Assignment(
                program_id=p.program_id, car=p.car, stage=p.stage, axle=axle,
                swapped=swapped, lift_used=swapped and route == "shared",
                ticks=p.ticks, headroom_after=rt.headroom(axle), status="accepted",
            ))
            found = backtrack(idx + 1, picks)
            if found is not None:
                return found
            picks.pop()
            mount[p.car] = current
            rt.release(p, axle, locked_before)
        return None

    picks = backtrack(0, [])
    if picks is not None:
        note = ("封存路线：每个节目一套独立轴对，开馆前封存、周末不借出"
                if route == "sealed" else
                "共用路线：只在带举升台的展台换轮，散场前出发列车的轴对随车锁死")
        return WeekendPlan(route=route, feasible=True, assignments=picks, note=note)

    # 不可行：按顺序给出第一个排不下去的节目及拒绝理由，前面的节目照常列出。
    assignments: list[Assignment] = []
    sim = _Runtime(quota, route)
    sim_mount = dict(initial_mount)
    for p in programs:
        current = sim_mount.get(p.car)
        opts = sim.candidates(p, current)
        if not opts:
            assignments.append(Assignment(
                program_id=p.program_id, car=p.car, stage=p.stage, axle=None,
                swapped=False, lift_used=False, ticks=p.ticks, headroom_after=None,
                status="rejected", reject_reasons=_reject_reasons(sim, p, current),
            ))
            break
        axle = opts[0]
        sim.take(p, axle)
        sim_mount[p.car] = axle
        assignments.append(Assignment(
            program_id=p.program_id, car=p.car, stage=p.stage, axle=axle,
            swapped=axle != current, lift_used=axle != current and route == "shared",
            ticks=p.ticks, headroom_after=sim.headroom(axle), status="accepted",
        ))
    return WeekendPlan(
        route=route, feasible=False,
        assignments=assignments,
        note="无可行节目单：已按顺序列出可履行部分与第一个被拒节目及理由",
    )
