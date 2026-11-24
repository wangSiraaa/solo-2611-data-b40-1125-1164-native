"""两晚排班求解：在字粒、机器、字盒、排版台约束下求一份可兑现的做法。

资源规则（与工坊约定逐条对应）：

1. 同一块版占着的字不能出现在别台机器上——同一夜班内一个字粒最多被一张票
   占用；封版（seal）的字两晚都借不出，两晚全程占用。
2. 封版路径：可提前封好留到最后一印，交班人直接开机；不耗排版台时段、
   开印时不开盒；但字粒在两个夜班都被锁死。
3. 现场拆版路径（recompose）：开印前拆旧版、现场压样后配版——
   - 占一次当班排版台时段；
   - 用旧版拆下的字不要求开盒（旧版本就占着这些字，拆字是车间内部动作）；
   - 从盒里新取的字只能来自当班有权拆开的字盒；
   - 字粒只在当班占用，次晚释放。
4. 一台机器一个夜班只印一块版；封版印刷同样占机器。
5. 只有身份推理能“有凭据交付”的字粒（看清观察唯一确定，或整盒数量唯一逼出）
   才能配给字位；矛盾粒一律不用。

求解按订单输入顺序逐票 DFS，优先接受早提交的票，每张票内 recompose 优先于
seal，位置按 slots 顺序、字粒按字典序枚举，整棵树确定性可重跑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .identity import IdentityReport
from .model import (
    Job,
    Order,
    Position,
    PressError,
    ROUTE_RECOMPOSE,
    ROUTE_SEAL,
    SealedForm,
)


class PressInventoryError(PressError):
    """静态库存（旧版/封版引用）与压样台账对不上。"""

# 拒接原因码（领班凭据上写的结论）。
REASON_NO_PIECE = "no_eligible_piece"          # 没有任何字粒能有凭据地满足字位
REASON_PIECE_BUSY = "piece_busy"               # 能识别的字粒两晚都被占/被封
REASON_MACHINE_BUSY = "machine_busy"           # 该晚机器已被别的版占用
REASON_BOX_CLOSED = "box_not_open_that_night"  # 候选字盒当班无权拆开
REASON_NO_TABLE_SLOT = "no_composing_table_slot"  # 排版台时段已排满
REASON_ROUTE_FORBIDDEN = "route_not_allowed"   # 订单限定的路径均走不通
REASON_BOX_INFEASIBLE = "box_ledger_infeasible"   # 整盒数量记录无法兑现


@dataclass
class PositionPlan:
    slot: str
    face: str
    piece_id: str
    box_id: str | None
    from_old_form: str | None      # 字粒来自哪块旧版（拆版路径专用）


@dataclass
class OrderDecision:
    order_id: str
    accepted: bool
    night: int
    machine: str
    route: str | None
    positions: list[PositionPlan]
    reason_code: str | None
    reason: str
    source_file: str
    source_row: int


@dataclass
class _State:
    # 每晚被占用的字粒：seal 两晚都放进去；recompose 只放当班。
    busy: list[set[str]]
    machines: list[dict[str, str]]   # 晚 -> 机器 -> 占用它的订单/封版 id
    table_used: list[int]
    available_old_pieces: dict[str, str]   # 可拆旧版字粒 -> 旧版 id
    broken_forms: set[str]
    # 当晚从哪块旧版拆下了哪粒字：晚 -> 字粒 -> 旧版（台账凭据，回滚时同步撤销）。
    broken_sources: list[dict[str, str]]


@dataclass
class ScheduleResult:
    decisions: list[OrderDecision]
    sealed_forms: list[SealedForm]
    table_slots: dict[int, int]
    open_boxes: dict[int, frozenset[str]]
    ledger: list[dict[str, Any]] = field(default_factory=list)


class Scheduler:
    def __init__(self, job: Job, identity: IdentityReport, orders: list[Order]):
        self.job = job
        self.identity = identity
        self.orders = orders
        self._validate_inventory()

    # ------------------------------------------------------------------
    # 前置校验
    # ------------------------------------------------------------------
    def _validate_inventory(self) -> None:
        known_pieces = set(self.identity.pieces)
        seen_old: dict[str, str] = {}
        for old in self.job.old_forms:
            for pid in old.pieces:
                if pid not in known_pieces:
                    raise PressInventoryError(f"旧版 {old.form_id} 引用了压样中不存在的字粒 {pid}")
                if pid in seen_old:
                    raise PressInventoryError(
                        f"字粒 {pid} 同时挂在旧版 {seen_old[pid]} 与 {old.form_id} 上，库存记录冲突"
                    )
                seen_old[pid] = old.form_id
        seen_sealed: set[str] = set()
        for sf in self.job.sealed_forms:
            for pid in sf.pieces:
                if pid not in known_pieces:
                    raise PressInventoryError(f"封版 {sf.form_id} 引用了压样中不存在的字粒 {pid}")
                if pid in seen_sealed:
                    raise PressInventoryError(f"字粒 {pid} 被多个封版重复占用")
                seen_sealed.add(pid)
        # 封版自身的字粒字面必须可交付，否则这份封版安排本身就不成立。
        for sf in self.job.sealed_forms:
            for pid in sf.pieces:
                if self.identity.committable_face(pid) is None:
                    raise PressInventoryError(
                        f"封版 {sf.form_id} 的字粒 {pid} 身份无法有凭据确认，不能提前封版"
                    )

    def _initial_state(self) -> _State:
        busy: list[set[str]] = [set(), set()]
        machines: list[dict[str, str]] = [{}, {}]
        for sf in self.job.sealed_forms:
            for pid in sf.pieces:
                busy[0].add(pid)
                busy[1].add(pid)
            machines[sf.print_night][sf.machine] = f"sealed:{sf.form_id}"
        old_pieces: dict[str, str] = {}
        for old in self.job.old_forms:
            for pid in old.pieces:
                old_pieces[pid] = old.form_id
        return _State(
            busy=busy,
            machines=machines,
            table_used=[0, 0],
            available_old_pieces=old_pieces,
            broken_forms=set(),
            broken_sources=[{}, {}],
        )

    # ------------------------------------------------------------------
    # 字位候选
    # ------------------------------------------------------------------
    def _eligible_pieces(self, position: Position, order: Order) -> list[str]:
        """能有凭据地满足该字位的全部字粒（未叠加当晚占用）。

        盒台账不可兑现时，该盒的“整盒数量排除”凭据失效，没有任何字粒可交付，
        并由调用方据此登记 box_ledger_infeasible 拒因。
        """
        out: list[str] = []
        for pid in sorted(self.identity.pieces):
            ob = self.identity.pieces[pid]
            if order.source_box and ob.box_id != order.source_box:
                continue
            if not self.identity.boxes[ob.box_id].feasible:
                continue
            if self.identity.committable_face(pid) != position.face:
                continue
            out.append(pid)
        return out

    def _has_infeasible_candidate_box(self, order: Order) -> bool:
        """该票来源盒（不限盒则全部盒）中是否存在不可兑现的盒台账。"""
        for box_id, ledger in self.identity.boxes.items():
            if order.source_box and box_id != order.source_box:
                continue
            if not ledger.feasible:
                return True
        return False

    def _box_open(self, box_id: str, night: int) -> bool:
        return box_id in self.job.open_boxes[night]

    def _piece_options(
        self, position: Position, order: Order, route: str, state: _State
    ) -> tuple[list[PositionPlan], set[str]]:
        """枚举某字位在指定路径下当前可用的配字方案；同时返回触发的拒绝码集合。"""
        night = order.night
        options: list[PositionPlan] = []
        blockers: set[str] = set()
        for pid in self._eligible_pieces(position, order):
            if pid in state.busy[night]:
                blockers.add(REASON_PIECE_BUSY)
                continue
            box_id = self.identity.pieces[pid].box_id
            old_form = state.available_old_pieces.get(pid)
            if route == ROUTE_RECOMPOSE:
                # 旧版拆下的字免开盒；盒取字要求当班有权拆该盒。
                if old_form is None and not self._box_open(box_id, night):
                    blockers.add(REASON_BOX_CLOSED)
                    continue
                options.append(
                    PositionPlan(
                        slot=position.slot, face=position.face, piece_id=pid,
                        box_id=None if old_form else box_id, from_old_form=old_form,
                    )
                )
            else:  # seal：封版只封盒字，且两个夜班都要空得出
                if old_form is not None:
                    # 旧版上的字已被旧版占着，不能再拿去提前封新版。
                    blockers.add(REASON_PIECE_BUSY)
                    continue
                if pid in state.busy[1 - night]:
                    blockers.add(REASON_PIECE_BUSY)
                    continue
                options.append(
                    PositionPlan(
                        slot=position.slot, face=position.face, piece_id=pid,
                        box_id=box_id, from_old_form=None,
                    )
                )
        if not options:
            if not self._eligible_pieces(position, order):
                blockers.add(REASON_NO_PIECE)
            return options, blockers
        # 确定性顺序：先拆旧版（免开盒），再按盒/字粒号开盒取字。
        options.sort(key=lambda pp: (pp.from_old_form is None, pp.box_id or "", pp.piece_id))
        return options, blockers

    # ------------------------------------------------------------------
    # 单票求解
    # ------------------------------------------------------------------
    def _try_order(
        self, order: Order, state: _State
    ) -> tuple[list[tuple[str, list[PositionPlan]]], set[str]]:
        """返回该票所有可行 (路径, 逐字位方案) 组合，以及失败原因码集合。"""
        night = order.night
        blockers: set[str] = set()
        feasible: list[tuple[str, list[PositionPlan]]] = []

        if state.machines[night].get(order.machine) is not None:
            blockers.add(REASON_MACHINE_BUSY)
            return feasible, blockers

        routes = [r for r in (ROUTE_RECOMPOSE, ROUTE_SEAL) if r in order.allowed_routes]
        if not routes:
            blockers.add(REASON_ROUTE_FORBIDDEN)
            return feasible, blockers

        if self._has_infeasible_candidate_box(order):
            blockers.add(REASON_BOX_INFEASIBLE)

        for route in routes:
            if route == ROUTE_RECOMPOSE and state.table_used[night] >= self.job.table_slots[night]:
                blockers.add(REASON_NO_TABLE_SLOT)
                continue

            per_position: list[list[PositionPlan]] = []
            route_ok = True
            for position in order.positions:
                options, pos_blockers = self._piece_options(position, order, route, state)
                blockers |= pos_blockers
                if not options:
                    route_ok = False
                    break
                per_position.append(options)
            if not route_ok:
                continue

            # 一张票内部不同字位不能拿同一粒字；DFS 配字。
            chosen: list[PositionPlan] = []
            used: set[str] = set()

            def assign(idx: int) -> bool:
                if idx == len(per_position):
                    return True
                for opt in per_position[idx]:
                    if opt.piece_id in used:
                        continue
                    chosen.append(opt)
                    used.add(opt.piece_id)
                    if assign(idx + 1):
                        return True
                    used.discard(opt.piece_id)
                    chosen.pop()
                return False

            if assign(0):
                feasible.append((route, list(chosen)))
        return feasible, blockers

    def _apply(self, order: Order, route: str, plans: list[PositionPlan], state: _State) -> None:
        night = order.night
        state.machines[night][order.machine] = order.order_id
        for pp in plans:
            pid = pp.piece_id
            state.busy[night].add(pid)
            if route == ROUTE_SEAL:
                state.busy[1 - night].add(pid)
            if pp.from_old_form:
                state.broken_forms.add(pp.from_old_form)
                # 拆下后该粒成为本晚松散字；旧版记录不再占用它。
                state.available_old_pieces.pop(pid, None)
                state.broken_sources[night][pid] = pp.from_old_form
        if route == ROUTE_RECOMPOSE:
            state.table_used[night] += 1

    def _rollback(self, order: Order, route: str, plans: list[PositionPlan], state: _State) -> None:
        night = order.night
        state.machines[night].pop(order.machine, None)
        for pp in plans:
            pid = pp.piece_id
            state.busy[night].discard(pid)
            if route == ROUTE_SEAL:
                state.busy[1 - night].discard(pid)
            if pp.from_old_form:
                state.available_old_pieces[pid] = pp.from_old_form
                state.broken_sources[night].pop(pid, None)
                if pp.from_old_form not in state.broken_sources[night].values():
                    state.broken_forms.discard(pp.from_old_form)
        if route == ROUTE_RECOMPOSE:
            state.table_used[night] -= 1

    # ------------------------------------------------------------------
    # 总求解
    # ------------------------------------------------------------------
    def solve(self) -> ScheduleResult:
        state = self._initial_state()
        decisions: list[OrderDecision] = []

        def go(idx: int) -> bool:
            if idx == len(self.orders):
                return True
            order = self.orders[idx]
            feasible, blockers = self._try_order(order, state)
            if not feasible:
                decisions.append(self._refuse(order, blockers))
                if go(idx + 1):
                    return True
                decisions.pop()
                return False
            for route, plans in feasible:
                self._apply(order, route, plans, state)
                decisions.append(self._accept(order, route, plans))
                if go(idx + 1):
                    return True
                decisions.pop()
                self._rollback(order, route, plans, state)
            return False

        go(0)
        ledger = self._build_ledger(state)
        return ScheduleResult(
            decisions=decisions,
            sealed_forms=list(self.job.sealed_forms),
            table_slots=dict(self.job.table_slots),
            open_boxes={n: frozenset(s) for n, s in self.job.open_boxes.items()},
            ledger=ledger,
        )

    # ------------------------------------------------------------------
    # 凭据文案
    # ------------------------------------------------------------------
    def _accept(self, order: Order, route: str, plans: list[PositionPlan]) -> OrderDecision:
        if route == ROUTE_SEAL:
            text = (
                f"接单：提前封版留到第 {order.night + 1} 夜班开机，{order.machine} 直接印；"
                f"封住的字两晚均借不出，不耗排版台、不开盒"
            )
        else:
            boxes = sorted({pp.box_id for pp in plans if pp.box_id})
            olds = sorted({pp.from_old_form for pp in plans if pp.from_old_form})
            bits = [f"接单：第 {order.night + 1} 夜班现场配版，占 1 个排版台时段（机器 {order.machine}）"]
            if olds:
                bits.append(f"拆旧版 {', '.join(olds)} 取字（免开盒）")
            if boxes:
                bits.append(f"从当班可开盒 {', '.join(boxes)} 取字")
            text = "；".join(bits)
        return OrderDecision(
            order_id=order.order_id, accepted=True, night=order.night, machine=order.machine,
            route=route, positions=plans, reason_code=None, reason=text,
            source_file=order.source_file, source_row=order.source_row,
        )

    def _refuse(self, order: Order, blockers: set[str]) -> OrderDecision:
        priority = [
            (REASON_NO_PIECE, "没有任何字粒能有凭据地满足该字位（身份不足或观察矛盾）"),
            (REASON_BOX_INFEASIBLE, "整盒铸字数量记录无法兑现，不能据此排除分配"),
            (REASON_MACHINE_BUSY, f"第 {order.night + 1} 夜班机器 {order.machine} 已被别的版占用"),
            (REASON_PIECE_BUSY, "能识别的字粒该晚（封版则两晚）均已被别的版占住"),
            (REASON_BOX_CLOSED, "候选字盒在当班无权拆开，旧版上又没有现成字"),
            (REASON_NO_TABLE_SLOT, f"第 {order.night + 1} 夜班排版台时段已排满，无法现场配版"),
            (REASON_ROUTE_FORBIDDEN, "订单允许的兑现路径均不可用"),
        ]
        for code, text in priority:
            if code in blockers:
                reason_code, reason = code, f"拒接：{text}"
                break
        else:
            reason_code, reason = "unsatisfied", "拒接：当前资源下无可行配版"
        return OrderDecision(
            order_id=order.order_id, accepted=False, night=order.night, machine=order.machine,
            route=None, positions=[], reason_code=reason_code, reason=reason,
            source_file=order.source_file, source_row=order.source_row,
        )

    def _build_ledger(self, state: _State) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for night in (0, 1):
            broken = state.broken_sources[night]
            rows.append({
                "night": night,
                "table_slots_total": self.job.table_slots[night],
                "table_slots_used": state.table_used[night],
                "machines_busy": dict(state.machines[night]),
                "pieces_busy": sorted(state.busy[night]),
                "open_boxes": sorted(self.job.open_boxes[night]),
                "old_forms_broken": sorted(set(broken.values())),
                "broken_pieces": [
                    {"piece_id": pid, "from_old_form": broken[pid]}
                    for pid in sorted(broken)
                ],
            })
        return rows
