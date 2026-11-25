"""缩尺船闸域模型测试：物理规则、相容水账、闸序调度。

运行：``python3.11 -m unittest tests.test_lock -v``
"""

from __future__ import annotations

import unittest

from bitcatalog.lock import (
    CHAMBER,
    DOWNSTREAM,
    GATE_DOWN,
    GATE_UP,
    LockError,
    LockState,
    PulseObs,
    SIDE,
    UPSTREAM,
    VALVE_DOWN,
    VALVE_SIDE,
    VALVE_UP,
    pool_available,
    reconcile_pulses,
    side_pool_topology,
    straight_through_topology,
)
from bitcatalog.lock_schedule import (
    DOWNBOUND,
    UPBOUND,
    LockScheduler,
    SchedulingError,
    Ship,
)
from bitcatalog.lock_scenarios import (
    UPSTREAM_LEVEL,
    scenario_ambiguous_pulse,
    scenario_deadline_three,
    scenario_side_too_small,
    scenario_single_baseline,
    scenario_straight_through,
    scenario_two_ships_recycle,
)


def base_state(topology=None, *, side: int = 0) -> LockState:
    topology = topology or side_pool_topology(chamber=4, side_capacity=4)
    return LockState(
        topology=topology,
        levels={CHAMBER: 0, UPSTREAM: 4, DOWNSTREAM: 0, SIDE: side},
        open_gates=frozenset(),
        open_valves=frozenset(),
    )


class PhysicalRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.topology = side_pool_topology(chamber=4, side_capacity=4)
        self.state = base_state(self.topology)

    def test_closed_valve_rejects_transfer(self) -> None:
        # 穿过关闭阀的转移必须被拒绝（题面硬规则）。
        with self.assertRaises(LockError) as caught:
            self.state.transfer(VALVE_UP, 1)
        self.assertEqual(caught.exception.code, "valve_closed")

    def test_open_valve_moves_one_cell(self) -> None:
        moved = self.state.open_valve(VALVE_UP).transfer(VALVE_UP, 1, direction=UPSTREAM)
        self.assertEqual(moved.level(CHAMBER), 1)
        # 外部河道是恒定标尺，引水后上游尺读数仍是 4。
        self.assertEqual(moved.level(UPSTREAM), 4)

    def test_per_beat_capacity_enforced(self) -> None:
        opened = self.state.open_valve(VALVE_UP)
        with self.assertRaises(LockError) as caught:
            opened.transfer(VALVE_UP, 2)
        self.assertEqual(caught.exception.code, "transfer_over_capacity")

    def test_gate_requires_level_equality(self) -> None:
        with self.assertRaises(LockError) as caught:
            self.state.open_gate(GATE_UP)
        self.assertEqual(caught.exception.code, "gate_level_mismatch")
        leveled = self.state.with_levels({CHAMBER: 4})
        opened = leveled.open_gate(GATE_UP)
        self.assertTrue(opened.gate_is_open(GATE_UP))

    def test_open_gate_blocks_valve(self) -> None:
        leveled = self.state.with_levels({CHAMBER: 4}).open_gate(GATE_UP)
        with self.assertRaises(LockError) as caught:
            leveled.open_valve(VALVE_UP)
        self.assertEqual(caught.exception.code, "gate_open_blocks_valve")

    def test_open_valve_blocks_gate(self) -> None:
        with_valve = self.state.open_valve(VALVE_SIDE)
        leveled = with_valve.with_levels({CHAMBER: 0})
        with self.assertRaises(LockError) as caught:
            leveled.open_gate(GATE_DOWN)
        self.assertEqual(caught.exception.code, "valve_open_blocks_gate")

    def test_side_pool_overflow_rejected(self) -> None:
        state = LockState(
            topology=side_pool_topology(chamber=4, side_capacity=2),
            levels={CHAMBER: 3, UPSTREAM: 4, DOWNSTREAM: 0, SIDE: 2},
            open_gates=frozenset(),
            open_valves=frozenset(),
        )
        opened = state.open_valve(VALVE_SIDE)
        with self.assertRaises(LockError) as caught:
            opened.transfer(VALVE_SIDE, 1, direction=CHAMBER)
        self.assertEqual(caught.exception.code, "level_out_of_range")

    def test_finite_water_conservation(self) -> None:
        state = self.state.open_valve(VALVE_UP).transfer(VALVE_UP, 1, direction=UPSTREAM)
        state = state.close_valve(VALVE_UP)
        state = state.open_valve(VALVE_UP).transfer(VALVE_UP, 1, direction=UPSTREAM)
        self.assertEqual(state.finite_total(), 2)

    def test_initial_level_over_capacity_rejected(self) -> None:
        with self.assertRaises(LockError) as caught:
            LockState(
                topology=self.topology,
                levels={CHAMBER: 9, UPSTREAM: 4, DOWNSTREAM: 0, SIDE: 0},
                open_gates=frozenset(),
                open_valves=frozenset(),
            )
        self.assertEqual(caught.exception.code, "level_out_of_range")


class LedgerReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.topology = side_pool_topology(chamber=4, side_capacity=4)
        self.levels = {CHAMBER: 0, UPSTREAM: 4, DOWNSTREAM: 0, SIDE: 0}

    def test_deterministic_pulses_single_ledger(self) -> None:
        pulses = [
            PulseObs.observed(beat=beat, valve=VALVE_UP, src=UPSTREAM, amount=1, candidates=CHAMBER)
            for beat in range(1, 5)
        ]
        valve_open = {beat: {VALVE_UP: True} for beat in range(1, 5)}
        books = reconcile_pulses(self.topology, self.levels, pulses, valve_open)
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0].level_at(4, CHAMBER), 4)

    def test_closed_valve_pulse_rejected(self) -> None:
        pulses = [PulseObs.observed(1, VALVE_UP, UPSTREAM, 1, CHAMBER)]
        books = reconcile_pulses(self.topology, self.levels, pulses, {1: {VALVE_UP: False}})
        self.assertEqual(books, ())

    def test_ambiguous_pulse_enumerates_full_ledgers(self) -> None:
        # 关键：不得把两个候选池的上限相加；必须产出两套完整账。
        pulses = [
            PulseObs.observed(1, VALVE_UP, UPSTREAM, 1, CHAMBER),
            PulseObs.observed(2, VALVE_UP, UPSTREAM, 1, CHAMBER),
            PulseObs.observed(3, VALVE_UP, UPSTREAM, 1, CHAMBER),
            PulseObs.observed(
                4, VALVE_UP, UPSTREAM, 1, (CHAMBER, SIDE),
                manifold=(UPSTREAM, CHAMBER, SIDE),
            ),
        ]
        valve_open = {beat: {VALVE_UP: True} for beat in range(1, 5)}
        books = reconcile_pulses(self.topology, self.levels, pulses, valve_open)
        self.assertEqual(len(books), 2)
        chamber_levels = {book.level_at(4, CHAMBER) for book in books}
        self.assertEqual(chamber_levels, {3, 4})
        # 每套账的有限总水量都是 4（水没消失，只是去向不同）。
        self.assertTrue(all(book.finite_total() == 4 for book in books))
        # 公共下界：闸室库存各套不一致 -> None，禁止凭预计库存开阀。
        self.assertIsNone(pool_available(books, 4, CHAMBER))
        self.assertEqual(pool_available(books, 4, SIDE), None)

    def test_ambiguous_without_manifold_is_physical_error(self) -> None:
        pulse = PulseObs.observed(1, VALVE_UP, UPSTREAM, 1, (CHAMBER, SIDE))
        with self.assertRaises(LockError) as caught:
            reconcile_pulses(self.topology, self.levels, [pulse], {1: {VALVE_UP: True}})
        self.assertEqual(caught.exception.code, "bad_valve_path")

    def test_overflow_assignment_eliminated(self) -> None:
        # 侧池已满（容量 4、存 4）时，“进侧池”的归属溢出被剔除，
        # 只留进闸室的那套账；源池是侧池，脉冲经侧阀把 1 格水送进闸室。
        levels = {CHAMBER: 2, UPSTREAM: 4, DOWNSTREAM: 0, SIDE: 4}
        pulse = PulseObs.observed(
            1, VALVE_SIDE, SIDE, 1, (CHAMBER, SIDE), manifold=(CHAMBER, SIDE)
        )
        books = reconcile_pulses(self.topology, levels, [pulse], {1: {VALVE_SIDE: True}})
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0].level_at(1, CHAMBER), 3)
        self.assertEqual(books[0].level_at(1, SIDE), 3)


