"""弹簧全程曲线、实簧占用、凸轮补偿与 runner/Parquet 集成测试。"""

from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq

from pqscan.runner import run
from pqscan.springdesk import (
    InstallationDesk,
    ZeroCorrection,
    build_curves,
    plan_cam_order,
    plan_direct_order,
)


def ledger_rows(spec, trial_id="T-1"):
    """spec: {spring_id: (outbound, return)}。"""
    rows = []
    for sid, (outbound, returning) in spec.items():
        rows.append({
            "spring_id": sid, "trial_id": trial_id,
            "direction": "去程", "force_level": "中档",
            "displacement": outbound,
        })
        rows.append({
            "spring_id": sid, "trial_id": trial_id,
            "direction": "回程", "force_level": "中档",
            "displacement": returning,
        })
    return rows


def requirement(**overrides):
    req = {
        "order_id": "die",
        "force_sequence": ["中档"],
        "minimum_lift": 0,
        "tolerance": 1,
    }
    req.update(overrides)
    return req


def test_loading_alignment_is_not_enough_for_direct_pair():
    curves = build_curves(ledger_rows({"甲": (2, 2), "乙": (2, 4)}))

    # 完整轮次必须拒装。
    verdict = plan_direct_order(
        requirement(), curves, available_spring_ids=["甲", "乙"]
    )
    assert verdict["accepted"] is False
    assert any("回程/卸载" in reason and "2" in reason
               for reason in verdict["rejection_reasons"])

    # 同一中档去程确实齐平，证明拒装来自回程而不是别的步骤。
    from pqscan.springdesk import evaluate_pair, normalize_requirement
    loading_req = normalize_requirement(requirement())
    loading_req["steps"][0]["directions"] = ("outbound",)
    assert evaluate_pair("甲", "乙", curves, loading_req)["passed"] is True


def test_ledger_backed_return_or_whole_group_correction_reverses_verdict():
    original = ledger_rows({"甲": (2, 2), "乙": (2, 4)})

    for scope in ("return", "whole_cycle"):
        correction = {
            "trial_id": "T-1",
            "spring_id": "乙",
            "scope": scope,
            "zero_shift": 1,
            "observation_id": f"obs-{scope}",
            "evidence": {"start_reading": 1, "source": "补录单-01"},
        }
        curves = build_curves(original, [correction])
        order = plan_direct_order(
            requirement(order_id=f"corrected-{scope}"),
            curves,
            available_spring_ids=["甲", "乙"],
        )
        assert order["accepted"] is True
        return_check = next(c for c in order["checks"] if c["direction"] == "return")
        assert return_check["max_height_difference"] <= 1

    # 恢复原台账、不带补录后，同一单必须恢复拒装。
    restored = build_curves(original)
    assert plan_direct_order(
        requirement(order_id="restored"), restored,
        available_spring_ids=["甲", "乙"],
    )["accepted"] is False


def test_zero_correction_cannot_be_selected_per_measurement_point():
    import pytest
    from pqscan.springdesk import OffsetEvidenceError

    with pytest.raises(OffsetEvidenceError):
        ZeroCorrection.from_dict({
            "trial_id": "T-1",
            "scope": "return",
            "force_level": "中档",  # 试图只改一个测点
            "delta": -1,
            "observation_id": "bad",
        })


def test_direct_counter_persists_c_occupancy_across_reopen(tmp_path):
    state = tmp_path / "desk.json"
    rows = ledger_rows({
        "L1": (2, 2), "L2": (2, 2), "C": (2, 2), "D": (2, 3),
    })
    desk = InstallationDesk(state)
    desk.receive_springs(["L1", "L2", "C", "D"])

    first = desk.submit_order(
        requirement(order_id="press-1", left_spring_id="L1",
                    tolerance=0, minimum_lift=2, mode="direct"),
        rows,
    )
    assert first["accepted"] is True
    assert first["pair"] == {"left_spring_id": "L1", "right_spring_id": "C"}
    assert desk.springs["C"]["status"] == "occupied"
    assert desk.springs["C"]["held_by"] == "press-1"

    desk.close()
    reopened = InstallationDesk.reopen(state)
    assert reopened.available_spring_ids() == ["D", "L2"]

    second = reopened.submit_order(
        requirement(order_id="press-2", left_spring_id="L2",
                    tolerance=0, minimum_lift=2, mode="direct"),
        rows,
    )
    assert second["accepted"] is False
    assert any("高差 1" in r for r in second["rejection_reasons"])
    # 拒装不占用 D。
    assert reopened.springs["D"]["status"] == "available"


