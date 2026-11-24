"""逐年宽窄参照带：加载与交叉对齐。

参照带是随题自造的逐年宽窄序列（fixtures_bridge/band.json）：
    {"start_year": 1, "widths": "窄宽窄宽……"}

梁片拼接后的读数串中，漏读环以 "?" 占位（通配，匹配任意一年）。
对齐 = 把读数串沿参照带滑动，所有非 "?" 位置都一致的落点才算成立；
每个落点给出一个“末环候选年”（读数串最后一环对应的公历年）。
只输出仍成立的年代对照，不做任何统计打分——落点要么成立要么不成立。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import PlaqueError

GAP = "?"          # 漏读环占位符
WIDTHS = ("窄", "宽")


@dataclass(frozen=True)
class ReferenceBand:
    """逐年宽窄参照带。"""

    start_year: int
    widths: str                    # 第 i 个字符 = start_year + i 年的宽窄

    @property
    def end_year(self) -> int:
        return self.start_year + len(self.widths) - 1

    def year_at(self, index: int) -> int:
        return self.start_year + index

    def align(self, pattern: str) -> list[int]:
        """返回读数串 pattern 在参照带上所有仍成立的末环候选年（升序）。

        pattern 由 窄/宽 组成，可含 "?"（漏读，通配）。落点合法当且仅当
        每个非 "?" 位置与参照带完全一致；候选年 = 落点末位对应的年份。
        """
        if not pattern:
            raise PlaqueError("读数串为空，无法对齐")
        bad = set(pattern) - set(WIDTHS) - {GAP}
        if bad:
            raise PlaqueError(f"读数串含非法字符: {sorted(bad)}")
        n = len(pattern)
        hits: list[int] = []
        for start in range(0, len(self.widths) - n + 1):
            window = self.widths[start : start + n]
            if all(p == GAP or p == w for p, w in zip(pattern, window)):
                hits.append(self.year_at(start + n - 1))
        return hits


def load_band(path: str) -> ReferenceBand:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    widths = raw["widths"]
    if not widths or set(widths) - set(WIDTHS):
        raise PlaqueError(f"参照带 {path} 含 窄/宽 以外的字符")
    start_year = raw["start_year"]
    if not isinstance(start_year, int) or isinstance(start_year, bool):
        raise PlaqueError(f"参照带 {path} 的 start_year 必须是整数")
    return ReferenceBand(start_year=start_year, widths=widths)
