"""虚构楹联擂台子包：词册审读、赛制裁定与获选墙挂卷。

模块分工
========
* :mod:`confcat.arena.wordbook` —— 本届联律词册装载，沿 TaskRepository
  归一行/列溯源补录单单号与真实签录人；
* :mod:`confcat.arena.scrolls` —— 带卷号参赛原文、同音标记与选手封注装载；
* :mod:`confcat.arena.review` —— 审读台：完整覆盖断词、同音一致读法、
  字数/词类相合与词末声类相反的联律裁定、全联标注；
* :mod:`confcat.arena.contest` —— 赛务柜台（题匾制/封匣制）、承诺名额、
  获选墙、截止回执与同场回放，SQLite 持久化、重开可查。

两类归一任务（词册、参赛卷）走同一个 TaskRepository 入口受理；比赛固定开场时
的词册任务版本，改版后已挂出的旧卷仍按原词册重开。
"""

from .contest import (
    PLAQUE,
    SEAL,
    ArenaOffice,
    Verdict,
)
from .review import (
    LineReading,
    PlacedWord,
    ReviewResult,
    ScrollReading,
    explain_reading,
    review_scroll,
)
from .scrolls import Scroll, ScrollBundle, SealNote, SealedWord, load_scrolls
from .wordbook import Entry, EntryTrace, Wordbook

__all__ = [
    "PLAQUE",
    "SEAL",
    "ArenaOffice",
    "Verdict",
    "LineReading",
    "PlacedWord",
    "ReviewResult",
    "ScrollReading",
    "explain_reading",
    "review_scroll",
    "Scroll",
    "ScrollBundle",
    "SealNote",
    "SealedWord",
    "load_scrolls",
    "Entry",
    "EntryTrace",
    "Wordbook",
]
