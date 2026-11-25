"""气象传真制版子系统测试（真实发布/渲染/重读，不使用任何外部结果）。

覆盖：
1. 短音脉冲编码与小字典（H/L/漏读）、真实 WAV 发布与摘要校验、重读解图；
2. 行相位：同一录段共用一种相位、候选相位枚举、校准条收窄与零证据全候选；
3. 题面三个 3x3 闭合判定（可签 / 低值连边不可签 / 裁顶行中心成边点不可签）；
4. 时间轴裁切、变速、混入重录一行（走真实 timeline/archive，块级来源可追溯）；
5. 逐格可能读数相同、但行内相位约束不同的两幅卷，可贴结果相反；
6. 制版入口受理短传真/换裁边单、按展次查回观众可见签与原卷依据；
7. 两家工坊（压印/游签）窗口前/后两次回放。
"""
import os
import shutil

from audiomix.fax import (
    HIGH, LOW, MISSING, Reel, Segment, publish_reel, load_published_reel,
    read_reel_waveform, possible_grids, cell_possible_values,
)
from audiomix.board import (
    Crop, Marker, reliable_closed_labels, low_components,
    component_touches_edge,
)
from audiomix.plate import IntakeDesk, PlateOrder
from audiomix.assets import AssetRegistry
from audiomix.archive import ArchiveStore
from audiomix import faxedit
from audiomix.craft import run_all_replays

TMP = "/tmp/audiomix_fax_verify"
H, L = HIGH, LOW


def _fresh():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)


def _publish(reel, name):
    path = os.path.join(TMP, name)
    publish_reel(reel, path)
    return path


def _grids(path):
    _, pcm, meta = load_published_reel(path)
    return possible_grids(read_reel_waveform(pcm, meta["reel"]))


# ----------------------------- 1. 物理层 ----------------------------- #
def test_publish_reread_roundtrip():
    _fresh()
    grid = [[H, H, H], [H, L, H], [H, H, H]]
    path = _publish(Reel("ROUND", [Segment("s", rows=grid, phase=0)]), "r.wav")
    reel, pcm, meta = load_published_reel(path)
    assert reel.reel_id == "ROUND"
    assert meta["pcm_digest"] and len(pcm) == meta["sample_count"]
    grids = _grids(path)
    assert grids == [grid]
    print("[OK] 真实 WAV 发布/重读往返，格值与卷号一致")


def test_dictionary_missing_is_not_value():
    _fresh()
    grid = [[H, H, H], [H, L, H], [H, H, H]]
    # 中心脉冲被读坏 -> 漏读；枚举时该格在 H/L 间展开，MISSING 不是第四种值
    seg = Segment("s", rows=grid, phase=0, corrupted={(1, 1)})
    path = _publish(Reel("MISS", [seg]), "m.wav")
    _, pcm, meta = load_published_reel(path)
    reads = read_reel_waveform(pcm, meta["reel"])
    assert reads[0].observed_rows[1][1] == MISSING
    grids = possible_grids(reads)
    assert {g[1][1] for g in grids} == {H, L}
    assert all(MISSING not in sum(g, []) for g in grids)
    print("[OK] 漏读按 H/L 枚举，不是第四种格值")


def test_phase_shared_per_segment_and_locked():
    _fresh()
    grid = [[H, H, H], [H, L, H], [H, H, H]]
    for phase in (0, 1, 2):
        path = _publish(Reel(f"P{phase}", [Segment("s", rows=grid, phase=phase)]),
                        f"p{phase}.wav")
        grids = _grids(path)
        # 真实发射相位在读法集合内（相位被校准条正确恢复或作为候选保留）
        assert grid in grids
    # 校准槽全部读坏 -> 零对齐证据 -> 全部相位仍可能，绝不硬猜
    seg = Segment("s", rows=grid, phase=0, calibration_corrupted={0, 1, 2})
    path = _publish(Reel("ZERO", [seg]), "z.wav")
    _, pcm, meta = load_published_reel(path)
    reads = read_reel_waveform(pcm, meta["reel"])
    assert reads[0].candidate_phases == [0, 1, 2]
    print("[OK] 段内共用一种相位；校准条锁相/收窄/零证据全候选")


