"""布景工坊接单服务：读消费仓归一任务的实际行，生成裁装回单并持久化。

接单合同
========
输入卷料归一任务编号、墙面归一任务编号与转台墙序，加上交货办法：

* **封签定向段（sealed）**：库内按面裁出定向纸段、封签随车，每面只载一段，
  上台后不能翻头或另开卷。所有出车现场必须共用同一份（墙 → 卷段）指派，
  否则整单不能担保。
* **整卷上台（whole_roll）**：带整卷上台，立好第一面才实测实销，再决定从余卷
  何处下刀。每个现场独立给指派；已剪掉的边头按占用坐标落库，后续订单只能领
  剩下的整段。

回单与复看
==========
* 接单即把两项任务的**实际行与映射来源（逐行血缘）快照**进工坊库；墙面任务
  后来修订相位/高度，旧裁单仍按当时快照复看。
* 每个现场各自落库：能否担保、逐墙卷段（卷号 + 原坐标，取模前信息不丢）、
  卡点（具体接缝与卷段）。
* 已担保订单的领纸坐标记为占用，跨订单保证同一段纸不会被两面墙/两班重复领走。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import CatalogError, OrderRejected, WorkshopConflict
from .ingest import TaskRepository
from .workshop_model import (
    COL_DIRECTION,
    COL_END,
    COL_FIRST_BEAT,
    COL_FROM_WALL,
    COL_HEIGHT,
    COL_MARKER,
    COL_OFFSET,
    COL_PERIOD,
    COL_PRE_MOUNTED,
    COL_ROLL_ID,
    COL_SCENARIO,
    COL_START,
    COL_TO_WALL,
    COL_WALL_ID,
    DIRECTIONS,
    MODE_SEALED,
    DELIVERY_MODES,
    Blockage,
    CutSegment,
    RollSpec,
    Scenario,
    ScenarioPlan,
    SeamSpec,
    WallSpec,
    WorkshopSpec,
    plan_scenario,
    roll_remaining_intervals,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workshop_order (
    order_id        TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    delivery_mode   TEXT NOT NULL,
    roll_task_id    TEXT NOT NULL,
    wall_task_id    TEXT NOT NULL,
    period          INTEGER NOT NULL,
    wall_order_json TEXT NOT NULL,
    guaranteed      INTEGER NOT NULL,
    scenarios_json  TEXT NOT NULL,
    roll_snapshot_json  TEXT NOT NULL,
    wall_snapshot_json  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workshop_order_assignment (
    order_id   TEXT NOT NULL,
    scenario   TEXT NOT NULL,
    wall_id    TEXT NOT NULL,
    roll_id    TEXT,
    start_coord INTEGER,
    end_coord  INTEGER,
    first_beat INTEGER,
    last_beat  INTEGER,
    PRIMARY KEY (order_id, scenario, wall_id),
    FOREIGN KEY (order_id) REFERENCES workshop_order(order_id)
);
CREATE TABLE IF NOT EXISTS workshop_order_blockage (
    order_id   TEXT NOT NULL,
    scenario   TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL,
    wall_id    TEXT,
    seam_json  TEXT,
    roll_id    TEXT,
    segment_json TEXT,
    PRIMARY KEY (order_id, scenario, seq),
    FOREIGN KEY (order_id) REFERENCES workshop_order(order_id)
);
CREATE TABLE IF NOT EXISTS workshop_occupancy (
    order_id   TEXT NOT NULL,
    scenario   TEXT NOT NULL,
    roll_id    TEXT NOT NULL,
    start_coord INTEGER NOT NULL,
    end_coord  INTEGER NOT NULL,
    wall_id    TEXT NOT NULL,
    PRIMARY KEY (order_id, scenario, roll_id, start_coord, end_coord)
);
CREATE TABLE IF NOT EXISTS workshop_task_row (
    task_id    TEXT NOT NULL,
    row_no     INTEGER NOT NULL,
    row_json   TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    PRIMARY KEY (task_id, row_no)
);
"""

DEFAULT_SCENARIO = "default"


