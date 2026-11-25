"""桌上冰壶清场交接的规则测试：题面每个示例与每条纪律各至少一条。"""

from __future__ import annotations

import sys
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.curling import (  # noqa: E402
    MAX_REPLAY_STONES,
    RED,
    EvidenceFrame,
    RefereeError,
    RefereeOffice,
    RELEASE,
    SEAL,
    ScoreDesk,
    naive_per_stone_minimum,
    score_range,
)
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
    build_ambiguous_end,
    line_example,
    resolved_plates_after_supplement,
    supplement_frame,
    tie_end,
)


class LineExamples(unittest.TestCase):
    def setUp(self):
        self.rw = RulerWarehouse(ALL_FRAMES)

    def tearDown(self):
        self.rw.close()

    def test_red_1_3_blue_4_red_scores_two(self):
        sol = line_example(self.rw, blue_at=4)
        case = score_range(sol.candidates, ScoreDesk.create(CIRCLE_LINE)).cases[0]
        self.assertEqual(case.scoring_side, RED)
        self.assertEqual(case.score, 2)

    def test_blue_moved_to_two_red_scores_one(self):
        # 只把蓝壶原帧改到两格：蓝成为最近壶，红仅 1 格那只严格近于蓝的 2 格。
        sol = line_example(self.rw, blue_at=2)
        case = score_range(sol.candidates, ScoreDesk.create(CIRCLE_LINE)).cases[0]
        self.assertEqual(case.scoring_side, RED)
        self.assertEqual(case.score, 1)

    def test_foul_removal_is_scoring_desk_only_measurement_unchanged(self):
        # 蓝壶恢复 4 格，但计分台撤除 3 格红壶；量测端还原结果不动。
        sol = line_example(self.rw, blue_at=4)
        before = {s: tuple(p) for s, p in sol.plate().centers.items()}
        desk = ScoreDesk.create(CIRCLE_LINE, removed=("red2",))
        case = score_range(sol.candidates, desk).cases[0]
        after = {s: tuple(p) for s, p in sol.plate().centers.items()}
        self.assertEqual(before, after)  # 撤除不改量测
        self.assertEqual(case.scoring_side, RED)
        self.assertEqual(case.score, 1)
        self.assertIn("red2", case.removed)

    def test_tie_at_nearest_scores_zero(self):
        sol, _ = tie_end(self.rw, stone_count=4)
        rng = score_range(sol.candidates, ScoreDesk.create(Fraction(9)))
        self.assertEqual(rng.guaranteed, 0)
        self.assertIsNone(rng.cases[0].scoring_side)

    def test_no_opponent_stones_all_in_circle_score(self):
        sol = line_example(self.rw, blue_at=4)
        # 计分台把唯一蓝壶撤掉：红方两只圈内壶全计。
        rng = score_range(sol.candidates, ScoreDesk.create(CIRCLE_LINE, removed=("blue1",)))
        self.assertEqual((rng.side, rng.guaranteed), (RED, 2))

    def test_outside_circle_ignored(self):
        # 圈半径收到 2：红 3 格与蓝 4 格出界，只剩红 1 格，红得 1。
        sol = line_example(self.rw, blue_at=4)
        rng = score_range(sol.candidates, ScoreDesk.create(Fraction(2)))
        self.assertEqual(rng.guaranteed, 1)
        self.assertEqual(set(rng.cases[0].outside), {"red2", "blue1"})


class RulerReadback(unittest.TestCase):
    def test_fingerprint_and_frame_number_readback_roundtrip(self):
        rw = RulerWarehouse(ALL_FRAMES)
        try:
            line_example(rw, blue_at=4)
            records = rw.warehouse.frame  # 原仓句柄存在
            readback = rw.frames[FRAME_BASIC].records
            kinds = sorted(r.kind for r in readback)
            self.assertEqual(kinds, [1, 1, 2, 2, 2, 3])  # 双钉 + 三壶 + 中心钉
            # 坐标精确读回（千分之一格定点量）
            red1 = next(r for r in readback if r.kind_name == "stone" and r.mark_id == 1)
            self.assertEqual((red1.x, red1.y), (Fraction(-1), Fraction(-1)))
            # 实际帧号隔离：本帧读回只含 101 号帧，另一帧号在同产品下不可见
            self.assertTrue(all(r.frame_id == FRAME_BASIC for r in readback))
            self.assertEqual(rw.warehouse.counts(rw.product)["total"], len(readback))
        finally:
            rw.close()

    def test_changed_definition_changes_fingerprint(self):
        p1 = RulerWarehouse((FRAME_BASIC,)).product
        p2 = RulerWarehouse((FRAME_BASIC, FRAME_A)).product
        self.assertNotEqual(p1.fingerprint, p2.fingerprint)
        # 消息集合是同一套布局的复制：共享位定义不变
        m1 = p1.message("overhead-ruler", FRAME_BASIC)
        m2 = p2.message("overhead-ruler", FRAME_BASIC)
        self.assertEqual(
            [s.name for s in m1.signals], [s.name for s in m2.signals]
        )


