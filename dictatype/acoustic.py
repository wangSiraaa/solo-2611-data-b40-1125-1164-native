"""声学侧：击序列 -> audiomix 时间轴发布产品 -> 实际混音数据仓逐样本 PCM。

本模块不自行实现任何混音语义，而是把每个键的短声样登记为 audiomix 素材，
按拍点把声样作为 clip 插入时间轴（同一拍点多击即多 clip 重叠），发布不可变
产品后交给 ``audiomix.archive.ArchiveStore`` 渲染。因此重叠落锤严格按仓库
实际 PCM 处理：全部有效 clip 的浮点贡献先求和，再做唯一一次恰半取偶舍入，
最后削顶到 int16——“一声不一定意味着一次键入”。
"""
from __future__ import annotations

import os
from fractions import Fraction
from typing import Any, Sequence

import numpy as np

from audiomix.assets import AssetRegistry
from audiomix.archive import ArchiveStore
from audiomix.engine import OUTPUT_RATE, render_reference
from audiomix.timeline import TimelineEditor

from .model import Key, Strike

# 拍点间隔：每拍 4 个 16kHz 样本（1/4000 秒）。单样本声样在相邻拍点上互不
# 重叠；同拍多击的声样从同一样本号开始，必然逐样本重叠混音。
BEAT_PERIOD_SECONDS = Fraction(1, 4000)
BEAT_PERIOD_SAMPLES = 4


def mix_strikes_pcm(keys: Sequence[Key], strikes: Sequence[Strike]) -> np.ndarray:
    """纯 Python/NumPy 的快速精确混音，供听打台 DFS 逐前缀核对。

    与 audiomix 仓库渲染严格同语义：每个声样按拍点起点放到输出样本号上，
    全部有效贡献先逐样本求和（float64），再做唯一一次恰半取偶（np.rint），
    最后削顶到 int16。声样等长且同拍对齐时本函数与仓库逐样本全等；听打台
    在接受每个完整解时仍会走一次 audiomix 块渲染 + 参考解释器做独立复核，
    因此快速路径的任何偏差都会被测试与终判捕获。
    """
    if not strikes:
        raise ValueError("空击序列无法混音")
    key_map = {k.key_id: k for k in keys}
    last_beat = max(st.beat for st in strikes)
    longest = max(len(key_map[st.key_id].sample) for st in strikes
                  if st.beat == last_beat)
    n = last_beat * BEAT_PERIOD_SAMPLES + longest
    acc = np.zeros(n, dtype=np.float64)
    for st in strikes:
        key = key_map[st.key_id]
        start = st.beat * BEAT_PERIOD_SAMPLES
        end = start + len(key.sample)
        acc[start:end] += np.asarray(key.sample, dtype=np.float64)
    rounded = np.rint(acc)                              # 恰半取偶，全片只舍入一次
    return np.clip(rounded, -32768, 32767).astype(np.int16)


def beat_start(beat: int) -> Fraction:
    """拍点 beat 的输出起始时刻（秒，精确有理数）。"""
    return BEAT_PERIOD_SECONDS * int(beat)


