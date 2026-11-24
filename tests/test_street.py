"""腾街单：磅单连接、物料守恒、禁倒堆位、岔口观测和反事实重放。"""

from __future__ import annotations

import unittest

from weighbridge import (
    Catalog,
    IN,
    OUT,
    StreetOffice,
    UNLOAD,
    Warehouse,
)
from weighbridge.scenarios import (
    build_ban_case,
    build_fork_market_case,
    build_route_case,
    build_witness_case,
    exact_unload_warehouse,
    replay_fork_worlds,
)
from weighbridge.street import (
    METHOD_FIXED,
    SlotUnavailable,
    STATUS_ACCEPTED,
    STATUS_REJECTED,
)


def warehouse_with_net_six() -> tuple[Warehouse, str]:
    catalog = Catalog()
    catalog.define_mass("tractor", "T1", 1000, 1000)
    wb = Warehouse(catalog)
    wb.add_ticket({"ticket_id": "IN6", "tractor": "T1", "catalog_seq": 0,
                   "ts": 1, "direction": IN, "gross": 1006})
    wb.add_ticket({"ticket_id": "OUT0", "tractor": "T1", "catalog_seq": 0,
                   "ts": 2, "direction": OUT, "gross": 1000})
    ev = wb.preview(UNLOAD, "IN6", "OUT0")
    return wb, wb.confirm(ev.trip_id).trip_id


class MassReconstructionTests(unittest.TestCase):
    def test_confirmed_trip_net_is_used_and_witness_clears_jia(self):
        wb, trip_id = warehouse_with_net_six()
        office = StreetOffice(wb)
        office.add_segments("甲", "乙", "丙")
        office.set_initial_snow("甲", 5)
        office.set_initial_snow("乙", 5)
        office.reference_confirmed_trip(trip_id)
        office.add_pickup("P-甲", trip_id, "甲", lo=0, hi=6)
        office.record_loading_witness("W-乙", trip_id, "乙", 1)

        recon = office.reconstruct()
        self.assertTrue(recon.feasible)
        self.assertEqual(recon.remaining_range["甲"], [0, 0])
        self.assertEqual(recon.remaining_range["乙"], [4, 4])
        self.assertEqual(recon.trip_allocations[trip_id]["segments"]["甲"], [5, 5])
        self.assertEqual(recon.trip_allocations[trip_id]["segments"]["乙"], [1, 1])

    def test_trip_weight_alone_does_not_clear_jia(self):
        case = build_witness_case(replaced=False)
        # 无见证的原始办公室另建：净重 6 可以是甲 1..5。
        wb, trip_id = warehouse_with_net_six()
        office = StreetOffice(wb)
        office.add_segments("甲", "乙", "丙")
        office.set_initial_snow("甲", 5)
        office.set_initial_snow("乙", 5)
        office.reference_confirmed_trip(trip_id)
        office.add_pickup("P-甲", trip_id, "甲", lo=0, hi=6)
        office.add_pickup("P-乙", trip_id, "乙", lo=0, hi=6)
        recon = office.reconstruct()
        self.assertTrue(recon.feasible)
        self.assertEqual(recon.remaining_range["甲"], [0, 4])
        self.assertIn(case["order"].status, {STATUS_ACCEPTED})

    def test_replacing_one_witness_rejects_same_commitment(self):
        ok = build_witness_case(replaced=False)
        bad = build_witness_case(replaced=True)
        self.assertEqual(ok["order"].status, STATUS_ACCEPTED)
        self.assertEqual(bad["order"].status, STATUS_REJECTED)
        self.assertIn("REMAINING_ABOVE_LINE", bad["order"].reasons[0])
        recon = bad["office"].reconstruct()
        # 乙装 5 后，甲只能装 1，剩 4。
        self.assertEqual(recon.remaining_range["甲"], [4, 4])
        self.assertEqual(recon.remaining_range["乙"], [0, 0])

    def test_transfer_is_not_export_and_net_cannot_be_claimed_twice(self):
        office = StreetOffice()
        office.add_segments("甲", "乙", "丙")
        office.set_initial_snow("甲", 5)
        office.set_initial_snow("乙", 0)
        # 四公斤只从甲转到乙，没有装车；街上总雪量不变。
        office.add_transfer("TR-1", "甲", "乙", 4)
        recon = office.reconstruct()
        self.assertTrue(recon.feasible)
        self.assertEqual(recon.remaining_range["甲"], [1, 1])
        self.assertEqual(recon.remaining_range["乙"], [4, 4])

        # 另建物理上足够厚的两段；两段都把同一车六公斤认作自己的装车量，
        # 虽不违反堆量，却违反确认车次净重，因此没有可行世界。
        claim_office = StreetOffice()
        claim_office.add_segments("甲", "乙", "丙")
        claim_office.set_initial_snow("甲", 10)
        claim_office.set_initial_snow("乙", 10)
        claim_office.add_confirmed_trip("T1", 6)
        claim_office.record_loading_witness("L-甲", "T1", "甲", 6)
        claim_office.record_loading_witness("L-乙", "T1", "乙", 6)
        conflict = claim_office.reconstruct()
        self.assertFalse(conflict.feasible)

    def test_only_confirmed_warehouse_trip_can_be_quoted(self):
        catalog = Catalog()
        catalog.define_mass("tractor", "T1", 1000, 1000)
        wb = Warehouse(catalog)
        wb.add_ticket({"ticket_id": "IN", "tractor": "T1", "catalog_seq": 0,
                       "ts": 1, "direction": IN, "gross": 1006})
        wb.add_ticket({"ticket_id": "OUT", "tractor": "T1", "catalog_seq": 0,
                       "ts": 2, "direction": OUT, "gross": 1000})
        draft = wb.preview(UNLOAD, "IN", "OUT")
        office = StreetOffice(wb)
        with self.assertRaises(Exception):
            office.reference_confirmed_trip(draft.trip_id)


