"""地方译本索引出版服务（SQLite 持久化）。

在 :mod:`confcat.biblio` 的纯规则之上，提供题面要求的可调用编目与签发入口：

编目端
------
* :meth:`BiblioService.load_declarations_from_task` —— 用**已有接入任务**归一得到
  的关系表与列来源（行号 + 列血缘）读取署名/笔名声明及其生效卷期；
* :meth:`BiblioService.submit_revision` —— 编辑提交题例以外的卷号、印本版次与
  一条声明修订；``amend`` 出新版本、``withdraw`` 撤回后重新形成候选身份。
  每次修订落一条不可变版本，旧版本全部保留（可凭凭证号调回）。

签发端（两种办索引办法，任选其一）
----------------------------------
* :meth:`issue_reprint_voucher` —— 重印配号：须凭印前核签的具名底稿绑定译者，
  纸索引与书一同发出；事后改归属走 :meth:`issue_errata`，留可追到旧册的勘误；
* :meth:`issue_inquiry` —— 馆员查问：只按来人手中版次上看得见的字逐问，
  能分开的当场签认，问不出的写明缺项（受理查询但不出凭证）。

收册 :meth:`register_copy` 对每一册都开受理凭证；岗位权限在各入口强制；
读者视图 :meth:`public_lookup` 不暴露出版社私藏的印前校样。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .biblio import (
    BiblioError,
    DeclarationSource,
    HoldingCopy,
    HoldingEvaluation,
    PenNameDeclaration,
    evaluate_holding,
)

# 岗位
ROLE_EDITOR = "editor"          # 编辑部：修订/撤回/勘误
ROLE_PREPRESS = "prepress"      # 印前核签岗位：具名校样核验
ROLE_LIBRARIAN = "librarian"    # 馆员：收册、查问、签发
ROLE_READER = "reader"          # 持书读者/公众：只看该版能答的信息

DEFAULT_REVISION_LIMIT = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS declaration_version (
    decl_id        TEXT NOT NULL,
    version        INTEGER NOT NULL,
    pen_name       TEXT NOT NULL,
    translator_id  TEXT NOT NULL,
    translator_name TEXT NOT NULL,
    original_language TEXT,
    volume_from    INTEGER,
    volume_to      INTEGER,
    withdrawn      INTEGER NOT NULL,
    source_json    TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    revision_no    INTEGER,
    PRIMARY KEY (decl_id, version)
);
CREATE TABLE IF NOT EXISTS declaration_state (
    decl_id         TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL,
    withdrawn       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS revision_log (
    revision_no   INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    decl_id       TEXT NOT NULL,
    version       INTEGER NOT NULL,
    actor         TEXT NOT NULL,
    note          TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holding_copy (
    copy_id        TEXT PRIMARY KEY,
    volume         INTEGER NOT NULL,
    edition        TEXT NOT NULL,
    pen_name       TEXT NOT NULL,
    title          TEXT NOT NULL,
    original_language TEXT,
    print_number   TEXT,
    visible_fields_json TEXT NOT NULL,
    printable_new_edition INTEGER NOT NULL,
    received_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prepress_proof (
    proof_id       TEXT PRIMARY KEY,
    copy_id        TEXT NOT NULL,
    draft_no       TEXT NOT NULL,
    signer_translator_id TEXT NOT NULL,
    signer_translator_name TEXT NOT NULL,
    verified       INTEGER NOT NULL,
    verifier       TEXT,
    verified_at    TEXT,
    private        INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS print_number (
    number        TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    consumed_by_copy TEXT,
    consumed_by_voucher TEXT
);
CREATE TABLE IF NOT EXISTS counter_receipt (
    receipt_id    TEXT PRIMARY KEY,
    copy_id       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    note          TEXT NOT NULL,
    result_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voucher (
    voucher_id    TEXT PRIMARY KEY,
    copy_id       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    translator_id TEXT,
    print_number  TEXT,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    actor         TEXT NOT NULL,
    eval_json     TEXT NOT NULL,
    decl_snapshot_json TEXT NOT NULL,
    superseded_by TEXT
);
CREATE TABLE IF NOT EXISTS errata (
    errata_id       TEXT PRIMARY KEY,
    old_voucher_id  TEXT NOT NULL,
    new_voucher_id  TEXT NOT NULL,
    copy_id         TEXT NOT NULL,
    print_number    TEXT NOT NULL,
    old_translator_id TEXT NOT NULL,
    new_translator_id TEXT NOT NULL,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    actor           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


@dataclass
class Actor:
    name: str
    role: str


@dataclass
class PrepressProof:
    proof_id: str
    copy_id: str
    draft_no: str
    signer_translator_id: str
    signer_translator_name: str
    verified: bool
    verifier: str | None
    verified_at: str | None
    private: bool = True


class BiblioService:
    """单连接串行使用的译本索引服务。"""

    def __init__(
        self,
        db_path: str | Path,
        revision_limit: int = DEFAULT_REVISION_LIMIT,
    ):
        self.db_path = str(db_path)
        self.revision_limit = revision_limit
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "BiblioService":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise BiblioError(
                f"岗位 {actor.role!r} 无权执行该操作，需要 {'/'.join(roles)}",
                reason="forbidden",
            )

    def _audit(self, actor: Actor, action: str, detail: dict) -> None:
        self.conn.execute(
            "INSERT INTO audit_log(at, actor, action, detail_json) VALUES(?, ?, ?, ?)",
            (_now(), actor.name, action, json.dumps(detail, ensure_ascii=False)),
        )

    # ================================================================== #
    # 声明：版本化读取
    # ================================================================== #
    def _next_version(self, decl_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM declaration_version WHERE decl_id = ?",
            (decl_id,),
        ).fetchone()
        return int(row["v"]) + 1

    def _revision_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM revision_log").fetchone()
        return int(row["c"])

    def _next_revision_no(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(revision_no), 0) + 1 AS n FROM revision_log"
        ).fetchone()
        return int(row["n"])

    def _insert_version(
        self,
        decl_id: str,
        pen_name: str,
        translator_id: str,
        translator_name: str,
        source: DeclarationSource,
        original_language: str | None,
        volume_from: int | None,
        volume_to: int | None,
        withdrawn: bool,
        revision_no: int | None,
    ) -> PenNameDeclaration:
        version = self._next_version(decl_id)
        decl = PenNameDeclaration(
            decl_id=decl_id,
            version=version,
            pen_name=pen_name,
            translator_id=translator_id,
            translator_name=translator_name,
            source=source,
            original_language=original_language,
            volume_from=volume_from,
            volume_to=volume_to,
            withdrawn=withdrawn,
        )
        self.conn.execute(
            "INSERT INTO declaration_version(decl_id, version, pen_name, translator_id,"
            " translator_name, original_language, volume_from, volume_to, withdrawn,"
            " source_json, created_at, revision_no) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decl_id, version, pen_name, translator_id, translator_name,
                original_language, volume_from, volume_to, 1 if withdrawn else 0,
                json.dumps(source.as_dict(), ensure_ascii=False), _now(), revision_no,
            ),
        )
        self.conn.execute(
            "INSERT INTO declaration_state(decl_id, current_version, withdrawn) VALUES(?, ?, ?)"
            " ON CONFLICT(decl_id) DO UPDATE SET current_version = excluded.current_version,"
            " withdrawn = excluded.withdrawn",
            (decl_id, version, 1 if withdrawn else 0),
        )
        return decl

    def load_declarations_from_task(
        self,
        actor: Actor,
        task_repo: Any,
        task_id: str,
        id_column: str = "decl_id",
    ) -> list[PenNameDeclaration]:
        """从已有接入任务的归一关系表+列血缘装载初始声明（不计入修订次数）。

        目标列约定：``decl_id/pen_name/translator_id/translator_name`` 必填；
        ``original_language/volume_from/volume_to/note`` 可选。
        """
        self._require(actor, ROLE_EDITOR)
        rows = task_repo.get_rows(task_id)
        lineage = {c["target"]: c["source_column"] for c in task_repo.get_lineage(task_id)}
        loaded: list[PenNameDeclaration] = []
        for row_no, row in enumerate(rows):
            decl_id = row[id_column]
            if self.conn.execute(
                "SELECT 1 FROM declaration_state WHERE decl_id = ?", (decl_id,)
            ).fetchone():
                continue  # 已装载过的声明不重复建
            source = DeclarationSource(
                task_id=task_id,
                row_no=row_no,
                columns={t: s for t, s in lineage.items() if t in row},
                note=row.get("note", ""),
            )
            decl = self._insert_version(
                decl_id=decl_id,
                pen_name=row["pen_name"],
                translator_id=row["translator_id"],
                translator_name=row["translator_name"],
                source=source,
                original_language=row.get("original_language") or None,
                volume_from=_parse_optional_int(row.get("volume_from")),
                volume_to=_parse_optional_int(row.get("volume_to")),
                withdrawn=False,
                revision_no=None,
            )
            loaded.append(decl)
        self._audit(actor, "load-declarations", {"task_id": task_id, "count": len(loaded)})
        self.conn.commit()
        return loaded

    def submit_revision(
        self,
        actor: Actor,
        kind: str,
        decl_id: str,
        note: str,
        pen_name: str | None = None,
        translator_id: str | None = None,
        translator_name: str | None = None,
        original_language: str | None = None,
        volume_from: int | None = None,
        volume_to: int | None = None,
        source: DeclarationSource | None = None,
    ) -> PenNameDeclaration:
        """编辑提交一条声明修订。

        kind=``amend``：新增/修订一条声明（题例以外的卷号、印本版次带来的新依据）；
        kind=``withdraw``：撤回既有声明，重新形成候选身份。
        整库修订（不含初始装载）至多 ``revision_limit`` 次。
        """
        self._require(actor, ROLE_EDITOR)
        if kind not in ("amend", "withdraw"):
            raise BiblioError(f"未知修订类型 {kind!r}", reason="bad-revision-kind")
        if self._revision_count() >= self.revision_limit:
            raise BiblioError(
                f"修订次数已达上限 {self.revision_limit}（三人/六册/至多三次修订）",
                reason="revision-limit",
            )
        revision_no = self._next_revision_no()

        state_row = self.conn.execute(
            "SELECT * FROM declaration_state WHERE decl_id = ?", (decl_id,)
        ).fetchone()

        if kind == "withdraw":
            if state_row is None:
                raise BiblioError(f"无法撤回：声明 {decl_id!r} 不存在", reason="no-such-declaration")
            current = self.get_declaration_version(decl_id, int(state_row["current_version"]))
            decl = self._insert_version(
                decl_id=current.decl_id,
                pen_name=current.pen_name,
                translator_id=current.translator_id,
                translator_name=current.translator_name,
                source=current.source,
                original_language=current.original_language,
                volume_from=current.volume_from,
                volume_to=current.volume_to,
                withdrawn=True,
                revision_no=revision_no,
            )
        else:
            if state_row is not None:
                base = self.get_declaration_version(decl_id, int(state_row["current_version"]))
                pen_name = pen_name or base.pen_name
                translator_id = translator_id or base.translator_id
                translator_name = translator_name or base.translator_name
                source = source or base.source
                # 显式 None 与“未提供”都按可空处理：amend 允许收窄/放宽范围
                original_language = original_language if original_language is not None else base.original_language
                volume_from = volume_from if volume_from is not None else base.volume_from
                volume_to = volume_to if volume_to is not None else base.volume_to
            else:
                missing = [
                    n for n, v in (
                        ("pen_name", pen_name),
                        ("translator_id", translator_id),
                        ("translator_name", translator_name),
                    ) if v is None
                ]
                if missing:
                    raise BiblioError(
                        f"新声明 {decl_id!r} 缺少必填字段 {missing}", reason="missing-field"
                    )
                source = source or DeclarationSource(
                    task_id="editor-revision", row_no=-1, columns={}, note=note
                )
            decl = self._insert_version(
                decl_id=decl_id,
                pen_name=pen_name,
                translator_id=translator_id,
                translator_name=translator_name,
                source=source,
                original_language=original_language,
                volume_from=volume_from,
                volume_to=volume_to,
                withdrawn=False,
                revision_no=revision_no,
            )

        self.conn.execute(
            "INSERT INTO revision_log(revision_no, kind, decl_id, version, actor, note,"
            " payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_no, kind, decl_id, decl.version, actor.name, note,
                json.dumps(decl.as_dict(), ensure_ascii=False), _now(),
            ),
        )
        self._audit(actor, f"revision-{kind}", {"decl_id": decl_id, "version": decl.version})
        self.conn.commit()
        return decl

    def get_declaration_version(self, decl_id: str, version: int) -> PenNameDeclaration:
        row = self.conn.execute(
            "SELECT * FROM declaration_version WHERE decl_id = ? AND version = ?",
            (decl_id, version),
        ).fetchone()
        if row is None:
            raise BiblioError(f"声明版本 {decl_id}@v{version} 不存在", reason="no-such-version")
        src = json.loads(row["source_json"])
        return PenNameDeclaration(
            decl_id=row["decl_id"],
            version=row["version"],
            pen_name=row["pen_name"],
            translator_id=row["translator_id"],
            translator_name=row["translator_name"],
            source=DeclarationSource(
                task_id=src["task_id"], row_no=src["row_no"],
                columns=src["columns"], note=src.get("note", ""),
            ),
            original_language=row["original_language"],
            volume_from=row["volume_from"],
            volume_to=row["volume_to"],
            withdrawn=bool(row["withdrawn"]),
        )

    def active_declarations(self) -> list[PenNameDeclaration]:
        """当前生效（未撤回）的声明，取每个 decl_id 的当前版本。"""
        rows = self.conn.execute(
            "SELECT d.* FROM declaration_version d JOIN declaration_state s"
            " ON d.decl_id = s.decl_id AND d.version = s.current_version"
            " WHERE s.withdrawn = 0 ORDER BY d.decl_id"
        ).fetchall()
        return [self.get_declaration_version(r["decl_id"], r["version"]) for r in rows]

    def list_revisions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM revision_log ORDER BY revision_no"
        ).fetchall()
        return [dict(r) for r in rows]

    # ================================================================== #
    # 交柜书册与印前校样
    # ================================================================== #
    def register_copy(
        self,
        actor: Actor,
        copy_id: str,
        volume: int,
        edition: str,
        pen_name: str,
        visible_fields: list[str] | tuple[str, ...],
        title: str = "",
        original_language: str | None = None,
        print_number: str | None = None,
        printable_new_edition: bool = False,
        note: str = "",
    ) -> dict:
        """收册：无论能否签出都开受理凭证，并按当前关系表给出可签依据或待查原因。"""
        self._require(actor, ROLE_LIBRARIAN, ROLE_EDITOR)
        if self.conn.execute("SELECT 1 FROM holding_copy WHERE copy_id = ?", (copy_id,)).fetchone():
            raise BiblioError(f"书册 {copy_id!r} 已收过", reason="duplicate-copy")
        self.conn.execute(
            "INSERT INTO holding_copy(copy_id, volume, edition, pen_name, title,"
            " original_language, print_number, visible_fields_json, printable_new_edition,"
            " received_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                copy_id, volume, edition, pen_name, title, original_language, print_number,
                json.dumps(sorted(set(visible_fields)), ensure_ascii=False),
                1 if printable_new_edition else 0, _now(),
            ),
        )
        evaluation = self._evaluate(copy_id)
        receipt_id = f"R-{copy_id}"
        self.conn.execute(
            "INSERT INTO counter_receipt(receipt_id, copy_id, created_at, note, result_json)"
            " VALUES(?, ?, ?, ?, ?)",
            (receipt_id, copy_id, _now(), note,
             json.dumps(evaluation.as_dict(), ensure_ascii=False)),
        )
        self._audit(actor, "register-copy", {"copy_id": copy_id, "status": evaluation.status})
        self.conn.commit()
        return {"receipt_id": receipt_id, "evaluation": evaluation.as_dict()}

    def _load_copy(self, copy_id: str) -> HoldingCopy:
        row = self.conn.execute(
            "SELECT * FROM holding_copy WHERE copy_id = ?", (copy_id,)
        ).fetchone()
        if row is None:
            raise BiblioError(f"书册 {copy_id!r} 未收柜", reason="no-such-copy")
        return HoldingCopy(
            copy_id=row["copy_id"],
            volume=row["volume"],
            edition=row["edition"],
            pen_name=row["pen_name"],
            visible_fields=frozenset(json.loads(row["visible_fields_json"])),
            print_number=row["print_number"],
            original_language=row["original_language"],
            title=row["title"],
            printable_new_edition=bool(row["printable_new_edition"]),
        )

    def verify_prepress_proof(
        self,
        actor: Actor,
        proof_id: str,
        copy_id: str,
        draft_no: str,
        signer_translator_id: str,
        signer_translator_name: str,
    ) -> PrepressProof:
        """印前具名校样核验：仅获权的印前岗位可执行；校样为出版社私藏（private）。"""
        self._require(actor, ROLE_PREPRESS)
        copy = self._load_copy(copy_id)
        if not copy.printable_new_edition:
            raise BiblioError(
                f"{copy_id!r} 不是可印新版，不受理印前校样", reason="not-printable"
            )
        # 具名校样上的署名者必须是该册当前关系表里的候选，否则底稿与书对不上
        current_ids = {c.translator_id for c in evaluate_holding(copy, self.active_declarations()).candidates}
        if signer_translator_id not in current_ids:
            raise BiblioError(
                f"底稿具名译者 {signer_translator_id} 不在 {copy_id} 的当前候选 {sorted(current_ids)} 中",
                reason="signer-not-candidate",
            )
        verified_at = _now()
        self.conn.execute(
            "INSERT INTO prepress_proof(proof_id, copy_id, draft_no, signer_translator_id,"
            " signer_translator_name, verified, verifier, verified_at, private)"
            " VALUES(?, ?, ?, ?, ?, 1, ?, ?, 1)",
            (proof_id, copy_id, draft_no, signer_translator_id,
             signer_translator_name, actor.name, verified_at),
        )
        self._audit(actor, "verify-proof", {"proof_id": proof_id, "copy_id": copy_id})
        self.conn.commit()
        return PrepressProof(
            proof_id=proof_id, copy_id=copy_id, draft_no=draft_no,
            signer_translator_id=signer_translator_id,
            signer_translator_name=signer_translator_name,
            verified=True, verifier=actor.name, verified_at=verified_at, private=True,
        )

    def _verified_proof_for_copy(self, copy_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM prepress_proof WHERE copy_id = ? AND verified = 1 ORDER BY verified_at",
            (copy_id,),
        ).fetchone()

    # ================================================================== #
    # 印号池
    # ================================================================== #
    def add_print_numbers(self, actor: Actor, numbers: list[str]) -> None:
        self._require(actor, ROLE_EDITOR, ROLE_LIBRARIAN)
        for n in numbers:
            self.conn.execute(
                "INSERT OR IGNORE INTO print_number(number, status) VALUES(?, 'available')", (n,)
            )
        self._audit(actor, "add-print-numbers", {"numbers": numbers})
        self.conn.commit()

    def available_print_numbers(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT number FROM print_number WHERE status = 'available' ORDER BY number"
        ).fetchall()
        return [r["number"] for r in rows]

    def _take_print_number(self, copy_id: str, voucher_id: str) -> str:
        available = self.available_print_numbers()
        if not available:
            raise BiblioError("库里已无可用印号", reason="no-print-number")
        number = available[0]
        self.conn.execute(
            "UPDATE print_number SET status = 'consumed', consumed_by_copy = ?,"
            " consumed_by_voucher = ? WHERE number = ?",
            (copy_id, voucher_id, number),
        )
        return number

    # ================================================================== #
    # 评估与签发
    # ================================================================== #
    def _evaluate(self, copy_id: str) -> HoldingEvaluation:
        copy = self._load_copy(copy_id)
        return evaluate_holding(copy, self.active_declarations())

    def _snapshot(self, evaluation: HoldingEvaluation) -> list[dict]:
        """凭证采用的声明版本快照（次日凭凭证号原样调回）。"""
        snap: dict[str, dict] = {}
        for cand in evaluation.candidates:
            d = cand.declaration
            snap[f"{d.decl_id}@v{d.version}"] = d.as_dict()
        return [snap[k] for k in sorted(snap)]

    def issue_reprint_voucher(self, actor: Actor, copy_id: str) -> dict:
        """重印配号：凭印前核签的具名底稿给这一册绑定译者并占用一个印号。"""
        self._require(actor, ROLE_LIBRARIAN)
        copy = self._load_copy(copy_id)
        if not copy.printable_new_edition:
            raise BiblioError(
                f"{copy_id} 非待印新版，不能走重印配号（旧藏不得凭空改扉页）",
                reason="not-printable",
            )
        proof = self._verified_proof_for_copy(copy_id)
        if proof is None:
            raise BiblioError(
                f"{copy_id} 缺印前核签的具名底稿，不能配号", reason="no-verified-proof"
            )
        evaluation = evaluate_holding(copy, self.active_declarations())
        candidate_ids = {c.translator_id for c in evaluation.candidates}
        if proof["signer_translator_id"] not in candidate_ids:
            raise BiblioError(
                "底稿具名译者已不在当前候选关系中，请先由编辑部处理声明",
                reason="signer-not-candidate",
            )

        voucher_id = f"V-{copy_id}"
        if self.conn.execute("SELECT 1 FROM voucher WHERE voucher_id = ?", (voucher_id,)).fetchone():
            raise BiblioError(f"{copy_id} 已签过重印凭证", reason="duplicate-voucher")
        number = self._take_print_number(copy_id, voucher_id)
        self.conn.execute(
            "UPDATE holding_copy SET print_number = ? WHERE copy_id = ?", (number, copy_id)
        )
        self.conn.execute(
            "INSERT INTO voucher(voucher_id, copy_id, kind, translator_id, print_number,"
            " status, created_at, actor, eval_json, decl_snapshot_json)"
            " VALUES(?, ?, 'reprint', ?, ?, 'active', ?, ?, ?, ?)",
            (
                voucher_id, copy_id, proof["signer_translator_id"], number, _now(), actor.name,
                json.dumps(evaluation.as_dict(), ensure_ascii=False),
                json.dumps(self._snapshot(evaluation), ensure_ascii=False),
            ),
        )
        self._audit(
            actor, "issue-reprint",
            {"copy_id": copy_id, "voucher_id": voucher_id, "print_number": number,
             "translator_id": proof["signer_translator_id"]},
        )
        self.conn.commit()
        return self.get_voucher(voucher_id)

    def issue_inquiry(self, actor: Actor, copy_id: str) -> dict:
        """馆员查问：按手中版次看得见的字逐问；唯一则签认，否则只登记缺项。"""
        self._require(actor, ROLE_LIBRARIAN)
        copy = self._load_copy(copy_id)
        evaluation = evaluate_holding(copy, self.active_declarations())
        receipt = self.conn.execute(
            "SELECT receipt_id FROM counter_receipt WHERE copy_id = ? ORDER BY rowid DESC LIMIT 1",
            (copy_id,),
        ).fetchone()
        if not evaluation.unique:
            # 问不出：受理查询，写明还缺哪项，不出凭证
            self._audit(actor, "inquiry-pending", {
                "copy_id": copy_id, "missing": list(evaluation.missing_fields),
            })
            self.conn.commit()
            return {
                "voucher": None,
                "receipt_id": receipt["receipt_id"] if receipt else None,
                "evaluation": evaluation.as_dict(),
                "pending_reason": (
                    "候选不唯一，还缺可核对字段: "
                    + ", ".join(evaluation.missing_fields)
                    if evaluation.missing_fields else "无成立候选"
                ),
            }

        voucher_id = f"Q-{copy_id}"
        if self.conn.execute("SELECT 1 FROM voucher WHERE voucher_id = ?", (voucher_id,)).fetchone():
            raise BiblioError(f"{copy_id} 已有查问凭证", reason="duplicate-voucher")
        self.conn.execute(
            "INSERT INTO voucher(voucher_id, copy_id, kind, translator_id, print_number,"
            " status, created_at, actor, eval_json, decl_snapshot_json)"
            " VALUES(?, ?, 'inquiry', ?, NULL, 'active', ?, ?, ?, ?)",
            (
                voucher_id, copy_id, evaluation.resolved_translator_id, _now(), actor.name,
                json.dumps(evaluation.as_dict(), ensure_ascii=False),
                json.dumps(self._snapshot(evaluation), ensure_ascii=False),
            ),
        )
        self._audit(actor, "issue-inquiry", {
            "copy_id": copy_id, "voucher_id": voucher_id,
            "translator_id": evaluation.resolved_translator_id,
        })
        self.conn.commit()
        return {"voucher": self.get_voucher(voucher_id), "evaluation": evaluation.as_dict()}

    def issue_errata(
        self, actor: Actor, old_voucher_id: str, new_translator_id: str, reason: str
    ) -> dict:
        """事后改归属：旧凭证不动，另发勘误，留可追到旧册/旧印号的记录。"""
        self._require(actor, ROLE_EDITOR)
        old = self.conn.execute(
            "SELECT * FROM voucher WHERE voucher_id = ?", (old_voucher_id,)
        ).fetchone()
        if old is None:
            raise BiblioError(f"凭证 {old_voucher_id!r} 不存在", reason="no-such-voucher")
        copy = self._load_copy(old["copy_id"])
        evaluation = evaluate_holding(copy, self.active_declarations())
        candidate_ids = {c.translator_id for c in evaluation.candidates}
        if new_translator_id not in candidate_ids:
            raise BiblioError(
                f"新译者 {new_translator_id} 不在 {old['copy_id']} 当前候选 {sorted(candidate_ids)} 中",
                reason="new-translator-not-candidate",
            )
        new_voucher_id = f"V2-{old['copy_id']}"
        # 勘误沿用旧册印号，不占新印号；纸索引勘误与旧册绑定
        self.conn.execute(
            "INSERT INTO voucher(voucher_id, copy_id, kind, translator_id, print_number,"
            " status, created_at, actor, eval_json, decl_snapshot_json, superseded_by)"
            " VALUES(?, ?, 'reprint', ?, ?, 'active', ?, ?, ?, ?, NULL)",
            (
                new_voucher_id, old["copy_id"], new_translator_id, old["print_number"],
                _now(), actor.name,
                json.dumps(evaluation.as_dict(), ensure_ascii=False),
                json.dumps(self._snapshot(evaluation), ensure_ascii=False),
            ),
        )
        self.conn.execute(
            "UPDATE voucher SET status = 'superseded', superseded_by = ? WHERE voucher_id = ?",
            (new_voucher_id, old_voucher_id),
        )
        errata_id = f"E-{old['copy_id']}"
        self.conn.execute(
            "INSERT INTO errata(errata_id, old_voucher_id, new_voucher_id, copy_id,"
            " print_number, old_translator_id, new_translator_id, reason, created_at, actor)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                errata_id, old_voucher_id, new_voucher_id, old["copy_id"],
                old["print_number"], old["translator_id"], new_translator_id,
                reason, _now(), actor.name,
            ),
        )
        self._audit(actor, "issue-errata", {
            "errata_id": errata_id, "old_voucher_id": old_voucher_id,
            "new_voucher_id": new_voucher_id,
        })
        self.conn.commit()
        return {
            "errata_id": errata_id,
            "old_voucher": self.get_voucher(old_voucher_id),
            "new_voucher": self.get_voucher(new_voucher_id),
        }

    # ================================================================== #
    # 查卷与倒查
    # ================================================================== #
    def get_voucher(self, voucher_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM voucher WHERE voucher_id = ?", (voucher_id,)
        ).fetchone()
        if row is None:
            raise BiblioError(f"凭证 {voucher_id!r} 不存在", reason="no-such-voucher")
        return {
            "voucher_id": row["voucher_id"],
            "copy_id": row["copy_id"],
            "kind": row["kind"],
            "translator_id": row["translator_id"],
            "print_number": row["print_number"],
            "status": row["status"],
            "created_at": row["created_at"],
            "actor": row["actor"],
            "superseded_by": row["superseded_by"],
            "evaluation": json.loads(row["eval_json"]),
            "decl_snapshot": json.loads(row["decl_snapshot_json"]),
        }

    def recall_voucher(self, voucher_id: str) -> dict:
        """次日带原凭证号调回当时采用的声明版本（即使其后已被撤回/修订）。"""
        voucher = self.get_voucher(voucher_id)
        versions = []
        for d in voucher["decl_snapshot"]:
            versions.append(self.get_declaration_version(d["decl_id"], d["version"]).as_dict())
        errata_rows = self.conn.execute(
            "SELECT * FROM errata WHERE old_voucher_id = ? OR new_voucher_id = ?",
            (voucher_id, voucher_id),
        ).fetchall()
        return {
            "voucher": voucher,
            "declaration_versions_then": versions,
            "errata": [dict(r) for r in errata_rows],
        }

    def lookup_by_volume(self, volume: int) -> list[dict]:
        """书到柜按卷号查：每册给可签依据或待查原因（馆员视图，含私藏校样状态）。"""
        rows = self.conn.execute(
            "SELECT copy_id FROM holding_copy WHERE volume = ? ORDER BY copy_id", (volume,)
        ).fetchall()
        out = []
        for r in rows:
            copy_id = r["copy_id"]
            evaluation = self._evaluate(copy_id)
            proof = self._verified_proof_for_copy(copy_id)
            voucher_rows = self.conn.execute(
                "SELECT voucher_id, kind, translator_id, print_number, status FROM voucher"
                " WHERE copy_id = ? ORDER BY created_at", (copy_id,)
            ).fetchall()
            receipt = self.conn.execute(
                "SELECT receipt_id, note FROM counter_receipt WHERE copy_id = ?"
                " ORDER BY rowid DESC LIMIT 1", (copy_id,)
            ).fetchone()
            out.append({
                "copy_id": copy_id,
                "receipt_id": receipt["receipt_id"] if receipt else None,
                "evaluation": evaluation.as_dict(),
                "signable_basis": self._signable_basis(copy_id, evaluation, proof),
                "pending_reason": None if evaluation.unique else (
                    "还缺可核对字段: " + ", ".join(evaluation.missing_fields)
                    if evaluation.missing_fields else "无成立候选"
                ),
                "verified_proof": (
                    {"proof_id": proof["proof_id"], "draft_no": proof["draft_no"],
                     "signer": proof["signer_translator_id"]} if proof else None
                ),
                "vouchers": [dict(v) for v in voucher_rows],
            })
        return out

    def _signable_basis(
        self, copy_id: str, evaluation: HoldingEvaluation, proof: sqlite3.Row | None
    ) -> str | None:
        copy = self._load_copy(copy_id)
        if copy.printable_new_edition and proof is not None and self.available_print_numbers():
            return "reprint: 具名底稿已核签且有可用印号"
        if copy.printable_new_edition and proof is not None:
            return "reprint-blocked: 底稿已核签但无可用印号"
        if evaluation.unique:
            return "inquiry: 可见字段足以唯一签认"
        return None

    def trace_print_number(self, number: str) -> dict:
        """从印本印号倒查到书、凭证、采用的声明版本与勘误链。"""
        copy_row = self.conn.execute(
            "SELECT * FROM holding_copy WHERE print_number = ?", (number,)
        ).fetchone()
        if copy_row is None:
            pool = self.conn.execute(
                "SELECT * FROM print_number WHERE number = ?", (number,)
            ).fetchone()
            if pool is None:
                raise BiblioError(f"印号 {number!r} 无记录", reason="unknown-print-number")
            return {"print_number": number, "pool": dict(pool), "copy": None}
        copy_id = copy_row["copy_id"]
        vouchers = self.conn.execute(
            "SELECT voucher_id FROM voucher WHERE copy_id = ? ORDER BY created_at", (copy_id,)
        ).fetchall()
        return {
            "print_number": number,
            "copy": dict(copy_row),
            "voucher_chain": [self.recall_voucher(v["voucher_id"]) for v in vouchers],
            "errata": [
                dict(r) for r in self.conn.execute(
                    "SELECT * FROM errata WHERE copy_id = ? ORDER BY created_at", (copy_id,)
                ).fetchall()
            ],
        }

    def public_lookup(self, volume: int) -> list[dict]:
        """读者视图：只暴露该版能答的问题与结论，绝不出现私藏印前校样。"""
        result = []
        for item in self.lookup_by_volume(volume):
            ev = item["evaluation"]
            result.append({
                "copy_id": item["copy_id"],
                "answerable_fields": ev["answerable_fields"],
                "missing_fields": ev["missing_fields"],
                "unique": ev["unique"],
                "resolved_translator_id": ev["resolved_translator_id"],
                "resolved_translator_name": ev["resolved_translator_name"],
                "candidate_translators": sorted({
                    c["translator_id"] for c in ev["candidates"]
                }),
                "verified_proof": None,  # 读者不必知道出版社私藏文件
                "voucher_count": len(item["vouchers"]),
            })
        return result

    def list_receipts(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT receipt_id, copy_id, note, created_at FROM counter_receipt ORDER BY rowid"
        ).fetchall()
        return [dict(r) for r in rows]
