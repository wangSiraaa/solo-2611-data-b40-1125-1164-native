"""布景工坊接单服务集成测试：归一任务实际行、两种交货、占用与快照复看。

全程走真实 SQLite（Catalog + TaskRepository + WorkshopRepository），覆盖：
* 封签/整卷两种工艺在双销位出车单上的担保差异；
* 跨订单卷坐标占用：已许边头不能再裁给后一班；
* 墙面任务修订后旧裁单仍按当时实际行复看；
* 输入不合法整单不落库，卡点指到具体接缝与卷段。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from confcat.catalog import Catalog
from confcat.errors import OrderRejected, WorkshopConflict
from confcat.ingest import TaskRepository
from confcat.workshop import WorkshopRepository
from confcat.workshop_model import MODE_SEALED, MODE_WHOLE_ROLL

ROLL_CONFIG = """
ingest:
  source_uri: file:///data/rolls.csv
  column_mapping:
    roll_id: rid
    pattern_period: cycle
    direction: side
    marker_phase: mark
    start_coord: c0
    end_coord: c1
"""

WALL_CONFIG = """
ingest:
  source_uri: file:///data/walls.csv
  column_mapping:
    scenario: scene
    wall_id: wid
    height: h
    first_beat: beat0
    pre_mounted: fixed
    from_wall: src
    to_wall: dst
    offset_beats: shift
