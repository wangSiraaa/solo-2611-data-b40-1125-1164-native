"""从 pqscan 结果 Parquet 重建每只锅可能经历的「底釉 × 温区」。

扫描证据每行是某只锅在某个挂位上的记录：
    pot_id, furnace_id, hanger_id, row(0/1), slot,
    glaze_batch(可空), hot_row(可空), witness_plate_id(可空)
外加 pqscan 追加的溯源列 _source_file/_source_row_group/_source_row_in_group。

关键约束：
- 挂架翻身入炉时两排挂位一道对调，所以「哪一排处于高温侧」是**整架一个朝向变量**
  hot_row ∈ {0,1}；同一挂架的不同锅必须共用同一朝向，不能各选一次。
- 见证片读数缺失（hot_row 为 NULL）时保留两种朝向，即保留两种可能性；
  读数存在则把该挂架的朝向钉死为出现过的那个值。
- 同一挂架的读数互相矛盾（同时出现 0 与 1）视为证据自相矛盾：该挂架无任何一致朝向，
  其上的锅一律判为「无法证明」，绝不猜一个朝向。
- 同一只锅可能有多条命中行（多次炉次/重复记录）：把各行给出的所有可能组合取并集，
  即「这只锅可能经历的底釉和温区」。
- 底釉批号缺失时无法枚举其可能的组合集合，标记为 unknown，永不判「可放行」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pyarrow.parquet as pq

from .model import Group, HIGH, LOW, PotEvidence, WarrantyError, ZONES

PROVENANCE = ("_source_file", "_source_row_group", "_source_row_in_group")
EVIDENCE_COLUMNS = (
    "pot_id", "furnace_id", "hanger_id", "row", "slot",
    "glaze_batch", "hot_row", "witness_plate_id",
)


@dataclass
class HangerOrientation:
    """一个挂架的朝向证据：哪些排号可能是高温侧。"""

    furnace_id: str
    hanger_id: str
    possible_hot_rows: frozenset[int]          # 子集 {0,1}；空集=证据自相矛盾
    readings: list[dict[str, Any]] = field(default_factory=list)  # 见证片读数及其来源
    conflicting: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "furnace_id": self.furnace_id,
            "hanger_id": self.hanger_id,
            "possible_hot_rows": sorted(self.possible_hot_rows),
            "conflicting": self.conflicting,
            "readings": self.readings,
        }


@dataclass
class PotRecords:
    """一只锅的全部证据行与由此得到的可能组合集合。"""

    pot_id: str
    furnace_id: str
    hanger_id: str
    rows: list[PotEvidence]
    possible_groups: set[Group] | None         # None = 无法枚举（底釉缺失/朝向矛盾）
    unknown_reason: str | None = None

    def source_records(self) -> list[dict[str, Any]]:
        return [
            {
                "glaze_batch": r.glaze,
                "row": r.row,
                "slot": r.slot,
                "hot_row": r.hot_row,
                "witness_plate_id": r.witness_plate_id,
                **r.provenance(),
            }
            for r in self.rows
        ]

    @property
    def glaze_values(self) -> list[str | None]:
        return [r.glaze for r in self.rows]


def _zone_for(row: int, hot_row: int) -> str:
    return HIGH if row == hot_row else LOW


def _row_possible_zones(row: int, possible_hot_rows: frozenset[int]) -> set[str]:
    return {_zone_for(row, h) for h in possible_hot_rows}


def load_evidence(
    paths: list[str],
    furnace_id: str | None = None,
    orientation_overrides: dict[tuple[str, str], int] | None = None,
) -> tuple[list[PotRecords], dict[tuple[str, str], HangerOrientation]]:
    """读取一个或多个 pqscan 结果 Parquet，重建锅记录与挂架朝向。

    paths 通常是先经 pqscan 扫描得到的 result Parquet（含溯源列）。
    orientation_overrides 表示「事后补到了一条架向读数」：键为 (炉号, 挂架号)，
    值为被钉死的高温侧排号。它只影响该挂架的朝向可能性，从而改变其上每只锅的
    可能温区；不会为单只锅单独挑朝向。
    """
    evidence: list[PotEvidence] = []
    for path in paths:
        pf = pq.ParquetFile(path)
        names = set(pf.schema_arrow.names)
        missing = [c for c in EVIDENCE_COLUMNS if c not in names]
        if missing:
            raise WarrantyError(f"{path}: 证据结果缺少列 {missing}")
        has_prov = all(c in names for c in PROVENANCE)
        for rgi in range(pf.num_row_groups):
            for ri, row in enumerate(pf.read_row_group(rgi).to_pylist()):
                if furnace_id is not None and row["furnace_id"] != furnace_id:
                    continue
                if row["row"] not in (0, 1):
                    raise WarrantyError(f"{path}: pot {row['pot_id']} 的挂位排号 row={row['row']} 非法（只允许 0/1）")
                if row["hot_row"] is not None and row["hot_row"] not in (0, 1):
                    raise WarrantyError(f"{path}: 挂架 {row['hanger_id']} 的 hot_row={row['hot_row']} 非法（只允许 0/1）")
                if has_prov:
                    prov = {
                        "source_file": row["_source_file"],
                        "source_row_group": row["_source_row_group"],
                        "source_row_in_group": row["_source_row_in_group"],
                    }
                else:
                    prov = {"source_file": path, "source_row_group": rgi, "source_row_in_group": ri}
                evidence.append(PotEvidence(
                    pot_id=str(row["pot_id"]),
                    furnace_id=str(row["furnace_id"]),
                    hanger_id=str(row["hanger_id"]),
                    row=int(row["row"]),
                    slot=int(row["slot"]),
                    glaze=row["glaze_batch"],
                    hot_row=row["hot_row"],
                    witness_plate_id=row["witness_plate_id"],
                    **prov,
                ))

    # ---- 先用全部证据行确定每个挂架的朝向可能性 --------------------------
    hangers: dict[tuple[str, str], HangerOrientation] = {}
    readings: dict[tuple[str, str], set[int]] = {}
    read_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for ev in evidence:
        key = (ev.furnace_id, ev.hanger_id)
        readings.setdefault(key, set())
        read_rows.setdefault(key, [])
        if ev.hot_row is not None:
            readings[key].add(ev.hot_row)
            read_rows[key].append({
                "hot_row": ev.hot_row,
                "witness_plate_id": ev.witness_plate_id,
                "pot_id": ev.pot_id,
                **ev.provenance(),
            })
    for key, vals in readings.items():
        if len(vals) > 1:
            orient = HangerOrientation(key[0], key[1], frozenset(), read_rows[key], conflicting=True)
        elif len(vals) == 1:
            orient = HangerOrientation(key[0], key[1], frozenset(vals), read_rows[key])
        else:
            orient = HangerOrientation(key[0], key[1], frozenset({0, 1}), [])
        hangers[key] = orient

    # 事后补到的架向读数：整架钉死朝向（对本架所有锅一致生效）。
    for key, hot_row in (orientation_overrides or {}).items():
        if hot_row not in (0, 1):
            raise WarrantyError(f"补读架向 ({key[0]}, {key[1]}) 的 hot_row={hot_row} 非法（只允许 0/1）")
        cur = hangers.get(key)
        if cur is None:
            hangers[key] = HangerOrientation(key[0], key[1], frozenset({hot_row}),
                                             [{"hot_row": hot_row, "witness_plate_id": None,
                                               "pot_id": None, "supplied_by": "manual_reading"}])
        elif cur.conflicting:
            raise WarrantyError(
                f"挂架 {key[1]} 原始读数自相矛盾，无法用单条补读覆盖，需人工核对见证片")
        elif cur.possible_hot_rows != frozenset({0, 1}) and hot_row not in cur.possible_hot_rows:
            # 已有确定读数且与补读相反：两条证据冲突，不得用补读硬覆盖。
            raise WarrantyError(
                f"挂架 {key[1]} 已有确定读数 hot_row={sorted(cur.possible_hot_rows)}，"
                f"与补读 hot_row={hot_row} 冲突，需人工核对见证片")
        else:
            cur.possible_hot_rows = frozenset({hot_row})
            cur.readings = cur.readings + [{"hot_row": hot_row, "witness_plate_id": None,
                                            "pot_id": None, "supplied_by": "manual_reading"}]

    # ---- 再按 (炉号, 锅号) 归并命中行并求可能组合 --------------------------
    by_pot: dict[tuple[str, str], list[PotEvidence]] = {}
    order: list[tuple[str, str]] = []
    for ev in evidence:
        key = (ev.furnace_id, ev.pot_id)
        if key not in by_pot:
            by_pot[key] = []
            order.append(key)
        by_pot[key].append(ev)

    pots: list[PotRecords] = []
    for key in order:
        rows = by_pot[key]
        furnace = key[0]
        hanger = rows[0].hanger_id
        possible: set[Group] = set()
        unknown_reason: str | None = None
        for r in rows:
            if r.glaze is None:
                unknown_reason = "底釉批号记录缺失，无法枚举可能的底釉"
                break
            orient = hangers[(r.furnace_id, r.hanger_id)]
            if orient.conflicting:
                unknown_reason = f"挂架 {r.hanger_id} 的见证片读数自相矛盾，无一致朝向"
                break
            for zone in _row_possible_zones(r.row, orient.possible_hot_rows):
                if zone not in ZONES:  # 防御性
                    raise WarrantyError(f"非法温区 {zone}")
                possible.add(Group(r.glaze, zone))
        pots.append(PotRecords(
            pot_id=key[1], furnace_id=furnace, hanger_id=hanger,
            rows=rows, possible_groups=None if unknown_reason else possible,
            unknown_reason=unknown_reason,
        ))
    return pots, hangers


def select_order_pots(pots: list[PotRecords], pot_ids: list[str], furnace_id: str | None = None) -> list[PotRecords]:
    """按订单给出的锅号取子集；缺失的锅号造一条「无证据」记录（永不判可放行）。"""
    index = {p.pot_id: p for p in pots}
    out: list[PotRecords] = []
    for pid in pot_ids:
        if pid in index:
            out.append(index[pid])
        else:
            out.append(PotRecords(
                pot_id=pid, furnace_id=furnace_id or "", hanger_id="",
                rows=[], possible_groups=None,
                unknown_reason="该锅号在扫描结果中没有命中行，缺少底釉/温区证据",
            ))
    return out
