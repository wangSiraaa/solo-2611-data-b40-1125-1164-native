"""口袋果园花期展图 端到端演示。

运行：``python demo/orchard_demo.py``
产物：``data/orchard_catalog.db``、``data/orchard_tasks.db`` 与控制台展图报告（含断言）。

演示覆盖题面全部要点：
1. 从实际归一任务读取开花日志/供粉日段/受粉日段，并用当时版本列来源定位补记；
2. 冷天修订让同片区多株一起迟一天；同一冷天既发生又没发生 -> 指名冲突（哪一天哪一边）；
3. 相容整园花期表 -> 方向正确、床距<=1 的承诺（甲仅次日可受、乙仅次日可供 -> 同开挂牌）；
4. 两次归档更正：供期改到第三天令甲失约；恢复后用“隔一空床”布展同样无邻树；
5. 固定床位图对片区尚可能冷天稳健；带轮盆树逐日换位（已开放通道不可穿越 + 每日上限）；
6. 两条交付路线承诺一致；六株、五床、四天以内；展示从实际归档任务复跑两次更正。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from confcat.catalog import Catalog
from confcat.ingest import TaskRepository

from orchard.commitments import diagnose_pair, evaluate_commitments
from orchard.errors import ColdRevisionConflict
from orchard.exhibit import (
    build_exhibit,
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
from orchard.observation import build_timeline

CATALOG_DB = ROOT / "data" / "orchard_catalog.db"
TASK_DB = ROOT / "data" / "orchard_tasks.db"
SAMPLES = ROOT / "orchard" / "samples"
for f in (CATALOG_DB, TASK_DB):
    f.unlink(missing_ok=True)


def hr(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def _paths(base: Path) -> dict[str, str]:
    return {
        "bloom": str(base / "bloom.csv"),
        "provide": str(base / "provide.csv"),
        "receive": str(base / "receive.csv"),
        "cold": str(base / "cold.csv"),
    }


def _load(repo, paths) -> tuple:
    tids = run_pipeline(repo.catalog, repo, paths, task_prefix="demo")
    facts = load_facts(repo, tids)
    timeline = build_timeline(facts, load_cold_claims(repo, tids["cold"]))
    return tids, facts, timeline


def main() -> int:
    with Catalog(CATALOG_DB) as cat:
        with TaskRepository(TASK_DB, cat) as repo:
            # ------------------------------------------------------------- #
            hr("1) 真实归一任务 + 当时版本列来源定位补记")
            tids, facts, timeline = _load(repo, _paths(SAMPLES))
            print("接入任务:", tids)
            print("整园花期表（无冷天）:")
            for t in facts.trees:
                print(
                    f"  {t}: 开花{sorted(timeline.bloom[t])} "
                    f"可供{sorted(timeline.provide[t])} 可受{sorted(timeline.receive[t])} "
                    f"片区{facts.region[t]}"
                )
            # 用当时版本列来源定位一条补记
            ref = locate_record(repo, tids["receive"], 0, "receive", concern="receive")
            print(
                f"定位补记: {ref.tree} 第{ref.day}天可受 "
                f"<- 任务 {ref.task_id} 行{ref.row_index} "
                f"原 CSV 列 {ref.source_column!r}(表头行 {ref.header_line}), "
                f"配置来源行 {ref.prov_value_line} 路径 {ref.prov_path}"
            )
            assert ref.tree == "甲" and ref.day == 2 and ref.source_column == "receive"

            # ------------------------------------------------------------- #
            hr("2) 示例：甲仅次日可受、相邻乙仅次日可供 -> 同开挂牌")
            diag = diagnose_pair(timeline, "甲", "乙")
            print("甲/乙 交集:", [(o.day, o.provider, o.receiver) for o in diag.overlaps])
            adj = evaluate_commitments(timeline, {"甲": 0, "乙": 1}, repo)
            print("相邻布展承诺:", [(c.receiver, c.provider, c.day) for c in adj.commitments])
            assert [(c.receiver, c.provider, c.day) for c in adj.commitments] == [("甲", "乙", 2)]

            # ------------------------------------------------------------- #
            hr("3) 冷天修订：同片区多株一起迟一天，承诺日期随之后移")
            cold_dir = ROOT / "data" / "_cold_demo"
            cold_dir.mkdir(exist_ok=True)
            for name in ("bloom.csv", "provide.csv", "receive.csv"):
                (cold_dir / name).write_text((SAMPLES / name).read_text(encoding="utf-8"), encoding="utf-8")
            (cold_dir / "cold.csv").write_text("region,day,cold\n北区,2,yes\n南区,1,no\n", encoding="utf-8")
            tids2 = run_pipeline(cat, repo, _paths(cold_dir), task_prefix="demo-cold")
            facts2 = load_facts(repo, tids2)
            tl2 = build_timeline(facts2, load_cold_claims(repo, tids2["cold"]))
            print("北区延迟天数:", tl2.cold.delay)
            print("甲开花/可受:", sorted(tl2.bloom["甲"]), sorted(tl2.receive["甲"]))
            print("乙开花/可供:", sorted(tl2.bloom["乙"]), sorted(tl2.provide["乙"]))
            diag2 = diagnose_pair(tl2, "甲", "乙")
            print("甲/乙 交集（冷天后）:", [(o.day, o.provider, o.receiver) for o in diag2.overlaps])
            assert [(o.day, o.provider, o.receiver) for o in diag2.overlaps] == [(3, "乙", "甲")]
            assert tl2.cold.delay["北区"] == 1

            # ------------------------------------------------------------- #
            hr("4) 同一冷天既发生又没发生 -> 指名冲突（哪一天哪一边）")
            conflict_dir = ROOT / "data" / "_conflict_demo"
            conflict_dir.mkdir(exist_ok=True)
            for name in ("bloom.csv", "provide.csv", "receive.csv"):
                (conflict_dir / name).write_text((SAMPLES / name).read_text(encoding="utf-8"), encoding="utf-8")
            (conflict_dir / "cold.csv").write_text(
                "region,day,cold\n北区,2,yes\n北区,2,no\n", encoding="utf-8"
            )
            tids3 = run_pipeline(cat, repo, _paths(conflict_dir), task_prefix="demo-conflict")
            facts3 = load_facts(repo, tids3)
            try:
                build_timeline(facts3, load_cold_claims(repo, tids3["cold"]))
                raise AssertionError("应抛冷天冲突")
            except ColdRevisionConflict as e:
                print("冲突:", e)
                print(
                    f"  缺的是: 第{e.day}天 片区{e.region} 的"
                    f"「发生 #{e.occurred_ref.row_index}」与"
                    f"「没发生 #{e.absent_ref.row_index}」两边"
                )
                assert e.day == 2 and e.region == "北区"

            # ------------------------------------------------------------- #
            hr("5) 两次归档更正复跑：供期改到第三天令甲失约；恢复后隔一空床仍无邻树")
            steps = replay_revisions(
                cat,
                repo,
                _paths(SAMPLES),
                [_paths(SAMPLES / "rev1"), _paths(SAMPLES / "rev2")],
                num_beds=5,
            )
            for st in steps:
                print(f"\n-- {st.label} (任务 {st.task_ids['bloom']} 等) --")
                print(render_report(st.report))

            # rev2：乙供期改第三天 -> 甲失约（第2天缺乙可供）
            rev2 = steps[2]
            diag_break = diagnose_pair(rev2.timeline, "甲", "乙")
            print("\n更正2 甲/乙 缺口:",
                  [(g.day, g.missing) for g in diag_break.missing_sides])
            assert not diag_break.has_overlap
            assert any(g.day == 2 and "provide" in g.missing for g in diag_break.missing_sides)

            # 恢复原花期（用初始表），但只许乙放隔一空床的位置（床距2）-> 无邻树
            forced = evaluate_commitments(timeline, {"甲": 0, "乙": 2}, repo)
            print("隔一空床(甲0,乙2) 承诺:", [(c.receiver, c.provider, c.day) for c in forced.commitments])
            print("  距离失配:", [(g.a, g.b) for g in forced.distance_gaps])
            assert forced.commitments == []
            assert forced.distance_gaps, "应报距离失配（同日可开但床距>1）"

            # ------------------------------------------------------------- #
            hr("6) 固定床位图对片区尚可能冷天稳健")
            regions = {timeline.region_of(t) for t in timeline.trees}
            scen = possible_scenarios(regions)
            fixed = best_fixed_layout(timeline, timeline.trees, 5, repo, scenarios=scen)
            print("床位分配:", fixed.bed_of)
            print("对全部冷天情景稳健:", fixed.robust,
                  "（情景数", len(scen), "）")
            for c in fixed.commitments():
                print(f"  承诺: {c.receiver} <- {c.provider} 于第{c.day}天")
            assert fixed.robust

            # ------------------------------------------------------------- #
            hr("7) 带轮盆树逐日换位（已开放通道不可穿越 + 每日搬运上限）")
            # 场景：戊/己 分属北/南，永久图不敢押（见 6 的取舍说明）；改走带轮盆：
            # 早班看到真实花况后逐日换位。初始把戊放 0、己放 4（离得远，无法挂牌）。
            # 约束：第1天开放 1-2 通道 -> 此后累计不可穿越；每日搬 1 盆。
            blocked = {1: {frozenset((1, 2))}}
            wheel = plan_wheeled(
                timeline,
                {"戊": 0, "己": 4},
                5,
                repo,
                days=(1, 2, 3),
                blocked_by_day=blocked,
                daily_cap=1,
                cumulative_blocks=True,
            )
            print("初始无牌:", [(c.receiver, c.provider) for c in
                             evaluate_commitments(timeline, {"戊": 0, "己": 4}, repo).commitments])
            for dp in wheel.days:
                print(
                    f"  第{dp.day}天 bed={dp.bed_of} 换位={dp.moved or '无'} "
                    f"承诺={[(c.receiver, c.provider, c.day) for c in dp.report.commitments]}"
                )
            # 每日 1 盆、需两盆各挪一步才能相邻（戊->1, 己->2），故逐步推进；
            # 累计阻断 1-2 使两盆分居两段，但床 1 与床 2 编号相邻——戊停在 1、己停在 2
            # 即“隔开放通道不再穿越、却仍互为邻树”。核对无盆越段。
            seg_of = {b: i for i, s in enumerate(segments(5, {frozenset((1, 2))})) for b in s}
            assert seg_of[0] == seg_of[1] and seg_of[2] == seg_of[3] == seg_of[4]
            for dp in wheel.days:
                assert len(dp.moved) <= 1
                assert seg_of[dp.bed_of["戊"]] == 0 and seg_of[dp.bed_of["己"]] == 1
            # 最终应推进到 戊@1、己@2 并挂牌（跨开放通道相邻，未穿越）
            last = wheel.days[-1]
            print(
                "  终态:", last.bed_of,
                "承诺:", [(c.receiver, c.provider, c.day) for c in last.report.commitments],
            )
            assert last.bed_of == {"戊": 1, "己": 2}
            assert [(c.receiver, c.provider) for c in last.report.commitments] == [("己", "戊")]

            # 反例：无阻断时第1天即可把己挪到戊旁挂牌
            free = plan_wheeled(
                timeline, {"戊": 0, "己": 4}, 5, repo, days=(1,), daily_cap=1
            )
            print("  无阻断时第1天:", free.days[0].bed_of, "换位", free.days[0].moved,
                  "-> 承诺", [(c.receiver, c.provider) for c in free.days[0].report.commitments])
            assert [(c.receiver, c.provider) for c in free.days[0].report.commitments]

            # 两条交付路线承诺一致（固定图 vs 轮式，同一份展览承诺）
            rep = build_exhibit(
                timeline,
                5,
                repo=repo,
                check_cold=True,
                wheeled=True,
                days=(1, 2, 3, 4),
                blocked_by_day={},
                daily_cap=1,
            )
            print("\n固定/轮式承诺一致:", rep.consistency_ok)
            assert rep.consistency_ok

    # 清理演示用临时 CSV（保留 SQLite 归档产物）
    import shutil
    for name in ("_cold_demo", "_conflict_demo"):
        shutil.rmtree(ROOT / "data" / name, ignore_errors=True)

    hr("全部果园演示断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
