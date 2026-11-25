"""口袋果园层测试：接入/定位、冷天归并、承诺方向与床距、布局稳健、复跑更正。

全部用真实 :class:`confcat` 目录与消费仓（临时 SQLite + 临时 CSV），
不 mock 掉接入层，保证“从实际归一任务读取、用当时版本列来源定位”的路径被覆盖。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from confcat.catalog import Catalog
from confcat.ingest import TaskRepository

from orchard.commitments import diagnose_pair, evaluate_commitments
from orchard.errors import ColdRevisionConflict
from orchard.exhibit import (
    build_exhibit,
    deliver,
    render_report,
    replay_revisions,
    run_pipeline,
)
from orchard.ingest import load_cold_claims, load_facts, locate_record
from orchard.layout import (
    best_fixed_layout,
    plan_wheeled,
    possible_scenarios,
    segments,
)
from orchard.observation import build_timeline, reconcile_cold


# --------------------------------------------------------------------------- #
# 事实夹具
# --------------------------------------------------------------------------- #
def bloom_csv(rows) -> str:
    """rows: list of (tree, region, day, bloom_bool)."""
    lines = ["tree,region,day,bloom"]
    for tree, region, day, flag in rows:
        lines.append(f"{tree},{region},{day},{'yes' if flag else 'no'}")
    return "\n".join(lines) + "\n"


def flag_csv(marker: str, rows) -> str:
    """rows: list of (tree, day, bool)."""
    lines = [f"tree,day,{marker}"]
    for tree, day, flag in rows:
        lines.append(f"{tree},{day},{'yes' if flag else 'no'}")
    return "\n".join(lines) + "\n"


def cold_csv(rows) -> str:
    """rows: list of (region, day, bool)."""
    lines = ["region,day,cold"]
    for region, day, flag in rows:
        lines.append(f"{region},{day},{'yes' if flag else 'no'}")
    return "\n".join(lines) + "\n"


# 甲/乙 only: 甲 bloom+receive day2; 乙 bloom+provide day2.
def base_two_tree():
    b = bloom_csv([("甲", "北", 1, False), ("甲", "北", 2, True),
                   ("乙", "北", 1, False), ("乙", "北", 2, True)])
    p = flag_csv("provide", [("甲", 1, False), ("甲", 2, False), ("乙", 2, True)])
    r = flag_csv("receive", [("甲", 2, True)])
    c = cold_csv([("北", 2, False)])
    return {"bloom": b, "provide": p, "receive": r, "cold": c}


class OrchardCase(unittest.TestCase):
    """每个测试自建临时目录、目录库与任务仓。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cat = Catalog(self.dir / "cat.db")
        self.repo = TaskRepository(self.dir / "task.db", self.cat)

    def tearDown(self):
        self.repo.close()
        self.cat.close()
        self.tmp.cleanup()

    def write(self, paths: dict[str, str]) -> dict[str, str]:
        out = {}
        for name, text in paths.items():
            p = self.dir / f"{name}.csv"
            p.write_text(text, encoding="utf-8")
            out[name] = str(p)
        return out

    def load(self, paths: dict[str, str], prefix: str = "t"):
        csv_paths = self.write(paths)
        tids = run_pipeline(self.cat, self.repo, csv_paths, prefix)
        facts = load_facts(self.repo, tids)
        timeline = build_timeline(facts, load_cold_claims(self.repo, tids["cold"]))
        return tids, facts, timeline


# --------------------------------------------------------------------------- #
# 1) 接入 + 列来源定位
# --------------------------------------------------------------------------- #
class IngestAndLineageTest(OrchardCase):
    def test_facts_loaded_via_real_tasks(self):
        tids, facts, tl = self.load(base_two_tree())
        self.assertEqual(facts.trees, ("甲", "乙"))
        self.assertEqual(facts.bloom["甲"], {2})
        self.assertEqual(facts.receive["甲"], {2})
        self.assertEqual(facts.provide["乙"], {2})
        # 每个关注点都是真实任务，且版本一致
        versions = {self.repo.get_task(t).version for t in tids.values()}
        self.assertEqual(len(versions), 1)

    def test_locate_record_uses_versioned_lineage(self):
        tids, facts, tl = self.load(base_two_tree())
        ref = locate_record(self.repo, tids["receive"], 0, "receive", concern="receive")
        self.assertEqual((ref.tree, ref.day, ref.concern), ("甲", 2, "receive"))
        # 来源列名与表头行号来自当时版本的 column lineage
        self.assertEqual(ref.source_column, "receive")
        self.assertEqual(ref.header_line, 1)
        # 配置来源路径指向 column_mapping 的该目标键
        self.assertEqual(ref.prov_path[:2], ("ingest", "column_mapping"))
        self.assertIsNotNone(ref.prov_value_line)

    def test_locate_record_out_of_range(self):
        tids, _, _ = self.load(base_two_tree())
        with self.assertRaises(IndexError):
            locate_record(self.repo, tids["receive"], 99, "receive", concern="receive")


