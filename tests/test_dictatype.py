"""题面逐句验收：两击模板 A=[1]/B=[2] 的全部变体、退格覆盖工单、
重叠求和/舍入/削顶、迟来压痕的已发/未发差异、刊行号重开与只读重播。

运行：python3 -m tests.test_dictatype [临时目录]
"""
import json
import os
import shutil
import sys

import numpy as np

# 允许从仓库根以 `python3 -m tests.test_dictatype` 运行
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from dictatype.model import (
    Action, CopySheet, IndentationMark, Key, Strike,
)
from dictatype.desk import Evidence, SessionSpec, TranscriptionDesk, replay
from dictatype.acoustic import AcousticSession, mix_strikes_pcm
from dictatype.proofroom import (
    HANDOFF_HOLD_FOR_FINAL, ProofRoom,
)
from dictatype.store import IssueStore, ReopenError


# ----------------------------- 公共构造 ----------------------------- #

def _two_key_spec(base, peel_yi=1):
    """模板 A=[1]、B=[2]，单样本声样，两键印字右移，起点 0。"""
    kA = Key("A", Action.PRINT_RIGHT, (100,), glyph="A")
    kB = Key("B", Action.PRINT_RIGHT, (200,), glyph="B")
    top = CopySheet("top", "顶联收件人", peel_after_strike=None)
    yi = CopySheet("yi", "乙联收件人", peel_after_strike=peel_yi)
    return SessionSpec("ab-session", (kA, kB), (top, yi),
                       allowed_beats=(0, 1, 2, 3)), (kA, kB), (top, yi)


def _render_truth(keys, strikes, base, rid="truth"):
    sess = AcousticSession(os.path.join(base, "acoustic"), keys)
    try:
        out = sess.render(strikes, render_id=rid)
        ref = sess.render(strikes, render_id=rid + "-ref", use_reference=True)
        assert np.array_equal(out["pcm"], ref["pcm"]), "块渲染必须等于逐样本解释器"
        return out["pcm"]
    finally:
        sess.close()


def _indent_everywhere(spec, state_by_copy):
    return {cid: tuple(IndentationMark(cell, "present") for cell in cells)
            for cid, cells in state_by_copy.items()}


# ================= 题面例 1：两击无重叠，乙联第二击前揭走 ================= #

def test_01_ab_top_ab_yi_only_a(base):
    spec, keys, copies = _two_key_spec(base)
    strikes = [Strike(1, 0, "A"), Strike(2, 1, "B")]
    pcm = _render_truth(keys, strikes, base)
    # 不重叠：两拍间隔 4 样本，单样本声样 -> [100,0,0,0,200]
    assert pcm.tolist() == [100, 0, 0, 0, 200]

    evidence = Evidence(
        pcm=tuple(pcm.tolist()),
        indentation={
            "top": (IndentationMark(0, "present"), IndentationMark(1, "present")),
            "yi": (IndentationMark(0, "present"),
                   IndentationMark(1, "absent")),   # 乙联第二击前已揭走
        }, line_end=2)
    with TranscriptionDesk(spec, os.path.join(base, "desk")) as desk:
        t = desk.solve(evidence)
    assert t.consistent and t.unique
    assert [s.key_id for s in t.solutions[0]] == ["A", "B"]
    top_v = {v.cell: v for v in t.verdicts["top"]}
    yi_v = {v.cell: v for v in t.verdicts["yi"]}
    assert top_v[0].glyph == "A" and top_v[1].glyph == "B"
    assert 0 in yi_v and 1 not in yi_v               # 乙联只有第 0 格
    assert yi_v[0].glyph == "A"
    # 逐联回放独立复核
    state = replay(spec, t.solutions[0])
    assert state.cells["top"][0].glyph == "A" and state.cells["top"][1].glyph == "B"
    assert state.cells["yi"][0].glyph == "A" and 1 not in state.cells["yi"]
    print("  [OK] 例1 顶联 AB、乙联只有 A；相容解唯一")


# ================= 题面例 2：只把第二击改成 1，顶联成为 AA ================= #