class SafeguardTests(unittest.TestCase):
    def test_ban_needs_signable_alternative_slot(self):
        case = build_ban_case()
        self.assertIsNone(case["ban_error"])
        self.assertEqual(case["order"].status, STATUS_ACCEPTED)
        q = case["office"].query_order("SR-禁倒")
        self.assertEqual(q["granted_slots"], ["丙位-1kg"])
        self.assertTrue(q["granted_slot_details"][0]["leases"][0]["signed"])

    def test_revoking_single_ban_rejects_ban_backed_order(self):
        ok = build_ban_case(revoked=False)
        revoked = build_ban_case(revoked=True)
        self.assertEqual(ok["order"].status, STATUS_ACCEPTED)
        self.assertEqual(revoked["order"].status, STATUS_REJECTED)
        self.assertIn("NO_ACTIVE_NO_DUMP", revoked["order"].reasons[0])

    def test_removing_one_reachable_point_rejects_route_order(self):
        ok = build_route_case(point_removed=False)
        bad = build_route_case(point_removed=True)
        self.assertEqual(ok["order"].status, STATUS_ACCEPTED)
        self.assertEqual(bad["order"].status, STATUS_REJECTED)
        self.assertIn("ROUTE_CANNOT_COVER", bad["order"].reasons[0])

    def test_late_observation_cannot_use_forecast_as_sight(self):
        late = build_route_case(late=True)
        self.assertEqual(late["order"].status, STATUS_REJECTED)
        self.assertIn("OBSERVATION_AFTER_FORK", late["order"].reasons[0])