class SchedulerTests(unittest.TestCase):
    def test_single_downbound_baseline_timing(self) -> None:
        scheduler = LockScheduler(
            side_pool_topology(chamber=4, side_capacity=4), base_state().levels
        )
        ship = Ship.create("甲", DOWNBOUND, 1, 12, (VALVE_UP, VALVE_SIDE, VALVE_DOWN))
        schedule = scheduler.run([ship])
        self.assertEqual(schedule.pass_beats["甲"], 10)
        # 前 4 拍闸室逐拍 +1，第 5 拍才开门。
        self.assertEqual([a.levels[CHAMBER] for a in schedule.actions[:5]], [1, 2, 3, 4, 4])

    def test_deadline_missed_with_same_ledger(self) -> None:
        scheduler = LockScheduler(
            side_pool_topology(chamber=4, side_capacity=4), base_state().levels
        )
        ship = Ship.create("急", DOWNBOUND, 1, 3, (VALVE_UP, VALVE_SIDE, VALVE_DOWN))
        with self.assertRaises(SchedulingError) as caught:
            scheduler.run([ship])
        self.assertEqual(caught.exception.code, "deadline_missed")

    def test_two_ships_recycle_timing_and_conservation(self) -> None:
        result = scenario_two_ships_recycle()
        self.assertTrue(result.feasible)
        schedule = result.detail["schedule"]
        self.assertEqual(schedule["pass_beats"]["乙(上行)"], 16)
        # 回收（chamber->side）全部早于复用（side->chamber）。
        self.assertLess(max(result.detail["side_capture_beats"]),
                        min(result.detail["side_reuse_beats"]))
        self.assertTrue(all(total <= UPSTREAM_LEVEL for total in
                            result.detail["finite_total_by_beat"].values()))

    def test_arrival_order_enforced(self) -> None:
        topology = side_pool_topology(chamber=4, side_capacity=4)
        scheduler = LockScheduler(topology, base_state().levels)
        first = Ship.create("先到", DOWNBOUND, 1, 20, (VALVE_UP, VALVE_SIDE, VALVE_DOWN))
        second = Ship.create("后到", UPBOUND, 5, 20, (VALVE_UP, VALVE_SIDE, VALVE_DOWN))
        with self.assertRaises(SchedulingError) as caught:
            scheduler.run([second, first])  # 故意逆序提交
        self.assertEqual(caught.exception.code, "out_of_order")

    def test_disallowed_branch_closes_gate(self) -> None:
        result = scenario_side_too_small()
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason_code, "branch_not_allowed")

    def test_straight_through_capacity_proof(self) -> None:
        result = scenario_straight_through()
        self.assertTrue(result.feasible)
        proof = result.detail["per_beat_capacity_proof"]
        self.assertTrue(proof["capacity_ok"])
        self.assertGreaterEqual(result.detail["whole_game_water"]["makeup_from_upper_pool"], 4)

    def test_scenario_ambiguous_closes_gate(self) -> None:
        result = scenario_ambiguous_pulse()
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason_code, "ambiguous_shortfall")
        self.assertEqual(result.detail["ledger_count"], 2)

    def test_scenario_baseline_and_deadline(self) -> None:
        self.assertTrue(scenario_single_baseline().feasible)
        dead = scenario_deadline_three()
        self.assertFalse(dead.feasible)
        self.assertEqual(dead.reason_code, "deadline_missed")


class StraightThroughTopologyTests(unittest.TestCase):
    def test_external_reservoirs_keep_fixed_gauge(self) -> None:
        topology = straight_through_topology(chamber=4, makeup_per_beat=1, drain_per_beat=1)
        state = LockState(
            topology=topology,
            levels={
                CHAMBER: 0, UPSTREAM: 4, DOWNSTREAM: 0,
                "makeup_source": 4, "drain_sink": 0,
            },
            open_gates=frozenset(),
            open_valves=frozenset(),
        )
        moved = state.open_valve("valve_makeup").transfer("valve_makeup", 1, direction="makeup_source")
        self.assertEqual(moved.level(CHAMBER), 1)
        self.assertEqual(moved.level("makeup_source"), 4)  # 恒定标尺


if __name__ == "__main__":
    unittest.main()
