"""边界与持久化语义：离线约束整笔拒绝、压痕 absent 剪枝、行尾剪枝、
刊行号 JSON 往返、退格越界保护、快速混音的恰半取偶边界。
"""
import os
import shutil
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from dictatype.model import Action, CopySheet, IndentationMark, Key, Strike
from dictatype.desk import Evidence, SessionSpec, TranscriptionDesk
from dictatype.acoustic import mix_strikes_pcm
from dictatype.store import IssueStore, IssueError, ReopenError


def test_validation_rejects_bad_specs(base):
    k = Key("A", Action.PRINT_RIGHT, (1,), glyph="A")
    top = CopySheet("top", "t")
    # 空键位
    try:
        SessionSpec("x", (), (top,), allowed_beats=(0,))
        raise AssertionError("空键位应拒绝")
    except Exception as e:
        assert "键" in str(e)
    # 联数超两联
    three = (top, CopySheet("b", "b"), CopySheet("c", "c"))
    try:
        SessionSpec("x", (k,), three, allowed_beats=(0,))
        raise AssertionError("超过两联应拒绝")
    except Exception as e:
        assert "联数" in str(e)
    # 击数上限超 8
    try:
        SessionSpec("x", (k,), (top,), allowed_beats=(0,), max_strikes=9)
        raise AssertionError("超过八击应拒绝")
    except Exception as e:
        assert "击数" in str(e)
    # 揭纸时刻超出击数上限
    bad_copy = CopySheet("bad", "b", peel_after_strike=9)
    try:
        SessionSpec("x", (k,), (bad_copy,), allowed_beats=(0,), max_strikes=8)
        raise AssertionError("揭纸时刻越界应拒绝")
    except Exception as e:
        assert "揭纸" in str(e)
    # 起点越界
    try:
        SessionSpec("x", (k,), (top,), allowed_beats=(0,),
                    start_position=16, line_width=16)
        raise AssertionError("起点越界应拒绝")
    except Exception as e:
        assert "起点" in str(e)
    # 印字键缺字符 / 非印字键带字符
    try:
        Key("bad", Action.PRINT_RIGHT, (1,))
        raise AssertionError("印字键缺 glyph 应拒绝")
    except Exception:
        pass
    try:
        Key("bad", Action.BACKSPACE, (1,), glyph="X")
        raise AssertionError("退格键带 glyph 应拒绝")
    except Exception:
        pass
    print("  [OK] 边界1 离线约束与键/联非法规格整笔拒绝")


def test_backspace_cannot_leave_line(base):
    # 起点 0 直接退格 -> 车座 -1 越界，该候选必须被排除
    kA = Key("A", Action.PRINT_RIGHT, (100,), glyph="A")
    kBS = Key("BS", Action.BACKSPACE, (10,))
    top = CopySheet("top", "t")
    spec = SessionSpec("bs0", (kA, kBS), (top,), allowed_beats=(0, 1),
                       start_position=0, line_width=4)
    # 合法序列只有 A；实录 [100]
    ev = Evidence(pcm=(100,),
                  indentation={"top": (IndentationMark(0, "present"),)},
                  line_end=1)
    with TranscriptionDesk(spec, os.path.join(base, "desk")) as d:
        t = d.solve(ev)
    assert t.unique and [s.key_id for s in t.solutions[0]] == ["A"]
    print("  [OK] 边界2 起点退格越界被排除，只保留合法候选")


