"""对板 / 放映投影 / 放映单 / 制度对比 的语义测试。

核心复现题面“试片”段落：

- 十个录音样本为一格；卷内每胶片帧推进一格；待校起点可能为 0 或 1。
- 第 1 帧板标只可能对到唯一板响格 2 -> 起点 = 1，格 3 的台词 -> 第 2 帧。
- 把渲染改到格 1、2 各有一声同板且二者均可能 -> 不能确保台词在第 2 帧。
- 原样带只把该帧改为院方静音帧 -> 同样交不了这句。
"""
from fractions import Fraction as F

import numpy as np

from audiomix.assets import AssetRegistry, sequence
from audiomix.archive import ArchiveStore
from audiomix.clapper import TemplateBook, template, SAMPLES_PER_CELL, cell_count
from audiomix.film import DialogueLine, FilmProject, FrameMark, Reel
from audiomix.recording import Recording
from audiomix.regimes import (
    EdgeStripRegime, LiveSyncRegime, LockBoundary, detect_with_new_bang,
    plan_remedy,
)
from audiomix.sync import project as sync_project, solve as sync_solve
from audiomix.venue import Playback, PlaybackError, ScreeningSheet

RATE = 16000
CELL = SAMPLES_PER_CELL


# --------------------------------------------------------------------- #
# 合成录音：在 16kHz PCM 里按格摆放板响模板
# --------------------------------------------------------------------- #
PAT_A = template("A", [300, -300, 1000, -1000, 300, -300])
PAT_B = template("B", [111, 222, 333, 444, 555, 666])


def make_recording_pcm(total_cells: int, bangs: dict[int, list[int]],
                       noise: int = 0, seed: int = 7) -> np.ndarray:
    """bangs: {pattern_index: [cell,...]}，其中 0->A、1->B。其余格填低噪正弦。"""
    rng = np.random.default_rng(seed)
    n = total_cells * CELL
    x = (rng.integers(-noise, noise + 1, size=n) if noise > 0
         else np.zeros(n, dtype=np.int64)).astype(np.int64)
    for pat_idx, cells in bangs.items():
        tmpl = (PAT_A if pat_idx == 0 else PAT_B).samples
        for c in cells:
            s = c * CELL
            x[s:s + len(tmpl)] = tmpl
    return x.astype(np.int16)


def _project():
    """一卷 6 帧、10 格；待校起点 {0,1}；第 1 帧有板标 A。"""
    reel = Reel("R1", frames=6, tape_cells=10, start_candidates=(0, 1))
    return FilmProject(
        film_id="试片",
        gears=(F(1),),                      # 每胶片帧推进一格
        reels=(reel,),
        frame_marks=(FrameMark("R1", 1, "A"),),
        lines=(DialogueLine("L1", cell=3, allowed_lo=2, allowed_hi=2,
                            text="格3的台词"),),
    )


def test_worked_example():
    proj = _project()
    dets = {"A": [2]}
    per_reel = sync_solve(proj, dets)
    sols = per_reel["R1"]
    assert len(sols) == 1 and sols[0].offset == 1 and sols[0].gear == 1, \
        f"应唯一得起点 1：{[ (s.gear,s.offset) for s in sols ]}"
    projres = sync_project(proj, dets)
    lp = projres.lines["L1"]
    assert lp.possible_frames == (2,), lp.possible_frames
    assert lp.deliverable and projres.deliverable_lines() == ["L1"]
    print("  [OK] 板标->唯一板响格2 => 起点1，格3台词->第2帧，可交付")


def test_two_bangs_ambiguous():
    proj = _project()
    dets = {"A": [1, 2]}       # 格1、格2 各有一声同板，二者均可能
    per_reel = sync_solve(proj, dets)
    sols = per_reel["R1"]
    assert {s.offset for s in sols} == {0, 1}, \
        f"应得两种起点：{[(s.gear,s.offset) for s in sols]}"
    projres = sync_project(proj, dets)
    lp = projres.lines["L1"]
    assert set(lp.possible_frames) == {2, 3}, lp.possible_frames
    assert not lp.deliverable and projres.undeliverable_lines() == ["L1"]
    print("  [OK] 格1、2 各一声同板 => 台词可为第2或第3帧，不能确保第2帧")


