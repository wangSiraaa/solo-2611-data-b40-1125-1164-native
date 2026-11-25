"""两位电话嘉宾回送台：端到端演示（全部场景不超过 8 拍）。

场景：
  S1 削顶/同值：甲乙同拍各 20000，母带饱和 32767——证明母带作差不可用；
     两回路两耳各听到对方 20000；乙干声改 10000 后只甲耳跟着变；
     单回路且禁止延迟 -> 出缺路权回执；允许拖后一拍的班次 -> 可交付。
  S2 已知接线 + 整数拍延迟：拍0 甲说7（乙耳已收），拍1 乙线 = 干声3 + 甲7折返
     = 10，两层轨可沿发布记录核对；甲耳只听乙本次3，乙耳不重听7。
     另存一版乙干声恰为7（与折返撞值），乙的7仍作为独立层轨保留。
  S3 迟到问句换线：第一条路已给乙耳约了未播完尾样，第二条路整场被前班订走；
     允许晚一拍 -> 签换线且旧尾样照送；无等候窗口 -> 回执（缺路权）。
  S4 缺来线：源嘉宾没有独立干声来线 -> missing_line 回执。

每只耳机都经过：接线发布 -> 逐耳时间轴发布 -> 数据仓渲染 -> 重开 -> 区间读取
核对（不超过 8 拍）。用法：python3 -m demo.phone_demo [输出目录]
"""
import os
import shutil
import sys

import numpy as np

from audiomix.archive import ArchiveStore
from audiomix.assets import AssetRegistry, write_wav
from audiomix.console import (
    CircuitLedger, DeliveryJob, EARS, ReturnConsole, ShiftSpec,
)
from audiomix.engine import validate_product, load_product
from audiomix.lines import (
    EchoSpec, GUEST_CN, PHONE_RATE, Route, WiringManifest,
    build_line_pcms, build_manifest, master_mix, naive_master_subtraction,
    register_show_assets,
)

BASE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_phone"


