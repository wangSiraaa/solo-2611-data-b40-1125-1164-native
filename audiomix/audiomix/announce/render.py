"""接头表与发版后的真实渲染。

站名出声只有两种来源：

1. 一段整词母录；
2. 两个片段按自制接头表拼接（重叠固定格数，重叠采样相加，禁止重剪）。

整词或拼接片都必须从发布后的 Release 读取 PCM——工作态未发版资产
不得出声，保证“所有人拿同一份词库、所选片都是真实渲染”。
"""
from __future__ import annotations

from dataclasses import dataclass

from .lexicon import Release
from .pcm import PCM, overlap_join


class RenderError(ValueError):
    """渲染前置不满足（未发版资产、接头表无此接头、重剪需求等）。"""


@dataclass(frozen=True)
class Joint:
    """一条自制接头：head 末尾与 tail 开头重叠 overlap 格后相加。"""

    head_id: str
    tail_id: str
    overlap: int


class SpliceTable:
    """乘务台自制的接头表；排期只容许固定一格重叠的接头。"""

    def __init__(self, joints: list[Joint] | None = None,
                 allowed_overlap: int = 1) -> None:
        self.allowed_overlap = allowed_overlap
        self._joints: dict[tuple[str, str], Joint] = {}
        for joint in joints or []:
            self.add(joint)

    def add(self, joint: Joint) -> None:
        if joint.overlap != self.allowed_overlap:
            raise RenderError(
                f"排期只容许接头重叠 {self.allowed_overlap} 格，"
                f"收到 {joint.overlap} 格"
            )
        self._joints[(joint.head_id, joint.tail_id)] = joint

    def get(self, head_id: str, tail_id: str) -> Joint:
        try:
            return self._joints[(head_id, tail_id)]
        except KeyError as exc:
            raise RenderError(
                f"接头表没有 {head_id} -> {tail_id} 的接头，禁止重剪"
            ) from exc

    def entries(self) -> tuple[Joint, ...]:
        return tuple(self._joints.values())


@dataclass(frozen=True)
class WholeWordPlan:
    """整词出声计划。"""

    asset_id: str

    def asset_refs(self) -> tuple[str, ...]:
        return (self.asset_id,)


@dataclass(frozen=True)
class SplicePlan:
    """两片段拼接出声计划。"""

    head_id: str
    tail_id: str
    overlap: int

    def asset_refs(self) -> tuple[str, ...]:
        return (self.head_id, self.tail_id)


@dataclass(frozen=True)
class Rendering:
    """一次从真实发版读出的渲染结果及其证据链。"""

    release_version: str
    plan_kind: str
    asset_refs: tuple[str, ...]
    source_pcms: tuple[PCM, ...]
    pcm: PCM
    overlap: int

    def samples(self) -> int:
        return len(self.pcm)


def render_plan(release: Release, plan: WholeWordPlan | SplicePlan,
                splice_table: SpliceTable) -> Rendering:
    """从发布后的真实资产渲染；任何前置缺失都直接拒绝，不做重剪。"""
    if isinstance(plan, WholeWordPlan):
        pcm = release.pcm_of(plan.asset_id)
        return Rendering(
            release_version=release.version,
            plan_kind="whole_word",
            asset_refs=(plan.asset_id,),
            source_pcms=(pcm,),
            pcm=pcm,
            overlap=0,
        )
    joint = splice_table.get(plan.head_id, plan.tail_id)
    head_pcm = release.pcm_of(plan.head_id)
    tail_pcm = release.pcm_of(plan.tail_id)
    pcm = overlap_join(head_pcm, tail_pcm, joint.overlap)
    return Rendering(
        release_version=release.version,
        plan_kind="splice",
        asset_refs=(plan.head_id, plan.tail_id),
        source_pcms=(head_pcm, tail_pcm),
        pcm=pcm,
        overlap=joint.overlap,
    )
