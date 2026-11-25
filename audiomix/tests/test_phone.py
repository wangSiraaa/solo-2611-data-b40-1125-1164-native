"""虚拟电话线与回送台测试：逐条固化题面的手算断言（全部场景 <= 8 拍）。

覆盖：
- 削顶/同值：母带作差失败，独立来线还原成功；两回路两耳各听对方 20000；
  乙干声 20000->10000 只甲耳跟着变、乙耳不变、旧 render 不重写；
  单回路禁延迟出缺路权回执；允许拖后一拍即可同时交付。
- 已知接线 + 整数拍延迟：观测=干声+折返逐层可核对；甲耳只听乙本次 3，
  乙耳不重听已收过的 7；撞值（乙干声 7 vs 甲折返 7）两层轨都保留。
- 迟到问句：尾样保护、拍界换路、整场预约不可借；无等候窗口出回执。
- 缺来线回执；exactly-once；路权簿冲突拒绝与持久化重开。
- 全程“发布 -> 渲染 -> 重开 -> 区间读取”核对两只耳机。
"""
import json
import os

import numpy as np
import pytest

from audiomix.archive import ArchiveStore
from audiomix.assets import AssetRegistry
from audiomix.console import (
    CircuitLedger, DeliveryJob, EARS, Reservation, ReturnConsole, RoutingError,
    ShiftSpec,
)
from audiomix.engine import load_product, validate_product
from audiomix.lines import (
    EchoSpec, Route, WiringError, WiringManifest,
    build_line_pcms, build_manifest, master_mix, naive_master_subtraction,
    register_show_assets,
)

N = 8


def _pcm(values):
    return np.asarray(values, dtype=np.int16)


@pytest.fixture()
def env(tmp_path):
    reg = AssetRegistry(str(tmp_path / "registry"))
    store = ArchiveStore(str(tmp_path / "archive"))
    console = ReturnConsole(reg, str(tmp_path / "work"))
    out = str(tmp_path / "products")
    os.makedirs(out, exist_ok=True)
    yield {"reg": reg, "store": store, "console": console, "out": out,
           "root": str(tmp_path)}
    store.close()


def _manifest(reg, dry_a, dry_b, routes=(), echoes=(), show_id="show"):
    dry = {"A": dry_a, "B": dry_b}
    observed = build_line_pcms(dry, list(echoes))
    assets = register_show_assets(reg, dry, observed, note_prefix=show_id + "-")
    manifest = build_manifest(show_id, assets, list(routes), list(echoes),
                              registry=reg)
    return manifest


def _render_plan(console, manifest, plan, store, out, shift_id):
    """发布逐耳产品、渲染入仓，并返回重开式区间读取结果。"""
    renders = {}
    for ear in EARS:
        path = os.path.join(out, f"product_{shift_id}_{ear}.json")
        console.render_ear(manifest, plan, ear, store,
                           render_id=f"{shift_id}-ear{ear}", product_path=path)
        check = console.verify_ear_render(manifest, plan, store,
                                          f"{shift_id}-ear{ear}", ear)
        assert check["ok"]
        renders[ear] = check["rendered"]
    return renders


# ---------------------- S1：削顶/同值/作差反例/单回路 ---------------------- #

def test_clipped_master_subtraction_fails_but_lines_survive(env):
    reg = env["reg"]
    dry_a = _pcm([0, 20000, 0, 0, 0, 0, 0, 0])
    dry_b = _pcm([0, 20000, 0, 0, 0, 0, 0, 0])
    master = master_mix(dry_a, dry_b)
    assert int(master[1]) == 32767                     # 40000 被削顶
    guess = naive_master_subtraction(master, dry_a)
    assert int(guess[1]) == 12767 and int(guess[1]) != 20000  # 作差扣掉了乙的话

    manifest = _manifest(reg, dry_a, dry_b, show_id="clip")
    manifest.validate(reg)
    recon = manifest.reconstruct(reg)
    assert [l["value"] for l in recon[("A", 1)]] == [20000]
    assert [l["value"] for l in recon[("B", 1)]] == [20000]
    segs_a = manifest.speech_segments(reg, "A")
    assert segs_a == [{"guest": "A", "lo": 1, "hi": 2, "beats": [1],
                       "values": [20000]}]


def test_two_routes_each_ear_hears_other_twenty_thousand(env):
    console, reg, store, out = env["console"], env["reg"], env["store"], env["out"]
    dry = _pcm([0, 20000, 0, 0, 0, 0, 0, 0])
    manifest = _manifest(reg, dry, dry, show_id="two20k")
    plan = console.plan(manifest, ShiftSpec("t", ["c1", "c2"], 0, N))
    assert plan["receipts"] == []
    renders = _render_plan(console, manifest, plan, store, out, "t")
    assert renders["A"][1] == 20000 and renders["B"][1] == 20000
    # 本句不回自己耳机：两耳除拍1外全静音
    assert renders["A"] == [0, 20000, 0, 0, 0, 0, 0, 0]
    assert renders["B"] == [0, 20000, 0, 0, 0, 0, 0, 0]
    # 两条独立回路各走一条，无共用
    routes_used = {d["route_id"] for d in plan["deliveries"]}
    assert routes_used == {"c1", "c2"}


