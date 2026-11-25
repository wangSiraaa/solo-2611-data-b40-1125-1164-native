"""题面全场景装配：词库发版、排播、试听入口的可复跑叙事。

本模块只做装配与留痕，所有语义判定都来自 announce 子包。
运行：python3 -m demo.station_scenario [工作区目录]
"""
from __future__ import annotations

import os
import sys

from audiomix.announce import (
    BookingTable, BroadcastDesk, CharacterTemplate, ContractKind,
    CostLedger, Joint, Lexicon, RecordingCalendar, Schedule, SpliceTable,
    StationTemplate, Trip, WorkspaceStore,
)
from audiomix.announce.hearing import Reason
from audiomix.announce.templates import analyze_speaker, segment_matches

SPEAKER_S1 = "S1"   # 甲站母录发音人
SPEAKER_S2 = "S2"   # 次班短音架发音人
SPEAKER_G = "SG"   # 音高档位复例发音人

# 题面严格模板：甲 [4,1,2]、乙 [4,1,3]、丙 [4,2,3]，均三样本、零误差。
JIA, YI, BING = "甲站", "乙站", "丙站"


def _print(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def build_lexicon() -> Lexicon:
    """装配同一份词库：甲站母录+分出资产、次班短音架、音高复例、全部模板。"""
    lex = Lexicon(sample_budget=100)

    # ---- 甲站整词母录 [4,1,2] 及同一母录分出的词头 [4]、尾字 [1,2] ---- #
    lex.record_master("M_JIA", "whole_word", SPEAKER_S1, [4, 1, 2], JIA)
    lex.derive("H_JIA", "M_JIA", "head", "词头·甲", 0, 1)
    lex.derive("T_JIA", "M_JIA", "tail", "尾字·甲", 1, 3)

    # ---- 次班短音架：带尾的头 [4,1] 与两枚来源不同的尾 [0,3]、[1,3] ---- #
    lex.record_master("H_41", "head", SPEAKER_S2, [4, 1], "带尾的头")
    lex.record_master("T_03", "tail", SPEAKER_S2, [0, 3], "尾字来源甲")
    lex.record_master("T_13", "tail", SPEAKER_S2, [1, 3], "尾字来源乙")

    # ---- 音高档位复例：同一发音人，一段落两档容差，另一段排除其中一档 ---- #
    lex.record_master("G_P1", "head", SPEAKER_G, [5], "复例·段一")
    lex.record_master("G_P2", "head", SPEAKER_G, [8], "复例·段二")
    # 单段兼容两个字：P1=[5] 在高档同时落入 云(容差1)/林(容差2)
    lex.add_character_template(CharacterTemplate("云", SPEAKER_G, "高档", (5,), 1))
    lex.add_character_template(CharacterTemplate("林", SPEAKER_G, "高档", (5,), 2))
    # 低档：P1=[5] 同时落入 风(tol1)/雨(tol2) 两个字，读法都保留；
    # P2=[8] 只符合低档的雨(tol2)，高档对 P2 无任何模板容许 → 整档被段二排除
    lex.add_character_template(CharacterTemplate("风", SPEAKER_G, "低档", (5,), 1))
    lex.add_character_template(CharacterTemplate("雨", SPEAKER_G, "低档", (6,), 2))

    # ---- 严格站名模板（零误差整句，三样本） ---- #
    lex.add_station_template(StationTemplate(JIA, (4, 1, 2)))
    lex.add_station_template(StationTemplate(YI, (4, 1, 3)))
    lex.add_station_template(StationTemplate(BING, (4, 2, 3)))
    return lex


def build_splice_table() -> SpliceTable:
    """自制接头表：排期只容许接头重叠一格，重叠采样相加，禁止重剪。"""
    return SpliceTable(
        joints=[
            Joint("H_41", "T_03", overlap=1),
            Joint("H_41", "T_13", overlap=1),
        ],
        allowed_overlap=1,
    )


def build_schedule() -> tuple[Schedule, BookingTable, RecordingCalendar]:
    schedule = Schedule()
    # 首班：方向“上行”，目标甲，相邻站乙/丙，三样本广播窗。
    schedule.add_trip(Trip(1, "上行", JIA, (YI, BING), window_samples=3))
    # 次班：临时改站为乙；整词乙首个可录档在次班之后（档期 3 > 班次 2）。
    schedule.add_trip(Trip(2, "上行", YI, (BING,), window_samples=3))
    booking = BookingTable()
    calendar = RecordingCalendar([3, 5])  # 整词乙最早可录档为 3
    return schedule, booking, calendar


def build_desks(schedule: Schedule, splice_table: SpliceTable,
                booking: BookingTable, calendar: RecordingCalendar
                ) -> tuple[BroadcastDesk, BroadcastDesk, CostLedger, CostLedger]:
    rental_ledger = CostLedger("租用店台账")
    buyout_ledger = CostLedger("买断店台账")
    rental = BroadcastDesk(
        "租用店", ContractKind.RENTAL, schedule, splice_table,
        booking, rental_ledger,
    )
    buyout = BroadcastDesk(
        "买断店", ContractKind.BUYOUT, schedule, splice_table,
        booking, buyout_ledger, calendar=calendar,
    )
    return rental, buyout, rental_ledger, buyout_ledger


def gear_demo(lexicon: Lexicon) -> None:
    _print("音高档位复例：一段兼容两字/两档，另一段排除其中一档")
    analysis = analyze_speaker(
        SPEAKER_G, lexicon.speaker_segments(SPEAKER_G),
        lexicon.character_templates,
    )
    print(f"发音人 {SPEAKER_G} 最终可成立档位：{list(analysis.candidate_gears)}")
    assert analysis.candidate_gears == ("低档",), "高档应被段二排除"
    for evidence in analysis.evidence_for("低档"):
        print(f"  {evidence.asset_id}: 保留读法 {list(evidence.readings)} "
              f"模板={list(evidence.matched_templates)}")
    p1 = next(e for e in analysis.evidence_for("低档") if e.asset_id == "G_P1")
    assert set(p1.readings) == {"风", "雨"}, "P1 须同时保留两个能成立的读法"
    p2 = next(e for e in analysis.evidence_for("低档") if e.asset_id == "G_P2")
    assert p2.readings == ("雨",), "P2 只在低档成立，用于排除高档"
    # 直接演示“一段录音兼容两个字”（高档视角下 P1 同时是云和林）
    high = [t for t in lexicon.character_templates
            if t.speaker == SPEAKER_G and t.gear == "高档"]
    hits = segment_matches((5,), high)
    print(f"  单段 [5] 在高档同时命中两个字：{sorted(t.character for t in hits)}")
    assert {t.character for t in hits} == {"云", "林"}


def master_revision_demo(lexicon: Lexicon, splice_table: SpliceTable) -> None:
    _print("母录改版联动：整词与分出尾字同生共死，已发版快照不变")
    r1 = lexicon.release("首版：甲站母录原版 [4,1,2]")
    print(f"发版 {r1.version} 资产：")
    for row in lexicon.catalog():
        print(f"  {row}")

    # 零误差且有三样本窗口时可播甲
    jia_asset = r1.assets["M_JIA"]
    assert jia_asset.pcm == (4, 1, 2)
    assert r1.station_template(JIA).admits(jia_asset.pcm)
    print("首版整词 [4,1,2] 对甲严格模板零误差，三样本窗 → 可播甲 ✓")

    # 只改母录末样本为 3：母录整词与分出尾字都无法播甲
    lexicon.revise_master("M_JIA", [4, 1, 3], "末样本 2→3 的改版")
    assert lexicon.assets["M_JIA"].pcm == (4, 1, 3)
    assert lexicon.assets["T_JIA"].pcm == (1, 3), "分出尾字必须随母录联动"
    assert not r1.station_template(JIA).admits(lexicon.assets["M_JIA"].pcm)
    print("改版后母录 =", list(lexicon.assets["M_JIA"].pcm),
          " 分出尾字 =", list(lexicon.assets["T_JIA"].pcm))
    print("  → 用该母录整词或分出尾字都无法再播甲；但 R1 快照仍是 [4,1,2] ✓")
    assert r1.assets["M_JIA"].pcm == (4, 1, 2), "已发版资产不可变"

    # 恢复原版，仅把窗缩到两样本且禁止截字，同样不能签
    lexicon.restore_master("M_JIA", "恢复甲站母录原版")
    r2 = lexicon.release("恢复版：母录回到 [4,1,2]")
    assert lexicon.assets["M_JIA"].pcm == (4, 1, 2)
    from audiomix.announce.render import WholeWordPlan, render_plan
    from audiomix.announce.hearing import adjudicate
    rendering = render_plan(r2, WholeWordPlan("M_JIA"), splice_table)
    report = adjudicate(r2, rendering, JIA, [YI, BING], window_samples=2)
    assert not report.can_air and report.reason == Reason.WINDOW_TOO_SMALL
    print("恢复原版后整词 =", list(rendering.pcm),
          " 但窗仅 2 样本且禁止截字 →", report.reason, "✓")


def first_trip_demo(lexicon: Lexicon, desks: tuple, schedule: Schedule) -> None:
    rental, buyout, rental_ledger, buyout_ledger = desks
    _print("首班（班次1，上行→甲站）：两家店各自办理并签播")
    r2 = lexicon.latest_release()
    for desk in (rental, buyout):
        decision = desk.evaluate_whole_word(r2, 1, "M_JIA")
        assert decision.signed, f"首班应可签播：{decision.detail}"
        print(f"[{desk.name}] {decision.plan_kind} 出声 {list(decision.order.pcm_snapshot)}"
              f" 依据发版 {decision.release_version} → {decision.reason}")
    schedule.mark_departed(1)
    print("首班已发车，播次冻结；后续词库改版不影响其原音回听。")


def second_trip_demo(lexicon: Lexicon, desks: tuple, schedule: Schedule,
                     tail_03_exclusive: bool) -> None:
    rental, buyout, rental_ledger, buyout_ledger = desks
    r2 = lexicon.latest_release()
    scenario = "情形B：[0,3] 已被另一班独占" if tail_03_exclusive \
        else "情形A：[0,3] 尚可借"
    _print(f"次班（班次2，临时改站→乙站）—— {scenario}")

    # 接头数学：[4,1] 与 [0,3] 重叠一格相加 → [4,1,3]=乙；
    #          [4,1] 与 [1,3] 重叠一格相加 → [4,2,3]=丙（误听）。
    from audiomix.announce.pcm import overlap_join
    yi_pcm = overlap_join((4, 1), (0, 3), 1)
    bing_pcm = overlap_join((4, 1), (1, 3), 1)
    print("接头实算：H_41+T_03 =", list(yi_pcm), "（乙）；"
          "H_41+T_13 =", list(bing_pcm), "（丙，邻站误听）")
    assert yi_pcm == (4, 1, 3) and bing_pcm == (4, 2, 3)

    if tail_03_exclusive:
        # T_03 的他班独占登记在调用方完成（见 run 主流程）
        pass

    # 租用店：先试会误听的 T_13，留拒播依据；再按情形试 T_03
    bad = rental.evaluate_splice(r2, 2, "H_41", "T_13")
    assert not bad.signed and bad.reason == Reason.NEIGHBOR_MIXUP
    print(f"[租用店] H_41+T_13 拒签：{bad.reason} | {bad.detail}")

    decision_03 = rental.evaluate_splice(r2, 2, "H_41", "T_03")
    if tail_03_exclusive:
        assert not decision_03.signed
        print(f"[租用店] H_41+T_03 拒签：{decision_03.reason} | {decision_03.detail}")
        print("[租用店] 本店能签的次班：无。依据：误听(T_13→丙)+独占(T_03)，留待补录。")
        rental_outcome = ("无播次", decision_03)
    else:
        assert decision_03.signed
        print(f"[租用店] H_41+T_03 签播：实际出声 {list(decision_03.order.pcm_snapshot)}"
              f"（{decision_03.reason}）")
        rental_outcome = (f"乙站 {list(decision_03.order.pcm_snapshot)}", decision_03)

    # 买断店：不受理短单元拼接；整词乙无录档，首个可录档 3 在次班之后
    splice_refused = buyout.evaluate_splice(r2, 2, "H_41", "T_03")
    assert not splice_refused.signed and splice_refused.reason == "合同不许可"
    print(f"[买断店] 拼接申请拒签：{splice_refused.detail}")
    # 买断店也没有乙的整词母录：构造“档期允许时补录后再播”的订单
    deferred = _buyout_deferred_order(buyout, r2, schedule)
    print(f"[买断店] 整词乙当前缺录档；已受理补录订单，新录音档 {deferred.deferred_slot}"
          "（在次班之后），档期到时补录后再播。")
    buyout_outcome = ("补录订单(档期3)", deferred)
    return rental_outcome, buyout_outcome


def _buyout_deferred_order(buyout: BroadcastDesk, release, schedule: Schedule):
    """买断店受理一个“档期允许时补录后再播”的订单。

    词库里没有乙的整词母录，先以缺录档依据登记，再约新录音档；
    档期 3 到来时录入新母录并重新办理，验证补录后可播。
    """
    from audiomix.announce.render import WholeWordPlan
    from audiomix.announce.hearing import adjudicate
    from audiomix.announce.render import render_plan
    # 当前无整词乙：直接用甲母录演示“当前不可播 → 约档”
    decision = buyout.evaluate_whole_word(release, 2, "M_JIA")
    assert not decision.signed and decision.deferred_slot == 3
    return decision


def buyout_record_then_air(lexicon: Lexicon, buyout: BroadcastDesk,
                           schedule: Schedule) -> None:
    _print("买断店补录履约：档期 3 录入乙站整词母录后再播（已签整段不得换尾）")
    slot = buyout.book_new_recording_slot()
    assert slot == 3
    lexicon.record_master("M_YI", "whole_word", SPEAKER_S1, [4, 1, 3], YI,
                          reason=f"档期{slot}补录")
    r3 = lexicon.release("补录版：乙站整词母录 [4,1,3]")
    # 补录后在新班（班次3，方向同为上行）播出
    schedule.add_trip(Trip(3, "上行", YI, (BING,), window_samples=3))
    decision = buyout.evaluate_whole_word(r3, 3, "M_YI")
    assert decision.signed
    print(f"[买断店] 档期{slot}补录 M_YI=[4,1,3]，班次3 出声 "
          f"{list(decision.order.pcm_snapshot)} → {decision.reason}")
    # 已签整段锁定：再拿同一母录办理另一班，触发锁定留痕（不得换尾）
    schedule.add_trip(Trip(4, "上行", YI, (BING,), window_samples=3))
    locked = buyout.evaluate_whole_word(r3, 4, "M_YI")
    assert not locked.signed and locked.reason == "整词已买断锁定"
    print(f"[买断店] M_YI 已买断锁定：{locked.detail} ✓")
    schedule.mark_departed(3)


def replay_demo(rental: BroadcastDesk, buyout: BroadcastDesk) -> None:
    _print("回听入口：接回首班，听到的仍是当时的甲站原音")
    for desk in (rental, buyout):
        order = desk.replay(1)
        print(f"[{desk.name}] 班次1 回听：目标 {order.target}，发版 "
              f"{order.release_version}，实际 PCM = {list(order.pcm_snapshot)}")
        assert order.pcm_snapshot == (4, 1, 2)
        assert order.target == JIA


def budget_demo(lexicon: Lexicon) -> None:
    _print("采样预算：各次录音合计不超过百样本")
    print(f"真实新录音合计 {lexicon.recorded_samples} / 预算 {lexicon.sample_budget}")
    assert lexicon.recorded_samples <= 100
    print("分出切片不产生新录音；全部真实录音样本数如上，未超百样本 ✓")


def run(root: str) -> dict:
    lexicon = build_lexicon()
    splice_table = build_splice_table()
    schedule, booking, calendar = build_schedule()
    rental, buyout, rental_ledger, buyout_ledger = build_desks(
        schedule, splice_table, booking, calendar,
    )
    desks = (rental, buyout, rental_ledger, buyout_ledger)

    gear_demo(lexicon)
    master_revision_demo(lexicon, splice_table)
    first_trip_demo(lexicon, desks, schedule)

    # 情形 A：[0,3] 尚可借
    outcome_a = second_trip_demo(lexicon, desks, schedule, tail_03_exclusive=False)
    schedule.mark_departed(2)

    # 情形 B 需要独立工作区（T_03 被另一班独占），单独装配一套并行状态
    lex_b, st_b, book_b, cal_b = build_lexicon(), None, BookingTable(), RecordingCalendar([3, 5])
    sched_b = Schedule()
    sched_b.add_trip(Trip(1, "上行", JIA, (YI, BING), 3))
    sched_b.add_trip(Trip(2, "上行", YI, (BING,), 3))
    sched_b.add_trip(Trip(9, "下行", JIA, (YI,), 3))  # 另一班
    rental_b, buyout_b, rl_b, bl_b = build_desks(sched_b, build_splice_table(),
                                                 book_b, cal_b)
    # 完成与主工作区相同的首版/改版/恢复发版
    r1b = lex_b.release("首版")
    lex_b.revise_master("M_JIA", [4, 1, 3], "改版")
    lex_b.restore_master("M_JIA", "恢复")
    r2b = lex_b.release("恢复版")
    rental_b.evaluate_whole_word(r2b, 1, "M_JIA")
    sched_b.mark_departed(1)
    # 另一班独占 T_03
    book_b.grant_exclusive("T_03", 9, owner="other")
    outcome_b = second_trip_demo(
        lex_b, (rental_b, buyout_b, rl_b, bl_b), sched_b,
        tail_03_exclusive=True,
    )

    # 主工作区继续：买断店档期 3 补录后再播
    buyout_record_then_air(lexicon, buyout, schedule)
    replay_demo(rental, buyout)
    budget_demo(lexicon)

    _print("代价台账（同一本目录、同一条改站，许可与代价按合同兑现）")
    for ledger in (rental_ledger, buyout_ledger):
        print(f"\n{ledger.name}（合计 {ledger.total()}）：")
        for e in ledger.entries:
            print(f"  班次{e.trip_no} {e.contract}/{e.action} "
                  f"{list(e.asset_refs)} 金额={e.amount} 备注={e.note}")

    # 持久化 + 重开：乘务员可再次调用
    store = WorkspaceStore(root)
    store.save(lexicon, splice_table, schedule, booking,
               rental_ledger, buyout_ledger, rental, buyout)
    reopened = store.load()
    re_order = reopened["rental_desk"].replay(1)
    assert re_order.pcm_snapshot == (4, 1, 2)
    _print(f"落盘重开：状态已保存到 {store.path}")
    print("重开后回听班次1 仍为", list(re_order.pcm_snapshot), "✓")

    return {
        "lexicon": lexicon, "schedule": schedule, "rental": rental,
        "buyout": buyout, "rental_ledger": rental_ledger,
        "buyout_ledger": buyout_ledger, "store": store,
        "outcome_a": outcome_a, "outcome_b": outcome_b,
        "parallel": {"lexicon": lex_b, "schedule": sched_b,
                     "rental": rental_b, "buyout": buyout_b},
    }


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_station"
    run(root)
    print("\n全部场景办理完成。")
