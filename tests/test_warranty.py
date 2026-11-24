"""保修接单功能测试：证据重建、整架朝向约束、三种办法、三杠杆、跨单台账与拆检分支。

运行：PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import inspect
import json

import pyarrow.parquet as pq
import pytest

from pqscan.warranty import (
    ACCEPT, ACCEPT_PENDING_CUT, REFUSE,
    Group, InventoryItem, OrderRequest, ProblemConfig, StockLedger, WitnessPot,
    decide, inventory_from_list, load_evidence, problem_config_from_dict,
    process_order, resolve_after_cut, run_orders, select_order_pots, witnesses_from_list,
)
from pqscan.warranty import decision as decision_mod
from pqscan.warranty.fixtures import build as build_fixtures
from pqscan.warranty.levers import demonstrate_levers


@pytest.fixture(scope="module")
def fixture(tmp_path_factory):
    base = tmp_path_factory.mktemp("warranty")
    info = build_fixtures(str(base))
    cfg = json.load(open(info["config"], encoding="utf-8"))
    base_dir = str(base)
    # 夹具内路径为相对名；测试直接调用时统一先解析为绝对路径，模拟 CLI 的 base_dir。
    ev = {
        order: [p if p.startswith("/") else f"{base_dir}/{p}" for p in paths]
        for order, paths in cfg["evidence_for_order"].items()
    }
    return {
        "info": info,
        "config": cfg,
        "base_dir": base_dir,
        "evidence": ev,
        "problem": problem_config_from_dict(cfg["problem"]),
        "inventory": inventory_from_list(cfg["inventory"]),
        "witnesses": witnesses_from_list(cfg["witnesses"]),
    }


def _pots(fixture, order="ORD-A"):
    paths = fixture["evidence"][order]
    pots, hangers = load_evidence(paths)
    return pots, hangers


# ---------------------------------------------------------------------------
# 证据重建：整架朝向一致，缺读数保留两种可能
# ---------------------------------------------------------------------------

def test_hanger_orientation_is_shared_and_analysis_reads_source_rows(fixture):
    pots, hangers = _pots(fixture)
    by = {p.pot_id: p for p in pots}
    # H1 有确定读数 hot_row=0：A10(排0) 只能 GX/high，A20(排1) 只能 GX/low。
    assert [g.label() for g in by["A10"].possible_groups] == ["GX/high"]
    assert [g.label() for g in by["A20"].possible_groups] == ["GX/low"]
    assert sorted(hangers[("furnace-alpha", "H1")].possible_hot_rows) == [0]
    # H2 读数缺失：两只锅都保留 {GY/high, GY/low}，同一架不能被分配两种朝向。
    assert sorted(g.label() for g in by["B10"].possible_groups) == ["GY/high", "GY/low"]
    assert sorted(g.label() for g in by["B20"].possible_groups) == ["GY/high", "GY/low"]
    assert sorted(hangers[("furnace-alpha", "H2")].possible_hot_rows) == [0, 1]
    # 每条证据都能查回原始 Parquet 的 (文件, 行组, 组内行)。
    base = fixture["base_dir"]
    for p in pots:
        for r in p.rows:
            src = r.source_file if r.source_file.startswith("/") else f"{base}/{r.source_file}"
            tbl = pq.ParquetFile(src).read_row_group(r.source_row_group)
            assert tbl.column("pot_id")[r.source_row_in_group].as_py() == p.pot_id


def test_conflicting_readings_make_hanger_unprovable(fixture, tmp_path):
    from pqscan.warranty.evidence_io import write_evidence_raw
    raw = str(tmp_path / "conflict.parquet")
    write_evidence_raw(raw, [
        [{"furnace_id": "F", "hanger_id": "H", "pot_id": "P1", "row": 0, "slot": 1,
          "glaze_batch": "G", "hot_row": 0, "witness_plate_id": "C"}],
        [{"furnace_id": "F", "hanger_id": "H", "pot_id": "P2", "row": 1, "slot": 2,
          "glaze_batch": "G", "hot_row": 1, "witness_plate_id": "C"}],
    ])
    pots, hangers = load_evidence([raw])
    assert hangers[("F", "H")].conflicting is True
    assert hangers[("F", "H")].possible_hot_rows == frozenset()
    assert all(p.possible_groups is None for p in pots)
    assert all("矛盾" in p.unknown_reason for p in pots)


def test_missing_glaze_is_never_record_safe(fixture, tmp_path):
    from pqscan.warranty.evidence_io import write_evidence_raw
    raw = str(tmp_path / "noglaze.parquet")
    write_evidence_raw(raw, [[
        {"furnace_id": "F", "hanger_id": "H", "pot_id": "P1", "row": 0, "slot": 1,
         "glaze_batch": None, "hot_row": 0, "witness_plate_id": "C"},
    ]])
    pots, _ = load_evidence([raw])
    assert pots[0].possible_groups is None and "底釉" in pots[0].unknown_reason
    sheet = decide("T", pots, ProblemConfig(frozenset({Group("G", "high")})), [], [])
    assert sheet.verdict == REFUSE  # 没有库存、组未知，不能放行


# ---------------------------------------------------------------------------
# 题目工作例：读数确定甲在右可保甲，但不得用甲放行左侧的乙
# ---------------------------------------------------------------------------

def test_known_orientation_releases_only_outside_pot(fixture):
    pots, _ = _pots(fixture)
    order = select_order_pots([p for p in pots if p.furnace_id == "furnace-alpha"],
                              ["A10", "A20"])
    sheet = decide("T", order, fixture["problem"], [], [], method="record")
    d = {x.pot_id: x for x in sheet.dispositions}
    # A10 在高温侧(疑似) -> record 不能担保；A20 在低温侧 -> 安全放行。
    assert d["A10"].status == "unresolved"
    assert d["A20"].status == "safe_record" and d["A20"].shipped_as == "original"
    assert sheet.verdict == REFUSE
    # 关键否定：A20 的“无风险”没有被用来放行 A10。
    assert d["A10"].status != "safe_record"


def test_orientation_override_conflicting_with_known_reading_raises(fixture):
    """补读架向若与已有确定读数相反，必须报冲突，不能用单条补读硬覆盖。"""
    from pqscan.warranty import WarrantyError
    paths = fixture["evidence"]["ORD-A"]
    # H1 已有 hot_row=0；补读 hot_row=1 与之冲突。
    with pytest.raises(WarrantyError):
        load_evidence(paths, orientation_overrides={("furnace-alpha", "H1"): 1})
    # H2 原为缺失，补读合法。
    pots, hangers = load_evidence(paths, orientation_overrides={("furnace-alpha", "H2"): 1})
    assert hangers[("furnace-alpha", "H2")].possible_hot_rows == frozenset({1})


def test_unknown_orientation_cannot_release_either_pot(fixture):
    pots, _ = _pots(fixture)
    order = select_order_pots([p for p in pots if p.furnace_id == "furnace-alpha"],
                              ["B10", "B20"])
    sheet = decide("T", order, fixture["problem"], [], [], method="record")
    # 同一挂架读数缺失：两只锅都可能落入 GY/high，谁也不能凭记录放行。
    assert all(d.status != "safe_record" for d in sheet.dispositions)
    assert sheet.verdict == REFUSE


# ---------------------------------------------------------------------------
# 三种整单办法
# ---------------------------------------------------------------------------

def test_record_method_refuses_when_any_pot_at_risk(fixture):
    pots, _ = _pots(fixture)
    order = select_order_pots([p for p in pots if p.furnace_id == "furnace-alpha"],
                              ["A10", "A20", "B10", "B20"])
    sheet = decide("T", order, fixture["problem"], fixture["inventory"],
                   fixture["witnesses"], method="record")
    assert sheet.verdict == REFUSE
    assert sheet.inventory_consumed == [] and sheet.witnesses_scrapped == []


def test_replace_method_consumes_inventory_only_for_at_risk(fixture):
    pots, _ = _pots(fixture)
    order = select_order_pots([p for p in pots if p.furnace_id == "furnace-alpha"],
                              ["A10", "A20"])
    sheet = decide("T", order, fixture["problem"], fixture["inventory"], [], method="replace")
    d = {x.pot_id: x for x in sheet.dispositions}
    assert d["A20"].status == "safe_record"          # 不需替换
    assert d["A10"].status == "replaced"
    assert sheet.inventory_consumed == ["R1"]
    assert sheet.verdict == ACCEPT


def test_replace_method_refuses_without_inventory(fixture):
    pots, _ = _pots(fixture)
    order = select_order_pots([p for p in pots if p.furnace_id == "furnace-alpha"], ["A10"])
    sheet = decide("T", order, fixture["problem"], [], [], method="replace")
    assert sheet.verdict == REFUSE and sheet.dispositions[0].status == "unresolved"


def test_cut_method_groups_must_match_exactly(fixture):
    pots, _ = _pots(fixture)
    order = select_order_pots([p for p in pots if p.furnace_id == "furnace-alpha"],
                              ["B10", "B20"])
    # 有 GY/high 见证锅 -> 条件接单。
    ok = decide("T", order, fixture["problem"], [], fixture["witnesses"], method="cut")
    assert ok.verdict == ACCEPT_PENDING_CUT
    # 换成 GX/high 见证锅（同底釉但组不同）-> 无法覆盖 GY/high -> 拒签。
    wrong = decide("T", order, fixture["problem"], [],
                   [WitnessPot("W-GX-HIGH", "GX", "high")], method="cut")
    assert wrong.verdict == REFUSE
    # 换成 GY/low 见证锅（同底釉、不同温区）-> 同样不能覆盖 GY/high。
    wrong_zone = decide("T", order, fixture["problem"], [],
                        [WitnessPot("W-GY-LOW", "GY", "low")], method="cut")
    assert wrong_zone.verdict == REFUSE


# ---------------------------------------------------------------------------
# 拆检结果只能到手后使用；阴性/阳性各自的作用范围
# ---------------------------------------------------------------------------

def test_decide_has_no_cut_result_input():
    """结构约束：decision.decide 的签名里不接受任何拆检结果，杜绝预挑分支。"""
    params = inspect.signature(decision_mod.decide).parameters
    assert not any("result" in name.lower() or "negative" in name.lower()
                   or "positive" in name.lower() for name in params)


def test_pending_sheet_states_both_branches(fixture):
    pots, _ = _pots(fixture)
    order = select_order_pots([p for p in pots if p.furnace_id == "furnace-alpha"],
                              ["B10", "B20"])
    sheet = decide("T", order, fixture["problem"], [], fixture["witnesses"], method="cut")
    assert sheet.verdict == ACCEPT_PENDING_CUT
    assert "GY/high" in sheet.branches
    br = sheet.branches["GY/high"]
    assert br["negative"]["pots_released"] == ["B10", "B20"]
    assert br["positive"]["pots_blocked"] == ["B10", "B20"]


def test_resolve_negative_releases_group(fixture):
    pots, _ = _pots(fixture)
    order = select_order_pots([p for p in pots if p.furnace_id == "furnace-alpha"],
                              ["B10", "B20"])
    sheet = decide("T", order, fixture["problem"], [], fixture["witnesses"], method="cut")
    out = resolve_after_cut(sheet, {"GY/high": "negative"}, fixture["inventory"])
    assert out["verdict"] == ACCEPT
    assert sorted(out["released"]) == ["B10", "B20"]
    assert out["quarantined"] == []


def test_resolve_positive_quarantines_then_replaces_or_refuses(fixture):
    pots, _ = _pots(fixture)
    order = select_order_pots([p for p in pots if p.furnace_id == "furnace-alpha"],
                              ["B10", "B20"])
    sheet = decide("T", order, fixture["problem"], [], fixture["witnesses"], method="cut")
    # 有可用库存 -> 阳性隔离后用库存替换，整单仍可接。
    out_ok = resolve_after_cut(sheet, {"GY/high": "positive"}, fixture["inventory"])
    assert out_ok["verdict"] == ACCEPT
    assert sorted(out_ok["quarantined"]) == ["B10", "B20"]
    assert sorted(out_ok["replaced_after_positive"]) == ["B10", "B20"]
    # 无可用库存 -> 阳性隔离后拒签。
    out_bad = resolve_after_cut(sheet, {"GY/high": "positive"}, [])
    assert out_bad["verdict"] == REFUSE
    assert sorted(out_bad["refused_after_positive"]) == ["B10", "B20"]


def test_resolve_positive_is_not_extrapolated_to_other_group(fixture):
    """阳性只隔离它代表的那一组，不波及别的组。"""
    pots, _ = _pots(fixture)
    # 构造一只需要 GY/high 与 GX/high 两组澄清的锅：用 H1 的 A20(GX/low 安全) 不行，
    # 改为直接用 B10/B20(GY/high) 且另造一只 GX/high 的待拆锅，确认阳性只锁 GY/high。
    order = select_order_pots([p for p in pots if p.furnace_id == "furnace-alpha"],
                              ["B10", "B20"])
    witnesses = fixture["witnesses"] + [WitnessPot("W-GX-HIGH", "GX", "high")]
    sheet = decide("T", order, fixture["problem"], [], witnesses, method="cut")
    out = resolve_after_cut(sheet, {"GY/high": "positive"}, fixture["inventory"])
    # 只有 B10/B20 被隔离；没有别的组的锅被牵连。
    assert sorted(out["quarantined"]) == ["B10", "B20"]


def test_resolve_requires_complete_results(fixture):
    pots, _ = _pots(fixture)
    order = select_order_pots([p for p in pots if p.furnace_id == "furnace-alpha"], ["B10"])
    sheet = decide("T", order, fixture["problem"], [], fixture["witnesses"], method="cut")
    out = resolve_after_cut(sheet, {}, fixture["inventory"])
    assert out["verdict"] == "incomplete"


def test_resolve_does_not_reuse_inventory_already_committed(fixture):
    """阳性分支不得挪用本单已替换用掉的库存。"""
    pots, _ = _pots(fixture)
    order = select_order_pots([p for p in pots if p.furnace_id == "furnace-alpha"],
                              ["A10", "B10"])
    r1 = InventoryItem("R1", "GX", "low")
    # 只给一件 R1：A10 先替换占 R1，B10 走拆检；给 resolve 的库存清单里也只有 R1，
    # 而 R1 已被本单承诺给 A10，不得再挪用 -> 阳性时无余量 -> 拒签。
    sheet = decide("T", order, fixture["problem"], [r1], fixture["witnesses"], method="auto")
    assert sheet.verdict == ACCEPT_PENDING_CUT
    assert sheet.inventory_consumed == ["R1"]
    out = resolve_after_cut(sheet, {"GY/high": "positive"}, [r1])
    assert out["verdict"] == REFUSE
    assert out["replaced_after_positive"] == {}

    # 若另有一件未承诺的 R2，则阳性分支可合法改用它，整单仍可接。
    r2 = InventoryItem("R2", "GY", "low")
    out2 = resolve_after_cut(sheet, {"GY/high": "positive"}, [r1, r2])
    assert out2["verdict"] == ACCEPT
    assert out2["replaced_after_positive"] == {"B10": "R2"}


# ---------------------------------------------------------------------------
# 跨单台账：已交出的替换锅 / 已拆见证锅不得再用
# ---------------------------------------------------------------------------

def test_ledger_blocks_reuse_across_orders(fixture):
    orders = [
        OrderRequest("ORD-A", "furnace-alpha", ["A10", "A20", "B10", "B20"], "auto"),
        OrderRequest("ORD-B", "furnace-beta", ["C10", "C20"], "auto"),
    ]
    doc = run_orders(orders, fixture["evidence"],
                     fixture["problem"], fixture["inventory"], fixture["witnesses"])
    reuse_inv = doc["reuse_report"]["inventory_items"]
    reuse_wit = doc["reuse_report"]["witness_pots"]
    assert all(v["reusable"] is False for v in reuse_inv.values())
    assert all(v["reusable"] is False for v in reuse_wit.values())
    assert doc["ledger"]["consumed_items"] == {"R1": "ORD-A", "R2": "ORD-A"}
    # 第二张单（不同炉号、不同锅号）也各有回单与溯源。
    assert [r["order"]["order_id"] for r in doc["receipts"]] == ["ORD-A", "ORD-B"]
    for rc in doc["receipts"]:
        assert all(a["all_exist"] for a in rc["receipt"]["provenance_audit"].values())


def test_second_order_cannot_reuse_first_orders_inventory(fixture):
    """强制第二张单也用 replace 办法：第一张已耗尽 R1/R2，第二张必须拒签（无库存）。"""
    orders = [
        OrderRequest("ORD-A", "furnace-alpha", ["A10", "A20"], "replace"),
        OrderRequest("ORD-B", "furnace-beta", ["C10", "C20"], "replace"),
    ]
    doc = run_orders(orders, fixture["evidence"],
                     fixture["problem"], fixture["inventory"], fixture["witnesses"])
    assert doc["receipts"][0]["verdict"] == ACCEPT
    assert doc["receipts"][1]["verdict"] == REFUSE  # 库存已被第一张单占满


def test_receipt_traces_each_pot_to_source_record(fixture):
    ledger = StockLedger()
    res = process_order(
        OrderRequest("ORD-B", "furnace-beta", ["C10", "C20"], "auto"),
        fixture["evidence"]["ORD-B"],
        fixture["problem"], fixture["inventory"], fixture["witnesses"], ledger,
    )
    audit = res["receipt"]["provenance_audit"]
    assert set(audit) == {"C10", "C20"}
    for pid, a in audit.items():
        assert a["all_exist"] and len(a["rows"]) == 1
        row = a["rows"][0]
        assert row["actual_pot_id"] == pid and row["pot_id_matches"] is True


# ---------------------------------------------------------------------------
# 三种单点杠杆各改变同一张订单
# ---------------------------------------------------------------------------

def test_three_levers_each_change_one_order(fixture):
    req = OrderRequest("ORD-A", "furnace-alpha", ["A10", "A20", "B10", "B20"], "auto")
    out = demonstrate_levers(
        req, fixture["evidence"]["ORD-A"],
        fixture["problem"], fixture["inventory"], fixture["witnesses"],
        extra_orientation={("furnace-alpha", "H2"): 1},
        extra_inventory=InventoryItem("R3", "GY", "low"),
        replacement_witness=WitnessPot("W-GX-HIGH", "GX", "high"),
        removed_witness_id="W-GY-HIGH",
    )
    base = out["baseline"]
    assert base["verdict"] == ACCEPT_PENDING_CUT
    lv = out["levers"]
    # 补一条架向读数：H2 朝向钉死 -> B10 变安全、B20 被替换，整单不再需要拆检。
    assert lv["orientation_reading_added"]["changed"] is True
    assert lv["orientation_reading_added"]["result"]["verdict"] == ACCEPT
    assert lv["orientation_reading_added"]["result"]["cuts"] == []
    # 改一件库存：多一件合格锅 -> B20 直接替换，整单可接、无需拆检。
    assert lv["inventory_item_changed"]["changed"] is True
    assert lv["inventory_item_changed"]["result"]["verdict"] == ACCEPT
    assert lv["inventory_item_changed"]["result"]["rows"]["B20"]["status"] == "replaced"
    # 换一只见证锅：撤掉 GY/high 见证锅 -> B20 无处澄清 -> 拒签。
    assert lv["witness_pot_changed"]["changed"] is True
    assert lv["witness_pot_changed"]["result"]["verdict"] == REFUSE
    assert lv["witness_pot_changed"]["result"]["rows"]["B20"]["status"] == "unresolved"
