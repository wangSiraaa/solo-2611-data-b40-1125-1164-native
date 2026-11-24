"""返券柜台端到端测试：求解 / 证据视图 / 预算 / 领券权 / 封账 / 纠正 / Warehouse。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from rebate import (
    CENTER_STATION,
    CollectionNetwork,
    Counter,
    CounterError,
    MODE_STATION,
    MODE_UNIFIED,
    NODE_BAG,
    NODE_BOX,
    NODE_HOUSEHOLD,
    NODE_TRIP,
    ORIGIN_CENTER,
    ORIGIN_STATION,
    PartialWeigh,
    REASON_BELOW_LINE,
    REASON_BUDGET_EXHAUSTED,
    REASON_DUPLICATE_CLAIM,
    REASON_RIGHT_IN_TRANSIT,
    REASON_WEIGHT_UNPROVEN,
    RESULT_DISPUTE,
    RESULT_ISSUED,
    RESULT_REJECTED,
    RESULT_SUSPENDED,
    RIGHT_HANDED_OVER,
    RIGHT_RECEIVED,
    RIGHT_SPENT,
    import_confirmed_trips,
)
from weighbridge import Catalog, IN, LOAD, OUT, Warehouse


# --------------------------------------------------------------- 网络/求解
class NetworkSolverTests(unittest.TestCase):
    def _two_bags_total_ten(self) -> CollectionNetwork:
        n = CollectionNetwork()
        n.add_box("BX", "TRIP1")
        # 甲乙各交一袋，每袋纸重只可能 4 或 6；车净重精确 10。
        n.add_bag("B甲", "甲", "BX", gross=6, tare_lo=0, tare_hi=2,
                  allowed_paper=(4, 6))
        n.add_bag("B乙", "乙", "BX", gross=6, tare_lo=0, tare_hi=2,
                  allowed_paper=(4, 6))
        n.add_trip_version("TRIP1", 10, 10)
        return n

    def test_total_alone_leaves_two_indistinguishable_worlds(self):
        n = self._two_bags_total_ten()
        r = n.solve()
        self.assertTrue(r.feasible)
        worlds = {
            tuple(sorted(w.household_paper.items())) for w in r.worlds
        }
        self.assertEqual(
            worlds,
            {
                tuple(sorted((("甲", 4), ("乙", 6)))),
                tuple(sorted((("甲", 6), ("乙", 4)))),
            },
        )
        # 没有别的证据时两户重量都无法证实。
        self.assertIsNone(r.proven["甲"])
        self.assertIsNone(r.proven["乙"])

    def test_own_partial_weigh_proves_household(self):
        n = self._two_bags_total_ten()
        n.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                                   ORIGIN_CENTER, household_id="甲"))
        r = n.solve()
        self.assertEqual(r.proven["甲"], (4, 4))
        self.assertEqual(r.proven["乙"], (6, 6))

    def test_other_household_weighing_can_prove_by_elimination(self):
        # 只有乙出示 6，甲无个人称量；总量约束反推出甲=4。
        n = self._two_bags_total_ten()
        n.add_partial(PartialWeigh("W乙", NODE_BAG, "B乙", 6, 6,
                                   ORIGIN_CENTER, household_id="乙"))
        r = n.solve()
        self.assertEqual(r.proven["甲"], (4, 4))

    def test_sealed_transfer_counts_exactly_once(self):
        # T1: B1(甲,4,转出)+B0(丙,3) 转出后 T1 净重只剩 3；
        # T2: B2 接收 B1(=4)+B3(乙,2) 净重 6。B1 全程只归一次。
        n = CollectionNetwork()
        for bx, tid in (("B0", "T1"), ("B1", "T1"), ("B2", "T2"), ("B3", "T2")):
            n.add_box(bx, tid)
        n.add_bag("g0", "丙", "B0", 3, allowed_paper=(3,))
        n.add_bag("g1", "甲", "B1", 4, allowed_paper=(4,))
        n.add_bag("g2", "乙", "B3", 2, allowed_paper=(2,))
        n.add_trip_version("T1", 3, 3)
        n.add_trip_version("T2", 6, 6)
        n.add_transfer("B1", "B2")
        r = n.solve()
        self.assertTrue(r.feasible)
        self.assertEqual(r.proven, {"丙": (3, 3), "甲": (4, 4), "乙": (2, 2)})
        # 重复计入 B1 的世界（T2=10）不可行。
        n.trips["T2"] = type(n.trips["T2"])("T2", 10, 10, 1)
        self.assertFalse(n.solve().feasible)

    def test_station_view_hides_other_station_private_weigh(self):
        n = self._two_bags_total_ten()
        n.add_partial(PartialWeigh("W乙", NODE_BAG, "B乙", 6, 6,
                                   ORIGIN_STATION, station_id="S2",
                                   household_id="乙"))
        # 中心看得见：甲可由乙的 6 反推。
        self.assertEqual(n.solve(None).proven["甲"], (4, 4))
        # S1 看不见 S2 私重：甲仍不可证实。
        self.assertIsNone(n.solve("S1").proven["甲"])
        # S2 自己看得见。
        self.assertEqual(n.solve("S2").proven["甲"], (4, 4))

    def test_household_level_partial_weigh(self):
        n = self._two_bags_total_ten()
        n.add_partial(PartialWeigh("WH", NODE_HOUSEHOLD, "甲", 4, 4,
                                   ORIGIN_CENTER, household_id="甲"))
        self.assertEqual(n.solve().proven["甲"], (4, 4))

    def test_infeasible_network_detected(self):
        n = self._two_bags_total_ten()
        n.trips["TRIP1"] = type(n.trips["TRIP1"])("TRIP1", 99, 99, 1)
        r = n.solve()
        self.assertFalse(r.feasible)


# --------------------------------------------------------------- 柜台决策
def build_two_household_counter(mode: str, budget: int = 2) -> Counter:
    c = Counter(mode=mode)
    c.register_station("S1")
    c.register_station("S2")
    c.register_household("甲")
    c.register_household("乙")
    c.open_period("P1", reduction_line=4, claim_deadline=100,
                  central_budget=budget if mode == MODE_UNIFIED else None)
    if mode == MODE_STATION:
        c.allocate_budget("P1", budget, "S1")
        c.allocate_budget("P1", budget, "S2")
    c.add_box("BX", "TRIP1")
    c.add_bag("B甲", "甲", "BX", 6, allowed_paper=(4, 6))
    c.add_bag("B乙", "乙", "BX", 6, allowed_paper=(4, 6))
    c.add_trip_version("TRIP1", 10, 10)
    return c


class CounterDecisionTests(unittest.TestCase):
    def test_no_evidence_both_suspended(self):
        c = build_two_household_counter(MODE_UNIFIED)
        d1 = c.apply("P1", "甲")
        d2 = c.apply("P1", "乙")
        self.assertEqual(d1.result, RESULT_SUSPENDED)
        self.assertEqual(d1.reason, REASON_WEIGHT_UNPROVEN)
        self.assertEqual(d2.result, RESULT_SUSPENDED)
        self.assertEqual(c.reconcile(), [])

    def test_own_weighing_issues_in_both_modes(self):
        for mode in (MODE_UNIFIED, MODE_STATION):
            with self.subTest(mode=mode):
                c = build_two_household_counter(mode)
                station = CENTER_STATION if mode == MODE_UNIFIED else "S1"
                c.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                                           ORIGIN_CENTER, household_id="甲"))
                d = c.apply("P1", "甲", station_id=station)
                self.assertEqual(d.result, RESULT_ISSUED)
                self.assertEqual(d.proven, 4)
                self.assertEqual(c.right_state("P1", "甲")["state"], RIGHT_SPENT)
                self.assertEqual(c.reconcile(), [])

    def test_zero_budget_blocks_issuance(self):
        c = build_two_household_counter(MODE_UNIFIED, budget=0)
        c.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                                   ORIGIN_CENTER, household_id="甲"))
        d = c.apply("P1", "甲")
        self.assertEqual(d.result, RESULT_SUSPENDED)
        self.assertEqual(d.reason, REASON_BUDGET_EXHAUSTED)

    def test_below_reduction_line_rejected(self):
        c = Counter(mode=MODE_UNIFIED)
        c.register_household("甲")
        c.open_period("P1", reduction_line=4, claim_deadline=100, central_budget=5)
        c.add_box("BX", "T")
        c.add_bag("B甲", "甲", "BX", 2, allowed_paper=(2,))
        c.add_trip_version("T", 2, 2)
        c.add_partial(PartialWeigh("W", NODE_BAG, "B甲", 2, 2,
                                   ORIGIN_CENTER, household_id="甲"))
        d = c.apply("P1", "甲")
        self.assertEqual(d.result, RESULT_REJECTED)
        self.assertEqual(d.reason, REASON_BELOW_LINE)

    def test_unified_signs_by_elimination_station_keeps_order(self):
        # 只有乙向中心出示 6，甲无个人称量。
        # 统一封账：中心看得到，能反推甲=4 并签给甲。
        cu = build_two_household_counter(MODE_UNIFIED)
        cu.add_partial(PartialWeigh("W乙", NODE_BAG, "B乙", 6, 6,
                                    ORIGIN_CENTER, household_id="乙"))
        du = cu.apply("P1", "甲")
        self.assertEqual(du.result, RESULT_ISSUED)
        self.assertEqual(du.proven, 4)

        # 分站断网：乙的凭条是 S2 私重，S1 看不到，甲只能留单。
        cs = build_two_household_counter(MODE_STATION)
        cs.ensure_right("P1", "甲", "S1")
        cs.add_partial(PartialWeigh("W乙", NODE_BAG, "B乙", 6, 6,
                                    ORIGIN_STATION, station_id="S2",
                                    household_id="乙"))
        ds = cs.apply("P1", "甲", station_id="S1")
        self.assertEqual(ds.result, RESULT_SUSPENDED)
        self.assertEqual(ds.reason, REASON_WEIGHT_UNPROVEN)
        # 甲自己的凭条到了即可办。
        cs.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                                    ORIGIN_STATION, station_id="S1",
                                    household_id="甲"))
        ds2 = cs.apply("P1", "甲", station_id="S1")
        self.assertEqual(ds2.result, RESULT_ISSUED)

    def test_last_quota_exhaustion_blocks_second_household(self):
        c = build_two_household_counter(MODE_UNIFIED, budget=1)
        c.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                                   ORIGIN_CENTER, household_id="甲"))
        c.add_partial(PartialWeigh("W乙", NODE_BAG, "B乙", 6, 6,
                                   ORIGIN_CENTER, household_id="乙"))
        self.assertEqual(c.apply("P1", "甲").result, RESULT_ISSUED)
        d = c.apply("P1", "乙")
        self.assertEqual(d.result, RESULT_SUSPENDED)
        self.assertEqual(d.reason, REASON_BUDGET_EXHAUSTED)


# --------------------------------------------------------------- 跨站领券权
class ClaimRightTests(unittest.TestCase):
    def test_handover_blocks_both_stations_until_received(self):
        c = build_two_household_counter(MODE_STATION)
        c.ensure_right("P1", "甲", "S1")
        c.hand_over("P1", "甲", "S1", "S2", msg_id="M1")
        self.assertEqual(c.right_state("P1", "甲")["state"], RIGHT_HANDED_OVER)
        for station in ("S1", "S2"):
            d = c.apply("P1", "甲", station_id=station)
            self.assertEqual(d.result, RESULT_REJECTED)
            self.assertEqual(d.reason, REASON_RIGHT_IN_TRANSIT)
        c.receive_right("P1", "甲", "S2", msg_id="M2")
        self.assertEqual(c.right_state("P1", "甲")["state"], RIGHT_RECEIVED)

    def test_resend_after_receive_does_not_restore_origin(self):
        c = build_two_household_counter(MODE_STATION)
        c.ensure_right("P1", "甲", "S1")
        c.hand_over("P1", "甲", "S1", "S2", msg_id="M1")
        c.receive_right("P1", "甲", "S2", msg_id="M2")
        # 原站补送同一消息：幂等，不把权打回在途，也不恢复 S1。
        c.hand_over("P1", "甲", "S1", "S2", msg_id="M1")
        self.assertEqual(c.right_state("P1", "甲")["state"], RIGHT_RECEIVED)
        # S2 凭自己的证据签出。
        c.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                                   ORIGIN_STATION, station_id="S2",
                                   household_id="甲"))
        self.assertEqual(c.apply("P1", "甲", station_id="S2").result,
                         RESULT_ISSUED)
        # 已交出原额的原站 S1 再次申请：只按 S1 证据出回执，重复申领被拒。
        d = c.apply("P1", "甲", station_id="S1")
        self.assertEqual(d.result, RESULT_REJECTED)
        self.assertEqual(d.reason, REASON_DUPLICATE_CLAIM)

    def test_receipt_visibility_redacts_other_station_private_weight(self):
        c = build_two_household_counter(MODE_STATION)
        c.ensure_right("P1", "甲", "S1")
        c.hand_over("P1", "甲", "S1", "S2")
        c.receive_right("P1", "甲", "S2")
        c.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                                   ORIGIN_STATION, station_id="S2",
                                   household_id="甲"))
        d = c.apply("P1", "甲", station_id="S2")
        s2 = c.receipt(d.receipt_id, viewer_role=ORIGIN_STATION,
                       viewer_station="S2")
        s1 = c.receipt(d.receipt_id, viewer_role=ORIGIN_STATION,
                       viewer_station="S1")
        self.assertIn("W甲", [p["weigh_id"] for p in s2["evidence"]["visible_partials"]])
        self.assertNotIn("W甲",
                         [p["weigh_id"] for p in s1["evidence"]["visible_partials"]])
        # 公开总量任何角色都看得到。
        self.assertIn("TRIP1", c.public_totals())


# --------------------------------------------------------------- 封账与争议
class ClosureDisputeTests(unittest.TestCase):
    def _issued_period(self) -> tuple[Counter, int]:
        c = Counter(mode=MODE_UNIFIED)
        c.register_household("甲")
        c.register_household("乙")
        c.open_period("P1", 4, 100, central_budget=5)
        c.add_box("BX", "T")
        c.add_box("BY", "T")
        c.add_bag("B甲", "甲", "BX", 6, allowed_paper=(4, 6))
        c.add_bag("B乙", "乙", "BY", 6, allowed_paper=(4, 6))
        c.add_trip_version("T", 10, 10)
        c.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                                   ORIGIN_CENTER, household_id="甲"))
        d = c.apply("P1", "甲")
        self.assertEqual(d.result, RESULT_ISSUED)
        return c, d.receipt_id

    def test_post_closure_only_dispute_no_second_coupon(self):
        c, rid = self._issued_period()
        c.close_period("P1", at_time=90)
        # 封账后正常申请一律拒签，不会在原券旁另兑。
        late = c.apply("P1", "甲")
        self.assertEqual(late.result, RESULT_REJECTED)
        # 称量纠正：净重 10->12，甲凭条 4->6（旧凭条归档留档）。
        c.add_trip_version("T", 12, 12, source="weighing-correction")
        c.correct_partial("W甲", "W甲.v2", 6, 6)
        disp = c.open_dispute("P1", "甲", original_receipt_id=rid)
        self.assertEqual(disp.result, RESULT_DISPUTE)
        self.assertEqual(disp.coupon_value, 0)
        self.assertEqual(disp.evidence["weight_delta"], 2)
        # 版本链完整、旧版归档。
        versions = c.trip_versions("T")
        self.assertEqual([v["active"] for v in versions], [0, 1])
        self.assertEqual(c.reconcile(), [])

    def test_downward_correction_records_negative_delta(self):
        c = Counter(mode=MODE_UNIFIED)
        c.register_household("甲")
        c.open_period("P1", 4, 100, central_budget=5)
        c.add_box("BX", "T")
        c.add_bag("B甲", "甲", "BX", 6, allowed_paper=(2, 4, 6))
        c.add_trip_version("T", 4, 4)
        c.add_partial(PartialWeigh("W", NODE_BAG, "B甲", 4, 4,
                                   ORIGIN_CENTER, household_id="甲"))
        rid = c.apply("P1", "甲").receipt_id
        c.close_period("P1", 90)
        c.add_trip_version("T", 2, 2, source="correction")
        c.correct_partial("W", "W2", 2, 2)
        disp = c.open_dispute("P1", "甲", original_receipt_id=rid)
        self.assertEqual(disp.evidence["weight_delta"], -2)
        self.assertEqual(c.reconcile(), [])

    def test_dispute_requires_closed_period(self):
        c, _ = self._issued_period()
        with self.assertRaises(CounterError):
            c.open_dispute("P1", "甲")


# --------------------------------------------------------------- Warehouse 集成
class WarehouseAdapterTests(unittest.TestCase):
    def test_confirmed_trips_and_review_chain_become_net_versions(self):
        cat = Catalog()
        cat.define_mass("tractor", "T1", 7000, 7200)
        cat.define_mass("trailer", "R1", 1500, 1600)
        cat.apply_attach("T1", "R1")
        w = Warehouse(cat)
        w.add_ticket({"ticket_id": "K1", "tractor": "T1", "catalog_seq": 1,
                      "ts": 100, "direction": IN, "gross": 9000})
        w.add_ticket({"ticket_id": "K2", "tractor": "T1", "catalog_seq": 1,
                      "ts": 200, "direction": OUT, "gross": 12000})
        ev = w.preview(LOAD, "K1", "K2")
        w.confirm(ev.trip_id)
        # 迟到目录更正 -> REVIEW 运输，净重仍是精确毛重差 3000。
        cat.apply_set_mass("trailer", "R1", 1400, 1500)
        re = w.reopen(ev.trip_id)
        self.assertEqual((re.net_lo, re.net_hi), (3000, 3000))

        c = Counter(mode=MODE_UNIFIED)
        c.register_household("甲")
        latest = import_confirmed_trips(
            c, w, box_for_trip={ev.trip_id: "BX"}
        )
        self.assertEqual(latest[ev.trip_id], 2)
        versions = c.trip_versions(ev.trip_id)
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual([v["active"] for v in versions], [0, 1])
        # 只有 CONFIRMED 车次被导入：草稿/复核不另起炉灶。
        self.assertEqual(set(c.public_totals()), {ev.trip_id})

    def test_draft_trips_are_not_imported(self):
        cat = Catalog()
        cat.define_mass("tractor", "T1", 7000, 7200)
        cat.define_mass("trailer", "R1", 1500, 1600)
        cat.apply_attach("T1", "R1")
        w = Warehouse(cat)
        w.add_ticket({"ticket_id": "K1", "tractor": "T1", "catalog_seq": 1,
                      "ts": 1, "direction": IN, "gross": 9000})
        w.add_ticket({"ticket_id": "K2", "tractor": "T1", "catalog_seq": 1,
                      "ts": 2, "direction": OUT, "gross": 12000})
        w.preview(LOAD, "K1", "K2")  # 仅 DRAFT，未 confirm
        c = Counter(mode=MODE_UNIFIED)
        self.assertEqual(import_confirmed_trips(c, w), {})


if __name__ == "__main__":
    unittest.main()
