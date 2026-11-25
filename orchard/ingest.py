"""果园事实接入层：用真实归一任务读取开花日志 / 供粉日段 / 受粉日段 / 冷天补记。

设计
====
* 每个关注点（bloom / provide / receive / cold）是一份 CSV，经 :class:`confcat.ingest.TaskRepository`
  的真实接入任务归一化为关系表；任务绑定产品+版本快照，逐列保留 column lineage；
* ``RawFacts`` 是归一化行的语义聚合（每株的开花/可供/受粉日集合 + 片区）；
* ``locate_record`` 用**当时版本的列来源**定位一条补记：给定任务、行号、目标列，
  返回该值来自哪一原 CSV 列、表头行号，以及列映射配置在 YAML 中的来源行。

布尔约定：单元格取值 ``yes``/``true``/``1``/``是`` 视为真；其余（含 ``no``）为假。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from confcat.errors import TaskRejected
from confcat.ingest import TaskRepository

_TRUE = {"yes", "true", "1", "是", "y"}


def _is_true(value: str) -> bool:
    return value.strip().lower() in _TRUE


def _as_int(value: str, *, task_id: str, column: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise TaskRejected(
            f"任务 {task_id!r} 列 {column!r} 期望整数日，实际为 {value!r}",
            path=(column,),
            reason="bad-day-value",
        ) from None


# --------------------------------------------------------------------------- #
# 事实聚合
# --------------------------------------------------------------------------- #
@dataclass
class RawFacts:
    """归一化行的语义视图（仍保留来源任务，便于回溯）。

    ``support`` 以 ``(concern, tree, day) -> 行号`` 记录每天事实来自哪一行，
    供「指明哪份记录支撑哪天的重合」使用。
    """

    trees: tuple[str, ...]
    region: dict[str, str]
    bloom: dict[str, set[int]]
    provide: dict[str, set[int]]
    receive: dict[str, set[int]]
    tasks: dict[str, str]  # concern -> task_id
    row_counts: dict[str, int] = field(default_factory=dict)
    support: dict[tuple[str, str, int], int] = field(default_factory=dict)

    def all_days(self) -> set[int]:
        days: set[int] = set()
        for d in (self.bloom, self.provide, self.receive):
            for s in d.values():
                days |= s
        return days

    def row_of(self, concern: str, tree: str, day: int) -> int | None:
        return self.support.get((concern, tree, day))


@dataclass(frozen=True)
class ColdClaim:
    """一条冷天断言。"""

    region: str
    day: int
    occurred: bool
    task_id: str
    row_index: int


@dataclass(frozen=True)
class RecordRef:
    """一条归一化记录的可追溯定位（供「指明哪份记录支撑哪天重合」）。"""

    task_id: str
    row_index: int
    concern: str
    tree: str | None
    region: str | None
    day: int
    target_column: str
    source_column: str
    header_line: int
    prov_value_line: int | None
    prov_path: tuple

    def as_dict(self) -> dict:
        return {
            "task": self.task_id,
            "row": self.row_index,
            "concern": self.concern,
            "tree": self.tree,
            "region": self.region,
            "day": self.day,
            "target_column": self.target_column,
            "source_column": self.source_column,
            "header_line": self.header_line,
            "config_line": self.prov_value_line,
            "config_path": list(self.prov_path),
        }


# --------------------------------------------------------------------------- #
# 用当时版本的列来源定位补记
# --------------------------------------------------------------------------- #
def _lineage_for(repo: TaskRepository, task_id: str, target_column: str) -> dict:
    for entry in repo.get_lineage(task_id):
        if entry["target"] == target_column:
            return entry
    raise TaskRejected(
        f"任务 {task_id!r} 没有目标列 {target_column!r} 的血缘",
        path=(target_column,),
        reason="missing-lineage",
    )


def locate_record(
    repo: TaskRepository,
    task_id: str,
    row_index: int,
    target_column: str,
    *,
    concern: str,
) -> RecordRef:
    """定位任务第 ``row_index`` 行某目标列的补记来源。

    返回的原 CSV 列名 + 表头行号来自**该任务创建时版本**的 column lineage；
    配置来源行来自列映射键的 provenance——正是“用当时版本的列来源定位”。
    """
    rows = repo.get_rows(task_id)
    if row_index < 0 or row_index >= len(rows):
        raise IndexError(f"任务 {task_id!r} 行号 {row_index} 越界（共 {len(rows)} 行）")
    row = rows[row_index]
    lineage = _lineage_for(repo, task_id, target_column)
    prov = lineage.get("provenance") or {}
    prov_path = tuple(prov.get("path", ()))
    value_line = None
    if prov.get("value_at"):
        value_line = prov["value_at"].get("line")
    raw_day = row.get("day")
    return RecordRef(
        task_id=task_id,
        row_index=row_index,
        concern=concern,
        tree=row.get("tree"),
        region=row.get("region"),
        day=_as_int(raw_day, task_id=task_id, column="day") if raw_day is not None else -1,
        target_column=target_column,
        source_column=lineage["source_column"],
        header_line=lineage["header_line"],
        prov_value_line=value_line,
        prov_path=prov_path,
    )


def _group_days(
    repo: TaskRepository, rows: list[dict], concern: str, task_id: str, support: dict
) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for row_no, row in enumerate(rows):
        tree = row.get("tree")
        if not tree:
            raise TaskRejected(
                f"任务 {task_id!r}（{concern}）存在缺少 tree 的行",
                path=("tree",),
                reason="missing-tree",
            )
        if not _is_true(row.get(concern, "")):
            continue
        day = _as_int(row.get("day", ""), task_id=task_id, column="day")
        out.setdefault(tree, set()).add(day)
        support[(concern, tree, day)] = row_no
    return out


# --------------------------------------------------------------------------- #
# 装载事实
# --------------------------------------------------------------------------- #
def load_facts(repo: TaskRepository, task_ids: dict[str, str]) -> RawFacts:
    """从一组接入任务装载 RawFacts。``task_ids`` 形如 {"bloom":..., "provide":...}。"""
    missing = {"bloom", "provide", "receive"} - set(task_ids)
    if missing:
        raise TaskRejected(f"缺少事实任务：{sorted(missing)}", reason="missing-task")

    bloom_rows = repo.get_rows(task_ids["bloom"])
    provide_rows = repo.get_rows(task_ids["provide"])
    receive_rows = repo.get_rows(task_ids["receive"])

    region: dict[str, str] = {}
    for row in bloom_rows:
        tree = row.get("tree")
        reg = row.get("region")
        if not tree or not reg:
            raise TaskRejected(
                f"任务 {task_ids['bloom']!r} 存在缺少 tree/region 的行",
                reason="missing-tree-region",
            )
        prev = region.get(tree)
        if prev is not None and prev != reg:
            raise TaskRejected(
                f"树 {tree!r} 片区不一致：{prev!r} vs {reg!r}",
                reason="region-conflict",
            )
        region[tree] = reg

    support: dict[tuple[str, str, int], int] = {}
    bloom = _group_days(repo, bloom_rows, "bloom", task_ids["bloom"], support)
    provide = _group_days(repo, provide_rows, "provide", task_ids["provide"], support)
    receive = _group_days(repo, receive_rows, "receive", task_ids["receive"], support)

    trees = tuple(region.keys())
    for tree in trees:
        bloom.setdefault(tree, set())
        provide.setdefault(tree, set())
        receive.setdefault(tree, set())

    return RawFacts(
        trees=trees,
        region=region,
        bloom=bloom,
        provide=provide,
        receive=receive,
        tasks=dict(task_ids),
        row_counts={
            "bloom": len(bloom_rows),
            "provide": len(provide_rows),
            "receive": len(receive_rows),
        },
        support=support,
    )


def load_cold_claims(repo: TaskRepository, task_id: str) -> list[ColdClaim]:
    """从冷天接入任务读取断言列表（保留行号以便定位补记）。"""
    rows = repo.get_rows(task_id)
    claims: list[ColdClaim] = []
    for i, row in enumerate(rows):
        region = row.get("region")
        if not region:
            raise TaskRejected(
                f"冷天任务 {task_id!r} 第 {i} 行缺少 region",
                path=("region",),
                reason="missing-region",
            )
        claims.append(
            ColdClaim(
                region=region,
                day=_as_int(row.get("day", ""), task_id=task_id, column="day"),
                occurred=_is_true(row.get("cold", "")),
                task_id=task_id,
                row_index=i,
            )
        )
    return claims
