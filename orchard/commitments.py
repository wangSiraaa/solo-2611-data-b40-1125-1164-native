"""承诺判定：方向正确的花期交集 + 床位距离 + 自己不作伴，并给出缺日/缺边的诊断。

规则（严格按题面）
==================
* 两株必须在**同一天**有方向正确的花期交集：受体当天开花且可受粉，供体当天开花且可供粉；
  即 ``d ∈ bloom[受] ∩ bloom[供] ∩ receive[受] ∩ provide[供]``；
* 相距至多一个床位：``|bed[甲] - bed[乙]| == 1``（隔一空床 |Δ|=2 即失配，自己不作伴；
  “邻树可同时照应左右两株” => 一株可同时与左右两株各成立承诺，不要求一对一配对）；
* 证据冲突时指出**缺的是哪一天哪一边**：``MissingSide(day, provider, receiver, missing)``，
  ``missing`` 是当天缺失的必要条件（供体开花/受体开花/供体可供/受体可受）。

一个方向若某天四项齐备即为一条 :class:`Commitment`（带支撑记录）；若某天有“意图”
（至少供/受其一生效）却四项不齐，则记 :class:`MissingSide`，用于解释为何失约。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import DAYS
from .ingest import RecordRef, locate_record
from .observation import BLOOM, PROVIDE, RECEIVE, Timeline

# 必要条件标签（缺失边的可读名）
NEED_PROVIDER_BLOOM = "provider_bloom"
NEED_RECEIVER_BLOOM = "receiver_bloom"
NEED_PROVIDE = "provide"
NEED_RECEIVE = "receive"

MAX_BED_GAP = 1  # 相距至多一个床位


@dataclass(frozen=True)
class Overlap:
    """一次方向正确的花期交集（某天供体向受体供粉）。"""

    day: int
    provider: str
    receiver: str


@dataclass(frozen=True)
class MissingSide:
    """某方向某天缺的条件（缺的是哪一天哪一边）。"""

    day: int
    provider: str
    receiver: str
    missing: tuple[str, ...]

    def describe(self) -> str:
        labels = {
            NEED_PROVIDER_BLOOM: f"{self.provider}当天开花",
            NEED_RECEIVER_BLOOM: f"{self.receiver}当天开花",
            NEED_PROVIDE: f"{self.provider}可供粉",
            NEED_RECEIVE: f"{self.receiver}可受粉",
        }
        return f"第{self.day}天缺 " + "、".join(labels[m] for m in self.missing)


@dataclass(frozen=True)
class Commitment:
    """一条可挂牌的承诺：受体、供体、日期与支撑记录。

    ``day`` 是整园花期表（已应用冷天平移）中的日期；``support`` 中每条记录的
    ``day`` 是其**原始记录日**（冷天前），两者可能相差片区延迟天数。
    """

    day: int
    provider: str
    receiver: str
    bed_gap: int
    support: tuple[RecordRef, ...]

    @property
    def partner_of(self) -> dict[str, str]:
        return {self.receiver: self.provider, self.provider: self.receiver}


@dataclass(frozen=True)
class PairDiagnosis:
    """一对树的方向/日历诊断结果（与床位无关）。"""

    a: str
    b: str
    overlaps: tuple[Overlap, ...]
    missing_sides: tuple[MissingSide, ...]

    @property
    def has_overlap(self) -> bool:
        return bool(self.overlaps)


def _conditions(timeline: Timeline, provider: str, receiver: str, day: int) -> dict[str, bool]:
    return {
        NEED_PROVIDER_BLOOM: day in timeline.bloom[provider],
        NEED_RECEIVER_BLOOM: day in timeline.bloom[receiver],
        NEED_PROVIDE: day in timeline.provide[provider],
        NEED_RECEIVE: day in timeline.receive[receiver],
    }


def _day_domain(timeline: Timeline, a: str, b: str) -> list[int]:
    days: set[int] = set(DAYS)
    for tree in (a, b):
        days |= timeline.bloom[tree]
        days |= timeline.provide[tree]
        days |= timeline.receive[tree]
    return sorted(days)


def _direction(
    timeline: Timeline, provider: str, receiver: str, days: list[int]
) -> tuple[list[Overlap], list[MissingSide]]:
    overlaps: list[Overlap] = []
    gaps: list[MissingSide] = []
    for day in days:
        cond = _conditions(timeline, provider, receiver, day)
        n_true = sum(cond.values())
        if n_true == len(cond):
            overlaps.append(Overlap(day, provider, receiver))
            continue
        # 只把“差一项”的日子记为缺口（如甲乙只差乙当天供粉），
        # 避免把无关的相邻树对报成缺口；这正是“失约”诊断的口径。
        if n_true >= len(cond) - 1:
            missing = tuple(k for k, ok in cond.items() if not ok)
            gaps.append(MissingSide(day, provider, receiver, missing))
    return overlaps, gaps


def diagnose_pair(timeline: Timeline, a: str, b: str) -> PairDiagnosis:
    """对一对树做双向诊断，汇总有效交集与缺边证据。"""
    if a == b:
        raise ValueError("自己不能给自己作伴")
    days = _day_domain(timeline, a, b)
    overlaps: list[Overlap] = []
    gaps: list[MissingSide] = []
    for provider, receiver in ((a, b), (b, a)):
        ov, gp = _direction(timeline, provider, receiver, days)
        overlaps.extend(ov)
        gaps.extend(gp)
    return PairDiagnosis(a, b, tuple(overlaps), tuple(gaps))


def bed_gap(bed_of: dict[str, int], a: str, b: str) -> int:
    return abs(bed_of[a] - bed_of[b])


def _support_refs(
    repo, timeline: Timeline, provider: str, receiver: str, day: int
) -> tuple[RecordRef, ...]:
    """收集支撑一条承诺的补记：受体受粉行、供体供粉行、两者开花行。"""
    refs: list[RecordRef] = []
    plan = [
        (RECEIVE, receiver),
        (PROVIDE, provider),
        (BLOOM, receiver),
        (BLOOM, provider),
    ]
    for concern, tree in plan:
        row = timeline.row_of(concern, tree, day)
        if row is None:
            continue
        refs.append(
            locate_record(
                repo,
                timeline.facts.tasks[concern],
                row,
                _target_column(concern),
                concern=concern,
            )
        )
    return tuple(refs)


def _target_column(concern: str) -> str:
    # 事实列名即 concern（bloom/provide/receive）；无关列不存在时由 lineage 检查兜底
    return concern


@dataclass
class CommitmentReport:
    """整园承诺结果：可挂牌承诺 + 日历缺口 + 距离失配。"""

    commitments: list[Commitment]
    calendar_gaps: list[PairDiagnosis]      # 有意图交集但床位内却无有效日（日历缺口）
    distance_gaps: list[PairDiagnosis]      # 日历有效但床位太远（布局失配）

    def partner_dates(self) -> dict[str, list[tuple[str, int]]]:
        """每株的伙伴与日期（可供展签使用）。"""
        out: dict[str, list[tuple[str, int]]] = {}
        for c in self.commitments:
            out.setdefault(c.receiver, []).append((c.provider, c.day))
            out.setdefault(c.provider, []).append((c.receiver, c.day))
        return out

    def exhibited_trees(self) -> list[str]:
        return sorted(self.partner_dates())

    def partner_of(self, tree: str) -> list[str]:
        return [p for p, _ in self.partner_dates().get(tree, [])]


def evaluate_commitments(
    timeline: Timeline,
    bed_of: dict[str, int],
    repo=None,
    *,
    pairs: list[tuple[str, str]] | None = None,
    max_bed_gap: int = MAX_BED_GAP,
) -> CommitmentReport:
    """在给定床位分配下评估承诺。

    仅对**相邻床位**（|Δ|≤max_bed_gap 且不同位）的树对判定可挂牌；
    日历有效但距离过远的树对进入 ``distance_gaps``；相邻但无有效日的进入 ``calendar_gaps``。
    若 ``pairs`` 给出候选人字对，则只看这些对，否则枚举全部树对。
    """
    trees = list(bed_of.keys())
    if pairs is None:
        pairs = [(trees[i], trees[j]) for i in range(len(trees)) for j in range(i + 1, len(trees))]

    commitments: list[Commitment] = []
    calendar_gaps: list[PairDiagnosis] = []
    distance_gaps: list[PairDiagnosis] = []

    for a, b in pairs:
        diag = diagnose_pair(timeline, a, b)
        adjacent = bed_gap(bed_of, a, b) <= max_bed_gap and bed_of[a] != bed_of[b]
        if diag.has_overlap:
            if adjacent:
                for ov in diag.overlaps:
                    support: tuple[RecordRef, ...] = ()
                    if repo is not None:
                        support = _support_refs(repo, timeline, ov.provider, ov.receiver, ov.day)
                    commitments.append(
                        Commitment(
                            day=ov.day,
                            provider=ov.provider,
                            receiver=ov.receiver,
                            bed_gap=bed_gap(bed_of, a, b),
                            support=support,
                        )
                    )
            else:
                distance_gaps.append(diag)
        else:
            if adjacent:
                calendar_gaps.append(diag)

    return CommitmentReport(commitments, calendar_gaps, distance_gaps)