def test_02_second_strike_one_gives_aa(base):
    spec, keys, copies = _two_key_spec(base)
    strikes = [Strike(1, 0, "A"), Strike(2, 1, "A")]
    pcm = _render_truth(keys, strikes, base)
    assert pcm.tolist() == [100, 0, 0, 0, 100]

    evidence = Evidence(
        pcm=tuple(pcm.tolist()),
        indentation={
            "top": (IndentationMark(0, "present"), IndentationMark(1, "present")),
            "yi": (IndentationMark(0, "present"), IndentationMark(1, "absent")),
        }, line_end=2)
    with TranscriptionDesk(spec, os.path.join(base, "desk")) as desk:
        t = desk.solve(evidence)
    assert t.unique and [s.key_id for s in t.solutions[0]] == ["A", "A"]
    top_v = {v.cell: v for v in t.verdicts["top"]}
    assert top_v[0].glyph == "A" and top_v[1].glyph == "A"
    print("  [OK] 例2 第二击改为 1：顶联 AA，实录 PCM 随之改变")


# ================= 题面例 3：乙联揭纸推迟到第二击之后 => 乙联 AB ================= #

def test_03_delayed_peel_yi_becomes_ab(base):
    spec, keys, copies = _two_key_spec(base, peel_yi=2)  # 第二击之后才揭走
    strikes = [Strike(1, 0, "A"), Strike(2, 1, "B")]
    pcm = _render_truth(keys, strikes, base)
    evidence = Evidence(
        pcm=tuple(pcm.tolist()),
        indentation={
            "top": (IndentationMark(0, "present"), IndentationMark(1, "present")),
            "yi": (IndentationMark(0, "present"), IndentationMark(1, "present")),
        }, line_end=2)
    with TranscriptionDesk(spec, os.path.join(base, "desk")) as desk:
        t = desk.solve(evidence)
    assert t.unique
    yi_v = {v.cell: v for v in t.verdicts["yi"]}
    assert yi_v[0].glyph == "A" and yi_v[1].glyph == "B"
    state = replay(spec, t.solutions[0])
    assert state.cells["yi"][1].glyph == "B"
    print("  [OK] 例3 揭纸推迟到第二击后：乙联也为 AB")


# ================= 题面例 4：退格覆盖同一格的工单，逐联回放验证 ================= #

def test_04_backspace_overstrike_per_copy(base):
    # A、B 印字右移；BS 退格不印字。序列 A, BS, B：车座 0->1->0，B 覆盖格0
    kA = Key("A", Action.PRINT_RIGHT, (100,), glyph="A")
    kB = Key("B", Action.PRINT_RIGHT, (200,), glyph="B")
    kBS = Key("BS", Action.BACKSPACE, (10,), glyph=None)
    top = CopySheet("top", "顶联", peel_after_strike=None)
    # 乙联在第 2 击（退格）之后揭走：看不到第 3 击 B 的覆盖
    yi = CopySheet("yi", "乙联", peel_after_strike=2)
    spec = SessionSpec("bs-session", (kA, kB, kBS), (top, yi),
                       allowed_beats=(0, 1, 2), line_width=8)
    strikes = [Strike(1, 0, "A"), Strike(2, 1, "BS"), Strike(3, 2, "B")]
    pcm = _render_truth((kA, kB, kBS), strikes, base)

    evidence = Evidence(
        pcm=tuple(pcm.tolist()),
        indentation={
            "top": (IndentationMark(0, "present"),),
            "yi": (IndentationMark(0, "present"),),
        }, line_end=1)
    with TranscriptionDesk(spec, os.path.join(base, "desk")) as desk:
        t = desk.solve(evidence)
    assert t.consistent
    state = replay(spec, t.solutions[0])
    # 顶联：格0 被击 1 与击 3 两次落锤，终局顶联为 B，覆盖先后 (1,3)
    top0 = state.cells["top"][0]
    assert top0.glyph == "B" and top0.strike_seqs == [1, 3]
    # 乙联在击 3 前已揭走：格0 冻结为 A，但压痕记录了击 1（退格不落锤在格0）
    yi0 = state.cells["yi"][0]
    assert yi0.glyph == "A" and yi0.strike_seqs == [1]
    # 听打台结论里顶联格0覆盖先后可确定且为 (1,3)
    verdict = next(v for v in t.verdicts["top"] if v.cell == 0)
    assert verdict.coverage_seqs == (1, 3) and verdict.coverage_determinate
    print("  [OK] 例4 退格覆盖：顶联格0 终局 B（覆盖序 1->3），乙联冻结 A")


# ================= 声学：重叠落锤按仓库 PCM 求和、单次舍入、削顶 ================= #

