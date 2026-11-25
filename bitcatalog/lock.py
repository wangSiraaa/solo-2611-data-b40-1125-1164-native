"""缩尺船闸域模型：整数格容器、闸门/阀门状态机与逐拍脉冲水账。

物理合同（本题）
================

* 闸室与各水池都是**等截面整数格容器**，容量为非负整数格，水位尺读数即
  整数格数；全程忽略渗漏，封闭管系内总水量守恒。
* 阀门连接两个容器，只有开/闭两态。**闸门**位于闸室与上/下游之间，也只有
  开/闭两态。阀门引水期间，其两侧的闸门必须全部关闭。
* 闸门开启的充要条件是**两侧水面相平**，即两侧水位尺读数相等；读数不等时
  开门请求按 :class:`LockError`（``gate_level_mismatch``）拒绝。
* 一个拍内一条开启的阀门最多转移一格水（一个脉冲）。穿过关闭阀门的转移按
  ``valve_closed`` 拒绝；越过容量上/下界的转移按 ``level_out_of_range`` 拒绝。
* 水账是**追加账**：每个已落账脉冲永久改变后续库存，已开闸用掉的水不能在
  下一步骤重新支配。

歧义脉冲
--------

观测端可能只知道某个脉冲“来自公共引水支路、进入 A 池或 B 池之一”，此时不得
把两个池的可能上限相加。:class:`LedgerBook` 枚举每一种全局相容的完整水账
（每个脉冲恰好归属一个候选池），逐套给出各容器逐拍库存，并逐套回答后续放船
可行性——只有相容账支持的方案才算可执行。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product as cartesian
from typing import Iterable, Mapping, Sequence

# 容器名约定
CHAMBER = "chamber"
UPSTREAM = "upstream"
DOWNSTREAM = "downstream"
SIDE = "side"

GATE_UP = "gate_up"      # 上游闸门：upstream <-> chamber
GATE_DOWN = "gate_down"  # 下游闸门：chamber <-> downstream

VALVE_UP = "valve_up"      # 上游引水阀：upstream <-> chamber
VALVE_DOWN = "valve_down"  # 下游泄水阀：chamber <-> downstream
VALVE_SIDE = "valve_side"  # 侧池支路阀：chamber <-> side
VALVE_MAKEUP = "valve_makeup"  # 独立上池补水阀（直通方案）
VALVE_DRAIN = "valve_drain"    # 独立下池排水阀（直通方案）

# 外部水库容器（容量视为无限，水位恒定为河道标尺）
MAKEUP_SOURCE = "makeup_source"
DRAIN_SINK = "drain_sink"

INFINITE_CAPACITY = -1  # capacity 取该值表示外部水库，不设格数上限


class LockError(ValueError):
    """船闸物理规则或水账规则被违反。"""

    def __init__(self, message: str, *, code: str = "lock_error", **detail: object):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# 拓扑与状态
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValveSpec:
    """一条阀门支路：``a <-> b``，每拍最多 ``per_beat`` 格。"""

    name: str
    a: str
    b: str
    per_beat: int = 1

    def pools(self) -> tuple[str, str]:
        return self.a, self.b

    def other(self, pool: str) -> str:
        if pool == self.a:
            return self.b
        if pool == self.b:
            return self.a
        raise LockError(f"{self.name!r} 不连接容器 {pool!r}", code="bad_valve_path", valve=self.name, pool=pool)


@dataclass(frozen=True)
class GateSpec:
    """一道闸门：``chamber <-> reach``，平水（尺读数相等）才准开。"""

    name: str
    chamber_side: str
    reach: str


@dataclass(frozen=True)
class LockTopology:
    """船闸管系：容器容量、阀门支路、闸门。

    ``capacities`` 中 ``INFINITE_CAPACITY`` 表示外部河道/水库，水位由外部
    标尺固定，补水/排水支路只对这种容器或显式声明的侧池开放。
    """

    capacities: Mapping[str, int]
    valves: tuple[ValveSpec, ...]
    gates: tuple[GateSpec, ...]

    def __post_init__(self) -> None:
        for pool, cap in self.capacities.items():
            if cap == INFINITE_CAPACITY:
                continue
            if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
                raise LockError(f"容器 {pool!r} 容量必须是非负整数", code="bad_capacity", pool=pool)
        names: set[str] = set()
        for valve in self.valves:
            if valve.name in names:
                raise LockError(f"阀门重名 {valve.name!r}", code="duplicate_name", name=valve.name)
            names.add(valve.name)
            for pool in valve.pools():
                if pool not in self.capacities:
                    raise LockError(
                        f"阀门 {valve.name!r} 引用了未声明容器 {pool!r}",
                        code="unknown_pool",
                        valve=valve.name,
                        pool=pool,
                    )
            if valve.per_beat < 1:
                raise LockError(
                    f"阀门 {valve.name!r} 每拍能力必须 >= 1", code="bad_valve_capacity", valve=valve.name
                )
        gate_names: set[str] = set()
        for gate in self.gates:
            if gate.name in gate_names:
                raise LockError(f"闸门重名 {gate.name!r}", code="duplicate_name", name=gate.name)
            gate_names.add(gate.name)
            for pool in (gate.chamber_side, gate.reach):
                if pool not in self.capacities:
                    raise LockError(
                        f"闸门 {gate.name!r} 引用了未声明容器 {pool!r}",
                        code="unknown_pool",
                        gate=gate.name,
                        pool=pool,
                    )

    @property
    def pools(self) -> tuple[str, ...]:
        return tuple(self.capacities)

    def valve(self, name: str) -> ValveSpec:
        for valve in self.valves:
            if valve.name == name:
                return valve
        raise LockError(f"未知阀门 {name!r}", code="unknown_valve", valve=name)

    def gate(self, name: str) -> GateSpec:
        for gate in self.gates:
            if gate.name == name:
                return gate
        raise LockError(f"未知闸门 {name!r}", code="unknown_gate", gate=name)

    def capacity(self, pool: str) -> int:
        try:
            return self.capacities[pool]
        except KeyError:
            raise LockError(f"未知容器 {pool!r}", code="unknown_pool", pool=pool) from None

    def is_infinite(self, pool: str) -> bool:
        return self.capacity(pool) == INFINITE_CAPACITY

    def gate_between(self, pool_a: str, pool_b: str) -> GateSpec | None:
        for gate in self.gates:
            if {gate.chamber_side, gate.reach} == {pool_a, pool_b}:
                return gate
        return None

    def gates_touching(self, pool: str) -> tuple[GateSpec, ...]:
        return tuple(
            gate
            for gate in self.gates
            if pool in (gate.chamber_side, gate.reach)
        )


def side_pool_topology(*, chamber: int, side_capacity: int, upstream_level: int = 4) -> LockTopology:
    """标准内河船闸 + 独立容量侧池（封闭回收管系）。

    上/下游是外部河道，标尺恒为 ``upstream_level`` / 0；闸室与侧池是有限
    整数格容器。侧池容量决定一闸水能留住多少。
    """

    return LockTopology(
        capacities={
            CHAMBER: chamber,
            UPSTREAM: INFINITE_CAPACITY,
            DOWNSTREAM: INFINITE_CAPACITY,
            SIDE: side_capacity,
        },
        valves=(
            ValveSpec(VALVE_UP, UPSTREAM, CHAMBER),
            ValveSpec(VALVE_DOWN, CHAMBER, DOWNSTREAM),
            ValveSpec(VALVE_SIDE, CHAMBER, SIDE),
        ),
        gates=(
            GateSpec(GATE_UP, CHAMBER, UPSTREAM),
            GateSpec(GATE_DOWN, CHAMBER, DOWNSTREAM),
        ),
    )


def straight_through_topology(*, chamber: int, makeup_per_beat: int, drain_per_beat: int) -> LockTopology:
    """直通方案：独立上池补水、独立下池排水，无侧池。"""

    return LockTopology(
        capacities={
            CHAMBER: chamber,
            UPSTREAM: INFINITE_CAPACITY,
            DOWNSTREAM: INFINITE_CAPACITY,
            MAKEUP_SOURCE: INFINITE_CAPACITY,
            DRAIN_SINK: INFINITE_CAPACITY,
        },
        valves=(
            ValveSpec(VALVE_UP, UPSTREAM, CHAMBER),
            ValveSpec(VALVE_DOWN, CHAMBER, DOWNSTREAM),
            replace(ValveSpec(VALVE_MAKEUP, MAKEUP_SOURCE, CHAMBER), per_beat=makeup_per_beat),
            replace(ValveSpec(VALVE_DRAIN, CHAMBER, DRAIN_SINK), per_beat=drain_per_beat),
        ),
        gates=(
            GateSpec(GATE_UP, CHAMBER, UPSTREAM),
            GateSpec(GATE_DOWN, CHAMBER, DOWNSTREAM),
        ),
    )


@dataclass(frozen=True)
class LockState:
    """某一拍开始（或结束）时整个管系的可观测状态。"""

    topology: LockTopology
    levels: Mapping[str, int]
    open_gates: frozenset[str]
    open_valves: frozenset[str]

    def __post_init__(self) -> None:
        for pool in self.topology.pools:
            level = self.levels.get(pool)
            if level is None:
                raise LockError(f"容器 {pool!r} 缺少初始水位", code="missing_level", pool=pool)
            if not isinstance(level, int) or isinstance(level, bool) or level < 0:
                raise LockError(f"容器 {pool!r} 水位必须是非负整数", code="bad_level", pool=pool)
            cap = self.topology.capacity(pool)
            if cap != INFINITE_CAPACITY and level > cap:
                raise LockError(
                    f"容器 {pool!r} 水位 {level} 超出容量 {cap}",
                    code="level_out_of_range",
                    pool=pool,
                    level=level,
                    capacity=cap,
                )
        for name in self.open_gates:
            self.topology.gate(name)
        for name in self.open_valves:
            self.topology.valve(name)

    def level(self, pool: str) -> int:
        return self.levels[pool]

    def gate_is_open(self, name: str) -> bool:
        return name in self.open_gates

    def valve_is_open(self, name: str) -> bool:
        return name in self.open_valves

    def with_levels(self, levels: Mapping[str, int]) -> "LockState":
        merged = dict(self.levels)
        merged.update(levels)
        return replace(self, levels=merged)

    def with_valves(self, opened: Iterable[str] = (), closed: Iterable[str] = ()) -> "LockState":
        names = set(self.open_valves)
        for name in opened:
            self.topology.valve(name)
            names.add(name)
        for name in closed:
            names.discard(name)
        return replace(self, open_valves=frozenset(names))

    def with_gates(self, opened: Iterable[str] = (), closed: Iterable[str] = ()) -> "LockState":
        names = set(self.open_gates)
        for name in opened:
            self.topology.gate(name)
            names.add(name)
        for name in closed:
            names.discard(name)
        return replace(self, open_gates=frozenset(names))

    # -- 物理规则校验 -----------------------------------------------------

    def _assert_valve_safe(self, valve: ValveSpec) -> None:
        """阀门引水期间，其两侧容器所触及的闸门必须全部关闭。"""

        for pool in valve.pools():
            for gate in self.topology.gates_touching(pool):
                if self.gate_is_open(gate.name):
                    raise LockError(
                        f"阀门 {valve.name!r} 引水时闸门 {gate.name!r} 必须关闭",
                        code="gate_open_blocks_valve",
                        valve=valve.name,
                        gate=gate.name,
                    )

    def open_valve(self, name: str) -> "LockState":
        valve = self.topology.valve(name)
        self._assert_valve_safe(valve)
        return self.with_valves(opened=(name,))

    def close_valve(self, name: str) -> "LockState":
        return self.with_valves(closed=(name,))

    def open_gate(self, name: str) -> "LockState":
        gate = self.topology.gate(name)
        left, right = self.level(gate.chamber_side), self.level(gate.reach)
        if left != right:
            raise LockError(
                f"闸门 {name!r} 两侧水位尺不平：{gate.chamber_side}={left} 格，{gate.reach}={right} 格",
                code="gate_level_mismatch",
                gate=name,
                left=left,
                right=right,
            )
        # 平水后开门，任何仍开启的引水阀都必须先关闭（门开即连通）。
        blocking = [
            valve.name
            for valve in self.topology.valves
            if self.valve_is_open(valve.name)
            and gate.chamber_side in valve.pools()
        ]
        if blocking:
            raise LockError(
                f"开闸 {name!r} 前必须先关阀 {blocking!r}",
                code="valve_open_blocks_gate",
                gate=name,
                valves=tuple(blocking),
            )
        return self.with_gates(opened=(name,))

    def close_gate(self, name: str) -> "LockState":
        return self.with_gates(closed=(name,))

    def transfer(self, valve_name: str, amount: int = 1, *, direction: str | None = None) -> "LockState":
        """沿开启阀门转移整数格水；关闭阀、越界、超每拍能力一律拒绝。"""

        valve = self.topology.valve(valve_name)
        if not self.valve_is_open(valve_name):
            raise LockError(
                f"拒绝穿过关闭阀门 {valve_name!r} 的转移",
                code="valve_closed",
                valve=valve_name,
            )
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise LockError("转移量必须是正整数", code="bad_transfer_amount", amount=amount)
        if amount > valve.per_beat:
            raise LockError(
                f"阀门 {valve_name!r} 每拍最多 {valve.per_beat} 格，本次请求 {amount} 格",
                code="transfer_over_capacity",
                valve=valve_name,
                requested=amount,
                per_beat=valve.per_beat,
            )
        self._assert_valve_safe(valve)
        src, dst = valve.pools()
        if direction is not None:
            pair = {src, dst}
            if direction not in pair:
                raise LockError(
                    f"阀门 {valve_name!r} 无法通向 {direction!r}",
                    code="bad_valve_path",
                    valve=valve_name,
                    pool=direction,
                )
            src, dst = direction, next(iter(pair - {direction}))
        src_level, dst_level = self.level(src), self.level(dst)
        if not self.topology.is_infinite(src) and src_level < amount:
            raise LockError(
                f"容器 {src!r} 仅剩 {src_level} 格，无法支出 {amount} 格",
                code="source_dry",
                pool=src,
                level=src_level,
                requested=amount,
            )
        dst_cap = self.topology.capacity(dst)
        if dst_cap != INFINITE_CAPACITY and dst_level + amount > dst_cap:
            raise LockError(
                f"容器 {dst!r} 进水 {amount} 格后为 {dst_level + amount}，超出容量 {dst_cap}",
                code="level_out_of_range",
                pool=dst,
                level=dst_level + amount,
                capacity=dst_cap,
            )
        # 外部河道/水库（无限容量）是恒定标尺：只对侧记账，自身尺读数不动。
        new_levels: dict[str, int] = {}
        if not self.topology.is_infinite(src):
            new_levels[src] = src_level - amount
        if dst_cap != INFINITE_CAPACITY:
            new_levels[dst] = dst_level + amount
        return self.with_levels(new_levels)

    def finite_total(self) -> int:
        """封闭管系（不含外部水库）内的总水量，守恒检查用。"""

        return sum(
            level
            for pool, level in self.levels.items()
            if not self.topology.is_infinite(pool)
        )


# ---------------------------------------------------------------------------
# 脉冲观测与相容水账
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PulseObs:
    """一个观测脉冲：``amount`` 格水从 ``src`` 经公共支管进入候选池之一。

    ``candidates`` 只有一个元素时是确定脉冲；多个元素表示观测端只知道脉冲
    进入这些池中的某一个（例如公共引水支管后的去向不明）。枚举水账时每个
    脉冲必须恰好选定一个候选池，不允许把候选池上限相加。

    ``manifold`` 给出公共支路上物理连通的全部池（含源池与各候选池）。缺省
    取单阀 ``valve`` 的两端；歧义脉冲必须显式给出支管，否则当某候选池不在
    该阀两端时，这套归属会因物理不通而被剔除（这正是“去向不明”需要支管
    模型的原因）。
    """

    beat: int
    valve: str
    src: str
    amount: int
    candidates: tuple[str, ...]
    manifold: tuple[str, ...] = ()

    @classmethod
    def observed(
        cls,
        beat: int,
        valve: str,
        src: str,
        amount: int,
        candidates: str | Sequence[str],
        *,
        manifold: Sequence[str] | None = None,
    ) -> "PulseObs":
        if isinstance(candidates, str):
            candidates = (candidates,)
        cand = tuple(candidates)
        if not cand:
            raise LockError("脉冲至少要有一个候选池", code="empty_candidates")
        if len(set(cand)) != len(cand):
            raise LockError(f"脉冲候选池重复 {cand!r}", code="duplicate_candidate")
        mani = tuple(manifold) if manifold is not None else ()
        return cls(beat=beat, valve=valve, src=src, amount=amount, candidates=cand, manifold=mani)

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1

    def reachable_pools(self, topology: LockTopology) -> frozenset[str]:
        """该脉冲物理上可到达的池集合：显式支管优先，否则取阀门两端。"""

        if self.manifold:
            unknown = [pool for pool in self.manifold if pool not in topology.capacities]
            if unknown:
                raise LockError(
                    f"公共支管引用了未声明容器 {unknown!r}",
                    code="unknown_pool",
                    pools=tuple(unknown),
                )
            return frozenset(self.manifold)
        return frozenset(topology.valve(self.valve).pools())


@dataclass(frozen=True)
class LedgerEntry:
    """水账上的一行：某拍某阀的一笔已落账转移。"""

    beat: int
    valve: str
    src: str
    dst: str
    amount: int
    ambiguous_pulse: bool = False

    def as_json(self) -> dict[str, object]:
        return {
            "beat": self.beat,
            "valve": self.valve,
            "from": self.src,
            "to": self.dst,
            "amount": self.amount,
            "ambiguous_pulse": self.ambiguous_pulse,
        }


@dataclass(frozen=True)
class LedgerBook:
    """从同一组脉冲观测重建出的一整套相容水账。

    每个 :class:`LedgerBook` 对应一种全局归属选择；歧义脉冲有 *k* 个候选、
    共 *n* 个歧义脉冲时，相容账最多 *k^n* 套，逐套独立核算库存。
    """

    topology: LockTopology
    initial_levels: Mapping[str, int]
    entries: tuple[LedgerEntry, ...]
    levels_by_beat: Mapping[int, Mapping[str, int]]

    def level_at(self, beat: int, pool: str) -> int:
        return self.levels_by_beat[beat][pool]

    def finite_total(self) -> int:
        final = max(self.levels_by_beat)
        return sum(
            level
            for pool, level in self.levels_by_beat[final].items()
            if not self.topology.is_infinite(pool)
        )

    def as_json(self) -> dict[str, object]:
        return {
            "entries": [entry.as_json() for entry in self.entries],
            "levels_by_beat": {
                str(beat): dict(levels) for beat, levels in sorted(self.levels_by_beat.items())
            },
            "finite_total": self.finite_total(),
        }


def _check_transfer_rules(
    topology: LockTopology,
    levels: dict[str, int],
    valve_open: Mapping[str, bool],
    entry: LedgerEntry,
    *,
    reachable: frozenset[str] | None = None,
) -> None:
    """落账前按物理规则复核一笔转移（关闭阀/越界/超每拍能力）。

    ``reachable`` 给定时按公共支管的可达集校验连通性（用于歧义脉冲）；
    否则严格要求转移两端就是观测阀门的两端。
    """

    valve = topology.valve(entry.valve)
    if not valve_open.get(entry.valve, False):
        raise LockError(
            f"拍 {entry.beat}：拒绝穿过关闭阀门 {entry.valve!r} 的转移",
            code="valve_closed",
            valve=entry.valve,
            beat=entry.beat,
        )
    if entry.amount > valve.per_beat:
        raise LockError(
            f"拍 {entry.beat}：阀门 {entry.valve!r} 每拍能力 {valve.per_beat} 格，请求 {entry.amount} 格",
            code="transfer_over_capacity",
            valve=entry.valve,
            beat=entry.beat,
        )
    connected = reachable if reachable is not None else frozenset((valve.a, valve.b))
    if entry.src not in connected or entry.dst not in connected:
        raise LockError(
            f"拍 {entry.beat}：公共支管/阀门 {entry.valve!r} 不连通 {entry.src!r}->{entry.dst!r}",
            code="bad_valve_path",
            valve=entry.valve,
            beat=entry.beat,
        )
    src_infinite = topology.is_infinite(entry.src)
    if not src_infinite and levels[entry.src] - entry.amount < 0:
        raise LockError(
            f"拍 {entry.beat}：容器 {entry.src!r} 库存不足（剩 {levels[entry.src]}，请求 {entry.amount}）",
            code="source_dry",
            pool=entry.src,
            beat=entry.beat,
        )
    dst_level = levels[entry.dst] + entry.amount
    cap = topology.capacity(entry.dst)
    if cap != INFINITE_CAPACITY and dst_level > cap:
        raise LockError(
            f"拍 {entry.beat}：容器 {entry.dst!r} 进水后 {dst_level} 超出容量 {cap}",
            code="level_out_of_range",
            pool=entry.dst,
            beat=entry.beat,
        )
    # 无限容器（外部河道）恒定标尺，只更新有限容器。
    if not src_infinite:
        levels[entry.src] -= entry.amount
    if cap != INFINITE_CAPACITY:
        levels[entry.dst] = dst_level


def reconcile_pulses(
    topology: LockTopology,
    initial_levels: Mapping[str, int],
    pulses: Sequence[PulseObs],
    valve_open_by_beat: Mapping[int, Mapping[str, bool]],
) -> tuple[LedgerBook, ...]:
    """枚举全部相容水账。

    逐脉冲做候选笛卡尔积，但每套账都从同一起始量独立复算全流程；任何一套
    账出现关闭阀转移、容器抽空或溢出，该套账直接剔除。返回的每套账都是
    **完整**水账（含逐拍库存），绝不把两个候选池的可能上限相加。
    """

    # 起始量本身先做容量校验
    LockState(
        topology=topology,
        levels=dict(initial_levels),
        open_gates=frozenset(),
        open_valves=frozenset(),
    )
    ordered = sorted(pulses, key=lambda pulse: (pulse.beat, pulse.valve))
    # 一次性物理可达校验：源池与每个候选池都必须在该脉冲的支管/阀门可达集内。
    for pulse in ordered:
        reachable = pulse.reachable_pools(topology)
        unreachable = [pool for pool in pulse.candidates if pool not in reachable]
        if unreachable or pulse.src not in reachable:
            raise LockError(
                f"拍 {pulse.beat} 脉冲候选 {pulse.candidates!r} 不在支管可达集 {sorted(reachable)!r} 内",
                code="bad_valve_path",
                valve=pulse.valve,
                beat=pulse.beat,
            )
    choices = [pulse.candidates for pulse in ordered]
    books: list[LedgerBook] = []
    for assignment in cartesian(*choices):
        levels = {pool: initial_levels.get(pool, 0) for pool in topology.pools}
        entries: list[LedgerEntry] = []
        levels_by_beat: dict[int, dict[str, int]] = {}
        # 同一拍可能多笔脉冲；逐拍快照在该拍全部脉冲落账后记录。
        beats = sorted({pulse.beat for pulse in ordered})
        per_beat: dict[int, list[tuple[PulseObs, str]]] = {beat: [] for beat in beats}
        for pulse, dst in zip(ordered, assignment):
            per_beat[pulse.beat].append((pulse, dst))
        rejected: LockError | None = None
        for beat in beats:
            valve_open = valve_open_by_beat.get(beat, {})
            for pulse, dst in per_beat[beat]:
                entry = LedgerEntry(
                    beat=beat,
                    valve=pulse.valve,
                    src=pulse.src,
                    dst=dst,
                    amount=pulse.amount,
                    ambiguous_pulse=pulse.ambiguous,
                )
                try:
                    _check_transfer_rules(
                        topology,
                        levels,
                        valve_open,
                        entry,
                        reachable=pulse.reachable_pools(topology),
                    )
                except LockError as exc:
                    rejected = exc
                    break
                entries.append(entry)
            if rejected is not None:
                break
            levels_by_beat[beat] = dict(levels)
        if rejected is None:
            books.append(
                LedgerBook(
                    topology=topology,
                    initial_levels=dict(initial_levels),
                    entries=tuple(entries),
                    levels_by_beat=levels_by_beat,
                )
            )
    return tuple(books)


def pool_available(books: Sequence[LedgerBook], beat: int, pool: str) -> int | None:
    """某池在某拍后，在**每套**相容账中都成立的库存量（公共下界）。

    若各套账库存不一致，返回 ``None`` 提醒调用方：库存取决于归属，不能凭
    预计库存开阀。注意这里取的是逐套账的最小值，而不是候选上限之和。
    """

    if not books:
        return None
    values = {book.level_at(beat, pool) for book in books}
    if len(values) == 1:
        return next(iter(values))
    return None
