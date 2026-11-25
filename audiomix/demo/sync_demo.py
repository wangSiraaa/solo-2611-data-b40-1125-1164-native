"""构造对板/放映演示输入：录音原件（archive render）、胶片工程、模板。

场景（与题面“试片”一致，扩成两卷小片）：

- 十个录音样本为一格；卷内每胶片帧推进一格（齿轮 1；另备齿轮 2 供换速）。
- 一卷：6 帧、10 格，待校起点 {0,1}；第 1 帧有板标 A；真实板响在格 2
  -> 唯一解起点 1；格 3 的台词落在第 2 帧。
- 二卷：6 帧、12 格（tape_base=10），待校起点 {0,1}；第 1 帧板标 B；
  真实板响在局部格 2（全局格 12）。
- 二卷内全局第 10 帧标为可补无对白帧，用于现场校正。
"""
from __future__ import annotations

import json
import os
import shutil
from fractions import Fraction as F

import numpy as np

from audiomix.archive import ArchiveStore
from audiomix.assets import AssetRegistry
from audiomix.clapper import SAMPLES_PER_CELL
from audiomix.film import DialogueLine, FilmProject, FrameMark, Reel
from audiomix.timeline import TimelineEditor

RATE = 16000
CELL = SAMPLES_PER_CELL
OUT = "/tmp/audiomix_syncdemo"


def _pcm(total_cells: int, bangs: dict[int, tuple[int, ...]]) -> np.ndarray:
    n = total_cells * CELL
    x = np.zeros(n, dtype=np.int64)
    for cell, tmpl in bangs.items():
        s = cell * CELL
        x[s:s + len(tmpl)] = tmpl
    return x.astype(np.int16)


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)
    reg = AssetRegistry(os.path.join(OUT, "registry"))
    store = ArchiveStore(os.path.join(OUT, "archive"))

    pat_a = (300, -300, 1000, -1000, 300, -300)
    pat_b = (111, 222, 333, 444, 555, 666)

    # 两卷录音相接：卷1格0..9，卷2格10..21
    reel1_cells, reel2_cells = 10, 12
    total = reel1_cells + reel2_cells
    pcm = _pcm(total, {
        2: pat_a,                       # 卷1 板响在局部格2
        reel1_cells + 2: pat_b,         # 卷2 板响在局部格2（全局格12）
    })

    # 切成 1600 样本块入时间轴，再渲染成“录音原件”（可重开的 16k render）
    ed = TimelineEditor(os.path.join(OUT, "registry"), reg, name="rec")
    block = 1600
    for i, s in enumerate(range(0, len(pcm), block)):
        seg = pcm[s:s + block]
        rec = reg.register(seg, RATE, note=f"seg{i}")
        ed.insert({"clip_id": f"seg{i}", "asset_id": rec.asset_id,
                   "a": 0, "b": len(seg), "start": F(s, RATE)})
    product = ed.publish()
    pcm_by_asset = {a["asset_id"]: reg.load_pcm(a["asset_id"])
                    for a in product["assets"]}
    store.render(product, F(len(pcm), RATE), pcm_by_asset, render_id="rec1")
    store.close()

    # 模板文件
    with open(os.path.join(OUT, "templates.json"), "w", encoding="utf-8") as fh:
        json.dump({"patterns": {"A": list(pat_a), "B": list(pat_b)}}, fh)

    # 胶片工程（两卷）
    reel1 = Reel("一卷", frames=6, tape_cells=reel1_cells, start_candidates=(0, 1))
    reel2 = Reel("二卷", frames=6, tape_cells=reel2_cells, start_candidates=(0, 1))
    project = FilmProject(
        film_id="两卷小片",
        gears=(F(1), F(2)),
        reels=(reel1, reel2),
        frame_marks=(FrameMark("一卷", 1, "A"), FrameMark("二卷", 1, "B")),
        lines=(
            DialogueLine("台词1", cell=3, allowed_lo=2, allowed_hi=2,
                         text="一卷格3的台词->第2帧"),
            DialogueLine("台词2", cell=reel1_cells + 3,
                         allowed_lo=6 + 2, allowed_hi=6 + 2,
                         text="二卷格3的台词->第8帧"),
        ),
        fillable_frames=frozenset({6 + 4}),   # 二卷局部第4帧 = 全局第10帧
        max_missing_in_recording=1,
        max_missing_in_film=1,
    )
    project.save(os.path.join(OUT, "project.json"))

    print("演示输入已生成于", OUT)
    print("  project.json / templates.json / archive/(render rec1) / registry/")


if __name__ == "__main__":
    main()
