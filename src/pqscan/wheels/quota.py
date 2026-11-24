"""轴对翻修额度：把履历段的可行走格数汇总成"还可走几格"。

馆内自订翻修期限 limit（总额度，单位格）。履历里每段的实际格数可能有多个
可行解（见 drum.feasible_ticks），模糊签条又会展开多条分支，因此单个轴对的
已耗格数给的是区间 [consumed_min, consumed_max]：

- headroom_min = limit - consumed_max：最保守剩余（任何履历分支、任何可行解下都成立）；
- headroom_max = limit - consumed_min：最乐观剩余。

馆规：运行后恰好到限可以收车，所以判定写成 consumed + ticks <= limit（等号允许），
超一格即超期。值班员拿到的是区间而不是单点，保守判定默认用 consumed_max。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..errors import WheelsError
from .history import Branch


@dataclass(frozen=True)
class AxleQuota:
    axle: str
    limit: int
    consumed_min: int
    consumed_max: int
    branches: tuple[str, ...]          # 参与汇总的可行分支名

    @property
    def headroom_min(self) -> int:
        """任何可行履历下都保证还能走的格数（可能为负=已超期）。"""
        return self.limit - self.consumed_max

    @property
    def headroom_max(self) -> int:
        return self.limit - self.consumed_min

    @property
    def overdue(self) -> bool:
        """连最乐观的已耗下限都已超期。"""
        return self.consumed_min > self.limit

    def can_cover(self, ticks: int, *, conservative: bool = True) -> bool:
        """该轴对再走 ticks 格是否不超期（conservative=True 用最大已耗口径）。"""
        consumed = self.consumed_max if conservative else self.consumed_min
        return consumed + ticks <= self.limit

    def to_dict(self) -> dict:
        return {
            "axle": self.axle,
            "overhaul_limit": self.limit,
            "consumed_min": self.consumed_min,
            "consumed_max": self.consumed_max,
            "headroom_min": self.headroom_min,
            "headroom_max": self.headroom_max,
            "overdue": self.overdue,
            "branches": list(self.branches),
        }


def build_quota(branches: list[Branch], limits: dict[str, int],
                spare_axles: list[str] | None = None) -> dict[str, AxleQuota]:
    """只汇总 feasible 分支；每条分支内段实际格数取 min/max 做区间并集。

    spare_axles 里没有任何履历段的纯库房备轮也纳入，已耗为 0、剩余等于整期限。
    """
    feasible = [b for b in branches if b.feasible]
    if not feasible:
        raise WheelsError("全部签条分支都被物理约束排斥，没有可用于排程的履历")

    axles = sorted({axle for b in feasible for axle in b.axle_segments}
                   | set(spare_axles or ()))
    quota: dict[str, AxleQuota] = {}
    for axle in axles:
        if axle not in limits:
            raise WheelsError(f"轴对 {axle} 缺少馆内翻修期限 limit")
        mins = maxes = None
        used_branches: list[str] = []
        for b in feasible:
            traces = b.axle_segments.get(axle)
            if not traces:
                continue
            lo = sum(t.feasible_ticks[0] for t in traces)
            hi = sum(t.feasible_ticks[-1] for t in traces)
            mins = lo if mins is None else min(mins, lo)
            maxes = hi if maxes is None else max(maxes, hi)
            used_branches.append(b.name)
        quota[axle] = AxleQuota(
            axle=axle, limit=limits[axle],
            consumed_min=mins if mins is not None else 0,
            consumed_max=maxes if maxes is not None else 0,
            branches=tuple(used_branches) or ("spare_no_history",),
        )
    return quota