def test_withdrawing_only_borrowable_spring_rejects_same_order(tmp_path):
    state = tmp_path / "desk.json"
    rows = ledger_rows({"L1": (2, 2), "C": (2, 2), "D": (2, 3)})
    desk = InstallationDesk(state)
    desk.receive_springs(["L1", "C", "D"])
    assert desk.submit_order(
        requirement(order_id="with-c", left_spring_id="L1",
                    tolerance=0, minimum_lift=2),
        rows,
    )["accepted"] is True

    desk2 = InstallationDesk(tmp_path / "desk2.json")
    desk2.receive_springs(["L1", "D"])  # 唯一可配 C 已撤库/被占用
    order = desk2.submit_order(
        requirement(order_id="without-c", left_spring_id="L1",
                    tolerance=0, minimum_lift=2),
        rows,
    )
    assert order["accepted"] is False


def test_cam_can_raise_low_side_for_jia_yi_return_gap():
    curves = build_curves(ledger_rows({"甲": (2, 2), "乙": (2, 4)}))
    accepted = plan_cam_order(requirement(
        order_id="jia-yi-cam", mode="cam", cam_capable=True,
        left_spring_id="甲", right_spring_id="乙",
        tolerance=1, minimum_lift=0,
        empty_beats=["transition"], unlock_beats=[3],
    ), curves)
    assert accepted["accepted"] is True
    assert accepted["cam"]["actual_locked_side"] == "left"
    return_check = next(c for c in accepted["checks"] if c["direction"] == "return")
    assert return_check["left_values"] == [3.0]
    assert return_check["right_values"] == [4.0]
    assert return_check["passed"] is True


def test_cam_plan_raises_actual_left_side_and_hands_off_unlock_beat():
    curves = build_curves(ledger_rows({
        "L1": (2, 2), "D": (2, 3), "L2": (2, 2), "C": (2, 2),
    }))
    first = plan_cam_order(requirement(
        order_id="cam-1", machine_id="M1", mode="cam", cam_capable=True,
        left_spring_id="L1", right_spring_id="D",
        tolerance=0, minimum_lift=2,
        empty_beats=[1], unlock_beats=[3],
    ), curves)
    assert first["accepted"] is True
    cam = first["cam"]
    assert cam["actual_locked_side"] == "left"
    assert cam["locked_at_beat"] == 1
    assert cam["next_unlock_beat"] == 3
    assert cam["actions"][0]["side"] == "left"

    second = plan_cam_order(requirement(
        order_id="cam-2", mode="cam", cam_capable=True,
        left_spring_id="L2", right_spring_id="C",
        tolerance=0, minimum_lift=2,
    ), curves)
    assert second["accepted"] is True
    assert second["cam"]["actions"] == []

    no_beat = plan_cam_order(requirement(
        order_id="cam-no-beat", mode="cam", cam_capable=True,
        left_spring_id="L1", right_spring_id="D",
        tolerance=0, minimum_lift=2, empty_beats=[],
    ), curves)
    assert no_beat["accepted"] is False


def test_removing_designated_empty_beat_rejects_cam_order():
    curves = build_curves(ledger_rows({"L": (2, 2), "D": (2, 3)}))
    req = requirement(
        mode="cam", cam_capable=True,
        left_spring_id="L", right_spring_id="D",
        tolerance=0, minimum_lift=2,
    )
    assert plan_cam_order(dict(req, empty_beats=[1], unlock_beats=[3]), curves)["accepted"]
    assert plan_cam_order(dict(req, empty_beats=[]), curves)["accepted"] is False