class AcousticSession:
    """一次听打会话的声学工作台：素材登记 + 时间轴 + 实际混音数据仓。

    听打台对候选逐击记录做声学核对时，与档案渲染走完全相同的 audiomix
    发布产品与 ArchiveStore 块渲染路径，不另写一套求和/舍入/削顶。
    """

    def __init__(self, root: str, keys: Sequence[Key]):
        if len(keys) > 64:
            raise ValueError("键位数量过多")
        self.root = root
        self.registry_root = os.path.join(root, "registry")
        self.archive_root = os.path.join(root, "archive")
        os.makedirs(root, exist_ok=True)
        self.registry = AssetRegistry(self.registry_root)
        self.keys: dict[str, Key] = {}
        self.asset_of: dict[str, str] = {}
        for key in keys:
            if key.key_id in self.keys:
                raise ValueError(f"重复 key_id: {key.key_id}")
            self.keys[key.key_id] = key
            pcm = np.asarray(key.sample, dtype=np.int16)
            rec = self.registry.register(pcm, OUTPUT_RATE, note=f"key:{key.key_id}")
            self.asset_of[key.key_id] = rec.asset_id
        self.store = ArchiveStore(self.archive_root)
        self._render_seq = 0
        self._product_seq = 0

    def close(self) -> None:
        self.store.close()

    # ----------------------------- 发布 ----------------------------- #
    def build_product(self, strikes: Sequence[Strike], name: str | None = None) -> dict[str, Any]:
        """把逐击记录发布成 audiomix 不可变时间轴产品。

        每击对应一个 clip：源区间为整段键声样，起点为该拍拍点时刻；
        同拍多击产生重叠 clip，重叠语义完全由 audiomix 决定。
        每次构造使用独立的编辑器名字（独立持久化状态），候选之间互不污染。
        """
        self._product_seq += 1
        editor = TimelineEditor(self.registry_root, self.registry,
                                name=name or f"acoustic_{id(self)}_{self._product_seq}")
        # 编辑器是持久化对象；全新名字保证不同候选互不污染
        for st in strikes:
            if st.key_id not in self.keys:
                raise KeyError(f"未知键: {st.key_id}")
            key = self.keys[st.key_id]
            editor.insert({
                "clip_id": f"s{st.seq:02d}_b{st.beat}_{st.key_id}",
                "asset_id": self.asset_of[st.key_id],
                "a": 0,
                "b": len(key.sample),
                "start": beat_start(st.beat),
            })
        return editor.publish()

    def total_duration(self, strikes: Sequence[Strike]) -> Fraction:
        """覆盖到最后一个声样末尾的总时长 T（左闭右开，取整到拍点格）。"""
        if not strikes:
            raise ValueError("空击序列无法渲染")
        last_beat = max(st.beat for st in strikes)
        longest = max(len(self.keys[st.key_id].sample) for st in strikes
                      if st.beat == last_beat)
        n_samples = last_beat * BEAT_PERIOD_SAMPLES + longest
        return Fraction(n_samples, OUTPUT_RATE)

    def render(self, strikes: Sequence[Strike], render_id: str | None = None,
               use_reference: bool = False) -> dict[str, Any]:
        """渲染逐击记录的实际 PCM。

        返回 {"render_id", "pcm", "product", "T", "info"}。
        默认走 ArchiveStore 的分块向量化渲染（仓库实际路径，含内容寻址复用）；
        ``use_reference=True`` 改走独立逐样本 Fraction 解释器，用于交叉核对
        两条路径逐样本一致。
        """
        product = self.build_product(strikes)
        T = self.total_duration(strikes)
        pcm_by_asset = {aid: self.registry.load_pcm(aid) for aid in self.asset_of.values()}
        if use_reference:
            pcm = render_reference(product, pcm_by_asset, T)
            rid = render_id or f"ref-{self._next_seq()}"
            return {"render_id": rid, "pcm": pcm, "product": product,
                    "T": T, "info": {"path": "reference"}}
        rid = render_id or f"ac-{self._next_seq()}"
        info = self.store.render(product, T, pcm_by_asset, render_id=rid)
        pcm = self.store.full_pcm(rid)
        return {"render_id": rid, "pcm": pcm, "product": product,
                "T": T, "info": info}

    def _next_seq(self) -> int:
        self._render_seq += 1
        return self._render_seq

    def matches_evidence(self, strikes: Sequence[Strike],
                         evidence_pcm: np.ndarray) -> dict[str, Any]:
        """候选击序列渲染出的 PCM 是否与实录逐样本相同。

        必须长度与每个样本都相等；同时用参考解释器独立复核一次，
        证明“仓库块渲染 == 逐样本解释器 == 实录”。
        """
        rendered = self.render(strikes)
        ref = self.render(strikes, use_reference=True)["pcm"]
        got = rendered["pcm"]
        if not np.array_equal(got, ref):
            raise RuntimeError("仓库渲染与逐样本解释器不一致（混音语义分歧）")
        ev = np.asarray(evidence_pcm, dtype=np.int16)
        equal = got.shape == ev.shape and bool(np.array_equal(got, ev))
        return {"acoustic_match": equal,
                "render_id": rendered["render_id"],
                "expected_samples": got.tolist(),
                "evidence_samples": ev.tolist()}


# ----------------------------- 函数式便捷入口 ----------------------------- #

def strikes_to_product(keys: Sequence[Key], strikes: Sequence[Strike],
                       root: str, name: str = "oneoff") -> dict[str, Any]:
    """一次性构造声学工作台并发布产品（供测试/演示直接调用）。"""
    sess = AcousticSession(root, keys)
    try:
        return sess.build_product(strikes, name=name)
    finally:
        sess.close()


def render_strikes(keys: Sequence[Key], strikes: Sequence[Strike],
                   root: str, render_id: str | None = None) -> dict[str, Any]:
    """一次性渲染击序列，返回含 PCM 的结果字典。"""
    sess = AcousticSession(root, keys)
    try:
        return sess.render(strikes, render_id=render_id)
    finally:
        sess.close()
