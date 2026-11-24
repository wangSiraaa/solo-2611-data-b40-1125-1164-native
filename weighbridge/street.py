"""腾街单受理、证据重建与岔口回放。

这个模块是过磅事实仓之上的街道业务层。它只相信两类事实：

1. ``Warehouse`` 中状态为 ``CONFIRMED`` 的运输评估，给出每趟卸雪净重区间；
2. 现场签条：初始/清扫前后堆量、分段装车见证、转堆签条、邻队落堆和观测时刻。

关键约束在模型里显式成立：

* 转堆同时做源段减、目标段加，因此搬了但仍在街上的雪不会变成外运；
* 同一车次的所有分段装车量之和必须落在确认净重区间内，净重不会被两段各领一次；
* 腾街结论按所有可行整数世界取最坏值，只有每个世界都低于余雪线才受理；
* 禁倒权必须有可实际签收的替代堆位；岔口观测必须在车过岔口前到达，旧预计表
  不能充当现场观测。

模块不依赖第三方库。
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from .warehouse import STATUS_CONFIRMED, Warehouse

# ---------------------------------------------------------------------------
# 常量与错误
# ---------------------------------------------------------------------------

STREET_RELEASE = "RELEASE"
STORAGE = "STORAGE"
NO_DUMP = "NO_DUMP"
SLOT_RENTAL = "SLOT_RENTAL"

STATUS_ACCEPTED = "ACCEPTED"
STATUS_SIGNED = "SIGNED"
STATUS_RECORDED = "RECORDED"
STATUS_REJECTED = "REJECTED"
STATUS_REVOKED = "REVOKED"

METHOD_AUTO = "AUTO"
METHOD_BAN = "BAN_REDIRECT"
METHOD_OBSERVE = "OBSERVED_FORK"
METHOD_FIXED = "FIXED_ROUTE"
METHOD_NONE = "NONE"

BEFORE_FORK = "BEFORE_FORK"
AFTER_FORK = "AFTER_FORK"

# 小规模桌面例子使用整数 kg。证据区间过宽时不假装穷举了全部世界。
MAX_ENUMERATED_ASSIGNMENTS = 250_000
DEFAULT_BOUND_LIMIT = 1_000_000


class StreetError(ValueError):
    """腾街业务输入、状态或受理错误。"""


class EvidenceConflict(StreetError):
    """签条、堆量界和确认磅单之间没有可行整数世界。"""


class SlotUnavailable(StreetError):
    """替代堆位容量不足或已经许给别人。"""


class PlanNotExecutable(StreetError):
    """没有能在所有证据世界中履行的现场动作。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bound:
    """整数闭区间 ``[lo, hi]``。"""

    lo: int
    hi: int

    def __post_init__(self) -> None:
        if isinstance(self.lo, bool) or isinstance(self.hi, bool):
            raise StreetError("bounds must be integers, not bool")
        if not isinstance(self.lo, int) or not isinstance(self.hi, int):
            raise StreetError("bounds must be integers")
        if self.lo < 0 or self.hi < 0:
            raise StreetError("snow amount must be >= 0")
        if self.lo > self.hi:
            raise StreetError(f"invalid bound [{self.lo}, {self.hi}]")

    @property
    def exact(self) -> int | None:
        return self.lo if self.lo == self.hi else None

    def to_list(self) -> list[int]:
        return [self.lo, self.hi]


def parse_bound(
    value: Any = None,
    *,
    amount: Any = None,
    lo: Any = None,
    hi: Any = None,
    default: tuple[int, int] | None = None,
    name: str = "amount",
) -> Bound:
    """把 int / ``(lo, hi)`` / dict / 关键字参数统一成 :class:`Bound`。"""

    if amount is not None:
        value = amount
    if value is not None and lo is not None:
        raise StreetError(f"{name}: pass either a value or lo/hi, not both")

    if lo is not None or hi is not None:
        if lo is None:
            lo = 0
        if hi is None:
            if default is not None:
                hi = default[1]
            else:
                raise StreetError(f"{name}: hi is required when lo/hi is used")
        return Bound(int(lo), int(hi))

    if value is None:
        if default is None:
            raise StreetError(f"{name} is required")
        return Bound(int(default[0]), int(default[1]))

    if isinstance(value, Bound):
        return value
    if isinstance(value, bool):
        raise StreetError(f"{name} must be an integer, not bool")
    if isinstance(value, int):
        return Bound(value, value)
    if isinstance(value, dict):
        return parse_bound(
            lo=value.get("lo", value.get("min")),
            hi=value.get("hi", value.get("max")),
            default=default,
            name=name,
        )
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise StreetError(f"{name} interval must have two elements")
        return Bound(int(value[0]), int(value[1]))
    if hasattr(value, "lo") and hasattr(value, "hi"):
        return Bound(int(value.lo), int(value.hi))
    raise StreetError(f"unsupported {name}: {value!r}")


def largest_integer_below(line: float) -> int:
    """余雪线允许的最大整数残留。

    正线按严格“低于”解释：``line=1`` 时只允许 ``0``。``line=0`` 作为“无雪”
    订单的便捷写法，允许残留 ``0``；负线没有物理意义。
    """

    if line < 0:
        raise StreetError("snow_line must be >= 0")
    if line == 0:
        return 0
    return math.ceil(line) - 1


def passes_snow_line(amount: int, line: float) -> bool:
    return amount <= largest_integer_below(line)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Bound):
        return value.to_list()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ---------------------------------------------------------------------------
# 记录类型
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    segment_id: str
    index: int
    name: str | None = None
    snow_line: float = 1

    def to_dict(self) -> dict[str, Any]:
        return {"segment_id": self.segment_id, "index": self.index,
                "name": self.name, "snow_line": self.snow_line}


@dataclass
class Stall:
    stall_id: str
    segment_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"stall_id": self.stall_id, "segment_id": self.segment_id}


@dataclass
class Lease:
    lease_id: str
    slot_id: str
    holder: str
    amount: int
    order_id: str | None = None
    prior: bool = False
    active: bool = True
    signed: bool = False
    receipt_id: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PileSlot:
    slot_id: str
    segment_id: str
    capacity: int
    leases: list[Lease] = field(default_factory=list)

    @property
    def reserved(self) -> int:
        return sum(x.amount for x in self.leases if x.active)

    @property
    def available(self) -> int:
        return self.capacity - self.reserved

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "segment_id": self.segment_id,
            "capacity": self.capacity,
            "reserved": self.reserved,
            "available": self.available,
            "leases": [x.to_dict() for x in self.leases],
        }


@dataclass
class TripRef:
    trip_id: str
    net_lo: int
    net_hi: int
    status: str = STATUS_CONFIRMED
    kind: str = "UNLOAD"
    source: str = "Warehouse"
    stops: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"net": [self.net_lo, self.net_hi]}


@dataclass
class OrderRecord:
    order_id: str
    kind: str
    status: str
    created_step: int
    title: str = ""
    segments: list[str] = field(default_factory=list)
    stalls: list[str] = field(default_factory=list)
    snow_lines: dict[str, float] = field(default_factory=dict)
    referenced_trips: list[str] = field(default_factory=list)
    granted_slots: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    evaluation: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status in (STATUS_ACCEPTED, STATUS_SIGNED, STATUS_RECORDED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "kind": self.kind,
            "status": self.status,
            "accepted": self.accepted,
            "created_step": self.created_step,
            "title": self.title,
            "segments": list(self.segments),
            "stalls": list(self.stalls),
            "snow_lines": dict(self.snow_lines),
            "referenced_trips": list(self.referenced_trips),
            "granted_slots": list(self.granted_slots),
            "reasons": list(self.reasons),
            "evaluation": _jsonable(self.evaluation),
            "payload": _jsonable(self.payload),
        }


@dataclass
class World:
    world_id: int
    values: dict[str, int]
    remaining: dict[str, int]
    trip_net: dict[str, int]
    timeline: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Reconstruction:
    feasible: bool
    worlds: list[World]
    remaining_range: dict[str, list[int]]
    trip_allocations: dict[str, dict[str, list[int]]]
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "world_count": len(self.worlds),
            "remaining_range": _jsonable(self.remaining_range),
            "trip_allocations": _jsonable(self.trip_allocations),
            "errors": list(self.errors),
            "worlds": [w.to_dict() for w in self.worlds],
        }


# ---------------------------------------------------------------------------
# 街道受理台
# ---------------------------------------------------------------------------

