"""展演排轮入口：从扁平 Parquet + JSON 装载，跑出交接清单与四类单点突变回放。

输出（out_dir 下）：
- wheel_branches.json：每个模糊签条分支的段轨迹、可行格数/跨零次数、排斥原因；
- wheel_quota.json   ：轴对 -> 已耗区间与剩余额度（值班员可从轴对指回车身段）；
- handover.json      ：两条路线的节目单（到哪个展台用哪套轮 / 拒绝哪段行程）；
- replay.json        ：基线与四类单点突变各自的可排性对照。
"""
from __future__ import annotations

import json
import os

import pyarrow.parquet as pq

from .history import ItinerarySegment, Observation, reconstruct
from .quota import build_quota
from .replay import (
    Scenario,
    mutate_lift,
    mutate_reading,
    mutate_spare,
    mutate_tag,
    run_replay,
)
from .schedule import Program, plan_weekend


def _load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_scenario(fixture_dir: str) -> Scenario:
    rows = pq.read_table(os.path.join(fixture_dir, "observations.parquet")).to_pylist()
    observations = [
        Observation(
            car=r["car"], ts=r["ts"], drum=r["drum"],
            axle=r["axle"], tag=r["swap_tag"], seg_id=r["seg_id"],
        )
        for r in rows
    ]
    itinerary = [
        ItinerarySegment(seg_id=s["seg_id"], car=s["car"], lo=s["lo"], hi=s["hi"])
        for s in _load_json(os.path.join(fixture_dir, "itinerary.json"))
    ]
    tag_candidates = _load_json(os.path.join(fixture_dir, "tags.json"))
    depot = _load_json(os.path.join(fixture_dir, "depot.json"))
    programs = [
        Program(
            program_id=p["program_id"], car=p["car"], stage=p["stage"],
            has_lift=p["has_lift"], ticks=p["ticks"],
            departs_before_close=p.get("departs_before_close", False),
        )
        for p in _load_json(os.path.join(fixture_dir, "programs.json"))
    ]
    return Scenario(
        name="baseline",
        observations=observations,
        itinerary=itinerary,
        tag_candidates=tag_candidates,
        limits=dict(depot["overhaul_limits"]),
        programs=programs,
        initial_mount=dict(depot["initial_mount"]),
        spare_axles=list(depot.get("spare_axles", [])),
    )


def run_wheels(fixture_dir: str, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    base = load_scenario(fixture_dir)

    branches = reconstruct(base.observations, base.itinerary, base.tag_candidates)
    quota = build_quota(branches, base.limits, base.spare_axles)

    branches_doc = {
        "rule": "同段可行格数必须与首尾鼓面模 10 余数相符且落在行程表区间内；"
                "跨零次数 = 实际格数 // 10；模糊签条的候选分支互斥并各自校验",
        "branches": [b.to_dict() for b in branches],
    }
    quota_doc = {
        "rule": "已耗区间取全部可行分支内段可行格数的 min/max；"
                "判定口径 历史最大已耗 + 本场使用 <= 翻修期限（恰好到限可收车）",
        "axles": [q.to_dict() for q in sorted(quota.values(), key=lambda q: q.axle)],
    }
    plans = [
        plan_weekend(base.programs, quota, base.initial_mount, route)
        for route in ("sealed", "shared")
    ]
    handover = {
        "rule": "sealed=开馆前每节目封存独立轴对、期间不借出；"
                "shared=只在带举升台展台换轮，散场前出发列车的轴对随车锁死、叫不回来",
        "plans": [p.to_dict() for p in plans],
    }

    # 四类单点突变（每类只改一处），目标都是让基线可排的节目单变成不可排。
    scenarios = [
        base,
        mutate_reading(base, "C-A", 12, drum=5),     # S1：8->5 余数 7，与区间 [2,4] 无交集
        mutate_tag(base, "T", "W2"),                  # 删掉唯一可行分支（W1 分支本就物理排斥）
        mutate_spare(base, "W3"),                     # 封存路线凑不齐第三套独立轴对
        mutate_lift(base, "ST-举升台"),               # 共用路线 P3 无法换轮
    ]
    replay_doc = {
        "rule": "每个场景独立重跑履历/额度/两条路线；feasible 翻成 false 即原节目不可排",
        "results": [r.to_dict() for r in run_replay(scenarios)],
    }

    for name, doc in (
        ("wheel_branches.json", branches_doc),
        ("wheel_quota.json", quota_doc),
        ("handover.json", handover),
        ("replay.json", replay_doc),
    ):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)

    return {"handover": handover, "replay": replay_doc,
            "branches": branches_doc, "quota": quota_doc}