def test_carried_left_lock_cannot_be_treated_as_reset_without_empty_beat(tmp_path):
    rows = ledger_rows({"L2": (2, 2), "C": (2, 2)})
    inherited = {"side": "left", "delta": 1, "next_unlock_beat": None}

    blocked = plan_cam_order(requirement(
        order_id="next-equal-start", mode="cam", cam_capable=True,
        left_spring_id="L2", right_spring_id="C", tolerance=0,
        equal_start_required=True,
    ), build_curves(rows), inherited_lock=inherited)
    assert blocked["accepted"] is False
    assert blocked["rejection_reasons"] == [
        "inherited_cam_lock_not_reset_before_equal_start"
    ]

    reset_ok = plan_cam_order(requirement(
        order_id="next-reset", mode="cam", cam_capable=True,
        left_spring_id="L2", right_spring_id="C", tolerance=0,
        equal_start_required=True, reset_beat="pre-start-empty",
    ), build_curves(rows), inherited_lock=inherited)
    assert reset_ok["accepted"] is True

    state = tmp_path / "locks.json"
    desk = InstallationDesk(state)
    desk.machine_locks["M"] = dict(inherited, status="locked", next_unlock_beat=3)
    import pytest
    with pytest.raises(Exception):
        desk.unlock_machine("M", beat=2)
    unlocked = desk.unlock_machine("M", beat=3)
    assert unlocked["status"] == "unlocked"
    assert "M" not in desk.machine_locks


def test_run_reads_selected_batch_from_actual_results_and_keeps_sources(tmp_path):
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    rows = ledger_rows({"L1": (2, 2), "C": (2, 2), "D": (2, 3)})
    table = pa.Table.from_pylist(rows, schema=pa.schema([
        pa.field("spring_id", pa.string()),
        pa.field("trial_id", pa.string()),
        pa.field("direction", pa.string()),
        pa.field("force_level", pa.string()),
        pa.field("displacement", pa.int64()),
    ]))
    pq.write_table(table, fixture_dir / "ledger.parquet")
    (fixture_dir / "files.json").write_text(json.dumps(["ledger.parquet"]), encoding="utf-8")

    queries = {
        "queries": [{
            "id": "selected_springs",
            "where": {"ge": ["displacement", 0]},
            "columns": ["spring_id", "trial_id", "direction",
                         "force_level", "displacement"],
        }],
        "spring_inspection": {
            "result": "selected_springs",
            "state_path": str(tmp_path / "desk.json"),
            "springs": ["L1", "C", "D"],
            "orders": [requirement(
                order_id="from-real-results", left_spring_id="L1",
                tolerance=0, minimum_lift=2,
            )],
        },
    }
    queries_path = fixture_dir / "queries.json"
    queries_path.write_text(json.dumps(queries, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"

    doc = run(str(fixture_dir / "files.json"), str(queries_path), str(out))
    assert all(q["passed"] for q in doc["verification"]["queries"])

    result_path = out / "results" / "selected_springs.parquet"
    assert result_path.exists()
    result = pq.read_table(result_path).to_pylist()
    assert all(r["_source_file"].endswith("ledger.parquet") for r in result)

    inspection = doc["spring_inspection"]
    assert inspection["result_parquet"] == str(result_path)
    assert inspection["physical_provenance_preserved"] is True
    order = inspection["orders"][0]
    assert order["accepted"] is True
    assert order["pair"]["right_spring_id"] == "C"

    report = json.loads((out / "spring_inspection.json").read_text(encoding="utf-8"))
    source_files = {
        s["_source_file"]
        for o in report["orders"]
        for check in o["checks"]
        for side in ("left", "right")
        for s in check["sources"][side]
    }
    assert all(path.endswith("ledger.parquet") for path in source_files)
    assert (out / "spring_curves.parquet").exists()
