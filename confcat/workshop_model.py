"""布景工坊裁纸交接的领域模型与裁装数学。

术语合同
========
* **花纹周期** ``period``：同一花纹按拍数纵向循环，拍号取模到 ``[0, period)``；
  取模只用于判拍，**卷号与卷上原坐标在全过程保留**，回单始终能对回原卷。
* **卷（RollSpec）**：一卷纸有固定正反方向（``direction``：正/反）、标记相位
  ``marker_phase``（坐标 0 那一格的拍号）与若干可用连续区间 ``intervals``
  （闭区间 ``[start, end]``，原坐标）。换一卷的可裁区间另办新单，区间在单间会被占用。
* **可裁段（CutSegment）**：裁纸员从卷的坐标推导出来、真正能裁的连续段，携带
  卷号、原坐标闭区间与装台方向的首/末拍。
* **墙（WallSpec）**：墙面高度（格数）、可选要求首拍；接缝（SeamSpec）记前墙→后墙
  应错开的拍数。墙按接单时给定的墙序连成一圈，末墙回到首墙。
* **现场（Scenario）**：同一出车单在不同台轴销位下的接缝要求变体；无销位差异时
  只有一个默认现场。库内两单看同一张出车单，接单时看不到实销，承诺必须覆盖每个现场。

接缝判花
========
纸段装上墙后沿墙自上而下逐格成拍。设前墙 X 末格相位为 ``last(X)``、后墙 Y 首格
相位为 ``first(Y)``，接缝 X→Y 要求错开 ``d`` 拍，则（末墙与首墙连成一圈同样适用）::

    (first(Y) - last(X) - 1) mod period == d

两种交货
========
* ``sealed`` 封签定向段：库内按面裁好、封签随车，每面一段、不能翻头或另开卷；
  所有现场必须共用同一份（墙 → 卷段）指派才能担保。
* ``whole_roll`` 整卷上台：立好第一面才看到实销，再从余卷下刀；每个现场独立选段，
  已剪掉的边头对后续订单按剩下的整段领（占用落到卷坐标上）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import OrderRejected

DIRECTION_FORWARD = "正"
DIRECTION_REVERSED = "反"
DIRECTIONS = (DIRECTION_FORWARD, DIRECTION_REVERSED)

MODE_SEALED = "sealed"          # 封签定向纸段
MODE_WHOLE_ROLL = "whole_roll"  # 带整卷上台
DELIVERY_MODES = (MODE_SEALED, MODE_WHOLE_ROLL)

MAX_WALLS = 6
MAX_ROLLS = 4

# 归一任务行的固定列名
COL_ROLL_ID = "roll_id"
COL_PERIOD = "pattern_period"
COL_DIRECTION = "direction"
COL_MARKER = "marker_phase"
COL_START = "start_coord"
COL_END = "end_coord"

COL_SCENARIO = "scenario"
COL_WALL_ID = "wall_id"
COL_HEIGHT = "height"
COL_FIRST_BEAT = "first_beat"
COL_FROM_WALL = "from_wall"
COL_TO_WALL = "to_wall"
COL_OFFSET = "offset_beats"
COL_PRE_MOUNTED = "pre_mounted"

ROLL_COLUMNS = (COL_ROLL_ID, COL_PERIOD, COL_DIRECTION, COL_MARKER, COL_START, COL_END)
WALL_COLUMNS = (
    COL_SCENARIO,
    COL_WALL_ID,
    COL_HEIGHT,
    COL_FIRST_BEAT,
    COL_FROM_WALL,
    COL_TO_WALL,
    COL_OFFSET,
    COL_PRE_MOUNTED,
)


# --------------------------------------------------------------------------- #
# 卷与可裁段
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RollSpec:
    """一卷纸的归一事实：方向、标记相位与可用连续区间（原坐标闭区间）。"""

    roll_id: str
    period: int
    direction: str
    marker_phase: int
    intervals: tuple[tuple[int, int], ...]

    def phase_at(self, coord: int) -> int:
        """卷上原坐标 ``coord`` 那一格的拍号。取模前的 (roll_id, coord) 不丢。"""
        return (self.marker_phase + coord) % self.period

    def validate(self) -> None:
        if not self.roll_id:
            raise OrderRejected("卷号不能为空", reason="empty-roll-id")
        if self.period <= 0:
            raise OrderRejected(f"卷 {self.roll_id!r} 花纹周期必须为正整数", reason="bad-period")
        if self.direction not in DIRECTIONS:
            raise OrderRejected(
                f"卷 {self.roll_id!r} 方向必须是 {DIRECTIONS} 之一，实际 {self.direction!r}",
                reason="bad-direction",
            )
        if not 0 <= self.marker_phase < self.period:
            raise OrderRejected(
                f"卷 {self.roll_id!r} 标记相位 {self.marker_phase} 越界（周期 {self.period}）",
                reason="phase-out-of-range",
            )
        if not self.intervals:
            raise OrderRejected(f"卷 {self.roll_id!r} 没有任何可用连续区间", reason="no-interval")
        prev_end = None
        for start, end in self.intervals:
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
                raise OrderRejected(
                    f"卷 {self.roll_id!r} 区间 [{start}, {end}] 非法（需 0 <= start <= end）",
                    reason="bad-interval",
                )
            if prev_end is not None and start <= prev_end:
                raise OrderRejected(
                    f"卷 {self.roll_id!r} 区间须互不相交且按坐标升序", reason="interval-overlap"
                )
            prev_end = end


@dataclass(frozen=True)
class CutSegment:
    """裁纸员交付给装台员的一段可裁纸：保留卷号与原坐标，另带装台方向首尾拍。"""

    roll_id: str
    start: int          # 原坐标闭区间左端
    end: int            # 原坐标闭区间右端
    first_beat: int     # 装上墙后第一格的拍号
    last_beat: int      # 装上墙后最后一格的拍号
    direction: str
    period: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def overlaps(self, other: "CutSegment") -> bool:
        """同卷坐标相交即视为同一段纸被重复领走。"""
        return self.roll_id == other.roll_id and not (self.end < other.start or other.end < self.start)

    def as_dict(self) -> dict:
        return {
            "roll_id": self.roll_id,
            "start": self.start,
            "end": self.end,
            "length": self.length,
            "direction": self.direction,
            "first_beat": self.first_beat,
            "last_beat": self.last_beat,
        }


def derive_segments(roll: RollSpec, height: int) -> list[CutSegment]:
    """裁纸员：从卷的可用连续区间推导高度 ``height`` 的全部真正可裁段。

    正向卷装台自上而下坐标递增，首拍取区间左端坐标的拍；反向卷不能翻头，
    装台方向与坐标走向相反，首拍取右端坐标的拍。长度不足的区间产不出段。
    """
    segments: list[CutSegment] = []
    for iv_start, iv_end in roll.intervals:
        if iv_end - iv_start + 1 < height:
            continue
        if roll.direction == DIRECTION_FORWARD:
            # 窗口 [s, s + height - 1] 在区间内滑动
            for s in range(iv_start, iv_end - height + 2):
                e = s + height - 1
                segments.append(
                    CutSegment(
                        roll_id=roll.roll_id,
                        start=s,
                        end=e,
                        first_beat=roll.phase_at(s),
                        last_beat=roll.phase_at(e),
                        direction=roll.direction,
                        period=roll.period,
                    )
                )
        else:
            # 反向：窗口右端 e 是装台首格，向左数 height 格
            for e in range(iv_start + height - 1, iv_end + 1):
                s = e - height + 1
                segments.append(
                    CutSegment(
                        roll_id=roll.roll_id,
                        start=s,
                        end=e,
                        first_beat=roll.phase_at(e),
                        last_beat=roll.phase_at(s),
                        direction=roll.direction,
                        period=roll.period,
                    )
                )
    return segments


# --------------------------------------------------------------------------- #
# 墙、接缝与现场
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SeamSpec:
    """前墙 → 后墙的接缝：装台时首拍须相对前墙末格错开 ``offset_beats`` 拍。"""

    from_wall: str
    to_wall: str
    offset_beats: int


@dataclass(frozen=True)
class WallSpec:
    """一面墙的高度与可选要求首拍；pre_mounted 表示该面已裁好上台（不领卷）。"""

    wall_id: str
    height: int
    first_beat: int | None = None
    pre_mounted: bool = False


@dataclass(frozen=True)
class Scenario:
    """一个出车现场（台轴销位）下的墙与接缝要求。"""

    name: str
    walls: tuple[WallSpec, ...]
    seams: tuple[SeamSpec, ...]

    def wall(self, wall_id: str) -> WallSpec:
        for w in self.walls:
            if w.wall_id == wall_id:
                return w
        raise KeyError(wall_id)

    def seam(self, from_wall: str, to_wall: str) -> SeamSpec | None:
        for s in self.seams:
            if s.from_wall == from_wall and s.to_wall == to_wall:
                return s
        return None


@dataclass
class WorkshopSpec:
    """一次接单所用的卷料事实、墙面现场与转台墙序（连成一圈）。"""

    period: int
    rolls: tuple[RollSpec, ...]
    scenarios: tuple[Scenario, ...]
    wall_order: tuple[str, ...]

    def roll(self, roll_id: str) -> RollSpec:
        for r in self.rolls:
            if r.roll_id == roll_id:
                return r
        raise OrderRejected(f"卷 {roll_id!r} 不在卷料任务内", reason="unknown-roll")

    def validate(self) -> None:
        if not self.rolls:
            raise OrderRejected("卷料任务没有任何卷", reason="empty-rolls")
        if len(self.rolls) > MAX_ROLLS:
            raise OrderRejected(f"最多 {MAX_ROLLS} 卷纸，实际 {len(self.rolls)}", reason="too-many-rolls")
        if len(set(r.roll_id for r in self.rolls)) != len(self.rolls):
            raise OrderRejected("卷号重复", reason="duplicate-roll-id")
        periods = {r.period for r in self.rolls}
        if len(periods) != 1 or next(iter(periods)) != self.period:
            raise OrderRejected("同一花纹下各卷周期必须一致", reason="period-mismatch")
        for r in self.rolls:
            r.validate()

        if not self.wall_order:
            raise OrderRejected("转台墙序为空", reason="empty-wall-order")
        if len(self.wall_order) > MAX_WALLS:
            raise OrderRejected(f"最多 {MAX_WALLS} 面墙，实际 {len(self.wall_order)}", reason="too-many-walls")
        if len(set(self.wall_order)) != len(self.wall_order):
            raise OrderRejected("转台墙序存在重复墙面", reason="duplicate-wall")
        if not self.scenarios:
            raise OrderRejected("墙面任务没有任何现场", reason="empty-scenario")

        wall_ids = set(self.wall_order)
        for sc in self.scenarios:
            sc_ids = {w.wall_id for w in sc.walls}
            if sc_ids != wall_ids:
                raise OrderRejected(
                    f"现场 {sc.name!r} 墙面集合 {sorted(sc_ids)} 与墙序 {sorted(wall_ids)} 不一致",
                    reason="scenario-wall-mismatch",
                )
            for w in sc.walls:
                if w.height <= 0:
                    raise OrderRejected(
                        f"现场 {sc.name!r} 墙 {w.wall_id!r} 高度必须为正整数", reason="bad-height"
                    )
                if w.first_beat is not None and not 0 <= w.first_beat < self.period:
                    raise OrderRejected(
                        f"墙 {w.wall_id!r} 首拍 {w.first_beat} 越界（周期 {self.period}）",
                        reason="first-beat-out-of-range",
                    )
            seam_pairs = set()
            for seam in sc.seams:
                if seam.from_wall not in wall_ids or seam.to_wall not in wall_ids:
                    raise OrderRejected(
                        f"现场 {sc.name!r} 接缝 {seam.from_wall}->{seam.to_wall} 引用未知墙",
                        reason="unknown-seam-wall",
                    )
                if not 0 <= seam.offset_beats < self.period:
                    raise OrderRejected(
                        f"接缝 {seam.from_wall}->{seam.to_wall} 错拍 {seam.offset_beats} 越界",
                        reason="offset-out-of-range",
                    )
                pair = (seam.from_wall, seam.to_wall)
                if pair in seam_pairs:
                    raise OrderRejected(f"接缝 {pair} 重复定义", reason="duplicate-seam")
                seam_pairs.add(pair)
            # 墙序连成一圈：相邻对（含末墙回首墙）都必须给得出接缝要求
            for i, wid in enumerate(self.wall_order):
                nxt = self.wall_order[(i + 1) % len(self.wall_order)]
                if sc.seam(wid, nxt) is None:
                    raise OrderRejected(
                        f"现场 {sc.name!r} 缺少接缝 {wid}->{nxt}（墙序须连成一圈）",
                        reason="missing-seam",
                    )


# --------------------------------------------------------------------------- #
# 装台核花
# --------------------------------------------------------------------------- #
def seam_ok(period: int, last_beat: int, next_first_beat: int, offset: int) -> bool:
    """接缝判花：后墙首拍相对前墙末格错开 offset 拍（模 period）。"""
    return (next_first_beat - last_beat - 1) % period == offset % period


@dataclass(frozen=True)
class Blockage:
    """不能承诺时指到具体接缝与卷段的卡点。"""

    kind: str                 # no-segment | seam | first-beat | overlap | pre-mounted
    detail: str
    wall_id: str | None = None
    seam: tuple[str, str] | None = None
    roll_id: str | None = None
    segment: tuple[int, int] | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "wall_id": self.wall_id,
            "seam": list(self.seam) if self.seam else None,
            "roll_id": self.roll_id,
            "segment": list(self.segment) if self.segment else None,
        }


@dataclass
class ScenarioPlan:
    """单个现场的接班结果：承诺墙、逐墙指派（已上台墙为 None）与卡点列表。"""

    scenario: str
    guaranteed: bool
    assignments: dict[str, CutSegment | None]
    blockages: list[Blockage] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "guaranteed": self.guaranteed,
            "assignments": {
                wall: (seg.as_dict() if seg is not None else None)
                for wall, seg in self.assignments.items()
            },
            "blockages": [b.as_dict() for b in self.blockages],
        }


def roll_remaining_intervals(
    roll: RollSpec, occupied: list[tuple[int, int]]
) -> tuple[tuple[int, int], ...]:
    """剪掉若干已占用坐标后，卷上还能整段领走的连续区间（闭区间，按坐标升序）。

    已剪掉的边头不属于余量：占用坐标从原区间中挖掉，剩余部分按整段重新拼成区间。
    """
    remaining: list[tuple[int, int]] = []
    occupied = sorted(occupied)
    for start, end in roll.intervals:
        windows = [(start, end)]
        for occ_s, occ_e in occupied:
            next_windows: list[tuple[int, int]] = []
            for w_s, w_e in windows:
                if occ_e < w_s or occ_s > w_e:
                    next_windows.append((w_s, w_e))
                    continue
                if occ_s > w_s:
                    next_windows.append((w_s, min(w_e, occ_s - 1)))
                if occ_e < w_e:
                    next_windows.append((max(w_s, occ_e + 1), w_e))
            windows = next_windows
        remaining.extend((s, e) for s, e in windows if e >= s)
    return tuple(remaining)


def _candidate_segments(
    spec: WorkshopSpec, scenario: Scenario, occupied: dict[str, list[tuple[int, int]]]
) -> dict[str, list[CutSegment]]:
    """每面待装墙的可裁清单：高度匹配、首拍匹配、未被本单/他单占用。"""
    candidates: dict[str, list[CutSegment]] = {}
    for wall in scenario.walls:
        if wall.pre_mounted:
            continue
        pool: list[CutSegment] = []
        for roll in spec.rolls:
            for seg in derive_segments(roll, wall.height):
                if wall.first_beat is not None and seg.first_beat != wall.first_beat:
                    continue
                if any(
                    seg.roll_id == rid and not (seg.end < s or e < seg.start)
                    for rid, ivs in occupied.items()
                    for s, e in ivs
                ):
                    continue
                pool.append(seg)
        # 稳定顺序：卷号、原坐标，保证回单可复看
        pool.sort(key=lambda g: (g.roll_id, g.start, g.end))
        candidates[wall.wall_id] = pool
    return candidates


def _check_ring(
    spec: WorkshopSpec,
    scenario: Scenario,
    chosen: dict[str, CutSegment | None],
) -> Blockage | None:
    """装台员按连成一圈的接缝逐一核花；含已上台墙的固定首拍。"""
    period = spec.period
    for i, wid in enumerate(spec.wall_order):
        nxt = spec.wall_order[(i + 1) % len(spec.wall_order)]
        seam = scenario.seam(wid, nxt)
        wall_to = scenario.wall(nxt)
        seg_from = chosen.get(wid)
        seg_to = chosen.get(nxt)

        if wall_to.pre_mounted:
            if wall_to.first_beat is None:
                return Blockage(
                    "pre-mounted",
                    f"已上台墙 {nxt} 缺少固定首拍，无法核接缝",
                    wall_id=nxt,
                    seam=(wid, nxt),
                )
            next_first = wall_to.first_beat
        else:
            if seg_to is None:
                return Blockage("no-segment", f"墙 {nxt} 没有可指派纸段", wall_id=nxt)
            next_first = seg_to.first_beat

        wall_from = scenario.wall(wid)
        if wall_from.pre_mounted:
            if wall_from.first_beat is None:
                return Blockage(
                    "pre-mounted",
                    f"已上台墙 {wid} 缺少固定首拍，无法核接缝",
                    wall_id=wid,
                    seam=(wid, nxt),
                )
            # 已裁好的墙沿墙逐格成拍，末格拍号由首拍与高度推出
            last_beat = (wall_from.first_beat + wall_from.height - 1) % period
        else:
            if seg_from is None:
                return Blockage("no-segment", f"墙 {wid} 没有可指派纸段", wall_id=wid)
            last_beat = seg_from.last_beat

        if not seam_ok(period, last_beat, next_first, seam.offset_beats):
            seg = chosen.get(nxt)
            return Blockage(
                "seam",
                f"接缝 {wid}->{nxt} 要求错 {seam.offset_beats} 拍，"
                f"实际错 {(next_first - last_beat - 1) % period} 拍",
                wall_id=nxt,
                seam=(wid, nxt),
                roll_id=seg.roll_id if seg else None,
                segment=(seg.start, seg.end) if seg else None,
            )
    return None


def _first_beat_of(scenario: Scenario, wall_id: str, chosen: dict) -> int | None:
    wall = scenario.wall(wall_id)
    if wall.pre_mounted:
        return wall.first_beat
    seg = chosen.get(wall_id)
    return seg.first_beat if seg is not None else None


def _last_beat_of(spec: WorkshopSpec, scenario: Scenario, wall_id: str, chosen: dict) -> int | None:
    wall = scenario.wall(wall_id)
    if wall.pre_mounted:
        if wall.first_beat is None:
            return None
        return (wall.first_beat + wall.height - 1) % spec.period
    seg = chosen.get(wall_id)
    return seg.last_beat if seg is not None else None


def _incoming_seam_ok(
    spec: WorkshopSpec, scenario: Scenario, wall_id: str, chosen: dict
) -> bool:
    """增量核花：wall_id 就位后，其入边（前墙 -> 本墙）能否合花。

    墙序连成一圈，前墙未就位时跳过，待其就位后再核；环闭合缝由末墙入边收口。
    """
    order = spec.wall_order
    idx = order.index(wall_id)
    prev_id = order[(idx - 1) % len(order)]
    if prev_id not in chosen:
        return True
    prev_wall = scenario.wall(prev_id)
    if not prev_wall.pre_mounted and chosen.get(prev_id) is None:
        return True
    last_beat = _last_beat_of(spec, scenario, prev_id, chosen)
    next_first = _first_beat_of(scenario, wall_id, chosen)
    if last_beat is None or next_first is None:
        return False
    seam = scenario.seam(prev_id, wall_id)
    return seam_ok(spec.period, last_beat, next_first, seam.offset_beats)


def plan_scenario(
    spec: WorkshopSpec,
    scenario: Scenario,
    occupied: dict[str, list[tuple[int, int]]],
    *,
    fixed: dict[str, CutSegment | None] | None = None,
) -> ScenarioPlan:
    """单个现场接班：回溯找一份每面一段、同段不重复领、整圈接缝合花的指派。

    ``fixed`` 用于封签模式：跨现场必须沿用同一份指派，本现场只能验收不能换段。
    """
    fixed = fixed or {}
    candidates = _candidate_segments(spec, scenario, occupied)
    walls_to_fill = [
        w.wall_id
        for w in scenario.walls
        if not w.pre_mounted and w.wall_id not in fixed
    ]

    # 已上台墙先占位
    chosen: dict[str, CutSegment | None] = {
        w.wall_id: None for w in scenario.walls if w.pre_mounted
    }
    chosen.update(fixed)

    # 固定指派先做可用性与同段重复校验
    used_segments: list[CutSegment] = [s for s in chosen.values() if s is not None]
    for i, seg in enumerate(used_segments):
        for other in used_segments[:i]:
            if seg.overlaps(other):
                return ScenarioPlan(
                    scenario.name,
                    False,
                    dict(chosen),
                    [
                        Blockage(
                            "overlap",
                            f"卷 {seg.roll_id} 段 [{seg.start},{seg.end}] 与 "
                            f"[{other.start},{other.end}] 被两面墙重复领走",
                            roll_id=seg.roll_id,
                            segment=(seg.start, seg.end),
                        )
                    ],
                )

    # 已上台墙/封签固定墙的入边先核一遍（固定墙本身不可换段）
    for wid in list(chosen.keys()):
        seg = chosen.get(wid)
        if seg is not None:
            wall = scenario.wall(wid)
            if wall.first_beat is not None and seg.first_beat != wall.first_beat:
                return ScenarioPlan(
                    scenario.name,
                    False,
                    dict(chosen),
                    [
                        Blockage(
                            "first-beat",
                            f"封签固定段首拍 {seg.first_beat} 与墙 {wid} 要求首拍 "
                            f"{wall.first_beat} 不符，上台不能翻头",
                            wall_id=wid,
                            roll_id=seg.roll_id,
                            segment=(seg.start, seg.end),
                        )
                    ],
                )
        if not _incoming_seam_ok(spec, scenario, wid, chosen):
            blockage = _ring_blockage(spec, scenario, wid, chosen)
            return ScenarioPlan(scenario.name, False, dict(chosen), [blockage])

    order = sorted(
        walls_to_fill,
        key=lambda wid: (len(candidates.get(wid, ())), wid),
    )

    def backtrack(idx: int) -> dict[str, CutSegment | None] | None:
        if idx == len(order):
            return dict(chosen)
        wid = order[idx]
        wall = scenario.wall(wid)
        for seg in candidates.get(wid, ()):
            if any(seg.overlaps(u) for u in used_segments):
                continue
            if wall.first_beat is not None and seg.first_beat != wall.first_beat:
                continue
            chosen[wid] = seg
            used_segments.append(seg)
            # 增量核花：本墙入边不合立即换下一段，而不是填满后才发现
            if _incoming_seam_ok(spec, scenario, wid, chosen):
                solution = backtrack(idx + 1)
                if solution is not None:
                    return solution
            used_segments.pop()
            del chosen[wid]
        return None

    solution = backtrack(0)
    if solution is None:
        blockage = _diagnose(spec, scenario, candidates, chosen, occupied)
        return ScenarioPlan(scenario.name, False, dict(chosen), [blockage])
    blockage = _check_ring(spec, scenario, solution)
    if blockage is not None:
        return ScenarioPlan(scenario.name, False, solution, [blockage])
    return ScenarioPlan(scenario.name, True, solution)


def _ring_blockage(
    spec: WorkshopSpec, scenario: Scenario, wall_id: str, chosen: dict
) -> Blockage:
    order = spec.wall_order
    idx = order.index(wall_id)
    prev_id = order[(idx - 1) % len(order)]
    seam = scenario.seam(prev_id, wall_id)
    last_beat = _last_beat_of(spec, scenario, prev_id, chosen)
    next_first = _first_beat_of(scenario, wall_id, chosen)
    actual = None if last_beat is None or next_first is None else (next_first - last_beat - 1) % spec.period
    # 卡点优先携带可换段那一侧（非已上台墙）的卷段，方便领班按卷段复查
    seg = chosen.get(wall_id)
    if seg is None:
        seg = chosen.get(prev_id)
    return Blockage(
        "seam",
        f"接缝 {prev_id}->{wall_id} 要求错 {seam.offset_beats} 拍，"
        f"实际错 {actual if actual is not None else '未知'} 拍",
        wall_id=wall_id,
        seam=(prev_id, wall_id),
        roll_id=seg.roll_id if seg else None,
        segment=(seg.start, seg.end) if seg else None,
    )


def _diagnose(
    spec: WorkshopSpec,
    scenario: Scenario,
    candidates: dict[str, list[CutSegment]],
    partial: dict[str, CutSegment | None],
    occupied: dict[str, list[tuple[int, int]]],
) -> Blockage:
    """回溯失败时把卡点指到具体墙/接缝/卷段。"""
    # 1) 有墙连一张可裁清单都没有
    for wall in scenario.walls:
        if wall.pre_mounted or wall.wall_id in partial:
            continue
        pool = candidates.get(wall.wall_id, [])
        if not pool:
            raw = [seg for roll in spec.rolls for seg in derive_segments(roll, wall.height)]
            if wall.first_beat is not None:
                beat_match = [s for s in raw if s.first_beat == wall.first_beat]
                if not beat_match:
                    return Blockage(
                        "first-beat",
                        f"墙 {wall.wall_id} 要求首拍 {wall.first_beat}，无卷段可满足",
                        wall_id=wall.wall_id,
                    )
            if not raw:
                return Blockage(
                    "no-segment",
                    f"墙 {wall.wall_id} 高 {wall.height} 格，没有任何卷段够长且未占用",
                    wall_id=wall.wall_id,
                )
            return Blockage(
                "overlap",
                f"墙 {wall.wall_id} 的可裁段均已被他单或本单占用",
                wall_id=wall.wall_id,
            )
    # 2) 段都在但接缝合不上：找主因接缝——后墙的任何候选段，对前墙任何可能相位
    #    都合不上花（与后续如何指派无关）。环闭合缝（末墙回首墙）放第二轮，
    #    优先报告墙序正向的主因卡点。
    ring_closure = (spec.wall_order[-1], spec.wall_order[0])
    for pass_no in (1, 2):
        for i, wid in enumerate(spec.wall_order):
            prev_id = spec.wall_order[(i - 1) % len(spec.wall_order)]
            if pass_no == 1 and (prev_id, wid) == ring_closure:
                continue
            wall = scenario.wall(wid)
            if wall.pre_mounted or wid in partial:
                continue
            prev_wall = scenario.wall(prev_id)
            if prev_wall.pre_mounted or prev_id in partial:
                prev_last_beats = [_last_beat_of(spec, scenario, prev_id, partial)]
            else:
                prev_last_beats = [seg.last_beat for seg in candidates.get(prev_id, ())]
            prev_last_beats = [b for b in prev_last_beats if b is not None]
            if not prev_last_beats:
                continue
            seam = scenario.seam(prev_id, wid)
            pool = candidates.get(wid, ())
            if all(
                not any(
                    seam_ok(spec.period, last_beat, seg.first_beat, seam.offset_beats)
                    for seg in pool
                )
                for last_beat in prev_last_beats
            ):
                last_beat = prev_last_beats[0]
                seg = pool[-1] if pool else None
                actual = (
                    (seg.first_beat - last_beat - 1) % spec.period if seg is not None else None
                )
                return Blockage(
                    "seam",
                    f"接缝 {prev_id}->{wid} 要求错 {seam.offset_beats} 拍，"
                    f"可用卷段实际错 {actual if actual is not None else '未知'} 拍",
                    wall_id=wid,
                    seam=(prev_id, wid),
                    roll_id=seg.roll_id if seg else None,
                    segment=(seg.start, seg.end) if seg else None,
                )
    # 3) 已就位墙之间现存的违例接缝（封签固定指派场景）
    for i, wid in enumerate(spec.wall_order):
        if wid not in partial:
            continue
        if not _incoming_seam_ok(spec, scenario, wid, partial):
            return _ring_blockage(spec, scenario, wid, partial)
    return Blockage("seam", "不存在满足整圈接缝与不重复领用的指派")