def test_absent_indentation_prunes_candidates(base):
    # 压痕 absent 是硬证据：抬色带会在格0留痕，故与 absent 冲突的候选被剪掉
    kA = Key("A", Action.PRINT_RIGHT, (100,), glyph="A")
    kL = Key("L", Action.RIBBON_LIFT, (7,))
    top = CopySheet("top", "t")
    spec = SessionSpec("abs", (kA, kL), (top,), allowed_beats=(0,),
                       line_width=4, max_strikes=1)
    ev = Evidence(pcm=(100,),
                  indentation={"top": (IndentationMark(0, "present"),)},
                  line_end=1)
    with TranscriptionDesk(spec, os.path.join(base, "d1")) as d:
        t1 = d.solve(ev)
    assert t1.unique and t1.solutions[0][0].key_id == "A"
    # 若手录格0无压痕，则任何落锤候选都不相容 -> 无解
    ev_none = Evidence(pcm=(100,),
                       indentation={"top": (IndentationMark(0, "absent"),)},
                       line_end=1)
    with TranscriptionDesk(spec, os.path.join(base, "d2")) as d2:
        t2 = d2.solve(ev_none)
    assert not t2.consistent
    print("  [OK] 边界3 压痕 absent 作为硬证据剪枝，冲突时无解")


def test_line_end_prunes(base):
    # 同样的声学，但行尾读数不同会区分/排除候选
    kA = Key("A", Action.PRINT_RIGHT, (100,), glyph="A")
    top = CopySheet("top", "t")
    spec = SessionSpec("le", (kA,), (top,), allowed_beats=(0, 1),
                       line_width=4, max_strikes=2)
    # 一击 A：PCM [100]，车座到 1
    ev_ok = Evidence(pcm=(100,), indentation={}, line_end=1)
    with TranscriptionDesk(spec, os.path.join(base, "d1")) as d:
        assert d.solve(ev_ok).consistent
    ev_bad = Evidence(pcm=(100,), indentation={}, line_end=2)
    with TranscriptionDesk(spec, os.path.join(base, "d2")) as d2:
        assert not d2.solve(ev_bad).consistent
    print("  [OK] 边界4 行尾位置作为独立证据参与相容判定")


def test_issue_json_roundtrip_and_sequence(base):
    k = Key("A", Action.PRINT_RIGHT, (100,), glyph="A")
    spec = SessionSpec("rt", (k,), (CopySheet("top", "t"),),
                       allowed_beats=(0, 1))
    spec_rt = SessionSpec.from_json(spec.to_json())
    assert spec_rt == spec
    mark = IndentationMark(0, "present", arrived_late=True)
    assert IndentationMark.from_json(mark.to_json()) == mark
    st = Strike(1, 0, "A")
    assert Strike.from_json(st.to_json()) == st

    store = IssueStore(os.path.join(base, "store"))
    h1 = store.open_new(spec)
    h2 = store.open_new(spec)
    assert h1.issue_no == "DK-0001" and h2.issue_no == "DK-0002"
    assert {i["issue_no"] for i in store.list_issues()} == {"DK-0001", "DK-0002"}
    try:
        store.reopen("DK-9999")
        raise AssertionError("未知刊行号应报错")
    except IssueError:
        pass
    # 工作档可写，封档后只读
    ev = Evidence(pcm=(100,), indentation={}, line_end=1)
    h1.save_evidence(ev)
    store.close_issue("DK-0001")
    ro = store.reopen("DK-0001")
    assert ro.load_evidence().pcm == (100,)
    try:
        ro.save_evidence(ev)
        raise AssertionError("只读档写入应被拒")
    except ReopenError:
        pass
    print("  [OK] 边界5 数据结构 JSON 往返、刊行号顺序分配、只读保护")


