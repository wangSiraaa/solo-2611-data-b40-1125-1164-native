"""工单：安装事件、梁的用材性质、榫孔先后、封榫记录与许可。

fixtures_bridge/workorder.json 结构：
    events   安装事件按先后排列，如 ["初建", "重装"]（年份未知，待求解）
    beams    每根梁：material = 现砍现装 | 库存旧材；
             events  = 该梁可能归属的安装事件（现梁可能为原物也可能为重装补配，
                       需靠封榫等记录收敛）
    joints   榫孔：mortise_beam（被切穿的卯方）必须先于 tenon_beam（榫方）就位；
             covers = 该节点盖住了哪根梁的哪些环位（被榫盖住的外圈不得引用）；
             sealed_after = 该节点自哪次事件后封存（封存后榫方梁未再拆装，
                             其归属事件不晚于该次）
    permits  view   = 各节点被盖环位的查看权（true 才可引用案卷中的被盖读数）；
             sample = 微芯取样许可（全桥只许可开一处，取一枚跨外圈的微芯）

约束语义：
- 现砍现装：安装年 == 砍伐年；库存旧材：安装年 >= 砍伐年（旧材只给下界，
  不能把安装提前到末轮年）。
- 带皮末轮在且可引用：砍伐年 == 末轮候选年之一（精确）；缺最外圈（或被盖
  且无权引用）：只能给出砍伐年下界（末轮年之后）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import PlaqueError

FRESH = "现砍现装"
STOCK = "库存旧材"


@dataclass(frozen=True)
class Joint:
    id: str
    mortise_beam: str            # 卯方（被切穿，先就位）
    tenon_beam: str              # 榫方（切穿对方，后就位）
    covers: dict[str, list[int]] # 梁号 -> 被盖住的梁内环位
    sealed_after: str            # 自该事件后封存


@dataclass(frozen=True)
class BeamOrder:
    material: str                # FRESH | STOCK
    events: tuple[str, ...]      # 该梁可能归属的安装事件


@dataclass
class WorkOrder:
    events: list[str]
    beams: dict[str, BeamOrder]
    joints: list[Joint] = field(default_factory=list)
    view_permits: dict[str, bool] = field(default_factory=dict)
    sample_permits: dict[str, bool] = field(default_factory=dict)

    def event_index(self, event: str) -> int:
        if event not in self.events:
            raise PlaqueError(f"未知安装事件: {event}")
        return self.events.index(event)

    def covered_positions(self, beam_id: str) -> dict[str, set[int]]:
        """该梁被各节点盖住的环位：{节点id: 环位集合}。"""
        out: dict[str, set[int]] = {}
        for joint in self.joints:
            positions = joint.covers.get(beam_id)
            if positions:
                out[joint.id] = set(positions)
        return out

    def allowed_events(self, beam_id: str) -> list[str]:
        """该梁在封榫记录收敛后的候选安装事件。

        榫方梁的归属事件不晚于其所在节点的封存事件；其余梁按工单原样。
        """
        order = self.beams.get(beam_id)
        if order is None:
            raise PlaqueError(f"工单中没有梁 {beam_id} 的安装记录")
        candidates = list(order.events)
        for joint in self.joints:
            if joint.tenon_beam != beam_id:
                continue
            limit = self.event_index(joint.sealed_after)
            candidates = [e for e in candidates if self.event_index(e) <= limit]
        if not candidates:
            raise PlaqueError(
                f"梁 {beam_id} 的候选安装事件被封榫记录全部排除，工单自相矛盾"
            )
        return candidates


def load_workorder(path: str) -> WorkOrder:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    events = raw.get("events")
    if not isinstance(events, list) or len(events) < 1 or not all(isinstance(e, str) for e in events):
        raise PlaqueError("工单 events 必须是非空字符串列表（按安装先后排列）")

    beams: dict[str, BeamOrder] = {}
    for beam_id, spec in raw.get("beams", {}).items():
        material = spec.get("material")
        if material not in (FRESH, STOCK):
            raise PlaqueError(f"梁 {beam_id} 的 material 必须是 {FRESH}/{STOCK}")
        evs = spec.get("events")
        if not isinstance(evs, list) or not evs or any(e not in events for e in evs):
            raise PlaqueError(f"梁 {beam_id} 的 events 必须取自工单事件列表")
        beams[beam_id] = BeamOrder(material=material, events=tuple(evs))

    joints: list[Joint] = []
    for j in raw.get("joints", []):
        for key in ("id", "mortise_beam", "tenon_beam", "sealed_after"):
            if key not in j:
                raise PlaqueError(f"榫孔记录缺字段 {key}: {j!r}")
        if j["sealed_after"] not in events:
            raise PlaqueError(f"榫孔 {j['id']} 的 sealed_after 不是已知事件")
        covers = {b: [int(p) for p in ps] for b, ps in j.get("covers", {}).items()}
        joints.append(Joint(id=j["id"], mortise_beam=j["mortise_beam"],
                            tenon_beam=j["tenon_beam"], covers=covers,
                            sealed_after=j["sealed_after"]))

    permits = raw.get("permits", {})
    return WorkOrder(
        events=list(events),
        beams=beams,
        joints=joints,
        view_permits={k: bool(v) for k, v in permits.get("view", {}).items()},
        sample_permits={k: bool(v) for k, v in permits.get("sample", {}).items()},
    )