def test_silent_locked_frame_fails():
    reel = Reel("R1", frames=6, tape_cells=10, start_candidates=(0, 1))
    proj = FilmProject(
        film_id="试片", gears=(F(1),), reels=(reel,),
        frame_marks=(FrameMark("R1", 1, "A"),),
        lines=(DialogueLine("L1", cell=3, allowed_lo=2, allowed_hi=2),),
        silent_locked_frames=frozenset({2}),     # 院方锁定第2帧为静音
    )
    dets = {"A": [2]}
    projres = sync_project(proj, dets)
    lp = projres.lines["L1"]
    assert lp.possible_frames == (2,)               # 解释仍是第2帧
    assert not lp.deliverable                       # 但静音帧不可交付
    print("  [OK] 原样带把该帧改为院方静音帧 => 同样交不了这句")


def test_templates_and_detection():
    book = TemplateBook([PAT_A, PAT_B])
    assert book.patterns == ["A", "B"]
    pcm = make_recording_pcm(10, {0: [2], 1: [5]}, noise=3)
    det = book.detect(pcm)
    assert det["A"] == [2] and det["B"] == [5], det
    bangs = book.bangs(pcm)
    assert bangs == [(2, "A"), (5, "B")], bangs
    # 模板跨格不应误配：把 A 放错格首应不检测
    bad = make_recording_pcm(10, {})
    s = 2 * CELL + 3
    bad[s:s + len(PAT_A.samples)] = np.array(PAT_A.samples, dtype=np.int16)
    assert TemplateBook([PAT_A]).detect(bad)["A"] == [], "非格首不应匹配"
    print("  [OK] 板响模板整格精确检测；非格首不误配")


# --------------------------------------------------------------------- #
# 录音原件 = 可重开的 16kHz archive render
# --------------------------------------------------------------------- #
def _build_archive(tmp_path, bangs, total_cells=12):
    reg = AssetRegistry(tmp_path / "reg")
    store = ArchiveStore(tmp_path / "arch")
    pcm = make_recording_pcm(total_cells, bangs, noise=0)
    # 把 PCM 切成每块 1600 样本的素材，逐段入时间轴拼回（验证块追溯）
    from audiomix.timeline import TimelineEditor
    ed = TimelineEditor(tmp_path / "reg", reg, name="rec")
    n = len(pcm)
    block = 1600
    for i, s in enumerate(range(0, n, block)):
        seg = pcm[s:s + block]
        rec = reg.register(seg, RATE, note=f"seg{i}")
        ed.insert({"clip_id": f"seg{i}", "asset_id": rec.asset_id,
                   "a": 0, "b": len(seg), "start": F(s, RATE)})
    product = ed.publish()
    store.render(product, F(n, RATE),
                 {a["asset_id"]: reg.load_pcm(a["asset_id"])
                  for a in product["assets"]}, render_id="rec1")
    return reg, store


def test_recording_reopen_and_provenance(tmp_path):
    reg, store = _build_archive(tmp_path, {0: [2]})
    # 重开：同一 render 两次读取逐样本相同
    rec = Recording(store, "rec1")
    a = rec.pcm()
    b = Recording(ArchiveStore(tmp_path / "arch"), "rec1").pcm()
    assert np.array_equal(a, b)
    book = TemplateBook([PAT_A])
    hits = rec.bangs_with_provenance(book)
    assert len(hits) == 1
    h = hits[0]
    assert h.cell == 2 and h.pattern == "A"
    assert h.block_index is not None and h.block_key is not None
    assert h.clip_ids, "板响应可追溯到具体 clip"
    # 格2 -> 样本20..26，落在第0块（0..1600）
    assert h.sample_start == 2 * CELL and h.block_index == 0
    print("  [OK] archive render 重开逐样本一致；板响可追溯到块/clip/素材")


def test_solve_from_archive(tmp_path):
    reg, store = _build_archive(tmp_path, {0: [2]})
    proj = _project()
    dets = Recording(store, "rec1").detect(TemplateBook([PAT_A]))
    assert dets["A"] == [2]
    res = sync_project(proj, dets)
    assert res.lines["L1"].deliverable
    print("  [OK] 从真实归档录音读板响并求解，台词可交付")


