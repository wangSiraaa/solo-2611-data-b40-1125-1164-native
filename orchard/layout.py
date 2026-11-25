"""展务端：把整园花期表与实地床位画在一起。

两条交付路线（二选一，但承诺必须一致）
======================================
固定床位图 (:func:`best_fixed_layout`)
    花前把所有树固定进地床。永久床位图要对**片区尚可能出现的冷天**稳健：
    对每个候选冷天情景（哪些片区各迟一天）重算整园花期表，要求同一批承诺仍成立
    （日期可平移，但树对/方向不变）。稳健性以 ``scenarios`` 的形式显式给出。

带轮盆树 (:func:`plan_wheeled`)
    早班看到真实花况后逐日换邻位。约束：
      * 已开放的通道不可推盆穿过（按日给出 ``blocked_by_day``，被阻断的相邻床位边界）；
      * 每天有搬运上限（``daily_cap``，统计当日换了床位的盆数）；
      * 床位按通道分成若干连通段，盆只能在同段内换位。

公共约定：床位 ``0..num_beds-1`` 线性排列，``|bed_a - bed_b| <= 1`` 视为相邻（可作伴）；
``六株五床`` 意味着至多五株能上台，余下不入床即不参展。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .commitments import Commitment, evaluate_commitments
from .observation import Timeline


# --------------------------------------------------------------------------- #
# 排列枚举
# --------------------------------------------------------------------------- #
def arrangements(pool, num_beds: int):
    """把 ``pool`` 中若干株注入不同床位；产出 tree->bed 映射。

    枚举所有大小 ``k <= min(len(pool), num_beds)`` 的树子集与床位排列——允许
    “少放几株”以获得更干净的展图（例如一株在花期上谁都配不上，不入床即不参展）。
    """
    pool = list(pool)
    cap = min(len(pool), num_beds)
    for k in range(cap, 0, -1):
        for chosen in itertools.combinations(pool, k):
            for perm in itertools.permutations(range(num_beds), k):
                yield dict(zip(chosen, perm))


def _score(report, bed_of: dict[str, int]) -> tuple:
    """排列评分：参展树数 > 承诺数 > 少浪费床位。

    “浪费床位”= 入了床却没有达成任何承诺的树；固定图不该把床给无法挂牌的树。
    """
    exhibited = len(report.exhibited_trees())
    partners = report.partner_dates()
    unpaired_placed = sum(1 for t in bed_of if t not in partners)
    return (exhibited, len(report.commitments), -unpaired_placed)


# --------------------------------------------------------------------------- #
# 冷天情景
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ColdScenario:
    """一个“尚可能出现的冷天”情景：``regions`` 中每个片区发生一次冷天（迟一天）。"""

    regions: frozenset[str]

    @classmethod
    def nominal(cls) -> "ColdScenario":
        return cls(frozenset())

    def label(self) -> str:
        return "无冷天" if not self.regions else "冷天:" + "+".join(sorted(self.regions))


def possible_scenarios(regions) -> list[ColdScenario]:
    """片区集合的全部冷天子集（含空集），用作固定床位图的稳健性检验。"""
    regions = sorted(set(regions))
    out: list[ColdScenario] = []
    for r in range(len(regions) + 1):
        for combo in itertools.combinations(regions, r):
            out.append(ColdScenario(frozenset(combo)))
    return out


def timeline_for_scenario(timeline: Timeline, scenario: ColdScenario) -> Timeline:
    """在既有冷天归并之上叠加一个情景（每个列名片区再迟一天）。

    情景断言用互不相同的负日号，确保它计入“发生日”集合却不会与真实冷天同 ``(片区,日)``
    冲突；延迟按**不同发生日**计数，因此叠加后每片区恰好多迟 1 天。
    """
    from .ingest import ColdClaim
    from .observation import build_timeline

    claims = list(timeline.claims)
    for i, region in enumerate(sorted(scenario.regions)):
        claims.append(
            ColdClaim(
                region=region,
                day=-(i + 1),  # 负日号：只借“发生日计数”，不与真实日历日冲突
                occurred=True,
                task_id="__scenario__",
                row_index=-1,
            )
        )
    return build_timeline(timeline.facts, claims)


# --------------------------------------------------------------------------- #
# 固定床位图
# --------------------------------------------------------------------------- #
@dataclass
class FixedLayoutResult:
    bed_of: dict[str, int]
    nominal: object  # CommitmentReport
    robust: bool
    per_scenario: dict[str, object] = field(default_factory=dict)
    broken_scenarios: list[str] = field(default_factory=list)

    def commitments(self):
        return self.nominal.commitments


def best_fixed_layout(
    timeline: Timeline,
    pool,
    num_beds: int,
    repo=None,
    *,
    scenarios: list[ColdScenario] | None = None,
    require_robust: bool = True,
) -> FixedLayoutResult:
    """选最优固定床位图并可验证冷天稳健性。

    稳健性口径：对每个情景重算整园花期表，要求承诺**树对与方向**集合不变
    （日期可随冷天平移）。某情景下承诺树对集合若缩小即记为不稳健。
    """
    scenarios = scenarios or [ColdScenario.nominal()]
    pooled = list(pool)

    # 预先算好各情景时间线（与床位无关）
    scen_timelines = {s.label(): timeline_for_scenario(timeline, s) for s in scenarios}

    best: FixedLayoutResult | None = None
    best_key: tuple | None = None
    for bed_of in arrangements(pooled, num_beds):
        nom_report = evaluate_commitments(timeline, bed_of, repo)
        score = _score(nom_report, bed_of)
        if not score[0]:
            continue  # 一株都展不出，跳过
        # 稳健性：情景下同一 tree-pair+方向 必须仍成立
        nominal_pairs = {(c.receiver, c.provider) for c in nom_report.commitments}
        broken: list[str] = []
        per_scenario = {}
        for s in scenarios:
            tl = scen_timelines[s.label()]
            rep = evaluate_commitments(tl, bed_of, repo)
            per_scenario[s.label()] = rep
            if not nominal_pairs.issubset({(c.receiver, c.provider) for c in rep.commitments}):
                broken.append(s.label())
        robust = not broken
        if require_robust and not robust:
            continue
        # 排序：稳健优先、参展树多、承诺多、少浪费床位、床位序做确定性尾键
        key = (
            1 if robust else 0,
            score,
            tuple(-bed_of[t] for t in sorted(bed_of)),
        )
        if best_key is None or key > best_key:
            best_key = key
            best = FixedLayoutResult(
                bed_of=bed_of,
                nominal=nom_report,
                robust=robust,
                per_scenario=per_scenario,
                broken_scenarios=broken,
            )
    if best is None:
        # 放宽稳健性要求再选一次，便于报告“最好的不稳健图”
        return best_fixed_layout(
            timeline, pooled, num_beds, repo, scenarios=scenarios, require_robust=False
        ) if require_robust else _empty_fixed(pooled, num_beds)
    return best


def _empty_fixed(pool, num_beds) -> FixedLayoutResult:
    bed_of = {t: i for i, t in enumerate(list(pool)[:num_beds])}
    from .commitments import CommitmentReport

    return FixedLayoutResult(
        bed_of=bed_of,
        nominal=CommitmentReport([], [], []),
        robust=False,
        per_scenario={},
        broken_scenarios=["无可展出的相邻配置"],
    )


# --------------------------------------------------------------------------- #
# 带轮盆树逐日换位
# --------------------------------------------------------------------------- #
def segments(num_beds: int, blocked: set[frozenset[int]]) -> list[list[int]]:
    """按被阻断的相邻边界把床位切成连通段。"""
    segs: list[list[int]] = []
    cur = [0]
    for i in range(1, num_beds):
        if frozenset((i - 1, i)) in blocked:
            segs.append(cur)
            cur = [i]
        else:
            cur.append(i)
    segs.append(cur)
    return segs


def _segment_of(seg_list: list[list[int]]) -> dict[int, int]:
    return {bed: i for i, seg in enumerate(seg_list) for bed in seg}


@dataclass
class DayPlan:
    day: int
    bed_of: dict[str, int]
    moved: list[str]
    report: object  # CommitmentReport


@dataclass
class WheeledPlan:
    initial_bed_of: dict[str, int]
    days: list[DayPlan]
    feasible: bool
    notes: list[str] = field(default_factory=list)

    def commitments(self) -> list[Commitment]:
        out: list[Commitment] = []
        for d in self.days:
            out.extend(d.report.commitments)
        return out

    def partner_dates(self) -> dict[str, list[tuple[str, int]]]:
        out: dict[str, list[tuple[str, int]]] = {}
        for d in self.days:
            for c in d.report.commitments:
                out.setdefault(c.receiver, []).append((c.provider, c.day))
                out.setdefault(c.provider, []).append((c.receiver, c.day))
        return out


def _potential_pairs(timeline: Timeline, trees) -> list[tuple[str, str]]:
    """日历上“可成为承诺”的树对（有方向正确的花期交集），用于换位爬坡。"""
    from .commitments import diagnose_pair

    out = []
    for i in range(len(trees)):
        for j in range(i + 1, len(trees)):
            if diagnose_pair(timeline, trees[i], trees[j]).has_overlap:
                out.append((trees[i], trees[j]))
    return out


def _proximity_gain(bed_of: dict[str, int], potentials) -> int:
    """潜在可挂牌树对的距离爬坡信号：距离越小越好（相邻=0）。

    取负的总床距，使“还需两步才能相邻”的树对也能每天各挪一步逐步逼近，
    而不是因为当日无法一步到位就原地不动。
    """
    return -sum(abs(bed_of[a] - bed_of[b]) for a, b in potentials)


def _best_target_within_budget(
    timeline: Timeline,
    prev_bed_of: dict[str, int],
    pool,
    num_beds: int,
    day: int,
    blocked: set[frozenset[int]],
    daily_cap: int,
    repo,
) -> tuple[dict[str, int], list[str], object, str | None]:
    """在当日通道段约束与搬运上限内，选一个使承诺最优的目标排列。

    评分梯次：当日承诺数 > 潜在可挂牌对的相邻数（爬坡） > 少搬动。
    后者让“需要两步才能相邻”的跨段树对也能逐步推进，而非原地不动。
    """
    seg_list = segments(num_beds, blocked)
    seg_index = _segment_of(seg_list)
    trees = list(prev_bed_of.keys())
    potentials = _potential_pairs(timeline, trees)

    # 每段内的候选排列（只在该段现有树之间换位；不跨段）
    seg_trees: dict[int, list[str]] = {}
    for tree, bed in prev_bed_of.items():
        seg_trees.setdefault(seg_index[bed], []).append(tree)

    # 对每段枚举其内部树的床位排列
    per_seg_options: list[list[dict[str, int]]] = []
    for si, seg in enumerate(seg_list):
        ts = seg_trees.get(si, [])
        opts: list[dict[str, int]] = []
        for perm in itertools.permutations(seg, len(ts)):
            opts.append(dict(zip(ts, perm)))  # 树 -> 床位（本段内）
        per_seg_options.append(opts)

    best = None
    best_key = None
    note = None
    for combo in itertools.product(*per_seg_options):
        target: dict[str, int] = {}
        for part in combo:
            target.update(part)
        moved = [t for t in trees if target.get(t) != prev_bed_of[t]]
        if len(moved) > daily_cap:
            continue
        rep = evaluate_commitments(
            timeline, target, repo,
            pairs=[(a, b) for a in trees for b in trees if a < b],
        )
        key = (
            len(rep.commitments),
            _proximity_gain(target, potentials),
            len(rep.exhibited_trees()),
            -len(moved),
        )
        if best_key is None or key > best_key:
            best_key = key
            best = (target, moved, rep, None)
    if best is None:
        note = f"第{day}天：通道段与搬运上限（{daily_cap}）下无可行换位"
        rep = evaluate_commitments(timeline, prev_bed_of, repo)
        return prev_bed_of, [], rep, note
    return best


def plan_wheeled(
    timeline: Timeline,
    initial_bed_of: dict[str, int],
    num_beds: int,
    repo=None,
    *,
    days,
    blocked_by_day: dict[int, set[frozenset[int]]] | None = None,
    daily_cap: int = 1,
    cumulative_blocks: bool = True,
) -> WheeledPlan:
    """逐日换邻位：早班看真实花况后，在通道/搬运约束内调整盆树到最优邻位。

    初始排列固定；每天按已开放通道切段，盆只能在段内换位，且当日换床位盆数不超过
    ``daily_cap``。``cumulative_blocks=True``（默认）时“已开放的通道不能再推盆穿过”：
    第 d 天封锁的通道在第 d 天及以后始终封锁；置 False 仅当天封锁。
    """
    blocked_by_day = blocked_by_day or {}
    prev = dict(initial_bed_of)
    day_plans: list[DayPlan] = []
    notes: list[str] = []
    feasible = True
    opened: set[frozenset[int]] = set()
    for day in days:
        opened |= set(blocked_by_day.get(day, set())) if cumulative_blocks else set()
        blocked = opened if cumulative_blocks else set(blocked_by_day.get(day, set()))
        target, moved, rep, note = _best_target_within_budget(
            timeline, prev, list(prev.keys()), num_beds, day, blocked, daily_cap, repo
        )
        if note:
            notes.append(note)
            feasible = False
        day_plans.append(DayPlan(day=day, bed_of=dict(target), moved=moved, report=rep))
        prev = dict(target)
    return WheeledPlan(initial_bed_of=dict(initial_bed_of), days=day_plans, feasible=feasible, notes=notes)
