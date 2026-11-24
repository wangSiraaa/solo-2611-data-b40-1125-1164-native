"""销模核销证据仓。

这个模块不猜身份：稳定序列号必须来自装箱时的身份记录或见证人在破碎前的现场
指认；只写“左瓣/甲瓣”的标签只是标签。称量区间、箱/芯皮重、封号交接和破碎台
出渣记录用于约束“哪些实体瓣的组合在物理上成立”。

判定采用保守的可能世界语义：

* 每条身份不明的装箱记录在候选实体中枚举；
* 同一实体不能同时装进两个箱；
* 每个可能世界都必须满足已确认车次的净重区间和破碎出渣区间；
* 只有在 **所有** 可行世界中都已破碎的实体，才进入“确定销毁”集合；
* 作品只有在所有可行世界中都不存在可凑齐的整模时，才允许开销模回单。

这样，一甲两乙中唯一甲被证明入炉即可销账；只证明碎了一个身份不明的乙时，
仍有一个乙可与甲配模，回单会被拒绝并指出剩余组合。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable, Sequence

from .warehouse import STATUS_CONFIRMED, Warehouse

ISSUED = "ISSUED"
WITHHELD = "WITHHELD"
ISSUABLE = "ISSUABLE"

ROUTE_WORKSHOP_SEALED = "WORKSHOP_SEALED"
ROUTE_ON_SITE_WITNESS = "ON_SITE_WITNESS"

BASIS_SERIAL = "serial"
BASIS_TAG = "tag"
BASIS_PACKING_WITNESS = "packing_witness"
BASIS_ON_SITE = "on_site_witness"
VALID_IDENTITY_BASIS = {BASIS_SERIAL, BASIS_TAG, BASIS_PACKING_WITNESS, BASIS_ON_SITE}

SCHEMA = """
CREATE TABLE IF NOT EXISTS mold_record (
    kind    TEXT NOT NULL,
    rid     TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (kind, rid)
);

