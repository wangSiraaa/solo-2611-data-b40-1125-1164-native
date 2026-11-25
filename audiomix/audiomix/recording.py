"""把数据仓里“可重开的 16kHz 渲染”当作录音原件。

录音原件不另存副本：它就是一个不可变的 archive render。重开同一
``archive_root + render_id`` 得到逐样本相同的 PCM，进而得到逐格相同的
板响检测（见 :mod:`audiomix.clapper`）。板响的“来源”来自该 render 的
块追溯（:meth:`ArchiveStore.read_trace`）：某个录音格落在哪一块、那一块
用了哪些 clip / 素材摘要。

这样“对板室从实际区间读出板响和来源”有据可查：检测到的每个板响都能
回溯到具体块与 clip/asset，而不是凭某个外部标注。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .archive import ArchiveStore
from .clapper import (
    REC_RATE, SAMPLES_PER_CELL, TemplateBook,
)


@dataclass(frozen=True)
class BangHit:
    """一次实际检测到的板响：格 + 花纹 + 来源（块/clip/asset）。"""

    cell: int
    pattern: str
    sample_start: int
    sample_end: int
    block_index: int | None
    block_key: str | None
    clip_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "cell": self.cell, "pattern": self.pattern,
            "sample_range": [self.sample_start, self.sample_end],
            "block_index": self.block_index, "block_key": self.block_key,
            "clip_ids": list(self.clip_ids), "asset_ids": list(self.asset_ids),
        }


class Recording:
    """一条录音原件（对某个 archive render 的只读视图）。"""

    def __init__(self, store: ArchiveStore, render_id: str):
        self.store = store
        self.render_id = render_id
        self.meta = store.render_meta(render_id)
        if self.meta["n_samples"] % SAMPLES_PER_CELL != 0:
            # 允许非整格，但要求样本率为 16k（本仓固定）
            pass

    @property
    def sample_rate(self) -> int:
        return REC_RATE

    def pcm(self) -> np.ndarray:
        """重开：从块存储拼出整段 PCM（逐样本可复现）。"""
        return self.store.full_pcm(self.render_id)

    def detect(self, book: TemplateBook) -> dict[str, list[int]]:
        """返回 ``{pattern: [global_cell,...]}``。"""
        return book.detect(self.pcm())

    def bangs_with_provenance(self, book: TemplateBook) -> list[BangHit]:
        """检测板响并逐条附上块/clip/asset 来源。"""
        pcm = self.pcm()
        raw = book.bangs(pcm)
        if not raw:
            return []
        # 一次取全书 trace，建立格 -> 块 的映射
        meta = self.store.render_meta(self.render_id)
        trace = self.store.read_trace(self.render_id, 0, meta["n_samples"])
        # 块 -> (block_index, block_key, clip_ids, asset_ids)
        def locate(sample: int):
            for t in trace:
                s0, s1 = t["sample_range"]
                if s0 <= sample < s1:
                    return t
            return None
        out: list[BangHit] = []
        for cell, pat in raw:
            s = cell * SAMPLES_PER_CELL
            tmpl_len = len(book.get(pat).samples)
            blk = locate(s)
            out.append(BangHit(
                cell=cell, pattern=pat, sample_start=s, sample_end=s + tmpl_len,
                block_index=(blk["block_index"] if blk else None),
                block_key=(blk["block_key"] if blk else None),
                clip_ids=tuple(blk["clip_ids"]) if blk else (),
                asset_ids=tuple(blk["asset_ids"]) if blk else (),
            ))
        return out
