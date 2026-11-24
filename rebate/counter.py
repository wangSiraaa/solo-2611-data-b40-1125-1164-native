"""社区废纸返券柜台事实仓 (rebate counter fact store).

柜台把 :mod:`rebate.network` 的收集网络落成可回查的 SQLite 事实，并叠加
经营层规则。它与过磅事实仓同构：*只追加、不覆盖*，称量纠正新增净重版本
与复核记录，旧券、旧回执、旧预算扣减都是历史事实。

经营层语义
----------
* **核算期** :meth:`Counter.open_period`：带减量线 ``reduction_line`` 与
  领券期限 ``claim_deadline``。街道可 :meth:`close_period` 统一封账，
  封账后不再签新券，只能 :meth:`open_dispute` 开 *差额争议*（只记差额，
  绝不在原券旁另兑一遍）。
* **预算**：可统一（CENTER）或拆到各期、各站
  (:meth:`allocate_budget`)。签出一组券时在同一事务内 *锁住* 相应预算；
  预算为零或不足只能挂起/拒签。
* **两种经营办法** ``mode``：

  - ``UNIFIED`` 中心统一封账：中心获准看凑成同车总数的全部局部称量，
    可在住户重量被全局证据唯一证实时代住户签券；
  - ``STATION`` 分站断网办理：分站只能凭 *本站或住户本人* 当时能看见的
    资料办理；看不到别户私重或另一站领取流水，证据不足先留单。
* **领券权（跨站令牌）**：每住户每期一枚，状态机
  ``HELD -> HANDED_OVER -> RECEIVED``。跨站时原站先
  :meth:`hand_over` 交出，收件未到时两站都不能花；接收站
  :meth:`receive_right` 接收后可办，原站即便补送一遍也不能恢复。
* **回执**：每次申请都产生带快照的回执（含经办方 *当时有权看见* 的
  证据、全部可行世界、证实重量、签出或挂起原因、扣到哪期哪户哪站）。
  按角色查询时自动剔除无权查看的私重与别站流水。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from .network import (
    ORIGIN_CENTER,
    ORIGIN_STATION,
    CollectionNetwork,
    PartialWeigh,
    SolveResult,
)

# 容量（题面：最多 3 户、2 站）。
MAX_HOUSEHOLDS = 3
MAX_STATIONS = 2

MODE_UNIFIED = "UNIFIED"
MODE_STATION = "STATION"
MODES = (MODE_UNIFIED, MODE_STATION)

CENTER_STATION = None  # 中心视角的 station_id

# 领券权状态机
RIGHT_HELD = "HELD"                # 本站持有，可办
RIGHT_HANDED_OVER = "HANDED_OVER"  # 已交出，收件未到；两站都不能花
RIGHT_RECEIVED = "RECEIVED"        # 接收站已收到，可办
RIGHT_SPENT = "SPENT"              # 已随签券核销

# 申请 / 券结果状态
RESULT_ISSUED = "ISSUED"
RESULT_SUSPENDED = "SUSPENDED"
RESULT_REJECTED = "REJECTED"
RESULT_DISPUTE = "DISPUTE"

# 挂起 / 拒签原因（回执码）
REASON_WEIGHT_UNPROVEN = "WEIGHT_UNPROVEN"
REASON_BELOW_LINE = "BELOW_REDUCTION_LINE"
REASON_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
REASON_CLAIM_DEADLINE_PASSED = "CLAIM_DEADLINE_PASSED"
REASON_PERIOD_CLOSED = "PERIOD_CLOSED"
REASON_NO_RIGHT_AT_STATION = "NO_CLAIM_RIGHT_AT_STATION"
REASON_RIGHT_IN_TRANSIT = "CLAIM_RIGHT_IN_TRANSIT"
REASON_NETWORK_INFEASIBLE = "NETWORK_INFEASIBLE"
REASON_DUPLICATE_CLAIM = "DUPLICATE_CLAIM"
REASON_ISSUED_OK = "ISSUED_OK"
REASON_POST_CLOSURE_DELTA = "POST_CLOSURE_DELTA"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS period (
    period_id        TEXT PRIMARY KEY,
    reduction_line   INTEGER NOT NULL CHECK (reduction_line >= 0),
    claim_deadline   INTEGER NOT NULL,
    opened_at        INTEGER NOT NULL,
    closed_at        INTEGER,
    budget_mode      TEXT NOT NULL CHECK (budget_mode IN ('UNIFIED','STATION'))
);

CREATE TABLE IF NOT EXISTS station (
    station_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS household (
    household_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS bag (
    bag_id        TEXT PRIMARY KEY,
    household_id  TEXT NOT NULL REFERENCES household(household_id),
    box_id        TEXT NOT NULL,
    gross         INTEGER NOT NULL CHECK (gross >= 0),
    tare_lo       INTEGER NOT NULL CHECK (tare_lo >= 0),
    tare_hi       INTEGER NOT NULL CHECK (tare_hi >= tare_lo),
    allowed_paper TEXT
);

CREATE TABLE IF NOT EXISTS box (
    box_id            TEXT PRIMARY KEY,
    trip_id           TEXT NOT NULL,
    include_in_trip   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS trip_version (
    trip_id              TEXT NOT NULL,
    version              INTEGER NOT NULL,
    lo                   INTEGER NOT NULL,
    hi                   INTEGER NOT NULL,
    predecessor_version  INTEGER,
    source               TEXT NOT NULL DEFAULT '',
    active               INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (trip_id, version)
);

CREATE TABLE IF NOT EXISTS transfer (
    source_box_id TEXT PRIMARY KEY REFERENCES box(box_id),
    dest_box_id   TEXT NOT NULL REFERENCES box(box_id)
);

CREATE TABLE IF NOT EXISTS partial_weigh (
    weigh_id     TEXT PRIMARY KEY,
    node_kind    TEXT NOT NULL CHECK (node_kind IN ('bag','box','trip','household')),
    node_id      TEXT NOT NULL,
    lo           INTEGER NOT NULL CHECK (lo >= 0),
    hi           INTEGER NOT NULL CHECK (hi >= lo),
    origin       TEXT NOT NULL CHECK (origin IN ('CENTER','STATION')),
    station_id   TEXT,
    household_id TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    predecessor  TEXT
);

CREATE TABLE IF NOT EXISTS claim_right (
    household_id TEXT NOT NULL,
    period_id    TEXT NOT NULL REFERENCES period(period_id),
    state        TEXT NOT NULL CHECK (state IN
                 ('HELD','HANDED_OVER','RECEIVED','SPENT')),
    holder_station TEXT,
    updated_order INTEGER NOT NULL,
    PRIMARY KEY (household_id, period_id)
);

CREATE TABLE IF NOT EXISTS right_event (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id TEXT NOT NULL,
    period_id    TEXT NOT NULL,
    from_state   TEXT,
    to_state     TEXT NOT NULL,
    station_id   TEXT,
    msg_id       TEXT,
    note         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS right_event_hp ON right_event(household_id, period_id);

CREATE TABLE IF NOT EXISTS application (
    receipt_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id    TEXT NOT NULL REFERENCES period(period_id),
    household_id TEXT NOT NULL REFERENCES household(household_id),
    station_id   TEXT,
    result       TEXT NOT NULL CHECK (result IN
                 ('ISSUED','SUSPENDED','REJECTED','DISPUTE')),
    reason       TEXT NOT NULL,
    coupon_value INTEGER NOT NULL,
    at_time      INTEGER NOT NULL,
    right_state  TEXT NOT NULL,
    trip_versions TEXT NOT NULL,
    evidence     TEXT NOT NULL,
    solve        TEXT NOT NULL,
    budget_after TEXT NOT NULL,
    predecessor_receipt INTEGER REFERENCES application(receipt_id)
);
CREATE INDEX IF NOT EXISTS application_hp ON application(period_id, household_id);

CREATE TABLE IF NOT EXISTS budget (
    period_id  TEXT NOT NULL REFERENCES period(period_id),
    station_id TEXT,
    amount     INTEGER NOT NULL,
    PRIMARY KEY (period_id, station_id)
);

CREATE TABLE IF NOT EXISTS budget_ledger (
    ledger_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id  TEXT NOT NULL,
    station_id TEXT,
    delta      INTEGER NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('ALLOCATE','LOCK','RELEASE')),
    receipt_id INTEGER REFERENCES application(receipt_id),
    note       TEXT NOT NULL DEFAULT ''
);
"""


