"""把布展 YAML 与两份归一任务产物装成可判定的展位对象。

布展 YAML 只引用归一任务里的牌/底座 id；真正的刻样与朝向规格仍以归档版本的
归一结果为准，布展层不复制任何领域数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..codec import to_plain
from ..errors import TaskRejected
from ..yaml_parser import parse_text
from ..materialize import materialize_all
from .domain import AnchorEndPolicy, MarkerPolicy, NoMarkerPolicy, read_sign
from .fabricator import FabricationOrder, fabricate
from .plates import BaseBundle, PlateBundle
from .placement import BaseInventory, Booth, Entrance


@dataclass
class ExhibitionInput:
    name: str
    max_cells: int
    reengrave_allowed: bool
    marker_policy: MarkerPolicy
    inventory: BaseInventory
    booths: list[Booth]
    raw: dict


def _marker_policy(spec: dict | None) -> MarkerPolicy:
    if not spec:
        return NoMarkerPolicy()
    name = spec.get("policy", "none")
    if name == "none":
        return NoMarkerPolicy()
    if name == "anchor-end":
        return AnchorEndPolicy(anchor_end=spec.get("anchor_end", "R"))
    raise TaskRejected(f"未知方向标记策略 {name!r}", reason="unknown-marker-policy")


def load_exhibition(
    yaml_path: str | Path,
    plate_bundle: PlateBundle,
    base_bundle: BaseBundle,
) -> ExhibitionInput:
    text = Path(yaml_path).read_text(encoding="utf-8")
    parsed = parse_text(text)
    docs = materialize_all(parsed)
    plain = to_plain(docs[0].root)
    if "exhibition" not in plain or not isinstance(plain["exhibition"], dict):
        raise TaskRejected("布展 YAML 顶层需要 exhibition 映射", reason="bad-exhibition")
    ex = plain["exhibition"]

    max_cells = int(ex.get("max_cells", 4))
    if not 1 <= max_cells <= 4:
        raise TaskRejected("每边至多四格", reason="bad-max-cells")
    for plate in plate_bundle.plates:
        if plate.n > max_cells:
            raise TaskRejected(
                f"牌 {plate.plate_id} 为 {plate.n} 格，超过展方最大格数 {max_cells}",
                reason="cell-limit",
            )

    inventory_ids = list(ex.get("inventory", []))
    for bid in inventory_ids:
        base_bundle.get(bid)
    # 库存数量：inventory 只给型号时默认各 1 件；可用 inventory_counts 显式给数
    raw_counts = ex.get("inventory_counts") or {}
    counts = {bid: int(raw_counts.get(bid, 1)) for bid in inventory_ids}
    inventory = BaseInventory.from_specs([base_bundle.get(bid) for bid in inventory_ids], counts)

    marker_policy = _marker_policy(ex.get("marker"))
    orders: list[FabricationOrder] = []
    booth_specs = ex.get("booths", [])
    seen_sign: set[str] = set()
    for booth in booth_specs:
        for ent in booth.get("entrances", []):
            sign_id = ent["entrance_id"]
            if sign_id in seen_sign:
                raise TaskRejected(f"入口 id 重复 {sign_id}", reason="duplicate-entrance")
            seen_sign.add(sign_id)
            # 提前校验 id 存在（缺失由 bundle 抛 KeyError）
            plate_bundle.get(ent["plate_id"])
            base_bundle.get(ent["base_id"])
            orders.append(FabricationOrder(sign_id, ent["plate_id"], ent["base_id"]))

    signs = fabricate(orders, plate_bundle, base_bundle, marker_policy)

    booths: list[Booth] = []
    for booth in booth_specs:
        entrances = []
        for ent in booth.get("entrances", []):
            sign = signs[ent["entrance_id"]]
            entrances.append(
                Entrance(
                    entrance_id=ent["entrance_id"],
                    sign=sign,
                    destination=str(ent["destination"]),
                    approach=ent.get("approach", "L"),
                    note=ent.get("note", ""),
                )
            )
        adjacency = tuple(tuple(pair) for pair in booth.get("adjacency", []))
        booths.append(Booth(booth["booth_id"], entrances, adjacency))

    return ExhibitionInput(
        name=ex.get("name", "exhibition"),
        max_cells=max_cells,
        reengrave_allowed=bool(ex.get("reengrave_allowed", False)),
        marker_policy=marker_policy,
        inventory=inventory,
        booths=booths,
        raw=ex,
    )