class ForkMarketTests(unittest.TestCase):
    def test_two_landing_worlds_and_both_replay_jia_yi_open(self):
        case = build_fork_market_case()
        self.assertEqual(case["order"].status, STATUS_ACCEPTED)
        self.assertEqual(case["order"].evaluation["selected_method"], "OBSERVED_FORK")
        replays = replay_fork_worlds(case)
        self.assertEqual([r["landing_segment"] for r in replays], ["甲", "乙"])
        self.assertEqual([r["target_segment"] for r in replays], ["甲", "乙"])
        self.assertTrue(all(r["requested_open"] for r in replays))
        self.assertEqual([r["open_blocks"][0][:2] for r in replays],
                         [["甲", "乙"], ["甲", "乙"]])

    def test_c_rented_blocks_ban_but_live_observation_still_works(self):
        case = build_fork_market_case(c_rented=True, sign_ban=True)
        self.assertIsInstance(case["ban_error"], SlotUnavailable)
        self.assertEqual(case["office"].query_order("ND-前夜")["status"], STATUS_REJECTED)
        # 禁倒单失败不占用堆位之外的状态；观测方案仍受理同一早市单。
        self.assertEqual(case["order"].status, STATUS_ACCEPTED)
        self.assertEqual(case["order"].evaluation["selected_method"], "OBSERVED_FORK")
        replays = replay_fork_worlds(case)
        self.assertTrue(all(r["requested_open"] for r in replays))

    def test_late_observation_rejects_jia_yi_but_recommends_yi_bing(self):
        case = build_fork_market_case(late_observation=True)
        order = case["order"]
        self.assertEqual(order.status, STATUS_REJECTED)
        q = case["office"].query_order("SR-早市")
        self.assertEqual(q["deliverable_segments"], ["乙", "丙"])
        self.assertEqual(q["recommended_partial"]["method"], METHOD_FIXED)

        # 接班人按建议另开乙丙单：固定走乙侧在两种落堆世界都能交乙丙。
        reduced = case["office"].submit_release_order(
            "SR-乙丙", segments=["乙", "丙"], snow_line=1,
            method=METHOD_FIXED, permit_ids=["ST-邻队"],
            forecast_segment="乙", capacity=1,
        )
        self.assertEqual(reduced.status, STATUS_ACCEPTED)

        lucky = case["office"].replay_morning(
            "SR-早市", landing_segment="乙", observed=False,
            observation_timing="AFTER_FORK", forecast_segment="乙",
        )
        wrong = case["office"].replay_morning(
            "SR-早市", landing_segment="甲", observed=False,
            observation_timing="AFTER_FORK", forecast_segment="乙",
        )
        self.assertTrue(lucky["requested_open"])
        self.assertFalse(wrong["requested_open"])
        self.assertEqual(wrong["target_segment"], "乙")
        self.assertEqual(wrong["open_blocks"], [["乙", "丙"]])

    def test_signed_ban_makes_late_observation_irrelevant(self):
        case = build_fork_market_case(late_observation=True, sign_ban=True)
        self.assertIsNone(case["ban_error"])
        self.assertEqual(case["order"].status, STATUS_ACCEPTED)
        self.assertEqual(case["order"].evaluation["selected_method"], "BAN_REDIRECT")
        q = case["office"].query_order("SR-早市")
        self.assertIn("丙位-1kg", q["granted_slots"])

    def test_recorded_replay_allocates_confirmed_last_trip_net_once(self):
        case = build_fork_market_case()
        office = case["office"]
        t2 = case["trip_ids"]["T2"]
        result = office.replay_morning(
            "SR-早市", landing_segment="甲", observed=True,
            load_trip_id=t2, record=True,
        )
        self.assertTrue(result["requested_open"])
        # 落堆和装车都已成为本办公室事实；确认净重 1 精确分配给甲一次。
        recon = office.reconstruct()
        self.assertTrue(recon.feasible)
        self.assertEqual(recon.trip_allocations[t2]["segments"]["甲"], [1, 1])
        q = office.query_order("SR-早市")
        details = next(t for t in q["trip_details"] if t["trip_id"] == t2)
        self.assertEqual(details["net"], [1, 1])
        self.assertEqual(details["accounting"]["segments"]["甲"], [1, 1])

    def test_witness_can_be_replaced_on_same_reviewable_order(self):
        case = build_witness_case(replaced=False)
        office = case["office"]
        self.assertEqual(case["order"].status, STATUS_ACCEPTED)
        office.replace_loading_witness("见证-乙", "见证-乙-更正", 5)
        q = office.query_order("SR-见证")
        self.assertEqual(q["status"], STATUS_REJECTED)
        # 旧见证没有从审计事件里消失，但已标记被新签条取代。
        old = next(e for e in office.events if e.get("slip_id") == "见证-乙")
        self.assertEqual(old["superseded_by"], "见证-乙-更正")

    def test_active_ban_rejects_later_dump_or_transfer_into_protected_block(self):
        case = build_fork_market_case(sign_ban=True)
        office = case["office"]
        with self.assertRaises(Exception):
            office.add_dump("ILLEGAL-DUMP", "甲", 1)
        with self.assertRaises(Exception):
            office.add_transfer("ILLEGAL-TRANSFER", "甲", "乙", 1)

    def test_separate_storage_and_release_orders_are_queryable_by_id(self):
        case = build_fork_market_case()
        office = case["office"]
        storage = office.query_order("ST-邻队")
        release = office.query_order("SR-早市")
        self.assertEqual(storage["kind"], "STORAGE")
        self.assertEqual(release["kind"], "RELEASE")
        self.assertEqual(storage["possible_segments"] if "possible_segments" in storage else
                         storage["payload"]["possible_segments"], ["甲", "乙"])
        self.assertTrue(release["referenced_trips"])
        self.assertEqual(release["stalls"], ["摊-甲", "摊-乙"])

    def test_exact_weighbridge_helper_has_expected_net(self):
        wb, ids = exact_unload_warehouse({"T1": 6, "T2": 1})
        for alias, net in (("T1", 6), ("T2", 1)):
            ev = wb.evaluation(ids[alias])
            self.assertEqual((ev.net_lo, ev.net_hi), (net, net))


if __name__ == "__main__":
    unittest.main()