# --------------------------------------------------------------------- #
# 放映单 / 按帧查询 / 重开 / 旧放映单复查
# --------------------------------------------------------------------- #
def test_screening_sheet_and_reopen(tmp_path):
    proj = _project()
    dets = {"A": [2]}
    chosen = {rid: sorted(sols, key=lambda s: (s.gear_index, s.offset))[0]
              for rid, sols in sync_solve(proj, dets).items()}
    sheet = ScreeningSheet(proj, chosen)
    e2 = sheet.entry(2)
    assert e2.local_frame == 2 and e2.line_ids == ("L1",)
    # 第2帧对应录音格 3（offset1 + f2）
    assert e2.cell == 3, e2.cell
    assert e2.sample_range == (30, 40), e2.sample_range
    # 导出放映单，重开并复查按帧查询
    path = sheet.save_manifest(tmp_path / "sheet.json")
    import json
    man = json.load(open(path))
    assert man["frame_entries"][2]["line_ids"] == ["L1"]
    assert man["reel_boundaries"] == [0]
    # 同一工程重开重算，放映单必须逐条相同（确定性）
    proj2 = FilmProject.load(proj.save(tmp_path / "proj.json"))
    chosen2 = {rid: sorted(sols, key=lambda s: (s.gear_index, s.offset))[0]
               for rid, sols in sync_solve(proj2, dets).items()}
    sheet2 = ScreeningSheet(proj2, chosen2)
    assert sheet2.to_manifest() == sheet.to_manifest(), "重开应得到同一放映单"
    print("  [OK] 放映单按帧查询 / 存盘重开 / 同工程重算逐条一致")


def test_playback_gear_change_at_boundary():
    # 两卷：卷1 已放帧，卷2 未放；换速只能在卷界（卷2首帧前）
    r1 = Reel("R1", frames=3, tape_cells=6, start_candidates=(1,))
    r2 = Reel("R2", frames=3, tape_cells=6, start_candidates=(0, 1))
    proj = FilmProject(
        film_id="两卷", gears=(F(1), F(2)), reels=(r1, r2),
        frame_marks=(FrameMark("R1", 1, "A"),),
    )
    dets = {"A": [0 + 1]}   # 卷1 tape_base=0，offset1 时 frame1->cell2? 这里直接给检测
    # 让卷1唯一解为 offset1（板标 frame1 -> 检测格 2）
    dets = {"A": [2]}
    chosen = {"R1": sync_solve(proj, dets)["R1"][0],
              "R2": sync_solve(proj, dets)["R2"][0]}
    sheet = ScreeningSheet(proj, chosen)
    pb = Playback(sheet)
    pb.play(0, 3)                       # 放映卷1全部
    # 卷2换速允许（尚无已放帧）
    pb.change_gear("R2", gear_index=1, offset=0)
    assert sheet.chosen["R2"].gear == F(2)
    # 已放帧保留：仍包含 0,1,2
    assert {0, 1, 2} <= pb.played
    # 卷1已放帧 -> 不能在其卷界后回改
    try:
        pb.change_gear("R1", gear_index=1, offset=1)
        raise AssertionError("卷1已有已放帧，换速应被拒绝")
    except PlaybackError:
        pass
    print("  [OK] 卷界可换速、已放帧保留；已放卷不可再换速")


def test_playback_fill_frame_rules():
    r1 = Reel("R1", frames=5, tape_cells=8, start_candidates=(1,))
    proj = FilmProject(
        film_id="补帧", gears=(F(1),), reels=(r1,),
        frame_marks=(FrameMark("R1", 1, "A"),),
        fillable_frames=frozenset({3, 4}),
        silent_locked_frames=frozenset({4}),
    )
    dets = {"A": [2]}
    sol = sync_solve(proj, dets)["R1"][0]
    sheet = ScreeningSheet(proj, {"R1": sol})
    pb = Playback(sheet)
    pb.fill_frame(3)                    # 可补、非静音：允许
    assert 3 in pb.filled
    pb.play(0, 5)
    for bad, why in [(4, "静音帧"), (0, "非可补帧")]:
        try:
            pb.fill_frame(bad)
            raise AssertionError(f"{why}应被拒绝")
        except PlaybackError:
            pass
    try:
        pb.fill_frame(3)
        raise AssertionError("已补/已放帧应被拒绝")
    except PlaybackError:
        pass
    print("  [OK] 只可补标定可补帧、非静音/已放帧；重复补拒绝")