class AmbiguousEnd(unittest.TestCase):
    def setUp(self):
        self.rw = RulerWarehouse(ALL_FRAMES)

    def tearDown(self):
        self.rw.close()

    def test_single_pin_yields_two_compatible_plates_score_one_or_two(self):
        _, sol_b, evidence = build_ambiguous_end(self.rw)
        self.assertEqual(len(sol_b.candidates), 2)
        plates, _, _ = ambiguous_plates(self.rw)
        rng = score_range(plates, ScoreDesk.create(CIRCLE_AMBIGUOUS))
        self.assertEqual((rng.side, rng.guaranteed, rng.best), (RED, 1, 2))
        self.assertFalse(rng.settled)
        # 唯一共同可见记号就是 P1 那枚定位钉
        visible_b = set(evidence[1].visible)
        self.assertEqual(visible_b, {"pin1", "red2"})

    def test_per_stone_minimum_patch_is_forbidden_and_different(self):
        plates, _, _ = ambiguous_plates(self.rw)
        desk = ScoreDesk.create(CIRCLE_AMBIGUOUS)
        rng = score_range(plates, desk)
        patched = naive_per_stone_minimum(plates, desk)
        # 整盘纪律只敢保证 1 分；错误拼法会把红二也按 5 格拼进来报 2 分
        self.assertEqual(rng.guaranteed, 1)
        self.assertEqual(patched.score, 2)
        self.assertIn("禁止", patched.reason)

    def test_supplement_after_shift_only_works_while_sealed(self):
        plates, _, _ = ambiguous_plates(self.rw)
        evidence_c, sol_c = supplement_frame(self.rw)
        self.assertTrue(sol_c.unique)
        office = RefereeOffice(SEAL)
        office.open_end(
            "E-ambig", ScoreDesk.create(CIRCLE_AMBIGUOUS), plates,
            [
                self.rw.evidence(FRAME_A, ("pin0", "pin1", "red1", "blue1", "center_nail")),
                self.rw.evidence(FRAME_B, ("pin1", "red2")),
            ],
            stone_count=3,
        )
        before = office.accept_score("E-ambig")
        self.assertFalse(before.can_sign)  # 歧义未消不能签死
        receipt = office.supplement("E-ambig", evidence_c, self._resolved())
        self.assertTrue(receipt.accepted)
        self.assertEqual((receipt.before_candidates, receipt.after_candidates), (2, 1))
        verdict = office.accept_score("E-ambig")
        self.assertTrue(verdict.can_sign)
        self.assertEqual(verdict.signable_score, 2)  # 补拍实盘：红二较近

    def _resolved(self):
        return resolved_plates_after_supplement(self.rw)

    def test_supplement_rejected_after_table_released(self):
        plates, _, _ = ambiguous_plates(self.rw)
        office = RefereeOffice(SEAL)
        office.open_end("E-seal", ScoreDesk.create(CIRCLE_AMBIGUOUS), plates,
                        [self.rw.evidence(FRAME_B, ("pin1", "red2"))], stone_count=3)
        # 强制还桌后再补拍：移壶，拒绝
        office.ends["E-seal"].table_sealed = False
        evidence_c, _ = supplement_frame(self.rw)
        receipt = office.supplement("E-seal", evidence_c, self._resolved())
        self.assertFalse(receipt.accepted)
        self.assertIn("实壶已移", receipt.note)

    def test_release_policy_appeal_uses_sealed_range_only(self):
        plates, _, _ = ambiguous_plates(self.rw)
        office = RefereeOffice(RELEASE)
        sealed = [
            self.rw.evidence(FRAME_A, ("pin0", "pin1", "red1", "blue1", "center_nail")),
            self.rw.evidence(FRAME_B, ("pin1", "red2")),
        ]
        office.open_end("E-rel", ScoreDesk.create(CIRCLE_AMBIGUOUS), plates, sealed, stone_count=3)
        office.release_table("E-rel")  # 还桌办法：按约放桌
        # 移壶后从别的场次补的图，帧号不在封存清单 -> 申诉驳回
        foreign = EvidenceFrame(
            frame_event_id="f000303-k1-0",
            frame_id=FRAME_C,
            product_fingerprint=self.rw.fingerprint,
            visible=("pin0", "pin1", "red2", "blue1", "center_nail"),
            sealed=True,
            live_stones=False,
        )
        result = office.appeal("E-rel", [foreign])
        self.assertEqual(result["accepted"], [])
        self.assertTrue(result["rejected"])
        # 原帧能证明的范围仍是 1..2，保证分 1
        self.assertEqual(
            (result["range_on_sealed_evidence"]["guaranteed"],
             result["range_on_sealed_evidence"]["best"]),
            (1, 2),
        )


