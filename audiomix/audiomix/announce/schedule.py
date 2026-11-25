"""班次、广播窗、独占预约与录音档期。

- Trip 是一次按方向的播音任务，带相邻站名单与允许占用的广播窗。
- BookingTable 记录短单元被哪一班独占；被独占的尾字其他班次不能借用。
- RecordingCalendar 是整词母录的可录档期；买断店补录只能约新档期。
- PlayOrder 是已签播次的不可变凭证（含实际 PCM 快照，供按原音回听）；
  Rejection 保存缺录档/误听/窗不足/独占等拒播依据。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .hearing import HearingReport
from .pcm import PCM


class ScheduleError(ValueError):
    """排期冲突或班次状态不合法。"""


@dataclass(frozen=True)
class Trip:
    no: int
    direction: str
    target: str
    neighbors: tuple[str, ...]
    window_samples: int
    departed: bool = False  # 车已发出：其播次冻结，只可回听不可改


@dataclass(frozen=True)
class PlayOrder:
    """已签播次：实际出声的全部证据，PCM 快照保证按原音回听。"""

    trip_no: int
    direction: str
    target: str
    contract: str
    release_version: str
    plan_kind: str
    asset_refs: tuple[str, ...]
    pcm_snapshot: PCM
    hearing: HearingReport


@dataclass(frozen=True)
class Rejection:
    trip_no: int
    target: str
    contract: str
    reason: str
    detail: str
    plan_kind: str
    asset_refs: tuple[str, ...]
    release_version: str
    hearing: HearingReport | None = None


class BookingTable:
    """短单元预约：记录每个短单元当前被哪一店的哪一班占用。

    租用合同下尾字可在同一店内跨班复用；只有被“另一班”（别家店/别的
    排期线）独占时，本店主才不能借。
    """

    def __init__(self) -> None:
        # asset_id -> (占用方 owner_key, 班次号)
        self._holds: dict[str, tuple[str, int]] = {}

    def take(self, asset_id: str, trip_no: int, owner: str = "self") -> None:
        existing = self._holds.get(asset_id)
        if existing is not None and existing[0] != owner:
            raise ScheduleError(
                f"短单元 {asset_id} 已被 {existing[1]} 班（{existing[0]}）独占，"
                f"{trip_no} 班不能借"
            )
        self._holds[asset_id] = (owner, trip_no)

    def grant_exclusive(self, asset_id: str, trip_no: int, owner: str = "other") -> None:
        """登记另一班对短单元的独占（本店此后不能再借）。"""
        self._holds[asset_id] = (owner, trip_no)

    def release(self, asset_id: str) -> None:
        self._holds.pop(asset_id, None)

    def is_held_by_other(self, asset_id: str, owner: str = "self") -> bool:
        existing = self._holds.get(asset_id)
        return existing is not None and existing[0] != owner

    def snapshot(self) -> dict[str, list]:
        return {k: list(v) for k, v in self._holds.items()}


class RecordingCalendar:
    """整词母录的可录档期（按序的档期序号，越早越小）。"""

    def __init__(self, available_slots: list[int]) -> None:
        self._slots = sorted(available_slots)

    def earliest_available(self) -> int:
        if not self._slots:
            raise ScheduleError("录音日历已无可录档期")
        return self._slots[0]

    def book_earliest(self) -> int:
        if not self._slots:
            raise ScheduleError("录音日历已无可录档期")
        return self._slots.pop(0)

    def slots(self) -> tuple[int, ...]:
        return tuple(self._slots)


@dataclass
class Schedule:
    trips: dict[int, Trip] = field(default_factory=dict)
    orders: dict[int, PlayOrder] = field(default_factory=dict)
    rejections: list[Rejection] = field(default_factory=list)

    def add_trip(self, trip: Trip) -> None:
        if trip.no in self.trips:
            raise ScheduleError(f"班次号重复：{trip.no}")
        self.trips[trip.no] = trip

    def sign(self, order: PlayOrder) -> None:
        trip = self.trips[order.trip_no]
        if trip.departed:
            raise ScheduleError(f"{trip.no} 班已发车，不能再签播")
        self.orders[order.trip_no] = order

    def reject(self, rejection: Rejection) -> None:
        self.rejections.append(rejection)

    def mark_departed(self, trip_no: int) -> None:
        trip = self.trips[trip_no]
        self.trips[trip_no] = Trip(
            no=trip.no, direction=trip.direction, target=trip.target,
            neighbors=trip.neighbors, window_samples=trip.window_samples,
            departed=True,
        )

    def replay(self, trip_no: int) -> PlayOrder:
        """车已发出的旧播次按原音回听（读的是签播时的 PCM 快照）。"""
        try:
            return self.orders[trip_no]
        except KeyError as exc:
            raise ScheduleError(f"{trip_no} 班没有已签播次") from exc
