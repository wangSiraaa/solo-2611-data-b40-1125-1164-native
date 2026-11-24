from __future__ import annotations

import unittest

from extcontract.garden import (
    GardenLedger,
    GardenPlanner,
    GardenWorld,
    build_ledger,
    build_planner,
    run_story,
)
from extcontract.garden.scenario import (
    BEAN,
    BOX1,
    BOX2,
    A,
    B,
    correction_document,
)


class LineageTests(unittest.TestCase):
    def test_corrected_b_history_follows_retained_soil_and_box(self) -> None:
        ledger = build_ledger(corrected_b=True)
        world = ledger.build_world()

        self.assertTrue(world.assess_lot(A, BEAN).eligible)
        self.assertTrue(world.assess_lot(BOX1, BEAN).eligible)

        b = world.assess_lot(B, BEAN)
        box2 = world.assess_lot(BOX2, BEAN)
        self.assertFalse(b.eligible)
        self.assertFalse(box2.eligible)
        self.assertEqual(b.sources, ["SOIL-B"])
        self.assertEqual(box2.sources, ["SOIL-B"])
        self.assertTrue(any(c["season_rank"] == -2 and c["planted_family"] == "豆" for c in b.conflicts))
        self.assertTrue(any(c["season_rank"] == -2 for c in box2.conflicts))

    def test_box_current_position_does_not_replace_source_history(self) -> None:
        ledger = build_ledger(corrected_b=True)
        world = ledger.build_world()
        planner = GardenPlanner(world)
        moved = planner.move_box(BOX1, "PB", shift=2)
        self.assertEqual(moved["status"], "MOVED")
        # BOX1 is physically beside B, whose record is bad, but its source is A.
        self.assertEqual(world.lot_position(BOX1), "PB")
        self.assertTrue(world.signable_adjacency(BOX1, B))
        eligibility = world.assess_lot(BOX1, BEAN)
        self.assertTrue(eligibility.eligible)
        self.assertEqual(eligibility.sources, ["SOIL-A"])

    def test_mixed_soil_inherits_every_source_and_missing_season_blocks(self) -> None:
        world = GardenWorld()
        world.add_source("S1").add_source("S2")
        world.add_planting("S1", -1, "禾").add_planting("S1", -2, "禾")
        world.add_planting("S2", -1, "禾")  # missing -2
        world.add_lot("MIX", "box")
        world.add_coverage("MIX", "S1", "1/2")
        world.add_coverage("MIX", "S2", "1/2")
        result = world.assess_lot("MIX", "豆")
        self.assertFalse(result.eligible)
        self.assertEqual(result.sources, ["S1", "S2"])
        self.assertTrue(any(m["source_id"] == "S2" and m["season_rank"] == -2 for m in result.missing_seasons))

        # If S2 has beans in either season, the mixed lot is also blocked.
        world.add_planting("S2", -2, "豆")
        result = world.assess_lot("MIX", "豆")
        self.assertFalse(result.eligible)
        self.assertTrue(any(c["source_id"] == "S2" for c in result.conflicts))

    def test_soil_destinations_must_account_for_every_batch(self) -> None:
        world = GardenWorld()
        world.add_source("S", amount=1)
        world.add_lot("C", "cell")
        world.add_movement(source="S", amount="1/2", destination="C", destination_kind="retained")
        world.finalize()
        self.assertTrue(any("destinations total 1/2 but declared amount is 1" in e for e in world.validation_errors))

    def test_place_rename_keeps_stable_source_identity(self) -> None:
        ledger = build_ledger(renamed=True)
        snapshot = ledger.publish("map")
        row = next(r for r in snapshot["rows"] if r["row_id"] == "LOT-A")
        self.assertEqual(row["revision"], 2)
        self.assertIn("改名后", row["values"]["display_name"])
        world = ledger.build_world()
        self.assertEqual(world.lots["A"].display_name, "春畦东一号（改名后）")
        self.assertTrue(world.assess_lot("A", "豆").eligible)

    def test_unapproved_box_stand_is_rejected(self) -> None:
        planner = build_planner()
        planner.world.approved_positions.discard("SECRET")
        result = planner.move_box("BOX-1", "SECRET", shift=2)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("not an approved box stand", result["reasons"][0])

    def test_publish_snapshot_carries_revision_and_provenance(self) -> None:
        ledger = GardenLedger()
        docs = build_ledger().publish_all()  # build accepted canonical snapshots elsewhere
        # Re-ingest canonical files into a fresh ledger to inspect receipts.
        from extcontract.garden.scenario import initial_documents

        fresh = GardenLedger()
        results = fresh.ingest_files(initial_documents())
        self.assertTrue(all(r.accepted for r in results.values()))
        corrected = fresh.ingest("planting", correction_document())
        self.assertTrue(corrected.accepted)
        snapshot = fresh.publish("planting")
        row = next(r for r in snapshot["rows"] if r["row_id"] == "P-B-before_last")
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["values"]["crop_family"], "豆")
        self.assertIn("crop_family", row["sources"])
        self.assertEqual(docs["planting"]["reader_version"], "garden-planting-r1")


