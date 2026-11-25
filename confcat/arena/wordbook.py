"""虚构楹联擂台：本届自定联律词册的装载与补录单溯源。

词册事实全部来自一次 **TaskRepository 归一任务** 的行与列：
编辑换一本带新词条的词册，就是按新版配置再递一次归一任务；已经挂出的旧卷
固定旧任务（=旧版词册），重开时仍按原词册裁定，见 :mod:`confcat.arena.contest`。

每行一个词条，列由 ``column_mapping`` 固定的目标列名承载（即 TaskRepository
实际归一的行来源与列来源）：

==================  ==================================================
目标列              含义
==================  ==================================================
headword            词形（可重复：同形不同词类/读法各占一行）
word_class          词类（如 名 / 形）
final_tone          词末平仄，仅 ``平`` / ``仄``
readings            读音，多读音以 ``|`` 分隔；小例词条均为唯一读音
supplement_no       该版词条对应的补录单单号
recorder            补录单真实签录人
==================  ==================================================

``trace_entry`` 沿 TaskRepository 的逐行值 + 列血缘（``task_column`` 里
原 CSV 列与表头行号）查回该版词条对应补录单及其签录人，改版后旧版溯源不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import ArenaError
from ..ingest import TaskRepository

TONES = ("平", "仄")
# 多读音分隔符（同一词条列内）
READING_SEP = "|"

# 归一任务固定的目标列名（column_mapping 的键，即“实际归一列来源”）
COL_HEADWORD = "headword"
COL_WORD_CLASS = "word_class"
COL_FINAL_TONE = "final_tone"
COL_READINGS = "readings"
COL_SUPPLEMENT = "supplement_no"
COL_RECORDER = "recorder"
REQUIRED_COLUMNS: tuple[str, ...] = (
    COL_HEADWORD,
    COL_WORD_CLASS,
    COL_FINAL_TONE,
    COL_READINGS,
    COL_SUPPLEMENT,
    COL_RECORDER,
)


@dataclass(frozen=True)
class Entry:
    """词册中的一个词条（一行归一结果）。"""

    headword: str
    word_class: str
    final_tone: str
    readings: tuple[str, ...]
    row_no: int  # 归一任务内的行号（0 基，即 task_row.row_no）

    def covers(self, text: str, start: int) -> bool:
        return text.startswith(self.headword, start)

    def as_dict(self) -> dict:
        return {
            "headword": self.headword,
            "word_class": self.word_class,
            "final_tone": self.final_tone,
            "readings": list(self.readings),
            "row_no": self.row_no,
        }


@dataclass(frozen=True)
class EntryTrace:
    """词条 -> 补录单/签录人的完整溯源（行来源 + 列来源）。"""

    row_no: int
    headword: str
    supplement_no: str
    recorder: str
    # 每个目标列实际来自哪个原 CSV 列、表头行号（TaskRepository 列血缘）
    columns: tuple[dict, ...]

    def as_dict(self) -> dict:
        return {
            "row_no": self.row_no,
            "headword": self.headword,
            "supplement_no": supplement_no,
            "recorder": recorder,
            "columns": list(self.columns),
        }


@dataclass
class Wordbook:
    """一届词册的不可变快照：词条索引 + 归一任务/版本坐标。"""

    task_id: str
    product: str
    version: int
    entries: tuple[Entry, ...]
    _rows: tuple[dict[str, str], ...] = field(repr=False, default=())
    _lineage: tuple[dict, ...] = field(repr=False, default=())
    # headword -> 词条行号列表（同形多词条全部保留，审读时逐一套用）
    _index: dict[str, tuple[int, ...]] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, repo: TaskRepository, task_id: str) -> "Wordbook":
        """沿一次已成功的归一任务装载词册；列缺失即赛务错误（整卷不受理）。"""
        summary = repo.get_task(task_id)  # 不存在抛 CatalogError
        rows = repo.get_rows(task_id)
        lineage = repo.get_lineage(task_id)
        present = {c["target"] for c in lineage}
        missing = [c for c in REQUIRED_COLUMNS if c not in present]
        if missing:
            raise ArenaError(f"词册任务 {task_id!r} 缺少目标列 {missing}，无法按本届词册受理")

        entries: list[Entry] = []
        for row_no, row in enumerate(rows):
            headword = row[COL_HEADWORD].strip()
            if not headword:
                continue
            tone = row[COL_FINAL_TONE].strip()
            if tone not in TONES:
                raise ArenaError(
                    f"词册第 {row_no} 行词条 {headword!r} 词末平仄 {tone!r} 非法，只允许 {TONES}"
                )
            readings = tuple(r.strip() for r in row[COL_READINGS].split(READING_SEP) if r.strip())
            if not readings:
                raise ArenaError(f"词册第 {row_no} 行词条 {headword!r} 未录任何读音")
            word_class = row[COL_WORD_CLASS].strip()
            if not word_class:
                raise ArenaError(f"词册第 {row_no} 行词条 {headword!r} 缺少词类")
            entries.append(Entry(headword, word_class, tone, readings, row_no))

        index: dict[str, list[int]] = {}
        for i, e in enumerate(entries):
            index.setdefault(e.headword, []).append(i)
        return cls(
            task_id=task_id,
            product=summary.product,
            version=summary.version,
            entries=tuple(entries),
            _rows=tuple(rows),
            _lineage=tuple(lineage),
            _index={k: tuple(v) for k, v in index.items()},
        )

    def lookup(self, headword: str) -> tuple[Entry, ...]:
        """同形词条全部返回（断词时每个词条都是一条候选读法来源）。"""
        return tuple(self.entries[i] for i in self._index.get(headword, ()))

    def entry_at(self, row_no: int) -> Entry:
        return self.entries[self._row_pos(row_no)]

    def _row_pos(self, row_no: int) -> int:
        for pos, e in enumerate(self.entries):
            if e.row_no == row_no:
                return pos
        raise ArenaError(f"词册中无行号 {row_no} 的词条")

    def trace_entry(self, row_no: int) -> EntryTrace:
        """按 TaskRepository 的实际归一行与列来源查回补录单号与真实签录人。"""
        entry = self.entry_at(row_no)
        row = self._rows[row_no]
        columns = tuple(
            {
                "target_column": c["target"],
                "source_column": c["source_column"],
                "header_line": c["header_line"],
                "value": row.get(c["target"], ""),
            }
            for c in self._lineage
            if c["target"] in REQUIRED_COLUMNS
        )
        return EntryTrace(
            row_no=row_no,
            headword=entry.headword,
            supplement_no=row[COL_SUPPLEMENT].strip(),
            recorder=row[COL_RECORDER].strip(),
            columns=columns,
        )

    def digest(self) -> dict:
        return {
            "task_id": self.task_id,
            "product": self.product,
            "version": self.version,
            "entry_count": len(self.entries),
        }
