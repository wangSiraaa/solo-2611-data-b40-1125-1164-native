"""触读制牌/布牌工具的单元测试：领域规则、两份归一、混淆与规划、版本隔离。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from confcat.catalog import Catalog
from confcat.errors import TaskRejected
from confcat.tactile.domain import (
    LEFT,
    RIGHT,
    AnchorEndPolicy,
    BaseSpec,
    NoMarkerPolicy,
    PlateSpec,
    flip_edges,
    flip_index,
    read_sign,
    signature_key,
)
from confcat.tactile.exhibition import load_exhibition
from confcat.tactile.fabricator import FabricationOrder, fabricate
from confcat.tactile.placement import (
    BaseInventory,
    Booth,
    Entrance,
    evaluate,
    plan_exhibition,
)
from confcat.tactile.plates import load_base_bundle, load_plate_bundle

DOT_YAML = """
tactile:
  source_uri: file:///dot.csv
  column_mapping:
    plate_id: plate_row
    n: n_cells
    dot_cells: convex_cells
    occluded_cells: masked_cells
    edges: touchable_sides
  partition_by:
    - plate_id
"""
BASE_YAML = """
tactile:
  source_uri: file:///base.csv
  column_mapping:
    base_id: seat_row
    kind: seat_type
    locked_side: lock_edge