# --------------------------------------------------------------------- #
# 两套制度 + 一次现场才出现的板响
# --------------------------------------------------------------------- #
def _two_reel_project():
    """两卷小片：R1 唯一（板 A 于格2）；R2 待校（板 B，起点候选 {0,1}）。

    卷2 的“台词2”只在全局第 6 帧（R2 局部帧2）可交付。
    """
    r1 = Reel("R1", frames=4, tape_cells=8, start_candidates=(1,))
    r2 = Reel("R2", frames=4, tape_cells=8, start_candidates=(0, 1))
    return FilmProject(
        film_id="两卷小片", gears=(F(1),), reels=(r1, r2),
        frame_marks=(FrameMark("R1", 1, "A"), FrameMark("R2", 1, "B")),
        lines=(
            DialogueLine("台词1", cell=3, allowed_lo=2, allowed_hi=2),
            DialogueLine("台词2", cell=8 + 3, allowed_lo=6, allowed_hi=6),
        ),
        fillable_frames=frozenset({5}),     # 卷2 内一帧可补空白，供现场校正
    )


def test_regimes_with_live_bang():
    proj = _two_reel_project()
    # 初始：A 在格2、B 在格10（R2 局部格2）-> R2 offset=1 唯一，台词2 在第6帧
    dets = {"A": [2], "B": [8 + 2]}
    per = sync_solve(proj, dets)
    assert len(per["R1"]) == 1 and per["R1"][0].offset == 1
    assert len(per["R2"]) == 1 and per["R2"][0].offset == 1, \
        [(s.gear, s.offset) for s in per["R2"]]
    assert sync_project(proj, dets).lines["台词2"].deliverable

    # 现场才出现的一声：B 又在 R2 局部格1（全局格9）出现 -> 起点 0/1 均可
    dets2 = detect_with_new_bang(dets, "B", 8 + 1)
    per2 = sync_solve(proj, dets2)
    assert {s.offset for s in per2["R2"]} == {0, 1}, \
        [(s.gear, s.offset) for s in per2["R2"]]
    open_proj = sync_project(proj, dets2)
    assert not open_proj.lines["台词2"].deliverable, "开放解释下不能确保第6帧"

    # 印死边条 @offset1：新板响后仍唯一落在第6帧 -> 台词2 可交付
    edge1 = EdgeStripRegime({"R2": LockBoundary("R2", 0, 1)})
    assert edge1.assess(proj, dets2)["lines"]["台词2"]["deliverable"]
    # 印死边条 @offset0：台词2 落到第7帧 -> 不可交付，且卷内不能逐句挪、须重印
    edge0 = EdgeStripRegime({"R2": LockBoundary("R2", 0, 0)})
    e0 = edge0.assess(proj, dets2)
    assert not e0["lines"]["台词2"]["deliverable"]
    r0 = plan_remedy(proj, dets2, edge0, e0)
    assert r0["reprint_reels"] == ["R2"] and r0["filler_frames"] == [], r0

    # 现场追板同步机：卷2 未验证（两种解释）-> 须真拿一帧空白校正，消耗可补帧
    live = LiveSyncRegime({})
    l_res = live.assess(proj, dets2)
    assert "R2" in l_res["needs_calibration_reels"], l_res
    l_rem = plan_remedy(proj, dets2, live, l_res)
    assert l_rem["calibration_cost"] == 1 and l_rem["filler_frames"] == [5], l_rem
    print("  [OK] 新板响下：边条@正确偏移免补、@错误偏移须重印；"
          "同步机须拿一帧空白校正")


def test_audio_track_export(tmp_path):
    """按帧导出音轨：每帧取 sample_range 拼成 16k 单声道 WAV，重开可读。"""
    import wave
    proj = _project()
    dets = {"A": [2]}
    chosen = {rid: sorted(sols, key=lambda s: (s.gear_index, s.offset))[0]
              for rid, sols in sync_solve(proj, dets).items()}
    sheet = ScreeningSheet(proj, chosen)
    src = np.arange(200, dtype=np.int16)      # 任意原件（只需可切窗）
    out = str(tmp_path / "track.wav")
    sheet.export_audio_track(src, out)
    with wave.open(out, "rb") as wf:
        assert wf.getframerate() == 16000 and wf.getnchannels() == 1
        got = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    # 每帧窗样本量相加 = 输出长度
    want = sum(b - a for a, b in sheet.frame_audio_windows())
    assert len(got) == want, (len(got), want)
    print("  [OK] 按帧导出相应音轨（16k 单声道），长度=逐帧窗之和")