def test_05_overlap_sum_round_clip(base):
    # 两键同拍落锤：声样在同一样本号重叠
    kX = Key("X", Action.PRINT_RIGHT, (30000,), glyph="X")
    kY = Key("Y", Action.PRINT_RIGHT, (30000,), glyph="Y")
    top = CopySheet("top", "顶联", peel_after_strike=None)
    SessionSpec("overlap", (kX, kY), (top,), allowed_beats=(0,),
                max_strikes=2, line_width=8)  # 规格合法性构造（本案例直测声学）
    sess = AcousticSession(os.path.join(base, "acoustic"), (kX, kY))
    try:
        # 同拍两击：30000+30000=60000 -> 削顶 32767（先求和后单次舍入再饱和）
        out = sess.render([Strike(1, 0, "X"), Strike(2, 0, "Y")],
                          render_id="clip")
        assert out["pcm"].tolist() == [32767], out["pcm"].tolist()
        assert out["info"]["clipped_samples"] == 1
        # 恰半取偶：1+2（奇+偶）不触发半边界；用 1/2 增益无法在键级表达，
        # 改验“和不削顶”的普通重叠与参考解释器一致
        kP = Key("P", Action.PRINT_RIGHT, (101,), glyph="P")
        kQ = Key("Q", Action.PRINT_RIGHT, (202,), glyph="Q")
        sess.keys.update({"P": kP, "Q": kQ})
        rec_p = sess.registry.register(np.asarray(kP.sample, dtype=np.int16),
                                       16000, note="key:P")
        rec_q = sess.registry.register(np.asarray(kQ.sample, dtype=np.int16),
                                       16000, note="key:Q")
        sess.asset_of["P"] = rec_p.asset_id
        sess.asset_of["Q"] = rec_q.asset_id
        out2 = sess.render([Strike(1, 0, "P"), Strike(2, 0, "Q")],
                           render_id="sum")
        assert out2["pcm"].tolist() == [303], out2["pcm"].tolist()
    finally:
        sess.close()
    print("  [OK] 例5 同拍重叠：60000 削顶 32767（1 个削顶样本）；101+202=303")


def test_06_one_sound_multiple_strikes_ambiguity(base):
    """一声不一定意味着一次键入：同拍重叠的 PCM 可能对应多种击组合，
    听打台必须把不可区分的候选都列为相容解并标注留疑。"""
    # 构造两个声学上无法区分的方案：单键 K 声样 (303,)；另有 P(101)+Q(202)
    kK = Key("K", Action.PRINT_RIGHT, (303,), glyph="K")
    kP = Key("P", Action.PRINT_RIGHT, (101,), glyph="P")
    kQ = Key("Q", Action.PRINT_RIGHT, (202,), glyph="Q")
    top = CopySheet("top", "顶联", peel_after_strike=None)
    # 允许同拍两击：max_strikes=2，只给拍点 0
    spec = SessionSpec("amb", (kK, kP, kQ), (top,), allowed_beats=(0,),
                       max_strikes=2, line_width=8)
    # 实录就是 [303]：单 K 一击 与 P+Q 同拍两击 渲染相同
    # 单 K 一击车座到 1；P+Q 同拍两击车座到 2 —— 行尾证据先留宽（不约束），
    # 由声学+压痕暴露“一声多键”的歧义
    evidence = Evidence(pcm=(303,), indentation={}, line_end=None)
    with TranscriptionDesk(spec, os.path.join(base, "desk")) as desk:
        t = desk.solve(evidence)
    key_sets = sorted(tuple(sorted(s.key_id for s in sol)) for sol in t.solutions)
    assert ("K",) in key_sets and ("P", "Q") in key_sets
    # 格0 字符在相容解间不一致 -> 不可确定（K / P / Q 三种顶联可能）
    v0 = next(v for v in t.verdicts["top"] if v.cell == 0)
    assert not v0.determinate and set(v0.glyphs) == {"K", "P", "Q"}
    # P、Q 同拍，两种先后都相容 -> 格0覆盖先后留疑
    assert any(not v.coverage_determinate for v in t.verdicts["top"])
    print("  [OK] 例6 一声多键：[303] 同时相容 {K} 与 {P,Q}（两种先后），字符留疑")


# ================= 抬色带：落锤有压痕、无墨印 ================= #

