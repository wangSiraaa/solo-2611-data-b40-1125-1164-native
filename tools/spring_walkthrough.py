#!/usr/bin/env python3
"""按题面场景跑一遍可核查的弹簧装机流程。

这不是额外测试框架；它通过 ``pqscan.runner.run`` 先产出
``output/<case>/results/spring_batch.parquet``，再从该真实 Parquet 受理装机单。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from pqscan.runner import run  # noqa: E402
from pqscan.springdesk import build_curves, plan_cam_order  # noqa: E402

FILES = os.path.join(ROOT, "fixtures", "spring_files.json")
OUT_ROOT = os.path.join(ROOT, "output")

QUERY = {
    "id": "spring_batch",
    "where": {"ge": ["displacement", 0]},
    "columns": [
        "spring_id", "trial_id", "direction", "force_level", "displacement",
    ],
}


def run_case(name: str, inspection: dict):
    out = os.path.join(OUT_ROOT, name)
    os.makedirs(out, exist_ok=True)
    queries = {"queries": [QUERY], "spring_inspection": inspection}
    qpath = os.path.join(out, "queries.json")
    with open(qpath, "w", encoding="utf-8") as fh:
        json.dump(queries, fh, ensure_ascii=False, indent=2)
    doc = run(FILES, qpath, out)
    return out, doc["spring_inspection"]


def main() -> int:
    # 1) 甲/乙：只看下压会误判，完整往返必须拒装。
    _, base = run_case("spring-ab-base", {
        "result": "spring_batch",
        "fresh_state": True,
        "orders": [{
            "order_id": "AB-BASE",
            "trial_id": "T-AB",
            "left_spring_id": "JIA",
            "right_spring_id": "YI",
            "force_sequence": ["middle"],
            "minimum_lift": 0,
            "tolerance": 1,
        }],
    })
    assert base["orders"][0]["accepted"] is False

    # 2) 台账依据：乙回程读数整体偏高 1 格，只改回程后改判接受。
    _, corrected_return = run_case("spring-ab-correct-return", {
        "result": "spring_batch",
        "fresh_state": True,
        "observations": [{
            "observation_id": "OBS-RETURN-01",
            "trial_id": "T-AB",
            "spring_id": "YI",
            "scope": "return",
            "zero_shift": 1,
            "evidence": {"start_reading": 1, "ledger_supplement": "回程出发点补测"},
        }],
        "orders": [{
            "order_id": "AB-RETURN-CORRECTED",
            "trial_id": "T-AB",
            "left_spring_id": "JIA",
            "right_spring_id": "YI",
            "force_sequence": ["middle"],
            "minimum_lift": 0,
            "tolerance": 1,
        }],
    })
    assert corrected_return["orders"][0]["accepted"] is True

    # 3) 直接配簧：第一台占用 C，重开台账后第二台只剩 D，拒装。
    _, direct = run_case("spring-two-dies-direct", {
        "result": "spring_batch",
        "state_path": os.path.join(OUT_ROOT, "spring-direct-desk.json"),
        "fresh_state": True,
        "springs": [
            {"spring_id": "L1", "status": "fixed"},
            {"spring_id": "L2", "status": "fixed"},
            "C", "D",
        ],
        "reopen_between_orders": True,
        "orders": [
            {
                "order_id": "DIE-1-DIRECT",
                "trial_id": "T-PRESS",
                "mode": "direct",
                "left_spring_id": "L1",
                "force_sequence": ["middle"],
                "minimum_lift": 2,
                "tolerance": 0,
            },
            {
                "order_id": "DIE-2-DIRECT",
                "trial_id": "T-PRESS",
                "mode": "direct",
                "left_spring_id": "L2",
                "force_sequence": ["middle"],
                "minimum_lift": 2,
                "tolerance": 0,
            },
        ],
    })
    assert [o["accepted"] for o in direct["orders"]] == [True, False]

    # 4) 凸轮柜制：首台指定 L1+D，第 1 空拍抬左；次台 L2+C 本身合格。
    _, cam = run_case("spring-two-dies-cam", {
        "result": "spring_batch",
        "state_path": os.path.join(OUT_ROOT, "spring-cam-desk.json"),
        "fresh_state": True,
        "springs": ["L1", "L2", "C", "D"],
        "reopen_between_orders": True,
        "orders": [
            {
                "order_id": "DIE-1-CAM",
                "machine_id": "CAM-PRESS-1",
                "trial_id": "T-PRESS",
                "mode": "cam",
                "cam_capable": True,
                "left_spring_id": "L1",
                "right_spring_id": "D",
                "force_sequence": ["middle"],
                "minimum_lift": 2,
                "tolerance": 0,
                "empty_beats": [1],
                "unlock_beats": [3],
            },
            {
                "order_id": "DIE-2-CAM",
                "machine_id": "CAM-PRESS-2",
                "trial_id": "T-PRESS",
                "mode": "cam",
                "cam_capable": True,
                "left_spring_id": "L2",
                "right_spring_id": "C",
                "force_sequence": ["middle"],
                "minimum_lift": 2,
                "tolerance": 0,
            },
        ],
    })
    assert [o["accepted"] for o in cam["orders"]] == [True, True]

    press_curves = build_curves_from_fixture()
    blocked = plan_cam_order({
        "order_id": "NEXT-EQUAL-START-NO-RESET",
        "mode": "cam", "cam_capable": True,
        "left_spring_id": "L2", "right_spring_id": "C",
        "force_sequence": ["middle"], "minimum_lift": 2, "tolerance": 0,
        "equal_start_required": True,
    }, press_curves,
       inherited_lock={"side": "left", "delta": 1, "next_unlock_beat": None})
    assert blocked["accepted"] is False

    print("AB base       :", base["orders"][0]["status"], base["orders"][0]["summary"])
    print("AB corrected  :", corrected_return["orders"][0]["status"],
          corrected_return["orders"][0]["summary"])
    for order in direct["orders"]:
        print("direct        :", order["order_id"], order["status"], order["summary"])
    for order in cam["orders"]:
        lock = (order.get("cam") or {}).get("actual_locked_side")
        unlock = (order.get("cam") or {}).get("next_unlock_beat")
        print("cam           :", order["order_id"], order["status"],
              f"lock={lock} next_unlock={unlock}", order["summary"])
    print("next cycle    :", blocked["status"], blocked["summary"])
    print("\nReports under output/spring-*/spring_inspection.json")
    return 0


def build_curves_from_fixture():
    import pyarrow.parquet as pq
    path = os.path.join(ROOT, "fixtures", "spring_ledger.parquet")
    return build_curves(pq.read_table(path).to_pylist(), trial_id="T-PRESS")


if __name__ == "__main__":
    raise SystemExit(main())
