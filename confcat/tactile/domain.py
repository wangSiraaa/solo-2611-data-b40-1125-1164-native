"""触读路牌领域核心：点阵刻样、底座朝向、半周翻转与触读签名。

坐标约定
========
一维格条，格号 ``0 .. n-1``，0 为左端、n-1 为右端。所有图样先以“制样正向”
（upright）描述；底座允许转半周时，访客实际摸到的是翻转后的图样。

访客的触读始终在其**自身的触觉参照系**里（他摸到的“左端”就是 felt 0），
因此不同朝向产生的触读用“翻转后的格点 + 翻转后的边”描述，方向标记也随牌
一起翻转——这正是“翻一块牌会不会把出口摸成楼梯”的判定基础。

两类底座
========
* ``locked`` 正向锁死底座：只允许 upright（可记录锁住哪一边 ``locked_side``）；
* ``swivel`` 普通座：允许转半周，朝向集合为 ``{upright, reversed}``。

可触摸的三件事（同一块牌由制牌员合成）
======================================
1. 未被遮挡的凸点（``dots - occluded``）；
2. 可摸边缘（``edges``，取 ``{"L","R"}`` 的子集）；
3. 方向标记（方案 A：在真实空格上刻的可摸锚点，搬位后仍可辨；无标记策略下为空）。

混淆判定只看**实际去向**：两块牌形状完全相同但指向同一去向不算混淆；
只有同一触读签名能对应到两个不同去向时才会把人引错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# 可摸边缘常量（正向参照系）
LEFT = "L"
RIGHT = "R"
_EDGES = (LEFT, RIGHT)

# 朝向
UPRIGHT = 0  # 正向
REVERSED = 1  # 转半周


def flip_index(i: int, n: int) -> int:
    """格号在半周翻转后的位置：i -> n-1-i。"""
    return n - 1 - i


def flip_edges(edges: Iterable[str]) -> frozenset[str]:
    """边缘随半周翻转：左端摸到的是原右端，反之亦然。"""
    return frozenset(RIGHT if e == LEFT else LEFT for e in edges)


# --------------------------------------------------------------------------- #
# 制样正向的牌与底座（归一任务产物）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlateSpec:
    """一块牌在制样正向下的点阵刻样。

    ``occluded`` 必须是 ``dots`` 的子集：被遮挡的凸点摸不到，但它占据的格仍
    不是空格（不能再刻方向标记）。
    """

    plate_id: str
    n: int
    dots: frozenset[int]
    occluded: frozenset[int] = frozenset()
    edges: frozenset[str] = frozenset(_EDGES)

    def __post_init__(self) -> None:
        if self.n < 1 or self.n > 4:
            raise ValueError(f"每边至多四格且至少一格，收到 n={self.n}")
        for i in self.dots:
            if not 0 <= i < self.n:
                raise ValueError(f"凸点格 {i} 越界（n={self.n}）")
        if not self.occluded.issubset(self.dots):
            raise ValueError("被遮挡格必须是已刻凸点格")
        bad = set(self.edges) - set(_EDGES)
        if bad:
            raise ValueError(f"未知边缘 {sorted(bad)}，仅允许 L/R")

    @property
    def felt_dots(self) -> frozenset[int]:
        """正向下真正能摸到的凸点（遮挡后的）。"""
        return self.dots - self.occluded

    @property
    def free_cells(self) -> frozenset[int]:
        """真实空格：既无凸点也无遮挡，可用于刻方向标记。"""
        return frozenset(range(self.n)) - self.dots

    def render(self) -> str:
        """正向 ASCII 刻样：● 凸点、○ 被遮挡凸点、· 空格，外加可摸边 []。"""
        cells = []
        for i in range(self.n):
            if i in self.occluded:
                cells.append("○")
            elif i in self.dots:
                cells.append("●")
            else:
                cells.append("·")
        left = "[" if LEFT in self.edges else " "
        right = "]" if RIGHT in self.edges else " "
        return f"{left}{' '.join(cells)}{right}"


@dataclass(frozen=True)
class BaseSpec:
    """底座允许的朝向。"""

    base_id: str
    kind: str  # "locked" | "swivel"
    locked_side: str | None = None  # locked 时锁住哪一边（记录用）

    def __post_init__(self) -> None:
        if self.kind not in ("locked", "swivel"):
            raise ValueError(f"未知底座类型 {self.kind!r}")
        if self.kind == "locked":
            if self.locked_side not in _EDGES:
                raise ValueError("locked 底座必须记录锁住的边 L/R")
        elif self.locked_side is not None:
            raise ValueError("swivel 底座不锁边")

    @property
    def orientations(self) -> tuple[int, ...]:
        return (UPRIGHT,) if self.kind == "locked" else (UPRIGHT, REVERSED)


# --------------------------------------------------------------------------- #
# 方向标记策略（题面方案 A：牌上留方向意义的可摸标记，占一个真实空格）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MarkerPolicy:
    """方向标记策略。``marker_cell`` 返回 None 表示该牌不打标记。"""

    name: str = "none"

    def marker_cell(self, plate: PlateSpec) -> int | None:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


@dataclass(frozen=True)
class NoMarkerPolicy(MarkerPolicy):
    """无方向标记：牌本身无方向意义，全靠底座锁边。"""

    name: str = "none"

    def marker_cell(self, plate: PlateSpec) -> int | None:
        return None


@dataclass(frozen=True)
class AnchorEndPolicy(MarkerPolicy):
    """方案 A：在牌的**固定一端**真实空格上刻锚点，约定“摸到锚点的一侧即该端”。

    锚点端是全展统一约定（默认右端 n-1），搬到别处仍可辨。该端被凸点/遮挡
    占用时**不回退到另一端**——回退会让锚点本身失去方向意义；此时该牌打不上
    方向标记（返回 None），只能依靠正向锁死底座。
    """

    anchor_end: str = RIGHT
    name: str = "anchor-end"

    def __post_init__(self) -> None:
        if self.anchor_end not in _EDGES:
            raise ValueError("anchor_end 仅允许 L/R")

    def marker_cell(self, plate: PlateSpec) -> int | None:
        anchor = plate.n - 1 if self.anchor_end == RIGHT else 0
        return anchor if anchor in plate.free_cells else None

    def describe(self) -> str:
        return f"anchor-end({self.anchor_end})"


# --------------------------------------------------------------------------- #
# 制牌员产物：可触摸图样集合
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FeltReading:
    """访客在某一朝向下实际摸到的图样（访客触觉参照系）。"""

    orientation: int
    dots: frozenset[int]
    edges: frozenset[str]
    marker: int | None  # 标记在触觉参照系中的格号；无标记为 None

    def render(self, n: int) -> str:
        cells = []
        for i in range(n):
            if self.marker == i:
                cells.append("◆")
            elif i in self.dots:
                cells.append("●")
            else:
                cells.append("·")
        left = "[" if LEFT in self.edges else " "
        right = "]" if RIGHT in self.edges else " "
        return f"{left}{' '.join(cells)}{right}"


def signature_key(reading: FeltReading) -> tuple:
    """触读签名：能被摸到的全部特征，哈希后用于分组比对。"""
    return (tuple(sorted(reading.dots)), tuple(sorted(reading.edges)), reading.marker)


@dataclass
class FabricatedSign:
    """制牌员把同一块牌折成的可触摸图样集合（含来源血缘）。"""

    plate: PlateSpec
    base: BaseSpec
    marker_policy: MarkerPolicy
    marker_cell: int | None
    readings: tuple[FeltReading, ...]
    source: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return self.plate.n

    def signatures(self) -> frozenset[tuple]:
        return frozenset(signature_key(r) for r in self.readings)


def read_sign(
    plate: PlateSpec,
    base: BaseSpec,
    policy: MarkerPolicy,
    source: dict | None = None,
) -> FabricatedSign:
    """制牌员工序：凸点遮挡 + 可摸边 + 允许翻转 + 方向标记 -> 触读图样集合。"""
    marker_cell = policy.marker_cell(plate)
    upright_dots = plate.felt_dots
    readings: list[FeltReading] = []
    for orientation in base.orientations:
        if orientation == UPRIGHT:
            dots = frozenset(upright_dots)
            edges = frozenset(plate.edges)
            marker = marker_cell
        else:
            dots = frozenset(flip_index(i, plate.n) for i in upright_dots)
            edges = flip_edges(plate.edges)
            marker = flip_index(marker_cell, plate.n) if marker_cell is not None else None
        readings.append(FeltReading(orientation, dots, edges, marker))
    return FabricatedSign(
        plate=plate,
        base=base,
        marker_policy=policy,
        marker_cell=marker_cell,
        readings=tuple(readings),
        source=dict(source or {}),
    )
