"""口袋果园花期展图（构建在 :mod:`confcat` 配置目录与消费仓之上）。

分工
====
* ``orchard.ingest``      —— 把「开花日志 / 供粉日段 / 受粉日段」作为真实 CSV 接入任务，
  用当时版本的列来源（column lineage）定位每条补记；
* ``orchard.observation`` —— 观测端把多版归档任务归出**相容的整园花期表**，
  校验「同一冷天不能既当成发生又当成没发生」，并给出每条重合的支撑记录；
* ``orchard.commitments`` —— 方向正确的花期交集判定：同一天、供粉/受粉方向正确、
  相距至多一个床位、自己不作伴；失败时指出缺的是哪一天哪一边；
* ``orchard.layout``      —— 展务端：固定床位图（须应付片区尚可能出现的冷天）
  与带轮盆树的换位方案（已开放通道不可穿越 + 每日搬运上限）；
* ``orchard.exhibit``     —— 串起上述环节，产出「承诺开展的树、伙伴、日期」，
  固定/轮式两条交付路线二选一但承诺一致。

题面规模：六株、五床、四天以内。
"""

from __future__ import annotations

DAYS = (1, 2, 3, 4)  # 四天以内
MAX_TREES = 6        # 六株
MAX_BEDS = 5         # 五床

__all__ = ["DAYS", "MAX_TREES", "MAX_BEDS", "__version__"]
__version__ = "1.0.0"