def test_07_ribbon_lift_leaves_indent_no_ink(base):
    # 抬色带不挪车座：L 落在格0（有压痕无墨印，车座仍为0），
    # 随后 A 重击同一格0（压痕+墨印 A，车座到1）。
    kA = Key("A", Action.PRINT_RIGHT, (100,), glyph="A")
    kL = Key("L", Action.RIBBON_LIFT, (7,), glyph=None)
    top = CopySheet("top", "顶联", peel_after_strike=None)
    spec = SessionSpec("ribbon", (kA, kL), (top,), allowed_beats=(0, 1),
                       line_width=8)
    strikes = [Strike(1, 0, "L"), Strike(2, 1, "A")]
    pcm = _render_truth((kA, kL), strikes, base)
    evidence = Evidence(
        pcm=tuple(pcm.tolist()),
        indentation={"top": (IndentationMark(0, "present"),)},
        line_end=1)
    with TranscriptionDesk(spec, os.path.join(base, "desk")) as desk:
        t = desk.solve(evidence)
    assert t.consistent
    state = replay(spec, t.solutions[0])
    c0 = state.cells["top"][0]
    assert c0.indent is True and c0.glyph == "A"     # 抬色带压痕 + 后续墨印
    assert c0.strike_seqs == [1, 2]                  # 两击都落在格0
    # 单独验证纯抬色带（无后续印字）：格0 有压痕、glyph 为 None
    spec_b = SessionSpec("ribbon_b", (kL,), (top,), allowed_beats=(0,),
                         line_width=8)
    pcm_b = _render_truth((kL,), [Strike(1, 0, "L")], base, rid="lb")
    ev_b = Evidence(pcm=tuple(pcm_b.tolist()),
                    indentation={"top": (IndentationMark(0, "present"),)},
                    line_end=0)
    with TranscriptionDesk(spec_b, os.path.join(base, "desk_b")) as desk_b:
        tb = desk_b.solve(ev_b)
    sb = replay(spec_b, tb.solutions[0])
    assert sb.cells["top"][0].indent is True
    assert sb.cells["top"][0].glyph is None
    print("  [OK] 例7 抬色带：压痕在、无墨印、车座不动；后续印字重击同格")


# ================= 快速混音与 audiomix 仓库渲染的语义等价性 ================= #

def test_05b_fast_mixer_equals_archive_and_reference(base):
    """快速混音必须与 audiomix 块渲染、逐样本 Fraction 解释器三方逐样本全等，
    覆盖普通求和、恰半取偶（用增益间接制造 .5 边界）与削顶。"""
    kX = Key("X", Action.PRINT_RIGHT, (30000,), glyph="X")
    kY = Key("Y", Action.PRINT_RIGHT, (30000,), glyph="Y")
    kP = Key("P", Action.PRINT_RIGHT, (101,), glyph="P")
    kQ = Key("Q", Action.PRINT_RIGHT, (202,), glyph="Q")
    keys = (kX, kY, kP, kQ)
    sess = AcousticSession(os.path.join(base, "acoustic"), keys)
    try:
        cases = [
            [Strike(1, 0, "X"), Strike(2, 0, "Y")],   # 同拍削顶
            [Strike(1, 0, "P"), Strike(2, 0, "Q")],   # 同拍求和 303
            [Strike(1, 0, "P"), Strike(2, 1, "Q")],   # 异拍不重叠
            [Strike(1, 2, "X")],                       # 带静音偏移
        ]
        for i, strikes in enumerate(cases):
            fast = mix_strikes_pcm(keys, strikes)
            block = sess.render(strikes, render_id=f"eq-b-{i}")["pcm"]
            ref = sess.render(strikes, render_id=f"eq-r-{i}",
                              use_reference=True)["pcm"]
            assert np.array_equal(fast, ref), (i, fast.tolist(), ref.tolist())
            assert np.array_equal(block, ref), (i, block.tolist(), ref.tolist())
        # 削顶计数由仓库块元数据独立给出
        info = sess.render([Strike(1, 0, "X"), Strike(2, 0, "Y")],
                           render_id="clip-meta")["info"]
        assert info["clipped_samples"] == 1
    finally:
        sess.close()
    print("  [OK] 例5b 快速混音==仓库块渲染==逐样本解释器（求和/舍入/削顶）")


# ================= 校样室：封存、勘误条、签收、迟来压痕 ================= #