# --------------------------------------------------------------------------- #
# 2) 冷天归并
# --------------------------------------------------------------------------- #
class ColdReconciliationTest(OrchardCase):
    def test_single_cold_region_shifts_all_trees_together(self):
        paths = base_two_tree()
        paths["cold"] = cold_csv([("北", 2, True)])
        _, facts, tl = self.load(paths)
        self.assertEqual(tl.cold.delay, {"北": 1})
        self.assertEqual(sorted(tl.bloom["甲"]), [3])
        self.assertEqual(sorted(tl.bloom["乙"]), [3])
        self.assertEqual(sorted(tl.provide["乙"]), [3])
        diag = diagnose_pair(tl, "甲", "乙")
        self.assertEqual([(o.day, o.provider, o.receiver) for o in diag.overlaps], [(3, "乙", "甲")])

    def test_conflict_same_day_both_occurred_and_not(self):
        paths = base_two_tree()
        paths["cold"] = cold_csv([("北", 2, True), ("北", 2, False)])
        csv_paths = self.write(paths)
        tids = run_pipeline(self.cat, self.repo, csv_paths, "c")
        facts = load_facts(self.repo, tids)
        with self.assertRaises(ColdRevisionConflict) as ctx:
            build_timeline(facts, load_cold_claims(self.repo, tids["cold"]))
        e = ctx.exception
        self.assertEqual((e.region, e.day), ("北", 2))
        # 哪一边：一条发生、一条没发生，行号可分辨
        self.assertTrue(e.occurred_ref.occurred)
        self.assertFalse(e.absent_ref.occurred)
        self.assertNotEqual(e.occurred_ref.row_index, e.absent_ref.row_index)

    def test_duplicate_occurred_counts_once(self):
        # 同一冷天被两次复跑各记一次“发生” -> 只按日历天计一次，延迟仍为 1
        from orchard.ingest import ColdClaim

        dup = [
            ColdClaim("北", 2, True, "a", 0),
            ColdClaim("北", 2, True, "b", 1),
        ]
        rec = reconcile_cold(dup)
        self.assertEqual(rec.delay, {"北": 1})

    def test_multi_day_cold_compounds(self):
        from orchard.ingest import ColdClaim

        rec = reconcile_cold([
            ColdClaim("北", 2, True, "a", 0),
            ColdClaim("北", 3, True, "a", 1),
        ])
        self.assertEqual(rec.delay, {"北": 2})

    def test_support_uses_original_day_after_shift(self):
        paths = base_two_tree()
        paths["cold"] = cold_csv([("北", 2, True)])
        _, facts, tl = self.load(paths)
        # 整园表里甲第 3 天可受，但原始记录仍是第 2 天
        self.assertEqual(tl.original_day("甲", 3), 2)
        self.assertEqual(tl.row_of("receive", "甲", 3), facts.support[("receive", "甲", 2)])