def test_gear_two_frame_audio_window():
    """gear=2 时一帧占 2 个录音格 = 20 样本，逐帧窗随之放大。"""
    r1 = Reel("R1", frames=3, tape_cells=8, start_candidates=(0,))
    proj = FilmProject(
        film_id="快走", gears=(F(1), F(2)), reels=(r1,),
        frame_marks=(FrameMark("R1", 1, "A"),),
    )
    # gear=2, offset=0: frame1 -> cell 2；板标 A 在格2
    dets = {"A": [2]}
    sols = sync_solve(proj, dets)["R1"]
    assert [(s.gear, s.offset) for s in sols] == [(F(2), 0)], \
        [(s.gear, s.offset) for s in sols]
    sheet = ScreeningSheet(proj, {"R1": sols[0]})
    a, b = sheet.entry(0).sample_range
    assert (a, b) == (0, 20), (a, b)         # 帧0 占格0..2
    a, b = sheet.entry(1).sample_range
    assert (a, b) == (20, 40), (a, b)        # 帧1 占格2..4
    print("  [OK] 换速 gear=2：一帧占两格，逐帧音频窗=20 样本")


def run(tmp_root="/tmp/audiomix_sync"):
    import tempfile
    from pathlib import Path
    test_worked_example()
    test_two_bangs_ambiguous()
    test_silent_locked_frame_fails()
    test_templates_and_detection()
    with tempfile.TemporaryDirectory() as d:
        test_recording_reopen_and_provenance(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_solve_from_archive(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_screening_sheet_and_reopen(Path(d))
    test_playback_gear_change_at_boundary()
    test_playback_fill_frame_rules()
    test_regimes_with_live_bang()
    with tempfile.TemporaryDirectory() as d:
        test_audio_track_export(Path(d))
    test_gear_two_frame_audio_window()
    test_missing_bang_budget_and_unmatched_mark()
    print("对板/放映语义测试全部通过")


def test_missing_bang_budget_and_unmatched_mark():
    """板标必须对到真实板响；同花纹间隙的“未见板响”受合并容差约束。"""
    reel = Reel("R", frames=8, tape_cells=12, start_candidates=(0,))
    # 板标花纹完全没有任何检测 -> 无解（不得静默跳过）
    p_none = FilmProject(film_id="none", gears=(F(1),), reels=(reel,),
                         frame_marks=(FrameMark("R", 2, "A"),))
    assert sync_solve(p_none, {"A": []})["R"] == []
    assert sync_solve(p_none, {})["R"] == []
    # 两板标 frame0/frame2，检测格0/格2 -> 夹 1 个未见板响，容差 2 内 -> 有解
    p_ok = FilmProject(film_id="ok", gears=(F(1),), reels=(reel,),
                       frame_marks=(FrameMark("R", 0, "A"), FrameMark("R", 2, "A")))
    sols = sync_solve(p_ok, {"A": [0, 2]})["R"]
    assert len(sols) == 1 and sols[0].unseen_bangs == 1, sols
    # 间隙 5 个未见板响 > 合并容差 2 -> 无解
    p_big = FilmProject(film_id="big", gears=(F(1),), reels=(reel,),
                        frame_marks=(FrameMark("R", 0, "A"), FrameMark("R", 6, "A")))
    assert sync_solve(p_big, {"A": [0, 6]})["R"] == []
    # 调大容差到 5 -> 同一情形有解
    p_big2 = FilmProject(film_id="big2", gears=(F(1),), reels=(reel,),
                         frame_marks=(FrameMark("R", 0, "A"), FrameMark("R", 6, "A")),
                         max_missing_in_recording=3, max_missing_in_film=2)
    assert len(sync_solve(p_big2, {"A": [0, 6]})["R"]) == 1
    print("  [OK] 板标必配真实板响；同花纹未见板响受合并容差（及各一）约束")


if __name__ == "__main__":
    run()