"""

ROLL_CSV = "rid,cycle,side,mark,c0,c1\nR,3,正,1,0,3\n"

# 甲已裁好首拍 0；双销位接缝要求恰好互换
WALL_CSV = (
    "scene,wid,h,beat0,fixed,src,dst,shift\n"
    "pin-A,A,3,0,是,,\n"
    "pin-A,B,3,,,,\n"
    "pin-A,A,3,0,是,A,B,1\n"
    "pin-A,B,3,,,B,A,2\n"
    "pin-B,A,3,0,是,,\n"
    "pin-B,B,3,,,,\n"
    "pin-B,A,3,0,是,A,B,2\n"
    "pin-B,B,3,,,B,A,1\n"
)

# 基础两墙单现场算例（三拍一轮，两卷各三格）
ROLL_CSV_TWO = (
    "rid,cycle,side,mark,c0,c1\n"
    "R1,3,正,0,0,2\n"
    "R2,3,正,1,0,2\n"
)
WALL_CSV_TWO = (
    "scene,wid,h,beat0,fixed,src,dst,shift\n"
    "default,A,3,0,,,\n"
    "default,B,3,,,,\n"
    "default,A,3,0,,A,B,1\n"
    "default,B,3,,,B,A,2\n"
)


class WorkshopServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cat = Catalog(d / "cat.db")
        self.cat.publish("rolls", ROLL_CONFIG)
        self.cat.publish("walls", WALL_CONFIG)
        self.cat.publish("rolls-two", ROLL_CONFIG)
        self.cat.publish("walls-two", WALL_CONFIG)
        self.roll_csv = d / "rolls.csv"
        self.wall_csv = d / "walls.csv"
        self.roll_csv.write_text(ROLL_CSV, encoding="utf-8")
        self.wall_csv.write_text(WALL_CSV, encoding="utf-8")
        self.roll_two_csv = d / "rolls_two.csv"
        self.wall_two_csv = d / "walls_two.csv"
        self.roll_two_csv.write_text(ROLL_CSV_TWO, encoding="utf-8")
        self.wall_two_csv.write_text(WALL_CSV_TWO, encoding="utf-8")

        self.tasks = TaskRepository(d / "tasks.db", self.cat)
        self.tasks.create_task("roll-task", "rolls", "doc-0", self.roll_csv)
        self.tasks.create_task("wall-task", "walls", "doc-0", self.wall_csv)
        self.tasks.create_task("roll-two", "rolls-two", "doc-0", self.roll_two_csv)
        self.tasks.create_task("wall-two", "walls-two", "doc-0", self.wall_two_csv)
        self.shop = WorkshopRepository(d / "shop.db", self.tasks)

    def tearDown(self):
        self.shop.close()
        self.tasks.close()
        self.cat.close()
        self.tmp.cleanup()

    def test_whole_roll_guarantees_both_pins_with_distinct_segments(self):
        receipt = self.shop.place_order(
            "o1", "roll-task", "wall-task", ["A", "B"], MODE_WHOLE_ROLL
        )
        self.assertTrue(receipt.guaranteed)
        plans = {p.scenario: p for p in receipt.scenarios}
        self.assertEqual(
            (plans["pin-A"].assignments["B"].start, plans["pin-A"].assignments["B"].end),
            (0, 2),
        )
        self.assertEqual(
            (plans["pin-B"].assignments["B"].start, plans["pin-B"].assignments["B"].end),
            (1, 3),
        )
        # 两个现场各自的实卷余量：甲销余尾端 3，乙销余边头 0
        ra = self.shop.realize_scenario("o1", "pin-A")
        rb = self.shop.realize_scenario("o1", "pin-B")
        self.assertEqual(ra["leftover_intervals"], {"R": [[3, 3]]})
        self.assertEqual(rb["leftover_intervals"], {"R": [[0, 0]]})

    def test_sealed_order_cannot_guarantee_two_pins_and_points_to_seam(self):
        receipt = self.shop.place_order(
            "o2", "roll-task", "wall-task", ["A", "B"], MODE_SEALED
        )
        self.assertFalse(receipt.guaranteed)
        failed = [p for p in receipt.scenarios if not p.guaranteed]
        self.assertTrue(failed)
        blockage = failed[0].blockages[0]
        self.assertEqual(blockage.kind, "seam")
        self.assertIn(blockage.seam, (("A", "B"), ("B", "A")))
        self.assertEqual(blockage.roll_id, "R")
        self.assertIsNotNone(blockage.segment)
        # 不能承诺的单不占用任何卷料
        self.assertEqual(self.shop.current_occupancy(), {})

    def test_second_shift_reaccepted_after_first_shift_reserved_edge(self):
        # 第一班整卷接单担保并占用（两现场都落占用，覆盖两种下刀可能）
        first = self.shop.place_order(
            "shift-1", "roll-task", "wall-task", ["A", "B"], MODE_WHOLE_ROLL
        )
        self.assertTrue(first.guaranteed)
        # 第二班用同一张出车单再受理：无论哪个现场都没有完整三格可领
        second = self.shop.place_order(
            "shift-2", "roll-task", "wall-task", ["A", "B"], MODE_WHOLE_ROLL
        )
        self.assertFalse(second.guaranteed)
        for plan in second.scenarios:
            self.assertTrue(plan.blockages)
            self.assertIn(plan.blockages[0].kind, ("no-segment", "overlap", "seam"))

    def test_explicit_first_cell_reservation_blocks_only_pin_a(self):
        # 另一班已签本卷首格：构造只占坐标 0 的先行订单（单墙现场）
        wall_csv = self.tmp_path("reserve_walls.csv")
        wall_csv.write_text(
            "scene,wid,h,beat0,fixed,src,dst,shift\n"
            "default,A,1,,,,\n"
            "default,A,1,,,A,A,2\n",
            encoding="utf-8",
        )
        self.cat.publish(
            "reserve-walls",
            WALL_CONFIG.replace("file:///data/walls.csv", "file:///data/reserve.csv"),
        )
        self.tasks.create_task("reserve-task", "reserve-walls", "doc-0", wall_csv)
        reserve = self.shop.place_order(
            "reserve", "roll-task", "reserve-task", ["A"], MODE_WHOLE_ROLL
        )
        self.assertTrue(reserve.guaranteed)
        occupied = self.shop.current_occupancy()
        self.assertEqual(occupied["R"], [(0, 0)])  # 已许的边头

        receipt = self.shop.place_order(
            "o3", "roll-task", "wall-task", ["A", "B"], MODE_WHOLE_ROLL
        )
        plans = {p.scenario: p for p in receipt.scenarios}
        self.assertFalse(plans["pin-A"].guaranteed)   # 甲销要用边头，边头已许人
        self.assertTrue(plans["pin-B"].guaranteed)    # 乙销从 [1,3] 下刀
        self.assertEqual(
            (plans["pin-B"].assignments["B"].start, plans["pin-B"].assignments["B"].end),
            (1, 3),
        )
        self.assertFalse(receipt.guaranteed)  # 有现场不能承诺 -> 整单不担保

    def test_two_roll_basic_example_end_to_end(self):
        receipt = self.shop.place_order(
            "o4", "roll-two", "wall-two", ["A", "B"], MODE_SEALED
        )
        self.assertTrue(receipt.guaranteed)
        plan = receipt.scenarios[0]
        self.assertEqual(plan.assignments["A"].roll_id, "R1")
        self.assertEqual(plan.assignments["B"].roll_id, "R2")
        # 同一段纸没有被两面墙重复领走
        keys = [(s.roll_id, s.start, s.end) for s in plan.assignments.values()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_revision_of_wall_task_keeps_old_receipt_view(self):
        self.shop.place_order("o5", "roll-two", "wall-two", ["A", "B"], MODE_SEALED)
        old_view = self.shop.get_order("o5")
        # 墙面任务后来修订（乙墙加高到 4 格），重跑归一产出新实际行与新任务编号
        revised = (
            "scene,wid,h,beat0,fixed,src,dst,shift\n"
            "default,A,3,0,,,\n"
            "default,B,4,,,,\n"
            "default,A,3,0,,A,B,1\n"
            "default,B,4,,,B,A,2\n"
        )
        revised_csv = self.tmp_path("walls_two_v2.csv")
        revised_csv.write_text(revised, encoding="utf-8")
        self.tasks.create_task("wall-two-v2", "walls-two", "doc-0", revised_csv)
        # 新单按新行受理：四格墙三格卷，不能承诺
        new_receipt = self.shop.place_order(
            "o6", "roll-two", "wall-two-v2", ["A", "B"], MODE_SEALED
        )
        self.assertFalse(new_receipt.guaranteed)
        # 旧裁单仍按当时行复看：乙墙高度快照仍是 3（墙定义行与接缝行各一条）
        self.assertEqual(
            sorted(int(r["height"]) for r in old_view["wall_snapshot"] if r["wall_id"] == "B"),
            [3, 3],
        )
        snapshot = self.shop.get_task_snapshot("wall-two")
        self.assertTrue(snapshot["lineage"])  # 映射来源随快照保留
        self.assertEqual(snapshot["rows"][0]["wall_id"], "A")

    def test_reopened_repository_persists_everything(self):
        self.shop.place_order("o7", "roll-two", "wall-two", ["A", "B"], MODE_WHOLE_ROLL)
        self.shop.close()
        shop2 = WorkshopRepository(Path(self.tmp.name) / "shop.db", self.tasks)
        order = shop2.get_order("o7")
        self.assertTrue(order["guaranteed"])
        self.assertEqual(order["wall_order"], ["A", "B"])
        self.assertEqual(len(order["assignments"]), 2)
        self.assertTrue(all(a["roll_id"] for a in order["assignments"]))
        shop2.close()

    def test_invalid_inputs_reject_without_persisting(self):
        with self.assertRaises(OrderRejected):
            self.shop.place_order("bad", "ghost-task", "wall-task", ["A"], MODE_SEALED)
        with self.assertRaises(OrderRejected):
            self.shop.place_order(
                "bad2", "roll-task", "wall-task", ["A", "X"], MODE_WHOLE_ROLL
            )
        with self.assertRaises(OrderRejected):
            self.shop.place_order(
                "bad3", "roll-task", "wall-task", ["A", "B"], "truck"
            )
        self.assertEqual(self.shop.list_orders(), [])

    def test_duplicate_order_id_conflicts(self):
        self.shop.place_order("dup", "roll-two", "wall-two", ["A", "B"], MODE_SEALED)
        with self.assertRaises(WorkshopConflict):
            self.shop.place_order("dup", "roll-two", "wall-two", ["A", "B"], MODE_SEALED)

    def test_sealed_fixed_assignment_rejects_first_beat_mismatch(self):
        # 第二现场对 B 要求不同首拍：封签固定段无法翻头，整单不担保且指到墙 B
        wall_csv = self.tmp_path("beat_walls.csv")
        wall_csv.write_text(
            "scene,wid,h,beat0,fixed,src,dst,shift\n"
            "s1,A,3,0,,,\n"
            "s1,B,3,1,,\n"
            "s1,A,3,0,,A,B,1\n"
            "s1,B,3,1,,B,A,2\n"
            "s2,A,3,0,,,\n"
            "s2,B,3,2,,\n"
            "s2,A,3,0,,A,B,2\n"
            "s2,B,3,2,,B,A,1\n",
            encoding="utf-8",
        )
        self.cat.publish(
            "beat-walls",
            WALL_CONFIG.replace("file:///data/walls.csv", "file:///data/beat.csv"),
        )
        self.tasks.create_task("beat-task", "beat-walls", "doc-0", wall_csv)
        receipt = self.shop.place_order(
            "beat", "roll-two", "beat-task", ["A", "B"], MODE_SEALED
        )
        self.assertFalse(receipt.guaranteed)
        failed = [p for p in receipt.scenarios if not p.guaranteed][0]
        # 封签固定段在第二现场无法同时满足首拍与环缝，卡点落到具体接缝/卷段
        self.assertIn(failed.blockages[0].kind, ("seam", "first-beat"))
        self.assertTrue(failed.blockages[0].seam)
        self.assertEqual(failed.blockages[0].roll_id, "R1")

    def test_task_snapshot_conflict_on_changed_rows(self):
        self.shop.place_order("o8", "roll-two", "wall-two", ["A", "B"], MODE_SEALED)
        # 同一任务编号被重跑且实际行变化（直接改归一结果表模拟重跑）：
        # 工坊拒绝混用新旧行，提示另办新单，旧裁单快照保持原样
        rows = self.tasks.conn.execute(
            "SELECT row_no, values_json FROM task_row WHERE task_id = 'wall-two' ORDER BY row_no"
        ).fetchall()
        for r in rows:
            values = json.loads(r["values_json"])
            # 只改 B 的墙定义行（非接缝行）
            if values.get("wall_id") == "B" and not values.get("from_wall"):
                values["height"] = "2"
                self.tasks.conn.execute(
                    "UPDATE task_row SET values_json = ? WHERE task_id = 'wall-two' AND row_no = ?",
                    (json.dumps(values, ensure_ascii=False), r["row_no"]),
                )
        self.tasks.conn.commit()
        with self.assertRaises(WorkshopConflict):
            self.shop.place_order(
                "o9", "roll-two", "wall-two", ["A", "B"], MODE_SEALED
            )
        # 旧裁单仍可按当时快照复看
        self.assertTrue(self.shop.get_order("o8")["guaranteed"])

    def tmp_path(self, name: str) -> Path:
        return Path(self.tmp.name) / name


if __name__ == "__main__":
    unittest.main()
