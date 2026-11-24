"""展演排轮测试：鼓面算术、履历分支、额度边界、两条路线与四类单点突变。

运行：PYTHONPATH=src .venv/bin/python -m pytest tests/test_wheels.py -q
"""
from __future__ import annotations

import pytest

from pqscan.errors import WheelsError
from pqscan.wheels.drum import advance, feasible_ticks, rollover_options, rollovers
from pqscan.wheels.history import reconstruct
from pqscan.wheels.quota import build_quota
from pqscan.wheels.replay import (
    mutate_lift,
    mutate_reading,
    mutate_spare,
    mutate_tag,
    run_replay,
)
from pqscan.wheels.runner import load_scenario, run_wheels
from pqscan.wheels.schedule import Program, plan_weekend

FIXTURE_DIR = "fixtures/wheels"


# ---------------------------------------------------------------------------
# 鼓面模 10 算术
# ---------------------------------------------------------------------------

def test_advance_wraps_at_ten():
    assert advance(8, 3) == 1          # 题面：8 走 3 格到 1
    assert advance(9, 1) == 0
    assert advance(0, 20) == 0
    with pytest.raises(WheelsError):
        advance(10, 1)
    with pytest.raises(WheelsError):
        advance(1, -1)


def test_rollovers_match_whole_trip():
    assert rollovers(7, 9, 12) == 1    # 7+12=19，跨一次零
    assert rollovers(7, 7, 10) == 1
    assert rollovers(8, 1, 3) == 0
    with pytest.raises(WheelsError):
        rollovers(8, 1, 2)            # 8 走 2 格只能到 0


def test_feasible_ticks_uses_remainder_class_and_range():
    # 8->1 余数 3，区间 [2,4] 内唯一解 3
    assert feasible_ticks(8, 1, 2, 4) == [3]
    assert rollover_options(8, 1, 2, 4) == [0]
    # 7->7 余数 0，区间 [0,10] 给出 0 与 10 两个可行解，跨零次数不同
    assert feasible_ticks(7, 7, 0, 10) == [0, 10]
    assert rollover_options(7, 7, 0, 10) == [0, 1]
    # 余数类与区间不相交 -> 履历矛盾
    assert feasible_ticks(8, 5, 2, 4) == []
    # 宽区间可以容纳多个跨零解：9->8 余数 9
    assert feasible_ticks(9, 8, 0, 30) == [9, 19, 29]
    assert rollover_options(9, 8, 0, 30) == [0, 1, 2]


# ---------------------------------------------------------------------------
# 履历复原：模糊签条的互斥分支与物理排斥
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def baseline():
    return load_scenario(FIXTURE_DIR)


def test_reconstruct_keeps_exclusive_branches(baseline):
    branches = reconstruct(baseline.observations, baseline.itinerary, baseline.tag_candidates)
    assert {b.name for b in branches} == {"T->W1", "T->W2"}
    by_name = {b.name: b for b in branches}
    rejected = {name: b for name, b in by_name.items() if not b.feasible}
    accepted = {name: b for name, b in by_name.items() if b.feasible}
    assert set(rejected) == {"T->W1"}
    assert "同时挂在车 C-A 与车 C-B" in rejected["T->W1"].exclusion_reasons[0]
    assert set(accepted) == {"T->W2"}
    # 被排斥的分支仍保留段轨迹（互斥可能性不丢弃）
    assert rejected["T->W1"].segments


def test_segment_consumption_and_rollover_trace(baseline):
    branches = reconstruct(baseline.observations, baseline.itinerary, baseline.tag_candidates)
    good = next(b for b in branches if b.feasible)
    s1 = next(s for s in good.segments if s.seg_id == "S1")
    assert (s1.before, s1.after, s1.feasible_ticks, s1.rollover_options) == (8, 1, [3], [0])
    s4 = next(s for s in good.segments if s.seg_id == "S4")
    assert s4.ambiguous_rollover and s4.feasible_ticks == [0, 10]
    s5 = next(s for s in good.segments if s.seg_id == "S5")
    assert s5.feasible_ticks == [12] and s5.rollover_options == [1]


def test_contradictory_segment_raises(baseline):
    broken = mutate_reading(baseline, "C-A", 12, drum=5)
    with pytest.raises(WheelsError, match="履历矛盾"):
        reconstruct(broken.observations, broken.itinerary, broken.tag_candidates)


# ---------------------------------------------------------------------------
# 额度：恰好到限可收车、超一格即超期
# ---------------------------------------------------------------------------

def test_quota_boundary_equality_allowed(baseline):
    branches = reconstruct(baseline.observations, baseline.itinerary, baseline.tag_candidates)
    quota = build_quota(branches, baseline.limits, baseline.spare_axles)
    w1 = quota["W1"]
    # S0=7 + S2=9 + S1=3 = 19；limit=21 -> 上场前剩 5、S1 后剩 2
    assert (w1.consumed_min, w1.consumed_max, w1.headroom_min) == (19, 19, 2)
    assert w1.can_cover(2) and not w1.can_cover(3)       # 再走 2 格恰好到限，3 格超期
    # 题面：上场前还可走 5 格，S1 实走 3 格后再走 2 可排、3 超期
    pre_s1 = w1.limit - (7 + 9)
    assert pre_s1 == 5
    assert (7 + 9) + 3 + 2 == w1.limit
    assert (7 + 9) + 3 + 3 > w1.limit
    w2 = quota["W2"]
    assert (w2.consumed_min, w2.consumed_max) == (16, 26)  # S4 的 0/10 二义
    assert w2.headroom_min == 4 and w2.headroom_max == 14
    assert quota["W3"].consumed_max == 0                 # 纯库房备轮


