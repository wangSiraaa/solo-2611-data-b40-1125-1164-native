"""乘务出声台：读词库证据 → 真实渲染 → 听辨裁决 → 按合同签播/拒播/回听。

乘务台只做编排，不持有素材：整词/拼接片都从发布后的 Release 读取。
两家店（买断店、租用店）拿同一本资产目录、同一条临时改站，
许可与代价由各自合同兑现。
"""
from __future__ import annotations

from dataclasses import dataclass

from .contracts import ContractKind, CostLedger
from .hearing import HearingReport, Reason, adjudicate
from .lexicon import Release
from .render import (
    Rendering, SplicePlan, SpliceTable, WholeWordPlan, render_plan,
)
from .schedule import (
    BookingTable, PlayOrder, RecordingCalendar, Rejection, Schedule, Trip,
)


@dataclass
class DeskDecision:
    """一次办理结果：要么签播，要么带依据拒播（可附补录档期）。"""

    signed: bool
    trip_no: int
    target: str
    contract: str
    plan_kind: str
    asset_refs: tuple[str, ...]
    release_version: str
    reason: str
    detail: str
    hearing: HearingReport | None = None
    order: PlayOrder | None = None
    deferred_slot: int | None = None


class BroadcastDesk:
    def __init__(self, name: str, kind: ContractKind, schedule: Schedule,
                 splice_table: SpliceTable, booking: BookingTable,
                 ledger: CostLedger,
                 calendar: RecordingCalendar | None = None,
                 buyout_unit_price: int = 50,
                 rental_unit_price: int = 8,
                 owner_key: str = "self") -> None:
        if kind is ContractKind.BUYOUT and calendar is None:
            raise ValueError("买断店必须配置录音日历，补录要约新档期")
        self.name = name
        self.kind = kind
        self.schedule = schedule
        self.splice_table = splice_table
        self.booking = booking
        self.ledger = ledger
        self.calendar = calendar
        self.owner_key = owner_key
        self.buyout_unit_price = buyout_unit_price
        self.rental_unit_price = rental_unit_price
        # 买断店已锁住的整词母录：已签整段不得换尾。
        self._locked_masters: set[str] = set()

    # ----------------------------- 内部工具 ----------------------------- #

    def _reject(self, trip: Trip, reason: str, detail: str, plan_kind: str,
                asset_refs: tuple[str, ...], release: Release,
                hearing: HearingReport | None = None) -> DeskDecision:
        rejection = Rejection(
            trip_no=trip.no, target=trip.target, contract=self.kind.value,
            reason=reason, detail=detail, plan_kind=plan_kind,
            asset_refs=asset_refs, release_version=release.version,
            hearing=hearing,
        )
        self.schedule.reject(rejection)
        return DeskDecision(
            signed=False, trip_no=trip.no, target=trip.target,
            contract=self.kind.value, plan_kind=plan_kind,
            asset_refs=asset_refs, release_version=release.version,
            reason=reason, detail=detail, hearing=hearing,
        )

    def _sign(self, trip: Trip, release: Release, rendering: Rendering,
              hearing: HearingReport, action: str) -> DeskDecision:
        order = PlayOrder(
            trip_no=trip.no, direction=trip.direction, target=trip.target,
            contract=self.kind.value, release_version=release.version,
            plan_kind=rendering.plan_kind, asset_refs=rendering.asset_refs,
            pcm_snapshot=rendering.pcm, hearing=hearing,
        )
        self.schedule.sign(order)
        self.ledger.charge(
            trip.no, self.kind, action, rendering.asset_refs,
            self.rental_unit_price if self.kind is ContractKind.RENTAL
            else self.buyout_unit_price,
            note=f"{self.name} 实际出声 {list(rendering.pcm)}",
        )
        return DeskDecision(
            signed=True, trip_no=trip.no, target=trip.target,
            contract=self.kind.value, plan_kind=rendering.plan_kind,
            asset_refs=rendering.asset_refs, release_version=release.version,
            reason=Reason.OK, detail=hearing.reason, hearing=hearing,
            order=order,
        )

    # ----------------------------- 拼接出声 ----------------------------- #

    def evaluate_splice(self, release: Release, trip_no: int,
                        head_id: str, tail_id: str) -> DeskDecision:
        """办理一个两片段拼接候选（租用店主用；买断店短单元受限）。"""
        trip = self.schedule.trips[trip_no]
        refs = (head_id, tail_id)
        for asset_id in refs:  # 必须来自同一份发版词库
            if asset_id not in release.assets:
                return self._reject(
                    trip, Reason.UNPUBLISHED, f"资产 {asset_id} 未在发版词库",
                    "splice", refs, release,
                )
        # 接头表没有此接头 → 禁止重剪，直接拒
        try:
            joint = self.splice_table.get(head_id, tail_id)
        except Exception as exc:
            return self._reject(
                trip, "无接头/禁止重剪", str(exc), "splice", refs, release,
            )
        if self.kind is ContractKind.BUYOUT:
            return self._reject(
                trip, "合同不许可",
                "买断店只受理整词录音，短单元拼接不在买断合同范围",
                "splice", refs, release,
            )
        if self.booking.is_held_by_other(tail_id, self.owner_key):
            holder = self.booking.snapshot()[tail_id]
            return self._reject(
                trip, "短单元已被独占",
                f"尾字 {tail_id} 已被 {holder[1]} 班（{holder[0]}）独占，"
                "本班不能借；须另找接头或留待补录",
                "splice", refs, release,
            )
        plan = SplicePlan(head_id=head_id, tail_id=tail_id, overlap=joint.overlap)
        rendering = render_plan(release, plan, self.splice_table)
        hearing = adjudicate(
            release, rendering, trip.target, list(trip.neighbors),
            trip.window_samples,
        )
        if not hearing.can_air:
            return self._reject(
                trip, hearing.reason,
                f"渲染={list(rendering.pcm)}，候选站命中={list(hearing.station_hits)}",
                "splice", refs, release, hearing,
            )
        # 租用：尾字跨班复用，本次接头与听辨结论对应本次实际出声
        self.booking.take(tail_id, trip_no, owner=self.owner_key)
        return self._sign(trip, release, rendering, hearing, "rental_use")

    # ----------------------------- 整词出声 ----------------------------- #

    def evaluate_whole_word(self, release: Release, trip_no: int,
                            asset_id: str) -> DeskDecision:
        """办理一个整词母录候选。"""
        trip = self.schedule.trips[trip_no]
        refs = (asset_id,)
        if asset_id not in release.assets:
            return self._reject(
                trip, Reason.UNPUBLISHED, f"资产 {asset_id} 未在发版词库",
                "whole_word", refs, release,
            )
        asset = release.assets[asset_id]
        if asset.kind != "whole_word":
            return self._reject(
                trip, "资产种类不符", f"{asset_id} 不是整词母录",
                "whole_word", refs, release,
            )
        plan = WholeWordPlan(asset_id=asset_id)
        rendering = render_plan(release, plan, self.splice_table)
        hearing = adjudicate(
            release, rendering, trip.target, list(trip.neighbors),
            trip.window_samples,
        )
        if not hearing.can_air:
            if self.kind is ContractKind.BUYOUT:
                # 买断店受理“档期允许时补录后再播”的订单
                slot = self.calendar.earliest_available()
                if slot > trip_no:
                    self.ledger.charge(
                        trip.no, self.kind, "deferred_order", refs,
                        self.buyout_unit_price,
                        note=f"{asset_id} 当前不可播，约新录音档 {slot} 补录后再播",
                    )
                    return DeskDecision(
                        signed=False, trip_no=trip.no, target=trip.target,
                        contract=self.kind.value, plan_kind="whole_word",
                        asset_refs=refs, release_version=release.version,
                        reason=hearing.reason,
                        detail=f"当前依据：{hearing.reason}；已约新录音档 {slot}",
                        hearing=hearing, deferred_slot=slot,
                    )
            return self._reject(
                trip, hearing.reason,
                f"渲染={list(rendering.pcm)}，候选站命中={list(hearing.station_hits)}",
                "whole_word", refs, release, hearing,
            )
        if self.kind is ContractKind.BUYOUT:
            if asset_id in self._locked_masters:
                return self._reject(
                    trip, "整词已买断锁定",
                    f"{asset_id} 已签整段锁定，不得换尾；补录要约新录音档",
                    "whole_word", refs, release, hearing,
                )
            decision = self._sign(trip, release, rendering, hearing, "buyout_lock")
            self._locked_masters.add(asset_id)
            return decision
        # 租用店不买断整词，按次租用整词母录
        return self._sign(trip, release, rendering, hearing, "rental_use")

    def book_new_recording_slot(self) -> int:
        """买断店补录约一个新的录音档。"""
        return self.calendar.book_earliest()

    # ------------------------------- 回听 ------------------------------- #

    def replay(self, trip_no: int) -> PlayOrder:
        """旧播次按原音回听（即便词库后来改版，听到的仍是签播时 PCM）。"""
        return self.schedule.replay(trip_no)
