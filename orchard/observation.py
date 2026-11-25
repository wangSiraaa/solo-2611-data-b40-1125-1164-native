"""观测端：把多份事实/冷天补记归出**相容的整园花期表**。

冷天修订合同
============
* 一条冷天断言按 ``(片区, 天)`` 归组；同组内出现「发生」与「没发生」并存即冲突，
  抛 :class:`orchard.errors.ColdRevisionConflict`，并指明是哪一天、哪一边
  （发生 / 没发生）——``day`` 与 ``occurred_ref`` / ``absent_ref`` 即“缺的是哪一天哪一边”。
* 同一片区若发生冷天，则该片区各株**一起迟一天**（按“发生的不同日历天数”计数，
  每个发生日 +1 天）。因为按**不同日历天**去重计数，
  “同一个冷天既当成发生又当成没发生”与“同一冷天被两次复跑各算一次”都不会发生：
  发生日只按日历天计入一次，矛盾断言直接判冲突。

整园花期表
==========
:class:`Timeline` 是片区内各株**经冷天平移后**的开花/可供/受粉日集合。每条事实的
support 仍以**原始日**索引，故用 ``original_day(tree, shifted_day)`` 反查是哪份记录支撑。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ColdRevisionConflict
from .ingest import ColdClaim, RawFacts

# 方向标签
PROVIDE = "provide"
RECEIVE = "receive"
BLOOM = "bloom"


@dataclass(frozen=True)
class ColdReconciliation:
    """冷天归并结果：每片区延迟天数 + 每个发生日的代表记录（用于定位补记）。"""

    delay: dict[str, int]
    occurred_days: dict[str, dict[int, ColdClaim]]  # region -> day -> 一条“发生”断言

    def delay_for(self, region: str) -> int:
        return self.delay.get(region, 0)


def reconcile_cold(claims: list[ColdClaim]) -> ColdReconciliation:
    """把（可能来自多次归档复跑的）冷天断言归并成一个相容视图。

    * 同 ``(region, day)`` 的断言必须一致；发生/没发生并存 -> 冲突；
    * 延迟按片区统计**不同发生日历天**，天然对“复跑两次”去重。
    """
    by_key: dict[tuple[str, int], list[ColdClaim]] = {}
    for c in claims:
        by_key.setdefault((c.region, c.day), []).append(c)

    delay: dict[str, int] = {}
    occurred_days: dict[str, dict[int, ColdClaim]] = {}
    for (region, day), group in sorted(by_key.items()):
        occurred = [c for c in group if c.occurred]
        absent = [c for c in group if not c.occurred]
        if occurred and absent:
            raise ColdRevisionConflict(
                region=region,
                day=day,
                occurred_ref=occurred[0],
                absent_ref=absent[0],
            )
        # 全部一致：要么都发生，要么都没发生
        if occurred:
            # 取最早一条为代表记录（保持确定性）
            rep = occurred[0]
            occurred_days.setdefault(region, {})[day] = rep

    for region, days in occurred_days.items():
        delay[region] = len(days)  # 每个发生日历天 +1，且按天去重
    return ColdReconciliation(delay=delay, occurred_days=occurred_days)


@dataclass(frozen=True)
class Timeline:
    """整园花期表（已应用冷天平移）。"""

    facts: RawFacts
    cold: ColdReconciliation
    bloom: dict[str, frozenset[int]]
    provide: dict[str, frozenset[int]]
    receive: dict[str, frozenset[int]]
    claims: tuple = ()

    @property
    def trees(self) -> tuple[str, ...]:
        return self.facts.trees

    def region_of(self, tree: str) -> str:
        return self.facts.region[tree]

    def original_day(self, tree: str, shifted_day: int) -> int:
        """把整园花期表的平移日还原为原始记录日（用于回查 support）。"""
        return shifted_day - self.cold.delay_for(self.region_of(tree))

    def row_of(self, concern: str, tree: str, shifted_day: int) -> int | None:
        return self.facts.row_of(concern, tree, self.original_day(tree, shifted_day))


def _shift(days: set[int], region: str, cold: ColdReconciliation) -> frozenset[int]:
    d = cold.delay_for(region)
    return frozenset(day + d for day in days)


def build_timeline(facts: RawFacts, claims: list[ColdClaim]) -> Timeline:
    """归并冷天并产出整园花期表（各株可开/可供/受粉的平移日集合）。"""
    cold = reconcile_cold(claims)
    bloom = {t: _shift(facts.bloom[t], facts.region[t], cold) for t in facts.trees}
    provide = {t: _shift(facts.provide[t], facts.region[t], cold) for t in facts.trees}
    receive = {t: _shift(facts.receive[t], facts.region[t], cold) for t in facts.trees}
    return Timeline(
        facts=facts,
        cold=cold,
        bloom=bloom,
        provide=provide,
        receive=receive,
        claims=tuple(claims),
    )
