"""放船调度：平水开门规则下的可执行闸序搜索。

输入每条船的抵达拍、最晚过闸拍（过闸拍 = 船离开闸室那一次开门拍）、航向
与允许开启的引水支路，调度器逐拍推进 :class:`~bitcatalog.lock.LockState`，
输出一份逐拍水账与闸序；不可行时给出决定闭闸的具体理由（错误码 + 拍号）。

关键纪律
========

* 闸门只在两侧尺读数相等时开启；阀门只在相关闸门全关时引水。
* 每条船按抵达顺序接受服务（先来先过），后船不得抢闸。
* 已落账的水是追加账：调度器只能在当前真实库存上继续转移，不能把上一闸
  用掉、又在下游/侧池里的水“重新支配”回上游。回收水只有真正回到可达容器
  后，才出现在该容器的当前水位上。
* 水位未知（歧义水账下各套账库存不一致）时，禁止开任何依赖该库存的阀
  （由 :mod:`bitcatalog.lock` 的相容水账在调度前剔除不可知方案）。

一拍 = 一个原子动作：一格脉冲转移，或一次开/关门。船进闸、出闸各占一拍
（开门-进/出-关门在同一拍内完成，门不会在两拍之间保持敞开）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .lock import (
    CHAMBER,
    DOWNSTREAM,
    GATE_DOWN,
    GATE_UP,
    LockError,
    LockState,
    LockTopology,
    MAKEUP_SOURCE,
    SIDE,
    UPSTREAM,
    VALVE_DOWN,
    VALVE_MAKEUP,
    VALVE_SIDE,
    VALVE_UP,
)

DOWNBOUND = "downbound"  # 上游 -> 闸室 -> 下游
UPBOUND = "upbound"      # 下游 -> 闸室 -> 上游

WAIT_UP = "wait_upstream"
WAIT_DOWN = "wait_downstream"
IN_CHAMBER = "in_chamber"
PASSED = "passed"


class SchedulingError(LockError):
    """不存在满足全部约束的可执行闸序。"""

    def __init__(self, message: str, *, code: str, beat: int | None = None, **detail: object):
        merged = {"beat": beat, **detail}
        super().__init__(message, code=code, **merged)
        self.beat = beat


@dataclass(frozen=True)
class Ship:
    name: str
    direction: str
    arrival_beat: int          # 船抵达引航道、可进闸的最早拍
    latest_pass_beat: int      # 最晚过闸拍（离开闸室的开门拍不得晚于它）
    allowed_valves: frozenset[str]  # 本船过闸期间允许开启的引水支路

    @classmethod
    def create(
        cls,
        name: str,
        direction: str,
        arrival_beat: int,
        latest_pass_beat: int,
        allowed_valves: Sequence[str],
    ) -> "Ship":
        if direction not in (DOWNBOUND, UPBOUND):
            raise SchedulingError(
                f"船 {name!r} 航向必须是 {DOWNBOUND!r} 或 {UPBOUND!r}",
                code="bad_direction",
            )
        if latest_pass_beat < arrival_beat:
            raise SchedulingError(
                f"船 {name!r} 最晚过闸拍早于抵达拍", code="bad_deadline"
            )
        if not allowed_valves:
            raise SchedulingError(f"船 {name!r} 至少要允许一条引水支路", code="no_allowed_valve")
        return cls(
            name=name,
            direction=direction,
            arrival_beat=arrival_beat,
            latest_pass_beat=latest_pass_beat,
            allowed_valves=frozenset(allowed_valves),
        )


@dataclass(frozen=True)
class Transfer:
    valve: str
    src: str
    dst: str
    amount: int = 1


@dataclass(frozen=True)
class BeatAction:
    """一拍内的完整动作记录（水账 + 门位 + 船位变化）。"""

    beat: int
    kind: str  # "transfer" | "gate" | "wait"
    valve_transfers: tuple[Transfer, ...]
    valves_open: tuple[str, ...]
    gates_open: tuple[str, ...]
    gate_events: tuple[tuple[str, str], ...]  # (gate, "open"/"close")
    levels: Mapping[str, int]
    ship_positions: Mapping[str, str]
    note: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            "beat": self.beat,
            "kind": self.kind,
            "valve_transfers": [
                {"valve": t.valve, "from": t.src, "to": t.dst, "amount": t.amount}
                for t in self.valve_transfers
            ],
            "valves_open": list(self.valves_open),
            "gates_open": list(self.gates_open),
            "gate_events": [{"gate": g, "event": e} for g, e in self.gate_events],
            "levels": dict(self.levels),
            "ship_positions": dict(self.ship_positions),
            "note": self.note,
        }


@dataclass(frozen=True)
class LockSchedule:
    topology: LockTopology
    ships: tuple[Ship, ...]
    initial_levels: Mapping[str, int]
    actions: tuple[BeatAction, ...]
    pass_beats: Mapping[str, int]
    valve_usage: Mapping[str, int]  # 每条阀累计过水量（整场水量核对用）

    @property
    def last_beat(self) -> int:
        return self.actions[-1].beat if self.actions else 0

    def as_json(self) -> dict[str, object]:
        return {
            "ships": [
                {
                    "name": ship.name,
                    "direction": ship.direction,
                    "arrival_beat": ship.arrival_beat,
                    "latest_pass_beat": ship.latest_pass_beat,
                    "allowed_valves": sorted(ship.allowed_valves),
                    "pass_beat": self.pass_beats.get(ship.name),
                }
                for ship in self.ships
            ],
            "initial_levels": dict(self.initial_levels),
            "pass_beats": dict(self.pass_beats),
            "valve_usage": dict(self.valve_usage),
            "actions": [action.as_json() for action in self.actions],
        }


# ---------------------------------------------------------------------------
# 单船计划（服务开始时一次性定死，运行中不重算，保证水账追加语义）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ShipPlan:
    entry_gate: str
    exit_gate: str
    target_entry_level: int
    target_exit_level: int
    fill: tuple[Transfer, ...]
    drain: tuple[Transfer, ...]
    side_retention: int

    @property
    def steps(self) -> int:
        """本船所需原子拍数：进水脉冲 + 进闸拍 + 泄水脉冲 + 出闸拍。"""

        return len(self.fill) + 1 + len(self.drain) + 1


@dataclass
class _Runtime:
    ship: Ship
    plan: _ShipPlan
    phase: str = "fill"  # fill -> entry -> drain -> exit -> done
    step_index: int = 0


class LockScheduler:
    """确定性调度器：每拍一个原子动作，物理拒绝带拍号上抛。

    对于本题规模（两船、七格以内），每拍一格的推进方式给出唯一的最早闸序；
    侧池回收优先于上游新水、也优先于泄下游，是唯一需要的策略选择。
    """

    def __init__(self, topology: LockTopology, initial_levels: Mapping[str, int]):
        self.topology = topology
        self.initial_levels = dict(initial_levels)
        self.state = LockState(
            topology=topology,
            levels=dict(initial_levels),
            open_gates=frozenset(),
            open_valves=frozenset(),
        )

    def _plan(self, ship: Ship) -> _ShipPlan:
        # 目标水位取河道恒定标尺（initial_levels 中外部容器的尺读数），
        # 与闸室当前水位无关。
        up_gauge = self.initial_levels[UPSTREAM]
        down_gauge = self.initial_levels[DOWNSTREAM]
        if ship.direction == DOWNBOUND:
            entry_level, exit_level = up_gauge, down_gauge
            entry_gate, exit_gate = GATE_UP, GATE_DOWN
        else:
            entry_level, exit_level = down_gauge, up_gauge
            entry_gate, exit_gate = GATE_DOWN, GATE_UP

        # 在模拟状态上一次性推演两段调整，保证第二段看到的侧池库存/空位
        # 已经包含第一段的转移，与运行时逐拍落账结果一致。
        scratch = self.state
        fill, _ = self._plan_adjust_with_retention(ship, scratch.level(CHAMBER), entry_level, scratch)
        for transfer in fill:
            scratch = scratch.with_levels(
                self._preview_levels(scratch, transfer)
            )
        drain, side_retention = self._plan_adjust_with_retention(
            ship, scratch.level(CHAMBER), exit_level, scratch
        )
        return _ShipPlan(
            entry_gate=entry_gate,
            exit_gate=exit_gate,
            target_entry_level=entry_level,
            target_exit_level=exit_level,
            fill=tuple(fill),
            drain=tuple(drain),
            side_retention=side_retention,
        )

    @staticmethod
    def _preview_levels(state: LockState, transfer: Transfer) -> dict[str, int]:
        """不开阀、只按转移方向推算结果库存（容量/干涸在运行时仍强校验）。"""

        out: dict[str, int] = {}
        if not state.topology.is_infinite(transfer.src):
            out[transfer.src] = state.level(transfer.src) - transfer.amount
        if not state.topology.is_infinite(transfer.dst):
            out[transfer.dst] = state.level(transfer.dst) + transfer.amount
        return out

    def _plan_adjust(self, ship: Ship, current: int, target: int) -> list[Transfer]:
        """闸室 ``current -> target`` 的转移计划（支持抬升与降低）。"""

        transfers, _ = self._plan_adjust_with_retention(ship, current, target, self.state)
        return transfers

    def _plan_adjust_with_retention(
        self,
        ship: Ship,
        current: int,
        target: int,
        state: LockState,
    ) -> tuple[list[Transfer], int]:
        """通用调整：返回 (转移序列, 本阶段净留入侧池格数)。

        抬升时侧池回收水优先、不足走上游/补水；降低时侧池空位优先、不足泄
        下游。所有库存与空位都按传入状态的**真实**水位计算，不按预计值。
        """

        if target == current:
            return [], 0
        transfers: list[Transfer] = []
        side_retention = 0
        if target > current:
            need = target - current
            remaining = need
            if SIDE in state.topology.capacities and VALVE_SIDE in ship.allowed_valves:
                take = min(remaining, state.level(SIDE))  # 只认真实回收水
                transfers.extend(Transfer(VALVE_SIDE, SIDE, CHAMBER) for _ in range(take))
                remaining -= take
            if remaining > 0:
                for valve_name, src in ((VALVE_UP, UPSTREAM), (VALVE_MAKEUP, MAKEUP_SOURCE)):
                    if remaining == 0:
                        break
                    if valve_name not in ship.allowed_valves:
                        continue
                    if valve_name not in {v.name for v in state.topology.valves}:
                        continue
                    transfers.extend(Transfer(valve_name, src, CHAMBER) for _ in range(remaining))
                    remaining = 0
            if remaining > 0:
                side_level = state.levels.get(SIDE)
                raise SchedulingError(
                    f"船 {ship.name!r} 闸室抬升还差 {remaining} 格：允许支路 {sorted(ship.allowed_valves)} "
                    f"在当前真实库存下供不出（侧池现存 {side_level if side_level is not None else '无此池'} 格）",
                    code="insufficient_water",
                )
            return transfers, side_retention

        # target < current：降低闸室，侧池回收优先
        need = current - target
        remaining = need
        if SIDE in state.topology.capacities and VALVE_SIDE in ship.allowed_valves:
            side_free = state.topology.capacity(SIDE) - state.level(SIDE)  # 只认真实空位
            take = min(remaining, side_free)
            transfers.extend(Transfer(VALVE_SIDE, CHAMBER, SIDE) for _ in range(take))
            side_retention = take
            remaining -= take
        if remaining > 0:
            if VALVE_DOWN not in ship.allowed_valves:
                raise SchedulingError(
                    f"船 {ship.name!r} 侧池只能再收 {side_retention} 格，余 {remaining} 格需泄下游，"
                    f"但 {VALVE_DOWN!r} 不在本船允许支路内",
                    code="branch_not_allowed",
                )
            transfers.extend(Transfer(VALVE_DOWN, CHAMBER, DOWNSTREAM) for _ in range(remaining))
        return transfers, side_retention

    # -- 逐拍执行 ---------------------------------------------------------

    def run(self, ships: Sequence[Ship], *, max_beats: int = 64) -> LockSchedule:
        ordered = sorted(ships, key=lambda s: (s.arrival_beat, s.name))
        if [ship.name for ship in ordered] != [ship.name for ship in ships]:
            raise SchedulingError(
                "放船顺序必须与抵达顺序一致；请按 arrival_beat 排序后再提交",
                code="out_of_order",
            )
        positions = {
            ship.name: (WAIT_UP if ship.direction == DOWNBOUND else WAIT_DOWN) for ship in ordered
        }
        actions: list[BeatAction] = []
        pass_beats: dict[str, int] = {}
        valve_usage: dict[str, int] = {}
        runtime: _Runtime | None = None
        cursor = 0
        beat = 0

        while cursor < len(ordered):
            beat += 1
            if beat > max_beats:
                raise SchedulingError(f"超过 {max_beats} 拍仍未完成全部过闸", code="horizon_exceeded", beat=beat)

            if runtime is None:
                ship = ordered[cursor]
                if beat < ship.arrival_beat:
                    actions.append(self._idle(beat, positions, f"等待 {ship.name} 抵达（第 {ship.arrival_beat} 拍到）"))
                    continue
                runtime = _Runtime(ship=ship, plan=self._plan(ship))
                self._assert_feasible(beat, runtime)

            ship = runtime.ship
            transfers: list[Transfer] = []
            gate_events: list[tuple[str, str]] = []
            kind = "transfer"
            note = ""

            # 单分派状态机：一拍严格一个原子动作（一笔脉冲，或一次开门周期）。
            # 空调水段只切相位、不占拍，内层 while 直接推进到本拍真正的动作。
            advanced = False
            while not advanced:
                if runtime.phase == "fill":
                    kind, note, advanced = self._beat_fill(beat, runtime, transfers)
                elif runtime.phase == "entry":
                    kind, note = self._beat_entry(beat, runtime, positions, gate_events)
                    advanced = True
                elif runtime.phase == "drain":
                    kind, note, advanced = self._beat_drain(beat, runtime, transfers)
                else:  # exit
                    kind, note = self._beat_exit(beat, runtime, positions, gate_events, pass_beats)
                    advanced = True
                    if beat > ship.latest_pass_beat:
                        raise SchedulingError(
                            f"船 {ship.name} 实际出闸拍 {beat} 晚于最晚过闸拍 {ship.latest_pass_beat}",
                            code="deadline_missed",
                            beat=beat,
                        )
                    runtime = None
                    cursor += 1

            for transfer in transfers:
                valve_usage[transfer.valve] = valve_usage.get(transfer.valve, 0) + transfer.amount
            actions.append(
                BeatAction(
                    beat=beat,
                    kind=kind,
                    valve_transfers=tuple(transfers),
                    valves_open=tuple(sorted(self.state.open_valves)),
                    gates_open=tuple(sorted(self.state.open_gates)),
                    gate_events=tuple(gate_events),
                    levels=dict(self.state.levels),
                    ship_positions=dict(positions),
                    note=note,
                )
            )

        return LockSchedule(
            topology=self.topology,
            ships=tuple(ordered),
            initial_levels=self.initial_levels,
            actions=tuple(actions),
            pass_beats=pass_beats,
            valve_usage=valve_usage,
        )

    def _beat_fill(
        self, beat: int, runtime: _Runtime, transfers: list[Transfer]
    ) -> tuple[str, str, bool]:
        plan = runtime.plan
        if runtime.step_index >= len(plan.fill):
            runtime.phase, runtime.step_index = "entry", 0
            return "wait", f"{runtime.ship.name} 入口水位已就绪，待命开 {plan.entry_gate}", False
        transfer = plan.fill[runtime.step_index]
        self._do_transfer(beat, transfer)
        transfers.append(transfer)
        runtime.step_index += 1
        note = (
            f"{runtime.ship.name} 进水：{transfer.src} -> {transfer.dst}"
            f"（闸室 {self.state.level(CHAMBER)}/{plan.target_entry_level} 格）"
        )
        if runtime.step_index == len(plan.fill):
            runtime.phase, runtime.step_index = "entry", 0
        return "transfer", note, True

    def _beat_entry(
        self,
        beat: int,
        runtime: _Runtime,
        positions: dict[str, str],
        gate_events: list[tuple[str, str]],
    ) -> tuple[str, str]:
        self._do_gate_cycle(beat, runtime.plan.entry_gate)
        gate_events.extend([(runtime.plan.entry_gate, "open"), (runtime.plan.entry_gate, "close")])
        positions[runtime.ship.name] = IN_CHAMBER
        runtime.phase, runtime.step_index = "drain", 0
        return "gate", f"{runtime.ship.name} 平水开 {runtime.plan.entry_gate} 进闸，随即关门"

    def _beat_drain(
        self, beat: int, runtime: _Runtime, transfers: list[Transfer]
    ) -> tuple[str, str, bool]:
        plan = runtime.plan
        if runtime.step_index >= len(plan.drain):
            runtime.phase, runtime.step_index = "exit", 0
            return "wait", f"{runtime.ship.name} 出口水位已就绪，待命开 {plan.exit_gate}", False
        transfer = plan.drain[runtime.step_index]
        self._do_transfer(beat, transfer)
        transfers.append(transfer)
        runtime.step_index += 1
        note = (
            f"{runtime.ship.name} 在闸调水：{transfer.src} -> {transfer.dst}"
            f"（闸室变至 {self.state.level(CHAMBER)} 格）"
        )
        if runtime.step_index == len(plan.drain):
            runtime.phase, runtime.step_index = "exit", 0
        return "transfer", note, True

    def _beat_exit(
        self,
        beat: int,
        runtime: _Runtime,
        positions: dict[str, str],
        gate_events: list[tuple[str, str]],
        pass_beats: dict[str, int],
    ) -> tuple[str, str]:
        self._do_gate_cycle(beat, runtime.plan.exit_gate)
        gate_events.extend([(runtime.plan.exit_gate, "open"), (runtime.plan.exit_gate, "close")])
        positions[runtime.ship.name] = PASSED
        pass_beats[runtime.ship.name] = beat
        return "gate", f"{runtime.ship.name} 平水开 {runtime.plan.exit_gate} 出闸，本闸结束"

    def _assert_feasible(self, beat: int, runtime: _Runtime) -> None:
        """服务开始时就算出最早出闸拍，来不及立即闭闸并说明理由。"""

        earliest_pass = beat + runtime.plan.steps - 1
        if earliest_pass > runtime.ship.latest_pass_beat:
            raise SchedulingError(
                f"船 {runtime.ship.name} 第 {beat} 拍才能开始服务，本闸需 {runtime.plan.steps} 拍"
                f"（进水 {len(runtime.plan.fill)} + 进闸 1 + 泄水 {len(runtime.plan.drain)} + 出闸 1），"
                f"最早第 {earliest_pass} 拍才能出闸，晚于最晚过闸拍 {runtime.ship.latest_pass_beat}",
                code="deadline_missed",
                beat=beat,
                earliest_pass=earliest_pass,
                latest_pass_beat=runtime.ship.latest_pass_beat,
            )

    def _do_transfer(self, beat: int, transfer: Transfer) -> None:
        try:
            self.state = self.state.open_valve(transfer.valve)
            self.state = self.state.transfer(transfer.valve, transfer.amount, direction=transfer.src)
        except LockError as exc:
            raise SchedulingError(
                f"第 {beat} 拍 {transfer.valve} 引水被拒：{exc}",
                code=exc.code,
                beat=beat,
                **{k: v for k, v in exc.detail.items() if k != "beat"},
            ) from exc
        self.state = self.state.close_valve(transfer.valve)

    def _do_gate_cycle(self, beat: int, gate_name: str) -> None:
        try:
            self.state = self.state.open_gate(gate_name)
        except LockError as exc:
            raise SchedulingError(
                f"第 {beat} 拍 {gate_name} 开门被拒：{exc}",
                code=exc.code,
                beat=beat,
                **{k: v for k, v in exc.detail.items() if k != "beat"},
            ) from exc
        self.state = self.state.close_gate(gate_name)

    def _idle(self, beat: int, positions: Mapping[str, str], note: str) -> BeatAction:
        return BeatAction(
            beat=beat,
            kind="wait",
            valve_transfers=(),
            valves_open=tuple(sorted(self.state.open_valves)),
            gates_open=tuple(sorted(self.state.open_gates)),
            gate_events=(),
            levels=dict(self.state.levels),
            ship_positions=dict(positions),
            note=note,
        )