class StreetOffice:
    """腾街单受理、证据重建、堆位/禁倒和岔口回放。"""

    def __init__(self, warehouse: Warehouse | None = None) -> None:
        self.warehouse = warehouse
        self.segments: dict[str, Segment] = {}
        self.segment_order: list[str] = []
        self.stalls: dict[str, Stall] = {}
        self.slots: dict[str, PileSlot] = {}
        self.trips: dict[str, TripRef] = {}
        self.events: list[dict[str, Any]] = []
        self.orders: dict[str, OrderRecord] = {}
        self.permits: dict[str, dict[str, Any]] = {}
        self.bans: dict[str, dict[str, Any]] = {}
        self.points: dict[str, dict[str, Any]] = {}
        self.observations: dict[str, dict[str, Any]] = {}
        self.forecasts: dict[str, dict[str, Any]] = {}
        self.replays: list[dict[str, Any]] = []
        self.initial: dict[str, Bound] = {}
        self._step = 0

    # ------------------------------------------------------------- 基础复制
    def clone(self) -> "StreetOffice":
        """深复制桌面状态；共享只读的过磅仓库连接。"""

        new = object.__new__(StreetOffice)
        new.__dict__.update(self.__dict__)
        for key, value in self.__dict__.items():
            if key != "warehouse":
                setattr(new, key, copy.deepcopy(value))
        return new

    def _next_step(self) -> int:
        self._step += 1
        return self._step

    def _record(self, kind: str, slip_id: str | None = None, **data: Any) -> dict[str, Any]:
        ev = {"step": self._next_step(), "kind": kind, "slip_id": slip_id, "data": data}
        self.events.append(ev)
        return ev

    def _known_slip(self, slip_id: str) -> bool:
        return any(e.get("slip_id") == slip_id for e in self.events) or slip_id in self.orders

    # ------------------------------------------------------------- 路段/摊位
    def add_segment(
        self,
        segment_id: str,
        *,
        name: str | None = None,
        snow_line: float = 1,
    ) -> Segment:
        if segment_id in self.segments:
            return self.segments[segment_id]
        if snow_line < 0:
            raise StreetError("snow_line must be >= 0; zero-snow segment can use 0 or 1")
        seg = Segment(segment_id, len(self.segment_order), name, snow_line)
        self.segments[segment_id] = seg
        self.segment_order.append(segment_id)
        return seg

    def add_segments(self, *segment_ids: str | Sequence[str]) -> list[Segment]:
        if len(segment_ids) == 1 and not isinstance(segment_ids[0], str):
            ids = list(segment_ids[0])
        else:
            ids = list(segment_ids)  # type: ignore[arg-type]
        return [self.add_segment(x) for x in ids]  # type: ignore[arg-type]

    def add_stall(self, stall_id: str, segment_id: str) -> Stall:
        if segment_id not in self.segments:
            raise StreetError(f"unknown segment {segment_id}")
        if stall_id in self.stalls:
            return self.stalls[stall_id]
        stall = Stall(stall_id, segment_id)
        self.stalls[stall_id] = stall
        return stall

    def _require_segment(self, segment_id: str) -> Segment:
        try:
            return self.segments[segment_id]
        except KeyError as exc:
            raise StreetError(f"unknown segment {segment_id!r}") from exc

    def _active_ban_protecting(self, segment_id: str) -> str | None:
        for ban_id, ban in self.bans.items():
            if ban.get("active") and segment_id in ban.get("protected_segments", []):
                return ban_id
        return None

    def resolve_segments(
        self,
        segments: Sequence[str] | str | None = None,
        stalls: Sequence[str] | None = None,
    ) -> list[str]:
        result: list[str] = []
        if segments is not None:
            if isinstance(segments, str):
                text = segments.strip()
                if text in self.segments:
                    result.append(text)
                else:
                    parts = [p for p in re.split(r"[,，、\s]+", text) if p]
                    if len(parts) > 1:
                        result.extend(parts)
                    elif len(text) > 1 and all(ch in self.segments for ch in text):
                        # 允许直接写 “甲乙” / “AB”。
                        result.extend(list(text))
                    else:
                        result.append(text)
            else:
                result.extend(segments)
        for sid in stalls or []:
            if sid not in self.stalls:
                raise StreetError(f"unknown stall {sid!r}")
            result.append(self.stalls[sid].segment_id)
        # 去重但保持调用方顺序。
        out: list[str] = []
        for s in result:
            self._require_segment(s)
            if s not in out:
                out.append(s)
        return out

    def as_contiguous_block(self, segment_ids: Sequence[str]) -> list[str]:
        """若给定路段连续，返回按街道顺序排列的块；否则报错。"""

        if not segment_ids:
            raise StreetError("at least one segment is required")
        ids = self.resolve_segments(segment_ids)
        indices = sorted(self.segments[s].index for s in ids)
        if indices != list(range(indices[0], indices[0] + len(indices))):
            raise StreetError(f"segments are not contiguous: {list(segment_ids)}")
        return [self.segment_order[i] for i in indices]

    def stalls_for(self, segment_ids: Sequence[str]) -> list[str]:
        wanted = set(segment_ids)
        # 按街道顺序返回，避免中文 ID 按码点排序成乙在甲前。
        by_segment: dict[str, list[str]] = {sid: [] for sid in self.segment_order}
        for stall_id, stall in self.stalls.items():
            if stall.segment_id in wanted:
                by_segment.setdefault(stall.segment_id, []).append(stall_id)
        out: list[str] = []
        for sid in self.segment_order:
            out.extend(sorted(by_segment.get(sid, [])))
        return out

    def contiguous_blocks(self, qualified: Iterable[str]) -> list[list[str]]:
        wanted = set(qualified)
        blocks: list[list[str]] = []
        current: list[str] = []
        for sid in self.segment_order:
            if sid in wanted:
                current.append(sid)
            elif current:
                blocks.append(current)
                current = []
        if current:
            blocks.append(current)
        return blocks

    # ------------------------------------------------------------- 初始/观测
    def set_initial_snow(
        self,
        segment_id: str,
        amount: Any = None,
        *,
        lo: int | None = None,
        hi: int | None = None,
    ) -> Bound:
        self._require_segment(segment_id)
        bound = parse_bound(amount, lo=lo, hi=hi, default=(0, 0), name=f"initial:{segment_id}")
        old = self.initial.get(segment_id)
        if old is not None:
            bound = Bound(max(old.lo, bound.lo), min(old.hi, bound.hi))
            if bound.lo > bound.hi:
                raise EvidenceConflict(f"inconsistent initial bounds for {segment_id}")
        self.initial[segment_id] = bound
        self._record("INITIAL", None, segment=segment_id, bound=bound.to_list())
        return bound

    def record_pile_bounds(
        self,
        slip_id: str,
        segment_id: str,
        amount: Any = None,
        *,
        lo: int | None = None,
        hi: int | None = None,
        stage: str = "observed",
        source: str = "field_slip",
    ) -> Bound:
        self._require_segment(segment_id)
        if self._known_slip(slip_id):
            raise StreetError(f"slip/order id {slip_id} already exists")
        bound = parse_bound(amount, lo=lo, hi=hi, name=f"pile:{slip_id}")
        self._record("PILE", slip_id, segment=segment_id, bound=bound.to_list(),
                     stage=stage, source=source)
        return bound

    def record_sweep(
        self,
        slip_id: str,
        segment_id: str,
        *,
        before: Any = None,
        after: Any = None,
        before_lo: int | None = None,
        before_hi: int | None = None,
        after_lo: int | None = None,
        after_hi: int | None = None,
    ) -> dict[str, Bound]:
        self._require_segment(segment_id)
        if self._known_slip(slip_id):
            raise StreetError(f"slip/order id {slip_id} already exists")
        b = parse_bound(before, lo=before_lo, hi=before_hi,
                        default=(0, DEFAULT_BOUND_LIMIT), name="before_sweep")
        a = parse_bound(after, lo=after_lo, hi=after_hi,
                        default=(0, DEFAULT_BOUND_LIMIT), name="after_sweep")
        self._record("SWEEP", slip_id, segment=segment_id,
                     before=b.to_list(), after=a.to_list())
        return {"before": b, "after": a}

    # ------------------------------------------------------------- 车次引用
    def reference_confirmed_trip(
        self,
        trip_id: str,
        *,
        stops: Sequence[str] | None = None,
        net: Any = None,
    ) -> TripRef:
        if trip_id in self.trips:
            return self.trips[trip_id]

        net_bound: Bound
        status = STATUS_CONFIRMED
        kind = "UNLOAD"
        source = "manual"
        if self.warehouse is not None:
            try:
                row = self.warehouse.trip(trip_id)
            except Exception as exc:  # WarehouseError 类型由 warehouse 定义
                raise StreetError(f"cannot load trip {trip_id}: {exc}") from exc
            if row["status"] != STATUS_CONFIRMED:
                raise StreetError(
                    f"trip {trip_id} is {row['status']}; only CONFIRMED net weight can be quoted"
                )
            ev = self.warehouse.evaluation(trip_id)
            if ev.net_lo is None or ev.net_hi is None:
                raise EvidenceConflict(f"confirmed trip {trip_id} has no net interval")
            net_bound = Bound(int(ev.net_lo), int(ev.net_hi))
            status = row["status"]
            kind = row.get("kind", "UNLOAD")
            source = "Warehouse"
        else:
            net_bound = parse_bound(net, default=(0, 0), name=f"net:{trip_id}")

        stop_ids = list(stops or [])
        for s in stop_ids:
            self._require_segment(s)
        ref = TripRef(trip_id, net_bound.lo, net_bound.hi, status, kind, source, stop_ids)
        self.trips[trip_id] = ref
        self._record("TRIP_REF", trip_id, ref=ref.to_dict())
        return ref

    def add_confirmed_trip(self, trip_id: str, net: Any, *, stops: Sequence[str] | None = None) -> TripRef:
        """登记已在外部确认的净重；有 ``Warehouse`` 时应优先用 reference。"""

        return self.reference_confirmed_trip(trip_id, stops=stops, net=net)

    # ------------------------------------------------------------- 现场动作
    def add_pickup(
        self,
        slip_id: str,
        trip_id: str,
        segment_id: str,
        amount: Any = None,
        *,
        lo: int | None = None,
        hi: int | None = None,
        witnessed: bool = True,
        note: str = "",
    ) -> dict[str, Any]:
        """记录某车次在某段的一次装车/收集。

        不给定数量时，用 ``[0, 车次净重上界]`` 作为待见证区间。
        """

        self._require_segment(segment_id)
        trip = self.trips.get(trip_id) or self.reference_confirmed_trip(trip_id)
        if trip.stops and segment_id not in trip.stops:
            raise StreetError(f"segment {segment_id} is not in declared stops of {trip_id}")
        if self._known_slip(slip_id):
            raise StreetError(f"slip/order id {slip_id} already exists")
        bound = parse_bound(
            amount, lo=lo, hi=hi,
            default=(0, max(trip.net_hi, trip.net_lo)),
            name=f"pickup:{slip_id}",
        )
        if bound.lo > trip.net_hi:
            raise EvidenceConflict(
                f"pickup {bound.to_list()} exceeds {trip_id} net upper bound {trip.net_hi}"
            )
        data = {"trip_id": trip_id, "segment": segment_id, "bound": bound.to_list(),
                "witnessed": witnessed, "note": note}
        ev = self._record("PICKUP", slip_id, **data)
        return ev

    def record_loading_witness(
        self,
        slip_id: str,
        trip_id: str,
        segment_id: str,
        amount: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if amount is None:  # type: ignore[unreachable]
            raise StreetError("loading witness needs an exact amount")
        return self.add_pickup(slip_id, trip_id, segment_id, int(amount),
                               witnessed=True, **kwargs)

    def add_transfer(
        self,
        slip_id: str,
        src: str,
        dst: str,
        amount: Any = None,
        *,
        lo: int | None = None,
        hi: int | None = None,
        forward_only: bool = True,
        note: str = "",
    ) -> dict[str, Any]:
        self._require_segment(src)
        self._require_segment(dst)
        if src == dst:
            raise StreetError("transfer source and destination must differ")
        if forward_only and self.segments[dst].index <= self.segments[src].index:
            raise StreetError(f"transfer {src}->{dst} must move to a later segment")
        ban_id = self._active_ban_protecting(dst)
        if ban_id is not None:
            raise StreetError(
                f"{dst} is protected by active no-dump order {ban_id}; transfer/pile destination rejected"
            )
        if self._known_slip(slip_id):
            raise StreetError(f"slip/order id {slip_id} already exists")
        bound = parse_bound(amount, lo=lo, hi=hi, name=f"transfer:{slip_id}")
        return self._record("TRANSFER", slip_id, src=src, dst=dst,
                             bound=bound.to_list(), note=note)

    def add_dump(
        self,
        slip_id: str,
        segment_id: str,
        amount: Any = None,
        *,
        lo: int | None = None,
        hi: int | None = None,
        permit_id: str | None = None,
        source: str = "actual_landing",
    ) -> dict[str, Any]:
        self._require_segment(segment_id)
        ban_id = self._active_ban_protecting(segment_id)
        if ban_id is not None:
            raise StreetError(
                f"{segment_id} is protected by active no-dump order {ban_id}; dump rejected"
            )
        if self._known_slip(slip_id):
            raise StreetError(f"slip/order id {slip_id} already exists")
        bound = parse_bound(amount, lo=lo, hi=hi, name=f"dump:{slip_id}")
        ev = self._record("DUMP", slip_id, segment=segment_id,
                          bound=bound.to_list(), permit_id=permit_id, source=source)
        if permit_id and permit_id in self.permits:
            self.permits[permit_id]["actual_segment"] = segment_id
            self.permits[permit_id]["landing_slip"] = slip_id
        return ev

    # ------------------------------------------------------------- 堆位
    def add_slot(self, slot_id: str, segment_id: str, capacity: int = 1) -> PileSlot:
        self._require_segment(segment_id)
        if capacity <= 0:
            raise StreetError("slot capacity must be positive")
        slot = PileSlot(slot_id, segment_id, int(capacity))
        self.slots[slot_id] = slot
        self._record("SLOT", slot_id, segment=segment_id, capacity=capacity)
        return slot

    def rent_slot(
        self,
        slot_id: str,
        holder: str,
        amount: int = 1,
        *,
        lease_id: str | None = None,
        order_id: str | None = None,
        prior: bool = True,
        signed: bool = True,
        receipt_id: str | None = None,
        note: str = "",
        allow_overflow: bool = False,
    ) -> Lease:
        if slot_id not in self.slots:
            raise StreetError(f"unknown slot {slot_id!r}")
        slot = self.slots[slot_id]
        if amount <= 0:
            raise StreetError("lease amount must be positive")
        lease_id = lease_id or f"LSE-{len(self.slots)}-{len(slot.leases)+1}"
        if not allow_overflow and slot.available < amount:
            raise SlotUnavailable(
                f"slot {slot_id} has {slot.available}kg available, cannot sign {amount}kg "
                f"for {holder}"
            )
        lease = Lease(lease_id, slot_id, holder, int(amount), order_id,
                      prior=prior, active=True, signed=signed,
                      receipt_id=receipt_id, note=note)
        slot.leases.append(lease)
        self._record("LEASE", lease_id, **lease.to_dict())
        return lease

    def prior_rent_slot(self, slot_id: str, holder: str = "other_team",
                        amount: int = 1, **kwargs: Any) -> Lease:
        kwargs.setdefault("prior", True)
        kwargs.setdefault("note", "already rented before tonight")
        return self.rent_slot(slot_id, holder, amount, **kwargs)

    # ------------------------------------------------------------- 暂存单/禁倒
    def create_storage_permit(
        self,
        permit_id: str,
        amount: Any = 1,
        *,
        holder: str = "neighbor_team",
        possible_segments: Sequence[str] | None = None,
        signed: bool = True,
        land_before_handoff: bool = True,
        title: str = "邻队暂存申请",
    ) -> OrderRecord:
        if permit_id in self.orders or permit_id in self.permits:
            raise StreetError(f"permit/order {permit_id} already exists")
        destinations = list(possible_segments or self.segment_order[:2])
        for s in destinations:
            self._require_segment(s)
        bound = parse_bound(amount, name=f"permit:{permit_id}")
        data = {
            "permit_id": permit_id,
            "holder": holder,
            "amount": bound.to_list(),
            "possible_segments": destinations,
            "original_possible_segments": list(destinations),
            "directed_segment": None,
            "ban_order_id": None,
            "actual_segment": None,
            "landing_slip": None,
            "signed": signed,
            "land_before_handoff": land_before_handoff,
        }
        self.permits[permit_id] = data
        rec = OrderRecord(
            permit_id, STORAGE, STATUS_SIGNED if signed else STATUS_DRAFT,
            self._next_step(), title=title, segments=list(destinations),
            payload=data, reasons=[] if signed else ["storage application not signed"],
        )
        self.orders[permit_id] = rec
        self._record("STORAGE_PERMIT", permit_id, **_jsonable(data))
        return rec

    def sign_no_dump_order(
        self,
        order_id: str,
        *,
        permit_id: str,
        protected_segments: Sequence[str] | str,
        destination_segment: str,
        slot_id: str,
        amount: int | None = None,
        holder: str | None = None,
        title: str = "前夜禁倒单",
    ) -> OrderRecord:
        """签发禁倒单；若替代堆位不能实际签收，则留下 REJECTED 单并抛错。"""

        if order_id in self.orders:
            raise StreetError(f"order {order_id} already exists")
        if permit_id not in self.permits:
            raise StreetError(f"unknown storage permit {permit_id}")
        permit = self.permits[permit_id]
        protected = self.as_contiguous_block(protected_segments) if not isinstance(
            protected_segments, str
        ) else [protected_segments]
        # as_contiguous_block expects sequence; string would iterate chars, so handle.
        if isinstance(protected_segments, str):
            protected = [protected_segments]
        for s in protected:
            self._require_segment(s)
        self._require_segment(destination_segment)
        if slot_id not in self.slots:
            raise StreetError(f"unknown alternative slot {slot_id}")
        need = int(amount if amount is not None else permit["amount"][1])
        holder = holder or permit["holder"]

        payload: dict[str, Any] = {
            "permit_id": permit_id,
            "protected_segments": protected,
            "destination_segment": destination_segment,
            "slot_id": slot_id,
            "amount": need,
            "holder": holder,
        }
        step = self._next_step()

        try:
            lease = self.rent_slot(
                slot_id, holder, need,
                lease_id=f"VCH-{order_id}", order_id=order_id,
                prior=False, signed=False,
                receipt_id=f"RCV-{order_id}",
                note="alternative slot delivered with no-dump order",
            )
        except SlotUnavailable as exc:
            rec = OrderRecord(order_id, NO_DUMP, STATUS_REJECTED, step, title=title,
                              segments=protected, granted_slots=[slot_id],
                              payload=payload, reasons=[str(exc)])
            self.orders[order_id] = rec
            self.bans[order_id] = {"active": False, **payload}
            raise

        payload["lease_id"] = lease.lease_id
        payload["voucher_receipt_id"] = lease.receipt_id
        permit["possible_segments"] = [destination_segment]
        permit["directed_segment"] = destination_segment
        permit["ban_order_id"] = order_id
        ban = {"active": True, **payload}
        self.bans[order_id] = ban
        rec = OrderRecord(
            order_id, NO_DUMP, STATUS_SIGNED, step, title=title,
            segments=protected, granted_slots=[slot_id], payload=payload,
            reasons=[f"slot {slot_id} voucher {lease.receipt_id} deliverable to {holder}"],
        )
        self.orders[order_id] = rec
        self._record("NO_DUMP", order_id, **_jsonable(payload))
        return rec

    def revoke_no_dump_order(self, order_id: str, *, reason: str = "ban right removed") -> OrderRecord:
        if order_id not in self.bans:
            raise StreetError(f"unknown no-dump order {order_id}")
        ban = self.bans[order_id]
        if not ban.get("active"):
            return self.orders[order_id]
        ban["active"] = False
        rec = self.orders[order_id]
        rec.status = STATUS_REVOKED
        rec.reasons.append(reason)
        permit_id = ban["permit_id"]
        permit = self.permits[permit_id]
        permit["possible_segments"] = list(permit["original_possible_segments"])
        permit["directed_segment"] = None
        permit["ban_order_id"] = None
        self._record("REVOKE_NO_DUMP", order_id, reason=reason)
        return rec

    def sign_delivered_slot(self, order_id: str) -> Lease:
        """邻队实际签收禁倒时交付的堆位凭证。"""

        rec = self.orders.get(order_id)
        if rec is None or rec.kind != NO_DUMP or rec.status != STATUS_SIGNED:
            raise StreetError(f"{order_id} is not a signed no-dump order")
        lease_id = rec.payload["lease_id"]
        for lease in self.slots[rec.payload["slot_id"]].leases:
            if lease.lease_id == lease_id:
                lease.signed = True
                rec.reasons.append(f"neighbor signed {lease.receipt_id}")
                return lease
        raise StreetError("delivered lease not found")

    def land_storage(
        self,
        permit_id: str,
        segment_id: str,
        *,
        receipt_id: str | None = None,
        amount: int | None = None,
        virtual: bool = False,
    ) -> dict[str, Any]:
        if permit_id not in self.permits:
            raise StreetError(f"unknown storage permit {permit_id}")
        permit = self.permits[permit_id]
        allowed = set(permit["possible_segments"])
        if segment_id not in allowed:
            raise StreetError(
                f"permit {permit_id} only allows {sorted(allowed)}, cannot land {segment_id}"
            )
        slip_id = receipt_id or f"LAND-{permit_id}-{segment_id}"
        if amount is None:
            lo, hi = permit["amount"]
            amount_bound = Bound(int(lo), int(hi))
        else:
            amount_bound = Bound(int(amount), int(amount))
        ev = self.add_dump(slip_id, segment_id, amount_bound,
                           permit_id=permit_id,
                           source="virtual_world" if virtual else "signed_landing")
        if not virtual:
            permit["actual_segment"] = segment_id
            permit["landing_slip"] = slip_id
            permit["signed_landing"] = True
        return ev

    # ------------------------------------------------------------- 岔口/观测
    def add_loading_point(
        self,
        point_id: str,
        segment_id: str,
        *,
        fork_id: str = "F1",
        reachable: bool = True,
        arrival_time: int | None = None,
        closure_time: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        self._require_segment(segment_id)
        actually_reachable = reachable
        if arrival_time is not None and closure_time is not None:
            actually_reachable = actually_reachable and arrival_time <= closure_time
        point = {
            "point_id": point_id, "segment": segment_id, "fork_id": fork_id,
            "reachable": actually_reachable, "explicit_reachable": reachable,
            "arrival_time": arrival_time, "closure_time": closure_time,
            "one_way_no_return": True, "note": note,
        }
        self.points[point_id] = point
        self._record("LOADING_POINT", point_id, **point)
        return point

    def set_point_reachable(self, point_id: str, reachable: bool) -> dict[str, Any]:
        if point_id not in self.points:
            raise StreetError(f"unknown loading point {point_id}")
        self.points[point_id]["reachable"] = reachable
        self.points[point_id]["explicit_reachable"] = reachable
        self._record("POINT_CHANGE", point_id, reachable=reachable)
        return self.points[point_id]

    def record_observation(
        self,
        observation_id: str,
        segment_id: str | None = None,
        *,
        fork_id: str = "F1",
        timing: str = BEFORE_FORK,
        before_fork: bool | None = None,
        visible: bool = True,
        amount: int | None = None,
        observer: str = "field_team",
    ) -> dict[str, Any]:
        if segment_id is not None:
            self._require_segment(segment_id)
        if before_fork is not None:
            timing = BEFORE_FORK if before_fork else AFTER_FORK
        obs = {
            "observation_id": observation_id, "segment": segment_id,
            "fork_id": fork_id, "timing": timing,
            "before_fork": timing == BEFORE_FORK,
            "visible": visible, "authoritative": True,
            "observer": observer, "amount": amount,
        }
        self.observations[observation_id] = obs
        self._record("OBSERVATION", observation_id, **obs)
        return obs

    def record_forecast(
        self,
        forecast_id: str,
        segment_id: str,
        *,
        fork_id: str = "F1",
        source: str = "city_old_forecast",
    ) -> dict[str, Any]:
        self._require_segment(segment_id)
        fc = {"forecast_id": forecast_id, "segment": segment_id, "fork_id": fork_id,
              "authoritative": False, "source": source}
        self.forecasts[forecast_id] = fc
        self._record("FORECAST", forecast_id, **fc)
        return fc

    # ------------------------------------------------------------- 证据重建
    def _amount_events(self) -> list[dict[str, Any]]:
        return [e for e in self.events
                if e["kind"] in {"PICKUP", "TRANSFER", "DUMP"}
                and not e.get("superseded_by")]

    def replace_loading_witness(
        self,
        old_slip_id: str,
        new_slip_id: str,
        amount: int,
        *,
        reason: str = "witness replaced",
        reevaluate_orders: bool = True,
    ) -> dict[str, Any]:
        """作废旧装车见证并追加新见证；旧事件保留在审计日志中。"""

        old = next((e for e in self.events if e.get("slip_id") == old_slip_id), None)
        if old is None or old["kind"] != "PICKUP":
            raise StreetError(f"unknown loading witness {old_slip_id!r}")
        if self._known_slip(new_slip_id):
            raise StreetError(f"slip/order id {new_slip_id} already exists")
        old["superseded_by"] = new_slip_id
        old["supersede_reason"] = reason
        data = old["data"]
        ev = self.add_pickup(
            new_slip_id, data["trip_id"], data["segment"], int(amount),
            witnessed=True, note=f"replaces {old_slip_id}: {reason}",
        )
        ev["replaces"] = old_slip_id
        if reevaluate_orders:
            for oid, rec in list(self.orders.items()):
                if rec.kind == STREET_RELEASE:
                    self.evaluate_order(oid)
        return ev

    def reconstruct(self) -> Reconstruction:
        vars_: list[tuple[str, int, int]] = []
        for sid in self.segment_order:
            b = self.initial.get(sid, Bound(0, 0))
            vars_.append((f"initial:{sid}", b.lo, b.hi))

        amount_key: dict[int, str] = {}
        for idx, ev in enumerate(self._amount_events()):
            key = f"amount:{ev['slip_id'] or idx}"
            amount_key[id(ev)] = key
            lo, hi = ev["data"]["bound"]
            vars_.append((key, int(lo), int(hi)))

        assignment_count = 1
        for _, lo, hi in vars_:
            assignment_count *= max(1, hi - lo + 1)
            if assignment_count > MAX_ENUMERATED_ASSIGNMENTS:
                raise StreetError(
                    "evidence ranges are too wide for exact tabletop enumeration; "
                    "add witnesses or tighten pile bounds"
                )

        keys = [k for k, _, _ in vars_]
        domains = [list(range(lo, hi + 1)) for _, lo, hi in vars_]
        values: dict[str, int] = {}
        worlds: list[World] = []
        errors: list[str] = []

        def check_bound(bound: Sequence[int], amount: int) -> bool:
            return int(bound[0]) <= amount <= int(bound[1])

        def evaluate_assignment(assign: dict[str, int]) -> World | None:
            inv = {sid: assign[f"initial:{sid}"] for sid in self.segment_order}
            timeline: list[dict[str, Any]] = []
            for ev in self.events:
                if ev.get("superseded_by"):
                    continue
                kind = ev["kind"]
                d = ev["data"]
                if kind == "PILE":
                    if not check_bound(d["bound"], inv[d["segment"]]):
                        return None
                    timeline.append({"step": ev["step"], "kind": kind,
                                     "slip_id": ev["slip_id"], "balances": dict(inv)})
                elif kind == "SWEEP":
                    seg = d["segment"]
                    if not check_bound(d["before"], inv[seg]):
                        return None
                    if not check_bound(d["after"], inv[seg]):
                        return None
                    timeline.append({"step": ev["step"], "kind": kind,
                                     "slip_id": ev["slip_id"], "balances": dict(inv)})
                elif kind in {"PICKUP", "TRANSFER", "DUMP"}:
                    amt = assign[amount_key[id(ev)]]
                    if kind == "PICKUP":
                        seg = d["segment"]
                        if amt > inv[seg]:
                            return None
                        inv[seg] -= amt
                    elif kind == "DUMP":
                        inv[d["segment"]] += amt
                    else:
                        src, dst = d["src"], d["dst"]
                        if amt > inv[src]:
                            return None
                        inv[src] -= amt
                        inv[dst] += amt
                    if any(x < 0 for x in inv.values()):
                        return None
                    timeline.append({"step": ev["step"], "kind": kind,
                                     "slip_id": ev["slip_id"], "amount": amt,
                                     "balances": dict(inv)})

            trip_net: dict[str, int] = {}
            sums: dict[str, int] = {tid: 0 for tid in self.trips}
            seg_amounts: dict[tuple[str, str], list[int]] = {}
            for ev in self._amount_events():
                if ev["kind"] != "PICKUP":
                    continue
                tid = ev["data"]["trip_id"]
                amt = assign[amount_key[id(ev)]]
                sums[tid] = sums.get(tid, 0) + amt
                seg_amounts.setdefault((tid, ev["data"]["segment"]), []).append(amt)
            for tid, total in sums.items():
                ref = self.trips[tid]
                if not (ref.net_lo <= total <= ref.net_hi):
                    return None
                trip_net[tid] = total

            wid = len(worlds)
            return World(wid, dict(assign), dict(inv), trip_net, timeline)

        def dfs(pos: int) -> None:
            if pos == len(keys):
                w = evaluate_assignment(values)
                if w is not None:
                    worlds.append(w)
                return
            key = keys[pos]
            for val in domains[pos]:
                values[key] = val
                dfs(pos + 1)
            values.pop(key, None)

        if vars_:
            dfs(0)

        if not worlds:
            errors.append("no feasible material-flow world matches slips, bounds and confirmed trips")
            return Reconstruction(False, [], {}, {}, errors)

        remaining_range: dict[str, list[int]] = {}
        for sid in self.segment_order:
            vals = [w.remaining[sid] for w in worlds]
            remaining_range[sid] = [min(vals), max(vals)]

        trip_allocations: dict[str, dict[str, list[int]]] = {}
        for tid, ref in self.trips.items():
            trip_allocations[tid] = {"net": [ref.net_lo, ref.net_hi], "segments": {}}
            by_seg: dict[str, list[int]] = {}
            for w in worlds:
                for ev in self._amount_events():
                    if ev["kind"] == "PICKUP" and ev["data"]["trip_id"] == tid:
                        seg = ev["data"]["segment"]
                        amt = w.values[amount_key[id(ev)]]
                        by_seg.setdefault(seg, []).append(amt)
            for seg, vals in by_seg.items():
                trip_allocations[tid]["segments"][seg] = [min(vals), max(vals)]
        return Reconstruction(True, worlds, remaining_range, trip_allocations, [])

    # ------------------------------------------------------------- 暂存世界
    def _pending_permit_ids(self, order: OrderRecord | None = None) -> list[str]:
        if order is not None:
            explicit = order.payload.get("permit_ids")
            if explicit is not None:
                return list(explicit)
        return [pid for pid, p in self.permits.items()
                if p.get("land_before_handoff", True) and not p.get("actual_segment")]

    def _destination_worlds(self, permit_ids: Sequence[str]) -> list[tuple[str, dict[str, str]]]:
        choices: list[tuple[str, list[str]]] = []
        for pid in permit_ids:
            p = self.permits[pid]
            if p.get("actual_segment"):
                continue
            dests = list(p["possible_segments"])
            if not dests:
                raise StreetError(f"permit {pid} has no possible landing segment")
            choices.append((pid, dests))
        worlds: list[tuple[str, dict[str, str]]] = [("W0", {})]
        for pid, dests in choices:
            next_worlds: list[tuple[str, dict[str, str]]] = []
            for label, mapping in worlds:
                for i, dst in enumerate(dests):
                    m = dict(mapping)
                    m[pid] = dst
                    next_worlds.append((f"{label}.{pid}={dst}", m))
            worlds = next_worlds
        return worlds

    def _office_with_landings(self, mapping: dict[str, str]) -> "StreetOffice":
        office = self.clone()
        for pid, dst in mapping.items():
            office.land_storage(pid, dst, virtual=True)
        return office

    # ------------------------------------------------------------- 腾街单受理
    def submit_release_order(
        self,
        order_id: str,
        *,
        segments: Sequence[str] | str | None = None,
        stalls: Sequence[str] | None = None,
        snow_line: float | None = None,
        lines: dict[str, float] | None = None,
        vendor_id: str | None = None,
        title: str = "腾街交场单",
        method: str = METHOD_AUTO,
        permit_ids: Sequence[str] | None = None,
        no_dump_order_id: str | None = None,
        trip_ids: Sequence[str] | None = None,
        final_trip_id: str | None = None,
        capacity: int = 1,
        fork_id: str = "F1",
        observation_id: str | None = None,
        observation_before_fork: bool | None = True,
        forecast_segment: str | None = None,
        point_map: dict[str, str] | None = None,
        accept: bool = True,
    ) -> OrderRecord:
        if order_id in self.orders:
            raise StreetError(f"order {order_id} already exists")
        seg_ids = self.resolve_segments(segments, stalls)
        block = self.as_contiguous_block(seg_ids)
        stall_ids = self.stalls_for(block)
        if stalls:
            # 保留街道顺序，同时确保摊贩显式提交的摊位不会丢失。
            extra = [s for s in stalls if s not in stall_ids]
            stall_ids = self.stalls_for(block + self.resolve_segments(stalls=extra))

        line_map: dict[str, float] = {}
        for sid in block:
            if lines and sid in lines:
                line_map[sid] = float(lines[sid])
            elif snow_line is not None:
                line_map[sid] = float(snow_line)
            else:
                line_map[sid] = float(self.segments[sid].snow_line)
            largest_integer_below(line_map[sid])

        referenced_trip_ids = list(trip_ids or [])
        if final_trip_id:
            # 单据上引用车号是受理事实；是否把净重纳入物料平衡，取决于是否已
            # reference_confirmed_trip 并拿到对应装车见证。
            referenced_trip_ids.append(final_trip_id)
        referenced_trip_ids = list(dict.fromkeys(referenced_trip_ids))

        method = method.upper().replace("-", "_")
        aliases = {"OBSERVATION": METHOD_OBSERVE, "OBSERVED": METHOD_OBSERVE,
                   "FORK": METHOD_OBSERVE, "LIVE": METHOD_OBSERVE,
                   "BAN": METHOD_BAN, "FORECAST": METHOD_FIXED}
        method = aliases.get(method, method)
        planned_trip_id = final_trip_id if final_trip_id not in self.trips else None
        payload = {
            "vendor_id": vendor_id,
            "method": method,
            "permit_ids": list(permit_ids) if permit_ids is not None else None,
            "no_dump_order_id": no_dump_order_id,
            "final_trip_id": final_trip_id,
            "planned_trip_id": planned_trip_id,
            "capacity": capacity,
            "fork_id": fork_id,
            "observation_id": observation_id,
            "observation_before_fork": observation_before_fork,
            "forecast_segment": forecast_segment,
            "point_map": dict(point_map or {}),
        }
        rec = OrderRecord(
            order_id, STREET_RELEASE, STATUS_RECORDED, self._next_step(),
            title=title, segments=block, stalls=stall_ids, snow_lines=line_map,
            referenced_trips=referenced_trip_ids, payload=payload,
        )
        self.orders[order_id] = rec
        if accept:
            self.evaluate_order(order_id)
        return rec

    # ------------------------------------------------------------- 评估
    def evaluate_order(self, order_id: str) -> OrderRecord:
        rec = self.orders[order_id]
        if rec.kind != STREET_RELEASE:
            return rec
        p = rec.payload
        permit_ids = self._pending_permit_ids(rec)
        p["resolved_permit_ids"] = permit_ids

        methods = [p["method"]]
        if p["method"] == METHOD_AUTO:
            methods = [METHOD_BAN, METHOD_OBSERVE, METHOD_FIXED, METHOD_NONE]
        results: dict[str, Any] = {}
        accepted_method: str | None = None

        for method in methods:
            if method == METHOD_BAN:
                res = self._eval_ban(rec, permit_ids)
            elif method == METHOD_OBSERVE:
                res = self._eval_observed(rec, permit_ids)
            elif method == METHOD_FIXED:
                res = self._eval_fixed(rec, permit_ids)
            elif method == METHOD_NONE:
                res = self._eval_none(rec, permit_ids)
            else:
                res = {"method": method, "guaranteed": False,
                       "blocker": "UNKNOWN_METHOD", "worlds": []}
            safe = self._safe_segments_under_result(res)
            res["safe_segments"] = safe
            res["safe_blocks"] = self.contiguous_blocks(safe)
            results[method] = res
            if res.get("guaranteed") and accepted_method is None:
                accepted_method = method
                break

        rec.evaluation = {"selected_method": accepted_method, "methods": results}
        for res in results.values():
            for sid in res.get("slot_ids", []):
                if sid not in rec.granted_slots:
                    rec.granted_slots.append(sid)
        if accepted_method is not None:
            rec.status = STATUS_ACCEPTED
            rec.reasons = [f"executable method: {accepted_method}"]
            selected = results[accepted_method]
            rec.referenced_trips = sorted(set(rec.referenced_trips + selected.get("trip_ids", [])))
            rec.granted_slots = sorted(set(rec.granted_slots + selected.get("slot_ids", [])))
            rec.evaluation["deliverable_segments"] = rec.segments
            rec.evaluation["deliverable_stalls"] = rec.stalls
        else:
            rec.status = STATUS_REJECTED
            blockers = []
            for m, r in results.items():
                if r.get("blocker") and r.get("worlds"):
                    blockers.append(f"{m}: {r['blocker']}")
                elif r.get("blocker"):
                    blockers.append(f"{m}: {r['blocker']}")
                elif not r.get("guaranteed"):
                    bad = sum(1 for w in r.get("worlds", []) if not w.get("pass"))
                    blockers.append(f"{m}: {bad} world(s) miss snow line")
            rec.reasons = blockers or ["no executable method guarantees the whole block"]
            # 每个已尝试动作各自能保守交出的连续块；接班人可据此另开缩段单。
            candidates: list[dict[str, Any]] = []
            for m, r in results.items():
                for block in r.get("safe_blocks", []):
                    candidates.append({
                        "method": m,
                        "segments": block,
                        "stalls": self.stalls_for(block),
                        "length": len(block),
                    })
            candidates.sort(key=lambda x: x["length"], reverse=True)
            recommended = candidates[0] if candidates else None
            safe = self._robust_safe_segments(results)
            rec.evaluation["partial_candidates"] = candidates
            rec.evaluation["recommended_partial"] = recommended
            rec.evaluation["robust_safe_segments"] = safe
            rec.evaluation["robust_safe_blocks"] = self.contiguous_blocks(safe)
            rec.evaluation["robust_safe_stalls"] = self.stalls_for(safe)
            if recommended:
                rec.evaluation["deliverable_segments"] = recommended["segments"]
                rec.evaluation["deliverable_stalls"] = recommended["stalls"]
        return rec

    def _safe_segments_under_result(self, result: dict[str, Any]) -> list[str]:
        """该策略执行后，在它自己的全部原子世界中都合格的段。"""

        safe_sets: list[set[str]] = []
        for world in result.get("worlds", []):
            atomics = world.get("atomic_worlds")
            if atomics is None:
                # 兼容直接放 final 的结构。
                atomics = [world] if "final" in world else []
            for aw in atomics:
                final = aw.get("final")
                if not final:
                    safe_sets.append(set())
                    continue
                safe = {s for s in self.segment_order
                        if s in final and passes_snow_line(final[s], self.segments[s].snow_line)}
                safe_sets.append(safe)
        if not safe_sets:
            return []
        safe = set.intersection(*safe_sets)
        return [s for s in self.segment_order if s in safe]

    def _order_ban_active(self, ban_id: str | None, permit_ids: Sequence[str]) -> tuple[bool, list[str], list[str]]:
        slot_ids: list[str] = []
        trip_ids: list[str] = []
        if ban_id:
            ban = self.bans.get(ban_id)
            if ban is not None:
                # 即使禁倒权已被撤，堆位仍是这张单引用/交付过的审计事实。
                slot_ids.append(ban["slot_id"])
            if not ban or not ban.get("active"):
                return False, slot_ids, trip_ids
            return True, slot_ids, trip_ids
        # 没有显式 ban id 时，只要每个待落堆 permit 都被有效禁倒单改道即可。
        for pid in permit_ids:
            p = self.permits[pid]
            bid = p.get("ban_order_id")
            if not bid or not self.bans.get(bid, {}).get("active"):
                return False, slot_ids, trip_ids
            slot_ids.append(self.bans[bid]["slot_id"])
        return True, slot_ids, trip_ids

    def _eval_ban(self, rec: OrderRecord, permit_ids: Sequence[str]) -> dict[str, Any]:
        active, slot_ids, _ = self._order_ban_active(rec.payload.get("no_dump_order_id"), permit_ids)
        if not active:
            return {"method": METHOD_BAN, "guaranteed": False,
                    "blocker": "NO_ACTIVE_NO_DUMP_OR_SLOT", "slot_ids": slot_ids, "worlds": []}
        world_results = self._static_world_results(rec, permit_ids, route=None)
        guaranteed = all(w["pass"] for w in world_results)
        return {"method": METHOD_BAN, "guaranteed": guaranteed,
                "slot_ids": slot_ids, "worlds": world_results,
                "blocker": None if guaranteed else "REMAINING_ABOVE_LINE"}

    def _eval_none(self, rec: OrderRecord, permit_ids: Sequence[str]) -> dict[str, Any]:
        world_results = self._static_world_results(rec, permit_ids, route=None)
        guaranteed = all(w["pass"] for w in world_results)
        return {"method": METHOD_NONE, "guaranteed": guaranteed,
                "worlds": world_results,
                "blocker": None if guaranteed else "REMAINING_ABOVE_LINE"}

    def _point_for(self, rec: OrderRecord, segment_id: str | None) -> dict[str, Any] | None:
        if segment_id is None:
            return None
        pmap = rec.payload.get("point_map") or {}
        pid = pmap.get(segment_id)
        if pid is not None:
            return self.points.get(pid)
        for point in self.points.values():
            if point["segment"] == segment_id and point["fork_id"] == rec.payload["fork_id"]:
                return point
        return None

    def _observation_timely(self, rec: OrderRecord) -> bool:
        obs_id = rec.payload.get("observation_id")
        if obs_id:
            obs = self.observations.get(obs_id)
            return bool(obs and obs.get("authoritative") and obs.get("before_fork")
                        and obs.get("visible", True))
        return bool(rec.payload.get("observation_before_fork", True))

    def _eval_observed(self, rec: OrderRecord, permit_ids: Sequence[str]) -> dict[str, Any]:
        if not self._observation_timely(rec):
            return {"method": METHOD_OBSERVE, "guaranteed": False,
                    "blocker": "OBSERVATION_AFTER_FORK", "worlds": []}
        return self._route_world_results(rec, permit_ids, observed=True)

    def _eval_fixed(self, rec: OrderRecord, permit_ids: Sequence[str]) -> dict[str, Any]:
        if not rec.payload.get("forecast_segment"):
            return {"method": METHOD_FIXED, "guaranteed": False,
                    "blocker": "NO_FIXED_FORECAST_ROUTE", "worlds": []}
        return self._route_world_results(rec, permit_ids, observed=False)

    def _static_world_results(
        self,
        rec: OrderRecord,
        permit_ids: Sequence[str],
        route: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for label, mapping in self._destination_worlds(permit_ids):
            office = self._office_with_landings(mapping)
            recon = office.reconstruct()
            if not recon.feasible:
                out.append({"label": label, "landing": mapping, "feasible": False,
                            "pass": False, "errors": recon.errors, "atomic_worlds": []})
                continue
            atomic = []
            all_pass = True
            for w in recon.worlds:
                final = dict(w.remaining)
                passed = all(passes_snow_line(final[s], rec.snow_lines[s])
                             for s in rec.segments)
                all_pass &= passed
                atomic.append({"world_id": w.world_id, "final": final, "pass": passed})
            out.append({"label": label, "landing": mapping, "feasible": True,
                        "pass": all_pass, "remaining_range": recon.remaining_range,
                        "atomic_worlds": atomic})
        return out

    def _route_world_results(
        self,
        rec: OrderRecord,
        permit_ids: Sequence[str],
        *,
        observed: bool,
    ) -> dict[str, Any]:
        capacity = int(rec.payload.get("capacity", 1))
        final_trip = rec.payload.get("final_trip_id")
        trip_ids = [final_trip] if final_trip and final_trip in self.trips else []
        planned_trip_id = final_trip if final_trip and final_trip not in self.trips else None
        slot_ids: list[str] = []
        world_results: list[dict[str, Any]] = []
        all_guaranteed = True
        any_world = False

        for label, mapping in self._destination_worlds(permit_ids):
            office = self._office_with_landings(mapping)
            recon = office.reconstruct()
            if not recon.feasible:
                world_results.append({"label": label, "landing": mapping,
                                      "feasible": False, "pass": False,
                                      "errors": recon.errors})
                all_guaranteed = False
                continue

            # 观测策略能看见是哪一张暂存单落到了哪一段。固定路线只看旧预计表。
            if observed:
                target = next(iter(mapping.values()), None)
                # 若该落点不在申请块内，它不会影响承诺；车无需为该单进入岔口。
            else:
                target = rec.payload.get("forecast_segment")

            point = self._point_for(rec, target)
            point_ok = bool(point and point.get("reachable"))
            atomic = []
            destination = point["point_id"] if point else None
            destination_segment = target if point else None

            for w in recon.worlds:
                any_world = True
                final = dict(w.remaining)
                loaded: dict[str, int] = {}
                blocker = None

                threatened = [s for s in rec.segments
                              if final[s] >= largest_integer_below(rec.snow_lines[s]) + 1]
                needs_action = bool(threatened)
                if needs_action:
                    if target is None:
                        blocker = "NO_ROUTE_TARGET"
                    elif target not in rec.segments:
                        # 落点在丙等承诺块外；申请块本身无雪，不需要装车。
                        pass
                    elif target not in threatened:
                        # 车到了干净/已低于线的一侧，真正超线的是另一侧，单行不能返回。
                        blocker = "WRONG_FORK_SIDE"
                    elif not point_ok:
                        blocker = "LOADING_POINT_UNREACHABLE"
                    else:
                        allowed = largest_integer_below(rec.snow_lines[target])
                        required = max(0, final[target] - allowed)
                        if required > capacity:
                            blocker = "TRUCK_CAPACITY_EXCEEDED"
                        else:
                            load = min(required, capacity, final[target])
                            final[target] -= load
                            loaded[target] = load
                passed = blocker is None and all(
                    passes_snow_line(final[s], rec.snow_lines[s]) for s in rec.segments
                )
                if not passed:
                    all_guaranteed = False
                atomic.append({
                    "world_id": w.world_id, "before": dict(w.remaining),
                    "loaded": loaded, "final": final, "pass": passed,
                    "blocker": blocker,
                    "destination_point": destination,
                    "destination_segment": destination_segment,
                    "one_way_no_return": True,
                })
            world_results.append({
                "label": label, "landing": mapping, "feasible": True,
                "observed": observed, "target_segment": target,
                "point": point, "point_reachable": point_ok,
                "pass": all(x["pass"] for x in atomic),
                "atomic_worlds": atomic,
            })

        if not any_world and not world_results:
            # 没有待处理暂存单时，观测/路线策略退化为直接核对当前事实。
            static = self._eval_none(rec, [])
            return {"method": METHOD_OBSERVE if observed else METHOD_FIXED,
                    "guaranteed": static["guaranteed"], "worlds": static["worlds"]}

        blocker = None if all_guaranteed else "ROUTE_CANNOT_COVER_EVERY_WORLD"
        return {"method": METHOD_OBSERVE if observed else METHOD_FIXED,
                "guaranteed": all_guaranteed, "blocker": blocker,
                "trip_ids": trip_ids, "planned_trip_id": planned_trip_id,
                "slot_ids": slot_ids,
                "worlds": world_results}

    def _robust_safe_segments(self, method_results: dict[str, Any]) -> list[str]:
        """在所有被尝试策略的所有原子实况中都低于各自默认线的段。

        对被拒整单，接班人至少能改开这些段；若不同策略结论冲突，保守取交集。
        """

        safe_sets: list[set[str]] = []
        for res in method_results.values():
            for w in res.get("worlds", []):
                if not w.get("feasible", True):
                    safe_sets.append(set())
                    continue
                for aw in w.get("atomic_worlds", []):
                    final = aw.get("final", {})
                    safe = {s for s in self.segment_order
                            if s in final and final[s] < self.segments[s].snow_line}
                    safe_sets.append(safe)
        if not safe_sets:
            return []
        safe = set.intersection(*safe_sets) if safe_sets else set()
        return [s for s in self.segment_order if s in safe]

    # ------------------------------------------------------------- 桌面回放
    def replay_morning(
        self,
        order_id: str,
        *,
        landing_segment: str,
        permit_id: str | None = None,
        observed: bool = True,
        observation_timing: str | None = None,
        forecast_segment: str | None = None,
        capacity: int | None = None,
        load_trip_id: str | None = None,
        record: bool = False,
    ) -> dict[str, Any]:
        """回放一个具体清晨实况。

        ``record=False`` 时在克隆上推演，不改变受理台；桌面演示可安全重放。
        """

        rec = self.orders[order_id]
        if rec.kind != STREET_RELEASE:
            raise StreetError(f"{order_id} is not a release order")
        office = self if record else self.clone()
        p = rec.payload
        pid = permit_id or (p.get("permit_ids") or [next(iter(self.permits), None)])[0]
        if pid is None:
            raise StreetError("replay needs a storage permit")

        timing = observation_timing
        if timing is None and p.get("observation_id"):
            obs = self.observations.get(p["observation_id"])
            timing = obs["timing"] if obs else BEFORE_FORK
        timing = timing or BEFORE_FORK
        timely = observed and timing == BEFORE_FORK

        # 落堆。
        if not self.permits[pid].get("actual_segment"):
            office.land_storage(pid, landing_segment)

        recon = office.reconstruct()
        if not recon.feasible:
            return {"order_id": order_id, "feasible": False, "errors": recon.errors}
        # 具体回放取唯一/第一个事实世界；若证据仍有区间，列出全部。
        base_world = recon.worlds[0]

        if timely:
            target = landing_segment
            decision_basis = "FIELD_OBSERVATION_BEFORE_FORK"
        else:
            target = forecast_segment or p.get("forecast_segment")
            decision_basis = "OLD_FORECAST_AFTER_OBSERVATION_TOO_LATE"

        point = office._point_for(rec, target) if target else None
        result: dict[str, Any] = {
            "order_id": order_id,
            "permit_id": pid,
            "landing_segment": landing_segment,
            "decision_basis": decision_basis,
            "timing": timing,
            "target_segment": target,
            "point": point,
            "one_way_no_return": True,
            "before": dict(base_world.remaining),
        }

        final = dict(base_world.remaining)
        loaded = {}
        cap = int(capacity if capacity is not None else p.get("capacity", 1))
        blocker = None
        if target and target in rec.segments:
            if point is None or not point.get("reachable"):
                blocker = "LOADING_POINT_UNREACHABLE"
            else:
                line = rec.snow_lines[target]
                allowed = largest_integer_below(line)
                required = max(0, final[target] - allowed)
                load = min(required, cap, final[target])
                if required > cap:
                    blocker = "CAPACITY_EXCEEDED"
                else:
                    final[target] -= load
                    loaded[target] = load
                    if record and load_trip_id and load:
                        trip_ref = office.trips.get(load_trip_id)
                        if trip_ref is None and office.warehouse is not None:
                            trip_ref = office.reference_confirmed_trip(load_trip_id)
                        office.add_pickup(
                            f"ACTUAL-{order_id}-{target}", load_trip_id, target, load,
                            note="actual fork dispatch loading witness",
                        )
                        if trip_ref is not None and load_trip_id not in rec.referenced_trips:
                            rec.referenced_trips.append(load_trip_id)
        elif target and target not in rec.segments:
            # 车被派到了承诺块外；不影响申请块，但要如实记录去向。
            pass

        requested_open = all(passes_snow_line(final[s], rec.snow_lines[s])
                             for s in rec.segments)
        if blocker is None and not requested_open:
            blocker = "WRONG_FORK_SIDE" if not timely else "REMAINING_ABOVE_LINE"
        result.update({
            "loaded": loaded,
            "final": final,
            "blocker": blocker,
            "requested_segments": rec.segments,
            "requested_open": requested_open,
        })
        qualified = [s for s in self.segment_order
                     if passes_snow_line(final.get(s, 0), self.segments[s].snow_line)]
        result["open_segments"] = qualified
        result["open_blocks"] = office.contiguous_blocks(qualified)
        result["open_stalls"] = office.stalls_for(qualified)
        result["deliverable_requested_stalls"] = rec.stalls if result["requested_open"] else []
        if record:
            self.replays.append(result)
        return result

    # ------------------------------------------------------------- 查询/导出
    def query_order(self, order_id: str) -> dict[str, Any]:
        if order_id not in self.orders:
            raise StreetError(f"unknown order {order_id!r}")
        rec = self.orders[order_id]
        d = rec.to_dict()
        # 接班人视图：把引用车次的确认净重和分段 allocation 直接展开。
        trip_view = []
        recon = None
        if rec.kind == STREET_RELEASE:
            try:
                recon = self.reconstruct()
            except StreetError:
                recon = None
        for tid in rec.referenced_trips:
            t = self.trips.get(tid)
            if t is not None:
                item = t.to_dict()
                if recon is not None and tid in recon.trip_allocations:
                    item["accounting"] = recon.trip_allocations[tid]
            elif self.warehouse is not None:
                try:
                    row = self.warehouse.trip(tid)
                    ev = self.warehouse.evaluation(tid)
                    item = {
                        "trip_id": tid,
                        "status": row["status"],
                        "kind": row.get("kind"),
                        "source": "Warehouse",
                        "net": [ev.net_lo, ev.net_hi],
                        "accounting_status": "quoted_before_segment_allocation",
                    }
                except Exception:
                    item = {"trip_id": tid, "accounting_status": "planned_trip"}
            else:
                item = {"trip_id": tid, "accounting_status": "planned_trip"}
            trip_view.append(item)
        d["trip_details"] = trip_view
        planned = rec.payload.get("planned_trip_id")
        if planned and planned not in rec.referenced_trips:
            d["planned_trip_id"] = planned
        d["granted_slot_details"] = [
            self.slots[sid].to_dict() for sid in rec.granted_slots if sid in self.slots
        ]
        d["slot_ledger"] = self.list_slots()
        if rec.kind == STREET_RELEASE:
            if rec.status == STATUS_ACCEPTED:
                d["deliverable_segments"] = rec.segments
                d["deliverable_stalls"] = rec.stalls
            else:
                d["deliverable_segments"] = rec.evaluation.get(
                    "deliverable_segments",
                    rec.evaluation.get("robust_safe_segments", []),
                )
                d["deliverable_stalls"] = rec.evaluation.get(
                    "deliverable_stalls",
                    rec.evaluation.get("robust_safe_stalls", []),
                )
                d["recommended_partial"] = rec.evaluation.get("recommended_partial")
            actions: list[dict[str, Any]] = []
            if rec.payload.get("no_dump_order_id"):
                bid = rec.payload["no_dump_order_id"]
                ban = self.bans.get(bid, {})
                actions.append({"type": "NO_DUMP", "order_id": bid,
                                "active": ban.get("active", False),
                                "protected_segments": ban.get("protected_segments", []),
                                "destination_segment": ban.get("destination_segment"),
                                "slot_id": ban.get("slot_id")})
            if rec.payload.get("observation_id") or rec.payload.get("method") == METHOD_OBSERVE:
                obs_id = rec.payload.get("observation_id")
                obs = self.observations.get(obs_id, {}) if obs_id else {}
                actions.append({"type": "FIELD_OBSERVATION_BEFORE_FORK",
                                "observation_id": obs_id,
                                "before_fork": obs.get("before_fork",
                                                       rec.payload.get("observation_before_fork", True)),
                                "authoritative": True})
            if rec.payload.get("final_trip_id"):
                actions.append({"type": "ONE_WAY_FORK_DISPATCH",
                                "trip_id": rec.payload["final_trip_id"],
                                "capacity": rec.payload.get("capacity"),
                                "forecast_segment": rec.payload.get("forecast_segment"),
                                "one_way_no_return": True})
            d["critical_actions"] = actions
        return d

    get_order = query_order
    # 常见受理/查询命名别名。
    accept_release_order = submit_release_order
    accept_order = submit_release_order
    submit_order = submit_release_order

    def list_orders(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [r.to_dict() for oid, r in self.orders.items()
                if kind is None or r.kind == kind]

    def list_slots(self) -> list[dict[str, Any]]:
        """全部堆位及此前已经许出/租出的容量。"""

        return [s.to_dict() for s in self.slots.values()]

    def audit(self) -> dict[str, Any]:
        """全街复核视图：段、堆位、车次分配、订单和事件。"""

        recon = self.reconstruct()
        return {
            "segments": [s.to_dict() for s in self.segments.values()],
            "stalls": [s.to_dict() for s in self.stalls.values()],
            "slots": [s.to_dict() for s in self.slots.values()],
            "trips": {tid: t.to_dict() for tid, t in self.trips.items()},
            "reconstruction": recon.to_dict(),
            "orders": self.list_orders(),
            "events": _jsonable(self.events),
            "replays": _jsonable(self.replays),
        }


# 较短/近义命名，便于不同调用习惯。
StreetDesk = StreetOffice
StreetClearance = StreetOffice
StreetReleaseOrder = OrderRecord