def _line(title: str) -> None:
    print("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


def _pcm(values: list[int]) -> np.ndarray:
    return np.asarray(values, dtype=np.int16)


def _reopen_verify(console: ReturnConsole, manifest_path: str, arch_dir: str,
                   plan_path: str, reg_dir: str, n_beats: int) -> None:
    """重开式核对：所有对象从磁盘重新装载，再做区间读取与产品校验。"""
    manifest = WiringManifest.load(manifest_path)
    plan = ReturnConsole.load_plan(plan_path)
    store = ArchiveStore(arch_dir)
    reg = AssetRegistry(reg_dir)
    manifest.validate(reg)
    for ear in EARS:
        rid = f"{plan['shift_id']}-ear{ear}"
        rendered = store.read_samples(rid, 0, n_beats)          # 区间读取
        trace = store.read_trace(rid, 0, n_beats)               # 逐块追溯
        expected = ReturnConsole.expected_ear_pcm(manifest, plan, reg, ear)
        assert np.array_equal(rendered, expected), (ear, rendered, expected)
        product = load_product(
            os.path.join(console.workdir, f"product_{plan['shift_id']}_{ear}.json"))
        pcm = {a["asset_id"]: reg.load_pcm(a["asset_id"]) for a in product["assets"]}
        validate_product(product, pcm)
        assert trace and all(t["sample_range"][1] <= n_beats for t in trace)
    store.verify_block_pcm_integrity()
    store.close()


def _run_shift(console: ReturnConsole, manifest: WiringManifest, shift: ShiftSpec,
               pre_booked: CircuitLedger | None,
               extra_jobs: list[DeliveryJob] | None,
               store: ArchiveStore, out_dir: str) -> dict:
    plan = console.plan(manifest, shift, pre_booked=pre_booked, extra_jobs=extra_jobs)
    console.save_plan(plan, os.path.join(out_dir, f"plan_{shift.shift_id}.json"))
    _save_ledger(plan, shift, out_dir)
    for ear in EARS:
        product_path = os.path.join(out_dir, f"product_{shift.shift_id}_{ear}.json")
        console.render_ear(manifest, plan, ear, store,
                           render_id=f"{shift.shift_id}-ear{ear}",
                           product_path=product_path)
        check = console.verify_ear_render(
            manifest, plan, store, f"{shift.shift_id}-ear{ear}", ear)
        assert check["ok"], check
        print(f"  {GUEST_CN[ear]}耳 render={check['render_id']} "
              f"逐拍={check['rendered']}")
    for r in plan["receipts"]:
        print(f"  [回执] {r['missing']} | {r['detail']} (code={r['reason_code']})")
    for d in plan["deliveries"]:
        print(f"  [投递] {GUEST_CN[d['ear']]}耳 <- {GUEST_CN[d['source_guest']]} "
              f"源[{d['source_lo']},{d['source_hi']}) 回路{d['route_id']} "
              f"换路拍={d['switch_at']} 播放[{d['play_lo']},{d['play_hi']}) "
              f"延迟={d['delay']}拍")
    return plan


def _save_ledger(plan: dict, shift: ShiftSpec, out_dir: str) -> str:
    """把计划内的路权簿单独落盘（重开时可独立装载核对）。"""
    from audiomix.console import Reservation
    ledger = CircuitLedger(
        plan["ledger"]["route_ids"], plan["ledger"]["n_beats"],
        [Reservation.from_json(x) for x in plan["ledger"]["reservations"]])
    path = os.path.join(out_dir, f"ledger_{shift.shift_id}.json")
    ledger.save(path)
    return path


# ============================ S1：削顶/同值与作差反例 ============================ #

def scenario_1(reg: AssetRegistry, console: ReturnConsole, store: ArchiveStore,
               out_dir: str) -> None:
    _line("S1 削顶/同值：独立干声可还原，母带作差不可用；两回路/单回路班次")
    n = 8
    dry_a = _pcm([0, 20000, 0, 0, 0, 0, 0, 0])
    dry_b = _pcm([0, 20000, 0, 0, 0, 0, 0, 0])
    observed = build_line_pcms({"A": dry_a, "B": dry_b}, echoes=[])
    assets = register_show_assets(reg, {"A": dry_a, "B": dry_b}, observed,
                                  note_prefix="S1-")
    manifest = build_manifest("S1", assets, routes=[], echoes=[], registry=reg)
    manifest_path = os.path.join(out_dir, "wiring_S1.json")
    manifest.save(manifest_path)
    manifest.validate(reg)

    # 总母带削顶；母带作差反例
    master = master_mix(dry_a, dry_b)
    write_wav(os.path.join(out_dir, "S1_master_clipped.wav"), master, PHONE_RATE)
    guess_b = naive_master_subtraction(master, dry_a)
    print(f"  拍1 母带={int(master[1])}（真实和 40000 已削顶）；"
          f"母带作差猜乙={int(guess_b[1])}，乙真实干声={int(dry_b[1])}")
    assert int(master[1]) == 32767 and int(guess_b[1]) == 12767
    # 独立来线还原不受削顶影响
    recon = manifest.reconstruct(reg)
    assert [l["value"] for l in recon[("A", 1)]] == [20000]
    assert [l["value"] for l in recon[("B", 1)]] == [20000]
    print("  按发布记录逐层还原：甲线拍1=20000、乙线拍1=20000（与母带无关）")

    # 班次 1a：两条独立回路、零延迟
    shift2 = ShiftSpec("S1-two-routes", ["c1", "c2"], max_delay_beats=0, n_beats=n)
    plan = _run_shift(console, manifest, shift2, None, None, store, out_dir)
    assert plan["receipts"] == []
    for ear in EARS:
        assert console.verify_ear_render(
            manifest, plan, store, f"{shift2.shift_id}-ear{ear}", ear)["rendered"][1] == 20000
        # 实际重读的两段回放：两只耳机各导一版 WAV（数据仓区间读取后导出）
        store.export_wav(
            f"{shift2.shift_id}-ear{ear}",
            os.path.join(out_dir, f"replay_S1_ear{ear}.wav"))
    _reopen_verify(console, manifest_path, os.path.join(out_dir, "archive"),
                   os.path.join(out_dir, "plan_S1-two-routes.json"),
                   reg.root, n)

    # 班次 1b：只把乙干声改成 10000（新素材、新发布；旧 render 不动）
    dry_b2 = _pcm([0, 10000, 0, 0, 0, 0, 0, 0])
    obs2 = build_line_pcms({"A": dry_a, "B": dry_b2}, echoes=[])
    assets2 = register_show_assets(reg, {"A": dry_a, "B": dry_b2}, obs2,
                                   note_prefix="S1b-")
    manifest2 = build_manifest("S1b", assets2, routes=[], echoes=[], registry=reg)
    manifest2.save(os.path.join(out_dir, "wiring_S1b.json"))
    shift2b = ShiftSpec("S1b-b10000", ["c1", "c2"], 0, n)
    plan2 = _run_shift(console, manifest2, shift2b, None, None, store, out_dir)
    ear_a = console.verify_ear_render(
        manifest2, plan2, store, "S1b-b10000-earA", "A")
    ear_b = console.verify_ear_render(
        manifest2, plan2, store, "S1b-b10000-earB", "B")
    assert ear_a["rendered"][1] == 10000 and ear_b["rendered"][1] == 20000
    print("  乙干声 20000->10000：甲耳拍1 跟着变为 10000，乙耳仍为甲的 20000")
    # 旧 render 原样保留（已播轨不重写）
    old = store.read_samples("S1-two-routes-earA", 0, n)
    assert int(old[1]) == 20000
    print("  旧 render S1-two-routes-earA 拍1 仍=20000（历史发布不可重写）")

    # 班次 1c：收回一条独立回路，两端禁止延迟 -> 不能同时交付
    shift1c = ShiftSpec("S1c-one-route-nowait", ["c1"], 0, n)
    plan3 = _run_shift(console, manifest, shift1c, None, None, store, out_dir)
    assert len(plan3["receipts"]) == 1
    assert plan3["receipts"][0]["reason_code"] == "no_route_rights"
    delivered_ears = {d["ear"] for d in plan3["deliveries"]}
    assert delivered_ears == {"A"}  # 确定次序：先到的甲耳问句拿到唯一回路
    print("  单回路+禁延迟：仅一只耳可交付，另一只收到缺路权回执")

    # 班次 1d：同样单回路，但允许拖后一拍
    shift1d = ShiftSpec("S1d-one-route-delay1", ["c1"], 1, n)
    plan4 = _run_shift(console, manifest, shift1d, None, None, store, out_dir)
    assert plan4["receipts"] == []
    pcm_a = store.read_samples("S1d-one-route-delay1-earA", 0, n)
    pcm_b = store.read_samples("S1d-one-route-delay1-earB", 0, n)
    assert int(pcm_a[1]) == 20000 and int(pcm_b[2]) == 20000 and int(pcm_b[1]) == 0
    print("  单回路+允许拖后一拍：两耳都交付，乙耳问句在拍2 播（延迟1拍，窗口内）")


# ============================ S2：已知接线、整数拍延迟、撞值不合并 ============================ #

def scenario_2(reg: AssetRegistry, console: ReturnConsole, store: ArchiveStore,
               out_dir: str) -> None:
    _line("S2 已知接线+整数拍延迟：拍0甲7、拍1乙线=干声3+折返7=10；撞值仍留乙的7")
    n = 8
    dry_a = _pcm([7, 0, 0, 0, 0, 0, 0, 0])
    dry_b = _pcm([0, 3, 0, 0, 0, 0, 0, 0])
    routes = [Route("r1", source_guest="A", target_ear="B", loopback_delay=1)]
    echoes = [EchoSpec(target_line="B", beat=1, source_guest="A",
                       source_beat=0, route_id="r1")]
    observed = build_line_pcms({"A": dry_a, "B": dry_b}, echoes)
    assert int(observed["B"][0]) == 0 and int(observed["B"][1]) == 10
    assets = register_show_assets(reg, {"A": dry_a, "B": dry_b}, observed,
                                  note_prefix="S2-")
    manifest = build_manifest("S2", assets, routes, echoes, registry=reg)
    manifest_path = os.path.join(out_dir, "wiring_S2.json")
    manifest.save(manifest_path)
    manifest.validate(reg)

    # 逐层核对：乙线拍1 两层轨（乙干声3 + 甲折返7），来源/回路/延迟齐全
    layers_b1 = manifest.reconstruct(reg)[("B", 1)]
    assert {(l["kind"], l["value"], l["source_guest"], l["source_beat"],
             l["route_id"], l["route_delay"]) for l in layers_b1} == {
        ("dry", 3, "B", 1, None, None),
        ("echo", 7, "A", 0, "r1", 1),
    }
    print("  乙线拍1 观测=10 = 干声层3(乙,拍1) + 折返层7(甲,拍0,回路r1,延迟1)")
    status = manifest.loopback_status()
    assert next(s for s in status if s["guest"] == "A" and s["source_beat"] == 0) == {
        "guest": "A", "source_beat": 0, "returned": True, "via_route": "r1",
        "arrived_line": "B", "arrived_beat": 1, "delay": 1,
    }
    print("  折返核对：甲拍0 的7 已经 r1 延迟1拍折返至乙线拍1")

    shift = ShiftSpec("S2-live", ["c1", "c2"], 0, n)
    plan = _run_shift(console, manifest, shift, None, None, store, out_dir)
    assert plan["receipts"] == []
    ear_a = console.verify_ear_render(manifest, plan, store, "S2-live-earA", "A")
    ear_b = console.verify_ear_render(manifest, plan, store, "S2-live-earB", "B")
    # 甲耳只听到乙本次的3（拍1）；乙耳听到甲的7（拍0），不重听已收过的7
    assert ear_a["rendered"] == [0, 3, 0, 0, 0, 0, 0, 0]
    assert ear_b["rendered"] == [7, 0, 0, 0, 0, 0, 0, 0]
    print("  甲耳逐拍=", ear_a["rendered"], "（只有乙本次的3，听不到甲自己的7）")
    print("  乙耳逐拍=", ear_b["rendered"], "（甲的7只出现一次，拍1不重听）")
    _reopen_verify(console, manifest_path,
                   os.path.join(out_dir, "archive"),
                   os.path.join(out_dir, "plan_S2-live.json"), reg.root, n)

    # 另存一版：乙干声恰为7，与折返撞值——属于乙的7必须作为独立层轨留下
    dry_b_eq = _pcm([0, 7, 0, 0, 0, 0, 0, 0])
    obs_eq = build_line_pcms({"A": dry_a, "B": dry_b_eq}, echoes)
    assert int(obs_eq["B"][1]) == 14
    assets_eq = register_show_assets(reg, {"A": dry_a, "B": dry_b_eq}, obs_eq,
                                     note_prefix="S2-eq-")
    manifest_eq = build_manifest("S2-eq", assets_eq, routes, echoes, registry=reg)
    manifest_eq.save(os.path.join(out_dir, "wiring_S2_eq7.json"))
    manifest_eq.validate(reg)
    layers_eq = manifest_eq.reconstruct(reg)[("B", 1)]
    kinds = sorted((l["kind"], l["value"], l["source_guest"]) for l in layers_eq)
    assert kinds == [("dry", 7, "B"), ("echo", 7, "A")]
    plan_eq = console.plan(
        manifest_eq, ShiftSpec("S2-eq-live", ["c1", "c2"], 0, n))
    console.save_plan(plan_eq, os.path.join(out_dir, "plan_S2-eq-live.json"))
    ear_a_eq = console.expected_ear_pcm(
        manifest_eq, plan_eq, reg, "A")  # 甲耳听到乙的7（独立身份，未被折返吞掉）
    assert int(ear_a_eq[1]) == 7
    print("  撞值版乙线拍1=14=乙干声7+甲折返7：两层轨同值不同身份均保留；"
          "甲耳仍收到属于乙的那个7")


# ============================ S3：迟到问句、尾样保护与路权签认 ============================ #

def scenario_3(reg: AssetRegistry, console: ReturnConsole, store: ArchiveStore,
               out_dir: str) -> None:
    _line("S3 迟到问句换线：尾样未播完 + 第二条路整场订走；可晚一拍 vs 无等候窗口")
    n = 8
    # 当班实况：甲拍0说7；乙拍1说3，另有拍3的迟到问句（晚到，手动提交）
    dry_a = _pcm([7, 0, 0, 0, 0, 0, 0, 0])
    dry_b = _pcm([0, 3, 0, 9, 0, 0, 0, 0])
    routes = [Route("r1", "A", "B", loopback_delay=1)]
    echoes = [EchoSpec("B", 1, "A", 0, "r1")]
    observed = build_line_pcms({"A": dry_a, "B": dry_b}, echoes)
    assets = register_show_assets(reg, {"A": dry_a, "B": dry_b}, observed,
                                  note_prefix="S3-")
    manifest = build_manifest("S3", assets, routes, echoes, registry=reg)
    manifest.save(os.path.join(out_dir, "wiring_S3.json"))

    # 前班订走：c1 给乙耳的未播完尾样（占 [3,4)，与迟到问句同拍到达），
    # c2 整场被前班订走
    def _booked() -> CircuitLedger:
        ledger = CircuitLedger(["c1", "c2"], n)
        ledger.reserve("c1", 3, 4, "B", "前班遗留：乙耳未播完尾样")
        ledger.reserve_entire_show("c2", "B", purpose="前班整场订走")
        return ledger

    # 同一条迟到一拍的乙问句：乙拍3 说话（此时 c1 尾样仍在路上），送甲耳
    late_q = DeliveryJob(job_id="job-late-B3->A", ear="A", source_guest="B",
                         lo=3, hi=4, arrives_beat=3, max_delay=1)
    # 常规作业不含该片段（它是迟到手动提交的问句，exactly-once 仍只排一次）
    skip = {("B", 3, 4)}

    # 班次 3a：允许拖后一拍 -> 签这次换线，旧尾样先按原预约送完
    shift_ok = ShiftSpec("S3-late-ok", ["c1", "c2"], 1, n)
    plan_ok = console.plan(manifest, shift_ok, pre_booked=_booked(),
                           extra_jobs=[late_q], skip_segments=skip)
    console.save_plan(plan_ok, os.path.join(out_dir, "plan_S3-late-ok.json"))
    _save_ledger(plan_ok, shift_ok, out_dir)
    for ear in EARS:
        console.render_ear(manifest, plan_ok, ear, store,
                           render_id=f"S3-late-ok-ear{ear}",
                           product_path=os.path.join(
                               out_dir, f"product_S3-late-ok_{ear}.json"))
        assert console.verify_ear_render(
            manifest, plan_ok, store, f"S3-late-ok-ear{ear}", ear)["ok"]
        print(f"  {GUEST_CN[ear]}耳 render=S3-late-ok-ear{ear} 逐拍="
              f"{store.read_samples(f'S3-late-ok-ear{ear}', 0, n).tolist()}")
    for d in plan_ok["deliveries"]:
        print(f"  [投递] {GUEST_CN[d['ear']]}耳 <- {GUEST_CN[d['source_guest']]} "
              f"源[{d['source_lo']},{d['source_hi']}) 回路{d['route_id']} "
              f"换路拍={d['switch_at']} 播放[{d['play_lo']},{d['play_hi']}) "
              f"延迟={d['delay']}拍")
    for r in plan_ok["receipts"]:
        print(f"  [回执] {r['missing']} | {r['detail']}")
    assert plan_ok["receipts"] == []
    # 前班的两条预约原样保留（本台不重写已订路权，尾样必送）
    saved = CircuitLedger.load(os.path.join(out_dir, "ledger_S3-late-ok.json"))
    assert any(r.route_id == "c1" and (r.lo, r.hi) == (3, 4) and r.ear == "B"
               for r in saved.reservations)
    assert any(r.route_id == "c2" and (r.lo, r.hi) == (0, n) and r.ear == "B"
               for r in saved.reservations)
    late = [d for d in plan_ok["deliveries"] if d["job_id"] == "job-late-B3->A"]
    assert len(late) == 1 and late[0]["route_id"] == "c1"
    assert late[0]["switch_at"] == 4 and late[0]["play_lo"] == 4 and late[0]["delay"] == 1
    pcm_a = store.read_samples("S3-late-ok-earA", 0, n)
    pcm_b = store.read_samples("S3-late-ok-earB", 0, n)
    # 甲耳：乙拍1的3 零延迟到拍1；迟到问句晚一拍于拍4；拍3 保持空（尾样占用）
    assert [int(v) for v in pcm_a] == [0, 3, 0, 0, 9, 0, 0, 0]
    # 乙耳：甲拍0的7 走乙耳整场预约的 c2，拍0 即播；尾样拍3 属前班内容不覆盖
    assert [int(v) for v in pcm_b] == [7, 0, 0, 0, 0, 0, 0, 0]
    print("  签认：c1 拍3 尾样按原预约给乙耳送完，拍4 拍界换耳，"
          "乙问句拍4 进甲耳（晚1拍，窗口内）；c2 整场不可借")

    # 班次 3b：无等候窗口（max_delay=0）-> 无法签，回执注明缺路权
    shift_no = ShiftSpec("S3-late-nowait", ["c1", "c2"], 0, n)
    plan_no = console.plan(manifest, shift_no, pre_booked=_booked(),
                           extra_jobs=[DeliveryJob(
                               job_id="job-late-B3->A", ear="A", source_guest="B",
                               lo=3, hi=4, arrives_beat=3, max_delay=0)],
                           skip_segments=skip)
    console.save_plan(plan_no, os.path.join(out_dir, "plan_S3-late-nowait.json"))
    r = next(x for x in plan_no["receipts"] if x["job_id"] == "job-late-B3->A")
    assert r["reason_code"] == "no_route_rights"
    print(f"  [无等候回执] {r['missing']}；{r['detail']}")
    print("  （c1 拍3 尾样未播完不能抢占，c2 整场订走，禁延迟 => 不签换线）")


# ============================ S4：缺来线回执 ============================ #

def scenario_4(reg: AssetRegistry, console: ReturnConsole,
               out_dir: str) -> None:
    _line("S4 缺来线：源嘉宾没有独立干声来线 -> missing_line 回执，不占路权")
    n = 8
    dry_a = _pcm([1, 2, 0, 0, 0, 0, 0, 0])
    dry_b = _pcm([0, 0, 0, 0, 0, 0, 0, 0])
    observed = build_line_pcms({"A": dry_a, "B": dry_b}, echoes=[])
    assets = register_show_assets(reg, {"A": dry_a, "B": dry_b}, observed,
                                  note_prefix="S4-")
    manifest = build_manifest("S4", assets, routes=[], echoes=[], registry=reg)
    manifest.save(os.path.join(out_dir, "wiring_S4.json"))
    # 伪造一个引用不存在来线 C 的作业（模拟换新来线但无独立 PCM）
    ghost = DeliveryJob(job_id="job-C2->A", ear="A", source_guest="C",
                        lo=2, hi=3, arrives_beat=2, max_delay=0)
    plan = console.plan(manifest, ShiftSpec("S4-shift", ["c1", "c2"], 0, n),
                        extra_jobs=[ghost])
    receipt = plan["receipts"][0]
    assert receipt["reason_code"] == "missing_line" and receipt["source_guest"] == "C"
    # 缺来线不占用任何回路：甲自己的问句仍可正常排程
    assert any(d["ear"] == "B" for d in plan["deliveries"])
    print(f"  [回执] {receipt['missing']} | {receipt['detail']}")
    print("  缺来线作业不写路权簿，其余来线的交付不受影响")


def main() -> None:
    shutil.rmtree(BASE, ignore_errors=True)
    reg_dir = os.path.join(BASE, "registry")
    out_dir = os.path.join(BASE, "products")
    arch_dir = os.path.join(out_dir, "archive")
    os.makedirs(out_dir, exist_ok=True)
    reg = AssetRegistry(reg_dir)
    store = ArchiveStore(arch_dir)
    console = ReturnConsole(reg, out_dir)

    scenario_1(reg, console, store, out_dir)
    scenario_2(reg, console, store, out_dir)
    scenario_3(reg, console, store, out_dir)
    scenario_4(reg, console, out_dir)

    # 数据仓重开总检：全部 render 可读、块完整性通过、总数在 8 拍规模内
    store2 = ArchiveStore(arch_dir)
    renders = store2.list_renders()
    assert all(m["n_samples"] == 8 for m in renders)
    store2.verify_block_pcm_integrity()
    store2.close()
    _line(f"全部场景通过：{len(renders)} 条逐耳 render，每条 8 拍；"
          f"产品/计划/路权簿/接线记录均在 {BASE}")


if __name__ == "__main__":
    main()