@dataclass
class OrderReceipt:
    """裁装回单：领班按单号复查实用段或卡住的那道缝。"""

    order_id: str
    delivery_mode: str
    roll_task_id: str
    wall_task_id: str
    period: int
    wall_order: tuple[str, ...]
    guaranteed: bool
    scenarios: list[ScenarioPlan]
    created_at: str

    def as_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "delivery_mode": self.delivery_mode,
            "roll_task_id": self.roll_task_id,
            "wall_task_id": self.wall_task_id,
            "period": self.period,
            "wall_order": list(self.wall_order),
            "guaranteed": self.guaranteed,
            "scenarios": [p.as_dict() for p in self.scenarios],
            "created_at": self.created_at,
        }


def _as_int(value, field: str, row_no: int) -> int:
    # CSV 经归一化后各列均为字符串；bool 不接受，数字串按十进制整数解析
    if isinstance(value, bool):
        raise OrderRejected(f"第 {row_no + 2} 行字段 {field} 需要整数，实际 {value!r}", reason="bad-int")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and (text.lstrip("-").isdigit()):
            return int(text)
    raise OrderRejected(f"第 {row_no + 2} 行字段 {field} 需要整数，实际 {value!r}", reason="bad-int")


def _opt_int(value, field: str, row_no: int) -> int | None:
    if value in (None, ""):
        return None
    return _as_int(value, field, row_no)


def _snapshot_int(value) -> int:
    """快照 JSON 里的数值可能以字符串落盘（CSV 归一值），复看时统一转 int。"""
    if isinstance(value, bool):
        raise OrderRejected(f"快照数值非法: {value!r}", reason="bad-int")
    return value if isinstance(value, int) else int(str(value).strip())


