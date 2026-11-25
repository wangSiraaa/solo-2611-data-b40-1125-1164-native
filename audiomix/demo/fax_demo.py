"""气象传真制版端到端演示（可独立复跑）。

走完整条真实链路：合成声带 -> 发布 WAV -> 登记素材 -> 时间轴裁切/变速/混行 ->
数据仓渲染与块级追溯 -> 重读解图 -> 制版入口开馆 -> 按展次查回 -> 两工坊回放。
用法：python3 -m demo.fax_demo [输出目录]
"""
import json
import os
import shutil
import sys

from audiomix.fax import (
    HIGH as H, LOW as L, Reel, Segment, publish_reel, load_published_reel,
    read_reel_waveform, possible_grids,
)
from audiomix.board import Crop, Marker, reliable_closed_labels
from audiomix.plate import IntakeDesk, PlateOrder
from audiomix.assets import AssetRegistry
from audiomix.archive import ArchiveStore
from audiomix import faxedit
from audiomix.craft import run_all_replays


def main(root: str = "/tmp/audiomix_fax_demo") -> None:
    # 数据仓 render_id 不可变，可复跑脚本自带干净工作区
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    print("=" * 64)
    print("气象传真声带制版端到端演示（真实发布/渲染/重读）")
    print("=" * 64)

    # 1) 原题 3x3：外围全高、正中低
    ring = [[H, H, H], [H, L, H], [H, H, H]]
    wav = os.path.join(root, "ring.wav")
    pub = publish_reel(Reel("RING-3x3", [Segment("s1", rows=ring, phase=0)]), wav)
    print(f"\n[1] 发布声带 {wav}（{pub['sample_count']} 样本，摘要 {pub['pcm_digest'][:12]}…）")
    _, pcm, meta = load_published_reel(wav)
    reads = read_reel_waveform(pcm, meta["reel"])
    print("    重读候选相位:", reads[0].candidate_phases,
          "| 仍可能读法数:", len(possible_grids(reads)))

    # 2) 时间轴编辑三件套（真实 timeline + archive）
    reg = AssetRegistry(os.path.join(root, "registry"))
    arc = ArchiveStore(os.path.join(root, "archive"))
    reel = Reel.from_json(meta["reel"])
    aid = faxedit.register_reel_asset(reg, reel, wav)
    cut = faxedit.cut_reel(
        reel, aid, reg, arc, drop_rows=1,
        out_wav=os.path.join(root, "cut.wav"), render_id="CUT")
    spd = faxedit.speed_reel(
        reel, aid, reg, arc, speed=2,
        out_wav=os.path.join(root, "speed.wav"), render_id="SPEED2")
    mix = faxedit.mix_rerecorded_line(
        reel, aid, reg, arc, row_index=1, new_line=[H, H, H],
        out_wav=os.path.join(root, "mix.wav"), render_id="MIX")
    print("\n[2] 时间轴编辑（块级来源可追溯）:")
    for r, name in ((cut, "裁切"), (spd, "变速"), (mix, "混录一行")):
        print(f"    {name}: render={r.render_id} 出处步骤="
              f"{[p['step'] for p in r.provenance]}")

    # 3) 制版入口：完整底图 vs 裁顶行
    desk = IntakeDesk(os.path.join(root, "desk"))
    full = desk.accept(PlateOrder(
        "full-3x3", wav, exhibition="甲展",
        markers=[Marker("中心槽", 1, 1, label_slot=True)]))
    cropped = desk.accept(PlateOrder(
        "cropped-3x3", wav, crop=Crop(top=1), exhibition="甲展"))
    print("\n[3] 开馆单:")
    print("    完整底图可见签:", [l["name"] for l in full.labels_visible],
          "| 观众中心读到:", full.cell_readings[1][1])
    print("    裁顶行后可见签:", [l["name"] for l in cropped.labels_visible],
          "| 原中心现在是边点，读到:", cropped.cell_readings[0][1])

    # 4) 边际相同、相位约束不同的卷对
    cal = [H, H, H, H, L]
    gq = [[H] * 5 for _ in range(5)]
    gq[2][2] = L; gq[2][3] = L; gq[1][0] = L
    gq[1][1] = L; gq[1][2] = H; gq[1][3] = L
    qpath = os.path.join(root, "anti.wav")
    publish_reel(Reel("Q-ANTI", [Segment("s", rows=gq, phase=0,
                                         calibration=cal,
                                         calibration_corrupted={0, 4})]), qpath)
    _, qpcm, qmeta = load_published_reel(qpath)
    Gq = possible_grids(read_reel_waveform(qpcm, qmeta["reel"]))
    gp = [[H] * 5 for _ in range(5)]
    gp[2][2] = L; gp[2][3] = L; gp[1][0] = L; gp[1][3] = L
    gp[1][1] = H; gp[1][2] = L; gp[0][2] = H; gp[0][3] = L
    ppath = os.path.join(root, "sync.wav")
    publish_reel(Reel("P-SYNC", [Segment("s", rows=gp, phase=0,
                                         calibration=cal,
                                         calibration_corrupted={0, 4})]), ppath)
    _, ppcm, pmeta = load_published_reel(ppath)
    Gp = possible_grids(read_reel_waveform(ppcm, pmeta["reel"]))
    print("\n[4] 逐格边际相同、相位约束不同的卷对:")
    print("    反相卷可贴签:", [l.name for l in reliable_closed_labels(Gq, [])])
    print("    同步卷可贴签:", [l.name for l in reliable_closed_labels(Gp, [])])

    # 5) 两工坊回放
    print("\n[5] 两工坊窗口前/后回放:")
    for craft, rows in run_all_replays().items():
        for r in rows:
            print(f"    {craft:8s} {r['order_timing']:14s} "
                  f"甲见签={r['jia_visitor_sees_closure']!s:5s} "
                  f"乙见签={r['yi_visitor_sees_closure']!s:5s} "
                  f"签终址={r['label_final_location']}")
    print("\n演示产物目录:", root)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_fax_demo")
