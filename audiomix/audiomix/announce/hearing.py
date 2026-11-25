"""严格听辨裁决：核对所有仍可能听出的候选站，决定能否按方向开播。

零误差严格模板是唯一听辨依据。渲染出的 PCM 与每个候选站（本站 +
全部相邻站）的严格模板逐样本比对：

- 一个候选站都对不上：缺录档（渲染本身不是任何合法站名）；
- 只对得上本方向目标站：可签；
- 同时对得上相邻站：会被听混，必须改选整词、另找接头或留待补录。

广播窗按样本数给硬边界，且禁止截字：渲染长度超过窗即拒签。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .lexicon import Release
from .render import Rendering
from .templates import StationTemplate


class Reason:
    OK = "可按方向开播"
    NO_CANDIDATE = "缺录档：渲染不匹配任何候选站严格模板"
    NEIGHBOR_MIXUP = "误听：相邻站会被听混，须改选整词/另找接头/留待补录"
    WINDOW_TOO_SMALL = "广播窗不足且禁止截字"
    UNPUBLISHED = "所选资产未在发版词库中，不得出声"


@dataclass(frozen=True)
class HearingReport:
    """一次听辨的完整证据（候选站核对结果全部留痕）。"""

    target: str
    neighbors: tuple[str, ...]
    window_samples: int
    rendered_samples: int
    station_hits: tuple[str, ...]           # 严格匹配上的全部候选站
    checked: Tuple[Tuple[str, bool], ...]   # 每个候选站的逐样本核对结论
    window_ok: bool
    can_air: bool
    reason: str
    rendered_pcm: tuple[int, ...]


def adjudicate(release: Release, rendering: Rendering, target: str,
               neighbors: list[str], window_samples: int,
               forbid_truncation: bool = True) -> HearingReport:
    """核对所有仍可能听出的候选站并给出可否开播结论。"""
    candidates = [target, *[n for n in neighbors if n != target]]
    templates: list[StationTemplate] = [
        release.station_template(name) for name in candidates
    ]
    hits = tuple(t.station for t in templates if t.admits(rendering.pcm))
    window_ok = rendering.samples() <= window_samples
    if not window_ok and forbid_truncation:
        reason = Reason.WINDOW_TOO_SMALL
        can_air = False
    elif not hits:
        reason = Reason.NO_CANDIDATE
        can_air = False
    elif target not in hits or any(h in neighbors for h in hits):
        reason = Reason.NEIGHBOR_MIXUP
        can_air = False
    else:
        reason = Reason.OK
        can_air = True
    checked = tuple((t.station, t.admits(rendering.pcm)) for t in templates)
    return HearingReport(
        target=target,
        neighbors=tuple(neighbors),
        window_samples=window_samples,
        rendered_samples=rendering.samples(),
        station_hits=hits,
        checked=checked,
        window_ok=window_ok,
        can_air=can_air,
        reason=reason,
        rendered_pcm=rendering.pcm,
    )