class CounterError(ValueError):
    """柜台输入或状态错误。"""


@dataclass(frozen=True)
class Decision:
    """一次申请的结构化结论。"""

    receipt_id: int
    result: str
    reason: str
    coupon_value: int
    proven: Optional[int]
    reduction_line: int
    right_state: str
    budget_remaining: int
    solve: SolveResult
    evidence: dict


# --------------------------------------------------------------------- 柜台
class Counter:
    """SQLite 支撑的返券柜台；每个 SQLite 文件即一个本站（或中心）。"""

    def __init__(self, path: str = ":memory:", mode: Optional[str] = None) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self.conn.executescript(SCHEMA)
        if mode is None:
            # 重开已有库：从首个核算期恢复经营模式；空库默认统一模式。
            row = self.conn.execute(
                "SELECT budget_mode FROM period ORDER BY period_id LIMIT 1"
            ).fetchone()
            mode = MODE_UNIFIED if row is None else row["budget_mode"]
        if mode not in MODES:
            raise CounterError(f"mode must be one of {MODES}")
        self.mode = mode

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Counter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ------------------------------------------------------------- 基础登记
    def register_station(self, station_id: str) -> None:
        if self._count("station") >= MAX_STATIONS:
            raise CounterError(f"station limit {MAX_STATIONS} exceeded")
        with self.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO station(station_id) VALUES (?)", (station_id,)
            )

    def register_household(self, household_id: str) -> None:
        if self._count("household") >= MAX_HOUSEHOLDS:
            raise CounterError(f"household limit {MAX_HOUSEHOLDS} exceeded")
        with self.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO household(household_id) VALUES (?)",
                (household_id,),
            )

    def _count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])

    def open_period(
        self,
        period_id: str,
        reduction_line: int,
        claim_deadline: int,
        *,
        opened_at: int = 0,
        central_budget: Optional[int] = None,
    ) -> None:
        if reduction_line < 0:
            raise CounterError("reduction_line must be >= 0")
        with self.transaction() as c:
            if c.execute(
                "SELECT 1 FROM period WHERE period_id=?", (period_id,)
            ).fetchone():
                raise CounterError(f"period {period_id} already exists")
            c.execute(
                "INSERT INTO period(period_id, reduction_line, claim_deadline, opened_at, "
                "budget_mode) VALUES (?,?,?,?,?)",
                (period_id, int(reduction_line), int(claim_deadline),
                 int(opened_at), self.mode),
            )
            # UNIFIED 的 central_budget 进中心池；STATION 必须显式 allocate_budget
            # 拆到各站，避免分站意外花到它看不见的中心池。
            if central_budget is not None and self.mode == MODE_UNIFIED:
                c.execute(
                    "INSERT INTO budget(period_id, station_id, amount) VALUES (?,?,?)",
                    (period_id, CENTER_STATION, int(central_budget)),
                )

    def close_period(self, period_id: str, at_time: Optional[int] = None) -> None:
        with self.transaction() as c:
            row = c.execute(
                "SELECT closed_at, claim_deadline FROM period WHERE period_id=?",
                (period_id,),
            ).fetchone()
            if row is None:
                raise CounterError(f"unknown period {period_id}")
            if row["closed_at"] is not None:
                return  # 封账幂等
            closed_at = int(row["claim_deadline"] if at_time is None else at_time)
            c.execute(
                "UPDATE period SET closed_at=? WHERE period_id=?",
                (closed_at, period_id),
            )

    def is_closed(self, period_id: str) -> bool:
        row = self.conn.execute(
            "SELECT closed_at FROM period WHERE period_id=?", (period_id,)
        ).fetchone()
        if row is None:
            raise CounterError(f"unknown period {period_id}")
        return row["closed_at"] is not None

    # ------------------------------------------------------------- 预算
    def allocate_budget(
        self, period_id: str, amount: int, station_id: Optional[str] = CENTER_STATION
    ) -> None:
        """向某期（中心池 station_id=None）或某站池追加预算。"""
        if amount < 0:
            raise CounterError("budget amount must be >= 0")
        with self.transaction() as c:
            self._require_period(c, period_id)
            if station_id is not None and not c.execute(
                "SELECT 1 FROM station WHERE station_id=?", (station_id,)
            ).fetchone():
                raise CounterError(f"unknown station {station_id}")
            c.execute(
                "INSERT INTO budget(period_id, station_id, amount) VALUES (?,?,?) "
                "ON CONFLICT(period_id, station_id) DO UPDATE SET "
                "amount = budget.amount + excluded.amount",
                (period_id, station_id, int(amount)),
            )
            c.execute(
                "INSERT INTO budget_ledger(period_id, station_id, delta, kind, note) "
                "VALUES (?,?,?,'ALLOCATE',?)",
                (period_id, station_id, int(amount), "allocate"),
            )

    def budget_remaining(self, period_id: str, station_id: Optional[str]) -> int:
        """某站 *当下能花* 的额度。

        UNIFIED：中心池与站池共用（中心统一调度）；STATION：分站只能花
        明确拆到本站的预算，看不到也花不到中心池/别站池。
        """
        if self.mode == MODE_UNIFIED:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS n FROM budget WHERE period_id=?",
                (period_id,),
            ).fetchone()
            return int(row["n"])
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS n FROM budget "
            "WHERE period_id=? AND station_id IS ?",
            (period_id, station_id),
        ).fetchone()
        return int(row["n"])

    def _lock_budget(
        self,
        c: sqlite3.Connection,
        period_id: str,
        station_id: Optional[str],
        amount: int,
        receipt_id: int,
    ) -> None:
        # UNIFIED 先锁站池再锁中心池；STATION 只锁本站池（看不到中心池）。
        pools = (station_id, CENTER_STATION) if self.mode == MODE_UNIFIED else (station_id,)
        remaining_need = amount
        for pool in pools:
            if remaining_need <= 0:
                break
            row = c.execute(
                "SELECT amount FROM budget WHERE period_id=? AND station_id IS ?",
                (period_id, pool),
            ).fetchone()
            if row is None:
                continue
            take = min(int(row["amount"]), remaining_need)
            if take <= 0:
                continue
            c.execute(
                "UPDATE budget SET amount=amount-? WHERE period_id=? AND station_id IS ?",
                (take, period_id, pool),
            )
            c.execute(
                "INSERT INTO budget_ledger(period_id, station_id, delta, kind, receipt_id, note) "
                "VALUES (?,?,?,'LOCK',?,?)",
                (period_id, pool, -take, receipt_id, f"locked for receipt {receipt_id}"),
            )
            remaining_need -= take
        if remaining_need > 0:
            raise CounterError(REASON_BUDGET_EXHAUSTED)

    # ------------------------------------------------------------- 收集事实
    def add_bag(
        self,
        bag_id: str,
        household_id: str,
        box_id: str,
        gross: int,
        tare_lo: int = 0,
        tare_hi: int = 0,
        allowed_paper: Optional[tuple[int, ...]] = None,
    ) -> None:
        self.register_household(household_id)
        stored_allowed = None if allowed_paper is None else json.dumps(
            sorted(set(int(v) for v in allowed_paper))
        )
        with self.transaction() as c:
            if not c.execute(
                "SELECT 1 FROM box WHERE box_id=?", (box_id,)
            ).fetchone():
                raise CounterError(f"box {box_id} must be registered before its bags")
            if c.execute("SELECT 1 FROM bag WHERE bag_id=?", (bag_id,)).fetchone():
                raise CounterError(f"bag {bag_id} already exists")
            c.execute(
                "INSERT INTO bag(bag_id, household_id, box_id, gross, tare_lo, tare_hi, "
                "allowed_paper) VALUES (?,?,?,?,?,?,?)",
                (bag_id, household_id, box_id, int(gross), int(tare_lo), int(tare_hi),
                 stored_allowed),
            )

    def add_box(self, box_id: str, trip_id: str, include_in_trip: bool = True) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO box(box_id, trip_id, include_in_trip) "
                "VALUES (?,?,?)",
                (box_id, trip_id, int(bool(include_in_trip))),
            )

    def add_trip_version(
        self,
        trip_id: str,
        lo: int,
        hi: int,
        *,
        source: str = "",
        predecessor_trip: str = "",
    ) -> int:
        """登记车次净重的新版本；旧活动版本自动归档（active=0），永不删除。"""
        if lo < 0 or hi < lo:
            raise CounterError(f"invalid net interval [{lo},{hi}]")
        with self.transaction() as c:
            prev = c.execute(
                "SELECT version FROM trip_version WHERE trip_id=? AND active=1",
                (trip_id,),
            ).fetchone()
            version = 1 if prev is None else int(prev["version"]) + 1
            predecessor_version = None if prev is None else int(prev["version"])
            c.execute(
                "UPDATE trip_version SET active=0 WHERE trip_id=? AND active=1",
                (trip_id,),
            )
            c.execute(
                "INSERT INTO trip_version(trip_id, version, lo, hi, predecessor_version, "
                "source, active) VALUES (?,?,?,?,?,?,1)",
                (trip_id, version, int(lo), int(hi), predecessor_version,
                 source or predecessor_trip),
            )
            return version

    def add_transfer(self, source_box_id: str, dest_box_id: str) -> None:
        with self.transaction() as c:
            s = c.execute(
                "SELECT trip_id FROM box WHERE box_id=?", (source_box_id,)
            ).fetchone()
            d = c.execute(
                "SELECT trip_id FROM box WHERE box_id=?", (dest_box_id,)
            ).fetchone()
            if s is None or d is None:
                raise CounterError("transfer references unknown box")
            if s["trip_id"] == d["trip_id"]:
                raise CounterError("a sealed transfer must move across different trips")
            c.execute(
                "INSERT OR IGNORE INTO transfer(source_box_id, dest_box_id) VALUES (?,?)",
                (source_box_id, dest_box_id),
            )
            # 同一封箱转车只归一次：源箱移出原车净重。
            c.execute(
                "UPDATE box SET include_in_trip=0 WHERE box_id=?", (source_box_id,)
            )

    def add_partial(self, partial: PartialWeigh) -> None:
        with self.transaction() as c:
            if c.execute(
                "SELECT 1 FROM partial_weigh WHERE weigh_id=?", (partial.weigh_id,)
            ).fetchone():
                raise CounterError(f"partial weigh {partial.weigh_id} already exists")
            if partial.origin == ORIGIN_STATION and not partial.station_id:
                raise CounterError("a STATION partial needs a station_id")
            c.execute(
                "INSERT INTO partial_weigh(weigh_id, node_kind, node_id, lo, hi, origin, "
                "station_id, household_id) VALUES (?,?,?,?,?,?,?,?)",
                (
                    partial.weigh_id, partial.node_kind, partial.node_id,
                    int(partial.lo), int(partial.hi), partial.origin,
                    partial.station_id, partial.household_id,
                ),
            )
            if partial.household_id:
                c.execute(
                    "INSERT OR IGNORE INTO household(household_id) VALUES (?)",
                    (partial.household_id,),
                )

    def correct_partial(
        self,
        old_weigh_id: str,
        new_weigh_id: str,
        lo: int,
        hi: int,
        *,
        note: str = "",
    ) -> PartialWeigh:
        """局部称量纠正：旧实名凭条归档（active=0），新版本生效。

        与车次净重版本链同构——只追加、不覆盖；封账前后都允许，但封账后
        只能经 :meth:`open_dispute` 走差额，不会再签新券。
        """
        with self.transaction() as c:
            old = c.execute(
                "SELECT * FROM partial_weigh WHERE weigh_id=? AND active=1",
                (old_weigh_id,),
            ).fetchone()
            if old is None:
                raise CounterError(f"unknown active partial weigh {old_weigh_id}")
            c.execute(
                "UPDATE partial_weigh SET active=0 WHERE weigh_id=?", (old_weigh_id,)
            )
            corrected = PartialWeigh(
                weigh_id=new_weigh_id, node_kind=old["node_kind"],
                node_id=old["node_id"], lo=int(lo), hi=int(hi),
                origin=old["origin"], station_id=old["station_id"],
                household_id=old["household_id"],
            )
            c.execute(
                "INSERT INTO partial_weigh "
                "(weigh_id, node_kind, node_id, lo, hi, origin, station_id, "
                " household_id, active, predecessor) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    corrected.weigh_id,
                    corrected.node_kind,
                    corrected.node_id,
                    corrected.lo,
                    corrected.hi,
                    corrected.origin,
                    corrected.station_id,
                    corrected.household_id,
                    1,
                    old_weigh_id,
                ),
            )
            return corrected

    # ------------------------------------------------------------- 网络重建
    # ------------------------------------------------------------- 网络重建

    # ------------------------------------------------------------- 网络重建
    def _rebuild_network(self, period_id: str) -> CollectionNetwork:
        """从落盘事实重建 *活动版本* 收集网络（旧净重版本仍留档可回查）。"""
        net = CollectionNetwork()
        for r in self.conn.execute("SELECT household_id FROM household ORDER BY household_id"):
            net.add_household(r["household_id"])
        for r in self.conn.execute("SELECT * FROM box ORDER BY box_id"):
            net.add_box(r["box_id"], r["trip_id"], bool(r["include_in_trip"]))
        for r in self.conn.execute("SELECT * FROM bag ORDER BY bag_id"):
            allowed = None if r["allowed_paper"] is None else tuple(
                int(v) for v in json.loads(r["allowed_paper"])
            )
            net.add_bag(
                r["bag_id"], r["household_id"], r["box_id"],
                int(r["gross"]), int(r["tare_lo"]), int(r["tare_hi"]),
                allowed_paper=allowed,
            )
        for r in self.conn.execute(
            "SELECT * FROM trip_version WHERE active=1 ORDER BY trip_id"
        ):
            net.add_trip_version(
                r["trip_id"], int(r["lo"]), int(r["hi"]),
                version=int(r["version"]),
                predecessor_version=r["predecessor_version"],
                source=r["source"],
            )
        for r in self.conn.execute("SELECT * FROM transfer"):
            # add_transfer 会再次把源箱置为不计入；落盘状态已一致。
            net.transfers[r["source_box_id"]] = r["dest_box_id"]
        for r in self.conn.execute(
            "SELECT * FROM partial_weigh WHERE active=1 ORDER BY weigh_id"
        ):
            net.add_partial(
                PartialWeigh(
                    weigh_id=r["weigh_id"], node_kind=r["node_kind"], node_id=r["node_id"],
                    lo=int(r["lo"]), hi=int(r["hi"]), origin=r["origin"],
                    station_id=r["station_id"], household_id=r["household_id"],
                )
            )
        return net

    def trip_versions(self, trip_id: str) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM trip_version WHERE trip_id=? ORDER BY version",
                (trip_id,),
            )
        ]

    def _network_from_snapshot(
        self,
        trip_versions_snapshot: dict,
        partial_rows: Optional[list[dict]] = None,
    ) -> CollectionNetwork:
        """按某张回执 *当时封存* 的净重版本与凭条快照重建历史网络。

        封账后的称量纠正只新增版本、不改写旧回执；核对必须在回执自己的
        历史快照上重算，而不是套用当前活动版本（否则会把可追溯的版本演进
        误判成被篡改）。``partial_rows`` 为该回执视角下当时可见的凭条；
        缺省回退到当前活动凭条。
        """
        net = CollectionNetwork()
        for r in self.conn.execute("SELECT household_id FROM household ORDER BY household_id"):
            net.add_household(r["household_id"])
        for r in self.conn.execute("SELECT * FROM box ORDER BY box_id"):
            net.add_box(r["box_id"], r["trip_id"], bool(r["include_in_trip"]))
        for r in self.conn.execute("SELECT * FROM bag ORDER BY bag_id"):
            allowed = None if r["allowed_paper"] is None else tuple(
                int(v) for v in json.loads(r["allowed_paper"])
            )
            net.add_bag(
                r["bag_id"], r["household_id"], r["box_id"],
                int(r["gross"]), int(r["tare_lo"]), int(r["tare_hi"]),
                allowed_paper=allowed,
            )
        for tid, versions in trip_versions_snapshot.items():
            active = max(versions, key=lambda v: int(v["version"]))
            net.add_trip_version(
                tid, int(active["lo"]), int(active["hi"]),
                version=int(active["version"]),
                predecessor_version=active["predecessor_version"],
                source=active["source"],
            )
        net.transfers = dict(
            (r["source_box_id"], r["dest_box_id"])
            for r in self.conn.execute("SELECT * FROM transfer")
        )
        if partial_rows is None:
            partial_rows = [
                dict(r)
                for r in self.conn.execute(
                    "SELECT * FROM partial_weigh WHERE active=1 ORDER BY weigh_id"
                )
            ]
        for r in partial_rows:
            if "weight" in r:  # 证据快照格式（PartialWeigh.to_dict）
                lo, hi = int(r["weight"][0]), int(r["weight"][1])
            else:
                lo, hi = int(r["lo"]), int(r["hi"])
            net.add_partial(
                PartialWeigh(
                    weigh_id=r["weigh_id"], node_kind=r["node_kind"], node_id=r["node_id"],
                    lo=lo, hi=hi, origin=r["origin"],
                    station_id=r.get("station_id"), household_id=r.get("household_id"),
                )
            )
        return net

    # ------------------------------------------------------------- 领券权
    def _next_order(self, c: sqlite3.Connection) -> int:
        return int(
            c.execute(
                "SELECT COALESCE(MAX(updated_order),0)+1 AS n FROM claim_right"
            ).fetchone()["n"]
        )

    def ensure_right(
        self, period_id: str, household_id: str, holder_station: Optional[str]
    ) -> None:
        """首次出现时按当前持有站建立 HELD 令牌。"""
        with self.transaction() as c:
            self._require_period(c, period_id)
            if c.execute(
                "SELECT 1 FROM claim_right WHERE household_id=? AND period_id=?",
                (household_id, period_id),
            ).fetchone():
                return
            order = self._next_order(c)
            c.execute(
                "INSERT INTO claim_right(household_id, period_id, state, holder_station, "
                "updated_order) VALUES (?,?, 'HELD', ?,?)",
                (household_id, period_id, holder_station, order),
            )
            c.execute(
                "INSERT INTO right_event(household_id, period_id, from_state, to_state, "
                "station_id, note) VALUES (?,?,'HELD','HELD',?,'ensure')",
                (household_id, period_id, holder_station),
            )

    def right_state(self, period_id: str, household_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM claim_right WHERE household_id=? AND period_id=?",
            (household_id, period_id),
        ).fetchone()
        return None if row is None else dict(row)

    def _transition(
        self,
        period_id: str,
        household_id: str,
        to_state: str,
        station_id: Optional[str],
        msg_id: Optional[str],
        note: str,
    ) -> None:
        with self.transaction() as c:
            row = c.execute(
                "SELECT * FROM claim_right WHERE household_id=? AND period_id=?",
                (household_id, period_id),
            ).fetchone()
            if row is None:
                raise CounterError(f"no claim right for {household_id} in {period_id}")
            # 跨站消息幂等：同一 msg_id 的事件重放直接返回，绝不恢复已交出的权。
            if msg_id is not None and c.execute(
                "SELECT 1 FROM right_event WHERE msg_id=? AND household_id=? AND period_id=?",
                (msg_id, household_id, period_id),
            ).fetchone():
                return
            c.execute(
                "INSERT INTO right_event(household_id, period_id, from_state, to_state, "
                "station_id, msg_id, note) VALUES (?,?,?,?,?,?,?)",
                (household_id, period_id, row["state"], to_state, station_id, msg_id, note),
            )
            c.execute(
                "UPDATE claim_right SET state=?, holder_station=?, updated_order=? "
                "WHERE household_id=? AND period_id=?",
                (to_state, station_id, self._next_order(c), household_id, period_id),
            )

    def hand_over(
        self,
        period_id: str,
        household_id: str,
        from_station: Optional[str],
        to_station: Optional[str],
        *,
        msg_id: Optional[str] = None,
    ) -> None:
        """原站交出领券权：HELD/RECEIVED -> HANDED_OVER；在途两站都不能花。"""
        row = self.right_state(period_id, household_id)
        if row is None:
            raise CounterError(f"no claim right for {household_id} in {period_id}")
        if row["state"] == RIGHT_HANDED_OVER:
            # 原站补送一遍：幂等返回，状态不变，不能恢复。
            self._transition(
                period_id, household_id, RIGHT_HANDED_OVER, from_station, msg_id,
                "duplicate hand-over while in transit",
            )
            return
        if row["state"] != RIGHT_HELD and row["state"] != RIGHT_RECEIVED:
            raise CounterError(
                f"right in state {row['state']} cannot be handed over"
            )
        self._transition(
            period_id, household_id, RIGHT_HANDED_OVER, None, msg_id,
            f"{from_station} handed over toward {to_station}",
        )

    def receive_right(
        self,
        period_id: str,
        household_id: str,
        station_id: Optional[str],
        *,
        msg_id: Optional[str] = None,
    ) -> None:
        """接收站收到领券权：HANDED_OVER -> RECEIVED。"""
        row = self.right_state(period_id, household_id)
        if row is None:
            raise CounterError(f"no claim right for {household_id} in {period_id}")
        if row["state"] == RIGHT_RECEIVED and row["holder_station"] == station_id:
            return  # 幂等
        if row["state"] != RIGHT_HANDED_OVER:
            raise CounterError(
                f"right in state {row['state']} cannot be received"
            )
        self._transition(
            period_id, household_id, RIGHT_RECEIVED, station_id, msg_id,
            f"received at {station_id}",
        )

    # ------------------------------------------------------------- 申请/签券
    @staticmethod
    def _require_period(c: sqlite3.Connection, period_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM period WHERE period_id=?", (period_id,)).fetchone()
        if row is None:
            raise CounterError(f"unknown period {period_id}")
        return row

    def _evidence_snapshot(
        self, net: CollectionNetwork, station_id: Optional[str]
    ) -> dict:
        """经办方 *当时有权看见* 的资料；无权私重在分站视图中直接剔除。"""
        return {
            "viewer_station": station_id,
            "mode": self.mode,
            "boxes": [b.to_dict() for b in net.boxes.values()],
            "transfers": dict(net.transfers),
            "trips": {t.trip_id: t.to_dict() for t in net.trips.values()},
            "bags": [
                {
                    **b.to_dict(),
                    # 袋的存在与允许袋重公开；纸重域由公开袋重推出。
                }
                for b in net.bags.values()
            ],
            "visible_partials": [
                pw.to_dict() for pw in net.visible_partials(station_id)
            ],
            "hidden_partial_count": len(net.partials)
            - len(net.visible_partials(station_id)),
        }

    def apply(
        self,
        period_id: str,
        household_id: str,
        *,
        station_id: Optional[str] = CENTER_STATION,
        at_time: Optional[int] = None,
        coupon_value: int = 1,
        client_partial: Optional[PartialWeigh] = None,
    ) -> Decision:
        """办理一户的返券申请，返回带回执号的结论。

        ``station_id=None`` 为中心；分站传入其站号。``client_partial`` 为
        住户 *现场自愿出示* 的实名称量，按出示者归属可见域临时纳入。
        """
        period = self._require_period(self.conn, period_id)
        ts = int(period["opened_at"] if at_time is None else at_time)
        if client_partial is not None:
            self.add_partial(client_partial)

        net = self._rebuild_network(period_id)
        # 查看视角：中心统一模式可见凑成同车总数的全部局部称量；分站断网
        # 模式只看本站/中心/住户本人当时能看见的资料。
        if self.mode == MODE_UNIFIED:
            viewer = CENTER_STATION
        else:
            viewer = station_id
        solve_result = net.solve(viewer)
        evidence = self._evidence_snapshot(net, viewer)

        right = self.right_state(period_id, household_id)
        right_state = right["state"] if right else RIGHT_HELD
        if right is None:
            self.ensure_right(period_id, household_id, station_id)
            right_state = RIGHT_HELD

        result, reason = self._decide(
            period, household_id, station_id, ts, solve_result, right_state,
            coupon_value,
        )

        proven = None
        pv = solve_result.proven.get(household_id)
        if pv is not None:
            proven = int(pv[0])
        budget_after = self.budget_remaining(period_id, station_id)

        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO application(period_id, household_id, station_id, result, reason, "
                "coupon_value, at_time, right_state, trip_versions, evidence, solve, "
                "budget_after) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    period_id, household_id, station_id, result, reason,
                    int(coupon_value), ts, right_state,
                    json.dumps(
                        {tid: self.trip_versions(tid) for tid in net.trips},
                        sort_keys=True,
                    ),
                    json.dumps(evidence, sort_keys=True, ensure_ascii=False),
                    json.dumps(solve_result.to_dict(), sort_keys=True, ensure_ascii=False),
                    json.dumps({"remaining": budget_after}),
                ),
            )
            receipt_id = int(cur.lastrowid)
            if result == RESULT_ISSUED:
                self._lock_budget(c, period_id, station_id, coupon_value, receipt_id)
                c.execute(
                    "UPDATE claim_right SET state='SPENT', updated_order=? "
                    "WHERE household_id=? AND period_id=?",
                    (self._next_order(c), household_id, period_id),
                )
                c.execute(
                    "INSERT INTO right_event(household_id, period_id, from_state, to_state, "
                    "station_id, note) VALUES (?,?,'HELD','SPENT',?,?)",
                    (household_id, period_id, station_id, f"spent by receipt {receipt_id}"),
                )
                budget_after = self.budget_remaining(period_id, station_id)
                c.execute(
                    "UPDATE application SET budget_after=? WHERE receipt_id=?",
                    (json.dumps({"remaining": budget_after}), receipt_id),
                )
        return Decision(
            receipt_id=receipt_id, result=result, reason=reason,
            coupon_value=coupon_value if result == RESULT_ISSUED else 0,
            proven=proven, reduction_line=int(period["reduction_line"]),
            right_state=right_state, budget_remaining=budget_after,
            solve=solve_result, evidence=evidence,
        )

    def _decide(
        self,
        period: sqlite3.Row,
        household_id: str,
        station_id: Optional[str],
        ts: int,
        solve_result: SolveResult,
        right_state: str,
        coupon_value: int,
    ) -> tuple[str, str]:
        period_id = period["period_id"]
        # 1) 领券权：在途两站都不能花；非持有站无权办理。
        if right_state == RIGHT_HANDED_OVER:
            return RESULT_REJECTED, REASON_RIGHT_IN_TRANSIT
        if right_state == RIGHT_SPENT:
            return RESULT_REJECTED, REASON_DUPLICATE_CLAIM
        # 2) 期限与封账。
        if period["closed_at"] is not None:
            return RESULT_REJECTED, REASON_PERIOD_CLOSED
        if ts > int(period["claim_deadline"]):
            return RESULT_SUSPENDED, REASON_CLAIM_DEADLINE_PASSED
        # 3) 证据能否唯一证实住户重量。
        if not solve_result.feasible:
            return RESULT_REJECTED, REASON_NETWORK_INFEASIBLE
        proven = solve_result.proven.get(household_id)
        if proven is None:
            return RESULT_SUSPENDED, REASON_WEIGHT_UNPROVEN
        if proven[0] < int(period["reduction_line"]):
            return RESULT_REJECTED, REASON_BELOW_LINE
        # 4) 预算：足额且持有领券权才签。
        if self.budget_remaining(period_id, station_id) < coupon_value:
            return RESULT_SUSPENDED, REASON_BUDGET_EXHAUSTED
        return RESULT_ISSUED, REASON_ISSUED_OK

    # ------------------------------------------------------------- 封账争议
    def open_dispute(
        self,
        period_id: str,
        household_id: str,
        *,
        station_id: Optional[str] = CENTER_STATION,
        original_receipt_id: Optional[int] = None,
        note: str = "",
    ) -> Decision:
        """封账后的称量纠正：只开差额争议，绝不另兑新券。"""
        period = self._require_period(self.conn, period_id)
        if period["closed_at"] is None:
            raise CounterError("disputes are only opened after the period is closed")
        net = self._rebuild_network(period_id)
        viewer = CENTER_STATION if self.mode == MODE_UNIFIED else station_id
        solve_result = net.solve(viewer)
        proven_now = solve_result.proven.get(household_id)
        evidence = self._evidence_snapshot(net, viewer)
        evidence["dispute_note"] = note
        evidence["original_receipt_id"] = original_receipt_id

        # 旧证实重量直接取 *原回执当时封存的 solve*（含当时的凭条版本），
        # 再与当前活动版本网络的新证实重量求差额。只记差额，不发新券。
        old_proven = None
        prior = None
        if original_receipt_id is not None:
            prior = self.receipt(original_receipt_id, viewer_role=ORIGIN_CENTER)
            old_proven = prior["solve"]["proven"].get(household_id)
            evidence["old_proven"] = old_proven

        delta = None
        if proven_now is not None and old_proven is not None:
            delta = int(proven_now[0]) - int(old_proven[0])
        evidence["weight_delta"] = delta

        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO application(period_id, household_id, station_id, result, reason, "
                "coupon_value, at_time, right_state, trip_versions, evidence, solve, "
                "budget_after, predecessor_receipt) "
                "VALUES (?,?,?, 'DISPUTE', ?, 0, ?, 'SPENT', ?,?,?, "
                "?, ?)",
                (
                    period_id, household_id, station_id,
                    REASON_POST_CLOSURE_DELTA,
                    int(period["closed_at"]),
                    json.dumps({tid: self.trip_versions(tid) for tid in net.trips},
                               sort_keys=True),
                    json.dumps(evidence, sort_keys=True, ensure_ascii=False),
                    json.dumps(solve_result.to_dict(), sort_keys=True, ensure_ascii=False),
                    json.dumps({"remaining": self.budget_remaining(period_id, station_id)}),
                    original_receipt_id,
                ),
            )
            receipt_id = int(cur.lastrowid)
        return Decision(
            receipt_id=receipt_id, result=RESULT_DISPUTE,
            reason=REASON_POST_CLOSURE_DELTA,
            coupon_value=0, proven=None if proven_now is None else int(proven_now[0]),
            reduction_line=int(period["reduction_line"]),
            right_state=RIGHT_SPENT,
            budget_remaining=self.budget_remaining(period_id, station_id),
            solve=solve_result, evidence=evidence,
        )

    # ------------------------------------------------------------- 回执查询
    def receipt(self, receipt_id: int, *, viewer_role: str = ORIGIN_CENTER,
                viewer_station: Optional[str] = None) -> dict:
        """按回执号取资料；按经办方角色过滤无权查看的私重与别站流水。

        ``viewer_role``：``CENTER`` 看全量；``STATION`` 只看本站凭条与本站
        领取流水；``HOUSEHOLD`` 只看本户凭条、公开总量与本站领券权。
        """
        row = self.conn.execute(
            "SELECT * FROM application WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        if row is None:
            raise CounterError(f"unknown receipt {receipt_id}")
        data = dict(row)
        data["evidence"] = json.loads(row["evidence"])
        data["solve"] = json.loads(row["solve"])
        data["trip_versions"] = json.loads(row["trip_versions"])
        data["budget_after"] = json.loads(row["budget_after"])

        if viewer_role == ORIGIN_CENTER:
            return data
        if viewer_role == ORIGIN_STATION:
            # 分站：剔除别站领取流水（非本站申请只能看到公开部分）与别站私重。
            if viewer_station is not None and data["station_id"] not in (
                viewer_station, CENTER_STATION
            ):
                data["station_id"] = "<redacted-other-station>"
            data["evidence"]["visible_partials"] = [
                pw for pw in data["evidence"]["visible_partials"]
                if pw["origin"] == ORIGIN_CENTER or pw["station_id"] == viewer_station
            ]
            data["evidence"]["hidden_partial_count"] = "*"
            data["budget_ledger"] = self._station_ledger(viewer_station)
            return data
        # 住户：自己的凭条、公开总量（车次净重）、本站领券权；无别户私重。
        hh = data["household_id"]

        def _own_partial(pw: dict) -> bool:
            if pw.get("household_id") == hh:
                return True  # 本户实名称量凭条
            # 无住户归属的公开凭条（车次/箱公开称量）对所有人可见。
            return pw.get("household_id") is None and pw["origin"] == ORIGIN_CENTER

        data["evidence"]["visible_partials"] = [
            pw for pw in data["evidence"]["visible_partials"] if _own_partial(pw)
        ]
        data["solve"]["worlds"] = [
            {
                "household_paper": {hh: w["household_paper"].get(hh)},
                "trip_net": w["trip_net"],
            }
            for w in data["solve"]["worlds"]
        ]
        data["right"] = self.right_state(data["period_id"], hh)
        return data

    def _station_ledger(self, station_id: Optional[str]) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM budget_ledger WHERE station_id IS ? OR station_id IS NULL "
                "ORDER BY ledger_id",
                (station_id,),
            )
        ]

    def public_totals(self) -> dict:
        """公开总量：各车次的 *活动* 净重版本（任何角色可见）。"""
        out: dict[str, dict] = {}
        for r in self.conn.execute(
            "SELECT * FROM trip_version WHERE active=1 ORDER BY trip_id"
        ):
            out[r["trip_id"]] = {"version": r["version"], "net": [r["lo"], r["hi"]]}
        return out

    def receipts(self) -> list[dict]:
        return [
            self.receipt(int(r["receipt_id"]))
            for r in self.conn.execute(
                "SELECT receipt_id FROM application ORDER BY receipt_id"
            )
        ]

    # ------------------------------------------------------------- 核对
    def reconcile(self) -> list[str]:
        """可回查核对：引用一致、版本链、预算非负、领券权状态机、重算可行世界。"""
        errs: list[str] = []

        for r in self.conn.execute("SELECT * FROM bag"):
            if not self.conn.execute(
                "SELECT 1 FROM box WHERE box_id=?", (r["box_id"],)
            ).fetchone():
                errs.append(f"bag {r['bag_id']} references missing box")
            if r["tare_hi"] < r["tare_lo"] or r["gross"] < r["tare_lo"]:
                errs.append(f"bag {r['bag_id']} has invalid tare/gross")
        for r in self.conn.execute("SELECT * FROM box"):
            if not self.conn.execute(
                "SELECT 1 FROM trip_version WHERE trip_id=? AND active=1",
                (r["trip_id"],),
            ).fetchone():
                errs.append(f"box {r['box_id']} trip {r['trip_id']} has no active version")
        for r in self.conn.execute("SELECT * FROM transfer"):
            s = self.conn.execute(
                "SELECT trip_id FROM box WHERE box_id=?", (r["source_box_id"],)
            ).fetchone()
            d = self.conn.execute(
                "SELECT trip_id FROM box WHERE box_id=?", (r["dest_box_id"],)
            ).fetchone()
            if s is None or d is None or s["trip_id"] == d["trip_id"]:
                errs.append(f"transfer {r['source_box_id']} invalid")
            inc = self.conn.execute(
                "SELECT include_in_trip FROM box WHERE box_id=?", (r["source_box_id"],)
            ).fetchone()
            if inc is not None and inc["include_in_trip"] != 0:
                errs.append(f"transferred box {r['source_box_id']} counted twice")

        # 净重版本链：每车次恰好一个 active，版本连续且指向前序。
        for tid, in self.conn.execute(
            "SELECT DISTINCT trip_id FROM trip_version ORDER BY trip_id"
        ):
            rows = self.conn.execute(
                "SELECT * FROM trip_version WHERE trip_id=? ORDER BY version", (tid,)
            ).fetchall()
            active = [r for r in rows if r["active"]]
            if len(active) != 1 or active[0]["version"] != rows[-1]["version"]:
                errs.append(f"trip {tid} version chain not single-headed")
            for i, r in enumerate(rows, start=1):
                if int(r["version"]) != i:
                    errs.append(f"trip {tid} version numbering gap")
                expect = None if i == 1 else i - 1
                if r["predecessor_version"] != expect:
                    errs.append(f"trip {tid} v{i} predecessor mismatch")

        # 预算非负；签券必有等额 LOCK。
        for r in self.conn.execute("SELECT * FROM budget"):
            if r["amount"] < 0:
                errs.append(f"budget {r['period_id']}/{r['station_id']} negative")
        for r in self.conn.execute(
            "SELECT * FROM application WHERE result='ISSUED'"
        ):
            locks = self.conn.execute(
                "SELECT COALESCE(SUM(-delta),0) AS n FROM budget_ledger "
                "WHERE receipt_id=? AND kind='LOCK'",
                (r["receipt_id"],),
            ).fetchone()["n"]
            if int(locks) != int(r["coupon_value"]):
                errs.append(
                    f"receipt {r['receipt_id']} locked {locks} != coupon {r['coupon_value']}"
                )
            right = self.right_state(r["period_id"], r["household_id"])
            if right is None or right["state"] != RIGHT_SPENT:
                errs.append(f"issued receipt {r['receipt_id']} right not SPENT")

        # 可行世界可在 *回执当时封存的版本与凭条快照* 上重算；
        # 版本/凭条演进不视为篡改，旧回执在自己的快照上必须可复现。
        for r in self.conn.execute("SELECT * FROM application ORDER BY receipt_id"):
            try:
                snapshot = json.loads(r["trip_versions"])
                evidence = json.loads(r["evidence"])
                partial_rows = evidence.get("visible_partials", [])
                historical = self._network_from_snapshot(snapshot, partial_rows)
                fresh = historical.solve(None)
                stored = json.loads(r["solve"])
                if stored["feasible"] != fresh.feasible:
                    errs.append(
                        f"receipt {r['receipt_id']} feasibility not reproducible on its snapshot"
                    )
                elif fresh.feasible:
                    stored_worlds = {
                        tuple(sorted(w["household_paper"].items()))
                        for w in stored["worlds"]
                    }
                    fresh_worlds = {
                        tuple(sorted(w.household_paper.items())) for w in fresh.worlds
                    }
                    if stored_worlds != fresh_worlds:
                        errs.append(
                            f"receipt {r['receipt_id']} feasible worlds not reproducible"
                        )
            except CounterError as exc:
                errs.append(f"receipt {r['receipt_id']} rebuild failed: {exc}")
        return errs
