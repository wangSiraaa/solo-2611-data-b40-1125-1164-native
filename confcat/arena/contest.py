"""赛务柜台：审卷受理、获选墙挂卷、承诺名额与回放（编辑部直接调用的入口）。

两类归一任务经同一入口受理：换带新词条的词册、递正文不同的新卷，都是再跑一次
:class:`~confcat.ingest.TaskRepository` 归一任务，然后把任务号交给本柜台。
每场比赛固定开场时的词册任务（=词册版本快照），因此**改版后仍可按原版重开
已经挂出的卷**：裁定与全联标注随挂卷订单落库，重开不依赖现行词册。

两套赛制（任选一套，受理时公布）
================================
``plaque`` 题匾制
    赛前在题匾上刻定一种上联断法，整场公开沿用；后来收的卷须守原匾，
    卷的审读读法里必须存在上联断法与匾完全一致者。
``seal`` 封匣制
    选手揭卷前各自封好上联断法与下联读音；挂卷时才拆，**事后补封无效**——
    封注时刻晚于本场截止时刻，或封注对不上审读台实际给出的读法，均不受理。

承诺与名额
==========
* 受理开场时公布这套赛制最多能承诺的不同作者人数（=墙格 ``slots``）；
* 一位作者最多挂一卷（唯一约束），先挂出的卷连同当时裁定仍可查；
* 名额按挂卷顺序落实到卷号，满额后后来的卷一律拒收。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..errors import ArenaError
from ..ingest import TaskRepository
from .review import ReviewResult, ScrollReading, explain_reading, review_scroll
from .scrolls import Scroll, ScrollBundle, SealNote, load_scrolls
from .wordbook import Wordbook

PLAQUE = "plaque"
SEAL = "seal"
RULESETS = (PLAQUE, SEAL)

# 挂卷裁定原因码
HUNG = "hung"
REJ_NO_READING = "no-valid-reading"
REJ_PLAQUE_MISMATCH = "plaque-mismatch"
REJ_NO_SEAL = "seal-missing"
REJ_SEAL_LATE = "seal-late"
REJ_SEAL_MISMATCH = "seal-mismatch"
REJ_AUTHOR_LIMIT = "author-already-hung"
REJ_NO_SLOT = "no-slot"
REJ_DUPLICATE_ORDER = "duplicate-order"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS arena_match (
    match_id        TEXT PRIMARY KEY,
    ruleset         TEXT NOT NULL,
    slots           INTEGER NOT NULL,
    deadline        TEXT,
    plaque_words    TEXT,
    wordbook_task   TEXT NOT NULL,
    wordbook_product TEXT NOT NULL,
    wordbook_version INTEGER NOT NULL,
    scroll_task     TEXT NOT NULL,
    scroll_product  TEXT NOT NULL,
    scroll_version  INTEGER NOT NULL,
    public_plaque   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    replay_of       TEXT,
    digest_json     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS arena_review (
    match_id    TEXT NOT NULL,
    scroll_no   TEXT NOT NULL,
    author      TEXT NOT NULL,
    qualified   INTEGER NOT NULL,
    readings_json TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL,
    PRIMARY KEY (match_id, scroll_no),
    FOREIGN KEY (match_id) REFERENCES arena_match(match_id)
);
CREATE TABLE IF NOT EXISTS arena_order (
    match_id     TEXT NOT NULL,
    scroll_no    TEXT NOT NULL,
    author       TEXT NOT NULL,
    slot_no      INTEGER,
    verdict      TEXT NOT NULL,
    reason       TEXT NOT NULL,
    reading_json TEXT,
    annotation_json TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (match_id, scroll_no),
    FOREIGN KEY (match_id) REFERENCES arena_match(match_id)
);
CREATE TABLE IF NOT EXISTS arena_promise (
    match_id   TEXT NOT NULL,
    slot_no    INTEGER NOT NULL,
    scroll_no  TEXT,
    author     TEXT,
    promised_at TEXT,
    PRIMARY KEY (match_id, slot_no),
    FOREIGN KEY (match_id) REFERENCES arena_match(match_id)
);
CREATE TABLE IF NOT EXISTS arena_receipt (
    match_id   TEXT NOT NULL,
    scroll_no  TEXT NOT NULL,
    author     TEXT NOT NULL,
    wordbook_task TEXT NOT NULL,
    wordbook_version INTEGER NOT NULL,
    deadline   TEXT,
    seal_json  TEXT,
    verdict    TEXT NOT NULL,
    reason     TEXT NOT NULL,
    PRIMARY KEY (match_id, scroll_no),
    FOREIGN KEY (match_id) REFERENCES arena_match(match_id)
);
"""


