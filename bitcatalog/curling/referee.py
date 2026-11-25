"""裁判室入口：受理计分、放桌与申诉。

两种交接办法（题面给出的两条路，裁判室必须显式选一条）：

* :data:`SEAL` 封实体桌等待争议：清桌前可以调相机用**实壶**补量；代价是
  下一局要等桌。
* :data:`RELEASE` 按约还桌：只凭还桌前已经拍到并封存的共同参照办申诉，
  移壶之后从其他场次补的图一律无效。

可签分纪律：在最后一壶移走以前，裁判**不能把一局分数签死**；任何时刻
只能签当时全部相容校正共同支持的保证分。补拍帧只有在封桌期、实壶未移时
取得，且与已封存原帧同产品可复查，才能消除歧义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .geometry import Plate
from .scoring import ScoreDesk, ScoreRange, score_range

SEAL = "seal"          # 封桌等待争议，允许实壶补量
RELEASE = "release"    # 按约还桌，只凭封存共同参照申诉
POLICIES = (SEAL, RELEASE)

MAX_REPLAY_STONES = 6


class RefereeError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceFrame:
    """一帧已封存的共同参照（原仓里的帧身份 + 同帧可见对象）。"""

    frame_event_id: str
    frame_id: int
    product_fingerprint: str
    visible: tuple[str, ...]
    sealed: bool = True
    live_stones: bool = True  # 取得时实壶是否仍在桌上

    def as_json(self) -> dict[str, object]:
        return {
            "frame_event_id": self.frame_event_id,
            "frame_id": self.frame_id,
            "product_fingerprint": self.product_fingerprint,
            "visible": list(self.visible),
            "sealed": self.sealed,
            "live_stones": self.live_stones,
        }


@dataclass(frozen=True)
class SupplementReceipt:
    """补拍回执：记录新帧是否合法、是否把歧义消成唯一盘。"""

    end_id: str
    accepted: bool
    frame: EvidenceFrame | None
    before_candidates: int
    after_candidates: int
    note: str

    def as_json(self) -> dict[str, object]:
        return {
            "end_id": self.end_id,
            "accepted": self.accepted,
            "frame": self.frame.as_json() if self.frame else None,
            "before_candidates": self.before_candidates,
            "after_candidates": self.after_candidates,
            "note": self.note,
        }


@dataclass
class End:
    """一局在裁判室的完整状态。"""

    end_id: str
    desk: ScoreDesk
    plates: tuple[Plate, ...]
    evidence: tuple[EvidenceFrame, ...]
    table_sealed: bool = True
    signed_score: int | None = None
    signed_side: str | None = None
    ledger: list[str] = field(default_factory=list)
    replay_limit_ok: bool = True
    released: bool = False

    @property
    def range(self) -> ScoreRange:
        return score_range(self.plates, self.desk)

    def log(self, line: str) -> None:
        self.ledger.append(line)


@dataclass(frozen=True)
class CheckpointVerdict:
    end_id: str
    can_sign: bool
    signable_score: int | None
    side: str | None
    can_place_next: bool
    note: str

    def as_json(self) -> dict[str, object]:
        return {
            "end_id": self.end_id,
            "can_sign": self.can_sign,
            "signable_score": self.signable_score,
            "side": self.side,
            "can_place_next": self.can_place_next,
            "note": self.note,
        }


class RefereeOffice:
    """选一种交接办法后，对外受理计分、放桌、补拍与申诉的唯一入口。"""

    def __init__(self, policy: str):
        if policy not in POLICIES:
            raise RefereeError(f"未知交接办法 {policy!r}，须为 {POLICIES}")
        self.policy = policy
        self.ends: dict[str, End] = {}
        # 实体桌是共享物理资源：记录当前占桌的局；它放桌后后续局才能摆壶。
        self.table_holder: str | None = None

    # -- 立案 ------------------------------------------------------------

    def open_end(
        self,
        end_id: str,
        desk: ScoreDesk,
        plates: Sequence[Plate],
        evidence: Sequence[EvidenceFrame],
        *,
        stone_count: int,
    ) -> End:
        replay_limit_ok = stone_count <= MAX_REPLAY_STONES
        end = End(
            end_id=end_id,
            desk=desk,
            plates=tuple(plates),
            evidence=tuple(evidence),
            replay_limit_ok=replay_limit_ok,
        )
        end.log(
            f"立案：{len(plates)} 套相容校正、{stone_count} 只壶"
            + ("" if replay_limit_ok else f"（超过 {MAX_REPLAY_STONES} 壶，不受理并列回放）")
            + f"，封存参照 {[e.frame_event_id for e in evidence]}"
        )
        self.ends[end_id] = end
        if self.table_holder is None:
            self.table_holder = end_id
        return end

    # -- 计分 ------------------------------------------------------------

    def accept_score(self, end_id: str) -> CheckpointVerdict:
        """受理计分：只签全部相容盘共同支持的保证分。"""

        end = self._get(end_id)
        rng = end.range
        if rng.settled:
            end.signed_score = rng.guaranteed
            end.signed_side = rng.side
            note = f"分数唯一，签 {rng.side} {rng.guaranteed} 分"
            end.log(note)
            return CheckpointVerdict(end_id, True, rng.guaranteed, rng.side,
                                     self.table_holder is None, note)
        note = (
            f"存在 {len(end.plates)} 套相容校正（{rng.side} 方 "
            f"{rng.guaranteed}..{rng.best} 分），最后一壶移走/歧义消除前不得签死；"
            f"此刻只能登记保证分 {rng.guaranteed}"
        )
        end.log(note)
        return CheckpointVerdict(end_id, False, None, rng.side,
                                 not end.table_sealed, note)

    # -- 放桌 ------------------------------------------------------------

    def release_table(self, end_id: str) -> str:
        end = self._get(end_id)
        if self.policy == SEAL and not end.range.settled:
            raise RefereeError(
                f"{end_id} 封桌办法下歧义未消，不能放桌；先补拍或登记保证分后改走还桌流程"
            )
        end.table_sealed = False
        end.released = True
        if self.table_holder == end_id:
            self.table_holder = None
        line = "实体桌释放，下一局可摆壶"
        end.log(line)
        return line

    def can_place_next(self, end_id: str) -> bool:
        """下一局能否摆壶：占桌局已放桌（或本就无人占桌）即可。"""

        self._get(end_id)  # 局号校验
        return self.table_holder is None

    # -- 补拍（仅封桌 + 实壶） ------------------------------------------

    def supplement(
        self,
        end_id: str,
        frame: EvidenceFrame,
        resolved_plates: Sequence[Plate] | None,
    ) -> SupplementReceipt:
        end = self._get(end_id)
        before = len(end.plates)
        if self.policy != SEAL:
            return SupplementReceipt(end_id, False, None, before, before,
                                     "按约还桌办法不设补拍窗口，新帧不予受理")
        if not end.table_sealed:
            return SupplementReceipt(end_id, False, None, before, before,
                                     "桌已释放、实壶已移，补拍无效（其他场次补图同样无效）")
        if not frame.live_stones:
            return SupplementReceipt(end_id, False, None, before, before,
                                     "补拍时实壶不在桌上，非实壶补量，拒绝")
        if not frame.sealed or any(
            frame.product_fingerprint != e.product_fingerprint for e in end.evidence
        ):
            return SupplementReceipt(end_id, False, None, before, before,
                                     "补拍帧与封存原帧不同产品指纹，无法共同复查")
        if resolved_plates is None or len(resolved_plates) != 1:
            return SupplementReceipt(end_id, False, frame, before, before,
                                     "补拍帧未把相容校正收敛为唯一整盘，不受理为消歧证据")
        end.plates = tuple(resolved_plates)
        end.evidence = (*end.evidence, frame)
        after = len(end.plates)
        note = "封桌期实壶补拍受理，歧义消除，可签唯一分"
        end.log(f"补拍 {frame.frame_event_id}：{before} -> {after} 套校正；{note}")
        return SupplementReceipt(end_id, True, frame, before, after, note)

    # -- 申诉 ------------------------------------------------------------

    def appeal(self, end_id: str, offered: Sequence[EvidenceFrame]) -> dict[str, object]:
        """迟到申诉：只受理还桌前已封存、且与原帧同指纹的共同参照。"""

        end = self._get(end_id)
        sealed_ids = {e.frame_event_id for e in end.evidence if e.sealed}
        accepted: list[EvidenceFrame] = []
        rejected: list[tuple[str, str]] = []
        for frame in offered:
            if frame.frame_event_id not in sealed_ids:
                rejected.append((frame.frame_event_id, "非还桌前封存的共同参照，移壶后补图无效"))
                continue
            if any(frame.product_fingerprint != e.product_fingerprint
                   for e in end.evidence if e.frame_event_id == frame.frame_event_id):
                rejected.append((frame.frame_event_id, "产品指纹与封存原帧不符"))
                continue
            accepted.append(frame)
        result = {
            "end_id": end_id,
            "policy": self.policy,
            "accepted": [f.as_json() for f in accepted],
            "rejected": [{"frame_event_id": fid, "reason": why} for fid, why in rejected],
            "range_on_sealed_evidence": end.range.as_json(),
            "signed": None if end.signed_score is None
                      else {"side": end.signed_side, "score": end.signed_score},
        }
        end.log(
            f"申诉受理 {len(accepted)} 帧、驳回 {len(rejected)} 帧；"
            f"封存证据下可证明区间 {end.range.guaranteed}..{end.range.best}"
        )
        return result

    # -- 并列回放（每局不超过六壶） ------------------------------------

    def tie_replay(self, end_id: str, plates: Sequence[Plate]) -> ScoreRange:
        end = self._get(end_id)
        if not end.replay_limit_ok:
            raise RefereeError(
                f"{end_id} 壶数超过 {MAX_REPLAY_STONES}，不受理最近壶并列回放"
            )
        rng = score_range(plates, end.desk)
        end.log(f"并列回放受理：结论 {'记零（同距）' if rng.guaranteed == 0 else f'{rng.side} {rng.guaranteed} 分'}")
        return rng

    # -- 检查点台账 ------------------------------------------------------

    def checkpoint(self, end_id: str) -> CheckpointVerdict:
        end = self._get(end_id)
        rng = end.range
        can_sign = rng.settled
        can_place = self.table_holder is None
        if can_sign:
            if rng.side is None:
                note = "唯一盘下最近双方同距，签零分"
            else:
                note = f"可签 {rng.side} {rng.guaranteed} 分"
        elif self.policy == SEAL:
            note = f"封桌待补拍：只能登记保证分 {rng.guaranteed}，桌未放、下一局不能摆壶"
        else:
            note = (f"已还桌：仅可认原帧能证明的分数范围 "
                    f"{rng.guaranteed}..{rng.best}，按保证分 {rng.guaranteed} 签收")
        return CheckpointVerdict(end_id, can_sign,
                                 rng.guaranteed if can_sign else None,
                                 rng.side, can_place, note)

    def _get(self, end_id: str) -> End:
        try:
            return self.ends[end_id]
        except KeyError:
            raise RefereeError(f"未知局号 {end_id!r}") from None