def test_change_b_dry_only_a_ear_changes_and_history_kept(env):
    console, reg, store, out = env["console"], env["reg"], env["store"], env["out"]
    dry_a = _pcm([0, 20000, 0, 0, 0, 0, 0, 0])
    manifest_v1 = _manifest(reg, dry_a, dry_a, show_id="v1")
    plan_v1 = console.plan(manifest_v1, ShiftSpec("v1", ["c1", "c2"], 0, N))
    _render_plan(console, manifest_v1, plan_v1, store, out, "v1")

    dry_b2 = _pcm([0, 10000, 0, 0, 0, 0, 0, 0])
    manifest_v2 = _manifest(reg, dry_a, dry_b2, show_id="v2")
    plan_v2 = console.plan(manifest_v2, ShiftSpec("v2", ["c1", "c2"], 0, N))
    renders = _render_plan(console, manifest_v2, plan_v2, store, out, "v2")
    assert renders["A"][1] == 10000                  # 甲耳跟着变
    assert renders["B"][1] == 20000                  # 乙耳不变（仍是甲的 20000）
    # 新素材新身份，且旧 render 原样可读（已播轨不重写）
    assert manifest_v2.dry_asset("B") != manifest_v1.dry_asset("B")
    old_a = store.read_samples("v1-earA", 0, N)
    assert int(old_a[1]) == 20000


def test_single_route_no_wait_cannot_deliver_both(env):
    console, reg, store, out = env["console"], env["reg"], env["store"], env["out"]
    dry = _pcm([0, 20000, 0, 0, 0, 0, 0, 0])
    manifest = _manifest(reg, dry, dry, show_id="one-route")
    plan = console.plan(manifest, ShiftSpec("nowait", ["c1"], 0, N))
    assert len(plan["deliveries"]) == 1
    assert len(plan["receipts"]) == 1
    r = plan["receipts"][0]
    assert r["reason_code"] == "no_route_rights"
    renders = _render_plan(console, manifest, plan, store, out, "nowait")
    assert renders["A"][1] == 20000 and renders["B"] == [0] * N


def test_single_route_with_one_beat_window_delivers_both(env):
    console, reg, store, out = env["console"], env["reg"], env["store"], env["out"]
    dry = _pcm([0, 20000, 0, 0, 0, 0, 0, 0])
    manifest = _manifest(reg, dry, dry, show_id="delay1")
    plan = console.plan(manifest, ShiftSpec("d1", ["c1"], 1, N))
    assert plan["receipts"] == []
    renders = _render_plan(console, manifest, plan, store, out, "d1")
    assert renders["A"][1] == 20000 and renders["A"][2] == 0
    assert renders["B"][1] == 0 and renders["B"][2] == 20000   # 拖后一拍
    delayed = [d for d in plan["deliveries"] if d["ear"] == "B"][0]
    assert delayed["delay"] == 1 and delayed["switch_at"] == 2


# ---------------------- S2：已知接线、整数拍延迟、撞值 ---------------------- #

@pytest.fixture()
def echo_manifest(env):
    reg = env["reg"]
    dry_a = _pcm([7, 0, 0, 0, 0, 0, 0, 0])
    dry_b = _pcm([0, 3, 0, 0, 0, 0, 0, 0])
    routes = [Route("r1", source_guest="A", target_ear="B", loopback_delay=1)]
    echoes = [EchoSpec(target_line="B", beat=1, source_guest="A",
                       source_beat=0, route_id="r1")]
    manifest = _manifest(reg, dry_a, dry_b, routes, echoes, show_id="echo")
    return manifest


def test_observed_pcm_two_layers_and_loopback_status(env, echo_manifest):
    reg = env["reg"]
    obs_b = reg.load_pcm(echo_manifest.observed_asset("B"))
    assert int(obs_b[0]) == 0 and int(obs_b[1]) == 10
    layers = echo_manifest.reconstruct(reg)[("B", 1)]
    assert {(l["kind"], l["value"], l["source_guest"], l["source_beat"],
             l["route_id"], l["route_delay"]) for l in layers} == {
        ("dry", 3, "B", 1, None, None),
        ("echo", 7, "A", 0, "r1", 1),
    }
    status = {(s["guest"], s["source_beat"]): s
              for s in echo_manifest.loopback_status()}
    assert status[("A", 0)]["returned"] is True
    assert status[("A", 0)]["arrived_beat"] == 1
    assert status[("A", 0)]["via_route"] == "r1"