# ----------------------------- 2. 三个 3x3 判定 ----------------------------- #
def test_three_canonical_closures():
    _fresh()
    desk = IntakeDesk(os.path.join(TMP, "desk"))
    ring = [[H, H, H], [H, L, H], [H, H, H]]
    p1 = _publish(Reel("RING", [Segment("s", rows=ring, phase=0)]), "ring.wav")
    s1 = desk.accept(PlateOrder(
        "o1", p1, markers=[Marker("center", 1, 1, label_slot=True)], exhibition="E"))
    assert len(s1.labels_visible) == 1
    assert s1.labels_visible[0]["anchor"] == [1, 1]

    open_top = [[H, L, H], [H, L, H], [H, H, H]]
    p2 = _publish(Reel("OPEN", [Segment("s", rows=open_top, phase=0)]), "open.wav")
    s2 = desk.accept(PlateOrder("o2", p2, exhibition="E"))
    assert s2.labels_visible == []
    grids = _grids(p2)
    comp = [c for c in low_components(grids[0]) if (1, 1) in c][0]
    assert component_touches_edge(comp, 3, 3)

    # 恢复原声、裁去顶行：原中心 (1,1) 变成新图 (0,1) 边点
    s3 = desk.accept(PlateOrder("o3", p1, crop=Crop(top=1), exhibition="E"))
    assert s3.dimensions == [2, 3]
    assert s3.cell_readings[0][1] == [L]      # 中心现在是顶边点
    assert s3.labels_visible == []
    print("[OK] 3x3：中心可签；低值连边不可签；裁顶行中心成边点不可签")


def test_one_label_per_component_and_evidence_per_reading():
    _fresh()
    # 两个互不相邻的内部低值块，各至多一签；每签保留每种读法依据
    grid = [[H, H, H, H, H],
            [H, L, H, L, H],
            [H, H, H, H, H],
            [H, L, H, L, H],
            [H, H, H, H, H]]
    path = _publish(Reel("MULTI", [Segment("s", rows=grid, phase=0)]), "multi.wav")
    labels = reliable_closed_labels(_grids(path), [])
    assert len(labels) == 4                    # 四个独立闭合块各一签
    for lab in labels:
        assert all(ev.closed for ev in lab.readings)
    print("[OK] 同一连通块最多一签，且每签保留每种读法依据")


# ----------------------------- 3. 真实时间轴编辑 ----------------------------- #
def test_cut_speed_mix_with_provenance():
    _fresh()
    grid = [[H, H, H], [H, L, H], [H, H, H]]
    reel = Reel("EDIT", [Segment("s", rows=grid, phase=0)])
    wav = _publish(reel, "orig.wav")
    reg = AssetRegistry(os.path.join(TMP, "reg"))
    arc = ArchiveStore(os.path.join(TMP, "arc"))
    aid = faxedit.register_reel_asset(reg, reel, wav)

    cut = faxedit.cut_reel(reel, aid, reg, arc, drop_rows=1,
                           out_wav=os.path.join(TMP, "cut.wav"), render_id="CUT")
    assert len(cut.reel.segments[0].rows) == 2
    rr = faxedit.reread_published(os.path.join(TMP, "cut.wav"), arc, "CUT")
    assert rr["block_trace"][0]["clip_ids"] == ["kept"]
    assert cut.provenance[0]["step"] == "cut"

    spd = faxedit.speed_reel(reel, aid, reg, arc, speed=2,
                             out_wav=os.path.join(TMP, "spd.wav"), render_id="SPD")
    rr2 = faxedit.reread_published(os.path.join(TMP, "spd.wav"), arc, "SPD")
    assert rr2["segment_reads"][0]["candidate_phases"] == [0]
    assert {p["step"] for p in spd.provenance} >= {"speed", "resample", "render"}

    mix = faxedit.mix_rerecorded_line(
        reel, aid, reg, arc, row_index=1, new_line=[H, H, H],
        out_wav=os.path.join(TMP, "mix.wav"), render_id="MIX")
    _, pcm, meta = load_published_reel(os.path.join(TMP, "mix.wav"))
    g = possible_grids(read_reel_waveform(pcm, meta["reel"]))
    assert g[0][1][1] == H                    # 重录行替换了中心低
    rr3 = faxedit.reread_published(os.path.join(TMP, "mix.wav"), arc, "MIX")
    assets = {a for t in rr3["block_trace"] for a in t["asset_ids"]}
    assert len(assets) == 2                   # 原声 + 重录行两条来源都在块索引里
    print("[OK] 裁切/变速/混行走真实时间轴与数据仓，卷号与样本出处完整")