class WorkshopRepository:
    """布景工坊仓：接裁装单、管卷坐标占用、按单号复看。"""

    def __init__(self, db_path: str | Path, task_repo: TaskRepository):
        self.db_path = str(db_path)
        self.task_repo = task_repo
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "WorkshopRepository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 归一任务实际行 + 映射来源快照
    # ------------------------------------------------------------------ #
    def _task_rows(self, task_id: str) -> tuple[list[dict], list[dict]]:
        try:
            self.task_repo.get_task(task_id)
        except CatalogError:
            raise OrderRejected(f"归一任务 {task_id!r} 不存在", reason="unknown-task") from None
        return self.task_repo.get_rows(task_id), self.task_repo.get_lineage(task_id)

    def _snapshot_task(self, task_id: str, rows: list[dict], lineage: list[dict]) -> None:
        if self.conn.execute(
            "SELECT 1 FROM workshop_task_row WHERE task_id = ? LIMIT 1", (task_id,)
        ).fetchone():
            # 同一任务行只快照一次；若任务被重跑导致行变化，拒绝混用（旧裁单持旧快照）
            existing = self.conn.execute(
                "SELECT row_json FROM workshop_task_row WHERE task_id = ? ORDER BY row_no", (task_id,)
            ).fetchall()
            current = [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows]
            stored = [r["row_json"] for r in existing]
            if current != stored:
                raise WorkshopConflict(
                    f"任务 {task_id!r} 的实际行与历史快照不一致；请用新编号另办新单"
                )
            return
        payload = [
            (
                task_id,
                idx,
                json.dumps(row, ensure_ascii=False, sort_keys=True),
                json.dumps(lineage, ensure_ascii=False),
            )
            for idx, row in enumerate(rows)
        ]
        self.conn.executemany(
            "INSERT INTO workshop_task_row(task_id, row_no, row_json, lineage_json)"
            " VALUES(?, ?, ?, ?)",
            payload,
        )

    def _load_rolls(self, roll_task_id: str) -> tuple[int, tuple[RollSpec, ...], list[dict], list[dict]]:
        rows, lineage = self._task_rows(roll_task_id)
        if not rows:
            raise OrderRejected(f"卷料任务 {roll_task_id!r} 没有实际行", reason="empty-task")
        required = {COL_ROLL_ID, COL_PERIOD, COL_DIRECTION, COL_MARKER, COL_START, COL_END}
        missing = required.difference(rows[0].keys())
        if missing:
            raise OrderRejected(
                f"卷料任务 {roll_task_id!r} 缺列 {sorted(missing)}（列映射目标列须覆盖）",
                reason="missing-column",
            )

        grouped: dict[str, dict] = {}
        for row_no, row in enumerate(rows):
            roll_id = str(row[COL_ROLL_ID]).strip()
            if not roll_id:
                raise OrderRejected(f"第 {row_no + 2} 行卷号为空", reason="empty-roll-id")
            period = _as_int(row[COL_PERIOD], COL_PERIOD, row_no)
            direction = str(row[COL_DIRECTION]).strip()
            if direction not in DIRECTIONS:
                raise OrderRejected(
                    f"第 {row_no + 2} 行方向 {direction!r} 非法，允许 {DIRECTIONS}",
                    reason="bad-direction",
                )
            marker = _as_int(row[COL_MARKER], COL_MARKER, row_no)
            start = _as_int(row[COL_START], COL_START, row_no)
            end = _as_int(row[COL_END], COL_END, row_no)
            entry = grouped.setdefault(
                roll_id,
                {"period": period, "direction": direction, "marker": marker, "intervals": []},
            )
            if entry["period"] != period or entry["direction"] != direction or entry["marker"] != marker:
                raise OrderRejected(
                    f"卷 {roll_id!r} 多行的周期/方向/标记相位不一致", reason="roll-fact-conflict"
                )
            entry["intervals"].append((start, end))

        rolls: list[RollSpec] = []
        periods = set()
        for roll_id, entry in grouped.items():
            intervals = tuple(sorted(entry["intervals"]))
            roll = RollSpec(
                roll_id=roll_id,
                period=entry["period"],
                direction=entry["direction"],
                marker_phase=entry["marker"],
                intervals=intervals,
            )
            roll.validate()
            periods.add(roll.period)
            rolls.append(roll)
        if len(periods) != 1:
            raise OrderRejected("卷料任务内各卷花纹周期不一致", reason="period-mismatch")
        return next(iter(periods)), tuple(rolls), rows, lineage

    def _load_scenarios(
        self, wall_task_id: str, period: int
    ) -> tuple[tuple[Scenario, ...], list[dict], list[dict]]:
        rows, lineage = self._task_rows(wall_task_id)
        if not rows:
            raise OrderRejected(f"墙面任务 {wall_task_id!r} 没有实际行", reason="empty-task")
        required = {
            COL_SCENARIO,
            COL_WALL_ID,
            COL_HEIGHT,
            COL_FIRST_BEAT,
            COL_FROM_WALL,
            COL_TO_WALL,
            COL_OFFSET,
            COL_PRE_MOUNTED,
        }
        missing = required.difference(rows[0].keys())
        if missing:
            raise OrderRejected(
                f"墙面任务 {wall_task_id!r} 缺列 {sorted(missing)}（列映射目标列须覆盖）",
                reason="missing-column",
            )

        scenario_names: list[str] = []
        walls_by_sc: dict[str, dict[str, WallSpec]] = {}
        seams_by_sc: dict[str, list[SeamSpec]] = {}
        for row_no, row in enumerate(rows):
            sc_name = str(row[COL_SCENARIO]).strip() or DEFAULT_SCENARIO
            if sc_name not in walls_by_sc:
                walls_by_sc[sc_name] = {}
                seams_by_sc[sc_name] = []
                scenario_names.append(sc_name)
            wall_id = str(row[COL_WALL_ID]).strip()
            if not wall_id:
                raise OrderRejected(f"第 {row_no + 2} 行墙号为空", reason="empty-wall-id")
            height = _as_int(row[COL_HEIGHT], COL_HEIGHT, row_no)
            first_beat = _opt_int(row[COL_FIRST_BEAT], COL_FIRST_BEAT, row_no)
            pre_mounted = str(row[COL_PRE_MOUNTED]).strip() == "是"
            from_wall = str(row[COL_FROM_WALL]).strip()
            to_wall = str(row[COL_TO_WALL]).strip()
            # 接缝行（带 from/to）不重复定义墙，只登记接缝
            if not from_wall or not to_wall:
                if wall_id in walls_by_sc[sc_name]:
                    raise OrderRejected(
                        f"现场 {sc_name!r} 墙 {wall_id!r} 重复定义", reason="duplicate-wall"
                    )
                walls_by_sc[sc_name][wall_id] = WallSpec(
                    wall_id=wall_id,
                    height=height,
                    first_beat=first_beat,
                    pre_mounted=pre_mounted,
                )
            else:
                offset = _as_int(row[COL_OFFSET], COL_OFFSET, row_no)
                seams_by_sc[sc_name].append(SeamSpec(from_wall, to_wall, offset))

        scenarios: list[Scenario] = []
        for name in scenario_names:
            scenarios.append(
                Scenario(
                    name=name,
                    walls=tuple(walls_by_sc[name].values()),
                    seams=tuple(seams_by_sc[name]),
                )
            )
        return tuple(scenarios), rows, lineage

    # ------------------------------------------------------------------ #
    # 占用：已担保订单领走的卷坐标（跨订单不重复领）
    # ------------------------------------------------------------------ #
    def current_occupancy(self) -> dict[str, list[tuple[int, int]]]:
        rows = self.conn.execute(
            "SELECT roll_id, start_coord, end_coord FROM workshop_occupancy ORDER BY roll_id, start_coord"
        ).fetchall()
        occupied: dict[str, list[tuple[int, int]]] = {}
        for r in rows:
            occupied.setdefault(r["roll_id"], []).append((r["start_coord"], r["end_coord"]))
        return occupied

    def remaining_intervals(self, roll: RollSpec) -> tuple[tuple[int, int], ...]:
        """按真实占用求某卷当前还能整段领走的连续区间。"""
        return roll_remaining_intervals(roll, self.current_occupancy().get(roll.roll_id, []))

    # ------------------------------------------------------------------ #
    def place_order(
        self,
        order_id: str,
        roll_task_id: str,
        wall_task_id: str,
        wall_order: list[str] | tuple[str, ...],
        delivery_mode: str,
    ) -> OrderReceipt:
        """工坊接单：生成所选工艺的裁装回单；输入不合法整单不落库。"""
        if delivery_mode not in DELIVERY_MODES:
            raise OrderRejected(
                f"交货办法必须是 {DELIVERY_MODES} 之一，实际 {delivery_mode!r}",
                reason="bad-delivery-mode",
            )
        if self.conn.execute(
            "SELECT 1 FROM workshop_order WHERE order_id = ?", (order_id,)
        ).fetchone():
            raise WorkshopConflict(f"裁装单 {order_id!r} 已存在")

        period, rolls, roll_rows, roll_lineage = self._load_rolls(roll_task_id)
        scenarios, wall_rows, wall_lineage = self._load_scenarios(wall_task_id, period)
        spec = WorkshopSpec(
            period=period,
            rolls=rolls,
            scenarios=scenarios,
            wall_order=tuple(wall_order),
        )
        spec.validate()

        occupied = self.current_occupancy()
        plans = self._solve(spec, occupied, delivery_mode)
        guaranteed = all(p.guaranteed for p in plans)

        created_at = datetime.now(timezone.utc).isoformat()
        try:
            self._snapshot_task(roll_task_id, roll_rows, roll_lineage)
            self._snapshot_task(wall_task_id, wall_rows, wall_lineage)
            self.conn.execute(
                "INSERT INTO workshop_order(order_id, created_at, delivery_mode, roll_task_id,"
                " wall_task_id, period, wall_order_json, guaranteed, scenarios_json,"
                " roll_snapshot_json, wall_snapshot_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    created_at,
                    delivery_mode,
                    roll_task_id,
                    wall_task_id,
                    period,
                    json.dumps(list(wall_order), ensure_ascii=False),
                    1 if guaranteed else 0,
                    json.dumps([p.as_dict() for p in plans], ensure_ascii=False),
                    json.dumps(roll_rows, ensure_ascii=False, sort_keys=True),
                    json.dumps(wall_rows, ensure_ascii=False, sort_keys=True),
                ),
            )
            for plan in plans:
                for wall_id, seg in plan.assignments.items():
                    self.conn.execute(
                        "INSERT INTO workshop_order_assignment(order_id, scenario, wall_id,"
                        " roll_id, start_coord, end_coord, first_beat, last_beat)"
                        " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            order_id,
                            plan.scenario,
                            wall_id,
                            seg.roll_id if seg else None,
                            seg.start if seg else None,
                            seg.end if seg else None,
                            seg.first_beat if seg else None,
                            seg.last_beat if seg else None,
                        ),
                    )
                for seq, b in enumerate(plan.blockages):
                    self.conn.execute(
                        "INSERT INTO workshop_order_blockage(order_id, scenario, seq, kind,"
                        " detail, wall_id, seam_json, roll_id, segment_json)"
                        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            order_id,
                            plan.scenario,
                            seq,
                            b.kind,
                            b.detail,
                            b.wall_id,
                            json.dumps(list(b.seam), ensure_ascii=False) if b.seam else None,
                            b.roll_id,
                            json.dumps(list(b.segment), ensure_ascii=False) if b.segment else None,
                        ),
                    )
            # 只有能担保的单才把领纸坐标记为真实占用；不能承诺的单不动库内余料
            if guaranteed:
                for plan in plans:
                    for wall_id, seg in plan.assignments.items():
                        if seg is None:
                            continue
                        self.conn.execute(
                            "INSERT INTO workshop_occupancy(order_id, scenario, roll_id,"
                            " start_coord, end_coord, wall_id) VALUES(?, ?, ?, ?, ?, ?)",
                            (
                                order_id,
                                plan.scenario,
                                seg.roll_id,
                                seg.start,
                                seg.end,
                                wall_id,
                            ),
                        )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return OrderReceipt(
            order_id=order_id,
            delivery_mode=delivery_mode,
            roll_task_id=roll_task_id,
            wall_task_id=wall_task_id,
            period=period,
            wall_order=tuple(wall_order),
            guaranteed=guaranteed,
            scenarios=plans,
            created_at=created_at,
        )

    def _solve(
        self, spec: WorkshopSpec, occupied: dict[str, list[tuple[int, int]]], delivery_mode: str
    ) -> list[ScenarioPlan]:
        if delivery_mode == MODE_SEALED:
            return self._solve_sealed(spec, occupied)
        return self._solve_whole_roll(spec, occupied)

    def _solve_sealed(
        self, spec: WorkshopSpec, occupied: dict[str, list[tuple[int, int]]]
    ) -> list[ScenarioPlan]:
        """封签：第一现场求出的固定指派，其余现场只能照单验收（不能翻头/另开卷）。"""
        base = plan_scenario(spec, spec.scenarios[0], occupied)
        if not base.guaranteed:
            return [
                base if sc.name == base.scenario
                else self._fixed_rejection(spec, sc, base.assignments, occupied)
                for sc in spec.scenarios
            ]
        fixed = dict(base.assignments)
        plans = [base]
        for sc in spec.scenarios[1:]:
            plans.append(plan_scenario(spec, sc, occupied, fixed=fixed))
        return plans

    def _fixed_rejection(
        self,
        spec: WorkshopSpec,
        scenario: Scenario,
        fixed: dict[str, CutSegment | None],
        occupied: dict[str, list[tuple[int, int]]],
    ) -> ScenarioPlan:
        plan = plan_scenario(spec, scenario, occupied, fixed=fixed)
        if plan.guaranteed:
            # 固定指派本可行但基底现场失败：封签单整体仍不能担保，记录依赖卡点
            return ScenarioPlan(
                scenario.name,
                False,
                dict(fixed),
                [Blockage("seam", "封签指派在基底现场已失败，本现场无法随车交付")],
            )
        return plan

    def _solve_whole_roll(
        self, spec: WorkshopSpec, occupied: dict[str, list[tuple[int, int]]]
    ) -> list[ScenarioPlan]:
        """整卷：各现场独立从余卷选段下刀，逐现场判断能否兑现。"""
        return [plan_scenario(spec, sc, occupied) for sc in spec.scenarios]

    # ------------------------------------------------------------------ #
    # 复看
    # ------------------------------------------------------------------ #
    def list_orders(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT order_id, created_at, delivery_mode, guaranteed, roll_task_id, wall_task_id"
            " FROM workshop_order ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_order(self, order_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM workshop_order WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise CatalogError(f"裁装单 {order_id!r} 不存在")
        assignments = self.conn.execute(
            "SELECT scenario, wall_id, roll_id, start_coord, end_coord, first_beat, last_beat"
            " FROM workshop_order_assignment WHERE order_id = ? ORDER BY scenario, wall_id",
            (order_id,),
        ).fetchall()
        blockages = self.conn.execute(
            "SELECT scenario, kind, detail, wall_id, seam_json, roll_id, segment_json"
            " FROM workshop_order_blockage WHERE order_id = ? ORDER BY scenario, seq",
            (order_id,),
        ).fetchall()
        return {
            "order_id": row["order_id"],
            "created_at": row["created_at"],
            "delivery_mode": row["delivery_mode"],
            "roll_task_id": row["roll_task_id"],
            "wall_task_id": row["wall_task_id"],
            "period": row["period"],
            "wall_order": json.loads(row["wall_order_json"]),
            "guaranteed": bool(row["guaranteed"]),
            "scenarios_json": row["scenarios_json"],
            "roll_snapshot": json.loads(row["roll_snapshot_json"]),
            "wall_snapshot": json.loads(row["wall_snapshot_json"]),
            "assignments": [dict(a) for a in assignments],
            "blockages": [
                {
                    **dict(b),
                    "seam": json.loads(b["seam_json"]) if b["seam_json"] else None,
                    "segment": json.loads(b["segment_json"]) if b["segment_json"] else None,
                }
                for b in blockages
            ],
        }

    def get_task_snapshot(self, task_id: str) -> dict:
        """领班复看某裁单当时所用归一任务实际行与映射来源。"""
        rows = self.conn.execute(
            "SELECT row_no, row_json, lineage_json FROM workshop_task_row WHERE task_id = ?"
            " ORDER BY row_no",
            (task_id,),
        ).fetchall()
        if not rows:
            raise CatalogError(f"工坊库内没有任务 {task_id!r} 的快照")
        return {
            "task_id": task_id,
            "rows": [json.loads(r["row_json"]) for r in rows],
            "lineage": json.loads(rows[0]["lineage_json"]),
        }

    def realize_scenario(self, order_id: str, scenario: str) -> dict:
        """现场接班：实销揭晓后报该现场承诺是否兑现、实卷哪端还在。"""
        order = self.get_order(order_id)
        plan_rows = [p for p in json.loads(order["scenarios_json"]) if p["scenario"] == scenario]
        if not plan_rows:
            raise CatalogError(f"裁装单 {order_id!r} 无现场 {scenario!r}")
        plan = plan_rows[0]
        leftovers: dict[str, list[tuple[int, int]]] = {}
        if plan["guaranteed"]:
            roll_specs = {r.roll_id: r for r in self._rolls_from_snapshot(order)}
            used: dict[str, list[tuple[int, int]]] = {}
            for wall_id, seg in plan["assignments"].items():
                if seg is None:
                    continue
                used.setdefault(seg["roll_id"], []).append((seg["start"], seg["end"]))
            for roll_id, ivs in used.items():
                leftovers[roll_id] = [
                    list(iv) for iv in roll_remaining_intervals(roll_specs[roll_id], ivs)
                ]
        return {
            "order_id": order_id,
            "scenario": scenario,
            "guaranteed": plan["guaranteed"],
            "assignments": plan["assignments"],
            "blockages": plan["blockages"],
            "leftover_intervals": leftovers,
        }

    def _rolls_from_snapshot(self, order: dict) -> tuple[RollSpec, ...]:
        grouped: dict[str, dict] = {}
        for row in order["roll_snapshot"]:
            rid = str(row[COL_ROLL_ID]).strip()
            entry = grouped.setdefault(
                rid,
                {
                    COL_PERIOD: _snapshot_int(row[COL_PERIOD]),
                    COL_DIRECTION: row[COL_DIRECTION],
                    COL_MARKER: _snapshot_int(row[COL_MARKER]),
                    "intervals": [],
                },
            )
            entry["intervals"].append(
                (_snapshot_int(row[COL_START]), _snapshot_int(row[COL_END]))
            )
        return tuple(
            RollSpec(
                roll_id=rid,
                period=e[COL_PERIOD],
                direction=e[COL_DIRECTION],
                marker_phase=e[COL_MARKER],
                intervals=tuple(sorted(e["intervals"])),
            )
            for rid, e in grouped.items()
        )