# --------------------------------------------------------------------------- #
# 3) 承诺：方向 / 床距 / 缺边
# --------------------------------------------------------------------------- #
class CommitmentTest(OrchardCase):
    def test_correct_direction_adjacent_signable(self):
        _, _, tl = self.load(base_two_tree())
        rep = evaluate_commitments(tl, {"甲": 0, "乙": 1}, self.repo)
        self.assertEqual([(c.receiver, c.provider, c.day) for c in rep.commitments],
                         [("甲", "乙", 2)])
        # 支撑记录齐备（受/供/两株开花）
        self.assertEqual(len(rep.commitments[0].support), 4)

    def test_wrong_direction_not_signable(self):
        # 甲不能供、乙不能受：方向反了，即使同天开花也不成立
        b = bloom_csv([("甲", "北", 2, True), ("乙", "北", 2, True)])
        p = flag_csv("provide", [("乙", 2, False)])  # 乙不供
        r = flag_csv("receive", [("甲", 2, True)])   # 只有甲受
        c = cold_csv([("北", 2, False)])
        _, _, tl = self.load({"bloom": b, "provide": p, "receive": r, "cold": c})
        rep = evaluate_commitments(tl, {"甲": 0, "乙": 1}, self.repo)
        self.assertEqual(rep.commitments, [])
        diag = diagnose_pair(tl, "甲", "乙")
        self.assertFalse(diag.has_overlap)

    def test_self_not_own_partner(self):
        _, _, tl = self.load(base_two_tree())
        with self.assertRaises(ValueError):
            diagnose_pair(tl, "甲", "甲")

    def test_two_bed_gap_no_neighbor_even_same_day(self):
        _, _, tl = self.load(base_two_tree())
        rep = evaluate_commitments(tl, {"甲": 0, "乙": 2}, self.repo)
        self.assertEqual(rep.commitments, [])
        self.assertEqual(len(rep.distance_gaps), 1)

    def test_missing_side_reports_day_and_side(self):
        # 乙供期改到第三天 -> 甲第2天缺“乙可供粉”
        paths = base_two_tree()
        paths["provide"] = flag_csv("provide", [("甲", 1, False), ("甲", 2, False), ("乙", 3, True)])
        _, _, tl = self.load(paths)
        diag = diagnose_pair(tl, "甲", "乙")
        self.assertFalse(diag.has_overlap)
        self.assertTrue(any(g.day == 2 and g.missing == ("provide",) for g in diag.missing_sides))
        # 描述里点名是“乙可供粉”
        self.assertIn("乙可供粉", [g.describe() for g in diag.missing_sides][0])

    def test_one_tree_can_serve_two_neighbors(self):
        # 中间株同时向左右两株供粉 -> 两条承诺并存
        # 甲(受,左) 乙(供,中) 丙(受,右)，乙开花+供 day2；甲/丙开花+受 day2
        b = bloom_csv([
            ("甲", "北", 2, True), ("乙", "北", 2, True), ("丙", "北", 2, True),
        ])
        p = flag_csv("provide", [("乙", 2, True)])
        r = flag_csv("receive", [("甲", 2, True), ("丙", 2, True)])
        c = cold_csv([("北", 2, False)])
        _, _, tl = self.load({"bloom": b, "provide": p, "receive": r, "cold": c})
        rep = evaluate_commitments(tl, {"甲": 0, "乙": 1, "丙": 2}, self.repo)
        got = {(c.receiver, c.provider, c.day) for c in rep.commitments}
        self.assertEqual(got, {("甲", "乙", 2), ("丙", "乙", 2)})


