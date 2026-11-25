"""馆员制版入口（IntakeDesk）：受理已渲染卷号、裁框与本场用签许可，按实读格值开馆。

一个入口同时受理：
- 一卷完整气象传真（已发布渲染声带）；
- 馆员另发的一卷短传真、换的一张裁边单——都从同一入口受理，按展次（exhibition）
  查到观众站在开放展板前真能看见的签，以及每枚签的原卷依据。

开馆只依据**实际重读声带得到的格值**（全部仍可能读法），不依据发射端自报相位。
开馆单列出：能按合同如期开的展、观众从哪个格读到什么、每枚可见签的读法依据。
旧目录按原展次永久可查（展品与签的后续移动不改写历史展次记录）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any

from .board import (
    Crop, Marker, crop_grid, crop_markers, reliable_closed_labels,
)
from .fax import (
    cell_possible_values, load_published_reel, possible_grids,
    read_reel_waveform,
)


class IntakeError(ValueError):
    pass


@dataclass
class PlateOrder:
    """一次受理请求：已渲染卷号、裁框、具名标记、本场用签许可。"""
    order_id: str
    reel_wav: str                       # 已发布渲染声带路径
    crop: Crop = field(default_factory=Crop)
    markers: list[Marker] = field(default_factory=list)
    labels_permitted: bool = True       # 本场用签许可
    exhibition: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id, "reel_wav": self.reel_wav,
            "crop": self.crop.to_json(),
            "markers": [m.to_json() for m in self.markers],
            "labels_permitted": self.labels_permitted, "exhibition": self.exhibition,
        }


@dataclass
class OpeningSheet:
    """开馆单：按实读格值办理，记录观众可见信息与全部依据。"""
    order_id: str
    exhibition: str
    reel_id: str
    render_digest: str
    crop: dict[str, int]
    dimensions: list[int]
    cell_readings: list[list[list[str]]]          # 每格观众能读到的值集合
    possible_grids: list[list[list[str]]]
    labels: list[dict[str, Any]]
    labels_visible: list[dict[str, Any]]
    can_open_on_time: bool
    reason: str
    provenance: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class IntakeDesk:
    """制版入口：持久化受理记录与按展次可查的旧目录。"""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.catalog_path = os.path.join(root, "catalog.json")
        self.orders: dict[str, OpeningSheet] = {}
        if os.path.exists(self.catalog_path):
            for item in json.load(open(self.catalog_path, encoding="utf-8")):
                self.orders[item["order_id"]] = _sheet_from_json(item)

    # ----------------------------- 受理 ----------------------------- #
    def accept(self, order: PlateOrder) -> OpeningSheet:
        """根据实际读到的格值办理开馆（短传真/换裁边单走同一入口）。"""
        if order.order_id in self.orders:
            raise IntakeError(f"受理号已存在: {order.order_id}")
        reel, pcm, meta = load_published_reel(order.reel_wav)
        reads = read_reel_waveform(pcm, meta["reel"])
        grids_raw = possible_grids(reads)
        if not grids_raw:
            raise IntakeError("声带不存在任何相容读法（校准条排除了全部相位）")

        # 裁框对每种仍可能读法一视同仁地裁切
        try:
            grids = [crop_grid(g, order.crop) for g in grids_raw]
        except ValueError as exc:
            raise IntakeError(str(exc)) from exc
        markers = crop_markers(order.markers, order.crop,
                               len(grids_raw[0]), len(grids_raw[0][0]))
        margin = cell_possible_values(grids)

        labels = reliable_closed_labels(grids, markers)
        if order.labels_permitted:
            visible = labels
        else:
            visible = []
        reason = ("具备可靠闭合低雨区签" if labels else "无在全部读法下都闭合的低值区")
        if not order.labels_permitted:
            reason += "；本场不许可用签，观众不见签"
        sheet = OpeningSheet(
            order_id=order.order_id,
            exhibition=order.exhibition or order.order_id,
            reel_id=reel.reel_id,
            render_digest=meta["pcm_digest"],
            crop=order.crop.to_json(),
            dimensions=[len(grids[0]), len(grids[0][0])],
            cell_readings=[[sorted(s) for s in row] for row in margin],
            possible_grids=grids,
            labels=[l.to_json() for l in labels],
            labels_visible=[l.to_json() for l in visible],
            can_open_on_time=True,
            reason=reason,
            provenance={
                "reel_wav": order.reel_wav,
                "pcm_digest": meta["pcm_digest"],
                "segment_reads": [r.to_json() for r in reads],
                "n_possible_readings": len(grids),
                "markers": [m.to_json() for m in markers],
            },
        )
        self.orders[order.order_id] = sheet
        self._flush()
        return sheet

    # ----------------------------- 查询 ----------------------------- #
    def by_exhibition(self, exhibition: str) -> list[OpeningSheet]:
        """按展次查回观众可见的签与原卷依据（旧目录永久可查）。"""
        return [s for s in self.orders.values() if s.exhibition == exhibition]

    def get(self, order_id: str) -> OpeningSheet:
        if order_id not in self.orders:
            raise KeyError(f"旧目录中无此受理号: {order_id}")
        return self.orders[order_id]

    def all_sheets(self) -> list[OpeningSheet]:
        return list(self.orders.values())

    def _flush(self) -> None:
        tmp = self.catalog_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([s.to_json() for s in self.orders.values()], fh,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, self.catalog_path)


def _sheet_from_json(obj: dict[str, Any]) -> OpeningSheet:
    return OpeningSheet(**obj)
