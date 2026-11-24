"""Board (样图) model: cells, adjacency and edge-mark geometry.

A board is a small rectangular grid (at most six cells).  Each cell requires a
particular face pattern zone *under each plate edition*, and each side of a
cell is either an outer border or touches a neighbouring cell.  Physical piece
edges carry one of three marks:

    "T"  tab (凸)      - mates only with a blank on the neighbouring piece
    "B"  blank (凹)    - mates only with a tab
    "F"  flat (平边)   - legal only on the outer border of the board

A piece may be rotated among its allowed orientations.  Rotation turns the
edge tuple; the mark itself never changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ContractError

EDGES: tuple[str, str, str] = ("T", "B", "F")
DIRECTIONS: tuple[str, str, str, str] = ("n", "e", "s", "w")
ORIENTATIONS: tuple[int, int, int, int] = (0, 90, 180, 270)
MAX_CELLS = 6
MAX_PIECES = 12


@dataclass(frozen=True)
class Cell:
    cell_id: str
    row: int
    col: int
    # edition -> face zone required at this cell under that edition
    zones: dict[str, str]

    def zone_for(self, edition: str) -> str | None:
        return self.zones.get(edition)


@dataclass(frozen=True)
class BoardSpec:
    board_id: str
    rows: int
    cols: int
    cells: tuple[Cell, ...]
    editions: tuple[str, ...]

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    def cell(self, cell_id: str) -> Cell | None:
        for c in self.cells:
            if c.cell_id == cell_id:
                return c
        return None

    def neighbor(self, cell: Cell, direction: str) -> Cell | None:
        """Return the cell adjacent on ``direction`` (n/e/s/w), or None."""
        dr, dc = _DELTA[direction]
        for other in self.cells:
            if other.row == cell.row + dr and other.col == cell.col + dc:
                return other
        return None

    def border_directions(self, cell: Cell) -> frozenset[str]:
        return frozenset(
            d for d in DIRECTIONS if self.neighbor(cell, d) is None
        )

    def internal_directions(self, cell: Cell) -> frozenset[str]:
        return frozenset(
            d for d in DIRECTIONS if self.neighbor(cell, d) is not None
        )


_DELTA = {
    "n": (-1, 0),
    "e": (0, 1),
    "s": (1, 0),
    "w": (0, -1),
}

# Opposite direction, used when checking the touching side of the neighbour.
_OPPOSITE = {"n": "s", "e": "w", "s": "n", "w": "e"}


def opposite(direction: str) -> str:
    return _OPPOSITE[direction]


def parse_board(raw: Any) -> BoardSpec:
    """Parse/validate a board document::

        {"board_id": "two-cell", "rows": 1, "cols": 2, "editions": ["X", "Y"],
         "cells": [{"cell_id": "L", "row": 0, "col": 0,
                    "zones": {"X": "ZL", "Y": "ZL"}}, ...]}
    """
    if not isinstance(raw, dict):
        raise ContractError("board document must be an object")
    extra = set(raw) - {"board_id", "rows", "cols", "editions", "cells"}
    if extra:
        raise ContractError(f"board: unexpected keys {sorted(extra)}")
    board_id = raw.get("board_id")
    if not isinstance(board_id, str) or not board_id:
        raise ContractError("board: board_id must be a non-empty string")
    rows, cols = raw.get("rows"), raw.get("cols")
    if type(rows) is not int or type(cols) is not int or rows < 1 or cols < 1:
        raise ContractError("board: rows/cols must be positive integers")
    editions = raw.get("editions")
    if not isinstance(editions, list) or not editions or any(
        not isinstance(e, str) or not e for e in editions
    ):
        raise ContractError("board: editions must be a non-empty list of strings")
    if len(set(editions)) != len(editions):
        raise ContractError("board: duplicate editions")
    cells_raw = raw.get("cells")
    if not isinstance(cells_raw, list) or not cells_raw:
        raise ContractError("board: cells must be a non-empty list")
    if len(cells_raw) > MAX_CELLS:
        raise ContractError(f"board: at most {MAX_CELLS} cells are supported")
    if len(cells_raw) != rows * cols:
        raise ContractError(
            f"board: {len(cells_raw)} cells do not fill a {rows}x{cols} grid"
        )

    cells: list[Cell] = []
    seen_positions: set[tuple[int, int]] = set()
    seen_ids: set[str] = set()
    for item in cells_raw:
        if not isinstance(item, dict):
            raise ContractError("board cell: must be an object")
        if set(item) != {"cell_id", "row", "col", "zones"}:
            raise ContractError(
                f"board cell {item.get('cell_id')!r}: keys must be exactly "
                "cell_id/row/col/zones"
            )
        cell_id = item["cell_id"]
        if not isinstance(cell_id, str) or not cell_id:
            raise ContractError("board cell: cell_id must be a non-empty string")
        if cell_id in seen_ids:
            raise ContractError(f"board cell: duplicate cell_id {cell_id!r}")
        seen_ids.add(cell_id)
        r, c = item["row"], item["col"]
        if type(r) is not int or type(c) is not int or not (0 <= r < rows) or not (0 <= c < cols):
            raise ContractError(f"board cell {cell_id!r}: row/col outside grid")
        if (r, c) in seen_positions:
            raise ContractError(f"board cell {cell_id!r}: two cells share one position")
        seen_positions.add((r, c))
        zones = item["zones"]
        if not isinstance(zones, dict) or not zones:
            raise ContractError(f"board cell {cell_id!r}: zones must be a non-empty object")
        for edition, zone in zones.items():
            if edition not in editions:
                raise ContractError(
                    f"board cell {cell_id!r}: zone for undeclared edition {edition!r}"
                )
            if not isinstance(zone, str) or not zone:
                raise ContractError(f"board cell {cell_id!r}: zone codes must be strings")
        cells.append(Cell(cell_id=cell_id, row=r, col=c, zones=dict(zones)))

    return BoardSpec(
        board_id=board_id,
        rows=rows,
        cols=cols,
        cells=tuple(cells),
        editions=tuple(editions),
    )


def rotate_edges(base: dict[str, str], orientation: int) -> dict[str, str]:
    """Rotate an orientation-0 ``{n,e,s,w}`` edge map by ``orientation``."""
    if orientation not in ORIENTATIONS:
        raise ContractError(f"orientation must be one of {ORIENTATIONS}, got {orientation}")
    turn = ORIENTATIONS.index(orientation)
    ordered = [base[d] for d in DIRECTIONS]
    return {
        d: ordered[(DIRECTIONS.index(d) - turn) % 4] for d in DIRECTIONS
    }


def edges_mate(edge: str, other: str) -> bool:
    """Two touching internal edges mate iff they are tab/blank opposites."""
    return (edge == "T" and other == "B") or (edge == "B" and other == "T")
