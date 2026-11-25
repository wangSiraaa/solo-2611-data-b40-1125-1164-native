"""SQLite plumbing shared by the quote service and the after-sales ledger."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_loads(value: str) -> Any:
    return json.loads(value)


def deep_copy(value: Any) -> Any:
    return json_loads(json.dumps(value, ensure_ascii=False))


def ensure_parent(path: str) -> None:
    if path and path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)


class Database:
    """A tiny per-operation connection factory.

    Every call opens its own connection so two instances (or two threads) can
    exercise real SQLite locking.  ``BEGIN IMMEDIATE`` serialises writers; the
    busy timeout makes a competing writer wait instead of failing spuriously.
    """

    def __init__(self, path: str, busy_timeout_ms: int = 30000):
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._memory_lock = threading.RLock()
        self._memory_conn: Optional[sqlite3.Connection] = None
        self._memory_ready = False
        ensure_parent(path)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            self._init_schema(conn)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self.path == ":memory:":
            with self._memory_lock:
                if self._memory_conn is None:
                    self._memory_conn = self._new_connection()
                    if not self._memory_ready:
                        self._init_schema(self._memory_conn)
                        self._memory_ready = True
                yield self._memory_conn
            return
        conn = self._new_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=self.busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY,
                seq INTEGER NOT NULL UNIQUE,
                content_json TEXT NOT NULL,
                coupons_json TEXT NOT NULL,
                products_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quotes (
                request_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                basket_sig TEXT NOT NULL,
                basket_json TEXT NOT NULL,
                quote_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sales (
                sale_id TEXT PRIMARY KEY,
                quote_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                quote_sig TEXT NOT NULL,
                quote_json TEXT NOT NULL,
                total INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sale_units (
                sale_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                price INTEGER NOT NULL,
                coupon_id TEXT,
                discount INTEGER NOT NULL,
                paid INTEGER NOT NULL,
                returned INTEGER NOT NULL DEFAULT 0,
                return_request_id TEXT,
                grade TEXT,
                PRIMARY KEY (sale_id, unit_id),
                FOREIGN KEY (sale_id) REFERENCES sales(sale_id)
            );

            CREATE TABLE IF NOT EXISTS return_requests (
                request_id TEXT PRIMARY KEY,
                sale_id TEXT NOT NULL,
                canonical_request TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (sale_id) REFERENCES sales(sale_id)
            );

            CREATE TABLE IF NOT EXISTS return_items (
                request_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                grade TEXT NOT NULL,
                original_paid INTEGER NOT NULL,
                refund INTEGER NOT NULL,
                PRIMARY KEY (request_id, unit_id),
                FOREIGN KEY (request_id) REFERENCES return_requests(request_id)
            );

            CREATE TABLE IF NOT EXISTS adjustments (
                event_id TEXT PRIMARY KEY,
                content_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                bucket TEXT NOT NULL CHECK (bucket IN ('RESTOCK', 'SCRAP')),
                delta INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_totals (
                sku TEXT NOT NULL,
                bucket TEXT NOT NULL CHECK (bucket IN ('RESTOCK', 'SCRAP')),
                qty INTEGER NOT NULL,
                PRIMARY KEY (sku, bucket)
            );

            -- ================= optics handover subsystem =================
            -- A telescope: an output interface plus inclusive focus-wheel
            -- travel [lo, hi]; the wheel can be adjusted on site and both
            -- endpoints count as reachable.
            CREATE TABLE IF NOT EXISTS telescopes (
                telescope_id TEXT PRIMARY KEY,
                out_iface TEXT NOT NULL,
                travel_lo INTEGER NOT NULL,
                travel_hi INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                CHECK (travel_lo <= travel_hi)
            );

            -- A batch of barrel shoulders sharing one zero-point bias, recovered
            -- by the technician from a two-end scale plus known test-focus cards.
            -- The recovered bias is an inclusive integer interval [lo, hi]; the
            -- *whole* interval is the set of compatible calibrations.
            CREATE TABLE IF NOT EXISTS barrel_batches (
                batch_id TEXT PRIMARY KEY,
                bias_lo INTEGER NOT NULL,
                bias_hi INTEGER NOT NULL,
                scale_lo INTEGER NOT NULL,
                scale_hi INTEGER NOT NULL,
                card_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                CHECK (bias_lo <= bias_hi)
            );

            -- A physical optical piece bound to a borrowed sale unit.
            --   kind = 'eyepiece': carries focal_position and a shoulder
            --          measurement interval [measured_lo, measured_hi]; its
            --          effective optical path is measured + batch bias.
            --   kind = 'adapter': carries an integer optical path and an
            --          interface direction in_iface -> out_iface.
            -- Pieces sharing a non-null group_id are sealed into one verified
            -- barrel group; no member may be lent alone before re-verification.
            CREATE TABLE IF NOT EXISTS optic_pieces (
                piece_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('eyepiece', 'adapter')),
                model TEXT NOT NULL,
                batch_id TEXT,
                group_id TEXT,
                focal_position INTEGER,
                in_iface TEXT,
                out_iface TEXT,
                nominal_path INTEGER,
                measured_lo INTEGER,
                measured_hi INTEGER,
                status TEXT NOT NULL DEFAULT 'IN_STOCK'
                    CHECK (status IN ('IN_STOCK', 'DELIVERED', 'RETURNED')),
                sale_id TEXT,
                unit_id TEXT,
                return_deadline INTEGER,
                repair_deadline INTEGER,
                lend_return_deadline INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (batch_id) REFERENCES barrel_batches(batch_id)
            );

            -- A pre-locked barrel group (our chosen delivery strategy): an
            -- eyepiece plus an ordered adapter chain for one telescope.  The
            -- observatory reads these technician-signed compatible combos.  A
            -- group is handed over only once verified; until it is re-verified
            -- (unsealed) no member may be lent in isolation.
            CREATE TABLE IF NOT EXISTS optic_groups (
                group_id TEXT PRIMARY KEY,
                telescope_id TEXT NOT NULL,
                eyepiece_id TEXT NOT NULL,
                adapter_json TEXT NOT NULL,
                total_lo INTEGER NOT NULL,
                total_hi INTEGER NOT NULL,
                net_lo INTEGER NOT NULL,
                net_hi INTEGER NOT NULL,
                travel_lo INTEGER NOT NULL,
                travel_hi INTEGER NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0,
                sealed INTEGER NOT NULL DEFAULT 1,
                verify_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (telescope_id) REFERENCES telescopes(telescope_id),
                FOREIGN KEY (eyepiece_id) REFERENCES optic_pieces(piece_id)
            );

            -- Observing sessions (at most three): two consecutive sessions
            -- share a night and may overlap in time.
            CREATE TABLE IF NOT EXISTS optic_sessions (
                session_id TEXT PRIMARY KEY,
                seq INTEGER NOT NULL UNIQUE,
                starts_at INTEGER NOT NULL,
                ends_at INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                CHECK (starts_at <= ends_at)
            );

            -- A borrow contract against a sale, binding one verified group to
            -- one session and a focus time slot.
            -- status: OPEN | DELIVERED | CANCELLED
            CREATE TABLE IF NOT EXISTS optic_orders (
                order_id TEXT PRIMARY KEY,
                sale_id TEXT NOT NULL,
                telescope_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                slot_lo INTEGER NOT NULL,
                slot_hi INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('OPEN', 'DELIVERED', 'CANCELLED')),
                content_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (telescope_id) REFERENCES telescopes(telescope_id),
                FOREIGN KEY (group_id) REFERENCES optic_groups(group_id),
                FOREIGN KEY (session_id) REFERENCES optic_sessions(session_id)
            );

            CREATE TABLE IF NOT EXISTS optic_order_pieces (
                order_id TEXT NOT NULL,
                piece_id TEXT NOT NULL,
                role TEXT NOT NULL,
                PRIMARY KEY (order_id, piece_id),
                FOREIGN KEY (order_id) REFERENCES optic_orders(order_id),
                FOREIGN KEY (piece_id) REFERENCES optic_pieces(piece_id)
            );

            -- Exclusive per-piece booking over a session time interval.  Overlap
            -- is detected on [starts_at, ends_at]; once a booking has been
            -- delivered (checked in) the piece cannot be promised to another
            -- instrument while the booking stays active.
            CREATE TABLE IF NOT EXISTS optic_bookings (
                order_id TEXT NOT NULL,
                piece_id TEXT NOT NULL,
                telescope_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                starts_at INTEGER NOT NULL,
                ends_at INTEGER NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                PRIMARY KEY (order_id, piece_id),
                FOREIGN KEY (order_id) REFERENCES optic_orders(order_id),
                FOREIGN KEY (piece_id) REFERENCES optic_pieces(piece_id)
            );

            -- Delivered handover receipts: one per fulfilled order.
            CREATE TABLE IF NOT EXISTS optic_deliveries (
                delivery_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE,
                telescope_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                sale_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                focus_lo INTEGER NOT NULL,
                focus_hi INTEGER NOT NULL,
                receipt_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES optic_orders(order_id)
            );
            """
        )
