"""Contracts and real publish() integration for the garden handover."""

from __future__ import annotations

from typing import Any

from extcontract import IngestionLibrary, parse_contract

from .world import Evidence, GardenWorld, exact_fraction

GARDEN_READER_VERSION = "garden-unified-r1"

PLANTING_CONTRACT: dict[str, Any] = {
    "version": "garden-planting-r1",
    "fields": [
        {"field_id": "source_id", "name": "土批稳定号", "type": {"kind": "TEXT"}, "nullable": False},
        {"field_id": "season_rank", "name": "相对季序(-1上季,-2上上季)", "type": {"kind": "INT", "bits": 16}, "nullable": False},
        {"field_id": "crop_family", "name": "作物族", "type": {"kind": "TEXT"}, "nullable": False},
        {"field_id": "crop_name", "name": "作物名称", "type": {"kind": "TEXT"}, "nullable": True},
    ],
}

MAP_CONTRACT: dict[str, Any] = {
    "version": "garden-map-r1",
    "fields": [
        {"field_id": "record_kind", "name": "图记录种类", "type": {"kind": "TEXT"}, "nullable": False},
        {"field_id": "lot_id", "name": "格或箱稳定号", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "related_id", "name": "相邻或连接对象", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "display_name", "name": "可改地名", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "lot_kind", "name": "cell/box/stand", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "position_id", "name": "当前位置节点", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "x", "name": "横坐标", "type": {"kind": "INT", "bits": 16}, "nullable": True},
        {"field_id": "y", "name": "纵坐标", "type": {"kind": "INT", "bits": 16}, "nullable": True},
        {"field_id": "width_units", "name": "可容纳宽度", "type": {"kind": "INT", "bits": 16}, "nullable": True},
        {"field_id": "sealed_bottom", "name": "箱是否封底", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "allowed", "name": "是否准许", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "handoff_capacity", "name": "过道本班交位数量", "type": {"kind": "INT", "bits": 16}, "nullable": True},
        {"field_id": "serves_text", "name": "收窄后服务点分号列表", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "source_id", "name": "旧土来源稳定号", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "fraction_text", "name": "实际覆盖比例", "type": {"kind": "TEXT"}, "nullable": True},
    ],
}

SOIL_CONTRACT: dict[str, Any] = {
    "version": "garden-soil-r1",
    "fields": [
        {"field_id": "record_kind", "name": "batch/movement", "type": {"kind": "TEXT"}, "nullable": False},
        {"field_id": "source_id", "name": "出土格及土批号", "type": {"kind": "TEXT"}, "nullable": False},
        {"field_id": "source_amount", "name": "该批土总量", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "origin_lot", "name": "取出格", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "amount", "name": "本次去向数量", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "destination_kind", "name": "retained/box/mixed", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "destination_id", "name": "留田格或真实箱号", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "sealed_bottom", "name": "是否封底", "type": {"kind": "TEXT"}, "nullable": True},
        {"field_id": "box_name", "name": "箱的显示名", "type": {"kind": "TEXT"}, "nullable": True},
    ],
}

CONTRACT_SPECS = {
    "planting": PLANTING_CONTRACT,
    "map": MAP_CONTRACT,
    "soil": SOIL_CONTRACT,
}


def garden_contracts(*, role: str = "reader") -> dict[str, Any]:
    return {
        kind: parse_contract(spec, role=role)  # type: ignore[arg-type]
        for kind, spec in CONTRACT_SPECS.items()
    }


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "yes", "true", "y", "是", "准", "允许"}


def _int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