class AllocationTests(unittest.TestCase):
    def test_story_outcomes_and_exclusive_orders(self) -> None:
        report = run_story()
        self.assertEqual(report["baseline"]["signed_order"]["boundary"], [A, B])
        self.assertEqual(report["corrected_B"]["two_place_plan"]["status"], "NO_FEASIBLE_ALLOCATION")
        self.assertEqual(
            report["restored_and_narrowed"]["two_place_plan"]["status"],
            "NO_FEASIBLE_ALLOCATION",
        )
        self.assertEqual(report["next_shift"]["move_box1"]["status"], "MOVED")
        self.assertEqual(report["next_shift"]["h2_sign"]["status"], "SIGNED")
        self.assertEqual(report["next_shift"]["h2_sign"]["order"]["box_ids"], [BOX1, BOX2])
        self.assertEqual(report["next_shift"]["h3_sign"]["status"], "REJECTED")
        self.assertIn(A, report["next_shift"]["boundary_occupancy"])
        self.assertIn(BOX1, report["next_shift"]["box_occupancy"])

    def test_signed_land_cannot_be_reassigned(self) -> None:
        planner = build_planner()
        first = planner.sign_application("H1", BEAN, places=2)
        self.assertEqual(first["status"], "SIGNED")
        second = planner.sign_application("H2", BEAN, mode="land", places=2)
        self.assertEqual(second["status"], "REJECTED")
        self.assertIn("only 0 eligible unbooked", " ".join(second["reasons"]))

    def test_box_is_exclusive_after_signing(self) -> None:
        planner = build_planner()
        planner.move_box(BOX1, "PB", shift=1)
        first = planner.sign_application("H1", BEAN, mode="box", places=2, shift=1)
        self.assertEqual(first["status"], "SIGNED")
        second = planner.sign_application("H2", BEAN, mode="box", places=2)
        self.assertEqual(second["status"], "REJECTED")
        # Even an empty old stand cannot reopen the real named box.
        move = planner.move_box(BOX1, "P1", shift=3)
        self.assertEqual(move["status"], "REJECTED")

    def test_three_household_limit(self) -> None:
        planner = GardenPlanner(GardenWorld(), max_households=3)
        planner.register_household("H1")
        planner.register_household("H2")
        planner.register_household("H3")
        with self.assertRaises(OverflowError):
            planner.register_household("H4")

    def test_narrow_aisle_capacity_blocks_pair(self) -> None:
        ledger = build_ledger(restored=True, narrow_aisle=True)
        planner = GardenPlanner(ledger.build_world())
        result = planner.plan("H", BEAN, mode="land", places=2)
        self.assertFalse(result["accepted"])
        self.assertTrue(
            any("can hand 1 place(s)" in x for d in result["diagnostics"] for x in d["access"]["errors"])
        )


if __name__ == "__main__":
    unittest.main()
