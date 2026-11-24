"""保修接单领域模型：锅 / 挂架 / 挂位 / 底釉批号 / 炉温见证片读数。

一次「炉次」的扫描证据是 pqscan 结果 Parquet 中的实际命中行：每行对应一只锅在一个
挂位上的记录，带底釉批号与该挂架的「高温侧排号」读数（由炉温见证片判定，可为缺失）。
挂架翻身入炉时两排挂位一道对调，因此「哪一排处于高温侧」是**整架**一个朝向变量，
不能为同一挂架上的每只锅各选一次朝向。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HIGH = "high"
LOW = "low"
ZONES = (HIGH, LOW)


class WarrantyError(Exception):
    """保修接单层的输入/一致性错误（区别于 pqscan 的 PlanError）。"""


@dataclass(frozen=True)
class Group:
    """一个生产组：底釉批号 × 温区。"""

    glaze: str
    zone: str

    def label(self) -> str:
        return f"{self.glaze}/{self.zone}"

    def to_dict(self) -> dict[str, str]:
        return {"glaze": self.glaze, "zone": self.zone}


@dataclass(frozen=True)
class PotEvidence:
    """扫描命中行里关于某只锅的一条证据记录（附 pqscan 溯源）。"""

    pot_id: str
    furnace_id: str
    hanger_id: str
    row: int                       # 该锅所在挂位排号：0 或 1
    slot: int
    glaze: str | None              # 底釉批号；None = 记录缺失
    hot_row: int | None            # 该挂架高温侧排号读数；None = 读数缺失
    witness_plate_id: str | None   # 给出该读数的炉温见证片编号（溯源）
    source_file: str
    source_row_group: int
    source_row_in_group: int

    def zone_under(self, hot_row: int) -> str:
        """在「高温侧排号为 hot_row」的世界里，该锅所处温区。"""
        return HIGH if self.row == hot_row else LOW

    def provenance(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_row_group": self.source_row_group,
            "source_row_in_group": self.source_row_in_group,
        }

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pot_id": self.pot_id,
            "furnace_id": self.furnace_id,
            "hanger_id": self.hanger_id,
            "row": self.row,
            "slot": self.slot,
            "glaze_batch": self.glaze,
            "hot_row": self.hot_row,
        }
        out.update(self.provenance())
        return out


@dataclass(frozen=True)
class ProblemConfig:
    """疑似致裂工况：某些 (底釉批号, 温区) 组合同时出现时锅沿可能有暗裂。

    这里记录的是**疑似**组合：落在该组合里的锅属于「待验证风险组」，拆检见证锅若为
    阴性才说明这批实物没问题。SAFE 的定义是「在任何一致朝向世界里都不落在疑似组合」。
    """

    problem_groups: frozenset[Group]

    def is_suspect(self, glaze: str | None, zone: str) -> bool:
        return glaze is not None and Group(glaze, zone) in self.problem_groups

    def to_dict(self) -> dict[str, Any]:
        return {"problem_groups": [g.to_dict() for g in sorted(self.problem_groups, key=lambda g: g.label())]}


@dataclass(frozen=True)
class InventoryItem:
    """库存替换锅：出厂即判定为合格（不在疑似组合里），可按单交出、一经交出不得他用。"""

    item_id: str
    glaze: str
    zone: str
    source: dict[str, Any] | None = None

    @property
    def group(self) -> Group:
        return Group(self.glaze, self.zone)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"item_id": self.item_id, "glaze_batch": self.glaze, "zone": self.zone}
        if self.source:
            out["source"] = self.source
        return out


@dataclass(frozen=True)
class WitnessPot:
    """可拆的见证锅：已知其所属生产组，拆检后报废，用于代表该组。"""

    pot_id: str
    glaze: str | None
    zone: str | None
    source: dict[str, Any] | None = None

    @property
    def group(self) -> Group | None:
        if self.glaze is None or self.zone is None:
            return None
        return Group(self.glaze, self.zone)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"pot_id": self.pot_id, "glaze_batch": self.glaze, "zone": self.zone}
        if self.source:
            out["source"] = self.source
        return out


# ---- JSON 装载 -------------------------------------------------------------

def _require(obj: dict[str, Any], key: str, ctx: str) -> Any:
    if key not in obj:
        raise WarrantyError(f"{ctx} 缺少字段 {key!r}")
    return obj[key]


def problem_config_from_dict(raw: dict[str, Any]) -> ProblemConfig:
    groups = set()
    for item in _require(raw, "problem_groups", "problem config"):
        try:
            groups.add(Group(str(item["glaze"]), str(item["zone"])))
        except (KeyError, TypeError) as exc:
            raise WarrantyError(f"problem_group 形式非法: {item!r}") from exc
    for g in groups:
        if g.zone not in ZONES:
            raise WarrantyError(f"温区非法: {g.zone!r}（只允许 {ZONES}）")
    return ProblemConfig(frozenset(groups))


def inventory_from_list(raw: list[dict[str, Any]]) -> list[InventoryItem]:
    items = []
    for i, item in enumerate(raw):
        try:
            items.append(InventoryItem(
                item_id=str(item["item_id"]),
                glaze=str(item["glaze_batch"]),
                zone=str(item["zone"]),
                source=item.get("source"),
            ))
        except (KeyError, TypeError) as exc:
            raise WarrantyError(f"库存项 #{i} 形式非法: {item!r}") from exc
    seen = set()
    for it in items:
        if it.item_id in seen:
            raise WarrantyError(f"库存项编号重复: {it.item_id}")
        seen.add(it.item_id)
        if it.zone not in ZONES:
            raise WarrantyError(f"库存项 {it.item_id} 温区非法: {it.zone!r}")
    return items


def witnesses_from_list(raw: list[dict[str, Any]]) -> list[WitnessPot]:
    out = []
    seen = set()
    for i, item in enumerate(raw):
        try:
            wp = WitnessPot(
                pot_id=str(item["pot_id"]),
                glaze=item.get("glaze_batch"),
                zone=item.get("zone"),
                source=item.get("source"),
            )
        except (KeyError, TypeError) as exc:
            raise WarrantyError(f"见证锅 #{i} 形式非法: {item!r}") from exc
        if wp.pot_id in seen:
            raise WarrantyError(f"见证锅编号重复: {wp.pot_id}")
        seen.add(wp.pot_id)
        if wp.zone is not None and wp.zone not in ZONES:
            raise WarrantyError(f"见证锅 {wp.pot_id} 温区非法: {wp.zone!r}")
        out.append(wp)
    return out
