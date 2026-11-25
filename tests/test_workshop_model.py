"""布景工坊领域模型单测：可裁段推导、接缝判花、回溯核花与余量区间。

题面三个手算算例（三拍一轮）在这里固化为真值：
1. 两卷各三格、首拍 0/1 能交；第二卷标记改成 2 不能交；乙面多一格也不能交；
2. 转台双销位各对应不同卷段；封签跨销位不可行、整卷可行；
3. 他班已签首格后按真实占用重新受理。
"""

from __future__ import annotations

import unittest

from confcat.errors import OrderRejected
from confcat.workshop_model import (
    DIRECTION_REVERSED,
    MODE_SEALED,
    Blockage,
    CutSegment,
    RollSpec,
    Scenario,
    SeamSpec,
    WallSpec,
    WorkshopSpec,
    derive_segments,
    plan_scenario,
    roll_remaining_intervals,
    seam_ok,
)


def two_wall_spec(mark_b: int = 1, h_b: int = 3, h_a: int = 3) -> WorkshopSpec:
    r1 = RollSpec("R1", 3, "正", 0, ((0, 2),))
    r2 = RollSpec("R2", 3, "正", mark_b, ((0, 2),))
    sc = Scenario(
        "default",
        (WallSpec("A", h_a, 0), WallSpec("B", h_b)),
        (SeamSpec("A", "B", 1), SeamSpec("B", "A", 2)),
    )
    return WorkshopSpec(3, (r1, r2), (sc,), ("A", "B"))


class SeamMathTest(unittest.TestCase):
    def test_seam_ok_modulo(self):
        # 前墙末格拍 2，要求错 1 拍 -> 后墙首拍 (2+1+1) mod 3 = 1
        self.assertTrue(seam_ok(3, 2, 1, 1))
        self.assertFalse(seam_ok(3, 2, 2, 1))
        # 错 0 拍即后墙首拍紧接前墙末格
        self.assertTrue(seam_ok(3, 2, 0, 0))

    def test_phase_keeps_roll_and_coord_before_modulo(self):
        roll = RollSpec("R9", 3, "正", 1, ((0, 5),))
        # 坐标 3：(1+3) mod 3 = 1；段仍带卷号与原坐标
        seg = derive_segments(roll, 2)[0]
        self.assertEqual((seg.roll_id, seg.start, seg.end), ("R9", 0, 1))
        self.assertEqual(roll.phase_at(3), 1)
        self.assertEqual(roll.phase_at(5), 0)


class CutSegmentTest(unittest.TestCase):
    def test_forward_segments_slide_within_interval(self):
        roll = RollSpec("R1", 3, "正", 0, ((0, 3),))
        segs = derive_segments(roll, 2)
        self.assertEqual([(s.start, s.end) for s in segs], [(0, 1), (1, 2), (2, 3)])
        self.assertEqual([(s.first_beat, s.last_beat) for s in segs], [(0, 1), (1, 2), (2, 0)])

    def test_reversed_segment_uses_right_end_as_first_beat(self):
        # 反向卷不能翻头：装台首格是窗口右端，首拍取右端坐标相位
        roll = RollSpec("RR", 3, DIRECTION_REVERSED, 0, ((0, 2),))
        segs = derive_segments(roll, 3)
        self.assertEqual(len(segs), 1)
        seg = segs[0]
        self.assertEqual((seg.start, seg.end), (0, 2))
        self.assertEqual((seg.first_beat, seg.last_beat), (2, 0))

    def test_short_interval_produces_nothing(self):
        roll = RollSpec("R1", 3, "正", 0, ((0, 1),))
        self.assertEqual(derive_segments(roll, 3), [])

    def test_segments_span_multiple_intervals(self):
        roll = RollSpec("R1", 3, "正", 0, ((0, 2), (10, 12)))
        segs = derive_segments(roll, 3)
        self.assertEqual([(s.start, s.end) for s in segs], [(0, 2), (10, 12)])

    def test_overlap_detection(self):
        s1 = CutSegment("R", 0, 2, 0, 2, "正", 3)
        s2 = CutSegment("R", 2, 4, 2, 1, "正", 3)
        s3 = CutSegment("R", 3, 4, 0, 1, "正", 3)
        s4 = CutSegment("Q", 0, 2, 0, 2, "正", 3)
        self.assertTrue(s1.overlaps(s2))
        self.assertFalse(s1.overlaps(s3))
        self.assertFalse(s1.overlaps(s4))  # 不同卷不算同一段纸

    def test_remaining_intervals_trims_edges_and_middle(self):
        roll = RollSpec("R", 3, "正", 0, ((0, 9),))
        # 剪掉边头 [0,2] 与中间一格 5
        self.assertEqual(
            roll_remaining_intervals(roll, [(0, 2), (5, 5)]),
            ((3, 4), (6, 9)),
        )
        # 整段领光
        self.assertEqual(roll_remaining_intervals(roll, [(0, 9)]), ())


