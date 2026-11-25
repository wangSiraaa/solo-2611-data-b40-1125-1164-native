"""胶片卷、帧号、场记板标、具名台词与锁帧的工程模型。

坐标约定（全部整数或精确有理数，杜绝浮点边界）：

- **胶片全局帧**：``0,1,2,...``，各卷按片卷次序首尾相接。第 r 卷占
  ``[frame_base[r], frame_base[r]+frames_r)``。
- **录音全局格**：以十个录音样本为一格（见 :mod:`audiomix.clapper`）。
  各卷的录音按同样次序排布，第 r 卷占
  ``[tape_base[r], tape_base[r]+tape_cells_r)``。
- **卷内映射**：卷 r 内局部帧 ``f`` 对应局部录音格
  ``offset + f * cells_per_frame``（``cells_per_frame`` 即走片速度的一档，
  为 ``1`` 时“每胶片帧推进一格”）。``offset`` 是该卷待校起点，可为一个
  小的候选集合（题面试片为 ``{0,1}``）。

一份 :class:`FilmProject` 是可序列化的：重开同一 JSON 必得同一对象，
求解结果只由这份 JSON 决定（不含任何时间戳/随机数）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .rationals import dump_r, load_r

# 题面“各少一声”的缺省上限：每卷录音带拼接处最多漏一声、胶片最多跳一格。
DEFAULT_MISSING_IN_RECORDING = 1
DEFAULT_MISSING_IN_FILM = 1


@dataclass(frozen=True)
class Reel:
    """一卷胶片，带它那一段录音的格数。"""

    reel_id: str
    frames: int                       # 本卷胶片帧数（局部帧 0..frames-1）
    tape_cells: int                   # 本卷占用的录音格数
    start_candidates: tuple[int, ...] = (0,)   # 待校起点的候选集合

    def __post_init__(self) -> None:
        if not self.reel_id:
            raise ValueError("reel_id 不能为空")
        if self.frames <= 0:
            raise ValueError(f"卷 {self.reel_id} 帧数必须为正")
        if self.tape_cells <= 0:
            raise ValueError(f"卷 {self.reel_id} 录音格数必须为正")
        if not self.start_candidates:
            raise ValueError(f"卷 {self.reel_id} 起点候选不能为空")
        for o in self.start_candidates:
            if o < 0:
                raise ValueError(f"卷 {self.reel_id} 起点候选不能为负")


@dataclass(frozen=True)
class FrameMark:
    """视觉场记手录的一条“第某帧有某花纹的板标”。"""

    reel_id: str
    frame: int            # 卷内局部帧号
    pattern: str


@dataclass(frozen=True)
class DialogueLine:
    """一条具名台词：在录音的某个格，允许落在 ``[allowed_lo, allowed_hi]`` 帧内。"""

    line_id: str
    cell: int             # 录音全局格
    allowed_lo: int       # 允许的胶片全局帧下界（含）
    allowed_hi: int       # 允许的胶片全局帧上界（含）
    text: str = ""

    def allows(self, global_frame: int) -> bool:
        return self.allowed_lo <= global_frame <= self.allowed_hi


@dataclass
class FilmProject:
    """一份完整的对板/排片工程（可整份存盘、重开）。"""

    film_id: str
    gears: tuple[Fraction, ...]                 # 已知的两档（或指定）走片速度
    reels: tuple[Reel, ...]
    frame_marks: tuple[FrameMark, ...] = ()
    lines: tuple[DialogueLine, ...] = ()
    locked_head_frames: frozenset[int] = frozenset()   # 已锁片头（全局帧）
    fillable_frames: frozenset[int] = frozenset()      # 可补的无对白帧（全局帧）
    silent_locked_frames: frozenset[int] = frozenset() # 院方锁定的静音帧（全局帧）
    max_missing_in_recording: int = DEFAULT_MISSING_IN_RECORDING
    max_missing_in_film: int = DEFAULT_MISSING_IN_FILM

    def __post_init__(self) -> None:
        if not self.film_id:
            raise ValueError("film_id 不能为空")
        if not self.gears:
            raise ValueError("至少需要一档走片速度")
        for g in self.gears:
            if g <= 0:
                raise ValueError(f"走片速度（每帧推进格数）必须为正，收到 {g}")
        if not self.reels:
            raise ValueError("至少需要一卷")
        ids = [r.reel_id for r in self.reels]
        if len(set(ids)) != len(ids):
            raise ValueError("reel_id 重复")
        # 预计算索引
        self._frame_base = {}
        self._tape_base = {}
        fb = tb = 0
        for r in self.reels:
            self._frame_base[r.reel_id] = fb
            self._tape_base[r.reel_id] = tb
            fb += r.frames
            tb += r.tape_cells
        self._total_frames = fb
        self._total_cells = tb
        # 场记板标必须落在其卷内
        for m in self.frame_marks:
            if m.reel_id not in self._frame_base:
                raise ValueError(f"板标引用未知卷 {m.reel_id}")
            if not (0 <= m.frame < self._reel(m.reel_id).frames):
                raise ValueError(f"板标帧 {m.frame} 越出卷 {m.reel_id}")
        for ln in self.lines:
            if not (0 <= ln.cell < tb):
                raise ValueError(f"台词 {ln.line_id} 的录音格 {ln.cell} 越出总格数 {tb}")

    # ------------------------- 索引/查询 ------------------------- #
    @property
    def total_frames(self) -> int:
        return self._total_frames

    @property
    def total_cells(self) -> int:
        return self._total_cells

    def reel(self, reel_id: str) -> Reel:
        return self._reel(reel_id)

    def _reel(self, reel_id: str) -> Reel:
        for r in self.reels:
            if r.reel_id == reel_id:
                return r
        raise KeyError(f"未知卷: {reel_id}")

    def frame_base(self, reel_id: str) -> int:
        return self._frame_base[reel_id]

    def tape_base(self, reel_id: str) -> int:
        return self._tape_base[reel_id]

    def reel_of_frame(self, global_frame: int) -> tuple[Reel, int]:
        """全局帧 -> (卷, 卷内局部帧)。"""
        if not (0 <= global_frame < self._total_frames):
            raise IndexError(f"帧 {global_frame} 越出总帧数 {self._total_frames}")
        for r in self.reels:
            b = self._frame_base[r.reel_id]
            if b <= global_frame < b + r.frames:
                return r, global_frame - b
        raise AssertionError("不可达")

    def reel_of_cell(self, global_cell: int) -> tuple[Reel, int] | None:
        """全局格 -> (卷, 卷内局部格)；落在任何卷录音段之外则返回 None。"""
        for r in self.reels:
            b = self._tape_base[r.reel_id]
            if b <= global_cell < b + r.tape_cells:
                return r, global_cell - b
        return None

    def marks_of(self, reel_id: str) -> list[FrameMark]:
        return sorted((m for m in self.frame_marks if m.reel_id == reel_id),
                      key=lambda m: m.frame)

    # ------------------------- 序列化 ------------------------- #
    def to_json(self) -> dict[str, Any]:
        return {
            "format": "audiomix.film/1",
            "film_id": self.film_id,
            "gears": [dump_r(g) for g in self.gears],
            "reels": [
                {"reel_id": r.reel_id, "frames": r.frames,
                 "tape_cells": r.tape_cells,
                 "start_candidates": list(r.start_candidates)}
                for r in self.reels
            ],
            "frame_marks": [
                {"reel_id": m.reel_id, "frame": m.frame, "pattern": m.pattern}
                for m in self.frame_marks
            ],
            "lines": [
                {"line_id": ln.line_id, "cell": ln.cell,
                 "allowed_lo": ln.allowed_lo, "allowed_hi": ln.allowed_hi,
                 "text": ln.text}
                for ln in self.lines
            ],
            "locked_head_frames": sorted(self.locked_head_frames),
            "fillable_frames": sorted(self.fillable_frames),
            "silent_locked_frames": sorted(self.silent_locked_frames),
            "max_missing_in_recording": self.max_missing_in_recording,
            "max_missing_in_film": self.max_missing_in_film,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "FilmProject":
        if obj.get("format") != "audiomix.film/1":
            raise ValueError("非法胶片工程：format 不匹配")
        reels = tuple(
            Reel(
                reel_id=str(r["reel_id"]),
                frames=int(r["frames"]),
                tape_cells=int(r["tape_cells"]),
                start_candidates=tuple(int(o) for o in r.get("start_candidates", [0])),
            )
            for r in obj["reels"]
        )
        marks = tuple(
            FrameMark(str(m["reel_id"]), int(m["frame"]), str(m["pattern"]))
            for m in obj.get("frame_marks", [])
        )
        lines = tuple(
            DialogueLine(str(ln["line_id"]), int(ln["cell"]),
                         int(ln["allowed_lo"]), int(ln["allowed_hi"]),
                         str(ln.get("text", "")))
            for ln in obj.get("lines", [])
        )
        return cls(
            film_id=str(obj["film_id"]),
            gears=tuple(load_r(g) for g in obj["gears"]),
            reels=reels,
            frame_marks=marks,
            lines=lines,
            locked_head_frames=frozenset(int(x) for x in obj.get("locked_head_frames", [])),
            fillable_frames=frozenset(int(x) for x in obj.get("fillable_frames", [])),
            silent_locked_frames=frozenset(int(x) for x in obj.get("silent_locked_frames", [])),
            max_missing_in_recording=int(obj.get("max_missing_in_recording",
                                                 DEFAULT_MISSING_IN_RECORDING)),
            max_missing_in_film=int(obj.get("max_missing_in_film",
                                            DEFAULT_MISSING_IN_FILM)),
        )

    def save(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh, ensure_ascii=False, indent=2)
        return path

    @classmethod
    def load(cls, path: str) -> "FilmProject":
        with open(path, encoding="utf-8") as fh:
            return cls.from_json(json.load(fh))