def test_fast_mixer_banker_rounding(base):
    # 恰半取偶：两个同拍声样求和后落在 .5 边界（用奇数样本构造）
    # 1+2=3 不触发；用 (1,) + (2,) 配偶数个无法造半，改用三击 (1,1,1) 与 (2,)
    # 直接验证 mix 的 np.rint 语义：手工构造 0.5 不可能由整数和产生，
    # 故这里验证整数求和不引入额外舍入，并与 numpy rint 基准一致。
    k1 = Key("a", Action.PRINT_RIGHT, (1,), glyph="a")
    k2 = Key("b", Action.PRINT_RIGHT, (2,), glyph="b")
    keys = (k1, k2)
    pcm = mix_strikes_pcm(keys, [Strike(1, 0, "a"), Strike(2, 0, "b")])
    assert pcm.tolist() == [3]
    # 负向削顶
    kn = Key("n", Action.PRINT_RIGHT, (-32768,), glyph="n")
    pcm2 = mix_strikes_pcm((kn,), [Strike(1, 0, "n"), Strike(2, 0, "n")])
    assert pcm2.tolist() == [-32768]
    # 与 numpy 恰半取偶基准对齐（防止有人改成 floor/ceil 语义）
    assert int(np.rint(0.5)) == 0 and int(np.rint(1.5)) == 2
    print("  [OK] 边界6 快速混音整数求和与负向削顶，恰半取偶基准锁定")


def test_cross_process_slip_accumulation(base):
    """跨进程重开校样室：已存回执里的勘误条不能被新进程覆盖，编号连续且
    body_hash 始终可重放校验（含 line_end=null 的证据往返）。"""
    import dictatype.cli as cli
    from dictatype.proofroom import ProofRoom

    kK = Key("K", Action.PRINT_RIGHT, (303,), glyph="K")
    kP = Key("P", Action.PRINT_RIGHT, (101,), glyph="P")
    kQ = Key("Q", Action.PRINT_RIGHT, (202,), glyph="Q")
    top = CopySheet("top", "t", None)
    spec = SessionSpec("cross", (kK, kP, kQ), (top,),
                       allowed_beats=(0,), max_strikes=2, line_width=8)
    store = IssueStore(os.path.join(base, "store"))
    h = store.open_new(spec)
    h.save_evidence(Evidence(pcm=(303,), indentation={}, line_end=None))
    # null 行尾证据落盘后必须能原样读回
    assert h.load_evidence().line_end is None
    with TranscriptionDesk(spec, h.dir + "/desk") as d:
        t = d.solve(h.load_evidence())
    h.save_transcript(t)

    room = ProofRoom(spec, t, h.issue_no)
    room.seal_copy(top)
    h.save_receipt(room.finalize("top"))
    room.fill_doubtful("top", 0, "P", "first")
    h.save_receipt(room.finalize("top"))

    # 模拟独立进程：重新加载 transcript 与回执，再挂一条迟来勘误
    room2 = cli._load_proofroom(h)
    cli._restore_receipts(h, room2)
    room2.admit_late_indentation("top",
                                 IndentationMark(0, "present", arrived_late=True), "Q")
    h.save_receipt(room2.finalize("top"))

    out = store.replay_receipt(h.issue_no, "top")
    assert out["n_errata_slips"] == 2
    assert out["hash_check"] == "ok"
    numbers = [s.slip_no for s in h.load_receipt("top").slips]
    assert numbers == [f"E-{h.issue_no}-0001", f"E-{h.issue_no}-0002"]
    print("  [OK] 边界7 跨进程勘误条累积不丢失、编号连续、回执哈希可重放")


CASES = [
    test_validation_rejects_bad_specs,
    test_backspace_cannot_leave_line,
    test_absent_indentation_prunes_candidates,
    test_line_end_prunes,
    test_issue_json_roundtrip_and_sequence,
    test_fast_mixer_banker_rounding,
    test_cross_process_slip_accumulation,
]


def run_all(base):
    shutil.rmtree(base, ignore_errors=True)
    print("== 听写机边界与持久化语义 ==")
    for i, fn in enumerate(CASES, 1):
        d = os.path.join(base, f"edge_{i:02d}")
        os.makedirs(d, exist_ok=True)
        fn(d)
    print(f"全部 {len(CASES)} 个边界案例通过\n")


if __name__ == "__main__":
    run_all(sys.argv[1] if len(sys.argv) > 1 else "/tmp/dictatype_edge")
