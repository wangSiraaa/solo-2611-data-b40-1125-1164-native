"""参赛卷装载：带卷号的上、下联原文、同音组与选手封注，同样来自归一任务。

编辑换一份正文不同的新卷，就是按同一套 ``column_mapping`` 再递一次归一任务；
:class:`~confcat.arena.contest.ArenaOffice` 的受理入口不区分词册任务还是卷任务，
统一经 :class:`~confcat.ingest.TaskRepository` 的归一行/列进入赛务层。

卷任务固定目标列：

==================  ==================================================
目标列              含义
==================  ==================================================
scroll_no           卷号（一场内唯一）
author              具名作者
upper_text          上联原文
lower_text          下联原文（每联不超过六字，装载时校验）
homophone_marks     同音标记：``字1=字2``，多组以 ``;`` 分隔，可空
seal_upper          封注的上联断法：词以 ``/`` 分隔，可空（题匾赛制不用）
seal_lower          封注的下联断法与读音：``词`` 或 ``词|读音``，``/`` 分隔
seal_at             封注时刻（ISO 字符串），与本场截止时刻比较先后
==================  ==================================================

同一张卷上标作同音的一组字必须一起采用同一读音（审读台在
:mod:`confcat.arena.review` 里强制），这里只负责把标记解析成字符组。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import ArenaError
from ..ingest import TaskRepository

MAX_LINE_CHARS = 6  # 本届合同：每联不超过六字

COL_NO = "scroll_no"
COL_AUTHOR = "author"
COL_UPPER = "upper_text"
COL_LOWER = "lower_text"
COL_HOMOPHONE = "homophone_marks"
COL_SEAL_UPPER = "seal_upper"
COL_SEAL_LOWER = "seal_lower"
COL_SEAL_AT = "seal_at"
REQUIRED_COLUMNS: tuple[str, ...] = (
    COL_NO,
    COL_AUTHOR,
    COL_UPPER,
    COL_LOWER,
    COL_HOMOPHONE,
    COL_SEAL_UPPER,
    COL_SEAL_LOWER,
    COL_SEAL_AT,
)

GROUP_SEP = ";"
PAIR_SEP = "="
WORD_SEP = "/"
READING_SEP = "|"


@dataclass(frozen=True)
class SealedWord:
    """封注里的一个词：断词词形 + 指定读音（可空，表示交由词册唯一读音）。"""

    word: str
    reading: str | None

    def as_dict(self) -> dict:
        return {"word": self.word, "reading": self.reading}


@dataclass(frozen=True)
class SealNote:
    """选手揭卷前封好的对上联断法与下联读音；事后补封无效（由赛务层判时刻）。"""

    upper_words: tuple[str, ...]
    lower_words: tuple[SealedWord, ...]
    sealed_at: str

    def as_dict(self) -> dict:
        return {
            "upper_words": list(self.upper_words),
            "lower_words": [w.as_dict() for w in self.lower_words],
            "sealed_at": self.sealed_at,
        }


@dataclass(frozen=True)
class Scroll:
    scroll_no: str
    author: str
    upper_text: str
    lower_text: str
    row_no: int
    # 同音组：每组是必须共用同一读音的字符元组（按出现位置识别）
    homophone_groups: tuple[tuple[str, ...], ...] = ()
    seal: SealNote | None = None
    raw_homophone: str = ""
    raw_seal_upper: str = ""
    raw_seal_lower: str = ""

    def as_dict(self) -> dict:
        return {
            "scroll_no": self.scroll_no,
            "author": self.author,
            "upper_text": self.upper_text,
            "lower_text": self.lower_text,
            "homophone_groups": [list(g) for g in self.homophone_groups],
            "seal": self.seal.as_dict() if self.seal else None,
        }


@dataclass
class ScrollBundle:
    """一场收卷任务的全部卷 + 任务/版本坐标。"""

    task_id: str
    product: str
    version: int
    scrolls: tuple[Scroll, ...]
    _by_no: dict[str, Scroll] = field(default_factory=dict, repr=False)

    def get(self, scroll_no: str) -> Scroll:
        if scroll_no not in self._by_no:
            raise ArenaError(f"卷号 {scroll_no!r} 不在本场收卷任务 {self.task_id!r} 中")
        return self._by_no[scroll_no]


def _parse_homophone(raw: str, scroll_no: str) -> tuple[tuple[str, ...], ...]:
    raw = (raw or "").strip()
    if not raw:
        return ()
    groups: list[tuple[str, ...]] = []
    for part in raw.split(GROUP_SEP):
        part = part.strip()
        if not part:
            continue
        chars = [c.strip() for c in part.split(PAIR_SEP) if c.strip()]
        if len(chars) < 2:
            raise ArenaError(f"卷 {scroll_no!r} 同音标记 {part!r} 至少要列两个字")
        groups.append(tuple(chars))
    return tuple(groups)


def _parse_seal(raw_upper: str, raw_lower: str, sealed_at: str, scroll_no: str) -> SealNote | None:
    raw_upper = (raw_upper or "").strip()
    raw_lower = (raw_lower or "").strip()
    if not raw_upper and not raw_lower:
        return None  # 未封注（题匾赛制允许；封匣赛制下由赛务层判无效）
    if not raw_upper or not raw_lower:
        raise ArenaError(f"卷 {scroll_no!r} 封注必须同时给出上联断法与下联读音")

    upper_words = tuple(w.strip() for w in raw_upper.split(WORD_SEP) if w.strip())
    lower_words: list[SealedWord] = []
    for token in raw_lower.split(WORD_SEP):
        token = token.strip()
        if not token:
            continue
        if READING_SEP in token:
            word, reading = token.split(READING_SEP, 1)
            lower_words.append(SealedWord(word.strip(), reading.strip()))
        else:
            lower_words.append(SealedWord(token, None))
    if not upper_words or not lower_words:
        raise ArenaError(f"卷 {scroll_no!r} 封注断词为空")
    return SealNote(upper_words, tuple(lower_words), (sealed_at or "").strip())


def load_scrolls(repo: TaskRepository, task_id: str) -> ScrollBundle:
    """沿一次已成功的归一任务装载本场全部参赛卷。"""
    summary = repo.get_task(task_id)
    rows = repo.get_rows(task_id)
    lineage = repo.get_lineage(task_id)
    present = {c["target"] for c in lineage}
    missing = [c for c in REQUIRED_COLUMNS if c not in present]
    if missing:
        raise ArenaError(f"收卷任务 {task_id!r} 缺少目标列 {missing}，无法受理")

    scrolls: list[Scroll] = []
    seen: set[str] = set()
    for row_no, row in enumerate(rows):
        no = row[COL_NO].strip()
        if not no:
            continue
        if no in seen:
            raise ArenaError(f"收卷任务 {task_id!r} 卷号 {no!r} 重复")
        seen.add(no)
        upper = row[COL_UPPER].strip()
        lower = row[COL_LOWER].strip()
        if not upper or not lower:
            raise ArenaError(f"卷 {no!r} 上、下联原文不得为空")
        if len(upper) > MAX_LINE_CHARS or len(lower) > MAX_LINE_CHARS:
            raise ArenaError(f"卷 {no!r} 超过每联 {MAX_LINE_CHARS} 字合同（上 {len(upper)} / 下 {len(lower)}）")
        groups = _parse_homophone(row[COL_HOMOPHONE], no)
        seal = _parse_seal(row[COL_SEAL_UPPER], row[COL_SEAL_LOWER], row[COL_SEAL_AT], no)
        scrolls.append(
            Scroll(
                scroll_no=no,
                author=row[COL_AUTHOR].strip(),
                upper_text=upper,
                lower_text=lower,
                row_no=row_no,
                homophone_groups=groups,
                seal=seal,
                raw_homophone=row[COL_HOMOPHONE],
                raw_seal_upper=row[COL_SEAL_UPPER],
                raw_seal_lower=row[COL_SEAL_LOWER],
            )
        )

    bundle = ScrollBundle(
        task_id=task_id,
        product=summary.product,
        version=summary.version,
        scrolls=tuple(scrolls),
    )
    bundle._by_no = {s.scroll_no: s for s in scrolls}
    return bundle
