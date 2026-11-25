"""气象传真声带的时间轴编辑：裁切、变速、混入重录的一行。

三类编辑都不直接改 PCM 字节，而是走仓库既有的时间轴设施：
:class:`~audiomix.timeline.TimelineEditor` 记录带完整转换参数的 clip，
:class:`~audiomix.archive.ArchiveStore` 逐块渲染、内容寻址并保留每块的
clip/asset 来源。本模块只负责传真几何（行/槽边界对齐）与编辑后的卷结构，
渲染出的新声带再由 :mod:`audiomix.fax` 重读解图。

样本出处链：原始卷 WAV 登记为素材 -> 时间轴 clip 引用其 asset_id 与源区间 ->
数据仓块记录 clip_id/asset_id/摘要 -> 重读结果绑定 render_id 与块追溯。
变速会改变输出时长，渲染后按传真槽几何线性重采样回标准槽计时，再落脉冲判读。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any
import os
import shutil

import numpy as np

from .archive import ArchiveStore
from .assets import AssetRegistry
from .engine import OUTPUT_RATE
from .fax import (
    HIGH, LOW, Reel, Segment, SYNC_GAP_SAMPLES,
    _row_stride, publish_reel, read_reel_waveform,
)
from .timeline import TimelineEditor


class FaxEditError(ValueError):
    pass


# ----------------------------- 几何与素材 ----------------------------- #
def _segment_sample_span(seg: Segment) -> int:
    return (len(seg.rows) + 1) * _row_stride(seg.row_len) + SYNC_GAP_SAMPLES


def register_reel_asset(registry: AssetRegistry, reel: Reel,
                        wav_path: str | None = None) -> str:
    """把一卷传真声带登记进素材仓，返回 asset_id（同字节幂等）。"""
    from .assets import read_wav
    if wav_path is None:
        from .fax import reel_pcm
        pcm = reel_pcm(reel)
    else:
        pcm, _ = read_wav(wav_path)
    rec = registry.register(pcm, OUTPUT_RATE, note=f"fax-reel:{reel.reel_id}")
    return rec.asset_id


def _new_editor(archive: ArchiveStore, registry: AssetRegistry,
                render_id: str) -> TimelineEditor:
    """每次编辑用 render_id 独立的时间轴工作目录，保证复跑互不污染。"""
    tl_root = os.path.join(archive.root, "fax_tl", render_id)
    shutil.rmtree(tl_root, ignore_errors=True)
    return TimelineEditor(tl_root, registry, name="fax")


def _render_timeline(editor: TimelineEditor, archive: ArchiveStore,
                     render_id: str, T: Fraction) -> dict[str, Any]:
    product = editor.publish()
    pcm_by_asset = {aid: editor.registry.load_pcm(aid)
                    for aid in {c.asset_id for c in editor.clips.values()}}
    return archive.render(product, T, pcm_by_asset, render_id=render_id)


# ----------------------------- 编辑结果 ----------------------------- #
@dataclass
class FaxEditResult:
    """一次编辑的完整凭据：新卷、发布路径、render_id 与来源说明。"""
    reel: Reel
    wav_path: str
    render_id: str
    product_id: str
    provenance: list[dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        return {
            "reel_id": self.reel.reel_id,
            "wav_path": self.wav_path,
            "render_id": self.render_id,
            "product_id": self.product_id,
            "provenance": self.provenance,
        }


def _finish(editor: TimelineEditor, archive: ArchiveStore, new_reel: Reel,
            out_wav: str, render_id: str, T: Fraction,
            provenance: list[dict[str, Any]]) -> FaxEditResult:
    info = _render_timeline(editor, archive, render_id, T)
    pcm = archive.full_pcm(render_id)
    # 渲染声带按标准传真几何落盘并发布（元数据保留新卷结构与出处）
    from .assets import write_wav
    write_wav(out_wav, pcm, OUTPUT_RATE)
    pub = publish_reel(new_reel, out_wav)
    product = editor.publish()
    provenance.append({"step": "render", "render_id": render_id,
                       "pcm_digest": pub["pcm_digest"],
                       "blocks_rendered": info["blocks_rendered"],
                       "blocks_reused": info["blocks_reused"]})
    return FaxEditResult(new_reel, out_wav, render_id,
                         product["product_id"], provenance)


# ----------------------------- 三类编辑 ----------------------------- #
def cut_reel(reel: Reel, asset_id: str, registry: AssetRegistry,
             archive: ArchiveStore, drop_rows: int, out_wav: str,
             render_id: str, note: str = "") -> FaxEditResult:
    """时间轴裁切：从声带开头整行移除 ``drop_rows`` 个数据行（含其行步长）。

    只允许按行步长整行裁切，保证剩余脉冲与槽计时不被破坏；移除的行从卷结构中
    同步删除，保留卷号并在出处里记录被裁行与源样本区间。
    """
    if not reel.segments:
        raise FaxEditError("空卷无法裁切")
    seg = reel.segments[0]
    if not (0 <= drop_rows <= len(seg.rows)):
        raise FaxEditError("裁行数越界")
    if len(reel.segments) != 1:
        raise FaxEditError("整行裁切目前只受理单段卷（多段请分段受理）")
    stride = _row_stride(seg.row_len)
    rec = registry.get(asset_id)
    # 源样本：跳过校准行（1 个步长）+ drop_rows 个数据行步长
    a = stride + drop_rows * stride
    b = _segment_sample_span(seg) - SYNC_GAP_SAMPLES
    if not (0 <= a < b <= rec.sample_count):
        raise FaxEditError(f"裁切源区间 [{a},{b}) 越界（素材 {rec.sample_count} 样本）")

    editor = _new_editor(archive, registry, render_id)
    editor.insert({"clip_id": "kept", "asset_id": asset_id, "a": a, "b": b,
                   "start": "0", "speed": "1", "gain": "1"})
    kept_rows = seg.rows[drop_rows:]
    new_seg = Segment(seg_id=seg.seg_id, rows=kept_rows, phase=seg.phase,
                      calibration=list(seg.calibration),
                      calibration_corrupted=set(seg.calibration_corrupted))
    new_reel = Reel(reel.reel_id, [new_seg], note=reel.note or note)
    T = Fraction(b - a, OUTPUT_RATE)
    provenance = [{"step": "cut", "source_asset": asset_id,
                   "source_sample_range": [a, b], "dropped_rows": drop_rows,
                   "reel_id": reel.reel_id}]
    return _finish(editor, archive, new_reel, out_wav, render_id, T, provenance)


def speed_reel(reel: Reel, asset_id: str, registry: AssetRegistry,
               archive: ArchiveStore, speed: int, out_wav: str,
               render_id: str, note: str = "") -> FaxEditResult:
    """变速：整数倍速重放整条声带（内容不变、时长缩为 1/speed）。

    速度走时间轴 clip 的精确有理 ``speed`` 参数；渲染后按传真槽几何重采样回
    标准槽计时，使后续脉冲判读仍落在固定槽宽上。卷结构（格值、相位）不变，
    出处记录速度参数与重采样关系。
    """
    if speed <= 0:
        raise FaxEditError("速度必须为正整数")
    seg = reel.segments[0]
    if len(reel.segments) != 1:
        raise FaxEditError("变速目前只受理单段卷")
    rec = registry.get(asset_id)
    b = _segment_sample_span(seg) - SYNC_GAP_SAMPLES
    editor = _new_editor(archive, registry, render_id)
    editor.insert({"clip_id": "sped", "asset_id": asset_id, "a": 0, "b": b,
                   "start": "0", "speed": str(speed), "gain": "1"})
    T = Fraction(b, OUTPUT_RATE * speed)
    provenance = [{"step": "speed", "source_asset": asset_id, "speed": speed,
                   "source_sample_range": [0, b], "reel_id": reel.reel_id}]
    result = _finish(editor, archive, reel, out_wav, render_id, T, provenance)
    # 重采样回标准槽计时，覆盖落盘 WAV 并重新发布（出处补充重采样一步）
    _resample_to_fax_grid(result, archive, speed, registry)
    return result


def _resample_to_fax_grid(result: FaxEditResult, archive: ArchiveStore,
                          speed: int, registry: AssetRegistry) -> None:
    """把变速渲染声带按 1/speed 线性重采样回标准 16kHz 传真槽几何。"""
    pcm = archive.read_samples(result.render_id, 0,
                               archive.render_meta(result.render_id)["n_samples"])
    if len(pcm) < 2:
        raise FaxEditError("变速渲染过短，无法重采样")
    # 目标长度 = 源长度 / speed（线性插值，末点保持）
    target_n = (len(pcm) + speed - 1) // speed
    src_idx = np.arange(target_n, dtype=np.float64) * speed
    i0 = np.floor(src_idx).astype(np.int64)
    frac = src_idx - i0
    i1 = np.minimum(i0 + 1, len(pcm) - 1)
    out = np.rint(pcm[i0] * (1 - frac) + pcm[i1] * frac).astype(np.int16)
    from .assets import write_wav
    write_wav(result.wav_path, out, OUTPUT_RATE)
    pub = publish_reel(result.reel, result.wav_path)
    result.provenance.append({"step": "resample", "factor": f"1/{speed}",
                              "samples_in": len(pcm), "samples_out": target_n,
                              "pcm_digest": pub["pcm_digest"]})


def mix_rerecorded_line(reel: Reel, asset_id: str, registry: AssetRegistry,
                        archive: ArchiveStore, row_index: int,
                        new_line: list[str], out_wav: str, render_id: str,
                        note: str = "") -> FaxEditResult:
    """混入重录的一行：用单独录制的新行素材，在指定数据行位置与原声叠混。

    新行是一条独立登记的传真行素材（同步间隔 + row_len 槽），与原声带在同一
    输出时刻重叠相加；重录行的格值替换卷结构中对应行，出处同时保留原 asset 与
    重录 asset，数据仓块的 clip/asset 索引能逐块看到两条来源。
    """
    seg = reel.segments[0]
    if len(reel.segments) != 1:
        raise FaxEditError("混行目前只受理单段卷")
    if not (0 <= row_index < len(seg.rows)):
        raise FaxEditError("重录行号越界")
    if len(new_line) != seg.row_len or any(s not in (HIGH, LOW) for s in new_line):
        raise FaxEditError("重录行必须是等长 H/L 行")
    stride = _row_stride(seg.row_len)
    b = _segment_sample_span(seg) - SYNC_GAP_SAMPLES
    rec = registry.get(asset_id)

    # 合成并登记重录行素材（与数据行同样的几何：同步间隔 + row_len 槽）
    from .fax import _pulse
    chunks = [np.zeros(SYNC_GAP_SAMPLES, dtype=np.int16)]
    for sym in new_line:
        chunks.append(_pulse(sym))
    line_pcm = np.concatenate(chunks).astype(np.int16)
    line_rec = registry.register(line_pcm, OUTPUT_RATE,
                                 note=f"rerecord-line:{reel.reel_id}:{row_index}")

    editor = _new_editor(archive, registry, render_id)
    editor.insert([
        {"clip_id": "orig", "asset_id": asset_id, "a": 0, "b": b,
         "start": "0", "speed": "1", "gain": "1"},
        {"clip_id": "rerecord", "asset_id": line_rec.asset_id,
         "a": 0, "b": len(line_pcm),
         "start": Fraction(stride + row_index * stride, OUTPUT_RATE),
         "speed": "1", "gain": "1"},
    ])
    new_rows = [list(r) for r in seg.rows]
    new_rows[row_index] = list(new_line)
    new_seg = Segment(seg_id=seg.seg_id, rows=new_rows, phase=seg.phase,
                      calibration=list(seg.calibration),
                      calibration_corrupted=set(seg.calibration_corrupted))
    new_reel = Reel(reel.reel_id, [new_seg], note=reel.note or note)
    T = Fraction(b, OUTPUT_RATE)
    provenance = [
        {"step": "mix", "source_asset": asset_id,
         "rerecord_asset": line_rec.asset_id, "row_index": row_index,
         "reel_id": reel.reel_id},
    ]
    return _finish(editor, archive, new_reel, out_wav, render_id, T, provenance)


# ----------------------------- 重读与追溯 ----------------------------- #
def reread_published(wav_path: str, archive: ArchiveStore,
                     render_id: str) -> dict[str, Any]:
    """重读编辑后发布的声带，绑定 render_id 与逐块样本出处。"""
    from .fax import load_published_reel
    reel, pcm, meta = load_published_reel(wav_path)
    reads = read_reel_waveform(pcm, meta["reel"])
    trace = archive.read_trace(render_id, 0,
                               archive.render_meta(render_id)["n_samples"])
    return {
        "reel_id": reel.reel_id,
        "render_id": render_id,
        "segment_reads": [r.to_json() for r in reads],
        "block_trace": [
            {"block_index": t["block_index"], "clip_ids": t["clip_ids"],
             "asset_ids": t["asset_ids"], "block_key": t["block_key"]}
            for t in trace
        ],
    }
