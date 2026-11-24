"""接印数据模型与输入加载。

所有业务输入均为扁平 Parquet 行（压样、盒数量、订单）；静态字典（字面特征定义、
可开盒权限、旧版、封版安排、台面时段）放在任务 JSON 中。读取 Parquet 时附上
来源文件与物理行号，保证每一条被采用的证据都能回溯到来源行。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from ..errors import PqScanError

# 扁平 Parquet 里的来源行列名（与 scanner 的溯源列风格一致）。
SRC_FILE = "_source_file"
SRC_ROW = "_source_row"

# 两条兑现路径：
# - seal  ：提前封版，字粒两晚都借不出，不耗排版台、开印时无需开盒；
# - recompose：开印前拆旧版/现场压样后配版，耗一次排版台时段。
ROUTE_SEAL = "seal"
ROUTE_RECOMPOSE = "recompose"
ROUTES = (ROUTE_SEAL, ROUTE_RECOMPOSE)


class PressError(PqScanError):
    """接印输入不合法或数据自相矛盾（不用于损坏 Parquet 恢复）。"""


# ---------------------------------------------------------------------------
# 压样观察
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Mark:
    """一行压样标记：对某字粒某个特征的一次人工观察。

    present=True/False 为看清的有/无；None 表示“看不清”，即不完整观察，
    在身份推理中只产生 UNKNOWN，既不能肯定也不能否定一个字面。
    """

    proof_id: str
    piece_id: str
    box_id: str | None
    taken_at: int
    feature: str
    present: bool | None
    face_hint: str | None
    source_file: str
    source_row: int


# ---------------------------------------------------------------------------
# 盒数量记录
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BoxCount:
    """一盒按批铸的字粒里，某字面的铸字数量记录。"""

    box_id: str
    face: str
    count: int
    source_file: str
    source_row: int


# ---------------------------------------------------------------------------
# 订单（领物票）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Position:
    """版上的固定字位：字位编号 + 该位要求的字面。"""

    slot: str
    face: str


@dataclass(frozen=True)
class Order:
    order_id: str
    night: int                       # 0 = 第一夜班，1 = 第二夜班
    machine: str
    positions: tuple[Position, ...]
    source_box: str | None           # 限定从哪盒配字；None = 不限制
    allowed_routes: tuple[str, ...]  # 该票允许的兑现路径
    source_file: str
    source_row: int


# ---------------------------------------------------------------------------
# 任务 JSON 中的静态对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OldForm:
    """一块占着字的旧版：现场拆开即可释放字粒，拆字不要求开盒权限。"""

    form_id: str
    pieces: tuple[str, ...]


@dataclass(frozen=True)
class SealedForm:
    """提前封好、留到最后一印的版。

    封住的字两晚都借不出（封版期间全程占用），交班人可直接开机。
    若 machine 被其在某晚占用，该晚该机器不能再排别的票。
    """

    form_id: str
    pieces: tuple[str, ...]
    machine: str
    print_night: int


@dataclass(frozen=True)
class Job:
    name: str
    nights: tuple[int, int]                       # 夜班边界时间戳（两晚的起点）
    proof_files: tuple[str, ...]
    box_count_file: str
    order_file: str
    faces: dict[str, frozenset[str]]              # 字面 -> 其必须具备的特征集合
    open_boxes: dict[int, frozenset[str]]         # 每班有权拆开的字盒
    table_slots: dict[int, int]                   # 每班排版台时段数
    old_forms: tuple[OldForm, ...]
    sealed_forms: tuple[SealedForm, ...]
    extra_predicate: dict[str, Any] | None        # 额外的原查询谓词（可为空）
    base_dir: str


# ---------------------------------------------------------------------------
# 扁平 Parquet 读取（附来源行）
# ---------------------------------------------------------------------------

def _read_flat(path: str) -> list[dict[str, Any]]:
    """读扁平 Parquet 为 dict 行列表，附 _source_file/_source_row。"""
    if not os.path.isfile(path):
        raise PressError(f"输入 Parquet 不存在: {path}")
    try:
        table = pq.read_table(path)
    except (pa.ArrowInvalid, OSError) as exc:
        raise PressError(f"无法读取 Parquet {path}: {exc}") from exc
    if any(pa.types.is_nested(t) for t in table.schema.types):
        raise PressError(f"{path}: 只接受扁平 schema，发现嵌套列")
    rows = table.to_pylist()
    for i, row in enumerate(rows):
        row[SRC_FILE] = path
        row[SRC_ROW] = i
    return rows


def _require_columns(path: str, rows: list[dict[str, Any]], required: tuple[str, ...]) -> None:
    if not rows:
        return
    missing = [c for c in required if c not in rows[0]]
    if missing:
        raise PressError(f"{path}: 缺少必需列 {missing}")


def load_marks(job: Job) -> list[Mark]:
    marks: list[Mark] = []
    for path in job.proof_files:
        rows = _read_flat(path)
        _require_columns(
            path, rows,
            ("proof_id", "piece_id", "box_id", "taken_at", "feature", "present", "face_hint"),
        )
        for r in rows:
            present = r["present"]
            if present is not None and not isinstance(present, bool):
                raise PressError(
                    f"{path}:{r[SRC_ROW]} present 必须是布尔或 NULL（看不清），实际 {present!r}"
                )
            if not isinstance(r["taken_at"], int) or isinstance(r["taken_at"], bool):
                raise PressError(f"{path}:{r[SRC_ROW]} taken_at 必须是整数时间戳")
            marks.append(
                Mark(
                    proof_id=str(r["proof_id"]),
                    piece_id=str(r["piece_id"]),
                    box_id=None if r["box_id"] is None else str(r["box_id"]),
                    taken_at=r["taken_at"],
                    feature=str(r["feature"]),
                    present=present,
                    face_hint=None if r["face_hint"] is None else str(r["face_hint"]),
                    source_file=path,
                    source_row=r[SRC_ROW],
                )
            )
    return marks


def load_box_counts(job: Job) -> list[BoxCount]:
    rows = _read_flat(job.box_count_file)
    _require_columns(job.box_count_file, rows, ("box_id", "face", "count"))
    counts: list[BoxCount] = []
    for r in rows:
        if not isinstance(r["count"], int) or isinstance(r["count"], bool) or r["count"] < 0:
            raise PressError(
                f"{job.box_count_file}:{r[SRC_ROW]} count 必须是非负整数，实际 {r['count']!r}"
            )
        counts.append(
            BoxCount(
                box_id=str(r["box_id"]), face=str(r["face"]), count=r["count"],
                source_file=job.box_count_file, source_row=r[SRC_ROW],
            )
        )
    return counts


def load_orders(job: Job) -> list[Order]:
    """订单 Parquet 是全标量扁平行：一个字位一行，订单级字段逐行重复。"""
    rows = _read_flat(job.order_file)
    _require_columns(
        job.order_file, rows,
        ("order_id", "night", "machine", "slot", "face", "source_box", "allowed_routes"),
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    order_seq: list[str] = []
    for r in rows:
        oid = str(r["order_id"])
        if oid not in grouped:
            grouped[oid] = []
            order_seq.append(oid)
        grouped[oid].append(r)

    orders: list[Order] = []
    for oid in order_seq:
        group = grouped[oid]
        first = group[0]
        night = first["night"]
        if night not in (0, 1):
            raise PressError(f"{job.order_file}: 订单 {oid} 的 night 只能是 0 或 1，实际 {night!r}")
        machine = str(first["machine"])
        source_box = None if first["source_box"] is None else str(first["source_box"])
        routes_raw = str(first["allowed_routes"])
        routes = tuple(s.strip() for s in routes_raw.split(",") if s.strip())
        if not routes or any(x not in ROUTES for x in routes):
            raise PressError(
                f"{job.order_file}: 订单 {oid} 的 allowed_routes 非法：{routes_raw!r}"
            )
        positions: list[Position] = []
        seen_slots: set[str] = set()
        for r in group:
            # 订单级字段在各行必须一致，否则视为脏数据拒绝。
            for col, expect in (("night", night), ("machine", machine),
                                ("source_box", source_box), ("allowed_routes", routes_raw)):
                if str(r[col]) != str(expect):
                    raise PressError(
                        f"{job.order_file}:{r[SRC_ROW]} 订单 {oid} 的 {col} 在字位行间不一致"
                    )
            slot = str(r["slot"])
            if slot in seen_slots:
                raise PressError(f"{job.order_file}: 订单 {oid} 的字位 {slot} 重复")
            seen_slots.add(slot)
            positions.append(Position(slot=slot, face=str(r["face"])))
        orders.append(
            Order(
                order_id=oid, night=night, machine=machine, positions=tuple(positions),
                source_box=source_box, allowed_routes=routes,
                source_file=job.order_file, source_row=group[0][SRC_ROW],
            )
        )
    return orders


# ---------------------------------------------------------------------------
# 任务 JSON
# ---------------------------------------------------------------------------

def validate_orders_against_job(orders: list[Order], job: Job) -> None:
    """订单引用的字面与来源盒必须在任务字典中存在。"""
    known_boxes = set(job.open_boxes[0]) | set(job.open_boxes[1])
    for order in orders:
        if order.source_box and order.source_box not in known_boxes:
            raise PressError(
                f"订单 {order.order_id} 限定的来源盒 {order.source_box} 不在任何班次的可开盒清单中"
            )
        for pos in order.positions:
            if pos.face not in job.faces:
                raise PressError(
                    f"订单 {order.order_id} 字位 {pos.slot} 要求未定义字面 {pos.face}"
                )


def load_job(job_json: str) -> Job:
    if not os.path.isfile(job_json):
        raise PressError(f"任务 JSON 不存在: {job_json}")
    base_dir = os.path.dirname(os.path.abspath(job_json))
    try:
        with open(job_json, encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise PressError(f"任务 JSON 解析失败: {exc}") from exc

    def _resolve(p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(base_dir, p)

    try:
        name = str(raw["name"])
        nights = tuple(raw["nights"])
        if len(nights) != 2 or not all(isinstance(x, int) for x in nights) or nights[0] >= nights[1]:
            raise PressError("nights 必须是两个递增的整数时间戳 [第一晚起点, 第二晚起点]")
        faces = {
            str(f): frozenset(str(x) for x in feats)
            for f, feats in raw["faces"].items()
        }
        if not faces:
            raise PressError("faces 至少要定义一个字面")
        open_boxes = {int(k): frozenset(v) for k, v in raw.get("open_boxes", {}).items()}
        table_slots = {int(k): int(v) for k, v in raw.get("table_slots", {}).items()}
        for n in (0, 1):
            open_boxes.setdefault(n, frozenset())
            table_slots.setdefault(n, 0)
        old_forms = tuple(
            OldForm(form_id=str(f["form_id"]), pieces=tuple(str(p) for p in f["pieces"]))
            for f in raw.get("old_forms", [])
        )
        sealed_forms = tuple(
            SealedForm(
                form_id=str(f["form_id"]), pieces=tuple(str(p) for p in f["pieces"]),
                machine=str(f["machine"]), print_night=int(f["print_night"]),
            )
            for f in raw.get("sealed_forms", [])
        )
        for sf in sealed_forms:
            if sf.print_night not in (0, 1):
                raise PressError(f"封版 {sf.form_id} 的 print_night 只能是 0 或 1")
        extra = raw.get("proof_predicate")
        if extra is not None and (not isinstance(extra, dict) or len(extra) != 1):
            raise PressError("proof_predicate 必须是单键谓词树（与 queries.json 同格式）")
        job = Job(
            name=name,
            nights=nights,  # type: ignore[arg-type]
            proof_files=tuple(_resolve(p) for p in raw["proof_files"]),
            box_count_file=_resolve(raw["box_count_file"]),
            order_file=_resolve(raw["order_file"]),
            faces=faces,
            open_boxes=open_boxes,
            table_slots=table_slots,
            old_forms=old_forms,
            sealed_forms=sealed_forms,
            extra_predicate=extra,
            base_dir=base_dir,
        )
    except KeyError as exc:
        raise PressError(f"任务 JSON 缺少必需字段: {exc}") from exc
    return job
