"""bridgeplaque 测试：参照带对齐、断口拼接、工单约束、签发/台账、演示翻转。

运行：PYTHONPATH=src .venv/bin/python -m pytest tests/test_bridgeplaque.py -q
"""
from __future__ import annotations

import json
import os

import pytest

from bridgeplaque import PlaqueError
from bridgeplaque.band import ReferenceBand, load_band
from bridgeplaque.beams import (Fragment, SourceRow, build_beam_records,
                                stitch_beam)
from bridgeplaque.demo import run_demo
from bridgeplaque.plaque import (Application, CRAFT_MICROCORE,
                                 CRAFT_NONINVASIVE, issue_plaque,
                                 lookup_plaque)
from bridgeplaque.solve import BeamDating, possibilities_for, solve_event_years
from bridgeplaque.workorder import FRESH, STOCK, load_workorder

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures_bridge")
SEGMENT = "南桥段"


def _app(beam="甲", craft=CRAFT_NONINVASIVE):
    return Application(segment=SEGMENT, beam_id=beam, craft=craft)


@pytest.fixture(scope="module")
def band():
    return load_band(os.path.join(FIXTURES, "band.json"))


@pytest.fixture(scope="module")
def workorder():
    return load_workorder(os.path.join(FIXTURES, "workorder.json"))


# ---------------------------------------------------------------------------
# 参照带：对齐与漏读通配
# ---------------------------------------------------------------------------

def test_band_alignment_design(band):
    """随题自造参照带满足演示设计：甲双候选、乙丙丁各自唯一。"""
    assert band.align("宽窄窄窄窄?宽窄窄") == [42, 54]      # 甲（漏读一环）
    assert band.end_year - band.start_year + 1 == 60


def test_band_gap_wildcard_and_no_hit():
    band = ReferenceBand(start_year=10, widths="窄宽窄宽")
    assert band.align("窄宽") == [11, 13]          # 末环年 = 落点末位
    assert band.align("窄?宽") == []               # 两个落点的第3环都是窄，不成立
    band2 = ReferenceBand(start_year=10, widths="窄窄宽宽")
    assert band2.align("窄?宽") == [12, 13]        # ? 通配中间一环
    assert band.align("宽宽") == []
    with pytest.raises(PlaqueError):
        band.align("窄x宽")


def test_fixture_beams_align_as_designed(band):
    """夹具案卷四梁的拼接读数在参照带上的落点符合设计（四梁、十二年）。"""
    import pyarrow.parquet as pq
    table = pq.read_table(os.path.join(FIXTURES, "archive", "beam_readings.parquet"))
    frags = [
        Fragment(row["beam_id"], row["fragment"], row["start_pos"], row["widths"],
                 bool(row["bark_edge"]), SourceRow("archive", 0, i))
        for i, row in enumerate(table.to_pylist())
    ]
    records = build_beam_records(frags)
    assert band.align(records["甲"].pattern) == [42, 54]     # 双候选：54 即另一次重装
    assert band.align(records["乙"].pattern) == [39]
    assert band.align(records["丙"].pattern) == [54]
    assert band.align(records["丁"].pattern) == [36]
    assert 54 - 42 == 12                                     # 初建->重装恰十二年


# ---------------------------------------------------------------------------
# 断口拼接：同树接回、只计一份佐证
# ---------------------------------------------------------------------------

def _frag(beam, frag, start, widths, bark=0):
    return Fragment(beam_id=beam, fragment=frag, start_pos=start, widths=widths,
                    bark_edge=bool(bark), source=SourceRow("f", 0, 0))


def test_stitch_joins_gap_into_single_tree_evidence():
    rec = stitch_beam([_frag("甲", "甲-2", 7, "宽窄窄", 1), _frag("甲", "甲-1", 1, "宽窄窄窄窄")])
    assert rec.pattern == "宽窄窄窄窄?宽窄窄"       # 断口空一环以 ? 接回
    assert rec.bark_edge is True                    # 最外片段末环带皮
    assert len(rec.fragments) == 2                  # 两块碎片 -> 一条 BeamRecord


def test_stitch_rejects_gap_wider_than_one_ring():
    with pytest.raises(PlaqueError, match="无法接回同一根树"):
        stitch_beam([_frag("甲", "甲-1", 1, "窄"), _frag("甲", "甲-2", 4, "宽")])