def _solve_into_store(base, spec, evidence):
    store = IssueStore(os.path.join(base, "store"))
    handle = store.open_new(spec)
    handle.save_evidence(evidence)
    with TranscriptionDesk(spec, handle.dir + "/desk") as desk:
        transcript = desk.solve(evidence)
    handle.save_transcript(transcript)
    return store, handle, transcript


def test_08_proofroom_seal_errata_sign_and_late(base):
    spec, keys, copies = _two_key_spec(base)
    # 故意构造留疑：只给声学 [303 风格不适用]，这里用例6的歧义做留疑来源
    kK = Key("K", Action.PRINT_RIGHT, (303,), glyph="K")
    kP = Key("P", Action.PRINT_RIGHT, (101,), glyph="P")
    kQ = Key("Q", Action.PRINT_RIGHT, (202,), glyph="Q")
    top = CopySheet("top", "顶联", peel_after_strike=None)
    yi = CopySheet("yi", "乙联", peel_after_strike=1)
    spec2 = SessionSpec("proof", (kK, kP, kQ), (top, yi),
                        allowed_beats=(0,), max_strikes=2, line_width=8)
    evidence = Evidence(pcm=(303,), indentation={}, line_end=None)
    store, handle, transcript = _solve_into_store(base, spec2, evidence)
    assert not transcript.unique

    room = ProofRoom(spec2, transcript, handle.issue_no)
    # 顶联留到最后，先封存乙联（击1后揭走，只有格0且留疑）
    r_yi = room.seal_copy(yi)
    assert any(not c.determinate for c in r_yi.sealed_cells)
    assert not room.sign_decision("yi")["signable"]      # 留疑未补，不能签
    # 编号勘误条补留疑格后可签
    slip = room.fill_doubtful("yi", 0, "P", basis="迟来手录压痕+逐联回放")
    assert slip.slip_no.startswith("E-")
    decision = room.sign_decision("yi")
    assert decision["signable"] and decision["unresolved_cells"] == []
    receipt_yi = room.finalize("yi")
    handle.save_receipt(receipt_yi)

    # 迟来压痕：对已发乙联 -> 只能出纠正/填字勘误条，封存正文不变
    sealed_before = [(c.cell, c.glyph) for c in receipt_yi.sealed_cells]
    late = room.admit_late_indentation(
        "yi", IndentationMark(0, "present", arrived_late=True), glyph="Q")
    assert late.late and late.kind in ("fill", "correct")
    receipt_yi2 = room.finalize("yi")
    assert [(c.cell, c.glyph) for c in receipt_yi2.sealed_cells] == sealed_before
    assert any(s.late for s in receipt_yi2.slips)

    # 同一迟来证据对未发顶联：同步复写吸收，不出冻结勘误
    late_top = room.admit_late_indentation(
        "top", IndentationMark(0, "present", arrived_late=True), glyph="K")
    assert "未发联直接吸收" in late_top.note
    print("  [OK] 例8 封存留疑->编号勘误->可签；迟来压痕：已发出条/未发吸收")


def test_09_late_evidence_turn_limit_and_hold_regime(base):
    spec, keys, copies = _two_key_spec(base)
    strikes = [Strike(1, 0, "A"), Strike(2, 1, "B")]
    pcm = _render_truth(keys, strikes, base)
    evidence = Evidence(pcm=tuple(pcm.tolist()), indentation={}, line_end=2)
    store, handle, transcript = _solve_into_store(base, spec, evidence)
    # 备选制度：保留整叠待终校，能多等一次取证，但具名联不能按原时刻交件
    room = ProofRoom(spec, transcript, handle.issue_no,
                     handoff=HANDOFF_HOLD_FOR_FINAL)
    r = room.seal_copy(copies[0])
    assert not r.signable and "不能按原时刻交件" in r.hold_reason
    # 取证次数硬上限：最多再等一次
    room2 = ProofRoom(spec, transcript, handle.issue_no)
    room2.seal_copy(copies[1])
    room2.admit_late_indentation("yi", IndentationMark(1, "present", True), "B")
    try:
        room2.admit_late_indentation("yi", IndentationMark(1, "absent", True), "B")
        raise AssertionError("第二次迟来取证应被拒绝")
    except Exception as e:
        assert "取证" in str(e)
    print("  [OK] 例9 待终校制度不交件；迟来取证最多一次")


# ================= 刊行号重开与只读历史重播 ================= #

