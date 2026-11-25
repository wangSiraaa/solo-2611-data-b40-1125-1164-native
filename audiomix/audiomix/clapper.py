"""板响（clapper）模板与录音格（cell）模型。

题面把“试片”定成：**十个录音样本为一格**。录音原件是仓库里可重开的
16kHz 渲染（见 :mod:`audiomix.recording`），因此

    格 k  == 录音样本 [k*10, (k+1)*10)
    格宽  == 10 样本 == 1/1600 秒

每次板响用一种事先给出的短 PCM 模板。模板按“花纹（pattern）”命名，
检测只做**整格对齐的精确匹配**：模板 k 落在格 c，当且仅当
``pcm[10*c : 10*c+len(tmpl)]`` 与模板逐样本相等。这样检测完全确定、
可复现，不依赖任何阈值或浮点相关，也不涉及任何视频编解码。

“允许录音带拼接处漏一个”在 :mod:`audiomix.sync` 里作为“每卷最多漏
一个板响”的约束处理；本模块只负责把实际录音读成“某花纹在哪些格”。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

REC_RATE = 16000
SAMPLES_PER_CELL = 10
CELLS_PER_SECOND = REC_RATE // SAMPLES_PER_CELL  # 1600


def cell_of_sample(n: int) -> int:
    """样本下标 -> 所在格。"""
    return n // SAMPLES_PER_CELL


def cell_start_sample(cell: int) -> int:
    """格 -> 起始样本下标。"""
    return cell * SAMPLES_PER_CELL


def cell_count(n_samples: int) -> int:
    """整格数（不足一格的部分不构成格）。"""
    return n_samples // SAMPLES_PER_CELL


@dataclass(frozen=True)
class Template:
    """一种花纹的短 PCM 模板（有符号 16bit，长度不超过一格）。"""

    pattern: str
    samples: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.pattern:
            raise ValueError("花纹名不能为空")
        if not self.samples:
            raise ValueError(f"花纹 {self.pattern} 的模板不能为空")
        if len(self.samples) > SAMPLES_PER_CELL:
            raise ValueError(
                f"花纹 {self.pattern} 的模板长度 {len(self.samples)} 超过一格 "
                f"({SAMPLES_PER_CELL} 样本)")
        for v in self.samples:
            if not (-32768 <= int(v) <= 32767):
                raise ValueError(f"花纹 {self.pattern} 模板样本 {v} 超出 int16")


def template(pattern: str, samples: Iterable[int]) -> Template:
    return Template(pattern, tuple(int(v) for v in samples))


class TemplateBook:
    """花纹 -> 模板 的登记册；检测返回 ``花纹 -> 升序格列表``。"""

    def __init__(self, templates: Iterable[Template] = ()):
        self._by_pattern: dict[str, Template] = {}
        for t in templates:
            self.add(t)

    def add(self, t: Template) -> Template:
        if t.pattern in self._by_pattern and self._by_pattern[t.pattern] != t:
            raise ValueError(f"花纹 {t.pattern} 已登记且模板不同")
        self._by_pattern[t.pattern] = t
        return t

    @property
    def patterns(self) -> list[str]:
        return sorted(self._by_pattern)

    def get(self, pattern: str) -> Template:
        if pattern not in self._by_pattern:
            raise KeyError(f"未登记的板花纹: {pattern}")
        return self._by_pattern[pattern]

    def detect(self, pcm: np.ndarray) -> dict[str, list[int]]:
        """在 16kHz 录音里按格对齐精确匹配每个模板。

        返回 ``{pattern: [cell, ...]}``（升序）。只在完整格内匹配，
        模板长度不超过一格，因此不会跨格误配。
        """
        arr = np.asarray(pcm, dtype=np.int16)
        n_cells = cell_count(len(arr))
        found: dict[str, list[int]] = {}
        for pat in self.patterns:
            tmpl = np.asarray(self._by_pattern[pat].samples, dtype=np.int16)
            L = len(tmpl)
            hits: list[int] = []
            # 逐格精确比较；L <= SAMPLES_PER_CELL，故每个模板只落在格首。
            for c in range(n_cells):
                s = c * SAMPLES_PER_CELL
                if np.array_equal(arr[s:s + L], tmpl):
                    hits.append(c)
            found[pat] = hits
        return found

    def bangs(self, pcm: np.ndarray) -> list[tuple[int, str]]:
        """检测到的全部板响，按 (格, 花纹) 升序排列。"""
        det = self.detect(pcm)
        events = [(c, pat) for pat, cells in det.items() for c in cells]
        return sorted(events)