def test_stitch_rejects_overlap_and_cross_beam():
    with pytest.raises(PlaqueError, match="重叠"):
        stitch_beam([_frag("甲", "甲-1", 1, "窄宽"), _frag("甲", "甲-2", 2, "宽")])
    with pytest.raises(PlaqueError, match="同一根梁"):
        stitch_beam([_frag("甲", "甲-1", 1, "窄"), _frag("乙", "乙-1", 3, "宽")])


def test_citable_pattern_drops_covered_tail():
    rec = stitch_beam([_frag("甲", "甲-1", 1, "窄宽"), _frag("甲", "甲-2", 4, "窄窄宽", 1)])
    assert rec.citable_pattern(set()) == "窄宽?窄窄宽"
    assert rec.citable_pattern({5, 6}) == "窄宽?窄"      # 被榫盖住的末尾外圈不得引用
    with pytest.raises(PlaqueError, match="末尾连续段"):
        rec.citable_pattern({2})                          # 被盖的不是末尾 -> 矛盾


# ---------------------------------------------------------------------------
# 工单与求解器
# ---------------------------------------------------------------------------

def test_workorder_sealing_narrows_events(workorder):
    assert workorder.allowed_events("甲") == ["初建"]          # J1 初建后封存
    assert workorder.allowed_events("乙") == ["初建"]
    assert workorder.covered_positions("甲") == {"J1": {8, 9}}
    with pytest.raises(PlaqueError):
        workorder.allowed_events("不存在梁")


def test_solver_stock_beam_cannot_pull_event_earlier(workorder):
    """现砍现装甲=42 锁定初建；旧材乙末轮 39 只压下界，初建不会提前到 39。"""
    datings = [
        BeamDating("甲", FRESH, ("初建",), frozenset({42, 54}), None),
        BeamDating("乙", STOCK, ("初建",), frozenset({39}), None),
        BeamDating("丙", FRESH, ("重装",), frozenset({54}), None),
        BeamDating("丁", STOCK, ("初建",), frozenset({36}), None),
    ]
    sols = solve_event_years(workorder, datings, 1, 60)
    assert possibilities_for(sols, "甲") == [("初建", 42)]
    assert all(s.years["初建"] == 42 and s.years["重装"] == 54 for s in sols)


def test_solver_lower_bound_only_defers(workorder):
    datings = [
        BeamDating("甲", FRESH, ("初建",), None, 43),          # 缺最外圈：只给下界
        BeamDating("丙", FRESH, ("重装",), frozenset({54}), None),
    ]
    sols = solve_event_years(workorder, datings, 1, 60)
    years = {y for _, y in possibilities_for(sols, "甲")}
    assert years == set(range(43, 54))                         # 不唯一 -> 应暂缓


# ---------------------------------------------------------------------------
# 签发端到端（真实跑 pqscan）
# ---------------------------------------------------------------------------

def test_base_issue_pins_year_42(tmp_path):
    out = str(tmp_path / "base")
    rec = issue_plaque(FIXTURES, out, _app())
    assert rec["plaque_id"] == "PB-0001"
    assert rec["status"] == "issued"
    assert rec["dating"] == {"event": "初建", "year": 42}
    # 候选中的另一次重装被排除，且注明依据
    excluded = {(e["excluded"]["event"], e["excluded"]["year"]): e["reason"]
                for e in rec["excluded_candidates"]}
    assert ("重装", 54) in excluded and "封榫" in excluded[("重装", 54)]
    # 旧材乙不能把桥提前三年
    assert "不能据乙梁把桥提前3年" in rec["public_conclusion"]
    # pqscan 计划确实跳过了北桥段行组（真正命中才进 results）
    plan = json.load(open(os.path.join(out, "pqscan", "plans", "plaque_segment.json"),
                          encoding="utf-8"))
    actions = [g["action"] for f in plan["files"] for g in f["row_groups"]]
    assert actions == ["keep", "skip"]


def test_record_traces_beam_fragments_and_source_rows(tmp_path):
    rec = issue_plaque(FIXTURES, str(tmp_path), _app())
    beams = {b["beam_id"]: b for b in rec["beam_evidence"]}
    assert set(beams) == {"甲", "乙", "丙", "丁"}
    jia = beams["甲"]
    assert jia["fragments"] == ["甲-1", "甲-2"]               # 同树两片，一份佐证
    assert jia["same_tree_single_evidence"] is True
    assert jia["joined_pattern"] == "宽窄窄窄窄?宽窄窄"
    assert jia["last_ring_candidates"] == [42, 54]
    assert jia["bark_citable"] is True
    assert jia["felling_constraint"] == {"kind": "exact", "years": [42, 54]}
    assert all(s["row_group"] == 0 for s in jia["source_rows"])   # 南桥段在 RG0
    assert beams["乙"]["felling_constraint"] == {"kind": "exact", "years": [39]}