# ----------------------------- 4. 边际相同、联合约束不同的卷对 ----------------------------- #
def test_same_marginals_different_phase_constraints():
    _fresh()
    cal = [H, H, H, H, L]
    corrupt = {0, 4}                          # 候选相位 {0,1}

    # Q（行内共享槽反相）：b1=col1 源(s1,s2)，b2=col2 源(s2,s3)，s1=s3=L,s2=H
    #   -> (b1,b2) ∈ {(L,H),(H,L)}，永不双双 L -> 中心在所有读法闭合 -> 可靠可签
    gq = [[H] * 5 for _ in range(5)]
    gq[2][2] = L; gq[2][3] = L                # 中心两相位恒 L
    gq[1][0] = L                              # 固定左边点
    gq[1][1] = L; gq[1][2] = H; gq[1][3] = L
    q_path = _publish(Reel("Q-anti-phase", [
        Segment("s", rows=gq, phase=0, calibration=cal,
                calibration_corrupted=corrupt)]), "q.wav")
    Gq = _grids(q_path)
    combos_q = {(g[1][1], g[1][2]) for g in Gq}
    assert combos_q == {(L, H), (H, L)}
    assert reliable_closed_labels(Gq, [])

    # P（段共相位同步）：b1、b2 分处两行，随同一相位一起翻 -> 含 (L,L) 同步读法，
    # 该读法下中心经低值串联触边 -> 不可靠、不可签。
    gp = [[H] * 5 for _ in range(5)]
    gp[2][2] = L; gp[2][3] = L
    gp[1][0] = L; gp[1][3] = L
    gp[1][1] = H; gp[1][2] = L                # b1 随相位 {H,L}
    gp[0][2] = H; gp[0][3] = L                # b2 随相位 {H,L}，与 b1 同步
    p_path = _publish(Reel("P-shared-phase", [
        Segment("s", rows=gp, phase=0, calibration=cal,
                calibration_corrupted=corrupt)]), "p.wav")
    Gp = _grids(p_path)
    # 两卷关键不确定格的逐格边际都恰为 {H,L}
    mq, mp = cell_possible_values(Gq), cell_possible_values(Gp)
    assert mq[1][1] == mq[1][2] == {H, L}
    assert mp[1][1] == {H, L} and mp[0][2] == {H, L}
    assert not reliable_closed_labels(Gp, [])
    print("[OK] 逐格边际同为 {H,L}，行内反相可签、段内同步含 LL 不可签")


# ----------------------------- 5. 入口与按展次查询 ----------------------------- #
def test_intake_short_fax_and_recrop():
    _fresh()
    desk = IntakeDesk(os.path.join(TMP, "catalog"))
    # 馆员另发一卷短传真（2x2），同一入口受理
    short = [[L, H], [H, H]]
    ps = _publish(Reel("SHORT", [Segment("s", rows=short, phase=0)]), "short.wav")
    sa = desk.accept(PlateOrder("short-1", ps, exhibition="展次甲"))
    assert sa.dimensions == [2, 2]
    # 换一张裁边单（同一卷、不同裁框），新受理号
    ring = [[H, H, H], [H, L, H], [H, H, H]]
    pr = _publish(Reel("RING", [Segment("s", rows=ring, phase=0)]), "ring.wav")
    desk.accept(PlateOrder("full", pr, exhibition="展次乙",
                           markers=[Marker("c", 1, 1, label_slot=True)]))
    desk.accept(PlateOrder("cropped", pr, crop=Crop(top=1), exhibition="展次乙"))
    yi = desk.by_exhibition("展次乙")
    assert {s.order_id for s in yi} == {"full", "cropped"}
    visible = [s for s in yi if s.labels_visible]
    assert [s.order_id for s in visible] == ["full"]
    # 旧目录按原展次仍可查回，且带原卷依据（摘要 + 段重读）
    got = desk.get("full")
    assert got.provenance["pcm_digest"] and got.provenance["segment_reads"]
    print("[OK] 短传真/换裁边单同一入口受理，按展次查回可见签与原卷依据")


def test_label_permit_gate():
    _fresh()
    desk = IntakeDesk(os.path.join(TMP, "cat2"))
    ring = [[H, H, H], [H, L, H], [H, H, H]]
    pr = _publish(Reel("RING", [Segment("s", rows=ring, phase=0)]), "ring.wav")
    ok = desk.accept(PlateOrder("permit", pr, labels_permitted=True, exhibition="X"))
    no = desk.accept(PlateOrder("denied", pr, labels_permitted=False, exhibition="X"))
    assert ok.labels_visible and no.labels_visible == []
    assert no.labels                  # 判读仍算出可靠签，只是本场不亮
    print("[OK] 用签许可控制观众是否见签，判读依据保留")


# ----------------------------- 6. 工坊回放 ----------------------------- #
def test_workshop_replays():
    res = run_all_replays()
    flt = {r["order_timing"]: r for r in res["floating"]}
    # 游签、窗口前：甲无签、乙拿到签；窗口后：甲锁签、乙无签
    assert flt["before_window"]["jia_visitor_sees_closure"] is False
    assert flt["before_window"]["yi_visitor_sees_closure"] is True
    assert flt["before_window"]["label_final_location"] == "plate:YI"
    assert flt["after_window"]["jia_visitor_sees_closure"] is True
    assert flt["after_window"]["yi_visitor_sees_closure"] is False
    # 压印：签随旧版封存，绝不到乙
    for r in res["press"]:
        assert r["yi_visitor_sees_closure"] is False
        assert r["label_final_location"] == "sealed:JIA"
    print("[OK] 压印/游签各回放窗口前/后，观众所见、封存与签终址正确")


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    for fn in ALL:
        fn()
    print(f"\n================ 传真制版 {len(ALL)} 组测试全部通过 ================")


if __name__ == "__main__":
    main()