# --------------------------------------------------------------------------- #
# 4) 布局：固定稳健 / 轮式通道与上限
# --------------------------------------------------------------------------- #
class LayoutTest(OrchardCase):
    def test_fixed_layout_robust_to_possible_cold(self):
        # 甲/乙 同片区：冷天一起迟，固定图仍成立
        _, _, tl = self.load(base_two_tree())
        scen = possible_scenarios({"北"})
        fixed = best_fixed_layout(tl, tl.trees, 5, self.repo, scenarios=scen)
        self.assertTrue(fixed.robust)
        self.assertEqual(
            [(c.receiver, c.provider) for c in fixed.commitments()], [("甲", "乙")]
        )

    def test_cross_region_pair_dropped_for_permanent_map(self):
        # 甲(北,受) 乙(南,供) 同天（跨片区，单独遇冷会错开）；
        # 丙(北,供) 丁(北,受) 同天（同片区，冷天一起迟仍成立）。
        # 永久图应选稳健的 丙/丁，放弃脆弱的 甲/乙。
        b = bloom_csv([
            ("甲", "北", 2, True), ("乙", "南", 2, True),
            ("丙", "北", 2, True), ("丁", "北", 2, True),
        ])
        p = flag_csv("provide", [("乙", 2, True), ("丙", 2, True)])
        r = flag_csv("receive", [("甲", 2, True), ("丁", 2, True)])
        c = cold_csv([("北", 2, False), ("南", 2, False)])
        _, _, tl = self.load({"bloom": b, "provide": p, "receive": r, "cold": c})
        self.assertTrue(diagnose_pair(tl, "甲", "乙").has_overlap)
        self.assertTrue(diagnose_pair(tl, "丙", "丁").has_overlap)
        scen = possible_scenarios({"北", "南"})
        fixed = best_fixed_layout(tl, tl.trees, 5, self.repo, scenarios=scen)
        self.assertTrue(fixed.robust)
        pairs = {frozenset((c2.receiver, c2.provider)) for c2 in fixed.commitments()}
        self.assertIn(frozenset(("丙", "丁")), pairs)
        self.assertNotIn(frozenset(("甲", "乙")), pairs)

    def test_only_cross_region_pair_means_no_robust_map(self):
        # 只有跨片区一对时，不存在稳健永久图 -> robust=False（诚实报告不可行）
        b = bloom_csv([("甲", "北", 2, True), ("乙", "南", 2, True)])
        p = flag_csv("provide", [("乙", 2, True)])
        r = flag_csv("receive", [("甲", 2, True)])
        c = cold_csv([("北", 2, False), ("南", 2, False)])
        _, _, tl = self.load({"bloom": b, "provide": p, "receive": r, "cold": c})
        scen = possible_scenarios({"北", "南"})
        fixed = best_fixed_layout(tl, tl.trees, 5, self.repo, scenarios=scen)
        self.assertFalse(fixed.robust)

    def test_segments_respect_blocked_channel(self):
        segs = segments(5, {frozenset((1, 2))})
        self.assertEqual(segs, [[0, 1], [2, 3, 4]])

    def test_wheeled_respects_daily_cap_and_channels(self):
        _, _, tl = self.load(base_two_tree())
        # 初始把甲乙放远（0 与 4，视作 5 床两端），通道全开时可换位
        wheel = plan_wheeled(tl, {"甲": 0, "乙": 4}, 5, self.repo, days=(1,), daily_cap=1)
        dp = wheel.days[0]
        self.assertLessEqual(len(dp.moved), 1)
        # 一天的搬运上限足以把乙挪到甲旁
        self.assertEqual(
            [(c.receiver, c.provider) for c in dp.report.commitments], [("甲", "乙")]
        )

    def test_cumulative_passage_block_keeps_pots_in_segment(self):
        _, _, tl = self.load(base_two_tree())
        # 第1天开放 1-2 通道 -> 累计封锁；甲乙初始分属两段，之后都不得跨越该通道
        blocked = {1: {frozenset((1, 2))}}
        wheel = plan_wheeled(
            tl, {"甲": 0, "乙": 4}, 5, self.repo,
            days=(1, 2), blocked_by_day=blocked, daily_cap=2, cumulative_blocks=True,
        )
        segs = segments(5, {frozenset((1, 2))})  # [[0,1],[2,3,4]]
        seg_of_bed = {bed: i for i, s in enumerate(segs) for bed in s}
        init_seg = {"甲": seg_of_bed[0], "乙": seg_of_bed[4]}
        for dp in wheel.days:
            for tree, seg in init_seg.items():
                self.assertEqual(
                    seg_of_bed[dp.bed_of[tree]], seg,
                    f"github:{dp.day} {tree} 跨过了已开放通道",
                )

    def test_non_cumulative_allows_crossing_after_unblock(self):
        _, _, tl = self.load(base_two_tree())
        blocked = {1: {frozenset((1, 2))}}
        wheel = plan_wheeled(
            tl, {"甲": 0, "乙": 4}, 5, self.repo,
            days=(1, 2), blocked_by_day=blocked, daily_cap=2, cumulative_blocks=False,
        )
        # 非累计：第2天通道恢复，乙可跨到甲相邻侧
        day2_beds = {dp.day: dp.bed_of for dp in wheel.days}[2]
        self.assertLessEqual(abs(day2_beds["甲"] - day2_beds["乙"]), 1)


