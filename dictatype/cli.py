"""dictatype 命令行：听打台与校样室的仓库化接口，全部可从刊行号重新打开。

典型流程（离线最多八击两联）：
  issue-open      按规格 JSON 开档，分配刊行号 DK-xxxx
  evidence-add    录入三路证据（实录 PCM、手录压痕格、行尾位置）
  transcribe      听打台穷举相容逐击记录，输出逐联逐格可确定性与覆盖先后
  proof-seal      校样室按揭纸时刻封存一联（可确定格封存，留疑格挂起）
  proof-fill      给留疑格挂编号勘误条
  proof-late      演示迟来压痕：已发联出勘误条、未发联同步复写吸收
  proof-sign      出具具名联签收结论与只读历史收件回执
  issue-close     交接封档（此后重开只读）
  issue-reopen    按刊行号重开
  receipt-replay  重播一次只读历史收件回执（哈希校验，不重渲染）
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from .desk import Evidence, SessionSpec, TranscriptionDesk
from .model import IndentationMark
from .proofroom import (
    HANDOFF_SEAL_AT_SEPARATION, ProofRoom,
)
from .store import IssueStore, ReopenError


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _load_spec(path: str) -> SessionSpec:
    return SessionSpec.from_json(json.load(open(path, encoding="utf-8")))


# ----------------------------- 命令实现 ----------------------------- #

def cmd_issue_open(p) -> None:
    store = IssueStore(p.store)
    spec = _load_spec(p.spec)
    handle = store.open_new(spec)
    _print({"issue_no": handle.issue_no, "state": handle.state,
            "session_id": handle.spec.session_id, "dir": handle.dir})


def cmd_issue_list(p) -> None:
    _print(IssueStore(p.store).list_issues())


def cmd_issue_reopen(p) -> None:
    handle = IssueStore(p.store).reopen(p.issue)
    _print({"issue_no": handle.issue_no, "state": handle.state,
            "read_only": handle.read_only, "session_id": handle.spec.session_id})


def cmd_issue_close(p) -> None:
    store = IssueStore(p.store)
    store.close_issue(p.issue)
    _print({"issue_no": p.issue, "state": "closed"})


def cmd_evidence_add(p) -> None:
    store = IssueStore(p.store)
    handle = store.reopen(p.issue)
    payload = json.load(open(p.evidence, encoding="utf-8"))
    evidence = Evidence(
        pcm=tuple(int(v) for v in payload["pcm"]),
        indentation={
            cid: tuple(IndentationMark.from_json(m) for m in marks)
            for cid, marks in payload.get("indentation", {}).items()},
        line_end=int(payload["line_end"]),
        pcm_source=str(payload.get("pcm_source", "inline")))
    handle.save_evidence(evidence)
    _print({"issue_no": p.issue, "saved": "evidence",
            "n_samples": len(evidence.pcm),
            "indentation_copies": list(evidence.indentation)})


def cmd_transcribe(p) -> None:
    store = IssueStore(p.store)
    handle = store.reopen(p.issue)
    evidence = handle.load_evidence()
    with TranscriptionDesk(handle.spec, handle.dir + "/desk") as desk:
        transcript = desk.solve(evidence)
    handle.save_transcript(transcript)
    out = transcript.to_json()
    if p.verbose:
        _print(out)
    else:
        _print({"issue_no": p.issue, "consistent": out["consistent"],
                "unique": out["unique"], "n_solutions": out["n_solutions"],
                "line_end": out["line_end"],
                "n_candidates_explored": out["n_candidates_explored"],
                "n_fast_mixes": out["n_fast_mixes"],
                "n_archive_confirms": out["n_archive_confirms"],
                "transcript_path": handle.dir + "/transcript.json"})


def _load_proofroom(handle) -> ProofRoom:
    data = handle.load_transcript()
    # 由存档 JSON 重建 Transcript（解/结论只读使用，ProofRoom 只消费结论）
    from .desk import CellVerdict, Transcript
    from .model import Strike
    solutions = tuple(tuple(Strike.from_json(s) for s in sol)
                      for sol in data["solutions"])
    verdicts = {
        cid: [CellVerdict(**{**v, "glyphs": tuple(v["glyphs"]),
                             "coverage_seqs": (None if v["coverage_seqs"] is None
                                               else tuple(v["coverage_seqs"]))})
              for v in vs]
        for cid, vs in data["verdicts"].items()}
    transcript = Transcript(
        session_id=data["session_id"], solutions=solutions, verdicts=verdicts,
        line_end_determinate=data["line_end_determinate"],
        line_end=data["line_end"],
        n_candidates_explored=data["n_candidates_explored"],
        n_fast_mixes=data["n_fast_mixes"],
        n_archive_confirms=data.get("n_archive_confirms", 0))
    return ProofRoom(handle.spec, transcript, handle.issue_no,
                     handoff=HANDOFF_SEAL_AT_SEPARATION)


def _restore_receipts(handle, room: ProofRoom) -> None:
    """把仓库里所有已存回执（含编号勘误条）挂回新建的校样室。

    CLI 每次调用都是独立进程，校样室不长期驻留；封存正文只读、勘误条只增，
    因此重挂回执即可在不改变封存格的前提下延续签收回执与勘误编号。
    """
    max_slip_seq = 0
    for copy_id in handle.list_receipts():
        receipt = handle.load_receipt(copy_id)
        room.receipts[copy_id] = receipt
        for s in receipt.slips:
            max_slip_seq = max(max_slip_seq, int(s.slip_no.split("-")[-1]))
    room._slip_seq = max_slip_seq


def cmd_proof_seal(p) -> None:
    store = IssueStore(p.store)
    handle = store.reopen(p.issue)
    room = _load_proofroom(handle)
    _restore_receipts(handle, room)
    if p.copy in room.receipts:
        raise ValueError(f"联 {p.copy} 已封存，禁止重复封存（正文只读）")
    sheet = next(c for c in handle.spec.copies if c.copy_id == p.copy)
    receipt = room.seal_copy(sheet)
    handle.save_receipt(receipt)
    _print({"sealed": True, **receipt.to_json()})


def cmd_proof_fill(p) -> None:
    store = IssueStore(p.store)
    handle = store.reopen(p.issue)
    room = _load_proofroom(handle)
    _restore_receipts(handle, room)
    slip = room.fill_doubtful(p.copy, p.cell, p.glyph, basis=p.basis)
    receipt = room.finalize(p.copy)
    handle.save_receipt(receipt)
    _print({"slip": slip.to_json(), "receipt": receipt.to_json()})


def cmd_proof_late(p) -> None:
    store = IssueStore(p.store)
    handle = store.reopen(p.issue)
    room = _load_proofroom(handle)
    _restore_receipts(handle, room)
    mark = IndentationMark(cell=p.cell, state=p.state, arrived_late=True)
    slip = room.admit_late_indentation(p.copy, mark, p.glyph)
    if p.copy in room.receipts:
        receipt = room.finalize(p.copy)
        handle.save_receipt(receipt)
        _print({"late_handling": "issued_copy_errata",
                "slip": slip.to_json(), "receipt": receipt.to_json()})
    else:
        _print({"late_handling": "unissued_copy_absorbed",
                "slip": slip.to_json(),
                "note": "联尚未发，证据同步复写吸收，封存前生效"})


def cmd_proof_sign(p) -> None:
    store = IssueStore(p.store)
    handle = store.reopen(p.issue)
    room = _load_proofroom(handle)
    _restore_receipts(handle, room)
    decision = room.sign_decision(p.copy)
    receipt = room.finalize(p.copy)
    handle.save_receipt(receipt)
    _print({"decision": decision, "receipt": receipt.to_json()})


def cmd_receipt_replay(p) -> None:
    result = IssueStore(p.store).replay_receipt(p.issue, p.copy)
    _print(result)


def cmd_render_demo(p) -> None:
    """不经过求解器，直接按规格+击序列 JSON 渲染声学 PCM（验收混音语义）。"""
    from .acoustic import AcousticSession
    from .model import Strike
    spec = _load_spec(p.spec)
    payload = json.load(open(p.strikes, encoding="utf-8"))
    strikes = [Strike.from_json(s) for s in payload["strikes"]]
    sess = AcousticSession(p.workdir, spec.keys)
    try:
        block = sess.render(strikes, render_id=p.render_id)
        ref = sess.render(strikes, render_id=p.render_id + "-ref",
                          use_reference=True)
        import numpy as np
        same = bool(np.array_equal(block["pcm"], ref["pcm"]))
        _print({"render_id": block["render_id"],
                "samples": block["pcm"].tolist(),
                "reference_samples": ref["pcm"].tolist(),
                "block_equals_reference": same,
                "archive_info": block["info"]})
    finally:
        sess.close()


# ----------------------------- 参数解析 ----------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dictatype",
                                 description="听写机听打台与校样室（刊行号可重开）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("issue-open"); q.add_argument("--store", required=True)
    q.add_argument("--spec", required=True); q.set_defaults(fn=cmd_issue_open)
    q = sub.add_parser("issue-list"); q.add_argument("--store", required=True)
    q.set_defaults(fn=cmd_issue_list)
    q = sub.add_parser("issue-reopen"); q.add_argument("--store", required=True)
    q.add_argument("--issue", required=True); q.set_defaults(fn=cmd_issue_reopen)
    q = sub.add_parser("issue-close"); q.add_argument("--store", required=True)
    q.add_argument("--issue", required=True); q.set_defaults(fn=cmd_issue_close)

    q = sub.add_parser("evidence-add"); q.add_argument("--store", required=True)
    q.add_argument("--issue", required=True); q.add_argument("--evidence", required=True)
    q.set_defaults(fn=cmd_evidence_add)

    q = sub.add_parser("transcribe"); q.add_argument("--store", required=True)
    q.add_argument("--issue", required=True)
    q.add_argument("--verbose", action="store_true"); q.set_defaults(fn=cmd_transcribe)

    q = sub.add_parser("proof-seal"); q.add_argument("--store", required=True)
    q.add_argument("--issue", required=True); q.add_argument("--copy", required=True)
    q.set_defaults(fn=cmd_proof_seal)

    q = sub.add_parser("proof-fill"); q.add_argument("--store", required=True)
    q.add_argument("--issue", required=True); q.add_argument("--copy", required=True)
    q.add_argument("--cell", type=int, required=True); q.add_argument("--glyph")
    q.add_argument("--basis", default="手录压痕+逐联回放"); q.set_defaults(fn=cmd_proof_fill)

    q = sub.add_parser("proof-late"); q.add_argument("--store", required=True)
    q.add_argument("--issue", required=True); q.add_argument("--copy", required=True)
    q.add_argument("--cell", type=int, required=True)
    q.add_argument("--state", choices=["present", "absent"], required=True)
    q.add_argument("--glyph"); q.set_defaults(fn=cmd_proof_late)

    q = sub.add_parser("proof-sign"); q.add_argument("--store", required=True)
    q.add_argument("--issue", required=True); q.add_argument("--copy", required=True)
    q.set_defaults(fn=cmd_proof_sign)

    q = sub.add_parser("receipt-replay"); q.add_argument("--store", required=True)
    q.add_argument("--issue", required=True); q.add_argument("--copy", required=True)
    q.set_defaults(fn=cmd_receipt_replay)

    q = sub.add_parser("render-demo"); q.add_argument("--spec", required=True)
    q.add_argument("--strikes", required=True); q.add_argument("--workdir", required=True)
    q.add_argument("--render-id", default="rdemo"); q.set_defaults(fn=cmd_render_demo)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except ReopenError as e:
        _print({"rejected": True, "read_only": True, "reason": str(e)})
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
