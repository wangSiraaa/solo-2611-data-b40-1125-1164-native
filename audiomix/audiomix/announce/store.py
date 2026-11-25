"""工作区 JSON 持久化：乘务员可随时落盘、重开后继续办理与回听。

状态全部由纯数据类构成，序列化为单个 state.json；词库发版快照、
已签播次的 PCM 快照、拒播依据、代价台账均原样保存，因此重开后
旧播次仍按原音回听，且能继续在最新发版上办理新班。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict

from .contracts import ContractKind, CostLedger, LedgerEntry
from .desk import BroadcastDesk
from .lexicon import (
    Asset, AssetRevision, DerivationSpec, Lexicon, Release,
)
from .render import Joint, SpliceTable
from .schedule import (
    BookingTable, PlayOrder, RecordingCalendar, Rejection, Schedule, Trip,
)
from .templates import CharacterTemplate, StationTemplate


def _asset_to_json(a: Asset) -> dict:
    return {
        "id": a.id, "kind": a.kind, "speaker": a.speaker,
        "pcm": list(a.pcm), "label": a.label, "master_id": a.master_id,
        "derivation": asdict(a.derivation) if a.derivation else None,
        "current_seq": a.current_seq,
        "revisions": [asdict(r) for r in a.revisions], "frozen": a.frozen,
    }


def _asset_from_json(d: dict) -> Asset:
    return Asset(
        id=d["id"], kind=d["kind"], speaker=d["speaker"],
        pcm=tuple(d["pcm"]), label=d["label"], master_id=d["master_id"],
        derivation=DerivationSpec(**d["derivation"]) if d["derivation"] else None,
        current_seq=d["current_seq"],
        revisions=tuple(AssetRevision(**r) for r in d["revisions"]),
        frozen=d["frozen"],
    )


def _release_to_json(rel: Release) -> dict:
    return {
        "version": rel.version, "note": rel.note,
        "assets": {k: _asset_to_json(a) for k, a in rel.assets.items()},
        "character_templates": [asdict(t) for t in rel.character_templates],
        "station_templates": [asdict(t) for t in rel.station_templates],
    }


def _release_from_json(d: dict) -> Release:
    return Release(
        version=d["version"], note=d["note"],
        assets={k: _asset_from_json(a) for k, a in d["assets"].items()},
        character_templates=tuple(
            CharacterTemplate(**t) for t in d["character_templates"]
        ),
        station_templates=tuple(
            StationTemplate(**{**t, "reference": tuple(t["reference"])})
            for t in d["station_templates"]
        ),
    )


def _hearing_to_json(h) -> dict:
    return {
        "target": h.target, "neighbors": list(h.neighbors),
        "window_samples": h.window_samples,
        "rendered_samples": h.rendered_samples,
        "station_hits": list(h.station_hits), "checked": [list(c) for c in h.checked],
        "window_ok": h.window_ok, "can_air": h.can_air, "reason": h.reason,
        "rendered_pcm": list(h.rendered_pcm),
    }


def _hearing_from_json(d: dict):
    from .hearing import HearingReport
    return HearingReport(
        target=d["target"], neighbors=tuple(d["neighbors"]),
        window_samples=d["window_samples"],
        rendered_samples=d["rendered_samples"],
        station_hits=tuple(d["station_hits"]),
        checked=tuple((c[0], c[1]) for c in d["checked"]),
        window_ok=d["window_ok"], can_air=d["can_air"], reason=d["reason"],
        rendered_pcm=tuple(d["rendered_pcm"]),
    )


def _order_to_json(o: PlayOrder) -> dict:
    return {
        "trip_no": o.trip_no, "direction": o.direction, "target": o.target,
        "contract": o.contract, "release_version": o.release_version,
        "plan_kind": o.plan_kind, "asset_refs": list(o.asset_refs),
        "pcm_snapshot": list(o.pcm_snapshot),
        "hearing": _hearing_to_json(o.hearing),
    }


def _order_from_json(d: dict) -> PlayOrder:
    return PlayOrder(
        trip_no=d["trip_no"], direction=d["direction"], target=d["target"],
        contract=d["contract"], release_version=d["release_version"],
        plan_kind=d["plan_kind"], asset_refs=tuple(d["asset_refs"]),
        pcm_snapshot=tuple(d["pcm_snapshot"]),
        hearing=_hearing_from_json(d["hearing"]),
    )


def _rejection_to_json(r: Rejection) -> dict:
    return {
        "trip_no": r.trip_no, "target": r.target, "contract": r.contract,
        "reason": r.reason, "detail": r.detail, "plan_kind": r.plan_kind,
        "asset_refs": list(r.asset_refs), "release_version": r.release_version,
        "hearing": _hearing_to_json(r.hearing) if r.hearing else None,
    }


def _rejection_from_json(d: dict) -> Rejection:
    return Rejection(
        trip_no=d["trip_no"], target=d["target"], contract=d["contract"],
        reason=d["reason"], detail=d["detail"], plan_kind=d["plan_kind"],
        asset_refs=tuple(d["asset_refs"]), release_version=d["release_version"],
        hearing=_hearing_from_json(d["hearing"]) if d["hearing"] else None,
    )


class WorkspaceStore:
    """把整套词库/排期/双店状态读写到一个目录。"""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.path = os.path.join(self.root, "state.json")

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def save(self, lexicon: Lexicon, splice_table: SpliceTable,
             schedule: Schedule, booking: BookingTable,
             rental_ledger: CostLedger, buyout_ledger: CostLedger,
             rental_desk: BroadcastDesk, buyout_desk: BroadcastDesk) -> None:
        state = {
            "lexicon": {
                "assets": {k: _asset_to_json(a) for k, a in lexicon.assets.items()},
                "character_templates": [
                    asdict(t) for t in lexicon.character_templates
                ],
                "station_templates": [
                    {**asdict(t), "reference": list(t.reference)}
                    for t in lexicon.station_templates
                ],
                "releases": {k: _release_to_json(r)
                             for k, r in lexicon.releases.items()},
                "next_release_no": lexicon._next_release_no,
                "sample_budget": lexicon.sample_budget,
                "recorded_samples": lexicon.recorded_samples,
            },
            "splice_table": {
                "allowed_overlap": splice_table.allowed_overlap,
                "joints": [asdict(j) for j in splice_table.entries()],
            },
            "schedule": {
                "trips": [asdict(t) for t in schedule.trips.values()],
                "orders": [_order_to_json(o) for o in schedule.orders.values()],
                "rejections": [_rejection_to_json(r) for r in schedule.rejections],
            },
            "booking": booking.snapshot(),
            "ledgers": {
                "rental": {"name": rental_ledger.name,
                           "entries": [asdict(e) for e in rental_ledger.entries]},
                "buyout": {"name": buyout_ledger.name,
                           "entries": [asdict(e) for e in buyout_ledger.entries]},
            },
            "calendars": {
                "buyout": list(buyout_desk.calendar.slots()),
            },
            "locks": sorted(buyout_desk._locked_masters),
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def load(self) -> dict:
        """重开工作区，重建词库、排期与两家乘务台。"""
        with open(self.path, encoding="utf-8") as f:
            state = json.load(f)

        lex = Lexicon(sample_budget=state["lexicon"]["sample_budget"])
        lex.character_templates = [
            CharacterTemplate(**t) for t in state["lexicon"]["character_templates"]
        ]
        lex.station_templates = [
            StationTemplate(station=t["station"], reference=tuple(t["reference"]))
            for t in state["lexicon"]["station_templates"]
        ]
        lex.assets = {k: _asset_from_json(a)
                      for k, a in state["lexicon"]["assets"].items()}
        lex.releases = {k: _release_from_json(r)
                        for k, r in state["lexicon"]["releases"].items()}
        lex._next_release_no = state["lexicon"]["next_release_no"]
        lex.recorded_samples = state["lexicon"]["recorded_samples"]

        splice_table = SpliceTable(
            joints=[Joint(**j) for j in state["splice_table"]["joints"]],
            allowed_overlap=state["splice_table"]["allowed_overlap"],
        )

        schedule = Schedule()
        for t in state["schedule"]["trips"]:
            schedule.add_trip(Trip(**{**t, "neighbors": tuple(t["neighbors"])}))
        schedule.orders = {
            o["trip_no"]: _order_from_json(o) for o in state["schedule"]["orders"]
        }
        schedule.rejections = [
            _rejection_from_json(r) for r in state["schedule"]["rejections"]
        ]

        booking = BookingTable()
        for asset_id, (owner, trip_no) in state["booking"].items():
            if owner == "self":
                booking.take(asset_id, trip_no, owner=owner)
            else:
                booking.grant_exclusive(asset_id, trip_no, owner=owner)

        def _ledger(key: str) -> CostLedger:
            raw = state["ledgers"][key]
            return CostLedger(
                name=raw["name"],
                entries=[LedgerEntry(**e) for e in raw["entries"]],
            )

        rental_ledger = _ledger("rental")
        buyout_ledger = _ledger("buyout")
        calendar = RecordingCalendar(state["calendars"]["buyout"])

        rental_desk = BroadcastDesk(
            "租用店", ContractKind.RENTAL, schedule, splice_table,
            booking, rental_ledger,
        )
        buyout_desk = BroadcastDesk(
            "买断店", ContractKind.BUYOUT, schedule, splice_table,
            booking, buyout_ledger, calendar=calendar,
        )
        buyout_desk._locked_masters = set(state["locks"])
        return {
            "lexicon": lex,
            "splice_table": splice_table,
            "schedule": schedule,
            "booking": booking,
            "rental_ledger": rental_ledger,
            "buyout_ledger": buyout_ledger,
            "rental_desk": rental_desk,
            "buyout_desk": buyout_desk,
        }