def test_echo_routing_a_hears_three_b_keeps_single_seven(env, echo_manifest):
    console, store, out = env["console"], env["store"], env["out"]
    plan = console.plan(echo_manifest, ShiftSpec("live", ["c1", "c2"], 0, N))
    assert plan["receipts"] == []
    renders = _render_plan(console, echo_manifest, plan, store, out, "live")
    assert renders["A"] == [0, 3, 0, 0, 0, 0, 0, 0]   # 只听乙本次的 3
    assert renders["B"] == [7, 0, 0, 0, 0, 0, 0, 0]   # 7 只出现一次
    # 每个问句恰好投递一次
    assert len(plan["deliveries"]) == 2
    assert {d["source_guest"] for d in plan["deliveries"]} == {"A", "B"}


def test_equal_values_keep_distinct_layers(env, echo_manifest):
    reg = env["reg"]
    dry_a = _pcm([7, 0, 0, 0, 0, 0, 0, 0])
    dry_b_eq = _pcm([0, 7, 0, 0, 0, 0, 0, 0])
    routes = [Route("r1", "A", "B", loopback_delay=1)]
    echoes = [EchoSpec("B", 1, "A", 0, "r1")]
    manifest_eq = _manifest(reg, dry_a, dry_b_eq, routes, echoes, show_id="eq7")
    obs_b = reg.load_pcm(manifest_eq.observed_asset("B"))
    assert int(obs_b[1]) == 14
    layers = manifest_eq.reconstruct(reg)[("B", 1)]
    assert sorted((l["kind"], l["value"], l["source_guest"], l["asset_id"])
                  for l in layers) == [
        ("dry", 7, "B", manifest_eq.dry_asset("B")),
        ("echo", 7, "A", manifest_eq.dry_asset("A")),
    ]
    # 甲耳仍收到属于乙的那个 7
    plan = console_plan(env, manifest_eq, "eq7")
    ear_a = ReturnConsole.expected_ear_pcm(manifest_eq, plan, reg, "A")
    assert int(ear_a[1]) == 7


def console_plan(env, manifest, shift_id, **kw):
    return env["console"].plan(
        manifest, ShiftSpec(shift_id, ["c1", "c2"], 0, N), **kw)


# ---------------------- S3：迟到问句、尾样保护、路权 ---------------------- #

@pytest.fixture()
def late_env(env):
    reg = env["reg"]
    dry_a = _pcm([7, 0, 0, 0, 0, 0, 0, 0])
    dry_b = _pcm([0, 3, 0, 9, 0, 0, 0, 0])
    routes = [Route("r1", "A", "B", loopback_delay=1)]
    echoes = [EchoSpec("B", 1, "A", 0, "r1")]
    manifest = _manifest(reg, dry_a, dry_b, routes, echoes, show_id="late")
    booked = CircuitLedger(["c1", "c2"], N)
    booked.reserve("c1", 3, 4, "B", "前班遗留尾样")
    booked.reserve_entire_show("c2", "B", purpose="前班整场订走")
    late_q = DeliveryJob("job-late-B3->A", "A", "B", 3, 4, 3, max_delay=1)
    return env, manifest, booked, late_q


def test_late_question_signed_with_tail_protected(late_env):
    env, manifest, booked, late_q = late_env
    console, store, out = env["console"], env["store"], env["out"]
    shift = ShiftSpec("late-ok", ["c1", "c2"], 1, N)
    plan = console.plan(manifest, shift, pre_booked=booked,
                        extra_jobs=[late_q], skip_segments={("B", 3, 4)})
    assert plan["receipts"] == []
    renders = _render_plan(console, manifest, plan, store, out, "late-ok")
    assert renders["A"] == [0, 3, 0, 0, 9, 0, 0, 0]
    late = [d for d in plan["deliveries"] if d["job_id"] == "job-late-B3->A"][0]
    assert (late["route_id"], late["switch_at"], late["play_lo"],
            late["delay"]) == ("c1", 4, 4, 1)
    # 旧尾样预约原样保留
    ledger = CircuitLedger(plan["ledger"]["route_ids"], N,
                           [Reservation.from_json(x)
                            for x in plan["ledger"]["reservations"]])
    assert any(r.route_id == "c1" and (r.lo, r.hi, r.ear) == (3, 4, "B")
               for r in ledger.reservations)
    assert any(r.route_id == "c2" and (r.lo, r.hi) == (0, N)
               for r in ledger.reservations)


