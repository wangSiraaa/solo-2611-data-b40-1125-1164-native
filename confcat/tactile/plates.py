"""两份归一任务：点阵刻样与底座朝向。

两份任务彼此独立、各自绑定**归档配置产品的某个版本快照**，满足题面
“点阵刻样、底座允许的朝向分属两份归一任务；从实际任务行、采用的列映射和
归档产品版本读取”。配置仍走受限 YAML 目录（``tactile/...`` 路径），实际行
仍走 :func:`confcat.csv_pipeline.normalize`，因此列映射冲突、原列缺失、
分区缺失等整次拒绝语义全部复用，列血缘也逐列保留。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..catalog import Catalog
from ..codec import to_plain
from ..csv_pipeline import MissingRule, NormalizedTable, normalize
from ..errors import CatalogError, TaskRejected
from ..model import MatMapping, MatScalar, MatSequence, Provenance
from .domain import BaseSpec, LEFT, PlateSpec, RIGHT

# 触觉任务在配置产品中的固定路径
PATH_TACTILE = ("tactile",)
PATH_SOURCE_URI = ("tactile", "source_uri")
PATH_COLUMN_MAPPING = ("tactile", "column_mapping")
PATH_MISSING = ("tactile", "missing_value")
PATH_PARTITION = ("tactile", "partition_by")


# --------------------------------------------------------------------------- #
# 从目录版本快照读取触觉任务配置（带每键来源证据）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TactileConfig:
    product: str
    version: int
    doc_name: str
    source_uri: str
    column_mapping: dict[str, str]
    mapping_prov: dict[str, Provenance | None]
    missing: MissingRule
    partition_by: tuple[str, ...]

    def describe(self) -> dict:
        return {
            "product": self.product,
            "version": self.version,
            "doc": self.doc_name,
            "source_uri": self.source_uri,
            "column_mapping": dict(self.column_mapping),
            "missing_value": {"token": self.missing.token, "default": self.missing.default},
            "partition_by": list(self.partition_by),
        }


def _node_at(cat: Catalog, product: str, doc: str, path: tuple, version: int):
    snapshot = cat.load_document(product, doc, version)
    try:
        return snapshot.find(path)
    except (KeyError, IndexError):
        raise TaskRejected(
            f"必需配置路径缺失: /{'/'.join(map(str, path))}", path=path, reason="missing-path"
        )


def load_tactile_config(cat: Catalog, product: str, doc: str, version: int | None = None) -> TactileConfig:
    """从归档产品版本读取一份触觉归一任务的配置与列映射来源。"""
    version = cat._resolve_version(product, version)

    uri_node = _node_at(cat, product, doc, PATH_SOURCE_URI, version)
    if not isinstance(uri_node, MatScalar) or not isinstance(uri_node.value, str):
        raise TaskRejected("tactile.source_uri 需要字符串", path=PATH_SOURCE_URI, reason="bad-type")

    map_node = _node_at(cat, product, doc, PATH_COLUMN_MAPPING, version)
    if not isinstance(map_node, MatMapping):
        raise TaskRejected("tactile.column_mapping 需要映射", path=PATH_COLUMN_MAPPING, reason="bad-type")
    column_mapping: dict[str, str] = {}
    mapping_prov: dict[str, Provenance | None] = {}
    for key, child in map_node.entries.items():
        if not isinstance(key, str) or not isinstance(child, MatScalar) or not isinstance(child.value, str):
            raise TaskRejected(
                "column_mapping 必须是 目标列(str)->原列(str)",
                path=PATH_COLUMN_MAPPING,
                reason="bad-type",
            )
        column_mapping[key] = child.value
        mapping_prov[key] = map_node.key_prov[key]
    if not column_mapping:
        raise TaskRejected("column_mapping 不能为空", path=PATH_COLUMN_MAPPING, reason="empty-mapping")

    missing = MissingRule()
    try:
        miss_node = cat.load_document(product, doc, version).find(PATH_MISSING)
        if not isinstance(miss_node, MatMapping):
            raise TaskRejected("missing_value 必须是映射", path=PATH_MISSING, reason="bad-type")
        token, default = "", None
        for key, child in miss_node.entries.items():
            if not isinstance(child, MatScalar) or not isinstance(child.value, str):
                raise TaskRejected("missing_value 的值必须是字符串", path=PATH_MISSING, reason="bad-type")
            if key == "token":
                token = child.value
            elif key == "default":
                default = child.value
            else:
                raise TaskRejected(f"missing_value 含未知键 {key!r}", path=PATH_MISSING, reason="unknown-key")
        missing = MissingRule(token=token, default=default)
    except (KeyError, IndexError):
        pass

    partition_by: tuple[str, ...] = ()
    try:
        part_node = cat.load_document(product, doc, version).find(PATH_PARTITION)
        if not isinstance(part_node, MatSequence):
            raise TaskRejected("partition_by 必须是序列", path=PATH_PARTITION, reason="bad-type")
        parts = []
        for child in part_node.items:
            if not isinstance(child, MatScalar) or not isinstance(child.value, str):
                raise TaskRejected("partition_by 元素必须是字符串", path=PATH_PARTITION, reason="bad-type")
            parts.append(child.value)
        partition_by = tuple(parts)
    except (KeyError, IndexError):
        pass

    return TactileConfig(
        product=product,
        version=version,
        doc_name=doc,
        source_uri=uri_node.value,
        column_mapping=column_mapping,
        mapping_prov=mapping_prov,
        missing=missing,
        partition_by=partition_by,
    )


def run_normalization(cfg: TactileConfig, csv_path: str | Path) -> NormalizedTable:
    """按归档版本配置对实际任务行执行归一（合同违规整次拒绝）。"""
    return normalize(
        csv_path=csv_path,
        column_mapping=cfg.column_mapping,
        mapping_prov=cfg.mapping_prov,
        missing=cfg.missing,
        partition_by=cfg.partition_by,
    )


# --------------------------------------------------------------------------- #
# 归一结果行 -> 领域对象
# --------------------------------------------------------------------------- #
def _cell_list(raw: str) -> frozenset[int]:
    raw = (raw or "").strip()
    if not raw:
        return frozenset()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise TaskRejected(f"格号列表应为逗号分隔整数，收到 {raw!r}", reason="bad-cell-list")
        out.add(int(part))
    return frozenset(out)


def _edge_set(raw: str) -> frozenset[str]:
    raw = (raw or "").strip()
    if not raw:
        return frozenset()
    edges = set()
    for ch in raw.replace("|", ",").replace(";", ",").split(","):
        ch = ch.strip().upper()
        if ch and ch not in (LEFT, RIGHT):
            raise TaskRejected(f"边缘只允许 L/R，收到 {ch!r}", reason="bad-edge")
        if ch:
            edges.add(ch)
    return frozenset(edges)


def _lineage_summary(table: NormalizedTable, target: str) -> dict:
    col = table.column(target)
    return {
        "target": col.target,
        "source_column": col.source_column,
        "header_line": col.header_line,
        "config_path": "/".join(col.provenance.path) if col.provenance else None,
        "defined_line": col.provenance.value_mark.line if col.provenance else None,
    }


@dataclass
class PlateBundle:
    plates: tuple[PlateSpec, ...]
    config: TactileConfig
    table: NormalizedTable
    sources: dict[str, dict] = field(default_factory=dict)

    def get(self, plate_id: str) -> PlateSpec:
        for p in self.plates:
            if p.plate_id == plate_id:
                return p
        raise KeyError(plate_id)


@dataclass
class BaseBundle:
    bases: tuple[BaseSpec, ...]
    config: TactileConfig
    table: NormalizedTable
    sources: dict[str, dict] = field(default_factory=dict)

    def get(self, base_id: str) -> BaseSpec:
        for b in self.bases:
            if b.base_id == base_id:
                return b
        raise KeyError(base_id)


def parse_plate_rows(cfg: TactileConfig, table: NormalizedTable) -> PlateBundle:
    """点阵刻样任务行 -> :class:`PlateSpec`（含实际行、列映射与版本血缘）。"""
    required = ("plate_id", "n", "dot_cells")
    for name in required:
        if name not in table.columns:
            raise TaskRejected(f"点阵刻样归一结果缺少目标列 {name!r}", reason="missing-target-column")

    plates: list[PlateSpec] = []
    sources: dict[str, dict] = {}
    for row_no, row in enumerate(table.rows):
        plate_id = row["plate_id"].strip()
        if not plate_id:
            raise TaskRejected(f"第 {row_no} 行 plate_id 为空", reason="empty-id")
        n_raw = row["n"].strip()
        if not n_raw.lstrip("-").isdigit():
            raise TaskRejected(f"牌 {plate_id} 的 n 不是整数: {n_raw!r}", reason="bad-n")
        n = int(n_raw)
        dots = _cell_list(row.get("dot_cells", ""))
        occluded = _cell_list(row.get("occluded_cells", "")) if "occluded_cells" in table.columns else frozenset()
        edges = _edge_set(row.get("edges", "")) if "edges" in table.columns else frozenset((LEFT, RIGHT))
        try:
            spec = PlateSpec(
                plate_id=plate_id,
                n=n,
                dots=dots,
                occluded=occluded,
                edges=edges or frozenset((LEFT, RIGHT)),
            )
        except ValueError as exc:
            raise TaskRejected(f"牌 {plate_id} 刻样非法: {exc}", reason="bad-plate") from None
        plates.append(spec)
        sources[plate_id] = {
            "row_no": row_no,
            "csv_row": dict(row),
            "product": cfg.product,
            "version": cfg.version,
            "doc": cfg.doc_name,
            "source_csv": table.source_csv,
            "lineage": {t: _lineage_summary(table, t) for t in table.columns},
        }
    return PlateBundle(tuple(plates), cfg, table, sources)


def parse_base_rows(cfg: TactileConfig, table: NormalizedTable) -> BaseBundle:
    """底座朝向任务行 -> :class:`BaseSpec`。"""
    required = ("base_id", "kind")
    for name in required:
        if name not in table.columns:
            raise TaskRejected(f"底座归一结果缺少目标列 {name!r}", reason="missing-target-column")

    bases: list[BaseSpec] = []
    sources: dict[str, dict] = {}
    for row_no, row in enumerate(table.rows):
        base_id = row["base_id"].strip()
        if not base_id:
            raise TaskRejected(f"第 {row_no} 行 base_id 为空", reason="empty-id")
        kind = row["kind"].strip().lower()
        locked_side = row.get("locked_side", "").strip().upper() or None
        try:
            spec = BaseSpec(base_id=base_id, kind=kind, locked_side=locked_side)
        except ValueError as exc:
            raise TaskRejected(f"底座 {base_id} 非法: {exc}", reason="bad-base") from None
        bases.append(spec)
        sources[base_id] = {
            "row_no": row_no,
            "csv_row": dict(row),
            "product": cfg.product,
            "version": cfg.version,
            "doc": cfg.doc_name,
            "source_csv": table.source_csv,
            "lineage": {t: _lineage_summary(table, t) for t in table.columns},
        }
    return BaseBundle(tuple(bases), cfg, table, sources)


def load_plate_bundle(cat: Catalog, product: str, doc: str, csv_path: str | Path,
                      version: int | None = None) -> PlateBundle:
    cfg = load_tactile_config(cat, product, doc, version)
    return parse_plate_rows(cfg, run_normalization(cfg, csv_path))


def load_base_bundle(cat: Catalog, product: str, doc: str, csv_path: str | Path,
                     version: int | None = None) -> BaseBundle:
    cfg = load_tactile_config(cat, product, doc, version)
    return parse_base_rows(cfg, run_normalization(cfg, csv_path))
