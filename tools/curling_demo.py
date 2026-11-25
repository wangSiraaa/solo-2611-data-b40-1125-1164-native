"""桌上冰壶清场交接演示：量尺目录入帧、整盘量测、计分与裁判室时间线。

运行：``python3 tools/curling_demo.py``。全程精确有理数，输出每一步的
帧号、产品指纹、校正假设、逐壶距离与裁判结论，可对照题面逐条核查。
"""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.curling import (  # noqa: E402
    EvidenceFrame,
    RefereeOffice,
    RELEASE,
    SEAL,
    ScoreDesk,
    naive_per_stone_minimum,
    score_range,
)
from bitcatalog.curling.geometry import fraction  # noqa: E402
from bitcatalog.curling.scenarios import (  # noqa: E402
    ALL_FRAMES,
    CIRCLE_AMBIGUOUS,
    CIRCLE_LINE,
    FRAME_A,
    FRAME_B,
    FRAME_BASIC,
    FRAME_C,
    RulerWarehouse,
    ambiguous_plates,
    line_example,
    resolved_plates_after_supplement,
    supplement_frame,
    tie_end,
)


def header(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def show_case(title: str, rng) -> None:
    print(f"\n[{title}]")
    for case in rng.cases:
        print(f"  盘{case.plate_index}：{case.reason}")
        print(f"    圈内距离: { {s: fraction(d) for s, d in sorted(case.in_circle.items())} }")
        if case.outside:
            print(f"    圈外/不计: {case.outside}")
        if case.removed:
            print(f"    计分台撤除: {case.removed}")
    print(f"  => 得分方 {rng.side}，可证明区间 {rng.guaranteed}..{rng.best}，"
          f"{'已可签死 ' + str(rng.guaranteed) if rng.settled else '不能签死'}")


def demo_ruler_and_line() -> RulerWarehouse:
    header("1. 量尺小目录：三活动分支、原仓入帧、按指纹+实际帧号读回")
    rw = RulerWarehouse(ALL_FRAMES)
    print(f"量尺目录产品指纹 sha256 = {rw.fingerprint}")
    print("分支: selector 1=定位钉 pin_id | 2=壶边标记 stone_id | 3=中心钉（仅共用 x/y）")

    line_example(rw, blue_at=4)
    readback = rw.frames[FRAME_BASIC].records
    print(f"实际帧号 {FRAME_BASIC} 读回 {len(readback)} 条：")
    for rec in readback:
        extra = f" id={rec.mark_id}" if rec.mark_id is not None else ""
        print(f"  {rec.frame_event_id:18s} {rec.kind_name:11s} "
              f"x={fraction(rec.x):>4s} y={fraction(rec.y):>4s}{extra}")

    header("2. 计分台三档示例（圈半径 5；红壶 1、3 格不动，只动蓝壶原帧/撤除）")
    sol4 = line_example(rw, blue_at=4)
    show_case("蓝 4 格（题面基线）", score_range(sol4.candidates, ScoreDesk.create(CIRCLE_LINE)))

    sol2 = line_example(rw, blue_at=2)
    show_case("只把蓝壶原帧改到 2 格", score_range(sol2.candidates, ScoreDesk.create(CIRCLE_LINE)))

    desk_remove = ScoreDesk.create(CIRCLE_LINE, removed=("red2",))
    show_case("蓝恢复 4 格，但计分台撤除 3 格红壶（量测不动）",
              score_range(sol4.candidates, desk_remove))

    sol_tie, _ = tie_end(rw, stone_count=4)
    show_case("最近壶并列回放（4 壶，<=6）",
              score_range(sol_tie.candidates, ScoreDesk.create(Fraction(9))))
    return rw


def demo_ambiguous(rw: RulerWarehouse) -> None:
    header("3. 换坐标那局：单钉双相容校正，比分可能 1 或 2")
    plates, sol_a, sol_b = ambiguous_plates(rw)
    print(f"曝光 {FRAME_A}（红一、蓝一同帧）：{sol_a.plate().calibration.basis}，唯一盘")
    print(f"曝光 {FRAME_B}（红二 + 仅有的共同定位钉 P1）：{len(sol_b.candidates)} 套相容校正")
    for plate in sol_b.candidates:
        d = plate.distance_to_center("red2")
        print(f"  {plate.calibration.describe()}")
        print(f"    -> 红二世界壶心 {plate.centers['red2']}，距心 {fraction(d)} 格")
    rng = score_range(plates, ScoreDesk.create(CIRCLE_AMBIGUOUS))
    show_case("两候选整盘（同次曝光共用一套校正，禁止逐壶拼盘）", rng)

    patched = naive_per_stone_minimum(plates, ScoreDesk.create(CIRCLE_AMBIGUOUS))
    print(f"\n  反面对照（每壶各自最小距离拼盘）：{patched.reason}，报 {patched.score} 分"
          f" —— 无任一校正支持，纪律禁止")

    header("4. 下一班补拍回执：红二、蓝一、中心钉同幅（保桌才可能取得）")
    evidence_c, sol_c = supplement_frame(rw)
    print(f"补拍帧 {evidence_c.frame_event_id}（实际帧号 {FRAME_C}），同产品指纹 "
          f"{evidence_c.product_fingerprint == rw.fingerprint}")
    print(f"双钉校正: {sol_c.plate().calibration.basis}；红二距心 "
          f"{fraction(sol_c.plate().distance_to_center('red2'))}，蓝一距心 "
          f"{fraction(sol_c.plate().distance_to_center('blue1'))}")


def demo_timeline(rw: RulerWarehouse, policy: str) -> None:
    header(f"5. 裁判室时间线（选定办法：{policy}）")
    plates, _, _ = ambiguous_plates(rw)
    office = RefereeOffice(policy)
    sealed = [
        rw.evidence(FRAME_A, ("pin0", "pin1", "red1", "blue1", "center_nail")),
        rw.evidence(FRAME_B, ("pin1", "red2")),
    ]
    office.open_end("E-换坐标局", ScoreDesk.create(CIRCLE_AMBIGUOUS), plates, sealed, stone_count=3)
    sol_next, stones = tie_end(rw, stone_count=4)
    office.open_end("E-已订开桌局", ScoreDesk.create(Fraction(9)), sol_next.candidates,
                    [rw.evidence(404, stones)], stone_count=len(stones))

    print("\n-- 当天收单 --")
    for end_id in ("E-换坐标局", "E-已订开桌局"):
        v = office.checkpoint(end_id)
        print(f"  {end_id}: 可签分={v.can_sign} 可摆壶={v.can_place_next} | {v.note}")

    print("\n-- 下一班开门 --")
    if policy == SEAL:
        evidence_c, _ = supplement_frame(rw)
        receipt = office.supplement(
            "E-换坐标局", evidence_c, resolved_plates_after_supplement(rw)
        )
        print(f"  补拍回执: 受理={receipt.accepted}，候选 {receipt.before_candidates}->"
              f"{receipt.after_candidates}，{receipt.note}")
        signed = office.accept_score("E-换坐标局")
        print(f"  签分: can_sign={signed.can_sign}，{signed.side} {signed.signable_score} 分")
        office.release_table("E-换坐标局")
        print(f"  放桌后 E-已订开桌局 可摆壶={office.can_place_next('E-已订开桌局')}")
    else:
        office.release_table("E-换坐标局")
        print("  按约还桌：E-已订开桌局 可摆壶=True；换坐标局不设补拍窗口")
        evidence_c, _ = supplement_frame(rw)
        receipt = office.supplement(
            "E-换坐标局", evidence_c, resolved_plates_after_supplement(rw)
        )
        print(f"  补拍回执: 受理={receipt.accepted}（{receipt.note}）")

    print("\n-- 迟到申诉到来 --")
    if policy == SEAL:
        offered = [rw.evidence(FRAME_C, ("pin0", "pin1", "red2", "blue1", "center_nail"))]
    else:
        offered = [EvidenceFrame(
            frame_event_id="foreign-other-session", frame_id=FRAME_C,
            product_fingerprint=rw.fingerprint,
            visible=("pin0", "pin1", "red2", "blue1", "center_nail"),
            sealed=True, live_stones=False,
        )]
    result = office.appeal("E-换坐标局", offered)
    print(f"  受理 {len(result['accepted'])} 帧，驳回 {len(result['rejected'])} 帧")
    for item in result["rejected"]:
        print(f"    驳回 {item['frame_event_id']}: {item['reason']}")
    sealed_rng = result["range_on_sealed_evidence"]
    print(f"  封存证据下可证明区间 {sealed_rng['guaranteed']}..{sealed_rng['best']}，"
          f"已签={result['signed']}")


def main() -> int:
    rw = demo_ruler_and_line()
    try:
        demo_ambiguous(rw)
        demo_timeline(rw, SEAL)
        # 另一条办法做对照（独立仓，避免帧事件号重复）
        rw2 = RulerWarehouse(ALL_FRAMES)
        try:
            demo_timeline(rw2, RELEASE)
        finally:
            rw2.close()
    finally:
        rw.close()
    print("\n演示结束。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
