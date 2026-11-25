"""放映单、按帧查询与回放（换速/补帧）状态。

一份 :class:`ScreeningSheet` 是“已提交一套声画对应”后可导出的**放映单**：
对每个胶片全局帧，可查询它落在哪卷、卷内第几帧、对应录音的哪一格/哪些
样本，以及落在该帧的具名台词与各类锁定/可补标记。放映单可导出为 JSON，
之后可整份存盘、重开、按帧查询，或被旧放映单复查复用。

回放语义（题面“可换速的卷界”“已放帧保留”“可补的无对白帧”）：

- 每卷有一套 (gear, offset) 映射；换速只能在**卷界**改档 ——
  :meth:`Playback.change_gear` 仅当该卷尚未放映任何帧时允许（此时正处在
  它的卷界），已放映的前序卷帧一律保留、不受影响。
- 已放映帧记入 ``played``，任何换速/补帧都不得改动已放映帧。
- 可补无对白帧只能取自工程标定的 ``fillable_frames``，且不得是已锁片头、
  院方静音帧或已放映帧；补帧会将其标记为“已用作校正/补白”。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .clapper import SAMPLES_PER_CELL
from .film import FilmProject
from .rationals import dump_r
from .sync import ReelSolution


class PlaybackError(ValueError):
    pass


@dataclass(frozen=True)
class FrameEntry:
    global_frame: int
    reel_id: str
    local_frame: int
    cell: Fraction                 # 卷内局部格 + tape_base = 全局格（有理）
    sample_range: tuple[int, int]  # 对应录音原件的样本半开区间
    line_ids: tuple[str, ...]
    flags: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "global_frame": self.global_frame,
            "reel_id": self.reel_id,
            "local_frame": self.local_frame,
            "cell": dump_r(self.cell),
            "cell_decimal": float(self.cell),
            "sample_range": [self.sample_range[0], self.sample_range[1]],
            "line_ids": list(self.line_ids),
            "flags": list(self.flags),
        }


class ScreeningSheet:
    """提交一套（每卷一个）解释后的放映单。"""

    def __init__(self, project: FilmProject,
                 chosen: dict[str, ReelSolution]):
        self.project = project
        self.chosen = chosen
        # 校验：已提交解必须来自该卷、且齿轮在工程给定档位内
        for rid, sol in chosen.items():
            reel = project.reel(rid)
            if sol.reel_id != rid:
                raise ValueError(f"解释 {rid} 与卷不匹配")
            if not (0 <= sol.gear_index < len(project.gears)):
                raise ValueError(f"卷 {rid} 齿轮档位非法")
            if sol.gear != project.gears[sol.gear_index]:
                raise ValueError(f"卷 {rid} 解释齿轮与工程档位不符")
            if sol.offset not in reel.start_candidates:
                raise ValueError(f"卷 {rid} 起点不在候选内")
        missing = [r.reel_id for r in project.reels if r.reel_id not in chosen]
        if missing:
            raise ValueError(f"缺少卷的提交解释: {missing}")

        # 台词 -> 全局帧 的落点（按提交解）
        self.line_at: dict[int, list[str]] = {}
        self.line_frame: dict[str, int] = {}
        for ln in project.lines:
            rc = project.reel_of_cell(ln.cell)
            if rc is None:
                continue
            reel, local_cell = rc
            sol = chosen[reel.reel_id]
            f = sol.local_frame_of_cell(local_cell)
            if f is None or not (0 <= f < reel.frames):
                continue
            gf = project.frame_base(reel.reel_id) + f
            self.line_frame[ln.line_id] = gf
            self.line_at.setdefault(gf, []).append(ln.line_id)
        for k in self.line_at:
            self.line_at[k].sort()

    # ------------------------- 查询 ------------------------- #
    def entry(self, global_frame: int) -> FrameEntry:
        reel, local = self.project.reel_of_frame(global_frame)
        sol = self.chosen[reel.reel_id]
        # 全局格 = tape_base + offset + f*gear；一帧占 [cell_start, cell_start+gear)
        local_cell = Fraction(sol.offset) + local * sol.gear
        cell = self.project.tape_base(reel.reel_id) + local_cell
        cell_end = cell + sol.gear
        # 逐帧音频窗：覆盖本帧所辖录音格，取整到样本（floor 起、ceil 止）
        import math
        a = math.floor(cell * SAMPLES_PER_CELL)
        b = math.ceil(cell_end * SAMPLES_PER_CELL)
        if b <= a:  # gear 极小窗口兜底，至少一个样本
            b = a + 1
        flags: list[str] = []
        if global_frame in self.project.locked_head_frames:
            flags.append("locked_head")
        if global_frame in self.project.silent_locked_frames:
            flags.append("silent_locked")
        if global_frame in self.project.fillable_frames:
            flags.append("fillable")
        line_ids = tuple(self.line_at.get(global_frame, ()))
        if line_ids:
            flags.append("dialogue")
        if not flags:
            flags.append("blank")
        return FrameEntry(global_frame, reel.reel_id, local, cell, (a, b),
                          line_ids, tuple(flags))

    def frame_query(self, global_frame: int) -> dict[str, Any]:
        return self.entry(global_frame).to_json()

    def range_query(self, lo: int, hi: int) -> list[dict[str, Any]]:
        return [self.entry(f).to_json()
                for f in range(max(0, lo), min(self.project.total_frames, hi))]

    def line_query(self, line_id: str) -> dict[str, Any] | None:
        if line_id not in self.line_frame:
            return None
        return self.entry(self.line_frame[line_id]).to_json()

    def reel_boundaries(self) -> list[int]:
        """卷界（每卷首帧的全局帧号，升序）—— 也即允许换速的位置。"""
        return [self.project.frame_base(r.reel_id) for r in self.project.reels]

    def frame_audio_windows(self) -> list[tuple[int, int]]:
        """逐帧对应的录音样本半开窗 ``[(a,b), ...]``，按全局帧号排列。"""
        return [self.entry(f).sample_range for f in range(self.project.total_frames)]

    def export_audio_track(self, source_pcm, out_path: str) -> str:
        """按放映单把录音原件按帧序拼成“相应音轨”（16bit 单声道 WAV）。

        ``source_pcm`` 是录音原件（与放映单同一坐标系）；每一帧取其
        ``sample_range`` 的样本顺序拼接，得到与放映单一帧一格对齐的音轨。
        """
        import wave
        import numpy as np
        src = np.asarray(source_pcm, dtype=np.int16)
        pieces = []
        for a, b in self.frame_audio_windows():
            a = max(0, min(len(src), a))
            b = max(a, min(len(src), b))
            if b > a:
                pieces.append(src[a:b])
        track = np.concatenate(pieces) if pieces else np.empty(0, dtype=np.int16)
        with wave.open(out_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(track.astype("<i2").tobytes())
        return out_path

    def to_manifest(self) -> dict[str, Any]:
        """完整放映单：几何、卷界、逐帧条目与音频映射。"""
        return {
            "format": "audiomix.screening/1",
            "film_id": self.project.film_id,
            "total_frames": self.project.total_frames,
            "sample_rate": 16000,
            "samples_per_cell": SAMPLES_PER_CELL,
            "reel_boundaries": self.reel_boundaries(),
            "reels": [
                {
                    "reel_id": r.reel_id,
                    "frame_base": self.project.frame_base(r.reel_id),
                    "frames": r.frames,
                    "tape_base": self.project.tape_base(r.reel_id),
                    "tape_cells": r.tape_cells,
                    "gear_index": self.chosen[r.reel_id].gear_index,
                    "gear": dump_r(self.chosen[r.reel_id].gear),
                    "offset": self.chosen[r.reel_id].offset,
                }
                for r in self.project.reels
            ],
            "frame_entries": [self.entry(f).to_json()
                              for f in range(self.project.total_frames)],
            "line_frames": dict(sorted(self.line_frame.items())),
        }

    def save_manifest(self, path: str) -> str:
        import json
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_manifest(), fh, ensure_ascii=False, indent=2)
        return path


class Playback:
    """回放状态机：卷界换速、已放帧保留、可补无对白帧。"""

    def __init__(self, sheet: ScreeningSheet):
        self.sheet = sheet
        self.project = sheet.project
        self.played: set[int] = set()
        self.filled: set[int] = set()

    def play(self, lo: int, hi: int) -> None:
        """放映 [lo,hi) 帧，记入已放帧。"""
        for f in range(max(0, lo), min(self.project.total_frames, hi)):
            self.played.add(f)

    def change_gear(self, reel_id: str, gear_index: int, offset: int) -> None:
        """在卷界换速：仅当该卷尚无已放帧时允许；已放帧保留不动。"""
        reel = self.project.reel(reel_id)
        base = self.project.frame_base(reel_id)
        played_in_reel = [f for f in self.played
                          if base <= f < base + reel.frames]
        if played_in_reel:
            raise PlaybackError(
                f"卷 {reel_id} 已有 {len(played_in_reel)} 帧放映，换速只能在其卷界进行")
        if not (0 <= gear_index < len(self.project.gears)):
            raise PlaybackError(f"齿轮档位非法: {gear_index}")
        if offset not in reel.start_candidates:
            raise PlaybackError(f"起点 {offset} 不在卷 {reel_id} 候选内")
        old = self.sheet.chosen[reel_id]
        from .sync import ReelSolution
        self.sheet.chosen[reel_id] = ReelSolution(
            reel_id=reel_id, gear_index=gear_index,
            gear=self.project.gears[gear_index], offset=offset,
            unseen_bangs=old.unseen_bangs, real_bangs=old.real_bangs)
        # 重新计算台词落点，但不触碰已放帧
        self._rebuild_line_map()

    def _rebuild_line_map(self) -> None:
        self.sheet.line_at = {}
        self.sheet.line_frame = {}
        for ln in self.project.lines:
            rc = self.project.reel_of_cell(ln.cell)
            if rc is None:
                continue
            reel, local_cell = rc
            sol = self.sheet.chosen[reel.reel_id]
            f = sol.local_frame_of_cell(local_cell)
            if f is None or not (0 <= f < reel.frames):
                continue
            gf = self.project.frame_base(reel.reel_id) + f
            self.sheet.line_frame[ln.line_id] = gf
            self.sheet.line_at.setdefault(gf, []).append(ln.line_id)
        for k in self.sheet.line_at:
            self.sheet.line_at[k].sort()

    def fill_frame(self, global_frame: int) -> None:
        """补一个无对白帧（仅限标定可补帧，不得锁头/静音/已放）。"""
        if global_frame not in self.project.fillable_frames:
            raise PlaybackError(f"帧 {global_frame} 不是可补无对白帧")
        if global_frame in self.project.locked_head_frames:
            raise PlaybackError(f"帧 {global_frame} 为已锁片头，不能补")
        if global_frame in self.project.silent_locked_frames:
            raise PlaybackError(f"帧 {global_frame} 为院方静音帧，不能补")
        if global_frame in self.played:
            raise PlaybackError(f"帧 {global_frame} 已放映，不能补")
        if global_frame in self.filled:
            raise PlaybackError(f"帧 {global_frame} 已补过")
        self.filled.add(global_frame)

    def state(self) -> dict[str, Any]:
        return {
            "played_frames": sorted(self.played),
            "filled_frames": sorted(self.filled),
            "chosen": {rid: sol.to_json() for rid, sol in self.sheet.chosen.items()},
        }