class StatedExampleTest(unittest.TestCase):
    """题面明文算例。"""

    def test_two_rolls_three_cells_can_be_delivered(self):
        spec = two_wall_spec()
        plan = plan_scenario(spec, spec.scenarios[0], {})
        self.assertTrue(plan.guaranteed)
        self.assertEqual(plan.assignments["A"].roll_id, "R1")
        self.assertEqual(plan.assignments["A"].first_beat, 0)
        self.assertEqual(plan.assignments["B"].roll_id, "R2")
        self.assertEqual(plan.assignments["B"].first_beat, 1)

    def test_changing_second_roll_marker_to_two_blocks_seam(self):
        spec = two_wall_spec(mark_b=2)
        plan = plan_scenario(spec, spec.scenarios[0], {})
        self.assertFalse(plan.guaranteed)
        b = plan.blockages[0]
        self.assertEqual(b.kind, "seam")
        self.assertEqual(b.seam, ("A", "B"))
        self.assertEqual(b.roll_id, "R2")  # 卡点指到具体卷段
        self.assertIsNotNone(b.segment)

    def test_taller_wall_b_same_cut_list_is_insufficient(self):
        spec = two_wall_spec(h_b=4)
        plan = plan_scenario(spec, spec.scenarios[0], {})
        self.assertFalse(plan.guaranteed)
        self.assertEqual(plan.blockages[0].kind, "no-segment")
        self.assertEqual(plan.blockages[0].wall_id, "B")

    def test_no_wall_claims_same_paper_twice(self):
        # 两墙都只能从同一卷领：高度之和超过卷长 -> 不重复领用则无解
        r1 = RollSpec("R1", 3, "正", 0, ((0, 4),))
        sc = Scenario(
            "default",
            (WallSpec("A", 3, 0), WallSpec("B", 3)),
            (SeamSpec("A", "B", 1), SeamSpec("B", "A", 2)),
        )
        spec = WorkshopSpec(3, (r1,), (sc,), ("A", "B"))
        plan = plan_scenario(spec, sc, {})
        self.assertFalse(plan.guaranteed)


