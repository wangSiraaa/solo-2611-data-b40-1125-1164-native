"""小整数 PCM 与接头拼接规则。

素材是有明确采样边界的本地短录音，用小整数序列表示。
排期只容许接头重叠固定格数：重叠格的采样逐格相加，禁止重剪——
除重叠相加外不做任何截断、移位或补样。
"""
from __future__ import annotations

from typing import Sequence

# 单声道 PCM：小整数采样的不可变元组，边界即元组长度。
PCM = tuple[int, ...]


class JointError(ValueError):
    """接头不可行（重叠格数超出任一片段边界等）。"""


def as_pcm(values: Sequence[int]) -> PCM:
    return tuple(int(v) for v in values)


def overlap_join(head: PCM, tail: PCM, overlap: int) -> PCM:
    """按接头表把两个片段拼成一段。

    head 末尾 ``overlap`` 格与 tail 开头 ``overlap`` 格逐格相加，
    其余采样原样保留；输出长度恒为 ``len(head) + len(tail) - overlap``。
    不做任何重剪：重叠格数必须落在两个片段的现有边界内。
    """
    if overlap < 1:
        raise JointError("接头至少重叠一格")
    if overlap > len(head) or overlap > len(tail):
        raise JointError(
            f"接头重叠 {overlap} 格超出片段边界 "
            f"(head={len(head)}, tail={len(tail)})，禁止重剪"
        )
    merged: list[int] = list(head[:-overlap])
    for i in range(overlap):
        merged.append(head[len(head) - overlap + i] + tail[i])
    merged.extend(tail[overlap:])
    return tuple(merged)
