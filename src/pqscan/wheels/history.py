"""按实物轴对接回履历，并保留换轮签条模糊造成的互斥分支。

数据模型（与扁平 Parquet 的观测行一一对应）：
- 观测按车号、时刻排序；相邻两次观测 o_i -> o_{i+1} 构成一段，段号 seg_id 记在
  起点观测上（末条观测没有段号）。行程表按 seg_id 给该段允许格数区间 [lo, hi]。
- 一次换轮发生在某次观测处：签条清楚时观测行直接带轴对号；签条模糊时轴对号为
  NULL、只带 swap_tag，候选轴对在 tags.json 里。同一 tag 的不同候选互斥，
  每个候选组合展开成一条分支（branch）。
- 段内走行归属于"起点观测时装在车上的那套轴对"；若起点是模糊签条，则该归属
  随分支而定。分支还要满足物理约束：同一时刻一套轴对不能同时装在两台车上，
  不满足的分支标记为不可行并写明排斥原因，但仍保留在输出里（互斥可能性不丢弃）。

段校验交给 drum.feasible_ticks：可行实际格数必须与首尾鼓面余数相符且落在
[lo, hi] 内；可行解为空即履历矛盾，直接报错。每个可行解的跨零次数 d//10
一并登记，若同一段不同可行解跨零次数不同，在段轨迹里标 ambiguous_rollover。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any

from .drum import feasible_ticks
from ..errors import WheelsError


@dataclass(frozen=True)
class Observation:
    car: str
    ts: int
    drum: int
    axle: str | None            # 签条清楚时的轴对号；模糊时为 None
    tag: str | None             # 模糊换轮签条 id
    seg_id: str | None          # 以本观测为起点的段号（末条观测为 None）


@dataclass(frozen=True)
class ItinerarySegment:
    seg_id: str
    car: str
    lo: int
    hi: int


@dataclass
class SegmentTrace:
    """一段在某条分支下的复原轨迹。"""

    seg_id: str
    car: str
    start_ts: int
    end_ts: int
    before: int
    after: int
    lo: int
    hi: int
    mounted_axle: str
    feasible_ticks: list[int]
    rollover_options: list[int]
    ambiguous_rollover: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "seg_id": self.seg_id,
            "car": self.car,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "drum_before": self.before,
            "drum_after": self.after,
            "allowed_range": [self.lo, self.hi],
            "mounted_axle": self.mounted_axle,
            "feasible_ticks": list(self.feasible_ticks),
            "rollover_options": list(self.rollover_options),
            "ambiguous_rollover": self.ambiguous_rollover,
            "consumed_min": self.feasible_ticks[0],
            "consumed_max": self.feasible_ticks[-1],
        }


@dataclass
class Branch:
    """一条互斥分支：每个模糊签条选定一个候选轴对。"""

    name: str
    tag_choice: dict[str, str]
    feasible: bool
    exclusion_reasons: list[str] = field(default_factory=list)
    # 轴对 -> 落在该轴对上的段（按时间）
    axle_segments: dict[str, list[SegmentTrace]] = field(default_factory=dict)
    segments: list[SegmentTrace] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.name,
            "tag_choice": dict(self.tag_choice),
            "feasible": self.feasible,
            "exclusion_reasons": list(self.exclusion_reasons),
            "segments": [s.to_dict() for s in self.segments],
            "axle_segments": {
                axle: [s.seg_id for s in traces]
                for axle, traces in sorted(self.axle_segments.items())
            },
        }


def _mounted_axle(obs: Observation, tag_choice: dict[str, str]) -> str:
    if obs.axle is not None:
        return obs.axle
    if obs.tag is None:
        raise WheelsError(f"车 {obs.car} 在 t={obs.ts} 的观测既无轴对号也无换轮签条")
    return tag_choice[obs.tag]


def reconstruct(
    observations: list[Observation],
    itinerary: list[ItinerarySegment],
    tag_candidates: dict[str, list[str]],
) -> list[Branch]:
    """展开全部签条分支，逐分支做段校验与轴对归集。

    观测/行程表的结构性问题（缺段、重复时刻、区间与鼓面矛盾）直接抛 WheelsError；
    分支间的物理排斥不抛错，登记在 Branch.exclusion_reasons 里保留下来。
    """
    by_car: dict[str, list[Observation]] = {}
    for obs in observations:
        by_car.setdefault(obs.car, []).append(obs)
    for car, rows in by_car.items():
        rows.sort(key=lambda o: o.ts)
        if len({o.ts for o in rows}) != len(rows):
            raise WheelsError(f"车 {car} 存在重复观测时刻")
        for obs in rows:
            if (obs.axle is None) == (obs.tag is None):
                raise WheelsError(
                    f"车 {car} 在 t={obs.ts} 的观测必须恰好带轴对号或模糊签条之一"
                )

    seg_by_id = {s.seg_id: s for s in itinerary}
    if len(seg_by_id) != len(itinerary):
        raise WheelsError("行程表存在重复 seg_id")

    # 每台车相邻观测配对，并把行程表区间挂上去。
    paired: list[tuple[Observation, Observation]] = []
    for car, rows in by_car.items():
        for cur, nxt in zip(rows, rows[1:]):
            if cur.seg_id is None:
                raise WheelsError(f"车 {car} 在 t={cur.ts} 的观测缺少段号 seg_id")
            if cur.seg_id not in seg_by_id:
                raise WheelsError(f"段 {cur.seg_id} 在行程表里找不到")
            spec = seg_by_id[cur.seg_id]
            if spec.car != car:
                raise WheelsError(f"段 {cur.seg_id} 归属车 {spec.car}，却挂在车 {car} 上")
            paired.append((cur, nxt))

    blurry_tags = sorted({o.tag for rows in by_car.values() for o in rows if o.tag is not None})
    for tag in blurry_tags:
        if tag not in tag_candidates or not tag_candidates[tag]:
            raise WheelsError(f"模糊签条 {tag} 没有候选轴对")

    branches: list[Branch] = []
    choices = [tag_candidates[tag] for tag in blurry_tags]
    for combo in product(*choices):
        tag_choice = dict(zip(blurry_tags, combo))
        name = "+".join(f"{tag}->{tag_choice[tag]}" for tag in blurry_tags) or "deterministic"
        branch = Branch(name=name, tag_choice=tag_choice, feasible=True)

        # 物理约束：同一轴对在同一时间窗 [t_i, t_{i+1}) 不能挂两台车。
        windows: dict[str, list[tuple[int, int, str]]] = {}
        for cur, nxt in paired:
            axle = _mounted_axle(cur, tag_choice)
            windows.setdefault(axle, []).append((cur.ts, nxt.ts, cur.car))
        for axle, wins in windows.items():
            wins.sort()
            for (s1, e1, c1), (s2, e2, c2) in zip(wins, wins[1:]):
                if s2 < e1:
                    branch.feasible = False
                    branch.exclusion_reasons.append(
                        f"轴对 {axle} 在 [{s2}, {min(e1, e2)}) 同时挂在车 {c1} 与车 {c2} 上"
                    )

        for cur, nxt in paired:
            spec = seg_by_id[cur.seg_id]
            ticks_options = feasible_ticks(cur.drum, nxt.drum, spec.lo, spec.hi)
            if not ticks_options:
                raise WheelsError(
                    f"段 {cur.seg_id}（车 {cur.car}）：鼓面 {cur.drum}->{nxt.drum} "
                    f"与行程表允许区间 [{spec.lo}, {spec.hi}] 无任何相符格数，履历矛盾"
                )
            roll_options = [d // 10 for d in ticks_options]
            trace = SegmentTrace(
                seg_id=cur.seg_id, car=cur.car, start_ts=cur.ts, end_ts=nxt.ts,
                before=cur.drum, after=nxt.drum, lo=spec.lo, hi=spec.hi,
                mounted_axle=_mounted_axle(cur, tag_choice),
                feasible_ticks=ticks_options, rollover_options=roll_options,
                ambiguous_rollover=len(set(roll_options)) > 1,
            )
            branch.segments.append(trace)
            branch.axle_segments.setdefault(trace.mounted_axle, []).append(trace)

        for traces in branch.axle_segments.values():
            traces.sort(key=lambda s: (s.start_ts, s.seg_id))
        branch.segments.sort(key=lambda s: (s.start_ts, s.car, s.seg_id))
        branches.append(branch)

    return branches
