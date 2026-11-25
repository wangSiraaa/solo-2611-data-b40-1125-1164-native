"""车站报站词库与出声台的全语义测试（纯标准库 unittest）。

覆盖：
1. PCM 接头重叠相加、禁止重剪；
2. 模板容差、一段兼容两字、音高档位归并与另一段排除一档；
3. 母录/分出资产、改版联动、发版快照不可变、采样预算；
4. 严格听辨：可播、误听邻站、缺录档、窗不足禁截字；
5. 整词/拼接只能读发版渲染；
6. 买断（锁定/不得换尾/补录约新档）与租用（跨班复用/独占）合同；
7. 次班两种情形（T_03 可借 / 被他班独占）的实际播音与拒播依据；
8. 已发车旧播次按原音回听；JSON 落盘重开等价。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from audiomix.announce import (
    BookingTable, BroadcastDesk, CharacterTemplate, ContractKind, CostLedger,
    Joint, Lexicon, RecordingCalendar, Schedule, SplicePlan, SpliceTable,
    StationTemplate, Trip, WholeWordPlan, WorkspaceStore, overlap_join,
    render_plan,
)
from audiomix.announce.hearing import Reason, adjudicate
from audiomix.announce.lexicon import LexiconError
from audiomix.announce.pcm import JointError
from audiomix.announce.render import Rendering
from audiomix.announce.templates import analyze_speaker, segment_matches

JIA, YI, BING = "甲站", "乙站", "丙站"


def make_lexicon() -> Lexicon:
    lex = Lexicon(sample_budget=100)
    lex.record_master("M_JIA", "whole_word", "S1", [4, 1, 2], JIA)
    lex.derive("H_JIA", "M_JIA", "head", "头", 0, 1)
    lex.derive("T_JIA", "M_JIA", "tail", "尾", 1, 3)
    lex.record_master("H_41", "head", "S2", [4, 1], "带尾的头")
    lex.record_master("T_03", "tail", "S2", [0, 3], "尾甲")
    lex.record_master("T_13", "tail", "S2", [1, 3], "尾乙")
    lex.add_station_template(StationTemplate(JIA, (4, 1, 2)))
    lex.add_station_template(StationTemplate(YI, (4, 1, 3)))
    lex.add_station_template(StationTemplate(BING, (4, 2, 3)))
    return lex


def make_splice_table() -> SpliceTable:
    return SpliceTable([Joint("H_41", "T_03", 1), Joint("H_41", "T_13", 1)], 1)


class PCMJointTests(unittest.TestCase):
    def test_overlap_join_adds_overlap_samples(self):
        self.assertEqual(overlap_join((4, 1), (0, 3), 1), (4, 1, 3))
        self.assertEqual(overlap_join((4, 1), (1, 3), 1), (4, 2, 3))
        self.assertEqual(overlap_join((1, 2, 3), (3, 4), 1), (1, 2, 6, 4))

    def test_overlap_join_length_and_no_recut(self):
        head, tail = (4, 1), (0, 3)
        joined = overlap_join(head, tail, 1)
        self.assertEqual(len(joined), len(head) + len(tail) - 1)
        with self.assertRaises(JointError):
            overlap_join(head, tail, 3)   # 超出两个片段边界，禁止重剪
        with self.assertRaises(JointError):
            overlap_join(head, tail, 0)   # 至少重叠一格

    def test_splice_table_enforces_single_cell_overlap(self):
        table = SpliceTable(allowed_overlap=1)
        with self.assertRaises(Exception):
            table.add(Joint("a", "b", 2))
        table.add(Joint("H_41", "T_03", 1))
        with self.assertRaises(Exception):
            table.get("H_41", "T_13")  # 无接头，禁止重剪


class TemplateAndGearTests(unittest.TestCase):
    def test_tolerance_match_and_length_boundary(self):
        t = CharacterTemplate("尾", "S", "档A", (1, 2), 1)
        self.assertTrue(t.admits((1, 3)))
        self.assertFalse(t.admits((1, 4)))
        self.assertFalse(t.admits((1,)))  # 采样边界必须一致

    def test_one_recording_two_readings_kept(self):
        templates = [
            CharacterTemplate("云", "G", "高档", (5,), 1),
            CharacterTemplate("林", "G", "高档", (5,), 2),
        ]
        hits = segment_matches((5,), templates)
        self.assertEqual({t.character for t in hits}, {"云", "林"})

    def test_gear_viable_only_if_all_segments_fit(self):
        templates = [
            CharacterTemplate("云", "SG", "高档", (5,), 1),
            CharacterTemplate("林", "SG", "高档", (5,), 2),
            CharacterTemplate("风", "SG", "低档", (5,), 1),
            CharacterTemplate("雨", "SG", "低档", (6,), 2),
        ]
        analysis = analyze_speaker("SG", [("P1", (5,)), ("P2", (8,))], templates)
        # 高档对 P2 无任何容许模板 → 高档被排除；低档两段都成立
        self.assertEqual(analysis.candidate_gears, ("低档",))
        p1 = next(e for e in analysis.evidence_for("低档") if e.asset_id == "P1")
        self.assertEqual(set(p1.readings), {"风", "雨"})
        p2 = next(e for e in analysis.evidence_for("低档") if e.asset_id == "P2")
        self.assertEqual(p2.readings, ("雨",))


class LexiconRevisionTests(unittest.TestCase):
    def test_derivation_follows_master_and_releases_immutable(self):
        lex = make_lexicon()
        r1 = lex.release("首版")
        self.assertEqual(r1.assets["T_JIA"].pcm, (1, 2))

        lex.revise_master("M_JIA", [4, 1, 3], "末样本改3")
        self.assertEqual(lex.assets["M_JIA"].pcm, (4, 1, 3))
        self.assertEqual(lex.assets["T_JIA"].pcm, (1, 3))  # 分出资产联动
        self.assertEqual(r1.assets["M_JIA"].pcm, (4, 1, 2))  # 旧版不变
        with self.assertRaises(LexiconError):
            lex.revise_master("H_JIA", [9], "不能改分出资产")

    def test_restore_and_window_truncation_rejected(self):
        lex = make_lexicon()
        lex.release("首版")
        lex.revise_master("M_JIA", [4, 1, 3], "改版")
        lex.restore_master("M_JIA", "恢复")
        r2 = lex.release("恢复版")
        rendering = render_plan(r2, WholeWordPlan("M_JIA"), make_splice_table())
        report = adjudicate(r2, rendering, JIA, [YI, BING], window_samples=2)
        self.assertFalse(report.can_air)
        self.assertEqual(report.reason, Reason.WINDOW_TOO_SMALL)

    def test_derive_out_of_bound_rejected(self):
        lex = Lexicon()
        lex.record_master("M", "whole_word", "S", [1, 2], "站")
        with self.assertRaises(LexiconError):
            lex.derive("D", "M", "tail", "尾", 1, 9)

    def test_sample_budget_counts_only_real_recordings(self):
        lex = Lexicon(sample_budget=5)
        lex.record_master("M", "whole_word", "S", [1, 2, 3], "站")  # 3
        lex.derive("D", "M", "tail", "尾", 0, 1)                    # 不计费
        with self.assertRaises(LexiconError):                       # 再录 3 超 5
            lex.record_master("M2", "tail", "S", [4, 5, 6], "尾")


class HearingAndRenderTests(unittest.TestCase):
    def setUp(self):
        self.lex = make_lexicon()
        self.rel = self.lex.release("发版")
        self.table = make_splice_table()

    def test_render_must_come_from_release(self):
        with self.assertRaises(LexiconError):
            render_plan(self.rel, WholeWordPlan("GHOST"), self.table)

    def test_whole_word_jia_air_with_zero_tolerance(self):
        rendering = render_plan(self.rel, WholeWordPlan("M_JIA"), self.table)
        report = adjudicate(self.rel, rendering, JIA, [YI, BING], 3)
        self.assertTrue(report.can_air)
        self.assertEqual(report.station_hits, (JIA,))

    def test_splice_yi_ok_and_bing_is_neighbor_mixup(self):
        yi = render_plan(self.rel, SplicePlan("H_41", "T_03", 1), self.table)
        report_yi = adjudicate(self.rel, yi, YI, [BING], 3)
        self.assertTrue(report_yi.can_air)
        self.assertEqual(yi.pcm, (4, 1, 3))

        bing = render_plan(self.rel, SplicePlan("H_41", "T_13", 1), self.table)
        report_b = adjudicate(self.rel, bing, YI, [BING], 3)
        self.assertFalse(report_b.can_air)
        self.assertEqual(report_b.reason, Reason.NEIGHBOR_MIXUP)
        self.assertEqual(report_b.station_hits, (BING,))

    def test_no_candidate_is_missing_recording(self):
        # 构造一个不属于任何严格模板的渲染（直接用片段 PCM 裁决）
        weird = Rendering("发版", "splice", ("H_41",), ((4, 1),), (7, 7, 7), 0)
        report = adjudicate(self.rel, weird, YI, [BING], 3)
        self.assertFalse(report.can_air)
        self.assertEqual(report.reason, Reason.NO_CANDIDATE)


def build_desks():
    schedule = Schedule()
    schedule.add_trip(Trip(1, "上行", JIA, (YI, BING), 3))
    schedule.add_trip(Trip(2, "上行", YI, (BING,), 3))
    booking = BookingTable()
    calendar = RecordingCalendar([3, 5])
    rental_ledger, buyout_ledger = CostLedger("租"), CostLedger("买")
    rental = BroadcastDesk("租用店", ContractKind.RENTAL, schedule,
                           make_splice_table(), booking, rental_ledger)
    buyout = BroadcastDesk("买断店", ContractKind.BUYOUT, schedule,
                           make_splice_table(), booking, buyout_ledger,
                           calendar=calendar)
    return schedule, booking, calendar, rental, buyout


class ContractAndTripTests(unittest.TestCase):
    def setUp(self):
        self.lex = make_lexicon()
        self.rel = self.lex.release("发版")
        (self.schedule, self.booking, self.calendar,
         self.rental, self.buyout) = build_desks()

    def test_first_trip_signed_and_replay_after_departure(self):
        for desk in (self.rental, self.buyout):
            d = desk.evaluate_whole_word(self.rel, 1, "M_JIA")
            self.assertTrue(d.signed)
        self.schedule.mark_departed(1)
        order = self.rental.replay(1)
        self.assertEqual(order.pcm_snapshot, (4, 1, 2))
        # 词库改版后回听仍是原音
        self.lex.revise_master("M_JIA", [4, 1, 3], "改版")
        self.assertEqual(self.rental.replay(1).pcm_snapshot, (4, 1, 2))

    def test_second_trip_rental_scenario_a_tail_available(self):
        bad = self.rental.evaluate_splice(self.rel, 2, "H_41", "T_13")
        self.assertFalse(bad.signed)
        self.assertEqual(bad.reason, Reason.NEIGHBOR_MIXUP)
        good = self.rental.evaluate_splice(self.rel, 2, "H_41", "T_03")
        self.assertTrue(good.signed)
        self.assertEqual(good.order.pcm_snapshot, (4, 1, 3))

    def test_second_trip_rental_scenario_b_tail_exclusive(self):
        self.booking.grant_exclusive("T_03", 9, owner="other")  # 另一班独占
        self.rental.evaluate_splice(self.rel, 2, "H_41", "T_13")
        blocked = self.rental.evaluate_splice(self.rel, 2, "H_41", "T_03")
        self.assertFalse(blocked.signed)
        self.assertEqual(blocked.reason, "短单元已被独占")
        # 本班没有任何签播
        self.assertNotIn(2, self.schedule.orders)
        reasons = {r.reason for r in self.schedule.rejections if r.trip_no == 2}
        self.assertIn(Reason.NEIGHBOR_MIXUP, reasons)
        self.assertIn("短单元已被独占", reasons)

    def test_buyout_rejects_splice_and_defers_to_new_slot(self):
        refused = self.buyout.evaluate_splice(self.rel, 2, "H_41", "T_03")
        self.assertFalse(refused.signed)
        self.assertEqual(refused.reason, "合同不许可")
        deferred = self.buyout.evaluate_whole_word(self.rel, 2, "M_JIA")
        self.assertFalse(deferred.signed)
        self.assertEqual(deferred.deferred_slot, 3)  # 次班(2)之后的档期

    def test_buyout_record_then_air_and_locked_master_no_tail_swap(self):
        slot = self.buyout.book_new_recording_slot()
        self.assertEqual(slot, 3)
        self.lex.record_master("M_YI", "whole_word", "S1", [4, 1, 3], YI)
        r2 = self.lex.release("补录版")
        self.schedule.add_trip(Trip(3, "上行", YI, (BING,), 3))
        first = self.buyout.evaluate_whole_word(r2, 3, "M_YI")
        self.assertTrue(first.signed)
        self.schedule.add_trip(Trip(4, "上行", YI, (BING,), 3))
        locked = self.buyout.evaluate_whole_word(r2, 4, "M_YI")
        self.assertFalse(locked.signed)
        self.assertEqual(locked.reason, "整词已买断锁定")

    def test_rental_tail_reuse_across_trips_each_own_evidence(self):
        self.schedule.add_trip(Trip(3, "上行", YI, (BING,), 3))
        d2 = self.rental.evaluate_splice(self.rel, 2, "H_41", "T_03")
        d3 = self.rental.evaluate_splice(self.rel, 3, "H_41", "T_03")
        self.assertTrue(d2.signed and d3.signed)  # 尾字跨班复用
        self.assertEqual(d2.order.hearing.station_hits, d3.order.hearing.station_hits)
        self.assertIsNot(d2.order, d3.order)      # 每次接头/听辨独立留痕


class PersistenceTests(unittest.TestCase):
    def test_save_and_reload_keeps_orders_and_catalog(self):
        root = tempfile.mkdtemp()
        try:
            lex = make_lexicon()
            table = make_splice_table()
            schedule, booking, calendar, rental, buyout = build_desks()
            rel = lex.release("发版")
            rental.evaluate_whole_word(rel, 1, "M_JIA")
            buyout.evaluate_whole_word(rel, 1, "M_JIA")
            schedule.mark_departed(1)
            store = WorkspaceStore(root)
            store.save(lex, table, schedule, booking,
                       rental.ledger, buyout.ledger, rental, buyout)
            self.assertTrue(store.exists())

            reopened = store.load()
            self.assertEqual(
                reopened["rental_desk"].replay(1).pcm_snapshot, (4, 1, 2)
            )
            self.assertEqual(
                reopened["buyout_desk"].replay(1).pcm_snapshot, (4, 1, 2)
            )
            # 同一本目录：资产一致
            self.assertEqual(
                sorted(reopened["lexicon"].catalog(), key=lambda x: x["id"]),
                sorted(lex.catalog(), key=lambda x: x["id"]),
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
