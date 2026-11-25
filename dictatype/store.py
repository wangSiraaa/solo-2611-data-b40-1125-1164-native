"""交接仓库：把听打与校样都做成可以从刊行号重新打开的接口。

仓库布局（root 下）：
  index.json                 刊行号 -> 开档目录、状态（working/closed）
  issues/DK-0001/
    spec.json                听打会话固定规格（键/联/拍点/行宽）
    evidence.json            三路证据（实录 PCM、压痕格、行尾）
    transcript.json          听打台相容解与逐格可确定性结论
    receipts/<copy>.json     具名联只读历史收件回执（含编号勘误条）
    registry/ archive/       audiomix 素材登记仓与实际混音数据仓
  issues/DK-0001/...

语义：
- 刊行号顺序分配（DK-0001 起），开档后规格与证据不可变；
- closed（已交接）的刊行号重开为只读视图，任何写操作抛 ReopenError；
- replay 只读取历史收件回执，按封存正文+勘误条重算 body_hash 比对，
  不重新渲染、不重新求解。
"""
from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

from .desk import Evidence, SessionSpec, Transcript
from .proofroom import Receipt

ISSUE_PREFIX = "DK"
STATE_WORKING = "working"
STATE_CLOSED = "closed"


class IssueError(ValueError):
    """开档/归档非法。"""


class ReopenError(IssueError):
    """对只读（已交接）刊行号执行写操作。"""


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _atomic_write(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


@dataclass
class IssueHandle:
    issue_no: str
    root: str
    state: str
    spec: SessionSpec

    @property
    def dir(self) -> str:
        return os.path.join(self.root, "issues", self.issue_no)

    @property
    def read_only(self) -> bool:
        return self.state == STATE_CLOSED

    def _require_writable(self) -> None:
        if self.read_only:
            raise ReopenError(f"刊行号 {self.issue_no} 已交接，重开为只读，禁止写操作")

    # ----------------------------- 持久化 ----------------------------- #
    def save_evidence(self, evidence: Evidence) -> None:
        self._require_writable()
        _atomic_write(os.path.join(self.dir, "evidence.json"), evidence.to_json())

    def load_evidence(self) -> Evidence:
        return Evidence.from_json(
            json.load(open(os.path.join(self.dir, "evidence.json"), encoding="utf-8")))

    def save_transcript(self, transcript: Transcript) -> None:
        self._require_writable()
        _atomic_write(os.path.join(self.dir, "transcript.json"), transcript.to_json())

    def load_transcript(self) -> dict[str, Any]:
        return json.load(open(os.path.join(self.dir, "transcript.json"), encoding="utf-8"))

    def save_receipt(self, receipt: Receipt) -> None:
        self._require_writable()
        _atomic_write(os.path.join(self.dir, "receipts", f"{receipt.copy_id}.json"),
                      receipt.to_json())

    def load_receipt(self, copy_id: str) -> Receipt:
        return Receipt.from_json(json.load(open(
            os.path.join(self.dir, "receipts", f"{copy_id}.json"), encoding="utf-8")))

    def list_receipts(self) -> list[str]:
        rdir = os.path.join(self.dir, "receipts")
        if not os.path.isdir(rdir):
            return []
        return sorted(f[:-5] for f in os.listdir(rdir) if f.endswith(".json"))


class IssueStore:
    """刊行号仓库：开档、重开、交接、只读重播。"""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(os.path.join(root, "issues"), exist_ok=True)
        self.index_path = os.path.join(root, "index.json")
        if not os.path.exists(self.index_path):
            _atomic_write(self.index_path, {"next_seq": 1, "issues": {}})

    # ----------------------------- 索引 ----------------------------- #
    def _load_index(self) -> dict[str, Any]:
        return json.load(open(self.index_path, encoding="utf-8"))

    def _save_index(self, index: dict[str, Any]) -> None:
        _atomic_write(self.index_path, index)

    def list_issues(self) -> list[dict[str, Any]]:
        index = self._load_index()
        return [{"issue_no": no, **meta} for no, meta in sorted(index["issues"].items())]

    # ----------------------------- 开档 ----------------------------- #
    def open_new(self, spec: SessionSpec) -> IssueHandle:
        index = self._load_index()
        seq = int(index["next_seq"])
        issue_no = f"{ISSUE_PREFIX}-{seq:04d}"
        if issue_no in index["issues"]:
            raise IssueError(f"刊行号冲突: {issue_no}")
        idir = os.path.join(self.root, "issues", issue_no)
        os.makedirs(os.path.join(idir, "receipts"), exist_ok=True)
        _atomic_write(os.path.join(idir, "spec.json"), spec.to_json())
        index["issues"][issue_no] = {"state": STATE_WORKING,
                                     "session_id": spec.session_id,
                                     "dir": idir}
        index["next_seq"] = seq + 1
        self._save_index(index)
        return IssueHandle(issue_no, self.root, STATE_WORKING, spec)

    def reopen(self, issue_no: str) -> IssueHandle:
        """从刊行号重新打开；已交接刊行号返回只读句柄。"""
        index = self._load_index()
        if issue_no not in index["issues"]:
            raise IssueError(f"未知刊行号: {issue_no}")
        meta = index["issues"][issue_no]
        spec = SessionSpec.from_json(
            json.load(open(os.path.join(meta["dir"], "spec.json"), encoding="utf-8")))
        return IssueHandle(issue_no, self.root, meta["state"], spec)

    def close_issue(self, issue_no: str) -> None:
        """交接封档：此后重开只读。"""
        index = self._load_index()
        if issue_no not in index["issues"]:
            raise IssueError(f"未知刊行号: {issue_no}")
        index["issues"][issue_no]["state"] = STATE_CLOSED
        self._save_index(index)

    # ----------------------------- 只读重播 ----------------------------- #
    def replay_receipt(self, issue_no: str, copy_id: str) -> dict[str, Any]:
        """重播一次只读历史收件回执：重算 body_hash 并与存档比对。

        不触碰声学仓、不重新求解，只验证封存正文与勘误条的内容完整性，
        并回放签收结论。
        """
        handle = self.reopen(issue_no)
        receipt = handle.load_receipt(copy_id)
        recomputed = Receipt.compute_body_hash(receipt.sealed_cells, receipt.slips)
        intact = hmac.compare_digest(recomputed, receipt.body_hash)
        if not intact:
            raise IssueError(
                f"回执 {receipt.receipt_id} 内容哈希不符：存档可能被篡改")
        return {
            "mode": "read_only_replay",
            "receipt": receipt.to_json(),
            "hash_check": "ok",
            "n_sealed_cells": len(receipt.sealed_cells),
            "n_errata_slips": len(receipt.slips),
            "signable": receipt.signable,
            "hold_reason": receipt.hold_reason,
        }
