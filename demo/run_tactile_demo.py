"""触读地图展端到端演示：两份归一任务 -> 制牌 -> 布牌审计 -> 无法开放站点。

运行：``python demo/run_tactile_demo.py``
产物：``data/tactile/catalog.db``（目录）与控制台报告。
内置断言，任何一步不符即失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from confcat.catalog import Catalog
from confcat.errors import TaskRejected
from confcat.tactile.domain import NoMarkerPolicy, read_sign
from confcat.tactile.exhibition import load_exhibition
from confcat.tactile.placement import Booth, Entrance, evaluate, plan_exhibition
from confcat.tactile.plates import load_base_bundle, load_plate_bundle

CATALOG_DB = ROOT / "data" / "tactile" / "catalog.db"
DOT_CSV = ROOT / "data" / "tactile" / "dot_tasks.csv"
BASE_CSV = ROOT / "data" / "tactile" / "base_tasks.csv"
DOT_YAML = ROOT / "samples" / "tactile" / "dot_product_v1.yaml"
BASE_YAML = ROOT / "samples" / "tactile" / "base_product_v1.yaml"
EXH_YAML = ROOT / "samples" / "tactile" / "exhibition.yaml"
CATALOG_DB.parent.mkdir(parents=True, exist_ok=True)
CATALOG_DB.unlink(missing_ok=True)


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    # 0. 发布两份归档产品 ----------------------------------------------- #
    hr("0) 发布两份归一任务配置（点阵刻样 / 底座朝向，各自版本快照）")
    with Catalog(CATALOG_DB) as cat:
        dot_info = cat.publish_file("tactile-dot", DOT_YAML)
        base_info = cat.publish_file("tactile-base", BASE_YAML)
        print("点阵产品版本:", dot_info.version, " 底座产品版本:", base_info.version)

        plates = load_plate_bundle(cat, "tactile-dot", "doc-0", DOT_CSV)
        bases = load_base_bundle(cat, "tactile-base", "doc-0", BASE_CSV)

        # 1. 血缘：实际任务行、列映射、归档版本 -------------------------- #
        hr("1) 归一血缘：每块牌来自哪一行、列映射指向哪个原列、哪条配置来源")
        src = plates.sources["P-EXIT"]
        print("P-EXIT 任务行:", src["csv_row"])
        print("  产品/版本/文档:", src["product"], src["version"], src["doc"])
        dot_lineage = src["lineage"]["dot_cells"]
        print("  dot_cells <- 原列", dot_lineage["source_column"],
              "配置定义行", dot_lineage["defined_line"])
        assert dot_lineage["source_column"] == "convex_cells"
        bsrc = bases.sources["B-SWIVEL"]
        print("B-SWIVEL 任务行:", bsrc["csv_row"], " 朝向:",
              [o for o in bases.get("B-SWIVEL").orientations])

        # 2. 制牌员：可触摸图样集合 -------------------------------------- #
        hr("2) 制牌员：把凸点遮挡、可摸边、允许翻转折成触读图样集合")
        exit_plate = plates.get("P-EXIT")
        stair_plate = plates.get("P-STAIR")
        swivel = bases.get("B-SWIVEL")
        lock_l = bases.get("B-LOCK-L")
        for name, plate, base in (("出口牌+普通座", exit_plate, swivel),
                                  ("楼梯牌+锁死座", stair_plate, lock_l)):
            sign = read_sign(plate, base, NoMarkerPolicy())
            print(name, plate.render(), "->", [r.render(plate.n) for r in sign.readings])

        # 3. 三格玩具牌的四个关键情形 ------------------------------------ #
        hr("3) 三格玩具：翻转一块牌，会不会把出口摸成楼梯")
        lock_r = bases.get("B-LOCK-R")

        def two_sign_booth(p_a, p_b, base_a, base_b, policy=NoMarkerPolicy()):
            return Booth("toy", [
                Entrance("e-exit", read_sign(p_a, base_a, policy), "出口"),
                Entrance("e-stair", read_sign(p_b, base_b, policy), "楼梯"),
            ])

        # 3a 两锁正座：可分
        c1 = evaluate([two_sign_booth(exit_plate, stair_plate, lock_l, lock_r)])
        print("3a 两正向锁死座：混淆数 =", len(c1), "（应为 0）")
        assert not c1

        # 3b 出口制版行改到右端点：撞形
        bad_exit = plates.get("P-EXIT-R")
        c2 = evaluate([two_sign_booth(bad_exit, stair_plate, lock_l, lock_r)])
        print("3b 出口改刻右端点：混淆数 =", len(c2), "（应给出引错的两次触读）")
        assert len(c2) == 1
        print(c2[0].describe())

        # 3c 恢复左端点，换允许转半周的普通座：出口可能被摸成楼梯
        c3 = evaluate([two_sign_booth(exit_plate, stair_plate, swivel, swivel)])
        print("3c 两只普通座（无方向标记）：混淆数 =", len(c3))
        assert len(c3) == 2
        for c in c3:
            print(c.describe())
        assert any(c.first.destination == "出口" and c.second.destination == "楼梯" for c in c3)

        # 3d 同形状同去向不算混淆
        same = Booth("same", [
            Entrance("e1", read_sign(exit_plate, lock_l, NoMarkerPolicy()), "出口"),
            Entrance("e2", read_sign(exit_plate, lock_r, NoMarkerPolicy()), "出口"),
        ])
        assert not evaluate([same])
        print("3d 两块相同形状但同指向出口：不混淆（仅不同实际去向才算）")

        # 4. 遮住端点仍可摸边的牌，供下一位布展员复验 -------------------- #
        hr("4) 遮端点牌：凸点摸不到、边仍可摸，复验其歧义")
        occ_l, occ_r = plates.get("P-OCC-L"), plates.get("P-OCC-R")
        print("刻样:", occ_l.render(), "/", occ_r.render(),
              " 可摸凸点:", sorted(occ_l.felt_dots), sorted(occ_r.felt_dots))
        occ_booth = Booth("occ", [
            Entrance("o-exit", read_sign(occ_l, swivel, NoMarkerPolicy()), "出口"),
            Entrance("o-stair", read_sign(occ_r, lock_l, NoMarkerPolicy()), "楼梯"),
        ])
        c4 = evaluate([occ_booth])
        print("遮端牌混淆数 =", len(c4), "（无凸点信息，仅靠边无法区分两个去向）")
        assert c4 and all(k[0] == () for k in [c.key for c in c4])

        # 5. 布展规划：无标记 vs 方案 A 方向标记 -------------------------- #
        hr("5) 六展位规划：无标记策略与方案 A（固定端锚点）对比")
        ex = load_exhibition(EXH_YAML, plates, bases)
        plan_none = plan_exhibition(ex.booths, ex.inventory, NoMarkerPolicy(), False)
        print("无标记： 开放", plan_none.open_booths, " 无法开放", plan_none.closed_booths)
        assert set(plan_none.closed_booths) == {"B2", "B3", "B4", "B5"}

        plan_anchor = plan_exhibition(ex.booths, ex.inventory, ex.marker_policy, False)
        print("方案 A： 开放", plan_anchor.open_booths, " 无法开放", plan_anchor.closed_booths)
        assert set(plan_anchor.open_booths) == {"B1", "B3", "B4", "B5", "B6"}
        assert plan_anchor.closed_booths == ["B2"]
        b2 = next(b for b in plan_anchor.booth_plans if b.booth_id == "B2")
        print("B2 无法开放原因:", b2.blocked_reasons)
        print("B2 改刻建议:", [p.describe() for p in b2.proposals])
        assert b2.proposals and b2.proposals[0].add_cells == (0,)
        # 锚点必须占真实空格、且固定端被占用时打不上
        # 楼梯牌右端有凸点 -> 固定右端锚点打不上；出口牌右端为空 -> 锚点落在格 2
        assert ex.marker_policy.marker_cell(stair_plate) is None
        assert ex.marker_policy.marker_cell(exit_plate) == 2
        assert ex.marker_policy.marker_cell(plates.get("P-4A")) == 3

        # 6. 库存不足 -> 无法开放站点 ------------------------------------ #
        hr("6) 展方输入：底座库存不足时给出无法开放站点")
        from confcat.tactile.placement import BaseInventory
        tight = BaseInventory.from_specs(
            [bases.get("B-LOCK-L-TIGHT"), bases.get("B-SWIVEL")],
            {"B-LOCK-L-TIGHT": 1, "B-SWIVEL": 1},
        )
        small = Booth("tight", [
            Entrance("t1", read_sign(exit_plate, bases.get("B-LOCK-L-TIGHT"), NoMarkerPolicy()), "出口"),
            Entrance("t2", read_sign(stair_plate, bases.get("B-LOCK-L-TIGHT"), NoMarkerPolicy()), "楼梯"),
        ])
        tight_plan = plan_exhibition([small], tight, NoMarkerPolicy(), False)
        print("库存 1 件锁死座给 2 个入口:", tight_plan.booth_plans[0].openable,
              tight_plan.booth_plans[0].blocked_reasons)
        assert not tight_plan.booth_plans[0].openable

        # 7. 版本隔离：发布 v2 不影响已按 v1 归一的结果 ------------------ #
        hr("7) 归档版本隔离：点阵产品发布 v2，v1 任务行不受影响")
        # v2 把 dot_cells 的列映射改指一份“中点凸点”的新任务行（配置层面真实改版）
        v2_csv = DOT_CSV.parent / "dot_tasks_v2.csv"
        v2_csv.write_text(DOT_CSV.read_text(encoding="utf-8").replace("P-EXIT,3,0,",
                                                                      "P-EXIT,3,1,"),
                          encoding="utf-8")
        v2_yaml = DOT_YAML.read_text(encoding="utf-8").replace(
            "dot_cells: convex_cells", "dot_cells: convex_cells"
        ).replace("file:///data/tactile/dot_tasks.csv",
                  "file:///data/tactile/dot_tasks_v2.csv")
        cat.publish("tactile-dot", v2_yaml, source_path=str(DOT_YAML))
        plates_v1 = load_plate_bundle(cat, "tactile-dot", "doc-0", DOT_CSV, version=1)
        plates_v2 = load_plate_bundle(cat, "tactile-dot", "doc-0", v2_csv, version=2)
        print("v1 P-EXIT 凸点:", sorted(plates_v1.get("P-EXIT").dots),
              " v2 P-EXIT 凸点:", sorted(plates_v2.get("P-EXIT").dots))
        assert plates_v1.get("P-EXIT").dots == frozenset({0})
        assert plates_v2.get("P-EXIT").dots == frozenset({1})

        # 8. 整次拒绝：列映射指向不存在的原列 ---------------------------- #
        hr("8) 归一任务整次拒绝：列映射原列缺失，任务不产出任何牌")
        bad = ("tactile:\n  source_uri: file:///x.csv\n  column_mapping:\n"
               "    plate_id: ghost_id\n    n: n_cells\n    dot_cells: convex_cells\n")
        cat.publish("tactile-dot-bad", bad)
        try:
            load_plate_bundle(cat, "tactile-dot-bad", "doc-0", DOT_CSV)
        except TaskRejected as exc:
            print("已拒绝:", exc)
        else:
            raise AssertionError("应当整次拒绝")

    # 9. 重开目录回查 --------------------------------------------------- #
    hr("9) 重开目录：版本与列血缘仍可回查")
    with Catalog(CATALOG_DB) as cat2:
        again = load_plate_bundle(cat2, "tactile-dot", "doc-0", DOT_CSV, version=1)
        lineage = again.sources["P-STAIR"]["lineage"]["plate_id"]
        print("重开后 P-STAIR plate_id <-", lineage["source_column"],
              "配置行", lineage["defined_line"], "完整性:", cat2.verify("tactile-dot"))
        assert cat2.verify("tactile-dot")["doc-0"] == "ok"

    hr("全部触读演示断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