class TimelineAndReplay(unittest.TestCase):
    def setUp(self):
        self.rw = RulerWarehouse(ALL_FRAMES)

    def tearDown(self):
        self.rw.close()

    def _office_with_two_ends(self, policy):
        ambiguous, _, _ = ambiguous_plates(self.rw)
        office = RefereeOffice(policy)
        office.open_end(
            "E-ambig", ScoreDesk.create(CIRCLE_AMBIGUOUS), ambiguous,
            [
                self.rw.evidence(FRAME_A, ("pin0", "pin1", "red1", "blue1", "center_nail")),
                self.rw.evidence(FRAME_B, ("pin1", "red2")),
            ],
            stone_count=3,
        )
        sol, stones = tie_end(self.rw, stone_count=4)
        office.open_end("E-next", ScoreDesk.create(Fraction(9)), sol.candidates,
                        [self.rw.evidence(404, stones)], stone_count=len(stones))
        return office

    def test_seal_policy_three_checkpoints(self):
        office = self._office_with_two_ends(SEAL)

        # 当天收单：歧义局不能签死、不能放桌；另一局已订此刻开桌却拿不到桌
        c1 = office.checkpoint("E-ambig")
        self.assertFalse(c1.can_sign)
        self.assertFalse(c1.can_place_next)
        self.assertEqual(office.checkpoint("E-next").can_place_next, False)

        # 下一班开门：保桌才取得补拍新帧，歧义消除后签 2 分，随后放桌
        evidence_c, _ = supplement_frame(self.rw)
        receipt = office.supplement(
            "E-ambig", evidence_c, resolved_plates_after_supplement(self.rw)
        )
        self.assertTrue(receipt.accepted)
        self.assertTrue(office.accept_score("E-ambig").can_sign)
        office.release_table("E-ambig")
        self.assertTrue(office.can_place_next("E-next"))

        # 迟到申诉：只认还桌前封存参照（含已入册的补拍帧）
        result = office.appeal(
            "E-ambig",
            [self.rw.evidence(FRAME_C, ("pin0", "pin1", "red2", "blue1", "center_nail"))],
        )
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(result["signed"]["score"], 2)

    def test_release_policy_counterfactual(self):
        office = self._office_with_two_ends(RELEASE)
        # 当天即可按约还桌：下一局能摆壶，但歧义局只能登记保证分 1
        office.release_table("E-ambig")
        self.assertTrue(office.can_place_next("E-next"))
        verdict = office.accept_score("E-ambig")
        self.assertFalse(verdict.can_sign)
        self.assertEqual(office.checkpoint("E-ambig").note[:6], "已还桌：仅可")
        # 下一班没有补拍资格；迟到申诉只能在封存证据上维持 1..2 区间
        evidence_c, _ = supplement_frame(self.rw)
        receipt = office.supplement(
            "E-ambig", evidence_c, resolved_plates_after_supplement(self.rw)
        )
        self.assertFalse(receipt.accepted)

    def test_replay_rejects_more_than_six_stones(self):
        office = RefereeOffice(SEAL)
        sol6, stones6 = tie_end(self.rw, stone_count=MAX_REPLAY_STONES)
        office.open_end("E-six", ScoreDesk.create(Fraction(9)), sol6.candidates,
                        [self.rw.evidence(404, stones6)], stone_count=6)
        rng = office.tie_replay("E-six", sol6.candidates)
        self.assertEqual(rng.guaranteed, 0)

        # 第七只壶：并列回放入口直接拒绝
        rw2 = RulerWarehouse((404,))
        try:
            sol7, stones7 = tie_end(rw2, stone_count=MAX_REPLAY_STONES + 1)
            office.open_end("E-seven", ScoreDesk.create(Fraction(9)), sol7.candidates,
                            [rw2.evidence(404, stones7)], stone_count=7)
            with self.assertRaises(RefereeError):
                office.tie_replay("E-seven", sol7.candidates)
        finally:
            rw2.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
