"""字粒身份推理：压样标记聚合 + 盒数量联合分配。

两层推理，严格区分“看清”与“看不清”：

1. 单粒观察（三值逻辑聚合）。同一字粒先后留下的标记按特征聚合：
   - present=True/False 是看清的有/无；NULL（看不清）是不完整观察，
     既不能肯定也不能否定，UNKNOWN 不参与结论；
   - 同一特征出现互相冲突的看清结论（既有 True 又有 False）= 矛盾，
     说明这些观察无法解释为同一字面，该粒立即不可用；
   - 人工 face_hint 与看清的笔画/缺口冲突时同样判矛盾（hint 不能盖过看清的事实）。
2. 整盒数量分配（CSP）。一盒按批铸的字粒另有各字数量记录；单粒候选不唯一时，
   用“每盒每字面的用量必须等于铸字数量”做联合分配。能唯一逼出字面的字粒
   （所有可行分配中字面相同）才可作为有凭据的交付；其余只能给候选集合。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import BoxCount, Job, Mark, PressError, SRC_FILE, SRC_ROW


@dataclass
class FeatureEvidence:
    """一个特征在同一字粒上的全部观察。"""

    feature: str
    seen_true: list[Mark] = field(default_factory=list)
    seen_false: list[Mark] = field(default_factory=list)
    unknown_marks: list[Mark] = field(default_factory=list)

    @property
    def contradicted(self) -> bool:
        return bool(self.seen_true and self.seen_false)

    @property
    def verdict(self) -> bool | None:
        """看清则为 True/False；只有看不清的观察则为 None（UNKNOWN）。"""
        if self.seen_true:
            return True
        if self.seen_false:
            return False
        return None


@dataclass
class PieceObservation:
    piece_id: str
    box_id: str
    features: dict[str, FeatureEvidence]
    face_hints: set[str]
    hint_contradiction: bool
    """face_hint 与看清的笔画/缺口事实冲突。"""
    mark_refs: list[dict[str, Any]]

    @property
    def contradicted(self) -> bool:
        return self.hint_contradiction or any(f.contradicted for f in self.features.values())

    def contradicts_face(self, face: str, face_def: frozenset[str]) -> bool:
        """看清的事实是否与该字面的标准特征集合冲突（UNKNOWN 不构成冲突）。"""
        for feat, ev in self.features.items():
            v = ev.verdict
            if v is None:
                continue
            if v != (feat in face_def):
                return True
        return False

    def candidate_faces(self, faces: dict[str, frozenset[str]]) -> set[str]:
        """与全部看清观察相容的字面集合；矛盾粒为空集。"""
        if self.contradicted:
            return set()
        out = set()
        for face, face_def in faces.items():
            if self.contradicts_face(face, face_def):
                continue
            if self.face_hints and face not in self.face_hints:
                continue
            out.add(face)
        return out


@dataclass
class BoxLedger:
    box_id: str
    pieces: tuple[str, ...]
    cast_counts: dict[str, int]
    """铸字数量记录（按字面）。"""
    forced_face: dict[str, str]
    """所有可行数量分配中字面都相同的字粒（单粒无法辨认也可能被整盒数量逼出）。"""
    candidate_faces: dict[str, set[str]]
    feasible: bool
    """是否存在满足铸字数量的整盒分配。"""
    witness: dict[str, str] | None
    """一份可行分配（字粒 -> 字面），作为领班凭据；不可行为 None。"""
    reason: str


@dataclass
class IdentityReport:
    pieces: dict[str, PieceObservation]
    boxes: dict[str, BoxLedger]

    def committable_face(self, piece_id: str) -> str | None:
        """该粒此刻能否被有凭据地当作某个字面交付。"""
        if piece_id not in self.pieces:
            return None
        return self.boxes[self.pieces[piece_id].box_id].forced_face.get(piece_id)

    def is_usable(self, piece_id: str) -> bool:
        ob = self.pieces.get(piece_id)
        return ob is not None and not ob.contradicted


def _mark_ref(mark: Mark) -> dict[str, Any]:
    return {
        "proof_id": mark.proof_id,
        "feature": mark.feature,
        "present": mark.present,
        "taken_at": mark.taken_at,
        SRC_FILE: mark.source_file,
        SRC_ROW: mark.source_row,
    }


def aggregate_marks(marks: list[Mark], job: Job) -> dict[str, PieceObservation]:
    """按字粒聚合全部被采信的压样标记。"""
    pieces: dict[str, PieceObservation] = {}
    for mark in marks:
        ob = pieces.get(mark.piece_id)
        if ob is None:
            ob = PieceObservation(
                piece_id=mark.piece_id,
                box_id=mark.box_id or "",
                features={},
                face_hints=set(),
                hint_contradiction=False,
                mark_refs=[],
            )
            pieces[mark.piece_id] = ob
        elif mark.box_id and ob.box_id and mark.box_id != ob.box_id:
            raise PressError(
                f"字粒 {mark.piece_id} 在压样中先后属于不同字盒 "
                f"({ob.box_id} / {mark.box_id})，来源行 {mark.source_row}"
            )
        elif mark.box_id and not ob.box_id:
            ob.box_id = mark.box_id
        ob.mark_refs.append(_mark_ref(mark))

        if mark.face_hint is not None:
            if mark.face_hint not in job.faces:
                raise PressError(
                    f"压样 {mark.proof_id} 的 face_hint={mark.face_hint!r} 未在 faces 字典中定义"
                )
            ob.face_hints.add(mark.face_hint)

        ev = ob.features.setdefault(mark.feature, FeatureEvidence(mark.feature))
        if mark.present is True:
            ev.seen_true.append(mark)
        elif mark.present is False:
            ev.seen_false.append(mark)
        else:
            ev.unknown_marks.append(mark)

    # hint 与看清事实的冲突：hint 指向的每个字面都必须与看清特征相容。
    for ob in pieces.values():
        if not ob.face_hints:
            continue
        if not any(
            not ob.contradicts_face(face, job.faces[face])
            and face in ob.face_hints
            for face in ob.face_hints
        ):
            ob.hint_contradiction = True
    return pieces


def _solve_box(
    box_id: str,
    piece_ids: tuple[str, ...],
    pieces: dict[str, PieceObservation],
    cast_counts: dict[str, int],
    faces: dict[str, frozenset[str]],
) -> BoxLedger:
    domains = {pid: pieces[pid].candidate_faces(faces) for pid in piece_ids}
    quota = {face: cast_counts.get(face, 0) for face in faces}

    if sum(quota.values()) != len(piece_ids):
        return BoxLedger(
            box_id=box_id, pieces=piece_ids, cast_counts=quota, forced_face={},
            candidate_faces=domains, feasible=False, witness=None,
            reason=f"盒内字粒数 {len(piece_ids)} 与铸字数量合计 {sum(quota.values())} 不符，数量记录无法兑现",
        )
    if any(not d for d in domains.values()):
        bad = [pid for pid, d in domains.items() if not d]
        return BoxLedger(
            box_id=box_id, pieces=piece_ids, cast_counts=quota, forced_face={},
            candidate_faces=domains, feasible=False, witness=None,
            reason=f"字粒 {bad} 的看清观察互相矛盾或与所有字面冲突，无任何可分配字面",
        )

    # 回溯求全部可行分配中均不变的字面（MRV 选粒，字典序保证结果确定）。
    used = {face: 0 for face in faces}
    assignment: dict[str, str] = {}
    solutions: list[dict[str, str]] = []
    cap = 1000  # 可行解只需要足够支撑“是否唯一逼出”的结论，封顶防止组合爆炸

    def backtrack() -> None:
        if len(solutions) >= cap:
            return
        if len(assignment) == len(piece_ids):
            solutions.append(dict(assignment))
            return
        pid = min(
            (p for p in piece_ids if p not in assignment),
            key=lambda p: (len(domains[p] - {f for f in faces if used[f] >= quota[f]}), p),
        )
        for face in sorted(domains[pid]):
            if used[face] >= quota[face]:
                continue
            assignment[pid] = face
            used[face] += 1
            backtrack()
            used[face] -= 1
            del assignment[pid]
            if len(solutions) >= cap:
                return

    backtrack()
    if not solutions:
        return BoxLedger(
            box_id=box_id, pieces=piece_ids, cast_counts=quota, forced_face={},
            candidate_faces=domains, feasible=False, witness=None,
            reason="不存在同时满足单粒候选与整盒铸字数量的分配，该盒数量记录无法兑现",
        )

    forced: dict[str, str] = {}
    for pid in piece_ids:
        vals = {sol[pid] for sol in solutions}
        if len(vals) == 1:
            forced[pid] = next(iter(vals))
    truncated = len(solutions) >= cap
    reason = (
        f"找到 {len(solutions)} 份可行分配"
        + ("（达到枚举上限，结论仅在已枚举解上成立）" if truncated else "")
        + f"，其中 {len(forced)} 粒被唯一逼出字面"
    )
    return BoxLedger(
        box_id=box_id, pieces=piece_ids, cast_counts=quota, forced_face=forced,
        candidate_faces=domains, feasible=True, witness=solutions[0], reason=reason,
    )


def build_identity_report(
    marks: list[Mark], box_counts: list[BoxCount], job: Job
) -> IdentityReport:
    pieces = aggregate_marks(marks, job)

    # 盒台账：以数量记录里出现的盒为准，压样里的盒必须能在数量记录中落户。
    count_rows: dict[str, dict[str, int]] = {}
    for bc in box_counts:
        if bc.face not in job.faces:
            raise PressError(
                f"盒数量记录 {bc.box_id} 出现未定义字面 {bc.face}（来源行 {bc.source_row}）"
            )
        count_rows.setdefault(bc.box_id, {})[bc.face] = (
            count_rows.setdefault(bc.box_id, {}).get(bc.face, 0) + bc.count
        )

    by_box: dict[str, list[str]] = {}
    for pid, ob in pieces.items():
        if not ob.box_id:
            raise PressError(f"字粒 {pid} 的压样从未给出盒号，无法落户到任何字盒")
        if ob.box_id not in count_rows:
            raise PressError(f"字粒 {pid} 所属盒 {ob.box_id} 没有铸字数量记录")
        by_box.setdefault(ob.box_id, []).append(pid)

    boxes: dict[str, BoxLedger] = {}
    for box_id, cast_counts in sorted(count_rows.items()):
        pids = tuple(sorted(by_box.get(box_id, [])))
        if not pids:
            raise PressError(
                f"盒 {box_id} 只有铸字数量记录，压样中却没有任何属于该盒的字粒"
            )
        boxes[box_id] = _solve_box(box_id, pids, pieces, cast_counts, job.faces)

    return IdentityReport(pieces=pieces, boxes=boxes)
