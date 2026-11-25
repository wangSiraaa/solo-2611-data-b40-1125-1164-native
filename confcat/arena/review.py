"""审读台：给出每联所有能完整覆盖原文的断词与读法，并按联律裁定。

审读合同（全部以本届词册为准）
==============================
1. 断词必须**完整覆盖**原文：从首字到末字逐词拼接，词册里没有的字无法成词，
   该断法直接出局；同形多词条时每个词条都是一条候选（词类/平仄/读法各不同）。
2. 上、下联对应词的**字数和词类须相合**，**词末声类相反**（平对仄、仄对平）。
3. 同一张卷上标作同音的一组字必须一起采用**同一读音**：凡词形内含该组任一
   字符的词全部并入同一读音组件，组件可取读音为各词读音集合的交集；交集为空
   则该断法无任何合法读法。
4. 一张卷可以有多种读法，但每个读法都是覆盖全联的一套完整赋值，**不能拼接
   两种互斥读法的局部**——本模块只枚举组件级一致赋值，天然不产生拼接读法。

输出按断词边界与读音确定性排序，保证重开仓库/回放时裁定可复现。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ArenaError
from .scrolls import Scroll
from .wordbook import Entry, Wordbook


@dataclass(frozen=True)
class PlacedWord:
    """断词后落到原文某一段上的一个词及其采用的读音。"""

    word: str
    start: int  # 0 基字符偏移（含）
    end: int  # 0 基字符偏移（不含）
    row_no: int  # 词册词条行号（裁定依据入口）
    word_class: str
    final_tone: str
    reading: str

    def as_dict(self) -> dict:
        return {
            "word": self.word,
            "start": self.start,
            "end": self.end,
            "row_no": self.row_no,
            "word_class": self.word_class,
            "final_tone": self.final_tone,
            "reading": self.reading,
        }


@dataclass(frozen=True)
class LineReading:
    text: str
    words: tuple[PlacedWord, ...]

    def word_tuple(self) -> tuple[str, ...]:
        return tuple(w.word for w in self.words)

    def as_dict(self) -> dict:
        return {"text": self.text, "words": [w.as_dict() for w in self.words]}


@dataclass(frozen=True)
class ScrollReading:
    """一套覆盖全联、同音一致、联律全合的完整读法。"""

    upper: LineReading
    lower: LineReading

    def as_dict(self) -> dict:
        return {"upper": self.upper.as_dict(), "lower": self.lower.as_dict()}


@dataclass
class ReviewResult:
    scroll_no: str
    readings: tuple[ScrollReading, ...]
    diagnostics: tuple[str, ...]  # 无合法读法时逐条说明被哪条规则淘汰

    @property
    def qualified(self) -> bool:
        return bool(self.readings)


# --------------------------------------------------------------------------- #
# 断词枚举：所有能完整覆盖原文的词条序列
# --------------------------------------------------------------------------- #
def _segment(text: str, book: Wordbook) -> list[list[tuple[int, int, Entry]]]:
    """DFS 枚举完整覆盖 ``text`` 的全部 (起点, 终点, 词条) 序列。"""
    n = len(text)
    results: list[list[tuple[int, int, Entry]]] = []

    def dfs(start: int, path: list[tuple[int, int, Entry]]) -> None:
        if start == n:
            results.append(list(path))
            return
        for end in range(start + 1, n + 1):
            for entry in book.lookup(text[start:end]):
                path.append((start, end, entry))
                dfs(end, path)
                path.pop()

    dfs(0, [])
    return results


# --------------------------------------------------------------------------- #
# 同音组件：并查集把必须同读的词并到一起
# --------------------------------------------------------------------------- #
class _DSU:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _build_components(
    scroll: Scroll,
    upper_words: list[PlacedWord],
    lower_words: list[PlacedWord],
) -> tuple[_DSU, list[str]]:
    """按卷上同音标记把词并入同读组件；标记字符在原文中不出现即赛务错误。"""
    all_words = upper_words + lower_words
    dsu = _DSU(len(all_words))
    notes: list[str] = []
    for group in scroll.homophone_groups:
        member_idx = [
            i for i, w in enumerate(all_words) if any(ch in w.word for ch in group)
        ]
        missing = [
            ch for ch in group if ch not in scroll.upper_text + scroll.lower_text
        ]
        if missing:
            raise ArenaError(
                f"卷 {scroll.scroll_no!r} 同音标记 {group!r} 中的字 {missing} 在原文中不出现"
            )
        if len(member_idx) < 2:
            notes.append(
                f"同音组 {''.join(group)} 只落在一个词 {all_words[member_idx[0]].word!r} 上，约束自动满足"
            )
            continue
        first = member_idx[0]
        for idx in member_idx[1:]:
            dsu.union(first, idx)
    return dsu, notes


def _enumerate_readings(
    upper_choices: list[tuple[int, int, Entry]],
    lower_choices: list[tuple[int, int, Entry]],
    scroll: Scroll,
    book: Wordbook,
) -> tuple[list[ScrollReading], list[str]]:
    """对一对通过联律的断词，枚举同音一致的全部完整读法赋值。"""
    upper_words = [
        PlacedWord(e.headword, s, t, e.row_no, e.word_class, e.final_tone, reading="")
        for s, t, e in upper_choices
    ]
    lower_words = [
        PlacedWord(e.headword, s, t, e.row_no, e.word_class, e.final_tone, reading="")
        for s, t, e in lower_choices
    ]
    dsu, notes = _build_components(scroll, upper_words, lower_words)
    all_words = upper_words + lower_words

    # 组件 -> 成员词；可取读音 = 各成员词条读音集合的交集
    groups: dict[int, list[int]] = {}
    for i in range(len(all_words)):
        groups.setdefault(dsu.find(i), []).append(i)

    component_options: list[tuple[tuple[int, ...], tuple[str, ...]]] = []
    for members in groups.values():
        allowed: set[str] | None = None
        for m in members:
            member_readings = set(book.entry_at(all_words[m].row_no).readings)
            allowed = member_readings if allowed is None else allowed & member_readings
        if not allowed:
            joined = ",".join(all_words[m].word for m in members)
            notes.append(
                f"同音组件（{joined}）各词读音集合无交集，无法采用同一读音"
            )
            return [], notes
        component_options.append((tuple(members), tuple(sorted(allowed))))

    # 笛卡尔积展开为完整赋值（只产出全联一致读法，不产生任何跨读法拼接）
    seen: set[tuple] = set()
    out: list[ScrollReading] = []
    chosen: dict[int, str] = {}

    def expand(idx: int) -> None:
        if idx == len(component_options):
            placed = [
                PlacedWord(
                    w.word, w.start, w.end, w.row_no, w.word_class, w.final_tone,
                    chosen[dsu.find(i)],
                )
                for i, w in enumerate(all_words)
            ]
            up = LineReading(scroll.upper_text, tuple(placed[: len(upper_words)]))
            lo = LineReading(scroll.lower_text, tuple(placed[len(upper_words):]))
            key = (
                tuple((w.word, w.reading) for w in up.words),
                tuple((w.word, w.reading) for w in lo.words),
            )
            if key not in seen:
                seen.add(key)
                out.append(ScrollReading(up, lo))
            return
        members, readings = component_options[idx]
        root = dsu.find(members[0])
        for reading in readings:
            chosen[root] = reading  # 整组共用同一读音
            expand(idx + 1)

    expand(0)
    return out, notes


def review_scroll(scroll: Scroll, book: Wordbook) -> ReviewResult:
    """审读一张卷：返回所有完整覆盖、同音一致、联律全合的读法与淘汰依据。"""
    diagnostics: list[str] = []

    upper_segs = _segment(scroll.upper_text, book)
    if not upper_segs:
        diagnostics.append(f"上联 {scroll.upper_text!r} 无字册词条能完整覆盖，无法断词")
    lower_segs = _segment(scroll.lower_text, book)
    if not lower_segs:
        diagnostics.append(f"下联 {scroll.lower_text!r} 无字册词条能完整覆盖，无法断词")
    if not upper_segs or not lower_segs:
        return ReviewResult(scroll.scroll_no, (), tuple(diagnostics))

    readings: list[ScrollReading] = []
    for up in upper_segs:
        for lo in lower_segs:
            if len(up) != len(lo):
                diagnostics.append(
                    f"断法 {[e.headword for _, _, e in up]} / {[e.headword for _, _, e in lo]}："
                    f"上下联词数不合（{len(up)} ≠ {len(lo)}）"
                )
                continue
            pair_ok = True
            for (us, ue, uentry), (ls, le, lentry) in zip(up, lo):
                if ue - us != le - ls:
                    diagnostics.append(
                        f"对应词 {uentry.headword!r}/{lentry.headword!r} 字数不合"
                        f"（{ue - us} ≠ {le - ls}）"
                    )
                    pair_ok = False
                    break
                if uentry.word_class != lentry.word_class:
                    diagnostics.append(
                        f"对应词 {uentry.headword!r}/{lentry.headword!r} 词类不合"
                        f"（{uentry.word_class} ≠ {lentry.word_class}）"
                    )
                    pair_ok = False
                    break
                if uentry.final_tone == lentry.final_tone:
                    diagnostics.append(
                        f"对应词 {uentry.headword!r}/{lentry.headword!r} 词末声类不相反"
                        f"（同为 {uentry.final_tone}）"
                    )
                    pair_ok = False
                    break
            if not pair_ok:
                continue
            found, notes = _enumerate_readings(up, lo, scroll, book)
            diagnostics.extend(notes)
            readings.extend(found)

    # 确定性排序：先上联边界、再下联边界、最后读音序列
    readings.sort(
        key=lambda r: (
            tuple((w.start, w.end) for w in r.upper.words),
            tuple((w.start, w.end) for w in r.lower.words),
            tuple(w.reading for w in r.upper.words),
            tuple(w.reading for w in r.lower.words),
        )
    )
    if not readings:
        diagnostics.append("全部断法均被字数/词类/平仄/同音规则淘汰，无完整合法读法")
    return ReviewResult(
        scroll.scroll_no, tuple(readings), tuple(dict.fromkeys(diagnostics))
    )


# --------------------------------------------------------------------------- #
# 全联标注：给作者的解释视图（含补录单/签录人依据）
# --------------------------------------------------------------------------- #
def explain_reading(reading: ScrollReading, book: Wordbook) -> dict:
    """把一套读法展开为可向作者解释的全联标注，逐词附词册补录单依据。"""

    def line_view(line: LineReading) -> dict:
        words = []
        for w in line.words:
            trace = book.trace_entry(w.row_no)
            words.append(
                {
                    **w.as_dict(),
                    "supplement_no": trace.supplement_no,
                    "recorder": trace.recorder,
                    "label": f"{w.word}【{w.reading}｜{w.word_class}｜{w.final_tone}】",
                }
            )
        return {
            "text": line.text,
            "words": words,
            "rendered": " ".join(w["label"] for w in words),
        }

    up_view = line_view(reading.upper)
    lo_view = line_view(reading.lower)
    pairs = []
    for uw, lw in zip(reading.upper.words, reading.lower.words):
        pairs.append(
            {
                "upper": uw.word,
                "lower": lw.word,
                "length_match": (uw.end - uw.start) == (lw.end - lw.start),
                "word_class_match": uw.word_class == lw.word_class,
                "tone_opposite": uw.final_tone != lw.final_tone,
                "upper_tone": uw.final_tone,
                "lower_tone": lw.final_tone,
            }
        )
    return {
        "upper": up_view,
        "lower": lo_view,
        "pair_checks": pairs,
        "rendered": f"上联：{up_view['rendered']}\n下联：{lo_view['rendered']}",
    }
