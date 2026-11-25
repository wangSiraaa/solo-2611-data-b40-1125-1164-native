"""制版端：版面裁边、具名标记、可放签位置与“闭合低雨区”实体标签。

核心规则（题面）：
- 一枚“闭合低雨区”签只可贴在**不与版边经上下左右低值连通**的低值区内；
- 同一低值连通块最多一签；
- 签必须在所有“仍可能读法”下都站得住（可靠辨别），并保留每签在每种读法下的
  依据；
- 具名标记给出版面落点，其中部分位置声明为可放实物标签的槽位；
- 裁框只做行列裁切（顶/底/左/右），裁后边点即新版边，原中心可能变成边点。

本模块只处理格值集合（:mod:`audiomix.fax` 枚举出的全部可能读法）与版面语义，
不直接接触音频。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable

from .fax import LOW

Grid = list[list[str]]


@dataclass(frozen=True)
class Crop:
    """版面裁边：从上/下/左/右各裁去的行列数。"""
    top: int = 0
    bottom: int = 0
    left: int = 0
    right: int = 0

    def __post_init__(self) -> None:
        if min(self.top, self.bottom, self.left, self.right) < 0:
            raise ValueError("裁边量不能为负")

    def to_json(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class Marker:
    """具名标记：名字 + 原图落点 (row,col) + 是否允许放实物标签。"""
    name: str
    row: int
    col: int
    label_slot: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def crop_grid(grid: Grid, crop: Crop) -> Grid:
    """按裁边单裁切一幅格值图，返回新图（边点随之改变）。"""
    h, w = len(grid), len(grid[0])
    r0, r1 = crop.top, h - crop.bottom
    c0, c1 = crop.left, w - crop.right
    if not (0 <= r0 < r1 <= h and 0 <= c0 < c1 <= w):
        raise ValueError("裁框把整幅图裁空或越界")
    return [row[c0:c1] for row in grid[r0:r1]]


def crop_markers(markers: Iterable[Marker], crop: Crop,
                 orig_h: int, orig_w: int) -> list[Marker]:
    """把具名标记映射到裁后坐标；落在被裁区域的标记移除。"""
    h, w = orig_h, orig_w
    r0, r1 = crop.top, h - crop.bottom
    c0, c1 = crop.left, w - crop.right
    out = []
    for m in markers:
        if r0 <= m.row < r1 and c0 <= m.col < c1:
            out.append(Marker(m.name, m.row - r0, m.col - c0, m.label_slot))
    return out


# ----------------------------- 低值连通块 ----------------------------- #
def low_components(grid: Grid) -> list[list[tuple[int, int]]]:
    """四邻接（上下左右）低值连通块，返回每块的格坐标列表。"""
    h, w = len(grid), len(grid[0])
    seen = [[False] * w for _ in range(h)]
    comps: list[list[tuple[int, int]]] = []
    for i in range(h):
        for j in range(w):
            if seen[i][j] or grid[i][j] != LOW:
                continue
            stack, comp = [(i, j)], []
            seen[i][j] = True
            while stack:
                r, c = stack.pop()
                comp.append((r, c))
                for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nr, nc = r + dr, c + dc
                    if (0 <= nr < h and 0 <= nc < w and not seen[nr][nc]
                            and grid[nr][nc] == LOW):
                        seen[nr][nc] = True
                        stack.append((nr, nc))
            comps.append(sorted(comp))
    return comps


def is_edge_cell(r: int, c: int, h: int, w: int) -> bool:
    return r == 0 or c == 0 or r == h - 1 or c == w - 1


def component_touches_edge(comp: list[tuple[int, int]], h: int, w: int) -> bool:
    return any(is_edge_cell(r, c, h, w) for r, c in comp)


# ----------------------------- 闭合签判定（跨全部读法） ----------------------------- #
@dataclass
class LabelEvidence:
    """一枚签在一种读法下的依据。"""
    reading_index: int
    component_cells: list[tuple[int, int]]
    touches_edge: bool
    closed: bool

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClosedLabel:
    """一枚可靠的“闭合低雨区”签及其在每种仍可能读法下的依据。"""
    name: str
    anchor: tuple[int, int]                  # 裁后图上的锚点（连通块代表格）
    component_signature: tuple[tuple[int, int], ...]
    readings: list[LabelEvidence]
    marker_name: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "anchor": list(self.anchor),
            "component_signature": [list(c) for c in self.component_signature],
            "marker_name": self.marker_name,
            "readings": [r.to_json() for r in self.readings],
        }


def reliable_closed_labels(possible: list[Grid], markers: Iterable[Marker],
                           label_prefix: str = "CLOSED") -> list[ClosedLabel]:
    """求在**全部**仍可能读法下都可靠的“闭合低雨区”签，每块至多一签。

    判定：一个格在某读法下“可签”，当且仅当它属于一个完全由低值组成且不接触
    版边的连通块。可靠签的锚点取**在所有读法里都可签**的格；不同读法下块的形状
    可以变化（相位/漏读本来就会改变格值），因此不要求坐标集合逐读法相同，只要求
    同一锚点在每种读法下都落在某个闭合低值块内，并把该读法下的实际块与触边情况
    完整留作依据。相邻的可靠锚点合并为一枚签（同一连通块最多一签）。
    具名标记的 label_slot 与锚点重合时，签挂到该具名槽位上。
    """
    if not possible:
        return []
    h, w = len(possible[0]), len(possible[0][0])
    slot_map = {(m.row, m.col): m.name for m in markers if m.label_slot}

    # 每种读法：格 -> 所在闭合块（仅登记不触边的低值块）
    closed_block_of: list[dict[tuple[int, int], frozenset[tuple[int, int]]]] = []
    for grid in possible:
        mapping: dict[tuple[int, int], frozenset[tuple[int, int]]] = {}
        for comp in low_components(grid):
            if not component_touches_edge(comp, h, w):
                key = frozenset(comp)
                for cell in comp:
                    mapping[cell] = key
        closed_block_of.append(mapping)

    def reliable_everywhere(cell: tuple[int, int]) -> bool:
        return all(cell in mapping for mapping in closed_block_of)

    # 用四邻接把“处处可靠”的格聚成锚点块，每块发一枚签
    reliable_cells = {(i, j) for i in range(h) for j in range(w)
                      if reliable_everywhere((i, j))}
    labels: list[ClosedLabel] = []
    visited: set[tuple[int, int]] = set()
    for seed in sorted(reliable_cells):
        if seed in visited:
            continue
        stack, group = [seed], []
        visited.add(seed)
        while stack:
            cell = stack.pop()
            group.append(cell)
            r, c = cell
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (r + dr, c + dc)
                if nb in reliable_cells and nb not in visited:
                    visited.add(nb)
                    stack.append(nb)
        group.sort()
        anchor = group[0]
        evid = []
        for ri, mapping in enumerate(closed_block_of):
            comp = sorted(mapping[anchor])
            evid.append(LabelEvidence(
                reading_index=ri, component_cells=comp,
                touches_edge=component_touches_edge(comp, h, w), closed=True))
        labels.append(ClosedLabel(
            name=f"{label_prefix}-{len(labels) + 1}", anchor=anchor,
            component_signature=tuple(group), readings=evid,
            marker_name=slot_map.get(anchor)))
    return labels


def labelable_cells(possible: list[Grid]) -> list[list[bool]]:
    """每格是否属于“在所有读法下都闭合”的低值块（供版面公示可签区域）。"""
    if not possible:
        return []
    h, w = len(possible[0]), len(possible[0][0])
    ok = [[False] * w for _ in range(h)]
    for lab in reliable_closed_labels(possible, []):
        for r, c in lab.component_signature:
            ok[r][c] = True
    return ok