def test_late_question_no_wait_window_receipt(late_env):
    env, manifest, booked, _ = late_env
    console = env["console"]
    nowait_q = DeliveryJob("job-late-B3->A", "A", "B", 3, 4, 3, max_delay=0)
    plan = console.plan(
        manifest, ShiftSpec("late-no", ["c1", "c2"], 0, N),
        pre_booked=booked, extra_jobs=[nowait_q],
        skip_segments={("B", 3, 4)})
    receipts = [r for r in plan["receipts"] if r["job_id"] == "job-late-B3->A"]
    assert len(receipts) == 1 and receipts[0]["reason_code"] == "no_route_rights"
    assert "窗口" in receipts[0]["missing"]


# ---------------------- S4：缺来线 ---------------------- #

def test_missing_line_receipt_does_not_consume_route(env):
    reg, console = env["reg"], env["console"]
    dry_a = _pcm([1, 2, 0, 0, 0, 0, 0, 0])
    dry_b = _pcm([0] * N)
    manifest = _manifest(reg, dry_a, dry_b, show_id="missing")
    ghost = DeliveryJob("job-C2->A", "A", "C", 2, 3, 2, max_delay=0)
    plan = console.plan(
        manifest, ShiftSpec("m", ["c1", "c2"], 0, N), extra_jobs=[ghost])
    r = plan["receipts"][0]
    assert r["reason_code"] == "missing_line" and r["source_guest"] == "C"
    assert any(d["ear"] == "B" for d in plan["deliveries"])


# ---------------------- 路权簿不变量与持久化 ---------------------- #

def test_ledger_overlap_rejected_and_reopened(tmp_path):
    ledger = CircuitLedger(["c1"], N)
    ledger.reserve("c1", 2, 5, "A", "x")
    with pytest.raises(RoutingError):
        ledger.reserve("c1", 4, 6, "B", "y")       # 重叠拒
    with pytest.raises(RoutingError):
        ledger.reserve("c1", 0, N + 1, "A", "z")   # 越界拒
    path = ledger.save(str(tmp_path / "ledger.json"))
    reopened = CircuitLedger.load(path)
    assert reopened.reservations == ledger.reservations
    # 整场预约后无任何空闲档
    full = CircuitLedger(["c9"], N)
    full.reserve_entire_show("c9", "A")
    assert full.earliest_free_slot("c9", 0, 1) is None


def test_earliest_free_slot_finds_gap(tmp_path):
    ledger = CircuitLedger(["c1"], N)
    ledger.reserve("c1", 3, 4, "B", "tail")
    assert ledger.earliest_free_slot("c1", 0, 2) == 0     # 前面有空档
    assert ledger.earliest_free_slot("c1", 3, 1) == 4     # 尾样后
    assert ledger.earliest_free_slot("c1", 7, 2) is None  # 班内容不下


# ---------------------- 接线记录防篡改与重开核对 ---------------------- #

def test_manifest_tamper_and_reopen_render(env, echo_manifest):
    reg, console, store, out = env["reg"], env["console"], env["store"], env["out"]
    path = echo_manifest.save(os.path.join(out, "wiring.json"))
    assert WiringManifest.load(path).manifest_id == echo_manifest.manifest_id
    bad = json.loads(json.dumps(echo_manifest.to_json()))
    bad["n_beats"] = 7
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bad, fh)
    with pytest.raises(WiringError):
        WiringManifest.load(path)

    # 重开式核对：从磁盘装载接线/计划/数据仓，再区间读取与产品校验
    good_path = echo_manifest.save(os.path.join(out, "wiring.json"))
    plan = console.plan(echo_manifest, ShiftSpec("reopen", ["c1", "c2"], 0, N))
    plan_path = console.save_plan(plan, os.path.join(out, "plan.json"))
    assert ReturnConsole.load_plan(plan_path)["plan_id"] == plan["plan_id"]
    bad_plan = json.loads(json.dumps(plan))
    bad_plan["max_delay_beats"] = 9
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(bad_plan, fh)
    with pytest.raises(RoutingError):
        ReturnConsole.load_plan(plan_path)
    console.save_plan(plan, plan_path)  # 还原合法计划
    _render_plan(console, echo_manifest, plan, store, out, "reopen")
    manifest2 = WiringManifest.load(good_path)
    plan2 = ReturnConsole.load_plan(plan_path)
    store2 = ArchiveStore(store.root)
    try:
        for ear in EARS:
            rid = f"reopen-ear{ear}"
            rendered = store2.read_samples(rid, 0, N)
            expected = ReturnConsole.expected_ear_pcm(manifest2, plan2, reg, ear)
            assert np.array_equal(rendered, expected)
            product = load_product(os.path.join(out, f"product_reopen_{ear}.json"))
            validate_product(
                product,
                {a["asset_id"]: reg.load_pcm(a["asset_id"])
                 for a in product["assets"]})
        store2.verify_block_pcm_integrity()
    finally:
        store2.close()