# --------------------------------------------------------------------------- #
# 5) 复跑两次更正（真实归档任务）
# --------------------------------------------------------------------------- #
class ReplayRevisionsTest(OrchardCase):
    def test_two_revisions_persist_and_change_outcomes(self):
        initial = base_two_tree()
        # 更正1：冷天 -> 承诺移到第3天
        rev1 = dict(initial)
        rev1["cold"] = cold_csv([("北", 2, True)])
        # 更正2：乙供期改第3天（无冷天）-> 甲失约
        rev2 = dict(initial)
        rev2["provide"] = flag_csv("provide", [("甲", 1, False), ("甲", 2, False), ("乙", 3, True)])

        def paths_of(rounds: list[dict[str, str]]) -> list[dict[str, str]]:
            """每轮的同一关注点写到独立文件，避免互相覆盖。"""
            out = []
            for r, d in enumerate(rounds):
                round_paths = {}
                for k, text in d.items():
                    p = self.dir / f"{k}.r{r}.csv"
                    p.write_text(text, encoding="utf-8")
                    round_paths[k] = str(p)
                out.append(round_paths)
            return out

        initial_paths, rev1_paths, rev2_paths = paths_of([initial, rev1, rev2])
        steps = replay_revisions(
            self.cat, self.repo, initial_paths, [rev1_paths, rev2_paths], num_beds=5
        )
        self.assertEqual([s.label for s in steps], ["初始", "更正1", "更正2"])

        # 三步都有各自归档任务，且可回查
        for st in steps:
            for tid in st.task_ids.values():
                self.assertEqual(self.repo.get_task(tid).status, "succeeded")

        # 初始：甲乙第2天
        self.assertIn(("甲", "乙", 2),
                      [(c["receiver"], c["provider"], c["day"]) for c in self._commit(steps[0])])
        # 更正1：甲乙第3天
        self.assertIn(("甲", "乙", 3),
                      [(c["receiver"], c["provider"], c["day"]) for c in self._commit(steps[1])])
        # 更正2：甲乙不再有交集
        diag = diagnose_pair(steps[2].timeline, "甲", "乙")
        self.assertFalse(diag.has_overlap)

    def _commit(self, step):
        return [
            {"receiver": c.receiver, "provider": c.provider, "day": c.day}
            for c in step.report.commitments()
        ]


# --------------------------------------------------------------------------- #
# 6) 报告渲染
# --------------------------------------------------------------------------- #
class RenderTest(OrchardCase):
    def test_report_names_commitments_and_gaps(self):
        _, _, tl = self.load(base_two_tree())
        rep = build_exhibit(tl, 5, repo=self.repo, check_cold=True)
        text = render_report(rep)
        self.assertIn("甲 <- 乙 于第2天", text)
        self.assertIn("支撑:", text)

    def test_report_shows_distance_explanation(self):
        _, _, tl = self.load(base_two_tree())
        rep = build_exhibit(tl, 5, repo=self.repo, check_cold=False)
        # 强制展示一个“同日可开但床距>1”的诊断
        forced = evaluate_commitments(tl, {"甲": 0, "乙": 2}, self.repo)
        self.assertEqual(forced.commitments, [])
        self.assertTrue(forced.distance_gaps)

    def test_deliver_is_machine_readable(self):
        _, _, tl = self.load(base_two_tree())
        rep = build_exhibit(tl, 5, repo=self.repo, check_cold=False)
        d = deliver(rep)
        self.assertEqual(d["exhibited_trees"], ["乙", "甲"] if "乙" < "甲" else ["甲", "乙"])
        self.assertEqual(d["partners"]["甲"][0]["partner"], "乙")
        self.assertEqual(d["partners"]["甲"][0]["day"], 2)
        self.assertEqual(d["partners"]["乙"][0]["direction"], "provide_to")
        self.assertTrue(d["robust_to_cold"])

    def test_scope_enforced(self):
        _, _, tl = self.load(base_two_tree())
        with self.assertRaises(ValueError):
            build_exhibit(tl, 6, repo=self.repo)  # 六床超过五床
        with self.assertRaises(ValueError):
            build_exhibit(tl, 5, repo=self.repo, days=(1, 2, 3, 4, 5))  # 五天超过四天


if __name__ == "__main__":
    unittest.main()