class TurntablePinTest(unittest.TestCase):
    """题面末段：两面转台、双销位、一卷四格相位 1,2,0,1。"""

    def setUp(self):
        self.roll = RollSpec("R", 3, "正", 1, ((0, 3),))
        self.pin_a = Scenario(
            "pin-A",
            (WallSpec("A", 3, 0, pre_mounted=True), WallSpec("B", 3)),
            (SeamSpec("A", "B", 1), SeamSpec("B", "A", 2)),
        )
        self.pin_b = Scenario(
            "pin-B",
            (WallSpec("A", 3, 0, pre_mounted=True), WallSpec("B", 3)),
            (SeamSpec("A", "B", 2), SeamSpec("B", "A", 1)),
        )
        self.spec = WorkshopSpec(3, (self.roll,), (self.pin_a, self.pin_b), ("A", "B"))
        self.spec.validate()

    def test_each_pin_yields_distinct_roll_end(self):
        pa = plan_scenario(self.spec, self.pin_a, {})
        pb = plan_scenario(self.spec, self.pin_b, {})
        self.assertTrue(pa.guaranteed and pb.guaranteed)
        # 甲销剪 [0,2]，余尾端坐标 3；乙销剪 [1,3]，余边头坐标 0
        self.assertEqual((pa.assignments["B"].start, pa.assignments["B"].end), (0, 2))
        self.assertEqual((pb.assignments["B"].start, pb.assignments["B"].end), (1, 3))
        # 甲面已上台，不领卷
        self.assertIsNone(pa.assignments["A"])

    def test_sealed_fixed_assignment_fails_other_pin(self):
        pa = plan_scenario(self.spec, self.pin_a, {})
        pb = plan_scenario(self.spec, self.pin_b, {}, fixed=dict(pa.assignments))
        self.assertFalse(pb.guaranteed)
        self.assertEqual(pb.blockages[0].seam, ("B", "A"))

    def test_reserved_first_cell_reaccepted_by_real_occupancy(self):
        # 另一班已签本卷首格（坐标 0）
        occupied = {"R": [(0, 0)]}
        pa = plan_scenario(self.spec, self.pin_a, occupied)
        pb = plan_scenario(self.spec, self.pin_b, occupied)
        self.assertFalse(pa.guaranteed)  # 甲销需要边头，边头已许人
        self.assertTrue(pb.guaranteed)
        self.assertEqual((pb.assignments["B"].start, pb.assignments["B"].end), (1, 3))
        # 不能把已许的边头裁给本班
        self.assertNotEqual(pa.blockages[0].kind, "overlap")
        self.assertEqual(pa.blockages[0].seam, ("A", "B"))


class ReversedRollTest(unittest.TestCase):
    """反向卷：方向固定不能翻头，装台首拍取窗口右端坐标相位。"""

    def test_reversed_roll_drives_seam(self):
        # 反向卷 mark=0、三格相位坐标 0,1,2 -> 拍 0,1,2；装台自右端起 -> 首拍 2 末拍 0
        rr = RollSpec("RR", 3, DIRECTION_REVERSED, 0, ((0, 2),))
        forward = RollSpec("RF", 3, "正", 0, ((0, 2),))
        # 甲用正向卷首拍 0；要求 A->B 错 1 拍 => B 首拍须 1
        # 反向卷整段首拍恒为 2，合不上 -> 不能交
        sc = Scenario(
            "default",
            (WallSpec("A", 3, 0), WallSpec("B", 3)),
            (SeamSpec("A", "B", 1), SeamSpec("B", "A", 2)),
        )
        spec = WorkshopSpec(3, (forward, rr), (sc,), ("A", "B"))
        plan = plan_scenario(spec, sc, {})
        self.assertFalse(plan.guaranteed)
        self.assertEqual(plan.blockages[0].seam, ("A", "B"))

    def test_reversed_roll_can_match_when_phase_aligns(self):
        # 反向卷 mark=1：坐标相位 1,2,0；整段装台首拍 = phase_at(2) = 0，末拍 = phase_at(0) = 1
        rr = RollSpec("RR", 3, DIRECTION_REVERSED, 1, ((0, 2),))
        sc = Scenario(
            "default",
            (WallSpec("A", 3, 0),),
            (SeamSpec("A", "A", 1),),  # 自接缝实际错 (0-1-1) mod 3 = 1
        )
        spec = WorkshopSpec(3, (rr,), (sc,), ("A",))
        plan = plan_scenario(spec, sc, {})
        self.assertTrue(plan.guaranteed)
        seg = plan.assignments["A"]
        self.assertEqual((seg.first_beat, seg.last_beat), (0, 1))


    def test_sealed_fixed_segment_first_beat_mismatch_rejected(self):
        roll = RollSpec("R", 3, "正", 0, ((0, 5),))
        sc = Scenario(
            "default",
            (WallSpec("A", 2, first_beat=0), WallSpec("B", 2, first_beat=2)),
            (SeamSpec("A", "B", 0), SeamSpec("B", "A", 0)),
        )
        spec = WorkshopSpec(3, (roll,), (sc,), ("A", "B"))
        # 固定给 B 一段首拍 1 的纸，与 B 要求首拍 2 不符 -> 封签不能翻头，拒绝
        fixed_b = CutSegment("R", 0, 1, 1, 2, "正", 3)
        plan = plan_scenario(spec, sc, {}, fixed={"B": fixed_b})
        self.assertFalse(plan.guaranteed)
        self.assertEqual(plan.blockages[0].kind, "first-beat")
        self.assertEqual(plan.blockages[0].wall_id, "B")