CREATE TABLE IF NOT EXISTS mold_receipt (
    receipt_id    TEXT PRIMARY KEY,
    work_id       TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_order INTEGER NOT NULL,
    payload       TEXT NOT NULL
);
"""


class MoldError(ValueError):
    """销模模块的输入、证据或状态错误。"""


class ReceiptWithheld(MoldError):
    """证据不足以签署销模回单。"""

    def __init__(self, verdict: "MoldVerdict") -> None:
        self.verdict = verdict
        super().__init__(
            f"write-off receipt for {verdict.work_id} withheld: "
            + "; ".join(verdict.issues[:5] or ["a complete mold can remain"])
        )


def _range(value: Any, lo: Any = None, hi: Any = None, *, name: str = "range") -> tuple[int, int]:
    """把整数、``(lo, hi)`` 或字典统一成闭整数区间。"""
    if value is not None and (lo is not None or hi is not None):
        raise MoldError(f"{name}: pass either a range value or lo/hi, not both")
    if value is not None:
        if isinstance(value, dict):
            lo = value.get("lo", value.get("weight_lo", value.get("from")))
            hi = value.get("hi", value.get("weight_hi", value.get("to")))
        elif isinstance(value, (tuple, list)) and len(value) == 2:
            lo, hi = value
        else:
            lo = hi = value
    if lo is None or hi is None:
        raise MoldError(f"{name} requires lo and hi")
    if isinstance(lo, bool) or isinstance(hi, bool):
        raise MoldError(f"{name} must be numeric, not bool")
    try:
        lo_i, hi_i = int(lo), int(hi)
    except (TypeError, ValueError) as exc:
        raise MoldError(f"{name} must contain integers") from exc
    if lo_i < 0 or hi_i < 0 or lo_i > hi_i:
        raise MoldError(f"{name} must satisfy 0 <= lo <= hi, got {lo_i},{hi_i}")
    return lo_i, hi_i


def _fraction(value: Any, *, name: str) -> Fraction:
    if isinstance(value, Fraction):
        return value
    try:
        return Fraction(value)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise MoldError(f"{name} must be a rational number") from exc


def _overlap(lo1: Fraction | int, hi1: Fraction | int,
             lo2: Fraction | int, hi2: Fraction | int) -> bool:
    return max(lo1, lo2) <= min(hi1, hi2)


def _weight_sum(ids: Iterable[str], pieces: dict[str, dict[str, Any]]) -> tuple[int, int]:
    lo = hi = 0
    for pid in ids:
        p = pieces[pid]
        lo += int(p["weight_lo"])
        hi += int(p["weight_hi"])
    return lo, hi


@dataclass(frozen=True)
class MoldVerdict:
    work_id: str
    status: str
    issuable: bool
    feasible_worlds: int
    certain_destroyed_ids: tuple[str, ...]
    possible_destroyed_ids: tuple[str, ...]
    remaining_molds: tuple[dict[str, Any], ...]
    world_destroyed_sets: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    batch_reports: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    issues: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "status": self.status,
            "issuable": self.issuable,
            "feasible_worlds": self.feasible_worlds,
            "certain_destroyed_ids": list(self.certain_destroyed_ids),
            "possible_destroyed_ids": list(self.possible_destroyed_ids),
            "remaining_molds": [dict(x) for x in self.remaining_molds],
            "world_destroyed_sets": [list(x) for x in self.world_destroyed_sets],
            "batch_reports": [dict(x) for x in self.batch_reports],
            "issues": list(self.issues),
            "evidence_refs": json.loads(json.dumps(self.evidence_refs, ensure_ascii=False)),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WriteOffReceipt:
    receipt_id: str
    work_id: str
    status: str
    client: str
    signatory: str
    issued_at: int | None
    route: str
    verdict: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "work_id": self.work_id,
            "status": self.status,
            "client": self.client,
            "signatory": self.signatory,
            "issued_at": self.issued_at,
            "route": self.route,
            "verdict": self.verdict,
        }


class MoldWriteOffLedger:
    """挂在 :class:`~weighbridge.Warehouse` 上的销模核销入口。

    也可不接磅单仓独立运行；此时车次必须直接给已确认净重区间。
    """

    def __init__(self, warehouse: Warehouse | str | None = None, path: str = ":memory:") -> None:
        if isinstance(warehouse, str):
            path, warehouse = warehouse, None
        self.warehouse = warehouse
        if warehouse is not None:
            self.conn = warehouse.conn
        else:
            self.conn = sqlite3.connect(path)
            self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

        self.pieces: dict[str, dict[str, Any]] = {}
        self.molds: dict[str, dict[str, Any]] = {}
        self.boxes: dict[str, dict[str, Any]] = {}
        self.lines: list[dict[str, Any]] = []
        self.shipments: dict[str, dict[str, Any]] = {}
        self.witnesses: dict[str, dict[str, Any]] = {}
        self.signatures: dict[str, dict[str, Any]] = {}
        self.openings: dict[str, dict[str, Any]] = {}
        self.batches: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, WriteOffReceipt] = {}
        self._line_seq = 0
        self._load_records()

    def _load_records(self) -> None:
        """从同一 SQLite 事实仓恢复证据；内存字典只是这些落盘记录的索引。"""
        try:
            rows = self.conn.execute("SELECT kind, rid, payload FROM mold_record").fetchall()
        except sqlite3.OperationalError:
            return
        for row in rows:
            kind, rid, raw = row["kind"], row["rid"], row["payload"]
            data = json.loads(raw)
            if kind == "piece":
                self.pieces[rid] = data
            elif kind == "mold":
                self.molds[rid] = data
            elif kind == "box":
                self.boxes[rid] = data
            elif kind == "line":
                self.lines.append(data)
                suffix = "".join(ch for ch in str(rid) if ch.isdigit())
                self._line_seq = max(self._line_seq, int(suffix or len(self.lines)))
            elif kind == "shipment":
                self.shipments[rid] = data
            elif kind == "witness":
                self.witnesses[rid] = data
            elif kind == "signature":
                self.signatures[rid] = data
            elif kind == "opening":
                self.openings[rid] = data
            elif kind == "batch":
                self.batches[rid] = data
        for row in self.conn.execute("SELECT payload FROM mold_receipt").fetchall():
            data = json.loads(row["payload"])
            self.receipts[data["receipt_id"]] = WriteOffReceipt(**data)

    # ------------------------------------------------------------- generic IO
    def _put_record(self, kind: str, rid: str, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        row = self.conn.execute(
            "SELECT payload FROM mold_record WHERE kind=? AND rid=?", (kind, rid)
        ).fetchone()
        if row is not None:
            old = row["payload"] if not isinstance(row, sqlite3.Row) else row["payload"]
            if old != raw:
                raise MoldError(f"{kind} {rid} already exists with different evidence")
            return
        self.conn.execute(
            "INSERT INTO mold_record(kind, rid, payload) VALUES (?,?,?)",
            (kind, rid, raw),
        )

    def snapshot(self) -> dict[str, Any]:
        """返回可用于单变量反事实改动的深拷贝数据。"""
        return json.loads(json.dumps({
            "pieces": list(self.pieces.values()),
            "molds": list(self.molds.values()),
            "boxes": list(self.boxes.values()),
            "lines": self.lines,
            "shipments": list(self.shipments.values()),
            "witnesses": list(self.witnesses.values()),
            "signatures": list(self.signatures.values()),
            "openings": list(self.openings.values()),
            "batches": list(self.batches.values()),
        }, ensure_ascii=False, default=str))

    # ------------------------------------------------------------- inventory
    def add_piece(
        self,
        piece_id: str | dict[str, Any],
        work_id: str | None = None,
        role: str | None = None,
        weight: Any = None,
        *,
        weight_lo: int | None = None,
        weight_hi: int | None = None,
        labels: Sequence[str] = (),
        workshop_sealable: bool = True,
        **extra: Any,
    ) -> dict[str, Any]:
        if isinstance(piece_id, dict):
            data = dict(piece_id)
            pid = str(data.pop("piece_id") or data.pop("id"))
            return self.add_piece(pid, **data)
        pid = str(piece_id)
        if not pid:
            raise MoldError("piece_id must be non-empty")
        if pid in self.pieces:
            raise MoldError(f"piece {pid} already exists")
        if work_id is None or role is None:
            raise MoldError(f"piece {pid} needs work_id and role")
        lo, hi = _range(weight, weight_lo, weight_hi, name=f"weight for {pid}")
        p = {
            "piece_id": pid,
            "work_id": str(work_id),
            "role": str(role),
            "weight_lo": lo,
            "weight_hi": hi,
            "labels": [str(x) for x in labels],
            "workshop_sealable": bool(workshop_sealable),
            **extra,
        }
        self.pieces[pid] = p
        self._put_record("piece", pid, p)
        return p

    def add_mold(
        self,
        mold_id: str | dict[str, Any],
        work_id: str | None = None,
        required_piece_ids: Sequence[str] | None = None,
        slots: dict[str, Any] | Sequence[dict[str, Any]] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        if isinstance(mold_id, dict):
            data = dict(mold_id)
            mid = str(data.pop("mold_id") or data.pop("id"))
            return self.add_mold(mid, **data)
        mid = str(mold_id)
        if mid in self.molds:
            raise MoldError(f"mold {mid} already exists")
        if work_id is None:
            raise MoldError(f"mold {mid} needs work_id")

        norm_slots: list[dict[str, Any]] = []
        if required_piece_ids:
            ids = [str(x) for x in required_piece_ids]
            for pid in ids:
                if pid not in self.pieces:
                    raise MoldError(f"mold {mid} references unknown piece {pid}")
            # 固定清单中的每个 ID 是一个独立槽位。
            counts: dict[str, int] = {}
            for pid in ids:
                slot = f"required-{pid}"
                norm_slots.append({"slot": slot, "piece_ids": [pid]})
        if slots is not None:
            if isinstance(slots, dict):
                iterable: Sequence[Any] = [
                    {"slot": name, **(spec if isinstance(spec, dict) else {"role": spec} if isinstance(spec, str) else {"piece_ids": list(spec)})}
                    for name, spec in slots.items()
                ]
            else:
                iterable = list(slots)
            for i, s in enumerate(iterable):
                s = dict(s)
                name = str(s.pop("slot", f"slot{i}"))
                piece_ids = s.get("piece_ids")
                if piece_ids is not None:
                    piece_ids = [str(x) for x in piece_ids]
                    for pid in piece_ids:
                        if pid not in self.pieces:
                            raise MoldError(f"mold {mid} slot {name} references unknown piece {pid}")
                elif not s.get("role"):
                    raise MoldError(f"mold {mid} slot {name} needs role or piece_ids")
                norm_slots.append({
                    "slot": name,
                    "role": s.get("role"),
                    "work_id": str(s.get("work_id", work_id)),
                    "piece_ids": piece_ids,
                })
        if not norm_slots:
            raise MoldError(f"mold {mid} needs required_piece_ids or slots")
        m = {"mold_id": mid, "work_id": str(work_id), "slots": norm_slots, **extra}
        self.molds[mid] = m
        self._put_record("mold", mid, m)
        return m

    # ------------------------------------------------------------- packing
    def seal_box(
        self,
        box_id: str | dict[str, Any],
        seal_id: str | None = None,
        tare: Any = None,
        *,
        tare_lo: int | None = None,
        tare_hi: int | None = None,
        box_tare: Any = None,
        core_tare: Any = None,
        tare_ref: str | None = None,
        tare_components: dict[str, Any] | None = None,
        packing_witness_id: str | None = None,
        packed_at: int | None = None,
        at_workshop: bool = True,
        identity_mode: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        if isinstance(box_id, dict):
            data = dict(box_id)
            bid = str(data.pop("box_id") or data.pop("id"))
            return self.seal_box(bid, **data)
        bid = str(box_id)
        if bid in self.boxes:
            raise MoldError(f"box {bid} already exists")
        if seal_id is None:
            raise MoldError(f"box {bid} needs a seal_id")
        for other in self.boxes.values():
            if other["seal_id"] == seal_id:
                raise MoldError(f"seal {seal_id} is already used by box {other['box_id']}")
        if box_tare is not None or core_tare is not None:
            blo, bhi = _range(box_tare if box_tare is not None else 0, name=f"box tare for {bid}")
            clo, chi = _range(core_tare if core_tare is not None else 0, name=f"core tare for {bid}")
            lo, hi = blo + clo, bhi + chi
        else:
            lo, hi = _range(tare, tare_lo, tare_hi, name=f"tare for {bid}")
        mode = identity_mode or (
            ROUTE_WORKSHOP_SEALED if at_workshop else ROUTE_ON_SITE_WITNESS
        )
        if mode not in {ROUTE_WORKSHOP_SEALED, ROUTE_ON_SITE_WITNESS}:
            raise MoldError(f"box {bid} has unknown identity_mode {mode!r}")
        b = {
            "box_id": bid,
            "seal_id": str(seal_id),
            "identity_mode": mode,
            "tare_lo": lo,
            "tare_hi": hi,
            "tare_ref": tare_ref or f"TARE-{bid}",
            "tare_components": {
                **dict(tare_components or {}),
                **({"box_tare": [blo, bhi]} if box_tare is not None else {}),
                **({"core_tare": [clo, chi]} if core_tare is not None else {}),
            },
            "packing_witness_id": packing_witness_id,
            "packed_at": packed_at,
            "at_workshop": bool(at_workshop),
            **extra,
        }
        self.boxes[bid] = b
        self._put_record("box", bid, b)
        return b

    # 语义别名：混箱也可“建档/封箱”，但不要求工场可封性。
    add_box = seal_box

    def pack_line(
        self,
        box_id: str | dict[str, Any],
        label: str | None = None,
        *,
        line_id: str | None = None,
        role: str | None = None,
        work_id: str | None = None,
        piece_id: str | None = None,
        identified_piece_id: str | None = None,
        identity_basis: str | None = None,
        candidate_ids: Sequence[str] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        if isinstance(box_id, dict):
            data = dict(box_id)
            return self.pack_line(**data)
        bid = str(box_id)
        if bid not in self.boxes:
            raise MoldError(f"unknown box {bid}")
        self._line_seq += 1
        lid = str(line_id or f"LINE{self._line_seq:04d}")
        if any(x["line_id"] == lid for x in self.lines):
            raise MoldError(f"packing line {lid} already exists")
        fixed = identified_piece_id or piece_id
        if fixed is not None:
            fixed = str(fixed)
            if fixed not in self.pieces:
                raise MoldError(f"line {lid} identifies unknown piece {fixed}")
            basis = identity_basis or BASIS_SERIAL
            if basis not in VALID_IDENTITY_BASIS:
                raise MoldError(f"invalid identity_basis {basis!r} for {fixed}")
        else:
            basis = None
        cands = None
        if candidate_ids is not None:
            cands = [str(x) for x in candidate_ids]
            for pid in cands:
                if pid not in self.pieces:
                    raise MoldError(f"line {lid} references unknown candidate piece {pid}")
        line = {
            "line_id": lid,
            "box_id": bid,
            "label": None if label is None else str(label),
            "role": None if role is None else str(role),
            "work_id": None if work_id is None else str(work_id),
            "identified_piece_id": fixed,
            "identity_basis": basis,
            "candidate_ids": cands,
            **extra,
        }
        self.lines.append(line)
        self._put_record("line", lid, line)
        return line

    # ------------------------------------------------------------- witnesses
    def add_witness(
        self,
        witness_id: str | dict[str, Any],
        name: str = "",
        *,
        kind: str = "site",
        valid_from: int | None = None,
        valid_to: int | None = None,
        saw_box_ids: Sequence[str] = (),
        **extra: Any,
    ) -> dict[str, Any]:
        if isinstance(witness_id, dict):
            data = dict(witness_id)
            wid = str(data.pop("witness_id") or data.pop("id"))
            return self.add_witness(wid, **data)
        wid = str(witness_id)
        if wid in self.witnesses:
            raise MoldError(f"witness {wid} already exists")
        if valid_from is not None and valid_to is not None and valid_from > valid_to:
            raise MoldError(f"witness {wid} has invalid availability window")
        saw = [str(x) for x in saw_box_ids]
        for bid in saw:
            if bid not in self.boxes:
                raise MoldError(f"witness {wid} saw unknown box {bid}")
        w = {
            "witness_id": wid,
            "name": name,
            "kind": str(kind),
            "valid_from": valid_from,
            "valid_to": valid_to,
            "saw_box_ids": saw,
            **extra,
        }
        self.witnesses[wid] = w
        self._put_record("witness", wid, w)
        return w

    def witness_sees_box(self, witness_id: str, box_id: str) -> None:
        if witness_id not in self.witnesses:
            raise MoldError(f"unknown witness {witness_id}")
        if box_id not in self.boxes:
            raise MoldError(f"unknown box {box_id}")
        saw = self.witnesses[witness_id]["saw_box_ids"]
        if box_id not in saw:
            saw.append(box_id)

    def sign_boxes(
        self,
        signature_id: str,
        witness_id: str,
        box_ids: Sequence[str],
        signed_at: int,
    ) -> dict[str, Any]:
        if witness_id not in self.witnesses:
            raise MoldError(f"unknown witness {witness_id}")
        if signature_id in self.signatures:
            raise MoldError(f"signature {signature_id} already exists")
        w = self.witnesses[witness_id]
        ids = [str(x) for x in box_ids]
        for bid in ids:
            if bid not in self.boxes:
                raise MoldError(f"signature {signature_id} references unknown box {bid}")
            if bid not in w["saw_box_ids"]:
                raise MoldError(
                    f"witness {witness_id} cannot sign {bid}: no actual viewing record"
                )
            if not self._time_in_window(signed_at, w):
                raise MoldError(
                    f"witness {witness_id} cannot sign {bid} at {signed_at}: outside window"
                )
        sig = {
            "signature_id": signature_id,
            "witness_id": witness_id,
            "box_ids": ids,
            "signed_at": int(signed_at),
        }
        self.signatures[signature_id] = sig
        self._put_record("signature", signature_id, sig)
        return sig

    @staticmethod
    def _time_in_window(t: int, w: dict[str, Any]) -> bool:
        return (w["valid_from"] is None or t >= w["valid_from"]) and (
            w["valid_to"] is None or t <= w["valid_to"]
        )

    def add_opening(
        self,
        opening_id: str | dict[str, Any],
        box_id: str | None = None,
        *,
        witness_id: str | None = None,
        opened_at: int | None = None,
        piece_ids: Sequence[str] = (),
        batch_id: str | None = None,
        signature_id: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        if isinstance(opening_id, dict):
            data = dict(opening_id)
            oid = str(data.pop("opening_id") or data.pop("id"))
            return self.add_opening(oid, **data)
        oid = str(opening_id)
        if oid in self.openings:
            raise MoldError(f"opening {oid} already exists")
        if box_id is None or witness_id is None or opened_at is None:
            raise MoldError(f"opening {oid} needs box_id, witness_id, opened_at")
        if box_id not in self.boxes:
            raise MoldError(f"opening {oid} references unknown box {box_id}")
        if witness_id not in self.witnesses:
            raise MoldError(f"opening {oid} references unknown witness {witness_id}")
        ids = [str(x) for x in piece_ids]
        for pid in ids:
            if pid not in self.pieces:
                raise MoldError(f"opening {oid} identifies unknown piece {pid}")
        if signature_id is not None and signature_id not in self.signatures:
            raise MoldError(f"opening {oid} references unknown signature {signature_id}")
        o = {
            "opening_id": oid,
            "box_id": str(box_id),
            "witness_id": str(witness_id),
            "opened_at": int(opened_at),
            "piece_ids": ids,
            "batch_id": batch_id,
            "signature_id": signature_id,
            **extra,
        }
        self.openings[oid] = o
        self._put_record("opening", oid, o)
        # 现场开箱记录本身也证明见证人实际看过该箱。
        self.witness_sees_box(witness_id, str(box_id))
        return o

    # ------------------------------------------------------------- shipment
    def attach_shipment(
        self,
        trip_id: str | dict[str, Any],
        box_ids: Sequence[str] | None = None,
        *,
        net: Any = None,
        net_lo: int | None = None,
        net_hi: int | None = None,
        gross: Any = None,
        vehicle_tare: Any = None,
        vehicle_deduction_ref: str | None = None,
        confirmed: bool | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        if isinstance(trip_id, dict):
            data = dict(trip_id)
            tid = str(data.pop("trip_id"))
            return self.attach_shipment(tid, **data)
        tid = str(trip_id)
        if tid in self.shipments:
            raise MoldError(f"shipment for trip {tid} already exists")
        ids = [str(x) for x in (box_ids or [])]
        for bid in ids:
            if bid not in self.boxes:
                raise MoldError(f"shipment {tid} references unknown box {bid}")
            other = next((s["trip_id"] for s in self.shipments.values() if bid in s["box_ids"]), None)
            if other is not None:
                raise MoldError(f"box {bid} is already attached to trip {other}")

        net_range = None
        if net is not None or net_lo is not None:
            net_range = _range(net, net_lo, net_hi, name=f"net range for {tid}")
        gross_range = _range(gross) if gross is not None else None
        vehicle_range = _range(vehicle_tare) if vehicle_tare is not None else None
        if gross_range is not None and net_range is None:
            if vehicle_range is None:
                raise MoldError(f"shipment {tid}: gross requires vehicle_tare to derive net")
            # 车重只在车次级扣一次；绝不允许按箱再扣同一车重。
            net_range = (gross_range[0] - vehicle_range[1], gross_range[1] - vehicle_range[0])

        s = {
            "trip_id": tid,
            "box_ids": ids,
            "net_lo": None if net_range is None else net_range[0],
            "net_hi": None if net_range is None else net_range[1],
            "gross_lo": None if gross_range is None else gross_range[0],
            "gross_hi": None if gross_range is None else gross_range[1],
            "vehicle_tare_lo": None if vehicle_range is None else vehicle_range[0],
            "vehicle_tare_hi": None if vehicle_range is None else vehicle_range[1],
            "vehicle_deduction_ref": vehicle_deduction_ref or f"VEHICLE-{tid}",
            "vehicle_deduction": "ONCE_AT_TRIP_LEVEL",
            "warehouse_confirmed": confirmed,
            **extra,
        }
        self.shipments[tid] = s
        self._put_record("shipment", tid, s)
        return s

    # ------------------------------------------------------------- crushing
    def add_crush_batch(
        self,
        batch_id: str | dict[str, Any],
        *,
        crushed_at: int | None = None,
        box_ids: Sequence[str] = (),
        opening_ids: Sequence[str] = (),
        piece_ids: Sequence[str] = (),
        slag_weight: Any = None,
        slag_lo: int | None = None,
        slag_hi: int | None = None,
        yield_lo: Any = None,
        yield_hi: Any = None,
        seal_readings: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        if isinstance(batch_id, dict):
            data = dict(batch_id)
            bid = str(data.pop("batch_id") or data.pop("id"))
            return self.add_crush_batch(bid, **data)
        bid = str(batch_id)
        if bid in self.batches:
            raise MoldError(f"crush batch {bid} already exists")
        if crushed_at is None:
            raise MoldError(f"crush batch {bid} needs crushed_at")
        boxes = [str(x) for x in box_ids]
        for b in boxes:
            if b not in self.boxes:
                raise MoldError(f"batch {bid} references unknown box {b}")
            other = next(
                (x["batch_id"] for x in self.batches.values() if b in x["box_ids"]), None
            )
            if other is not None:
                raise MoldError(f"box {b} was already received by crush batch {other}")
        openings = [str(x) for x in opening_ids]
        for oid in openings:
            if oid not in self.openings:
                raise MoldError(f"batch {bid} references unknown opening {oid}")
            other = next(
                (x["batch_id"] for x in self.batches.values() if oid in x["opening_ids"]),
                None,
            )
            if other is not None:
                raise MoldError(f"opening {oid} is already linked to crush batch {other}")
        pids = [str(x) for x in piece_ids]
        for pid in pids:
            if pid not in self.pieces:
                raise MoldError(f"batch {bid} references unknown piece {pid}")
        slag = None
        if slag_weight is not None or slag_lo is not None:
            slag = _range(slag_weight, slag_lo, slag_hi, name=f"slag for batch {bid}")
        ylo = _fraction(1 if yield_lo is None else yield_lo, name="yield_lo")
        yhi = _fraction(1 if yield_hi is None else yield_hi, name="yield_hi")
        if ylo < 0 or ylo > yhi:
            raise MoldError(f"batch {bid} has invalid yield interval")
        readings: dict[str, dict[str, Any]] = {}
        for key, value in (seal_readings or {}).items():
            if isinstance(value, str):
                readings[str(key)] = {"seal_id": value, "intact": True}
            else:
                v = dict(value)
                readings[str(key)] = {
                    "seal_id": str(v.get("seal_id")),
                    "intact": bool(v.get("intact", True)),
                }
        b = {
            "batch_id": bid,
            "crushed_at": int(crushed_at),
            "box_ids": boxes,
            "opening_ids": openings,
            "claimed_piece_ids": pids,
            "slag_lo": None if slag is None else slag[0],
            "slag_hi": None if slag is None else slag[1],
            "yield_lo": str(ylo),
            "yield_hi": str(yhi),
            "seal_readings": readings,
            **extra,
        }
        self.batches[bid] = b
        self._put_record("batch", bid, b)
        return b

    # ------------------------------------------------------------- evaluation
    def _resolved_shipment_net(self, sh: dict[str, Any], issues: list[str]) -> tuple[int, int] | None:
        tid = sh["trip_id"]
        if self.warehouse is not None:
            try:
                info = self.warehouse.trip(tid)
            except Exception as exc:  # warehouse raises WarehouseError
                issues.append(f"trip {tid}: {exc}")
                return None
            if info["status"] != STATUS_CONFIRMED:
                issues.append(f"trip {tid} is {info['status']}, not CONFIRMED")
                return None
            try:
                ev = self.warehouse.evaluation(tid)
            except Exception as exc:
                issues.append(f"trip {tid}: cannot load evaluation: {exc}")
                return None
            if ev.net_lo is None or ev.net_hi is None:
                issues.append(f"trip {tid} has no feasible confirmed net interval")
                return None
            net = (int(ev.net_lo), int(ev.net_hi))
            if sh["net_lo"] is not None and (sh["net_lo"], sh["net_hi"]) != net:
                issues.append(
                    f"trip {tid}: supplied net {sh['net_lo']},{sh['net_hi']} conflicts with "
                    f"confirmed weighbridge net {net[0]},{net[1]}"
                )
            sh["resolved_net_lo"], sh["resolved_net_hi"] = net
            sh["net_source"] = "warehouse.confirmed_evaluation"
            return net
        if sh["net_lo"] is None:
            issues.append(f"trip {tid}: no confirmed net interval supplied")
            return None
        if sh.get("warehouse_confirmed") is False:
            issues.append(f"trip {tid} is not marked confirmed")
            return None
        sh["resolved_net_lo"], sh["resolved_net_hi"] = sh["net_lo"], sh["net_hi"]
        sh["net_source"] = "supplied_confirmed_interval"
        return sh["net_lo"], sh["net_hi"]

    def _line_domain(
        self,
        line: dict[str, Any],
        observed_all: set[str],
        observed_by_box: dict[str, set[str]],
    ) -> list[str]:
        if line["identified_piece_id"] is not None:
            domain = [line["identified_piece_id"]]
        elif line["candidate_ids"] is not None:
            domain = list(line["candidate_ids"])
        else:
            domain = [
                pid for pid, p in self.pieces.items()
                if (line["work_id"] is None or p["work_id"] == line["work_id"])
                and (line["role"] is None or p["role"] == line["role"])
            ]
        # 现场已在某箱看到的 ID，不能被枚举到另一箱。
        box = line["box_id"]
        domain = [
            pid for pid in domain
            if pid not in observed_all or pid in observed_by_box.get(box, set())
        ]
        # 工场封箱路径中，不可封的实体瓣不能作为该箱内容的可行世界。
        if self.boxes.get(box, {}).get("at_workshop", False):
            domain = [pid for pid in domain if self.pieces[pid].get("workshop_sealable", True)]
        return sorted(set(domain))

    def _opening_admissibility(self) -> tuple[dict[str, dict[str, Any]], list[str]]:
        reports: dict[str, dict[str, Any]] = {}
        issues: list[str] = []
        for oid, o in self.openings.items():
            problems: list[str] = []
            w = self.witnesses.get(o["witness_id"])
            if w is None:
                problems.append("witness missing")
            else:
                if not self._time_in_window(o["opened_at"], w):
                    problems.append(
                        f"opening at {o['opened_at']} outside witness window "
                        f"[{w['valid_from']},{w['valid_to']}]"
                    )
                if o["box_id"] not in w["saw_box_ids"]:
                    problems.append("witness did not actually sign/see this box")
            sig_id = o.get("signature_id")
            if sig_id:
                sig = self.signatures.get(sig_id)
                if sig is None or o["box_id"] not in sig["box_ids"]:
                    problems.append("signature does not cover this box")
            batch = self.batches.get(o["batch_id"]) if o.get("batch_id") else None
            if batch is None:
                problems.append("opening is not linked to a crush batch")
            elif o["opened_at"] >= batch["crushed_at"]:
                problems.append("identity recorded at/after crushing; fragments cannot show serial")
            admissible = not problems
            reports[oid] = {"opening_id": oid, "admissible": admissible, "issues": problems}
            if not admissible:
                issues.extend(f"opening {oid}: {x}" for x in problems)
        return reports, issues

    def _box_chain_ok(self, batch: dict[str, Any], box_id: str) -> tuple[bool, list[str]]:
        box = self.boxes[box_id]
        problems: list[str] = []
        if box.get("identity_mode", ROUTE_WORKSHOP_SEALED) == ROUTE_WORKSHOP_SEALED:
            pw = box.get("packing_witness_id")
            if not pw:
                problems.append(f"{box_id} lacks a packing witness")
            elif pw not in self.witnesses:
                problems.append(f"packing witness {pw} for {box_id} is not recorded")
        reading = batch["seal_readings"].get(box_id)
        if reading is None:
            problems.append(f"batch {batch['batch_id']} has no seal intake reading for {box_id}")
            return False, problems
        if not reading["intact"]:
            problems.append(f"seal {box['seal_id']} on {box_id} was broken before crushing")
        if reading["seal_id"] != box["seal_id"]:
            problems.append(
                f"seal mismatch on {box_id}: packed {box['seal_id']}, "
                f"intake {reading['seal_id']}"
            )
        return not problems, problems

    def _valid_world_constraints(
        self,
        assignment: dict[str, str],
        observed_by_box: dict[str, set[str]],
        admissible_openings: dict[str, dict[str, Any]],
        opening_reports: dict[str, dict[str, Any]],
        domains: dict[str, list[str]] | None = None,
        weight_failures: list[str] | None = None,
    ) -> bool:
        assigned_by_box: dict[str, set[str]] = {}
        lines_by_box: dict[str, list[str]] = {}
        for lid, pid in assignment.items():
            line = next(x for x in self.lines if x["line_id"] == lid)
            bid = line["box_id"]
            assigned_by_box.setdefault(bid, set()).add(pid)
            lines_by_box.setdefault(bid, []).append(lid)
        for bid, ids in observed_by_box.items():
            box_lids = [x["line_id"] for x in self.lines if x["box_id"] == bid]
            for pid in ids:
                representable = any(
                    domains is not None and pid in domains.get(lid, []) for lid in box_lids
                )
                if representable and pid not in assigned_by_box.get(bid, set()):
                    if weight_failures is not None:
                        weight_failures.append(f"observed id {pid} not represented in {bid}")
                    return False

        # 车次净重 - 箱/芯皮重 = 模瓣总重区间。车重已在磅单评估中扣除，绝不再次扣。
        for sh in self.shipments.values():
            net = sh.get("resolved_net")
            if net is None:
                continue
            tares_lo = sum(self.boxes[b]["tare_lo"] for b in sh["box_ids"])
            tares_hi = sum(self.boxes[b]["tare_hi"] for b in sh["box_ids"])
            mold_lo = net[0] - tares_hi
            mold_hi = net[1] - tares_lo
            ids: set[str] = set()
            for bid in sh["box_ids"]:
                ids.update(observed_by_box.get(bid, set()))
                ids.update(assigned_by_box.get(bid, set()))
            wlo, whi = _weight_sum(ids, self.pieces)
            if not _overlap(wlo, whi, mold_lo, mold_hi):
                if weight_failures is not None:
                    weight_failures.append(
                        f"trip {sh['trip_id']}: assigned {sorted(ids)} weight [{wlo},{whi}] "
                        f"does not intersect inferred mold weight [{mold_lo},{mold_hi}]"
                    )
                return False

        # 破碎台出渣区间约束每一批实际入炉的实体组合。
        for batch in self.batches.values():
            ids = set()
            for oid in batch["opening_ids"]:
                if opening_reports.get(oid, {}).get("admissible"):
                    ids.update(self.openings[oid]["piece_ids"])
            # opening.batch_id 也可能反向指向本批。
            for oid, o in self.openings.items():
                if o.get("batch_id") == batch["batch_id"] and opening_reports.get(oid, {}).get("admissible"):
                    ids.update(o["piece_ids"])
            for bid in batch["box_ids"]:
                ids.update(assigned_by_box.get(bid, set()))
            if batch["slag_lo"] is not None:
                wlo, whi = _weight_sum(ids, self.pieces)
                ylo = _fraction(batch["yield_lo"], name="yield_lo")
                yhi = _fraction(batch["yield_hi"], name="yield_hi")
                out_lo, out_hi = ylo * wlo, yhi * whi
                if not _overlap(Fraction(batch["slag_lo"]), Fraction(batch["slag_hi"]), out_lo, out_hi):
                    if weight_failures is not None:
                        weight_failures.append(
                            f"batch {batch['batch_id']}: {sorted(ids)} yield [{out_lo},{out_hi}] "
                            f"does not intersect slag [{batch['slag_lo']},{batch['slag_hi']}]"
                        )
                    return False
        return True

    def _enumerate_worlds(
        self,
        opening_reports: dict[str, dict[str, Any]],
        *,
        max_worlds: int = 5000,
    ) -> tuple[list[dict[str, str]], list[str], bool]:
        observed_all: set[str] = set()
        observed_by_box: dict[str, set[str]] = {}
        for oid, report in opening_reports.items():
            if not report["admissible"]:
                continue
            o = self.openings[oid]
            ids = set(o["piece_ids"])
            observed_all.update(ids)
            observed_by_box.setdefault(o["box_id"], set()).update(ids)

        domains: dict[str, list[str]] = {}
        for line in self.lines:
            domains[line["line_id"]] = self._line_domain(line, observed_all, observed_by_box)
            if not domains[line["line_id"]]:
                return [], [f"line {line['line_id']} has no possible identified piece"], False

        line_by_id = {x["line_id"]: x for x in self.lines}
        worlds: list[dict[str, str]] = []
        failures: list[str] = []
        truncated = False

        def dfs(assignment: dict[str, str], used: set[str]) -> None:
            nonlocal truncated
            if truncated:
                return
            if len(assignment) == len(domains):
                probe: list[str] = []
                if self._valid_world_constraints(
                    assignment, observed_by_box, opening_reports, opening_reports, domains, probe
                ):
                    worlds.append(dict(assignment))
                elif len(failures) < 12 and probe:
                    failures.append(probe[0])
                if len(worlds) >= max_worlds:
                    truncated = True
                return
            remaining = [lid for lid in domains if lid not in assignment]
            lid = min(remaining, key=lambda x: len([p for p in domains[x] if p not in used]))
            for pid in domains[lid]:
                if pid in used:
                    continue
                # 快速检查：同箱已观察 ID 必须有足够槽位。
                assignment[lid] = pid
                used.add(pid)
                dfs(assignment, used)
                used.remove(pid)
                del assignment[lid]
                if truncated:
                    return

        dfs({}, set())
        if truncated:
            failures.append(f"more than {max_worlds} feasible/explored worlds; analysis is conservative")
        return worlds, failures[:12], truncated

    def _mold_remaining_in_world(self, mold: dict[str, Any], destroyed: set[str]) -> dict[str, Any] | None:
        chosen: list[str] = []

        def fill(slot_idx: int, used: set[str]) -> bool:
            if slot_idx == len(mold["slots"]):
                return True
            slot = mold["slots"][slot_idx]
            if slot["piece_ids"] is not None:
                candidates = list(slot["piece_ids"])
            else:
                candidates = [
                    pid for pid, p in self.pieces.items()
                    if p["work_id"] == slot["work_id"] and p["role"] == slot["role"]
                ]
            for pid in candidates:
                if pid in destroyed or pid in used:
                    continue
                chosen.append(pid)
                used.add(pid)
                if fill(slot_idx + 1, used):
                    return True
                used.remove(pid)
                chosen.pop()
            return False

        if fill(0, set()):
            return {
                "mold_id": mold["mold_id"],
                "work_id": mold["work_id"],
                "chosen_piece_ids": tuple(chosen),
                "note": "all listed entities survive in this feasible evidence world",
            }
        return None

    def evaluate_work(
        self,
        work_id: str,
        *,
        require_weighing: bool = True,
        require_slag: bool = True,
    ) -> MoldVerdict:
        issues: list[str] = []
        if not any(p["work_id"] == work_id for p in self.pieces.values()):
            raise MoldError(f"work {work_id} has no pieces")

        # 重量证据必须来自实际 CONFIRMED 车次；解析结果回填到 shipment。
        for sh in self.shipments.values():
            net = self._resolved_shipment_net(sh, issues)
            sh["resolved_net"] = net
        shipment_by_box = {
            bid: sh for sh in self.shipments.values() for bid in sh["box_ids"]
        }

        # 皮重抵扣引用不得复用；车重引用尤其不能落到两个箱/两条抵扣上。
        tare_owners: dict[str, str] = {}
        for b in self.boxes.values():
            ref = b["tare_ref"]
            if ref in tare_owners and tare_owners[ref] != b["box_id"]:
                issues.append(f"tare reference {ref} reused by {tare_owners[ref]} and {b['box_id']}")
            tare_owners[ref] = b["box_id"]
        for sh in self.shipments.values():
            ref = sh["vehicle_deduction_ref"]
            if ref in tare_owners:
                issues.append(
                    f"vehicle deduction {ref} is also used as box tare {tare_owners[ref]}; "
                    "vehicle weight would be deducted twice"
                )

        opening_reports, opening_issues = self._opening_admissibility()
        # 开窗/见证缺陷先保留在 batch reports；若其影响目标作品，下面进入总 issues。
        hard_issues = list(issues)

        worlds, solver_failures, truncated = self._enumerate_worlds(opening_reports)
        if hard_issues:
            worlds = []
        if not worlds:
            hard_issues.extend(solver_failures)
            if not hard_issues:
                hard_issues.append("no feasible identity/weight world matches all evidence")
        if truncated:
            hard_issues.append("evidence remains too ambiguous to prove universal destruction")

        # 每个可行世界的确定销毁集合。
        world_destroyed: list[set[str]] = []
        batch_reports: list[dict[str, Any]] = []
        chain_issues: list[str] = []
        inferred_combos = {b: set() for b in self.batches}
        proven_combos = {b: set() for b in self.batches}
        observed_ids_all_admissible = {
            pid
            for oid, rep in opening_reports.items() if rep["admissible"]
            for pid in self.openings[oid]["piece_ids"]
        }

        for wi, assignment in enumerate(worlds or [{}]):
            assigned_by_box: dict[str, set[str]] = {}
            for lid, pid in assignment.items():
                bid = next(x["box_id"] for x in self.lines if x["line_id"] == lid)
                assigned_by_box.setdefault(bid, set()).add(pid)

            destroyed = set(observed_ids_all_admissible)
            for batch in self.batches.values():
                per_box: dict[str, Any] = {}
                physical_ids: set[str] = set()
                proven_batch_ids: set[str] = set()
                # 现场指认的实体先进入本批物理组合与销毁集合。
                for oid in batch["opening_ids"]:
                    if opening_reports.get(oid, {}).get("admissible"):
                        ids = set(self.openings[oid]["piece_ids"])
                        physical_ids.update(ids)
                        proven_batch_ids.update(ids)
                for oid, o in self.openings.items():
                    if o.get("batch_id") == batch["batch_id"] and opening_reports.get(oid, {}).get("admissible"):
                        ids = set(o["piece_ids"])
                        physical_ids.update(ids)
                        proven_batch_ids.update(ids)
                for bid in batch["box_ids"]:
                    ok, problems = self._box_chain_ok(batch, bid)
                    per_box[bid] = {"chain_ok": ok, "issues": problems}
                    identity_mode = self.boxes[bid].get(
                        "identity_mode", ROUTE_WORKSHOP_SEALED
                    )
                    box_ids = assigned_by_box.get(bid, set())
                    physical_ids.update(box_ids)
                    if wi == 0 and not ok and identity_mode == ROUTE_WORKSHOP_SEALED:
                        chain_issues.extend(
                            f"batch {batch['batch_id']} box {bid}: {problem}"
                            for problem in problems
                        )
                    if wi == 0 and require_weighing:
                        ship = shipment_by_box.get(bid)
                        if ship is None:
                            chain_issues.append(
                                f"batch {batch['batch_id']} box {bid}: box is not on a confirmed shipment"
                            )
                        elif ship.get("resolved_net") is None:
                            chain_issues.append(
                                f"batch {batch['batch_id']} box {bid}: no confirmed net weight interval"
                            )
                    # 只有“装箱身份 + 封号交接”路径才能凭枚举组合记销毁；
                    # 现场路径必须等合格的开箱指认。
                    if ok and worlds and identity_mode == ROUTE_WORKSHOP_SEALED:
                        destroyed.update(box_ids)
                        proven_batch_ids.update(box_ids)
                if worlds:
                    inferred_combos[batch["batch_id"]].add(tuple(sorted(physical_ids)))
                    proven_combos[batch["batch_id"]].add(tuple(sorted(proven_batch_ids)))
                # 无有效开箱支持的破碎台 claimed_piece_ids 不能作为身份证据。
                supported = set()
                for oid in batch["opening_ids"]:
                    if opening_reports.get(oid, {}).get("admissible"):
                        supported.update(self.openings[oid]["piece_ids"])
                for oid, o in self.openings.items():
                    if o.get("batch_id") == batch["batch_id"] and opening_reports.get(oid, {}).get("admissible"):
                        supported.update(o["piece_ids"])
                unsupported = [pid for pid in batch["claimed_piece_ids"] if pid not in supported]
                if wi == 0 and require_slag and batch["slag_lo"] is None:
                    chain_issues.append(
                        f"batch {batch['batch_id']}: missing crushing-table slag/weight record"
                    )
                if unsupported:
                    per_box["claimed_piece_ids"] = {
                        "unsupported_identity": unsupported,
                        "issue": "crusher fragments cannot establish a serial after crushing",
                    }
                if wi == 0:
                    batch_reports.append({
                        "batch_id": batch["batch_id"],
                        "crushed_at": batch["crushed_at"],
                        "box_chain": per_box,
                        "unsupported_claimed_piece_ids": unsupported,
                        "slag": None if batch["slag_lo"] is None else [batch["slag_lo"], batch["slag_hi"]],
                    })
            world_destroyed.append(destroyed)

        for report in batch_reports:
            bid = report["batch_id"]
            combos = inferred_combos.get(bid, set())
            pcombos = proven_combos.get(bid, set())
            report["inferred_combinations"] = [list(x) for x in sorted(combos)]
            report["proven_destroyed_combinations"] = [list(x) for x in sorted(pcombos)]
            if combos:
                report["certain_batch_piece_ids"] = list(
                    set.intersection(*(set(x) for x in combos))
                )
            if pcombos:
                report["certain_proven_piece_ids"] = list(
                    set.intersection(*(set(x) for x in pcombos))
                )

        target_molds = [m for m in self.molds.values() if m["work_id"] == work_id]
        remaining_aggregate: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
        any_world_with_mold = False
        all_worlds_without_mold = True
        for i, destroyed in enumerate(world_destroyed):
            world_has_mold = False
            for m in target_molds:
                rem = self._mold_remaining_in_world(m, destroyed)
                if rem is not None:
                    world_has_mold = True
                    any_world_with_mold = True
                    key = (rem["mold_id"], rem["chosen_piece_ids"])
                    if key not in remaining_aggregate:
                        rem["world_index"] = i if worlds else None
                        rem["feasible_evidence_world"] = bool(worlds)
                        rem["destroyed_in_that_world"] = sorted(destroyed)
                        remaining_aggregate[key] = rem
            if world_has_mold:
                all_worlds_without_mold = False

        if worlds:
            certain = set.intersection(*world_destroyed) if world_destroyed else set()
            possible = set.union(*world_destroyed) if world_destroyed else set()
        else:
            certain, possible = set(), set()

        # 与目标作品有关的见证缺陷也作为回单 blocker。
        target_piece_ids = {pid for pid, p in self.pieces.items() if p["work_id"] == work_id}
        relevant_opening_issues: list[str] = []
        for oid, rep in opening_reports.items():
            if rep["admissible"]:
                continue
            o = self.openings[oid]
            relevant = any(pid in target_piece_ids for pid in o["piece_ids"])
            if not relevant:
                box = self.boxes.get(o["box_id"])
                relevant = box is not None and any(
                    p["work_id"] == work_id for lid in [] for p in []
                )
                if not relevant:
                    relevant = any(
                        line["box_id"] == o["box_id"]
                        and (line["work_id"] == work_id or line.get("identified_piece_id") in target_piece_ids)
                        for line in self.lines
                    )
            if relevant:
                relevant_opening_issues.extend(f"opening {oid}: {x}" for x in rep["issues"])

        refs = self._evidence_refs(work_id)
        relevant_chain_issues = [
            issue for issue in chain_issues
            if any(f"box {bid}:" in issue for bid in refs["box_ids"])
            or any(f"batch {bid}:" in issue for bid in refs["crush_batch_ids"])
        ]
        reason = (
            "all feasible evidence worlds destroy every compatible mold"
            if worlds and all_worlds_without_mold and not hard_issues
            and not relevant_opening_issues and not relevant_chain_issues
            else "at least one complete mold can remain or evidence is inconsistent/incomplete"
        )
        issuable = bool(
            worlds
            and all_worlds_without_mold
            and not hard_issues
            and not relevant_opening_issues
            and not relevant_chain_issues
            and not truncated
        )
        status = ISSUED if issuable else WITHHELD
        issues_out = list(dict.fromkeys(
            hard_issues + relevant_opening_issues + relevant_chain_issues
        ))
        if not target_molds:
            issues_out.append(f"work {work_id} has no mold definitions; cannot prove every mold destroyed")
            issuable = False
            status = WITHHELD

        return MoldVerdict(
            work_id=work_id,
            status=status,
            issuable=issuable,
            feasible_worlds=len(worlds),
            certain_destroyed_ids=tuple(sorted(certain & target_piece_ids)),
            possible_destroyed_ids=tuple(sorted(possible & target_piece_ids)),
            remaining_molds=tuple(remaining_aggregate.values()),
            world_destroyed_sets=tuple(tuple(sorted(x & target_piece_ids)) for x in world_destroyed),
            batch_reports=tuple(batch_reports),
            issues=tuple(issues_out),
            evidence_refs=refs,
            reason=reason,
        )

    def work_ids(self) -> list[str]:
        return sorted({p["work_id"] for p in self.pieces.values()})

    def evaluate_all(self) -> dict[str, MoldVerdict]:
        return {wid: self.evaluate_work(wid) for wid in self.work_ids()}

    def issuable_works(self) -> list[str]:
        """回答“能给哪件作品出销模回单”。"""
        return [wid for wid, verdict in self.evaluate_all().items() if verdict.issuable]

    def _evidence_refs(self, work_id: str) -> dict[str, Any]:
        pids = {pid for pid, p in self.pieces.items() if p["work_id"] == work_id}
        mold_ids = [mid for mid, m in self.molds.items() if m["work_id"] == work_id]
        box_ids = set()
        for line in self.lines:
            if line.get("work_id") == work_id or line.get("identified_piece_id") in pids:
                box_ids.add(line["box_id"])
            elif line["candidate_ids"] and any(x in pids for x in line["candidate_ids"]):
                box_ids.add(line["box_id"])
        for o in self.openings.values():
            if any(pid in pids for pid in o["piece_ids"]):
                box_ids.add(o["box_id"])
        ship_ids = [sid for sid, s in self.shipments.items() if any(b in box_ids for b in s["box_ids"])]
        batch_ids = [
            bid for bid, b in self.batches.items()
            if any(x in box_ids for x in b["box_ids"])
            or any(pid in pids for pid in b["claimed_piece_ids"])
            or any(
                pid in pids
                for oid in b["opening_ids"]
                for pid in self.openings.get(oid, {}).get("piece_ids", [])
            )
        ]
        witness_ids = [
            o["witness_id"] for o in self.openings.values()
            if o["batch_id"] in batch_ids or o["box_id"] in box_ids
        ]
        packing_witness_ids = [
            self.boxes[b]["packing_witness_id"]
            for b in box_ids
            if b in self.boxes and self.boxes[b].get("packing_witness_id")
        ]
        witness_ids.extend(packing_witness_ids)
        opening_ids = [
            oid for oid, o in self.openings.items()
            if o["box_id"] in box_ids or o.get("batch_id") in batch_ids
        ]
        trip_ticket_ids: list[str] = []
        if self.warehouse is not None:
            for tid in ship_ids:
                try:
                    info = self.warehouse.trip(tid)
                    for role in ("IN", "OUT"):
                        t = info.get("tickets", {}).get(role)
                        if t:
                            trip_ticket_ids.append(t["ticket_id"])
                except Exception:
                    pass
        return {
            "work_id": work_id,
            "piece_ids": sorted(pids),
            "mold_ids": mold_ids,
            "box_ids": sorted(box_ids),
            "seal_ids": [self.boxes[b]["seal_id"] for b in sorted(box_ids) if b in self.boxes],
            "shipment_trip_ids": ship_ids,
            "weighbridge_ticket_ids": sorted(set(trip_ticket_ids)),
            "crush_batch_ids": batch_ids,
            "witness_ids": sorted(set(witness_ids)),
            "packing_witness_ids": sorted(set(packing_witness_ids)),
            "opening_ids": opening_ids,
        }

    # ------------------------------------------------------------- receipts
    def preview_receipt(self, work_id: str) -> MoldVerdict:
        return self.evaluate_work(work_id)

    def issue_receipt(
        self,
        receipt_id: str,
        work_id: str,
        *,
        client: str = "委托人",
        signatory: str = "铸像厂核销员",
        issued_at: int | None = None,
        route: str = ROUTE_WORKSHOP_SEALED,
        strict: bool = False,
        require_weighing: bool = True,
        require_slag: bool = True,
    ) -> WriteOffReceipt:
        if receipt_id in self.receipts:
            existing = self.receipts[receipt_id]
            if existing.work_id != work_id:
                raise MoldError(f"receipt {receipt_id} already exists for {existing.work_id}")
            return existing
        verdict = self.evaluate_work(
            work_id,
            require_weighing=require_weighing,
            require_slag=require_slag,
        )
        if not verdict.issuable:
            receipt = WriteOffReceipt(
                receipt_id=receipt_id,
                work_id=work_id,
                status=WITHHELD,
                client=client,
                signatory=signatory,
                issued_at=issued_at,
                route=route,
                verdict=verdict.to_dict(),
            )
            if strict:
                raise ReceiptWithheld(verdict)
            # 拒绝回单也落盘，便于告诉委托人还剩哪套。
            self.receipts[receipt_id] = receipt
            self._save_receipt(receipt)
            return receipt

        receipt = WriteOffReceipt(
            receipt_id=receipt_id,
            work_id=work_id,
            status=ISSUED,
            client=client,
            signatory=signatory,
            issued_at=issued_at,
            route=route,
            verdict=verdict.to_dict(),
        )
        self.receipts[receipt_id] = receipt
        self._save_receipt(receipt)
        return receipt

    def _save_receipt(self, receipt: WriteOffReceipt) -> None:
        order = len(self.receipts)
        self.conn.execute(
            "INSERT OR REPLACE INTO mold_receipt(receipt_id, work_id, status, created_order, payload) "
            "VALUES (?,?,?,?,?)",
            (
                receipt.receipt_id,
                receipt.work_id,
                receipt.status,
                order,
                json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True),
            ),
        )

    def get_receipt(self, receipt_id: str) -> WriteOffReceipt:
        if receipt_id in self.receipts:
            return self.receipts[receipt_id]
        row = self.conn.execute(
            "SELECT payload FROM mold_receipt WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        if row is None:
            raise MoldError(f"unknown receipt {receipt_id}")
        data = json.loads(row["payload"])
        r = WriteOffReceipt(**data)
        self.receipts[receipt_id] = r
        return r

    def trace_receipt(self, receipt_id: str) -> dict[str, Any]:
        r = self.get_receipt(receipt_id)
        v = r.verdict
        refs = v["evidence_refs"]
        weighing: dict[str, Any] = {}
        for tid in refs["shipment_trip_ids"]:
            entry: dict[str, Any] = {"shipment": self.shipments.get(tid)}
            if self.warehouse is not None:
                try:
                    entry["trip"] = self.warehouse.trip(tid)
                    entry["explain_trip"] = self.warehouse.explain_trip(tid)
                except Exception as exc:
                    entry["error"] = str(exc)
            weighing[tid] = entry
        physical = {
            "pieces": {pid: self.pieces[pid] for pid in refs["piece_ids"] if pid in self.pieces},
            "molds": {mid: self.molds[mid] for mid in refs["mold_ids"] if mid in self.molds},
            "boxes": {bid: self.boxes[bid] for bid in refs["box_ids"] if bid in self.boxes},
            "packing_lines": [x for x in self.lines if x["box_id"] in refs["box_ids"]],
            "seals": {bid: self.boxes[bid]["seal_id"] for bid in refs["box_ids"] if bid in self.boxes},
            "witnesses": {wid: self.witnesses[wid] for wid in refs["witness_ids"] if wid in self.witnesses},
            "signatures": list(self.signatures.values()),
            "openings": {oid: self.openings[oid] for oid in refs["opening_ids"] if oid in self.openings},
            "crush_batches": {bid: self.batches[bid] for bid in refs["crush_batch_ids"] if bid in self.batches},
        }
        return {
            "receipt": r.to_dict(),
            "weighing_evidence": weighing,
            "physical_evidence": physical,
            "reverse_lookup": {
                "receipt_id": r.receipt_id,
                "work_id": r.work_id,
                "trip_ids": refs["shipment_trip_ids"],
                "ticket_ids": refs["weighbridge_ticket_ids"],
                "seal_ids": refs["seal_ids"],
                "box_ids": refs["box_ids"],
                "crush_batch_ids": refs["crush_batch_ids"],
            },
        }


# 常见英文别名，方便不同调用习惯。
MoldAudit = MoldWriteOffLedger
MoldWriteOff = MoldWriteOffLedger
WriteOffLedger = MoldWriteOffLedger
MoldReceipt = WriteOffReceipt
MoldDestructionLedger = MoldWriteOffLedger
MoldDestructionService = MoldWriteOffLedger
WriteOffService = MoldWriteOffLedger
DestructionLedger = MoldWriteOffLedger
MoldDestructionReceipt = WriteOffReceipt
DestructionReceipt = WriteOffReceipt


def write_off_molds(
    warehouse: Warehouse | None = None,
    *,
    work_id: str | None = None,
    receipt_id: str = "MOLD-WRITEOFF",
    strict: bool = False,
    **issue_kwargs: Any,
) -> WriteOffReceipt | MoldWriteOffLedger:
    """一次性入口：建账、评核并（可能被拒绝地）生成销模回单。

    未给 ``work_id`` 时返回新账本，便于继续登记证据。
    """
    ledger = MoldWriteOffLedger(warehouse)
    if work_id is None:
        return ledger
    return ledger.issue_receipt(receipt_id, work_id, strict=strict, **issue_kwargs)