@dataclass
class Verdict:
    match_id: str
    scroll_no: str
    author: str
    accepted: bool
    verdict: str
    reason: str
    slot_no: int | None
    reading: ScrollReading | None
    annotation: dict | None
    created_at: str

    def as_dict(self) -> dict:
        return {
            "match_id": self.match_id,
            "scroll_no": self.scroll_no,
            "author": self.author,
            "accepted": self.accepted,
            "verdict": self.verdict,
            "reason": self.reason,
            "slot_no": self.slot_no,
            "reading": self.reading.as_dict() if self.reading else None,
            "annotation": self.annotation,
            "created_at": self.created_at,
        }


class ArenaOffice:
    """擂台赛务柜台。词册/卷均经 TaskRepository 归一任务进入，结果落真实 SQLite。"""

    def __init__(self, db_path: str | Path, repo: TaskRepository):
        self.db_path = str(db_path)
        self.repo = repo
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._book_cache: dict[str, Wordbook] = {}
        self._bundle_cache: dict[str, ScrollBundle] = {}

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ArenaOffice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 装载（经归一任务的实际行/列；同入口受理词册与卷两类任务）
    # ------------------------------------------------------------------ #
    def load_wordbook(self, wordbook_task: str) -> Wordbook:
        if wordbook_task not in self._book_cache:
            self._book_cache[wordbook_task] = Wordbook.load(self.repo, wordbook_task)
        return self._book_cache[wordbook_task]

    def load_scroll_bundle(self, scroll_task: str) -> ScrollBundle:
        if scroll_task not in self._bundle_cache:
            self._bundle_cache[scroll_task] = load_scrolls(self.repo, scroll_task)
        return self._bundle_cache[scroll_task]

    # ------------------------------------------------------------------ #
    def open_match(
        self,
        match_id: str,
        ruleset: str,
        wordbook_task: str,
        scroll_task: str,
        slots: int,
        deadline: str | None = None,
        plaque_words: tuple[str, ...] | None = None,
        replay_of: str | None = None,
    ) -> dict:
        """开场：固定词册/卷任务版本，预跑全场审读，公布可承诺作者人数。"""
        if ruleset not in RULESETS:
            raise ArenaError(f"未知赛制 {ruleset!r}，只支持 {RULESETS}")
        if slots < 1:
            raise ArenaError("墙格 slots 必须 >= 1")
        if self.conn.execute(
            "SELECT 1 FROM arena_match WHERE match_id = ?", (match_id,)
        ).fetchone():
            raise ArenaError(f"比赛 {match_id!r} 已开场")

        book = self.load_wordbook(wordbook_task)
        bundle = self.load_scroll_bundle(scroll_task)

        if ruleset == PLAQUE:
            if not plaque_words:
                raise ArenaError("题匾制必须在赛前刻定一种上联断法 plaque_words")
            joined = "".join(plaque_words)
            upper_texts = {s.upper_text for s in bundle.scrolls}
            if any(joined != t for t in upper_texts):
                raise ArenaError(
                    f"题匾断法 {list(plaque_words)} 拼接 {joined!r} 与场上联原文 {upper_texts} 不一致"
                )
            # 匾上每一段都必须是本届词册真实收录的词条，否则刻不出合法断法
            unknown = [w for w in plaque_words if not book.lookup(w)]
            if unknown:
                raise ArenaError(f"题匾断法含词册未收录的词 {unknown}，无法刻匾")
            public_plaque = "/".join(plaque_words)
        else:
            if not deadline:
                raise ArenaError("封匣制必须给出封注截止时刻 deadline")
            # 场单上的公示匾仍记录赛前刻定的参考断法（若给），但不用于裁定
            public_plaque = "/".join(plaque_words) if plaque_words else ""

        created_at = datetime.now(timezone.utc).isoformat()
        digest = {
            "wordbook": book.digest(),
            "scrolls": {
                "task_id": bundle.task_id,
                "product": bundle.product,
                "version": bundle.version,
                "scroll_nos": [s.scroll_no for s in bundle.scrolls],
            },
        }
        try:
            self.conn.execute(
                "INSERT INTO arena_match(match_id, ruleset, slots, deadline, plaque_words,"
                " wordbook_task, wordbook_product, wordbook_version, scroll_task,"
                " scroll_product, scroll_version, public_plaque, created_at, replay_of, digest_json)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    match_id, ruleset, slots, deadline,
                    json.dumps(list(plaque_words or ()), ensure_ascii=False),
                    wordbook_task, book.product, book.version,
                    scroll_task, bundle.product, bundle.version,
                    public_plaque, created_at, replay_of,
                    json.dumps(digest, ensure_ascii=False),
                ),
            )
            # 预跑审读台：每卷所有完整覆盖读法与淘汰依据随场落库
            reviews: dict[str, ReviewResult] = {}
            for scroll in bundle.scrolls:
                result = review_scroll(scroll, book)
                reviews[scroll.scroll_no] = result
                self.conn.execute(
                    "INSERT INTO arena_review(match_id, scroll_no, author, qualified,"
                    " readings_json, diagnostics_json) VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        match_id, scroll.scroll_no, scroll.author,
                        1 if result.qualified else 0,
                        json.dumps([r.as_dict() for r in result.readings], ensure_ascii=False),
                        json.dumps(list(result.diagnostics), ensure_ascii=False),
                    ),
                )
            # 受理时公布最多能承诺的不同作者人数，并把空槽位落实为承诺行
            for slot_no in range(1, slots + 1):
                self.conn.execute(
                    "INSERT INTO arena_promise(match_id, slot_no, scroll_no, author, promised_at)"
                    " VALUES(?, ?, NULL, NULL, NULL)",
                    (match_id, slot_no),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return {
            "match_id": match_id,
            "ruleset": ruleset,
            "slots": slots,
            "max_distinct_authors_promised": slots,
            "deadline": deadline,
            "public_plaque": public_plaque,
            "wordbook": digest["wordbook"],
            "scrolls": digest["scrolls"],
            "review_summary": {
                no: {"qualified": r.qualified, "reading_count": len(r.readings)}
                for no, r in reviews.items()
            },
            "replay_of": replay_of,
            "created_at": created_at,
        }

    # ------------------------------------------------------------------ #
    def _match(self, match_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM arena_match WHERE match_id = ?", (match_id,)
        ).fetchone()
        if row is None:
            raise ArenaError(f"比赛 {match_id!r} 不存在")
        return row

    def get_review(self, match_id: str, scroll_no: str) -> dict:
        """按卷号查所选赛制下场内审读台实际给出的读法与依据。"""
        self._match(match_id)
        row = self.conn.execute(
            "SELECT * FROM arena_review WHERE match_id = ? AND scroll_no = ?",
            (match_id, scroll_no),
        ).fetchone()
        if row is None:
            raise ArenaError(f"比赛 {match_id!r} 无卷号 {scroll_no!r}")
        return {
            "match_id": match_id,
            "scroll_no": scroll_no,
            "author": row["author"],
            "qualified": bool(row["qualified"]),
            "readings": json.loads(row["readings_json"]),
            "diagnostics": json.loads(row["diagnostics_json"]),
        }

    # ------------------------------------------------------------------ #
    def _eligible_reading(
        self, match: sqlite3.Row, scroll: Scroll, review: ReviewResult
    ) -> tuple[ScrollReading | None, str, str]:
        """按所选赛制（题匾/封匣）从审读台读法里挑出合式者，否则给出拒收原因。"""
        if not review.qualified:
            return None, REJ_NO_READING, "审读台未给出任何完整覆盖且联律全合的读法"

        ruleset = match["ruleset"]
        if ruleset == PLAQUE:
            plaque = tuple(json.loads(match["plaque_words"]))
            for reading in review.readings:
                if reading.upper.word_tuple() == plaque:
                    return reading, HUNG, "上联断法与赛前刻匾一致"
            return (
                None,
                REJ_PLAQUE_MISMATCH,
                f"卷的合法读法均不守原匾 {list(plaque)}（后来收的卷须守原匾）",
            )

        # 封匣制：挂卷时拆封，事后补封无效
        seal: SealNote | None = scroll.seal
        if seal is None:
            return None, REJ_NO_SEAL, "封匣制下未在揭卷前封好上联断法与下联读音"
        if match["deadline"] and seal.sealed_at > match["deadline"]:
            return (
                None,
                REJ_SEAL_LATE,
                f"封注时刻 {seal.sealed_at} 晚于截止时刻 {match['deadline']}，封匣已到期，事后补封无效",
            )
        seal_upper = seal.upper_words
        seal_lower = [(w.word, w.reading) for w in seal.lower_words]
        for reading in review.readings:
            if reading.upper.word_tuple() != seal_upper:
                continue
            if len(reading.lower.words) != len(seal_lower):
                continue
            ok = True
            for placed, (word, pinned) in zip(reading.lower.words, seal_lower):
                if placed.word != word:
                    ok = False
                    break
                if pinned is not None and placed.reading != pinned:
                    ok = False
                    break
            if ok:
                return reading, HUNG, "封注与审读台实际给出的读法一致，拆封合式"
        return (
            None,
            REJ_SEAL_MISMATCH,
            "封注的断法/读音对不上审读台实际给出的任何完整读法",
        )

    def hang(self, match_id: str, scroll_no: str) -> Verdict:
        """受理一张具名挂卷订单：赛制闸口 -> 作者唯一 -> 名额，全部通过才上墙。"""
        match = self._match(match_id)
        bundle = self.load_scroll_bundle(match["scroll_task"])
        book = self.load_wordbook(match["wordbook_task"])
        scroll = bundle.get(scroll_no)

        if self.conn.execute(
            "SELECT 1 FROM arena_order WHERE match_id = ? AND scroll_no = ?",
            (match_id, scroll_no),
        ).fetchone():
            raise ArenaError(f"卷 {scroll_no!r} 的挂卷订单已存在，先挂出的裁定仍可查，不重复受理")

        review_row = self.conn.execute(
            "SELECT * FROM arena_review WHERE match_id = ? AND scroll_no = ?",
            (match_id, scroll_no),
        ).fetchone()
        # 从落库 JSON 重建读法对象（重开仓库后仍可裁定，不依赖内存/现行词册）
        review = ReviewResult(
            scroll_no,
            tuple(_decode_reading(d) for d in json.loads(review_row["readings_json"])),
            tuple(json.loads(review_row["diagnostics_json"])),
        )

        reading, code, reason = self._eligible_reading(match, scroll, review)
        slot_no = None
        annotation = None
        accepted = False

        if code == HUNG:
            # 一位作者最多挂一卷
            dup_author = self.conn.execute(
                "SELECT 1 FROM arena_order WHERE match_id = ? AND author = ? AND verdict = ?",
                (match_id, scroll.author, HUNG),
            ).fetchone()
            if dup_author:
                code, reason = REJ_AUTHOR_LIMIT, f"作者 {scroll.author!r} 已挂有一卷，一位作者最多挂一卷"
            else:
                free = self.conn.execute(
                    "SELECT MIN(slot_no) AS s FROM arena_promise WHERE match_id = ? AND scroll_no IS NULL",
                    (match_id,),
                ).fetchone()["s"]
                if free is None:
                    code, reason = REJ_NO_SLOT, f"本场 {match['slots']} 个名额已满，承诺已全部落实到卷号"
                else:
                    slot_no = free
                    accepted = True

        created_at = datetime.now(timezone.utc).isoformat()
        if accepted and reading is not None:
            annotation = explain_reading(reading, book)
        try:
            self.conn.execute(
                "INSERT INTO arena_order(match_id, scroll_no, author, slot_no, verdict,"
                " reason, reading_json, annotation_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    match_id, scroll_no, scroll.author, slot_no,
                    HUNG if accepted else code,
                    reason,
                    json.dumps(reading.as_dict(), ensure_ascii=False) if reading else None,
                    json.dumps(annotation, ensure_ascii=False) if annotation else None,
                    created_at,
                ),
            )
            self.conn.execute(
                "INSERT INTO arena_receipt(match_id, scroll_no, author, wordbook_task,"
                " wordbook_version, deadline, seal_json, verdict, reason)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    match_id, scroll_no, scroll.author,
                    match["wordbook_task"], match["wordbook_version"],
                    match["deadline"],
                    json.dumps(scroll.seal.as_dict(), ensure_ascii=False) if scroll.seal else None,
                    HUNG if accepted else code, reason,
                ),
            )
            if accepted:
                self.conn.execute(
                    "UPDATE arena_promise SET scroll_no = ?, author = ?, promised_at = ?"
                    " WHERE match_id = ? AND slot_no = ?",
                    (scroll_no, scroll.author, created_at, match_id, slot_no),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return Verdict(
            match_id=match_id,
            scroll_no=scroll_no,
            author=scroll.author,
            accepted=accepted,
            verdict=HUNG if accepted else code,
            reason=reason,
            slot_no=slot_no,
            reading=reading if accepted else None,
            annotation=annotation,
            created_at=created_at,
        )

    # ------------------------------------------------------------------ #
    def wall(self, match_id: str) -> dict:
        """获选墙对外视图：只显示获选卷，按墙格排序，附全联标注。"""
        match = self._match(match_id)
        rows = self.conn.execute(
            "SELECT * FROM arena_order WHERE match_id = ? AND verdict = ? ORDER BY slot_no",
            (match_id, HUNG),
        ).fetchall()
        return {
            "match_id": match_id,
            "ruleset": match["ruleset"],
            "public_plaque": match["public_plaque"],
            "slots": match["slots"],
            "hung_count": len(rows),
            "cells": [
                {
                    "slot_no": r["slot_no"],
                    "scroll_no": r["scroll_no"],
                    "author": r["author"],
                    "annotation": json.loads(r["annotation_json"]),
                    "hung_at": r["created_at"],
                }
                for r in rows
            ],
        }

    def promises(self, match_id: str) -> dict:
        """名额承诺落实情况：每个槽位对应到卷号/作者（未满为空槽）。"""
        self._match(match_id)
        rows = self.conn.execute(
            "SELECT * FROM arena_promise WHERE match_id = ? ORDER BY slot_no", (match_id,)
        ).fetchall()
        return {
            "match_id": match_id,
            "max_distinct_authors": len(rows),
            "slots": [
                {
                    "slot_no": r["slot_no"],
                    "scroll_no": r["scroll_no"],
                    "author": r["author"],
                    "promised_at": r["promised_at"],
                }
                for r in rows
            ],
        }

    def order(self, match_id: str, scroll_no: str) -> dict:
        """按卷号查挂卷订单裁定与依据（含被拒订单）。"""
        row = self.conn.execute(
            "SELECT * FROM arena_order WHERE match_id = ? AND scroll_no = ?",
            (match_id, scroll_no),
        ).fetchone()
        if row is None:
            raise ArenaError(f"比赛 {match_id!r} 卷 {scroll_no!r} 尚无挂卷订单")
        return {
            "match_id": match_id,
            "scroll_no": row["scroll_no"],
            "author": row["author"],
            "accepted": row["verdict"] == HUNG,
            "verdict": row["verdict"],
            "reason": row["reason"],
            "slot_no": row["slot_no"],
            "reading": json.loads(row["reading_json"]) if row["reading_json"] else None,
            "annotation": json.loads(row["annotation_json"]) if row["annotation_json"] else None,
            "created_at": row["created_at"],
        }

    def appeal(self, match_id: str, scroll_no: str) -> dict:
        """申诉人按本人卷号对照已用词册版本与截止回执（不含他人卷信息）。"""
        receipt = self.conn.execute(
            "SELECT * FROM arena_receipt WHERE match_id = ? AND scroll_no = ?",
            (match_id, scroll_no),
        ).fetchone()
        if receipt is None:
            raise ArenaError(f"比赛 {match_id!r} 卷 {scroll_no!r} 无截止回执")
        book = self.load_wordbook(receipt["wordbook_task"])
        return {
            "match_id": match_id,
            "scroll_no": scroll_no,
            "author": receipt["author"],
            "wordbook_used": book.digest(),
            "deadline": receipt["deadline"],
            "seal_on_record": json.loads(receipt["seal_json"]) if receipt["seal_json"] else None,
            "verdict": receipt["verdict"],
            "reason": receipt["reason"],
            "review": self.get_review(match_id, scroll_no),
        }

    def replay_match(
        self,
        replay_id: str,
        source_match_id: str,
        scroll_task: str,
        hang_order: tuple[str, ...],
    ) -> dict:
        """从同一场赛前记录另开回放：词册/原文/题匾照前，仅换收卷任务与受理顺序。

        ``hang_order`` 给出回放中挂卷订单的受理次序（如题面：甲的签发先照原回执
        发生，再受理乙）；每个订单的裁定结果一并返回，展示已刻匾或已到期封匣
        如何限制柜台。
        """
        src = self._match(source_match_id)
        opened = self.open_match(
            match_id=replay_id,
            ruleset=src["ruleset"],
            wordbook_task=src["wordbook_task"],
            scroll_task=scroll_task,
            slots=src["slots"],
            deadline=src["deadline"],
            plaque_words=tuple(json.loads(src["plaque_words"])),
            replay_of=source_match_id,
        )
        verdicts = [self.hang(replay_id, no).as_dict() for no in hang_order]
        return {"opened": opened, "verdicts": verdicts, "wall": self.wall(replay_id)}


# --------------------------------------------------------------------------- #
def _decode_reading(data: dict) -> ScrollReading:
    from .review import LineReading, PlacedWord

    def line(d: dict) -> LineReading:
        return LineReading(
            text=d["text"],
            words=tuple(
                PlacedWord(
                    word=w["word"], start=w["start"], end=w["end"], row_no=w["row_no"],
                    word_class=w["word_class"], final_tone=w["final_tone"], reading=w["reading"],
                )
                for w in d["words"]
            ),
        )

    return ScrollReading(upper=line(data["upper"]), lower=line(data["lower"]))