class ValidationTest(unittest.TestCase):
    def test_roll_rejects_bad_direction_and_phase(self):
        with self.assertRaises(OrderRejected):
            RollSpec("R", 3, "横", 0, ((0, 2),)).validate()
        with self.assertRaises(OrderRejected):
            RollSpec("R", 3, "正", 3, ((0, 2),)).validate()

    def test_spec_rejects_missing_ring_seam(self):
        r1 = RollSpec("R1", 3, "正", 0, ((0, 5),))
        sc = Scenario(
            "default",
            (WallSpec("A", 3, 0), WallSpec("B", 3)),
            (SeamSpec("A", "B", 1),),  # 缺 B->A，圈合不上
        )
        spec = WorkshopSpec(3, (r1,), (sc,), ("A", "B"))
        with self.assertRaises(OrderRejected) as ctx:
            spec.validate()
        self.assertEqual(ctx.exception.reason, "missing-seam")

    def test_spec_limits_six_walls_four_rolls(self):
        rolls = tuple(RollSpec(f"R{i}", 3, "正", 0, ((0, 9),)) for i in range(5))
        sc = Scenario("default", (WallSpec("X", 1),), ())
        with self.assertRaises(OrderRejected):
            WorkshopSpec(3, rolls, (sc,), ("X",)).validate()

    def test_six_wall_ring_with_four_rolls_within_limit(self):
        rolls = tuple(RollSpec(f"R{i}", 3, "正", 0, ((0, 9),)) for i in range(4))
        walls = tuple(WallSpec(f"W{i}", 2) for i in range(6))
        seams = tuple(
            SeamSpec(f"W{i}", f"W{(i + 1) % 6}", 0) for i in range(6)
        )
        sc = Scenario("default", walls, seams)
        spec = WorkshopSpec(3, rolls, (sc,), tuple(f"W{i}" for i in range(6)))
        spec.validate()  # 不抛即通过规模合同
        plan = plan_scenario(spec, sc, {})
        self.assertTrue(plan.guaranteed)
        # 6 面墙各领一段，且无同卷坐标重复
        used = [(s.roll_id, s.start, s.end) for s in plan.assignments.values()]
        self.assertEqual(len(used), len(set(used)))

    def test_scenario_wall_set_mismatch_rejected(self):
        rolls = (RollSpec("R1", 3, "正", 0, ((0, 9),)),)
        sc_a = Scenario("s1", (WallSpec("A", 2), WallSpec("B", 2)),
                        (SeamSpec("A", "B", 0), SeamSpec("B", "A", 0)))
        sc_b = Scenario("s2", (WallSpec("A", 2), WallSpec("X", 2)),
                        (SeamSpec("A", "X", 0), SeamSpec("X", "A", 0)))
        spec = WorkshopSpec(3, rolls, (sc_a, sc_b), ("A", "B"))
        with self.assertRaises(OrderRejected) as ctx:
            spec.validate()
        self.assertEqual(ctx.exception.reason, "scenario-wall-mismatch")


if __name__ == "__main__":
    unittest.main()