"""
DOT_CSV = (
    "plate_row,n_cells,convex_cells,masked_cells,touchable_sides\n"
    "P-EXIT,3,0,,L|R\n"
    "P-STAIR,3,2,,L|R\n"
    "P-SAME,3,2,,L|R\n"
    "P-OCC,3,0,0,L|R\n"
    "P-4,4,0,,L|R\n"
)
BASE_CSV = (
    "seat_row,seat_type,lock_edge\n"
    "L-LOCK,locked,L\n"
    "R-LOCK,locked,R\n"
    "SWV,swivel,\n"
)


class DomainTest(unittest.TestCase):
    def test_flip_index_and_edges(self):
        self.assertEqual([flip_index(i, 3) for i in range(3)], [2, 1, 0])
        self.assertEqual(flip_edges([LEFT, RIGHT]), frozenset((LEFT, RIGHT)))
        self.assertEqual(flip_edges([LEFT]), frozenset({RIGHT}))

    def test_plate_validation(self):
        with self.assertRaises(ValueError):
            PlateSpec("x", 5, frozenset({0}))  # 超过四格
        with self.assertRaises(ValueError):
            PlateSpec("x", 3, frozenset({3}))  # 越界
        with self.assertRaises(ValueError):
            PlateSpec("x", 3, frozenset({0}), frozenset({1}))  # 遮挡非凸点
        with self.assertRaises(ValueError):
            PlateSpec("x", 3, frozenset({0}), edges=frozenset({"X"}))

    def test_felt_dots_free_cells_and_render(self):
        p = PlateSpec("p", 3, frozenset({0, 2}), frozenset({2}))
        self.assertEqual(p.felt_dots, frozenset({0}))
        self.assertEqual(p.free_cells, frozenset({1}))
        self.assertEqual(p.render(), "[● · ○]")

    def test_base_orientations(self):
        self.assertEqual(BaseSpec("a", "locked", LEFT).orientations, (0,))
        self.assertEqual(BaseSpec("b", "swivel").orientations, (0, 1))
        with self.assertRaises(ValueError):
            BaseSpec("c", "locked")  # locked 必须记录锁边
        with self.assertRaises(ValueError):
            BaseSpec("d", "weird")

    def test_fixed_anchor_never_falls_back(self):
        pol = AnchorEndPolicy(RIGHT)
        # 右端空闲 -> 锚点在固定右端
        self.assertEqual(pol.marker_cell(PlateSpec("a", 3, frozenset({0}))), 2)
        # 右端被凸点占用 -> 打不上，绝不回退左端
        self.assertIsNone(pol.marker_cell(PlateSpec("b", 3, frozenset({2}))))
        # 被遮挡的凸点仍占格，锚点同样打不上
        self.assertIsNone(
            pol.marker_cell(PlateSpec("c", 3, frozenset({2}), frozenset({2})))
        )

    def test_swivel_readings_are_mirror(self):
        p = PlateSpec("a", 3, frozenset({0}))
        sign = read_sign(p, BaseSpec("s", "swivel"), NoMarkerPolicy())
        self.assertEqual(len(sign.readings), 2)
        self.assertEqual(sign.readings[0].dots, frozenset({0}))
        self.assertEqual(sign.readings[1].dots, frozenset({2}))


class ConfusionTest(unittest.TestCase):
    def setUp(self):
        self.exit_p = PlateSpec("exit", 3, frozenset({0}))
        self.stair_p = PlateSpec("stair", 3, frozenset({2}))
        self.lock_l = BaseSpec("ll", "locked", LEFT)
        self.lock_r = BaseSpec("lr", "locked", RIGHT)
        self.swv = BaseSpec("sw", "swivel")

    def _booth(self, pa, pb, ba, bb, dest_b="楼梯", policy=NoMarkerPolicy()):
        return Booth("B", [
            Entrance("ea", read_sign(pa, ba, policy), "出口"),
            Entrance("eb", read_sign(pb, bb, policy), dest_b),
        ])

    def test_locked_upright_is_distinguishable(self):
        self.assertEqual(evaluate([self._booth(self.exit_p, self.stair_p, self.lock_l, self.lock_r)]), [])

    def test_reengraved_collision_confuses(self):
        bad_exit = PlateSpec("exit-r", 3, frozenset({2}))
        conf = evaluate([self._booth(bad_exit, self.stair_p, self.lock_l, self.lock_r)])
        self.assertEqual(len(conf), 1)
        self.assertEqual({conf[0].first.destination, conf[0].second.destination}, {"出口", "楼梯"})
        self.assertEqual(conf[0].key[0], (2,))  # 同一右端凸点签名

    def test_swivel_can_make_exit_read_as_stair(self):
        conf = evaluate([self._booth(self.exit_p, self.stair_p, self.swv, self.swv)])
        self.assertEqual(len(conf), 2)
        # 至少一次是“出口正向”撞上“楼梯转半周”，反之亦然
        orients = {(c.first.orientation, c.second.orientation) for c in conf}
        self.assertIn((0, 1), orients)
        self.assertIn((1, 0), orients)

    def test_same_shape_same_destination_not_confusion(self):
        booth = Booth("B", [
            Entrance("e1", read_sign(self.exit_p, self.lock_l, NoMarkerPolicy()), "出口"),
            Entrance("e2", read_sign(self.exit_p, self.lock_r, NoMarkerPolicy()), "出口"),
        ])
        self.assertEqual(evaluate([booth]), [])

    def test_approach_side_mirrors_signature(self):
        # 左侧走近的出口牌（左点）与右侧走近的楼梯牌（其右点被镜像到触觉左端）
        # 在访客触觉参照系里是同一签名 -> 对侧入口也会引错
        b = Booth(
            "B",
            [
                Entrance("left", read_sign(self.exit_p, self.lock_l, NoMarkerPolicy()), "出口", LEFT),
                Entrance("right", read_sign(self.stair_p, self.lock_r, NoMarkerPolicy()), "楼梯", RIGHT),
            ],
        )
        conf = evaluate([b])
        self.assertEqual(len(conf), 1)
        self.assertEqual(conf[0].key[0], (0,))  # 访客都在触觉 0 号格摸到点

    def test_anchor_rescues_swivel_when_end_free(self):
        pol = AnchorEndPolicy(RIGHT)
        # 四格牌两端：A 右端空闲可打锚点，B 右端占用打不上 -> 可分
        a = PlateSpec("a4", 4, frozenset({0}))
        bplate = PlateSpec("b4", 4, frozenset({3}))
        conf = evaluate([self._booth(a, bplate, self.swv, self.swv, policy=pol)])
        self.assertEqual(conf, [])


class NormalizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.dot_csv = self.dir / "dot.csv"
        self.base_csv = self.dir / "base.csv"
        self.dot_csv.write_text(DOT_CSV, encoding="utf-8")
        self.base_csv.write_text(BASE_CSV, encoding="utf-8")
        self.cat = Catalog(self.dir / "cat.db")
        self.cat.publish("dot-product", DOT_YAML)
        self.cat.publish("base-product", BASE_YAML)

    def tearDown(self):
        self.cat.close()
        self.tmp.cleanup()

    def test_bundles_carry_lineage_and_version(self):
        plates = load_plate_bundle(self.cat, "dot-product", "doc-0", self.dot_csv)
        bases = load_base_bundle(self.cat, "base-product", "doc-0", self.base_csv)
        self.assertEqual({p.plate_id for p in plates.plates},
                         {"P-EXIT", "P-STAIR", "P-SAME", "P-OCC", "P-4"})
        src = plates.sources["P-EXIT"]
        self.assertEqual(src["version"], 1)
        self.assertEqual(src["lineage"]["dot_cells"]["source_column"], "convex_cells")
        self.assertTrue(src["lineage"]["dot_cells"]["defined_line"])
        occ = plates.get("P-OCC")
        self.assertEqual(occ.dots, frozenset({0}))
        self.assertEqual(occ.occluded, frozenset({0}))
        self.assertEqual(occ.felt_dots, frozenset())
        self.assertEqual(bases.get("SWV").orientations, (0, 1))

    def test_fabrication_joins_both_task_sources(self):
        plates = load_plate_bundle(self.cat, "dot-product", "doc-0", self.dot_csv)
        bases = load_base_bundle(self.cat, "base-product", "doc-0", self.base_csv)
        signs = fabricate(
            [FabricationOrder("S1", "P-EXIT", "SWV")], plates, bases, AnchorEndPolicy()
        )
        self.assertIn("dot_task", signs["S1"].source)
        self.assertIn("base_task", signs["S1"].source)
        self.assertEqual(signs["S1"].source["marker_policy"], "anchor-end(R)")

    def test_missing_source_column_rejects_whole_task(self):
        bad_yaml = DOT_YAML.replace("plate_id: plate_row", "plate_id: ghost")
        self.cat.publish("bad", bad_yaml)
        with self.assertRaises(TaskRejected):
            load_plate_bundle(self.cat, "bad", "doc-0", self.dot_csv)

    def test_occluded_must_be_subset_of_dots(self):
        bad = DOT_CSV.replace("P-OCC,3,0,0,", "P-OCC,3,0,2,")  # 遮挡格 2 无凸点
        self.dot_csv.write_text(bad, encoding="utf-8")
        with self.assertRaises(TaskRejected):
            load_plate_bundle(self.cat, "dot-product", "doc-0", self.dot_csv)

    def test_version_snapshot_isolation(self):
        v2 = DOT_YAML.replace("source_uri: file:///dot.csv",
                              "source_uri: file:///dot.csv\n  _rev: 2")
        # 用一份中点凸点的新行，并让 v2 配置指向它（通过新原列名）
        v2_csv = self.dir / "dot2.csv"
        v2_csv.write_text(DOT_CSV.replace("P-EXIT,3,0,", "P-EXIT,3,1,"), encoding="utf-8")
        self.cat.publish("dot-product", v2)  # v2 文本不同但映射不变 -> v1/v2 结果一致
        v1 = load_plate_bundle(self.cat, "dot-product", "doc-0", self.dot_csv, version=1)
        v2_loaded = load_plate_bundle(self.cat, "dot-product", "doc-0", self.dot_csv, version=2)
        self.assertEqual(v1.get("P-EXIT").dots, v2_loaded.get("P-EXIT").dots)
        self.assertEqual(self.cat.list_versions("dot-product"), [1, 2])


class PlanningTest(unittest.TestCase):
    def setUp(self):
        self.exit_p = PlateSpec("exit", 3, frozenset({0}))
        self.stair_p = PlateSpec("stair", 3, frozenset({2}))
        self.lock = BaseSpec("LK", "locked", LEFT)
        self.swv = BaseSpec("SW", "swivel")

    def _entrance(self, eid, plate, base, dest, approach=LEFT):
        return Entrance(eid, read_sign(plate, base, NoMarkerPolicy()), dest, approach)

    def test_inventory_shortage_blocks_booth(self):
        inv = BaseInventory.from_specs([self.lock], {"LK": 1})
        booth = Booth("B", [
            self._entrance("e1", self.exit_p, self.lock, "出口"),
            self._entrance("e2", self.stair_p, self.lock, "楼梯"),
        ])
        plan = plan_exhibition([booth], inv, NoMarkerPolicy(), False)
        self.assertFalse(plan.booth_plans[0].openable)
        self.assertTrue(any("耗尽" in r for r in plan.booth_plans[0].blocked_reasons))

    def test_collision_without_reengrave_permission_closes(self):
        same = PlateSpec("dup", 3, frozenset({2}))  # 与楼梯同形
        inv = BaseInventory.from_specs([self.lock], {"LK": 2})
        booth = Booth("B", [
            self._entrance("e1", same, self.lock, "出口"),
            self._entrance("e2", self.stair_p, self.lock, "楼梯"),
        ])
        plan = plan_exhibition([booth], inv, NoMarkerPolicy(), reengrave_allowed=False)
        self.assertEqual(plan.closed_booths, ["B"])
        self.assertTrue(plan.booth_plans[0].proposals)

    def test_reengrave_allowed_flags_recheck_instead_of_silent_open(self):
        same = PlateSpec("dup", 3, frozenset({2}))
        inv = BaseInventory.from_specs([self.lock], {"LK": 2})
        booth = Booth("B", [
            self._entrance("e1", same, self.lock, "出口"),
            self._entrance("e2", self.stair_p, self.lock, "楼梯"),
        ])
        plan = plan_exhibition([booth], inv, NoMarkerPolicy(), reengrave_allowed=True)
        # 仍标记为不可开放，直到按建议重制并复验
        self.assertFalse(plan.booth_plans[0].openable)
        self.assertTrue(any("复验" in r for r in plan.booth_plans[0].blocked_reasons))

    def test_planner_does_not_swap_swivel_for_locked(self):
        # 指定普通座时，规划不得擅自换锁死座来消除翻转混淆
        inv = BaseInventory.from_specs([self.lock, self.swv], {"LK": 2, "SW": 2})
        booth = Booth("B", [
            self._entrance("e1", self.exit_p, self.swv, "出口"),
            self._entrance("e2", self.stair_p, self.swv, "楼梯"),
        ])
        plan = plan_exhibition([booth], inv, NoMarkerPolicy(), False)
        self.assertEqual(plan.closed_booths, ["B"])
        self.assertEqual(
            [ep.assigned_base for ep in plan.booth_plans[0].entrances], ["SW", "SW"]
        )


class ExhibitionLoadingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "dot.csv").write_text(DOT_CSV, encoding="utf-8")
        (self.dir / "base.csv").write_text(BASE_CSV, encoding="utf-8")
        self.cat = Catalog(self.dir / "cat.db")
        self.cat.publish("dot-product", DOT_YAML)
        self.cat.publish("base-product", BASE_YAML)
        self.plates = load_plate_bundle(self.cat, "dot-product", "doc-0", self.dir / "dot.csv")
        self.bases = load_base_bundle(self.cat, "base-product", "doc-0", self.dir / "base.csv")

    def tearDown(self):
        self.cat.close()
        self.tmp.cleanup()

    def _yaml(self, body: str) -> Path:
        p = self.dir / "ex.yaml"
        p.write_text("exhibition:\n" + body, encoding="utf-8")
        return p

    def test_cell_limit_rejects_oversized_plate(self):
        yaml_text = self._yaml(
            '  max_cells: 3\n'
            '  inventory:\n'
            '    - SWV\n'
            '  inventory_counts:\n'
            '    SWV: 2\n'
            '  booths:\n'
            '    - booth_id: B\n'
            '      entrances:\n'
            '        - entrance_id: e1\n'
            '          plate_id: P-4\n'
            '          base_id: SWV\n'
            '          destination: x\n'
            '          approach: "L"\n'
            '        - entrance_id: e2\n'
            '          plate_id: P-EXIT\n'
            '          base_id: SWV\n'
            '          destination: y\n'
            '          approach: "L"\n'
        )
        with self.assertRaises(TaskRejected):
            load_exhibition(yaml_text, self.plates, self.bases)

    def test_unknown_base_id_rejected(self):
        yaml_text = self._yaml(
            '  inventory:\n'
            '    - GHOST\n'
            '  booths:\n'
        )
        with self.assertRaises(KeyError):
            load_exhibition(yaml_text, self.plates, self.bases)


if __name__ == "__main__":
    unittest.main()