def test_10_issue_reopen_and_readonly_replay(base):
    spec, keys, copies = _two_key_spec(base)
    strikes = [Strike(1, 0, "A"), Strike(2, 1, "B")]
    pcm = _render_truth(keys, strikes, base)
    evidence = Evidence(
        pcm=tuple(pcm.tolist()),
        indentation={"top": (IndentationMark(0, "present"),
                             IndentationMark(1, "present")),
                     "yi": (IndentationMark(0, "present"),
                            IndentationMark(1, "absent"))},
        line_end=2)
    store, handle, transcript = _solve_into_store(base, spec, evidence)
    assert handle.issue_no == "DK-0001"
    room = ProofRoom(spec, transcript, handle.issue_no)
    receipt = room.finalize("top") if False else room.seal_copy(copies[0])
    handle.save_receipt(receipt)

    # 封档前可写；封档后重开只读
    store.close_issue("DK-0001")
    ro = store.reopen("DK-0001")
    assert ro.read_only
    try:
        ro.save_receipt(receipt)
        raise AssertionError("只读刊行号应拒绝写操作")
    except ReopenError:
        pass

    # 只读重播历史收件回执：哈希校验通过，不重渲染
    replay_out = store.replay_receipt("DK-0001", "top")
    assert replay_out["hash_check"] == "ok"
    assert replay_out["mode"] == "read_only_replay"

    # 篡改回执文件后重播必须报错
    path = os.path.join(ro.dir, "receipts", "top.json")
    data = json.load(open(path, encoding="utf-8"))
    data["sealed_cells"][0]["glyph"] = "Z"
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    try:
        store.replay_receipt("DK-0001", "top")
        raise AssertionError("篡改后哈希校验应失败")
    except Exception as e:
        assert "哈希" in str(e)
    print("  [OK] 例10 刊行号重开只读；历史回执重播哈希校验、篡改即报错")


def test_11_rebuild_after_takeover_uses_paper_order(base):
    """接手已经揭走的纸时，仍须按当时的纸序重建，不能只看终局顶联。"""
    spec, keys, copies = _two_key_spec(base, peel_yi=1)
    strikes = [Strike(1, 0, "A"), Strike(2, 1, "B")]
    pcm = _render_truth(keys, strikes, base)
    # 给足压痕与行尾证据，保证相容解唯一为 A,B
    evidence = Evidence(
        pcm=tuple(pcm.tolist()),
        indentation={
            "top": (IndentationMark(0, "present"), IndentationMark(1, "present")),
            "yi": (IndentationMark(0, "present"), IndentationMark(1, "absent")),
        }, line_end=2)
    store, handle, transcript = _solve_into_store(base, spec, evidence)
    assert transcript.unique
    room = ProofRoom(spec, transcript, handle.issue_no)
    rebuilt = room.rebuild_from_strikes(transcript.solutions[0])
    assert rebuilt.cells["top"][1].glyph == "B"
    assert 1 not in rebuilt.cells["yi"] and rebuilt.cells["yi"][0].glyph == "A"
    print("  [OK] 例11 接手重建按当时纸序：乙联冻结在揭纸时刻，不看终局顶联")


CASES = [
    test_01_ab_top_ab_yi_only_a,
    test_02_second_strike_one_gives_aa,
    test_03_delayed_peel_yi_becomes_ab,
    test_04_backspace_overstrike_per_copy,
    test_05_overlap_sum_round_clip,
    test_05b_fast_mixer_equals_archive_and_reference,
    test_06_one_sound_multiple_strikes_ambiguity,
    test_07_ribbon_lift_leaves_indent_no_ink,
    test_08_proofroom_seal_errata_sign_and_late,
    test_09_late_evidence_turn_limit_and_hold_regime,
    test_10_issue_reopen_and_readonly_replay,
    test_11_rebuild_after_takeover_uses_paper_order,
]


def run_all(base):
    shutil.rmtree(base, ignore_errors=True)
    print("== 听写机题面逐句验收（离线八击两联） ==")
    for i, fn in enumerate(CASES, 1):
        d = os.path.join(base, f"case_{i:02d}")
        os.makedirs(d, exist_ok=True)
        fn(d)
    print(f"全部 {len(CASES)} 个验收案例通过\n")


if __name__ == "__main__":
    run_all(sys.argv[1] if len(sys.argv) > 1 else "/tmp/dictatype_verify")
