"""题面场景：单船基线、歧义脉冲水账、死线、两船反向回收、侧池容量、直通方案。

每个场景返回一个结构化结果（可 JSON 序列化），并由 ``tools/lock_demo.py``
统一打印逐拍水账与闭闸理由。场景编号与题面叙述一一对应：

* :func:`scenario_single_baseline` —— 下游 0 / 上游 4 / 闸室 0，四拍平水。
* :func:`scenario_ambiguous_pulse` —— 末个入池脉冲可能去了别池，枚举整套
  相容水账，演示“不能把两个池的可能上限相加”。
* :func:`scenario_deadline_three` —— 最迟开上门时间移到第三拍，同一水账
  无法如期放船。
* :func:`scenario_two_ships_recycle` —— 两艘反向船，侧池回收水何时才可
  用于下一闸（主交付场景，七格以内逐拍账）。
* :func:`scenario_side_too_small` —— 侧池容量不足，凭预计库存开阀被拒。
* :func:`scenario_straight_through` —— 独立上池补水/下池排水的直通方案，
  附每拍补排能力与整场水量证明。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .lock import (
    CHAMBER,
    DOWNSTREAM,
    LockError,
    LockTopology,
    PulseObs,
    SIDE,
    UPSTREAM,
    VALVE_DOWN,
    VALVE_DRAIN,
    VALVE_MAKEUP,
    VALVE_SIDE,
    VALVE_UP,
    reconcile_pulses,
    side_pool_topology,
    straight_through_topology,
)
from .lock_schedule import (
    DOWNBOUND,
    UPBOUND,
    LockScheduler,
    SchedulingError,
    Ship,
)

UPSTREAM_LEVEL = 4
CHAMBER_SIZE = 4


def _standard_topology(side_capacity: int = 4) -> LockTopology:
    return side_pool_topology(chamber=CHAMBER_SIZE, side_capacity=side_capacity)


def _standard_levels(side: int = 0) -> dict[str, int]:
    # 外部河道标尺恒定；闸室与侧池给真实起始量。
    return {
        CHAMBER: 0,
        UPSTREAM: UPSTREAM_LEVEL,
        DOWNSTREAM: 0,
        SIDE: side,
    }


# ---------------------------------------------------------------------------
# 场景 1：单船基线 —— 四格清水，逐拍一格，第四拍平水
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    feasible: bool
    reason_code: str | None
    reason: str | None
    detail: Mapping[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {
            "scenario": self.name,
            "feasible": self.feasible,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "detail": self.detail,
        }


def scenario_single_baseline() -> ScenarioResult:
    """下游 0、上游 4、闸室 0；水池核清有 4 格，逐拍放一格第 4 拍平水。"""

    topology = _standard_topology()
    # 水账端：四笔确定脉冲，全部上游 -> 闸室，每拍一笔。
    pulses = [
        PulseObs.observed(beat=beat, valve=VALVE_UP, src=UPSTREAM, amount=1, candidates=CHAMBER)
        for beat in range(1, 5)
    ]
    valve_open = {beat: {VALVE_UP: True} for beat in range(1, 5)}
    books = reconcile_pulses(topology, _standard_levels(), pulses, valve_open)
    assert len(books) == 1
    book = books[0]
    level_at_4 = book.level_at(4, CHAMBER)

    # 放船端：第 5 拍开上门进闸（平水 4=4），泄四格到侧池，再开下门出闸。
    scheduler = LockScheduler(topology, _standard_levels())
    ship = Ship.create(
        "甲(下行)",
        DOWNBOUND,
        arrival_beat=1,
        latest_pass_beat=12,
        allowed_valves=(VALVE_UP, VALVE_SIDE, VALVE_DOWN),
    )
    schedule = scheduler.run([ship])
    return ScenarioResult(
        name="single_baseline",
        feasible=(level_at_4 == UPSTREAM_LEVEL and schedule.pass_beats[ship.name] == 10),
        reason_code=None,
        reason=None,
        detail={
            "pulse_ledger": book.as_json(),
            "level_at_beat_4": level_at_4,
            "schedule": schedule.as_json(),
            "conclusion": f"四笔脉冲全部入闸，第 4 拍闸室 {level_at_4} 格与上游标尺 4 平，第 5 拍准开上门。",
        },
    )


# ---------------------------------------------------------------------------
# 场景 2：歧义脉冲 —— 末个脉冲可能进闸室、也可能进侧池
# ---------------------------------------------------------------------------


def scenario_ambiguous_pulse() -> ScenarioResult:
    """同一组观测枚举整套相容账：一套 4 格可平水，一套只 3 格差一格。

    观测端只知道公共引水支路上有 4 个脉冲，前 3 个确定进闸室，第 4 个去向
    在闸室/侧池之间不明。正确做法是交出两套完整水账分别核算，而不是认为
    闸室“最多 4 格”就按 4 格安排放船。
    """

    topology = _standard_topology()
    # 公共引水支管把上游、闸室、侧池连在同一观测点之后；阀门状态仍按
    # valve_up 的开/闭观测记账，去向在支管后分叉。
    manifold = (UPSTREAM, CHAMBER, SIDE)
    pulses = [
        PulseObs.observed(beat=1, valve=VALVE_UP, src=UPSTREAM, amount=1, candidates=CHAMBER),
        PulseObs.observed(beat=2, valve=VALVE_UP, src=UPSTREAM, amount=1, candidates=CHAMBER),
        PulseObs.observed(beat=3, valve=VALVE_UP, src=UPSTREAM, amount=1, candidates=CHAMBER),
        # 末个脉冲：公共支管后去向不明，闸室或侧池二选一
        PulseObs.observed(
            beat=4,
            valve=VALVE_UP,
            src=UPSTREAM,
            amount=1,
            candidates=(CHAMBER, SIDE),
            manifold=manifold,
        ),
    ]
    valve_open = {beat: {VALVE_UP: True} for beat in range(1, 5)}
    books = reconcile_pulses(topology, _standard_levels(), pulses, valve_open)

    per_book = []
    feasible_books = 0
    for index, book in enumerate(books, start=1):
        chamber_level = book.level_at(4, CHAMBER)
        side_level = book.level_at(4, SIDE)
        can_level = chamber_level >= UPSTREAM_LEVEL
        if can_level:
            feasible_books += 1
        per_book.append(
            {
                "ledger_no": index,
                "chamber_at_4": chamber_level,
                "side_at_4": side_level,
                "enough_to_level": can_level,
                "entries": [entry.as_json() for entry in book.entries],
                "shortfall": max(0, UPSTREAM_LEVEL - chamber_level),
            }
        )

    # 放船可行性必须对**每套**相容账成立；只要有一套账差一格，就不能保证平水。
    universally_feasible = feasible_books == len(books)
    short_book = next((row for row in per_book if not row["enough_to_level"]), None)
    return ScenarioResult(
        name="ambiguous_pulse",
        feasible=universally_feasible,
        reason_code=None if universally_feasible else "ambiguous_shortfall",
        reason=None
        if universally_feasible
        else f"相容账 {short_book['ledger_no']} 中末脉冲进了侧池，闸室只有 {short_book['chamber_at_4']} 格，"
        f"距平水差 {short_book['shortfall']} 格；两池上限不得相加，按该账必须闭闸。",
        detail={
            "compatible_ledgers": per_book,
            "ledger_count": len(books),
            "note": "禁止把闸室与侧池的可能上限相加；逐套账独立回答放船可行性。",
        },
    )


# ---------------------------------------------------------------------------
# 场景 3：死线 —— 最迟开上门第三拍，同一水账来不及
# ---------------------------------------------------------------------------


def scenario_deadline_three() -> ScenarioResult:
    """恢复 4 格水账，但要求第 3 拍前必须完成过闸；进水本身就要 4 拍。

    水账与基线完全相同（同一套 4 格脉冲），只改最晚过闸拍：第 1 拍开始
    服务，本闸需要 10 个原子拍（进水 4 + 进闸 1 + 泄水 4 + 出闸 1），
    最早第 10 拍才能出闸，死线 3 必然违约——不是水不够，是时间窗不够。
    """

    topology = _standard_topology()
    tight = Ship.create(
        "甲(下行,急)",
        DOWNBOUND,
        arrival_beat=1,
        latest_pass_beat=3,
        allowed_valves=(VALVE_UP, VALVE_SIDE, VALVE_DOWN),
    )
    scheduler = LockScheduler(topology, _standard_levels())
    try:
        scheduler.run([tight])
    except SchedulingError as exc:
        return ScenarioResult(
            name="deadline_three",
            feasible=False,
            reason_code=exc.code,
            reason=f"同一 4 格水账无法如期放船：{exc}",
            detail={
                "beat": exc.beat,
                "fill_pulses_required": UPSTREAM_LEVEL,
                "earliest_entry_gate_beat": UPSTREAM_LEVEL + 1,
                "latest_entry_gate_beat": 3,
                "detail": exc.detail,
            },
        )
    return ScenarioResult("deadline_three", True, None, None, {"note": "不应到达"})  # pragma: no cover


# ---------------------------------------------------------------------------
# 场景 4（主交付）：两艘反向船 + 侧池全回收
# ---------------------------------------------------------------------------


def scenario_two_ships_recycle() -> ScenarioResult:
    """甲下行、乙上行；侧池容量 4，一闸水全部留住再归还，全程不补新水。"""

    topology = _standard_topology(side_capacity=4)
    levels = _standard_levels(side=0)
    ships = [
        Ship.create(
            "甲(下行)",
            DOWNBOUND,
            arrival_beat=1,
            latest_pass_beat=20,
            allowed_valves=(VALVE_UP, VALVE_SIDE, VALVE_DOWN),
        ),
        Ship.create(
            "乙(上行)",
            UPBOUND,
            arrival_beat=2,
            latest_pass_beat=20,
            allowed_valves=(VALVE_UP, VALVE_SIDE, VALVE_DOWN),
        ),
    ]
    scheduler = LockScheduler(topology, levels)
    schedule = scheduler.run(ships)

    # 守恒核对：有限容器（闸室+侧池）总水量守恒。首闸从上游借入 4 格后，
    # 这 4 格始终在闸室/侧池之间周转，闭系统内一格不丢。进水拍水量逐拍
    # 上涨是正常的，因此只要求：任何一拍不超过借入总量，且终值恰好等于它。
    initial_finite = sum(value for pool, value in levels.items() if not topology.is_infinite(pool))
    borrowed = UPSTREAM_LEVEL
    finite_totals = {
        action.beat: sum(
            action.levels[pool] for pool in topology.pools if not topology.is_infinite(pool)
        )
        for action in schedule.actions
    }
    conserved = (
        max(finite_totals.values()) <= borrowed
        and finite_totals[schedule.last_beat] == borrowed
        and all(total >= 0 for total in finite_totals.values())
    )
    # 回收水可用性：乙的进水动作中，侧池 -> 闸室的第一拍必须晚于甲最后一笔
    # 闸室 -> 侧池脉冲（回收完成之后才可用）。
    side_capture_beats = [
        action.beat
        for action in schedule.actions
        for transfer in action.valve_transfers
        if transfer.valve == VALVE_SIDE and transfer.src == CHAMBER
    ]
    side_reuse_beats = [
        action.beat
        for action in schedule.actions
        for transfer in action.valve_transfers
        if transfer.valve == VALVE_SIDE and transfer.src == SIDE
    ]
    reuse_only_after_capture = (
        bool(side_capture_beats)
        and bool(side_reuse_beats)
        and min(side_reuse_beats) > max(side_capture_beats)
    )
    return ScenarioResult(
        name="two_ships_recycle",
        feasible=conserved and reuse_only_after_capture,
        reason_code=None,
        reason=None,
        detail={
            "schedule": schedule.as_json(),
            "finite_total_by_beat": finite_totals,
            "finite_total_expected": initial_finite + UPSTREAM_LEVEL,
            "side_capture_beats": side_capture_beats,
            "side_reuse_beats": side_reuse_beats,
            "recycle_rule": (
                f"回收水在第 {max(side_capture_beats)} 拍才全部进入侧池，"
                f"乙最早第 {min(side_reuse_beats)} 拍才能调用——未到账的水不得提前支配。"
            ),
            "pass_beats": dict(schedule.pass_beats),
            "valve_usage": dict(schedule.valve_usage),
        },
    )


# ---------------------------------------------------------------------------
# 场景 5：侧池容量不足 —— 凭预计库存开阀被物理拒绝
# ---------------------------------------------------------------------------


def scenario_side_too_small() -> ScenarioResult:
    """侧池只建 2 格，展方要求一闸 4 格全部回收、不设泄下游支路。

    计划阶段侧池真实空位只有 2 格，剩余 2 格既进不了侧池（容量墙）又没有
    允许的泄水支路，``branch_not_allowed`` 直接闭闸；若强行只开侧阀，运行时
    第三笔脉冲会撞 ``level_out_of_range``。两种拒绝都证明：水位/容量未知时
    不能凭预计库存开阀。
    """

    topology = _standard_topology(side_capacity=2)
    levels = _standard_levels(side=0)
    ship = Ship.create(
        "甲(下行)",
        DOWNBOUND,
        arrival_beat=1,
        latest_pass_beat=20,
        # 全回收诉求：只给上游引水与侧池阀，不给泄下游支路。
        allowed_valves=(VALVE_UP, VALVE_SIDE),
    )
    scheduler = LockScheduler(topology, levels)
    try:
        scheduler.run([ship])
    except SchedulingError as exc:
        return ScenarioResult(
            name="side_too_small",
            feasible=False,
            reason_code=exc.code,
            reason=f"侧池容量 2 格留不住一闸 4 格水：{exc}",
            detail={"beat": exc.beat, "detail": exc.detail, "side_capacity": 2},
        )

    # 兜底：直接对状态机发第三笔超额回收脉冲，验证物理层 level_out_of_range。
    from .lock import LockState  # 局部导入避免顶层循环观感

    state = LockState(
        topology=topology,
        levels={CHAMBER: 2, UPSTREAM: UPSTREAM_LEVEL, DOWNSTREAM: 0, SIDE: 2},
        open_gates=frozenset(),
        open_valves=frozenset(),
    )
    try:
        state.open_valve(VALVE_SIDE).transfer(VALVE_SIDE, 1, direction=CHAMBER)
    except LockError as exc:
        return ScenarioResult(
            name="side_too_small",
            feasible=False,
            reason_code=exc.code,
            reason=f"侧池满后第三笔回收脉冲被物理拒绝：{exc}",
            detail={"detail": exc.detail, "side_capacity": 2},
        )
    return ScenarioResult("side_too_small", True, None, None, {"note": "不应到达"})  # pragma: no cover


# ---------------------------------------------------------------------------
# 场景 6：直通方案 —— 独立上池补水、下池排水
# ---------------------------------------------------------------------------


def scenario_straight_through() -> ScenarioResult:
    """无侧池：上行补水、下行排水均走独立外部支路，证明能力与整场水量。"""

    topology = straight_through_topology(chamber=CHAMBER_SIZE, makeup_per_beat=1, drain_per_beat=1)
    levels = {
        CHAMBER: 0,
        UPSTREAM: UPSTREAM_LEVEL,
        DOWNSTREAM: 0,
        # 外部水库给恒定标尺读数；INFINITE_CAPACITY 保证转移后尺读数不动。
        "makeup_source": UPSTREAM_LEVEL,
        "drain_sink": 0,
    }
    # 甲下行只走上游引水 + 下游泄水；乙上行走独立补水 + 独立排水。
    ships = [
        Ship.create(
            "甲(下行)",
            DOWNBOUND,
            arrival_beat=1,
            latest_pass_beat=20,
            allowed_valves=(VALVE_UP, VALVE_DOWN),
        ),
        Ship.create(
            "乙(上行)",
            UPBOUND,
            arrival_beat=2,
            latest_pass_beat=20,
            allowed_valves=(VALVE_MAKEUP, VALVE_DRAIN),
        ),
    ]
    scheduler = LockScheduler(topology, levels)
    schedule = scheduler.run(ships)

    makeup = schedule.valve_usage.get(VALVE_MAKEUP, 0)
    drain = schedule.valve_usage.get(VALVE_DRAIN, 0)
    upstream_draw = schedule.valve_usage.get(VALVE_UP, 0)
    downstream_dump = schedule.valve_usage.get(VALVE_DOWN, 0)
    # 每拍补/排能力均为 1 格，而任何一拍至多 1 笔脉冲 -> 能力逐拍足够。
    max_per_beat = max(
        (
            sum(1 for transfer in action.valve_transfers if transfer.valve in (VALVE_MAKEUP, VALVE_DRAIN))
            for action in schedule.actions
        ),
        default=0,
    )
    capacity_ok = max_per_beat <= 1
    # 整场水量：外部水库无限，需证明的是有限闸室从未越界（调度器已逐拍强校验）。
    chamber_ok = all(0 <= action.levels[CHAMBER] <= CHAMBER_SIZE for action in schedule.actions)
    return ScenarioResult(
        name="straight_through",
        feasible=capacity_ok and chamber_ok,
        reason_code=None,
        reason=None,
        detail={
            "schedule": schedule.as_json(),
            "whole_game_water": {
                "makeup_from_upper_pool": makeup,
                "drain_to_lower_pool": drain,
                "upstream_river_draw": upstream_draw,
                "downstream_river_dump": downstream_dump,
            },
            "per_beat_capacity_proof": {
                "makeup_per_beat": 1,
                "drain_per_beat": 1,
                "max_external_pulses_in_one_beat": max_per_beat,
                "capacity_ok": capacity_ok,
            },
            "chamber_bounds_ok": chamber_ok,
            "pass_beats": dict(schedule.pass_beats),
        },
    )


ALL_SCENARIOS = (
    scenario_single_baseline,
    scenario_ambiguous_pulse,
    scenario_deadline_three,
    scenario_two_ships_recycle,
    scenario_side_too_small,
    scenario_straight_through,
)


def run_all() -> list[ScenarioResult]:
    return [fn() for fn in ALL_SCENARIOS]
