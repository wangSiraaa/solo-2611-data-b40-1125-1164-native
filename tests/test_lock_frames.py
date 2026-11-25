"""船闸帧目录、门位控制校验与观测仓接入测试。

运行：``python3.11 -m unittest tests.test_lock_frames -v``
"""

from __future__ import annotations

import unittest

from bitcatalog.lock import side_pool_topology
from bitcatalog.lock_frames import (
    BUS,
    FRAME_GATE_CMD,
    FRAME_LEVEL_GAUGE,
    FRAME_SHIP_PHASE,
    FRAME_VALVE_CMD,
    build_lock_product,
    encode_gate_frame,
    encode_gauge_frame,
    encode_ship_frame,
    encode_valve_frame,
    ingest_lock_frames,
    validate_lock_frame,
)
from bitcatalog.warehouse import FrameEvent, ObservationWarehouse

NS = 1_000_000_000


class FrameValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.product = build_lock_product()
        cls.topology = side_pool_topology(chamber=4, side_capacity=4)

    def test_catalog_compiles_with_disjoint_branches(self) -> None:
        # 互斥分支复用同一位段（pulse_amount / valve_close_reason）必须通过编译。
        ids = {(m.bus, m.frame_id) for m in self.product.messages}
        self.assertEqual(
            ids,
            {(BUS, FRAME_GATE_CMD), (BUS, FRAME_VALVE_CMD),
             (BUS, FRAME_LEVEL_GAUGE), (BUS, FRAME_SHIP_PHASE)},
        )

    def test_open_gate_requires_equal_levels(self) -> None:
        good = encode_gate_frame(self.product, gate_id=1, open=True, left_level=4, right_level=4)
        bad = encode_gate_frame(self.product, gate_id=1, open=True, left_level=3, right_level=4)
        self.assertTrue(validate_lock_frame(self.product, good, frame_id=FRAME_GATE_CMD).accepted)
        verdict = validate_lock_frame(self.product, bad, frame_id=FRAME_GATE_CMD)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.code, "gate_level_mismatch")

    def test_closed_gate_branch_has_no_level_fields(self) -> None:
        payload = encode_gate_frame(self.product, gate_id=1, open=False, close_reason=1)
        verdict = validate_lock_frame(self.product, payload, frame_id=FRAME_GATE_CMD)
        self.assertTrue(verdict.accepted)
        decoded = self.product.message(BUS, FRAME_GATE_CMD).decode(payload)
        active = {signal.name for signal in decoded.signals}
        self.assertNotIn("left_level", active)
        self.assertNotIn("right_level", active)
        self.assertIn("close_reason", active)

    def test_closed_valve_carries_no_pulse(self) -> None:
        payload = encode_valve_frame(self.product, valve_id=1, direction=None)
        verdict = validate_lock_frame(self.product, payload, frame_id=FRAME_VALVE_CMD)
        self.assertTrue(verdict.accepted)
        decoded = self.product.message(BUS, FRAME_VALVE_CMD).decode(payload)
        self.assertNotIn("pulse_amount", {s.name for s in decoded.signals})

    def test_gauge_overflow_rejected(self) -> None:
        payload = encode_gauge_frame(self.product, pool="chamber", level=7)
        verdict = validate_lock_frame(
            self.product, payload, frame_id=FRAME_LEVEL_GAUGE, topology=self.topology
        )
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.code, "level_out_of_range")

    def test_gauge_infinite_pool_accepts_high_reading(self) -> None:
        payload = encode_gauge_frame(self.product, pool="upstream", level=4)
        self.assertTrue(
            validate_lock_frame(
                self.product, payload, frame_id=FRAME_LEVEL_GAUGE, topology=self.topology
            ).accepted
        )

    def test_ship_phase_frame_roundtrip(self) -> None:
        payload = encode_ship_frame(
            self.product, ship_id=2, phase="in_chamber", beat=11, pass_beat=16
        )
        decoded = self.product.message(BUS, FRAME_SHIP_PHASE).decode(payload)
        values = {s.name: s.raw for s in decoded.signals}
        self.assertEqual(values["beat"], 11)
        self.assertEqual(values["pass_beat"], 16)


class WarehouseIngestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.product = build_lock_product()
        cls.topology = side_pool_topology(chamber=4, side_capacity=4)

    def _events(self):
        return [
            FrameEvent(
                frame_event_id="ok-gate",
                bus=BUS,
                frame_id=FRAME_GATE_CMD,
                occurred_at=NS,
                payload_hex=encode_gate_frame(
                    self.product, gate_id=1, open=True, left_level=4, right_level=4
                ).hex(),
            ),
            FrameEvent(
                frame_event_id="bad-gate",
                bus=BUS,
                frame_id=FRAME_GATE_CMD,
                occurred_at=2 * NS,
                payload_hex=encode_gate_frame(
                    self.product, gate_id=1, open=True, left_level=2, right_level=4
                ).hex(),
            ),
            FrameEvent(
                frame_event_id="ok-gauge",
                bus=BUS,
                frame_id=FRAME_LEVEL_GAUGE,
                occurred_at=3 * NS,
                payload_hex=encode_gauge_frame(self.product, pool="chamber", level=3).hex(),
            ),
        ]

    def test_semantic_filter_then_warehouse_ingest(self) -> None:
        with ObservationWarehouse(":memory:") as warehouse:
            result = ingest_lock_frames(
                warehouse, self.product, self._events(), topology=self.topology
            )
            self.assertEqual(result.accepted, 2)
            self.assertEqual(len(result.rejected_semantic), 1)
            self.assertEqual(result.rejected_semantic[0]["code"], "gate_level_mismatch")
            counts = warehouse.counts(self.product)
            self.assertEqual(counts["ok"], 2)
            self.assertEqual(counts["rejected"], 0)  # 语义拒绝帧根本没进仓
            # 入库帧可按窗口聚合复查。
            agg = warehouse.query_window(self.product, "gauge_level", 0, 10 * NS)
            self.assertEqual(agg.count, 1)
            self.assertEqual(int(agg.total), 3)

    def test_bad_hex_still_rejected_by_warehouse_layer(self) -> None:
        events = [
            FrameEvent(
                frame_event_id="bad-hex",
                bus=BUS,
                frame_id=FRAME_GATE_CMD,
                occurred_at=NS,
                payload_hex="abc",  # 奇数 nibble
            )
        ]
        with ObservationWarehouse(":memory:") as warehouse:
            # 语义层先解 hex：非法字节直接抛错，由调用方决定不入仓。
            with self.assertRaises(ValueError):
                ingest_lock_frames(warehouse, self.product, events, topology=self.topology)


if __name__ == "__main__":
    unittest.main()
