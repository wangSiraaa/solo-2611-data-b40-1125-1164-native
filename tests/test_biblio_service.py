"""地方译本索引：BiblioService 集成测试（SQLite 持久化、岗位权限、重开）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from confcat.biblio import BiblioError, DeclarationSource
from confcat.index_service import (
    Actor,
    BiblioService,
    ROLE_EDITOR,
    ROLE_LIBRARIAN,
    ROLE_PREPRESS,
    ROLE_READER,
)

editor = Actor("编", ROLE_EDITOR)
prepress = Actor("印", ROLE_PREPRESS)
librarian = Actor("馆", ROLE_LIBRARIAN)
reader = Actor("读", ROLE_READER)


def _seed(svc: BiblioService) -> None:
    svc.add_print_numbers(editor, ["PN-1", "PN-2"])
    # 甲译法文、乙译日文；丙第二时期复用
    rows = [
        ("D1", "远舟", "TJIA", "甲", "法文", 1, 6),
        ("D2", "远舟", "TYI", "乙", "日文", 1, 6),
        ("D3", "远舟", "TBING", "丙", None, 7, 9),
    ]
    for i, (did, pen, tid, name, lang, vf, vt) in enumerate(rows):
        svc._insert_version(
            decl_id=did, pen_name=pen, translator_id=tid, translator_name=name,
            source=DeclarationSource("seed-task", i, {"pen_name": "pen"}),
            original_language=lang, volume_from=vf, volume_to=vt,
            withdrawn=False, revision_no=None,
        )
    svc.conn.commit()


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "idx.db"
        self.svc = BiblioService(self.db)
        _seed(self.svc)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def _register_six(self):
        copies = [
            ("V01", 1, "法文", "OLD-1", ["original_language"], False),
            ("V02", 2, "法文", "OLD-2", ["original_language"], False),
            ("V03", 3, "日文", "OLD-3", ["original_language"], False),
            ("V04", 4, "日文", "OLD-4", ["original_language"], False),
            ("V05", 5, "法文", None, ["original_language"], True),
            ("V06", 6, "法文", None, [], False),
        ]
        for cid, vol, lang, pn, vis, printable in copies:
            self.svc.register_copy(
                librarian, cid, volume=vol, edition="ed", pen_name="远舟",
                visible_fields=vis, original_language=lang, print_number=pn,
                printable_new_edition=printable,
            )

    # ------------------------------------------------------------------ #
    def test_register_gives_receipt_and_evaluation(self):
        self._register_six()
        receipts = self.svc.list_receipts()
        self.assertEqual(len(receipts), 6)
        v06 = self.svc._evaluate("V06")
        self.assertFalse(v06.unique)
        self.assertEqual(v06.missing_fields, ("original_language",))
        v03 = self.svc._evaluate("V03")
        self.assertTrue(v03.unique and v03.resolved_translator_id == "TYI")

    def test_reader_cannot_register_or_revise(self):
        with self.assertRaises(BiblioError) as c:
            self.svc.register_copy(reader, "X", 1, "e", "远舟", [])
        self.assertEqual(c.exception.reason, "forbidden")
        with self.assertRaises(BiblioError):
            self.svc.submit_revision(reader, "amend", "D9", note="x",
                                     pen_name="远舟", translator_id="t",
                                     translator_name="n")

    def test_reprint_requires_verified_named_proof_and_print_number(self):
        self._register_six()
        # 无校样不能配号
        with self.assertRaises(BiblioError) as c:
            self.svc.issue_reprint_voucher(librarian, "V05")
        self.assertEqual(c.exception.reason, "no-verified-proof")
        # 非印前岗位不能核签
        with self.assertRaises(BiblioError):
            self.svc.verify_prepress_proof(
                librarian, "P", "V05", "DR", "TJIA", "甲"
            )
        self.svc.verify_prepress_proof(
            prepress, "PRF", "V05", "DR-05", "TJIA", "甲"
        )
        v = self.svc.issue_reprint_voucher(librarian, "V05")
        self.assertEqual(v["print_number"], "PN-1")
        self.assertEqual(v["translator_id"], "TJIA")
        # 印号池剩一个；旧藏不能走重印
        self.assertEqual(self.svc.available_print_numbers(), ["PN-2"])
        with self.assertRaises(BiblioError) as c:
            self.svc.issue_reprint_voucher(librarian, "V06")
        self.assertEqual(c.exception.reason, "not-printable")

    def test_proof_signer_must_be_current_candidate(self):
        self._register_six()
        with self.assertRaises(BiblioError) as c:
            self.svc.verify_prepress_proof(
                prepress, "P", "V05", "DR", "TBING", "丙"
            )
        self.assertEqual(c.exception.reason, "signer-not-candidate")

    def test_inquiry_signs_unique_and_pends_ambiguous(self):
        self._register_six()
        ok = self.svc.issue_inquiry(librarian, "V03")
        self.assertIsNotNone(ok["voucher"])
        self.assertEqual(ok["voucher"]["translator_id"], "TYI")
        pending = self.svc.issue_inquiry(librarian, "V06")
        self.assertIsNone(pending["voucher"])
        self.assertIn("original_language", pending["pending_reason"])
        # 受理凭证仍在
        self.assertTrue(pending["receipt_id"].startswith("R-"))

    def test_revision_limit_and_withdrawal_reforms_candidates(self):
        self._register_six()
        self.svc.submit_revision(
            editor, "amend", "D4", note="n", pen_name="远舟",
            translator_id="TYI", translator_name="乙",
            original_language="法文", volume_from=1, volume_to=6,
        )
        self.assertFalse(self.svc._evaluate("V01").unique)
        self.svc.submit_revision(editor, "withdraw", "D4", note="撤")
        self.assertTrue(self.svc._evaluate("V01").unique)
        self.svc.submit_revision(
            editor, "amend", "D5", note="n", pen_name="远舟",
            translator_id="TYI", translator_name="乙",
            original_language="法文", volume_from=5, volume_to=5,
        )
        with self.assertRaises(BiblioError) as c:
            self.svc.submit_revision(editor, "amend", "D6", note="n",
                                     pen_name="远舟", translator_id="x",
                                     translator_name="x")
        self.assertEqual(c.exception.reason, "revision-limit")
        self.assertEqual(len(self.svc.list_revisions()), 3)

    def test_voucher_snapshot_recallable_after_withdrawal(self):
        self._register_six()
        self.svc.verify_prepress_proof(prepress, "P", "V05", "DR", "TJIA", "甲")
        self.svc.issue_reprint_voucher(librarian, "V05")
        self.svc.submit_revision(
            editor, "amend", "D4", note="n", pen_name="远舟",
            translator_id="TYI", translator_name="乙",
            original_language="法文", volume_from=1, volume_to=6,
        )
        self.svc.submit_revision(editor, "withdraw", "D4", note="撤")
        recall = self.svc.recall_voucher("V-V05")
        ids = {(d["decl_id"], d["version"]) for d in recall["declaration_versions_then"]}
        self.assertEqual(ids, {("D1", 1)})  # 锁签发当时版本，不受后续修订影响
        # D4 两个版本都仍可按版本号取回
        self.assertFalse(self.svc.get_declaration_version("D4", 1).withdrawn)
        self.assertTrue(self.svc.get_declaration_version("D4", 2).withdrawn)

    def test_errata_keeps_old_voucher_and_reuses_print_number(self):
        self._register_six()
        self.svc.verify_prepress_proof(prepress, "P", "V05", "DR", "TJIA", "甲")
        old = self.svc.issue_reprint_voucher(librarian, "V05")
        used = old["print_number"]
        available_before = len(self.svc.available_print_numbers())
        # 乙先成为卷五候选（第 1 次修订）
        self.svc.submit_revision(
            editor, "amend", "D5", note="复核", pen_name="远舟",
            translator_id="TYI", translator_name="乙",
            original_language="法文", volume_from=5, volume_to=5,
        )
        self.svc.verify_prepress_proof(prepress, "P2", "V05", "DR", "TYI", "乙")
        out = self.svc.issue_errata(editor, "V-V05", "TYI", "改归乙")
        self.assertEqual(out["old_voucher"]["status"], "superseded")
        self.assertEqual(out["new_voucher"]["print_number"], used)
        # 不占新印号
        self.assertEqual(len(self.svc.available_print_numbers()), available_before)
        # 非候选改归属被拒
        with self.assertRaises(BiblioError):
            self.svc.issue_errata(editor, "V2-V05", "TBING", "乱改")

    def test_print_number_trace_and_public_view_hides_proof(self):
        self._register_six()
        self.svc.verify_prepress_proof(prepress, "P", "V05", "DR", "TJIA", "甲")
        self.svc.issue_reprint_voucher(librarian, "V05")
        trace = self.svc.trace_print_number("PN-1")
        self.assertEqual(trace["copy"]["copy_id"], "V05")
        self.assertTrue(trace["voucher_chain"])
        pub = self.svc.public_lookup(5)[0]
        self.assertIsNone(pub["verified_proof"])  # 读者看不到私藏校样
        staff = self.svc.lookup_by_volume(5)[0]
        self.assertIsNotNone(staff["verified_proof"])  # 馆员看得到核签状态

    def test_reopen_preserves_everything(self):
        self._register_six()
        self.svc.verify_prepress_proof(prepress, "P", "V05", "DR", "TJIA", "甲")
        self.svc.issue_reprint_voucher(librarian, "V05")
        self.svc.submit_revision(
            editor, "amend", "D4", note="n", pen_name="远舟",
            translator_id="TYI", translator_name="乙",
            original_language="法文", volume_from=1, volume_to=6,
        )
        self.svc.close()
        with BiblioService(self.db) as reopened:
            self.assertTrue(reopened._evaluate("V01").status == "ambiguous")
            v = reopened.get_voucher("V-V05")
            self.assertEqual(v["print_number"], "PN-1")
            self.assertEqual(len(reopened.list_receipts()), 6)
            self.assertEqual(reopened.available_print_numbers(), ["PN-2"])


if __name__ == "__main__":
    unittest.main()