# ---------------------------------------------------------------------------
# 两条路线
# ---------------------------------------------------------------------------

def test_baseline_both_routes_feasible(baseline):
    branches = reconstruct(baseline.observations, baseline.itinerary, baseline.tag_candidates)
    quota = build_quota(branches, baseline.limits, baseline.spare_axles)
    for route in ("sealed", "shared"):
        plan = plan_weekend(baseline.programs, quota, baseline.initial_mount, route)
        assert plan.feasible
        assert [a.axle for a in plan.assignments] == ["W1", "W2", "W3"]
    sealed, shared = (
        plan_weekend(baseline.programs, quota, baseline.initial_mount, r) for r in ("sealed", "shared")
    )
    # 封存路线 P3 不算举升台换轮；共用路线 P3 必须用举升台
    assert sealed.assignments[2].lift_used is False
    assert shared.assignments[2].lift_used is True


def test_sealed_requires_distinct_axles(baseline):
    branches = reconstruct(baseline.observations, baseline.itinerary, baseline.tag_candidates)
    quota = build_quota(branches, baseline.limits, baseline.spare_axles)
    plan = plan_weekend(baseline.programs[:2], quota, baseline.initial_mount, "sealed")
    assert plan.feasible and len({a.axle for a in plan.assignments}) == 2
    # 再排一个需要独立轴对的节目，但只剩 W1/W2 已封存
    extra = baseline.programs + [Program("PX", "C-A", "ST-主线", False, 1, False)]
    plan = plan_weekend(extra, quota, baseline.initial_mount, "sealed")
    assert not plan.feasible
    assert "没有可借出的轴对" in plan.assignments[-1].reject_reasons[0]


def test_shared_no_lift_keeps_current_axle(baseline):
    branches = reconstruct(baseline.observations, baseline.itinerary, baseline.tag_candidates)
    quota = build_quota(branches, baseline.limits, baseline.spare_axles)
    # ST-主线 无举升台：C-A 只能用现挂 W1，即便 W3 有更多额度也不许换
    progs = [Program("P1", "C-A", "ST-主线", False, 1, False)]
    plan = plan_weekend(progs, quota, baseline.initial_mount, "shared")
    assert plan.feasible and plan.assignments[0].axle == "W1"
    assert plan.assignments[0].swapped is False


def test_three_ticks_overruns_when_two_reach_limit(baseline):
    """题面边界：W1 只剩 2 格时，无举升台展台走 3 格必须被拒（等号合法、超一格不行）。"""
    branches = reconstruct(baseline.observations, baseline.itinerary, baseline.tag_candidates)
    quota = build_quota(branches, baseline.limits, baseline.spare_axles)
    progs = [Program("P1", "C-A", "ST-主线", False, 3, False)]
    plan = plan_weekend(progs, quota, baseline.initial_mount, "shared")
    assert not plan.feasible
    reason = plan.assignments[0].reject_reasons[0]
    assert "剩余 2 格 < 本段 3 格" in reason


def test_departed_train_locks_its_axle(baseline):
    branches = reconstruct(baseline.observations, baseline.itinerary, baseline.tag_candidates)
    quota = build_quota(branches, baseline.limits, baseline.spare_axles)
    # P2 散场前出发锁死 W2；无举升台的后续节目想沿用 W2 必须被拒
    progs = [
        Program("P2", "C-B", "ST-侧线", False, 4, True),
        Program("P3", "C-B", "ST-侧线", False, 1, False),
    ]
    plan = plan_weekend(progs, quota, baseline.initial_mount, "shared")
    assert not plan.feasible
    assert "锁死" in plan.assignments[-1].reject_reasons[0]


# ---------------------------------------------------------------------------
# 四类单点突变：每类只改一处，都让原节目单不可排
# ---------------------------------------------------------------------------

def test_four_single_mutations_each_break_schedule(baseline):
    scenarios = [
        mutate_reading(baseline, "C-A", 12, drum=5),
        mutate_tag(baseline, "T", "W2"),
        mutate_spare(baseline, "W3"),
        mutate_lift(baseline, "ST-举升台"),
    ]
    results = {r.mutation: r for r in run_replay(scenarios)}

    # 读数突变：履历直接矛盾，两条路线都无法排
    assert results["mutate_reading"].history_error
    # 签条突变：唯一可行分支被删，build_quota 阶段报错
    assert results["mutate_tag"].history_error
    # 备轮突变：两条路线都缺第三套轮
    spare = results["mutate_spare"]
    assert all(not p["feasible"] for p in spare.plans)
    # 举升台突变：只打共用路线，封存路线仍可排
    lift = results["mutate_lift"]
    by_route = {p["route"]: p for p in lift.plans}
    assert by_route["sealed"]["feasible"] is True
    assert by_route["shared"]["feasible"] is False
    assert "无举升台" in by_route["shared"]["assignments"][-1]["reject_reasons"][0]


def test_runner_writes_handover_and_replay(tmp_path):
    doc = run_wheels(FIXTURE_DIR, str(tmp_path))
    assert all(p["feasible"] for p in doc["handover"]["plans"])
    baseline_res = doc["replay"]["results"][0]
    assert baseline_res["mutation"] == "baseline"
    assert all(p["feasible"] for p in baseline_res["plans"])
    for name in ("wheel_branches.json", "wheel_quota.json", "handover.json", "replay.json"):
        assert (tmp_path / name).exists()
