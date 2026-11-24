"""地方译本索引：领域纯规则测试（无 IO）。

覆盖题面核心例子：甲乙同署“远舟”，靠扉页语种可分；补录乙译法文后变两人候选；
撤回补录并换成删掉语种栏的节印本后，同一张关系表也不能靠问语种消歧。
"""

from __future__ import annotations

import unittest

from confcat.biblio import (
    FIELD_ORIGINAL_LANGUAGE,
    DeclarationSource,
    HoldingCopy,
    PenNameDeclaration,
    candidate_identities,
    evaluate_holding,
)


def _src(note: str = "test") -> DeclarationSource:
    return DeclarationSource(task_id="t", row_no=0, columns={"pen_name": "pen"}, note=note)


def _decl(
    decl_id: str,
    translator: str,
    name: str,
    language: str | None = None,
    vfrom: int | None = None,
    vto: int | None = None,
    withdrawn: bool = False,
    version: int = 1,
) -> PenNameDeclaration:
    return PenNameDeclaration(
        decl_id=decl_id, version=version, pen_name="远舟",
        translator_id=translator, translator_name=name, source=_src(),
        original_language=language, volume_from=vfrom, volume_to=vto, withdrawn=withdrawn,
    )


def _copy(
    copy_id: str = "C1",
    volume: int = 1,
    language: str | None = "法文",
    visible: tuple[str, ...] = (FIELD_ORIGINAL_LANGUAGE,),
    printable: bool = False,
) -> HoldingCopy:
    return HoldingCopy(
        copy_id=copy_id, volume=volume, edition="e", pen_name="远舟",
        visible_fields=frozenset(visible), original_language=language,
        printable_new_edition=printable,
    )


class DomainRuleTest(unittest.TestCase):
    def setUp(self):
        # 初刊：甲译法文、乙译日文，均署“远舟”
        self.decls = [
            _decl("D1", "TJIA", "甲", language="法文", vfrom=1, vto=6),
            _decl("D2", "TYI", "乙", language="日文", vfrom=1, vto=6),
        ]

    def test_language_field_distinguishes_on_printed_title_page(self):
        fr = evaluate_holding(_copy(language="法文"), self.decls)
        jp = evaluate_holding(_copy(language="日文"), self.decls)
        self.assertTrue(fr.unique and fr.resolved_translator_id == "TJIA")
        self.assertTrue(jp.unique and jp.resolved_translator_id == "TYI")
        self.assertEqual(fr.missing_fields, ())
        self.assertIn(FIELD_ORIGINAL_LANGUAGE, fr.answerable_fields)

    def test_added_french_declaration_creates_two_candidates(self):
        # 补录一条有出处的乙译法文声明
        decls = self.decls + [_decl("D4", "TYI", "乙", language="法文", vfrom=1, vto=6)]
        ev = evaluate_holding(_copy(language="法文"), decls)
        self.assertFalse(ev.unique)
        self.assertEqual(ev.status, "ambiguous")
        self.assertEqual(
            sorted(c.translator_id for c in ev.candidates), ["TJIA", "TYI"]
        )

    def test_withdraw_reforms_candidate_identities(self):
        decls = self.decls + [
            _decl("D4", "TYI", "乙", language="法文", vfrom=1, vto=6, withdrawn=True)
        ]
        ev = evaluate_holding(_copy(language="法文"), decls)
        self.assertTrue(ev.unique and ev.resolved_translator_id == "TJIA")

    def test_abridged_copy_without_language_column_cannot_ask_language(self):
        # 同一张关系表（甲乙都能译法文时），节印本删了语种栏
        decls = self.decls + [_decl("D4", "TYI", "乙", language="法文", vfrom=1, vto=6)]
        abridged = _copy(language="法文", visible=())  # 扉页无语种栏
        ev = evaluate_holding(abridged, decls)
        self.assertFalse(ev.unique)
        self.assertNotIn(FIELD_ORIGINAL_LANGUAGE, ev.answerable_fields)
        self.assertIn(FIELD_ORIGINAL_LANGUAGE, ev.missing_fields)
        # 即便初刊关系表（甲法乙日），节印本也不能靠问语种分；乙的日文限定不可核对
        ev2 = evaluate_holding(abridged, self.decls)
        self.assertFalse(ev2.unique)
        self.assertEqual(
            sorted(c.translator_id for c in ev2.candidates), ["TJIA", "TYI"]
        )

    def test_candidate_keeps_declaration_source(self):
        cands = candidate_identities(_copy(language="法文"), self.decls)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].declaration.source.task_id, "t")
        self.assertEqual(cands[0].checked[FIELD_ORIGINAL_LANGUAGE], "法文")

    def test_volume_scope_supports_same_pen_name_in_two_periods(self):
        # 丙在卷七至卷九复用同一笔名
        decls = self.decls + [_decl("D3", "TBING", "丙", vfrom=7, vto=9)]
        early = evaluate_holding(_copy(volume=2, language="日文"), decls)
        late = evaluate_holding(_copy(volume=7, language="日文"), decls)
        self.assertTrue(early.unique and early.resolved_translator_id == "TYI")
        self.assertTrue(late.unique and late.resolved_translator_id == "TBING")

    def test_no_candidate_when_pen_name_or_volume_mismatch(self):
        other = HoldingCopy(
            copy_id="X", volume=1, edition="e", pen_name="孤鸿",
            visible_fields=frozenset({FIELD_ORIGINAL_LANGUAGE}),
            original_language="法文",
        )
        ev = evaluate_holding(other, self.decls)
        self.assertEqual(ev.status, "no-candidate")
        out_of_range = evaluate_holding(_copy(volume=99, language="法文"), self.decls)
        self.assertEqual(out_of_range.status, "no-candidate")

    def test_unchecked_constraint_recorded_not_filtered(self):
        # 语种不可见时，带语种限定的声明不被排除，而是记进 unchecked
        abridged = _copy(language="法文", visible=())
        cands = candidate_identities(abridged, self.decls)
        self.assertEqual(len(cands), 2)
        by_id = {c.translator_id: c for c in cands}
        self.assertEqual(by_id["TYI"].unchecked, {FIELD_ORIGINAL_LANGUAGE: "日文"})
        self.assertEqual(by_id["TJIA"].unchecked, {FIELD_ORIGINAL_LANGUAGE: "法文"})


if __name__ == "__main__":
    unittest.main()
