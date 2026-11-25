"""校样室：按各联纸揭下的时刻、哪些联仍可同步复写、哪些格可以加盖勘误，
决定能签哪些具名收件联。

交接制度（二选一，本仓库采用第一种并固化为常量）：
- ``seal_at_separation``（本仓制度）：联纸分离（揭走）的时刻即封存该时刻
  *可确定* 的字格；留疑的格以后只能用有编号的勘误条补。已发联的纸面状态
  冻结，迟来证据只能产生勘误条，绝不改写封存正文。
- 备选制度 ``hold_for_final``（预约保留整叠待终校）：可多等一次取证，
  但被扣着的具名联不能按原时刻交件——本模块只提供判定说明，不开此交接通道。

关键不变量：重击覆盖只作用于当拍仍垫在下面的联；判断签收绝不能只看终局
顶联，必须按当时的纸序逐联重建（desk.replay 已保证）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Sequence

from .desk import CellVerdict, ReplayState, SessionSpec, Transcript, replay
from .model import CopySheet, IndentationMark, Strike

HANDOFF_SEAL_AT_SEPARATION = "seal_at_separation"
HANDOFF_HOLD_FOR_FINAL = "hold_for_final"

# 迟来证据可等待的取证次数上限（两种制度的共同硬约束）
MAX_FURTHER_EVIDENCE_TURNS = 1


class ProofRoomError(ValueError):
    """校样室非法操作。"""


@dataclass(frozen=True)
class SealedCell:
    """揭纸时刻封存的一格。"""

    cell: int
    glyph: str | None
    indent_present: bool
    determinate: bool                 # 封存时字符是否已可确定
    coverage_seqs: tuple[int, ...] | None
    coverage_determinate: bool

    def to_json(self) -> dict[str, Any]:
        return {"cell": self.cell, "glyph": self.glyph,
                "indent_present": self.indent_present,
                "determinate": self.determinate,
                "coverage_seqs": self.coverage_seqs,
                "coverage_determinate": self.coverage_determinate}

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "SealedCell":
        cs = obj.get("coverage_seqs")
        return SealedCell(
            cell=int(obj["cell"]), glyph=obj.get("glyph"),
            indent_present=bool(obj["indent_present"]),
            determinate=bool(obj["determinate"]),
            coverage_seqs=None if cs is None else tuple(int(v) for v in cs),
            coverage_determinate=bool(obj["coverage_determinate"]))


@dataclass(frozen=True)
class ErrataSlip:
    """有编号的勘误条：只能补/改留疑格或用迟来证据纠正封存正文。"""

    slip_no: str                      # 全局编号，如 E-0001
    copy_id: str
    cell: int
    kind: str                         # fill（补留疑）/ correct（纠正封存）
    glyph: str | None                 # 勘误后的字符
    basis: str                        # 证据依据（压痕/实录/回放）
    late: bool                        # 是否由迟来证据触发
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("fill", "correct"):
            raise ProofRoomError(f"非法勘误类型: {self.kind}")

    def to_json(self) -> dict[str, Any]:
        return {"slip_no": self.slip_no, "copy_id": self.copy_id, "cell": self.cell,
                "kind": self.kind, "glyph": self.glyph, "basis": self.basis,
                "late": self.late, "note": self.note}

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "ErrataSlip":
        return ErrataSlip(slip_no=str(obj["slip_no"]), copy_id=str(obj["copy_id"]),
                          cell=int(obj["cell"]), kind=str(obj["kind"]),
                          glyph=obj.get("glyph"), basis=str(obj["basis"]),
                          late=bool(obj["late"]), note=str(obj.get("note", "")))


@dataclass
class Receipt:
    """一具名收件联的历史收件回执（只读，可重播复核）。"""

    receipt_id: str
    issue_no: str
    copy_id: str
    recipient: str
    peel_after_strike: int | None
    sealed_cells: tuple[SealedCell, ...]
    slips: tuple[ErrataSlip, ...]
    signable: bool
    hold_reason: str                  # 不可签时的原因（可签为空串）
    transcript_id: str
    body_hash: str                    # 封存正文+勘误条的内容哈希（重播比对用）

    def to_json(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id, "issue_no": self.issue_no,
            "copy_id": self.copy_id, "recipient": self.recipient,
            "peel_after_strike": self.peel_after_strike,
            "sealed_cells": [c.to_json() for c in self.sealed_cells],
            "slips": [s.to_json() for s in self.slips],
            "signable": self.signable, "hold_reason": self.hold_reason,
            "transcript_id": self.transcript_id, "body_hash": self.body_hash,
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "Receipt":
        return Receipt(
            receipt_id=str(obj["receipt_id"]), issue_no=str(obj["issue_no"]),
            copy_id=str(obj["copy_id"]), recipient=str(obj["recipient"]),
            peel_after_strike=obj.get("peel_after_strike"),
            sealed_cells=tuple(SealedCell.from_json(c) for c in obj["sealed_cells"]),
            slips=tuple(ErrataSlip.from_json(s) for s in obj["slips"]),
            signable=bool(obj["signable"]), hold_reason=str(obj.get("hold_reason", "")),
            transcript_id=str(obj["transcript_id"]), body_hash=str(obj["body_hash"]))

    @staticmethod
    def compute_body_hash(sealed_cells: Sequence[SealedCell],
                          slips: Sequence[ErrataSlip]) -> str:
        canonical = json.dumps(
            {"cells": [c.to_json() for c in sealed_cells],
             "slips": [s.to_json() for s in slips]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProofRoom:
    """校样室：从听打台结论与揭纸时刻生成/封存/勘误/签收各联。"""

    def __init__(self, spec: SessionSpec, transcript: Transcript,
                 issue_no: str, handoff: str = HANDOFF_SEAL_AT_SEPARATION):
        if not transcript.consistent:
            raise ProofRoomError("听打台无相容解，不能进校样室")
        if handoff not in (HANDOFF_SEAL_AT_SEPARATION, HANDOFF_HOLD_FOR_FINAL):
            raise ProofRoomError(f"未知交接制度: {handoff}")
        self.spec = spec
        self.transcript = transcript
        self.issue_no = issue_no
        self.handoff = handoff
        self._slip_seq = 0
        self._slips: dict[str, list[ErrataSlip]] = {c.copy_id: [] for c in spec.copies}
        # 迟来取证轮次按联分别计数：同一批迟来证据可分别送达已发联与未发联，
        # 各自最多再等一次取证；联与联之间不互相占用额度。
        self._late_turns: dict[str, int] = {c.copy_id: 0 for c in spec.copies}
        self.receipts: dict[str, Receipt] = {}

    # ------------------------- 封存（联纸分离时刻） ------------------------- #
    def _verdict_map(self, copy_id: str) -> dict[int, CellVerdict]:
        return {v.cell: v for v in self.transcript.verdicts.get(copy_id, ())}

    def seal_copy(self, sheet: CopySheet) -> Receipt:
        """揭纸时刻封存该联：只封该时刻已可确定的格，留疑格挂起待勘误。"""
        if sheet.copy_id not in {c.copy_id for c in self.spec.copies}:
            raise ProofRoomError(f"未知联: {sheet.copy_id}")
        vmap = self._verdict_map(sheet.copy_id)
        sealed: list[SealedCell] = []
        for cell in sorted(vmap):
            v = vmap[cell]
            sealed.append(SealedCell(
                cell=cell,
                glyph=v.glyph if v.determinate else None,
                indent_present=v.indent_present if v.indent_determinate else True,
                determinate=v.determinate,
                coverage_seqs=v.coverage_seqs,
                coverage_determinate=v.coverage_determinate))
        hold = ""
        if self.handoff == HANDOFF_HOLD_FOR_FINAL:
            hold = ("预约保留整叠待终校：可多等一次取证，"
                    "被扣着的具名联不能按原时刻交件")
        elif any(not c.determinate for c in sealed):
            hold = "存在封存时刻留疑格，须挂编号勘误条后方可签收"
        receipt = Receipt(
            receipt_id=f"R-{self.issue_no}-{sheet.copy_id}",
            issue_no=self.issue_no, copy_id=sheet.copy_id,
            recipient=sheet.recipient, peel_after_strike=sheet.peel_after_strike,
            sealed_cells=tuple(sealed), slips=(),
            signable=(not hold), hold_reason=hold,
            transcript_id=self.transcript.session_id,
            body_hash=Receipt.compute_body_hash(sealed, ()))
        self.receipts[sheet.copy_id] = receipt
        return receipt

    # ------------------------- 编号勘误条 ------------------------- #
    def _next_slip_no(self) -> str:
        self._slip_seq += 1
        return f"E-{self.issue_no}-{self._slip_seq:04d}"

    def fill_doubtful(self, copy_id: str, cell: int, glyph: str | None,
                      basis: str) -> ErrataSlip:
        """对留疑格补发填字勘误条（封存正文不改写，以勘误条为准）。"""
        receipt = self._require_receipt(copy_id)
        if self._cell_is_determinate(receipt, cell):
            raise ProofRoomError(f"{copy_id} 格 {cell} 封存时已可确定，不能用填字条")
        slip = ErrataSlip(self._next_slip_no(), copy_id, cell, "fill",
                          glyph, basis, late=False)
        self._attach(copy_id, slip)
        return slip

    def admit_late_indentation(self, copy_id: str, mark: IndentationMark,
                               glyph: str | None) -> ErrataSlip:
        """接收一条迟来压痕：已发联只能出纠正/填字勘误条，不动封存正文。

        尚未发的联（无回执）仍可同步复写：直接在其当前回放状态上吸收证据，
        不产生勘误条。演示同一迟来证据对已发联/未发联的不同处理。
        """
        if self._late_turns[copy_id] >= MAX_FURTHER_EVIDENCE_TURNS:
            raise ProofRoomError(f"联 {copy_id} 迟来取证次数已用完（最多再等一次取证）")
        self._late_turns[copy_id] += 1
        if copy_id not in self.receipts:
            # 未发联：可同步复写，证据并入听打台结论后由终校封存
            return self._absorb_into_unissued(copy_id, mark, glyph)
        receipt = self.receipts[copy_id]
        sealed = next((c for c in receipt.sealed_cells if c.cell == mark.cell), None)
        kind = "fill" if (sealed is None or not sealed.determinate) else "correct"
        slip = ErrataSlip(self._next_slip_no(), copy_id, mark.cell, kind,
                          glyph, basis=f"迟来压痕({mark.state})", late=True,
                          note="已发联封存正文冻结，以本勘误条为准")
        self._attach(copy_id, slip)
        return slip

    def _absorb_into_unissued(self, copy_id: str, mark: IndentationMark,
                              glyph: str | None) -> ErrataSlip:
        """未发联吸收迟来证据：不冻结、不出条，返回一条占位说明条记录在案。"""
        if mark.state == "present" and glyph is None:
            # 仅有压痕（如抬色带击），不产生字符结论，只登记取证轮次
            slip = ErrataSlip(self._next_slip_no(), copy_id, mark.cell, "fill",
                              None, basis=f"迟来压痕({mark.state})，联未发可同步复写",
                              late=True, note="未发联直接吸收，封存前生效")
        else:
            slip = ErrataSlip(self._next_slip_no(), copy_id, mark.cell, "fill",
                              glyph, basis=f"迟来压痕({mark.state})，联未发可同步复写",
                              late=True, note="未发联直接吸收，封存前生效")
        self._slips[copy_id].append(slip)
        return slip

    # ------------------------- 签收判定 ------------------------- #
    def _all_slips(self, copy_id: str) -> tuple[ErrataSlip, ...]:
        """该联截至当前的全部勘误条：已存回执里的 + 本进程新增的。

        校样室可被按刊行号重新打开（独立进程），回执中的勘误条是权威累积
        状态；本进程 _slips 只保存重开后新挂的条。
        """
        existing = self.receipts[copy_id].slips if copy_id in self.receipts else ()
        known = {s.slip_no for s in existing}
        merged = list(existing) + [s for s in self._slips[copy_id]
                                   if s.slip_no not in known]
        return tuple(merged)

    def sign_decision(self, copy_id: str) -> dict[str, Any]:
        """决定具名联能否签收：留疑格必须全部有勘误条；纠正条不阻塞签收。"""
        receipt = self._require_receipt(copy_id)
        slips = self._all_slips(copy_id)
        doubtful = [c.cell for c in receipt.sealed_cells if not c.determinate]
        filled = {s.cell for s in slips if s.kind == "fill"}
        unresolved = [cell for cell in doubtful if cell not in filled]
        signable = not unresolved and self.handoff == HANDOFF_SEAL_AT_SEPARATION
        reason = receipt.hold_reason
        if unresolved:
            reason = f"留疑格 {unresolved} 尚无编号勘误条"
        elif self.handoff == HANDOFF_HOLD_FOR_FINAL:
            reason = receipt.hold_reason
        return {"copy_id": copy_id, "recipient": receipt.recipient,
                "signable": signable, "unresolved_cells": unresolved,
                "slips": [s.to_json() for s in slips], "reason": reason}

    def finalize(self, copy_id: str) -> Receipt:
        """出具最终回执（含截至当前的全部勘误条与签收结论），正文仍只读。"""
        decision = self.sign_decision(copy_id)
        old = self._require_receipt(copy_id)
        slips = self._all_slips(copy_id)
        receipt = Receipt(
            receipt_id=old.receipt_id, issue_no=old.issue_no, copy_id=old.copy_id,
            recipient=old.recipient, peel_after_strike=old.peel_after_strike,
            sealed_cells=old.sealed_cells, slips=slips,
            signable=decision["signable"], hold_reason=decision["reason"],
            transcript_id=old.transcript_id,
            body_hash=Receipt.compute_body_hash(old.sealed_cells, slips))
        self.receipts[copy_id] = receipt
        return receipt

    # ------------------------- 接手重建 ------------------------- #
    def rebuild_from_strikes(self, strikes: Sequence[Strike]) -> ReplayState:
        """接手已经揭走的纸时，仍须按当时的纸序逐联重建。

        返回单个 ReplayState，其 ``cells`` 已按 copy_id 分联；重击覆盖只作用
        于当拍仍垫在下面的联，故不能只看终局顶联。
        """
        return replay(self.spec, strikes)

    # ------------------------- 内部工具 ------------------------- #
    def _require_receipt(self, copy_id: str) -> Receipt:
        if copy_id not in self.receipts:
            raise ProofRoomError(f"联 {copy_id} 尚未封存（未揭纸/无回执）")
        return self.receipts[copy_id]

    def _cell_is_determinate(self, receipt: Receipt, cell: int) -> bool:
        target = next((c for c in receipt.sealed_cells if c.cell == cell), None)
        return target is not None and target.determinate

    def _attach(self, copy_id: str, slip: ErrataSlip) -> None:
        self._slips[copy_id].append(slip)
        old = self.receipts[copy_id]
        # 必须合并已存回执里的勘误条（跨进程重开场景），不能只用本进程新增部分
        merged = self._all_slips(copy_id)
        self.receipts[copy_id] = Receipt(
            receipt_id=old.receipt_id, issue_no=old.issue_no, copy_id=old.copy_id,
            recipient=old.recipient, peel_after_strike=old.peel_after_strike,
            sealed_cells=old.sealed_cells, slips=merged,
            signable=old.signable, hold_reason=old.hold_reason,
            transcript_id=old.transcript_id,
            body_hash=Receipt.compute_body_hash(old.sealed_cells, merged))