def test_repeated_calls_and_second_beam_application(tmp_path):
    """馆员反复调用：同一台账连续登记；换一根梁的案卷再办一次。"""
    out = str(tmp_path)
    rec1 = issue_plaque(FIXTURES, out, _app("甲"))
    rec2 = issue_plaque(FIXTURES, out, _app("乙"))            # 换梁再办
    assert (rec1["plaque_id"], rec2["plaque_id"]) == ("PB-0001", "PB-0002")
    assert rec2["status"] == "issued" and rec2["dating"]["year"] == 42
    # 乙自己的证据只是下界，结论靠甲锁定
    assert "乙梁库存旧材" in rec2["public_conclusion"]
    # 凭单号查回：具体梁片、安装证据、当时可公开的结论
    back = lookup_plaque(os.path.join(out, "plaque_ledger.json"), "PB-0001")
    assert back is not rec1 and back == rec1                  # 从磁盘查回
    assert back["beam_evidence"] and back["installation_evidence"]
    assert back["public_conclusion"] == rec1["public_conclusion"]
    with pytest.raises(PlaqueError, match="PB-9999"):
        lookup_plaque(os.path.join(out, "plaque_ledger.json"), "PB-9999")


def test_unknown_beam_or_segment_rejected(tmp_path):
    with pytest.raises(PlaqueError, match="未命中梁"):
        issue_plaque(FIXTURES, str(tmp_path / "a"), _app("不存在梁"))
    with pytest.raises(PlaqueError, match="工单没有"):
        issue_plaque(FIXTURES, str(tmp_path / "b"),
                     Application("北桥段", "戊", CRAFT_NONINVASIVE))


def test_microcore_consumes_the_only_allowed_site(tmp_path):
    """微芯定稿后，同一台账再申请微芯被拒绝（只可开一处）。"""
    fixtures = str(tmp_path / "fixtures")
    _make_view_denied_fixtures(fixtures)
    out = str(tmp_path / "out")
    rec1 = issue_plaque(fixtures, out, _app("甲", CRAFT_MICROCORE))
    assert rec1["status"] == "issued" and rec1["dating"]["year"] == 42
    assert rec1["core"]["joint"] == "J1" and "观察机会已永久损失" in rec1["core"]["cost"]
    rec2 = issue_plaque(fixtures, out, _app("乙", CRAFT_MICROCORE))
    assert rec2["status"] == "deferred"
    assert any("只可开一处" in p or "已用于 PB-0001" in p for p in rec2["problems"])


def _make_view_denied_fixtures(dst):
    """复制夹具并收回 J1 查看权、放开取样许可（供微芯工艺测试）。"""
    import shutil
    shutil.copytree(FIXTURES, dst)
    path = os.path.join(dst, "workorder.json")
    doc = json.load(open(path, encoding="utf-8"))
    doc["permits"]["view"]["J1"] = False
    doc["permits"]["sample"]["J1"] = True
    json.dump(doc, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 演示矩阵：单项改动翻转同一次署年
# ---------------------------------------------------------------------------

def test_demo_matrix_flips(tmp_path):
    steps = run_demo(FIXTURES, str(tmp_path))
    by_demo = {s["demo"]: s for s in steps}
    assert by_demo["基准"]["status"] == "issued"
    assert by_demo["基准"]["dating"] == {"event": "初建", "year": 42}
    # 只改单片证据 / 封榫年代 / 查看权 -> 都翻转成暂缓
    for name in ("D1 单片证据", "D2 封榫年代", "D3 查看权", "D4 取样许可（无）"):
        assert by_demo[name]["status"] == "deferred", name
    # D2 的不唯一恰是 初建42 / 重装54
    assert by_demo["D2 封榫年代"]["possibilities"] == [
        {"event": "初建", "year": 42}, {"event": "重装", "year": 54}]
    # 只放开取样许可 -> 微芯定稿 42；再开第二处 -> 拒绝
    assert by_demo["D4 取样许可（有）"]["dating"] == {"event": "初建", "year": 42}
    assert by_demo["D4 再开第二处"]["status"] == "deferred"
