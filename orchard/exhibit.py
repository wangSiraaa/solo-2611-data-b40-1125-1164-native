"""展图编排：把接入事实、整园花期表、承诺、固定/轮式布局串成一份交付。

本模块同时提供**从实际归档任务复跑两次更正**的入口：
:func:`run_pipeline` 针对一组（可能已被更正过的）CSV 现场创建真实接入任务并落下
归档行；:func:`replay_revisions` 依次跑初始 + 两次更正，返回每一步的归档任务号与
展图结果，便于比较“哪次更正让哪几株一起迟一天 / 哪次让承诺失约”。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from confcat.catalog import Catalog
from confcat.ingest import TaskRepository

from . import DAYS, MAX_BEDS, MAX_TREES
from .commitments import diagnose_pair
from .ingest import RawFacts, load_cold_claims, load_facts
from .layout import (
    ColdScenario,
    FixedLayoutResult,
    WheeledPlan,
    best_fixed_layout,
    plan_wheeled,
    possible_scenarios,
)
from .observation import Timeline, build_timeline


# --------------------------------------------------------------------------- #
# 配置与任务
# --------------------------------------------------------------------------- #
CONCERNS = ("bloom", "provide", "receive", "cold")

# 每个关注点的目标列（与原 CSV 列同名；列映射由 config 给出，lineage 记录来源）
COLUMNS = {
    "bloom": ("tree", "region", "day", "bloom"),
    "provide": ("tree", "day", "provide"),
    "receive": ("tree", "day", "receive"),
    "cold": ("region", "day", "cold"),
}


def render_config_yaml(csv_paths: dict[str, str], product_label: str = "orchard-facts") -> str:
    """为四个关注点生成一份多文档受限 YAML（每文档各自 ingest 配置）。"""
    chunks: list[str] = []
    for concern in CONCERNS:
        cols = COLUMNS[concern]
        lines = [
            "---",
            f"concern: {concern}",
            "ingest:",
            f"  source_uri: {csv_paths[concern]}",
            "  column_mapping:",
        ]
        for c in cols:
            lines.append(f"    {c}: {c}")
        chunks.append("\n".join(lines) + "\n")
    return "".join(chunks)


def run_pipeline(
    catalog: Catalog,
    repo: TaskRepository,
    csv_paths: dict[str, str],
    task_prefix: str,
    *,
    version: int | None = None,
) -> dict[str, str]:
    """发布（或复用）配置并按四个关注点各建一个真实接入任务。

    返回 ``{"bloom": task_id, ...}``。``version`` 为 ``None`` 时发布新版本；
    给定 ``version`` 时在该版本上建任务（复跑更正时固定版本便于对照）。
    """
    if version is None:
        info = catalog.publish(
            "orchard-facts",
            render_config_yaml(csv_paths),
            doc_names=list(CONCERNS),
        )
        version = info.version
    task_ids: dict[str, str] = {}
    for concern in CONCERNS:
        tid = f"{task_prefix}-{concern}"
        summary = repo.create_task(
            tid, "orchard-facts", concern, csv_paths[concern], version=version
        )
        if summary.version != version:
            raise RuntimeError(f"任务 {tid} 未绑定预期版本 v{version}")
        task_ids[concern] = tid
    return task_ids


# --------------------------------------------------------------------------- #
# 展图结果
# --------------------------------------------------------------------------- #
@dataclass
class ExhibitReport:
    timeline: Timeline
    fixed: FixedLayoutResult
    wheeled: WheeledPlan | None
    consistency_ok: bool
    notes: list[str] = field(default_factory=list)
    pair_notes: list[str] = field(default_factory=list)  # 关键树对的缺口/失配说明

    def signable(self) -> dict[str, list[tuple[str, int]]]:
        return self.fixed.nominal.partner_dates()

    def commitments(self):
        return self.fixed.nominal.commitments


def build_exhibit(
    timeline: Timeline,
    num_beds: int,
    *,
    pool=None,
    repo=None,
    check_cold: bool = True,
    days=DAYS,
    wheeled: bool = False,
    blocked_by_day=None,
    daily_cap: int = 1,
    enforce_scope: bool = True,
) -> ExhibitReport:
    """产出一份展图：固定床位图（必要时对冷天情景稳健）+ 可选轮式逐日方案。

    两条路线若同时计算，则校验承诺树对/方向一致（``consistency_ok``）。
    ``enforce_scope`` 时校验题面规模（六株、五床、四天以内）。
    """
    pool = list(pool if pool is not None else timeline.trees)
    if enforce_scope:
        if len(pool) > MAX_TREES:
            raise ValueError(f"超出规模：{len(pool)} 株 > {MAX_TREES}")
        if num_beds > MAX_BEDS:
            raise ValueError(f"超出规模：{num_beds} 床 > {MAX_BEDS}")
        if days and max(days) > max(DAYS):
            raise ValueError(f"超出规模：{max(days)} 天 > {max(DAYS)}")
    scenarios = (
        possible_scenarios({timeline.region_of(t) for t in pool})
        if check_cold
        else [ColdScenario.nominal()]
    )
    fixed = best_fixed_layout(timeline, pool, num_beds, repo, scenarios=scenarios)

    # 解释为何某些“同日可开”的树对没进固定图：跨片区时冷天只迟一边 -> 日期错开
    pair_notes: list[str] = []
    if check_cold:
        placed_pairs = {frozenset((c.receiver, c.provider)) for c in fixed.commitments()}
        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                a, b = pool[i], pool[j]
                if frozenset((a, b)) in placed_pairs:
                    continue
                diag = diagnose_pair(timeline, a, b)
                if not diag.overlaps:
                    continue
                days = ",".join(str(o.day) for o in diag.overlaps)
                ra, rb = timeline.region_of(a), timeline.region_of(b)
                if ra != rb:
                    pair_notes.append(
                        f"{a}/{b} 第{days}天可开，但分属 {ra}/{rb}；片区单独遇冷会只迟一边、"
                        f"日期错开，故不入永久床位图（可留带轮盆按真实花况次日再换）"
                    )
                elif a in fixed.bed_of and b in fixed.bed_of:
                    pair_notes.append(
                        f"{a}/{b} 第{days}天可开，但固定图里未能同床相邻"
                    )

    wheel = None
    consistency_ok = True
    notes: list[str] = []
    if wheeled:
        initial = dict(sorted(fixed.bed_of.items(), key=lambda kv: kv[1]))
        wheel = plan_wheeled(
            timeline,
            initial,
            num_beds,
            repo,
            days=list(days),
            blocked_by_day=blocked_by_day or {},
            daily_cap=daily_cap,
        )
        fixed_pairs = {(c.receiver, c.provider) for c in fixed.commitments()}
        wheel_pairs = {(c.receiver, c.provider) for c in wheel.commitments()}
        consistency_ok = fixed_pairs == wheel_pairs
        if not consistency_ok:
            notes.append(
                f"固定图承诺对 {sorted(fixed_pairs)} 与轮式承诺对 {sorted(wheel_pairs)} 不一致"
            )
        if not wheel.feasible:
            notes.extend(wheel.notes)
    if not fixed.robust:
        notes.append("固定床位图对某些冷天情景不稳健：" + ", ".join(fixed.broken_scenarios))
    return ExhibitReport(
        timeline=timeline,
        fixed=fixed,
        wheeled=wheel,
        consistency_ok=consistency_ok,
        notes=notes,
        pair_notes=pair_notes,
    )


def deliver(report: ExhibitReport) -> dict:
    """交出最终承诺：开展的树、每株的伙伴与日期、未成牌原因摘要。

    这是“同一份展览承诺”的可机读交付物；固定与轮式两条路线都据此出展签。
    """
    timeline = report.timeline
    bed_of = report.fixed.bed_of
    commitments = report.commitments()
    partners: dict[str, list[dict]] = {}
    for c in commitments:
        partners.setdefault(c.receiver, []).append(
            {"partner": c.provider, "day": c.day, "direction": "receive_from",
             "bed_gap": c.bed_gap}
        )
        partners.setdefault(c.provider, []).append(
            {"partner": c.receiver, "day": c.day, "direction": "provide_to",
             "bed_gap": c.bed_gap}
        )
    unexhibited = [t for t in timeline.trees if t not in partners]
    return {
        "exhibited_trees": sorted(partners),
        "partners": partners,
        "unexhibited_trees": unexhibited,
        "beds": dict(bed_of),
        "robust_to_cold": report.fixed.robust,
        "wheeled_consistent": report.consistency_ok,
        "notes": list(report.notes),
        "pair_notes": list(report.pair_notes),
    }


# --------------------------------------------------------------------------- #
# 从实际归档任务复跑两次更正
# --------------------------------------------------------------------------- #
@dataclass
class RevisionStep:
    label: str
    task_ids: dict[str, str]
    timeline: Timeline
    facts: RawFacts
    report: ExhibitReport | None


def replay_revisions(
    catalog: Catalog,
    repo: TaskRepository,
    initial_csv_paths: dict[str, str],
    revision_csv_paths: list[dict[str, str]],
    num_beds: int,
    *,
    check_cold: bool = True,
) -> list[RevisionStep]:
    """跑初始 + 每份更正，返回每一步的真实归档任务号与展图。

    每次更正可改任意 CSV（数据层更正），配置版本随发布前进；数据集每次都
    以全新 task 前缀落库，故每一步都可独立回查（真正“从实际归档任务复跑”）。
    """
    steps: list[RevisionStep] = []
    all_rounds = [("初始", initial_csv_paths)] + [
        (f"更正{i + 1}", paths) for i, paths in enumerate(revision_csv_paths)
    ]
    for i, (label, csv_paths) in enumerate(all_rounds):
        task_ids = run_pipeline(catalog, repo, csv_paths, task_prefix=f"rev{i}")
        facts = load_facts(repo, task_ids)
        claims = load_cold_claims(repo, task_ids["cold"])
        timeline = build_timeline(facts, claims)
        report = build_exhibit(
            timeline, num_beds, repo=repo, check_cold=check_cold
        )
        steps.append(RevisionStep(label=label, task_ids=task_ids, timeline=timeline, facts=facts, report=report))
    return steps


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _tree_line(timeline: Timeline, tree: str, bed_of: dict[str, int]) -> str:
    bed = bed_of.get(tree, "-")
    bloom = ",".join(map(str, sorted(timeline.bloom[tree]))) or "-"
    prov = ",".join(map(str, sorted(timeline.provide[tree]))) or "-"
    recv = ",".join(map(str, sorted(timeline.receive[tree]))) or "-"
    return f"  {tree:<3} bed={bed!s:<2} bloom={bloom:<8} provide={prov:<8} receive={recv}"


def render_report(report: ExhibitReport) -> str:
    """把展图渲染成可读文本（展签/报告用）。"""
    tl = report.timeline
    bed_of = report.fixed.bed_of
    lines: list[str] = []
    lines.append("整园花期表（已应用冷天平移）")
    for tree in tl.trees:
        lines.append(_tree_line(tl, tree, bed_of))
    unplaced = [t for t in tl.trees if t not in bed_of]
    if unplaced:
        lines.append("  未入床：" + ", ".join(unplaced))

    committed_pairs = {
        frozenset((c.receiver, c.provider)) for c in report.commitments()
    }

    lines.append("")
    lines.append("可挂牌承诺（受体 <- 供体 @ 日，床距）")
    if not report.commitments():
        lines.append("  （无）")
    for c in report.commitments():
        lines.append(
            f"  {c.receiver} <- {c.provider} 于第{c.day}天（床距 {c.bed_gap}）"
        )
        for ref in c.support:
            lines.append(
                f"      支撑: {ref.concern} {ref.tree} 记录第{ref.day}天 "
                f"<- 任务 {ref.task_id} 行{ref.row_index} "
                f"原列 {ref.source_column!r}（表头行 {ref.header_line}，"
                f"配置行 {ref.prov_value_line}）"
            )

    # 关键树对诊断：凡“有交集却未挂牌”或“差一项”的树对，都指出原因
    lines.append("")
    lines.append("树对诊断（缺日/缺边/床距）")
    any_diag = False
    trees = list(tl.trees)
    for i in range(len(trees)):
        for j in range(i + 1, len(trees)):
            a, b = trees[i], trees[j]
            diag = diagnose_pair(tl, a, b)
            pair = frozenset((a, b))
            if diag.overlaps and pair not in committed_pairs:
                days = ",".join(str(o.day) for o in diag.overlaps)
                if a in bed_of and b in bed_of:
                    why = f"床距 {abs(bed_of[a] - bed_of[b])} 超过 1"
                else:
                    why = "未同时入床"
                lines.append(f"  {a}/{b}: 第{days}天有交集但无挂牌邻树（{why}）")
                any_diag = True
            elif not diag.overlaps and diag.missing_sides:
                for ms in diag.missing_sides:
                    lines.append(f"  {a}/{b}: {ms.describe()}（差一项，无法挂牌）")
                any_diag = True
    if not any_diag:
        lines.append("  （所有相关树对均无缺口）")

    if report.pair_notes:
        lines.append("")
        lines.append("固定床位图的取舍说明")
        for n in report.pair_notes:
            lines.append(f"  * {n}")

    if report.wheeled is not None:
        lines.append("")
        lines.append("带轮盆树逐日方案")
        for dp in report.wheeled.days:
            moved = ",".join(dp.moved) or "无"
            lines.append(f"  第{dp.day}天 换位={moved}")

    if report.notes:
        lines.append("")
        lines.append("备注")
        for n in report.notes:
            lines.append(f"  - {n}")
    return "\n".join(lines)