class GardenLedger:
    """Three real ``IngestionLibrary`` instances and publish-based rebuilding."""

    def __init__(self) -> None:
        self.reader_contracts = garden_contracts(role="reader")
        self.writer_contracts = garden_contracts(role="writer")
        self.libraries = {
            kind: IngestionLibrary(contract) for kind, contract in self.reader_contracts.items()
        }
        self.results: list[Any] = []

    def ingest(self, kind: str, document: dict[str, Any]) -> Any:
        result = self.libraries[kind].ingest(self.writer_contracts[kind], document)
        self.results.append(result)
        return result

    def ingest_files(self, files: dict[str, dict[str, Any]]) -> dict[str, Any]:
        out = {}
        for kind, document in files.items():
            out[kind] = self.ingest(kind, document)
        return out

    def publish(self, kind: str) -> dict[str, Any]:
        return self.libraries[kind].publish()

    def publish_all(self) -> dict[str, dict[str, Any]]:
        return {kind: lib.publish() for kind, lib in self.libraries.items()}

    def revision_basis(self) -> dict[str, Any]:
        basis: dict[str, Any] = {}
        for kind, snapshot in self.publish_all().items():
            basis[kind] = {
                "reader_version": snapshot["reader_version"],
                "rows": {
                    row["row_id"]: row["revision"] for row in snapshot["rows"]
                },
            }
        return basis

    def build_world(self) -> GardenWorld:
        return world_from_snapshots(self.publish_all())


def world_from_snapshots(snapshots: dict[str, dict[str, Any]]) -> GardenWorld:
    """Read current effective rows from actual ``publish()`` snapshots."""

    world = GardenWorld()

    planting = snapshots.get("planting", {"rows": []})
    for row in planting.get("rows", []):
        v = row["values"]
        source = v["source_id"]
        rank = _int(v["season_rank"])
        ev = Evidence(row_id=row["row_id"], revision=row["revision"], sources=row.get("sources", {}))
        if source and rank is not None and v.get("crop_family"):
            world.add_planting(source, rank, v["crop_family"], evidence=ev)

    map_snapshot = snapshots.get("map", {"rows": []})
    for row in map_snapshot.get("rows", []):
        v = row["values"]
        kind = v.get("record_kind")
        if kind == "lot":
            lot_id = v["lot_id"]
            world.add_lot(
                lot_id,
                kind=v.get("lot_kind") or "cell",
                display_name=v.get("display_name"),
                x=_int(v.get("x")),
                y=_int(v.get("y")),
                width=_int(v.get("width_units")) or 1,
                sealed_bottom=_truthy(v.get("sealed_bottom"), True),
                current_position=v.get("position_id") or None,
            )
        if kind == "position":
            world.add_position(v["lot_id"], _int(v.get("x")), _int(v.get("y")))
            if _truthy(v.get("allowed"), True):
                world.approve_position(v["lot_id"])
        elif kind == "adjacency":
            world.add_adjacency(v["lot_id"], v["related_id"])
        elif kind == "position_adjacency":
            world.add_position_adjacency(v["lot_id"], v["related_id"])
        elif kind == "edge":
            world.add_edge(
                v["lot_id"],
                v["related_id"],
                allowed=_truthy(v.get("allowed"), True),
                width=_int(v.get("width_units")) or 2,
            )
        elif kind == "aisle":
            serves = None
            if v.get("serves_text"):
                serves = {part for part in str(v["serves_text"]).split(";") if part}
            world.add_aisle(
                v["lot_id"],
                allowed=_truthy(v.get("allowed"), True),
                width=_int(v.get("width_units")) or 2,
                handoff_capacity=_int(v.get("handoff_capacity")),
                serves=serves,
            )
        elif kind == "coverage":
            world.add_coverage(
                v["lot_id"],
                v["source_id"],
                exact_fraction(v.get("fraction_text"), default=__import__("fractions").Fraction(1)),
            )

    soil = snapshots.get("soil", {"rows": []})
    for row in soil.get("rows", []):
        v = row["values"]
        kind = v.get("record_kind")
        if kind == "batch":
            world.add_source(
                v["source_id"],
                amount=v.get("source_amount") or 1,
                origin_lot=v.get("origin_lot"),
            )
        elif kind == "movement":
            world.add_movement(
                source=v["source_id"],
                amount=v["amount"],
                destination=v["destination_id"],
                destination_kind=v.get("destination_kind"),
                origin_lot=v.get("origin_lot"),
                movement_id=row["row_id"],
                sealed_bottom=_truthy(v.get("sealed_bottom"), True),
                box_name=v.get("box_name"),
            )

    # Planting-only source rows still need history even if no batch row exists.
    for source in world.histories:
        world.source_amounts.setdefault(source, __import__("fractions").Fraction(0))
    world.finalize()
    return world
