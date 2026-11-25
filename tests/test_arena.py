"""楹联擂台合同测试：审读台、题匾/封匣两套赛制、名额承诺、词册改版与回放。

小例严格按题面：
* 白露：整词名仄（bailu-whole）；拆开 白=形平、露=名仄；
* 清霜：只录整词名平；晚霞：只录 晚=形仄、霞=名平；
* 上联白露，甲投清霜、乙投晚霞；读音取词条唯一读音。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from confcat.catalog import Catalog
from confcat.errors import ArenaError
from confcat.ingest import TaskRepository
from confcat.arena import (
    PLAQUE,
    SEAL,
    ArenaOffice,
    Wordbook,
    explain_reading,
    review_scroll,
)
from confcat.arena.contest import (
    REJ_AUTHOR_LIMIT,
    REJ_NO_SLOT,
    REJ_PLAQUE_MISMATCH,
    REJ_SEAL_LATE,
)
from confcat.arena.wordbook import REQUIRED_COLUMNS, Entry

WB_CONFIG = """
ingest:
  source_uri: file:///wb.csv
  column_mapping:
    headword: word
    word_class: pos
    final_tone: tone
    readings: reading
    supplement_no: supplement_form
    recorder: signer
"""

SC_CONFIG = """
ingest:
  source_uri: file:///sc.csv
  column_mapping:
    scroll_no: juan_no
    author: author_name
    upper_text: shang_lian
    lower_text: xia_lian
    homophone_marks: tong_yin
    seal_upper: feng_shang
    seal_lower: feng_xia
    seal_at: feng_time
"""

WB_V1 = """word,pos,tone,reading,supplement_form,signer
白露,名,仄,bailu-whole,BL-01,柳签录
白,形,平,bai-split,BL-02,柳签录
露,名,仄,lu-split,BL-02,柳签录
清霜,名,平,qingshuang-whole,QS-01,陶签录
晚,形,仄,wan-split,WX-01,陶签录
霞,名,平,xia-split,WX-01,陶签录
"""

# v2：只撤词册中白露的整词条
WB_V2 = """word,pos,tone,reading,supplement_form,signer
白,形,平,bai-split,BL-02,柳签录
露,名,仄,lu-split,BL-02,柳签录
清霜,名,平,qingshuang-whole,QS-01,陶签录
晚,形,仄,wan-split,WX-01,陶签录
霞,名,平,xia-split,WX-01,陶签录
"""

# v3：还原整词条（补录单号更新为 BL-03，验证溯源随版本固定）
WB_V3 = """word,pos,tone,reading,supplement_form,signer
白露,名,仄,bailu-whole,BL-03,柳签录
白,形,平,bai-split,BL-02,柳签录
露,名,仄,lu-split,BL-02,柳签录
清霜,名,平,qingshuang-whole,QS-01,陶签录
晚,形,仄,wan-split,WX-01,陶签录
霞,名,平,xia-split,WX-01,陶签录
"""

SCROLLS = """juan_no,author_name,shang_lian,xia_lian,tong_yin,feng_shang,feng_xia,feng_time
J01,甲,白露,清霜,,白露,清霜,2026-11-01T09:00:00
J02,乙,白露,晚霞,,白/露,晚/霞,2026-11-01T10:00:00
J03,丙,白露,清霜,,白露,清霜,2026-11-01T11:00:00
J04,丁,白露,晚霞,,白/露,晚/霞,2026-11-01T12:00:00
"""

REPLAY_SCROLLS = """juan_no,author_name,shang_lian,xia_lian,tong_yin,feng_shang,feng_xia,feng_time
J01,甲,白露,清霜,,白露,清霜,2026-11-01T09:00:00
J02,乙,白露,晚霞,,白/露,晚/霞,2026-11-03T12:00:00
"""

DEADLINE = "2026-11-02T00:00:00"


class ArenaFixture:
    def __init__(self, td: Path):
        self.td = td
        (td / "wb_v1.csv").write_text(WB_V1, encoding="utf-8")
        (td / "wb_v2.csv").write_text(WB_V2, encoding="utf-8")
        (td / "wb_v3.csv").write_text(WB_V3, encoding="utf-8")
        (td / "sc.csv").write_text(SCROLLS, encoding="utf-8")
        (td / "sc_replay.csv").write_text(REPLAY_SCROLLS, encoding="utf-8")
        self.cat = Catalog(td / "cat.db")
        self.cat.publish("wb-product", WB_CONFIG)
        self.cat.publish("sc-product", SC_CONFIG)
        self.repo = TaskRepository(td / "task.db", self.cat)
        for tid, prod, csv_name in (
            ("wb-v1", "wb-product", "wb_v1.csv"),
            ("wb-v2", "wb-product", "wb_v2.csv"),
            ("wb-v3", "wb-product", "wb_v3.csv"),
            ("sc-m1", "sc-product", "sc.csv"),
            ("sc-rp", "sc-product", "sc_replay.csv"),
        ):
            self.repo.create_task(tid, prod, "doc-0", td / csv_name)
        self.office = ArenaOffice(td / "arena.db", self.repo)

    def close(self):
        self.office.close()
        self.repo.close()
        self.cat.close()


class ReviewDeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = ArenaFixture(Path(self.tmp.name))

    def tearDown(self):
        self.fx.close()
        self.tmp.cleanup()

    def test_bailu_two_segmentations_and_mini_example_verdicts(self):
        book = self.fx.office.load_wordbook("wb-v1")
        bundle = self.fx.office.load_scroll_bundle("sc-m1")

        # 甲（清霜只录整词名平）：唯一合式读法 = 整词白露（名仄）对整词清霜（名平）
        r_jia = review_scroll(bundle.get("J01"), book)
        self.assertTrue(r_jia.qualified)
        self.assertEqual(len(r_jia.readings), 1)
        reading = r_jia.readings[0]
        self.assertEqual(reading.upper.word_tuple(), ("白露",))
        self.assertEqual(reading.lower.word_tuple(), ("清霜",))
        uw, lw = reading.upper.words[0], reading.lower.words[0]
        self.assertEqual((uw.word_class, uw.final_tone), ("名", "仄"))
        self.assertEqual((lw.word_class, lw.final_tone), ("名", "平"))
        self.assertEqual(uw.reading, "bailu-whole")
        self.assertEqual(lw.reading, "qingshuang-whole")

        # 乙（晚霞只录两个词：形仄/名平）：唯一合式读法 = 白/露 对 晚/霞
        r_yi = review_scroll(bundle.get("J02"), book)
        self.assertTrue(r_yi.qualified)
        self.assertEqual(len(r_yi.readings), 1)
        reading2 = r_yi.readings[0]
        self.assertEqual(reading2.upper.word_tuple(), ("白", "露"))
        self.assertEqual(reading2.lower.word_tuple(), ("晚", "霞"))
        classes = [(w.word_class, w.final_tone) for w in reading2.upper.words]
        self.assertEqual(classes, [("形", "平"), ("名", "仄")])
        classes2 = [(w.word_class, w.final_tone) for w in reading2.lower.words]
        self.assertEqual(classes2, [("形", "仄"), ("名", "平")])

        # 拆白露对整清霜必被淘汰：词数不合（2 ≠ 1）；诊断里留得下依据
        self.assertTrue(any("词数不合" in d for d in r_jia.diagnostics))

    def test_annotation_carries_supplement_and_recorder(self):
        book = self.fx.office.load_wordbook("wb-v1")
        bundle = self.fx.office.load_scroll_bundle("sc-m1")
        reading = review_scroll(bundle.get("J01"), book).readings[0]
        note = explain_reading(reading, book)
        up_word = note["upper"]["words"][0]
        self.assertEqual(up_word["supplement_no"], "BL-01")
        self.assertEqual(up_word["recorder"], "柳签录")
        self.assertTrue(all(p["length_match"] and p["word_class_match"] and p["tone_opposite"]
                            for p in note["pair_checks"]))
        self.assertIn("白露【bailu-whole｜名｜仄】", note["rendered"])

    def test_trace_uses_task_row_and_column_lineage(self):
        book = self.fx.office.load_wordbook("wb-v1")
        trace = book.trace_entry(0)  # 白露整词条
        self.assertEqual((trace.supplement_no, trace.recorder), ("BL-01", "柳签录"))
        targets = {c["target_column"] for c in trace.columns}
        self.assertEqual(targets, set(REQUIRED_COLUMNS))
        # 列来源指向真实原 CSV 列与表头行号
        col = next(c for c in trace.columns if c["target_column"] == "recorder")
        self.assertEqual(col["source_column"], "signer")
        self.assertEqual(col["header_line"], 1)

    def test_wordbook_revision_removes_whole_bailu(self):
        book2 = self.fx.office.load_wordbook("wb-v2")
        bundle = self.fx.office.load_scroll_bundle("sc-m1")
        r = review_scroll(bundle.get("J01"), book2)
        self.assertFalse(r.qualified)  # 只剩拆法，与整词清霜词数不合
        self.assertTrue(any("词数不合" in d for d in r.diagnostics))

    def test_homophone_group_forces_one_reading_and_blocks_mixes(self):
        # 情形一：两个“乐”字落在上下联非对应位置，同音组把它们并成一个组件，
        # 只能整套同读 le 或整套同读 yue，不允许一联 le、另一联 yue 的拼接。
        entries = (
            Entry("乐", "名", "平", ("le", "yue"), 0),
            Entry("雪", "名", "仄", ("xue",), 1),
            Entry("月", "名", "仄", ("yuee",), 2),
        )
        book = Wordbook(task_id="t", product="p", version=1, entries=entries,
                        _rows=(), _lineage=(), _index={})
        book._index = {"乐": (0,), "雪": (1,), "月": (2,)}
        from confcat.arena.scrolls import Scroll

        # 上联“乐雪”（名平+名仄），下联“月乐”（名仄+名平）
        scroll = Scroll(
            scroll_no="T1", author="试", upper_text="乐雪", lower_text="月乐",
            row_no=0, homophone_groups=(("乐",),),
        )
        r = review_scroll(scroll, book)
        self.assertTrue(r.qualified)
        for reading in r.readings:
            up_lo = next(w for w in reading.upper.words if w.word == "乐").reading
            lo_lo = next(w for w in reading.lower.words if w.word == "乐").reading
            self.assertEqual(up_lo, lo_lo)  # 同组字必须同读
        self.assertEqual(
            {next(w for w in rd.upper.words if w.word == "乐").reading for rd in r.readings},
            {"le", "yue"},
        )
        self.assertEqual(len(r.readings), 2)  # 只有两套完整赋值，无拼接读法

        # 情形二：同音组件各词读音集合无交集 -> 该断法无任何合法读法
        entries2 = (
            Entry("乐天", "名", "平", ("letian",), 0),
            Entry("乐地", "名", "仄", ("yuedi",), 1),
        )
        book2 = Wordbook(task_id="t2", product="p", version=1, entries=entries2,
                         _rows=(), _lineage=(), _index={})
        book2._index = {"乐天": (0,), "乐地": (1,)}
        scroll2 = Scroll(
            scroll_no="T2", author="试", upper_text="乐天", lower_text="乐地",
            row_no=0, homophone_groups=(("乐",),),
        )
        r2 = review_scroll(scroll2, book2)
        self.assertFalse(r2.qualified)
        self.assertTrue(any("无交集" in d for d in r2.diagnostics))


class SealRulesetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = ArenaFixture(Path(self.tmp.name))

    def tearDown(self):
        self.fx.close()
        self.tmp.cleanup()

    def test_open_publishes_promise_and_two_authors_fill_wall(self):
        opened = self.fx.office.open_match(
            "M1", SEAL, "wb-v1", "sc-m1", slots=2,
            deadline=DEADLINE, plaque_words=("白露",),
        )
        # 受理时公布最多能承诺的不同作者人数
        self.assertEqual(opened["max_distinct_authors_promised"], 2)
        self.assertEqual(opened["public_plaque"], "白露")  # 场单公示匾取整词读

        v1 = self.fx.office.hang("M1", "J01")
        v2 = self.fx.office.hang("M1", "J02")
        self.assertTrue(v1.accepted and v2.accepted)
        self.assertEqual((v1.slot_no, v2.slot_no), (1, 2))

        wall = self.fx.office.wall("M1")
        self.assertEqual([c["scroll_no"] for c in wall["cells"]], ["J01", "J02"])
        # 承诺落实到卷号
        slots = self.fx.office.promises("M1")["slots"]
        self.assertEqual([(s["slot_no"], s["scroll_no"]) for s in slots],
                         [(1, "J01"), (2, "J02")])
        # 对外只显示获选卷：丙、丁不在墙上
        self.assertEqual(wall["hung_count"], 2)

    def test_quota_exhaustion_and_author_limit(self):
        self.fx.office.open_match(
            "M2", SEAL, "wb-v1", "sc-m1", slots=1, deadline=DEADLINE,
            plaque_words=("白露",),
        )
        first = self.fx.office.hang("M2", "J01")
        self.assertTrue(first.accepted)
        # 只把最后名额交给另一份已签卷（丙 J03 内容同甲但作者不同）
        second = self.fx.office.hang("M2", "J03")
        self.assertFalse(second.accepted)
        self.assertEqual(second.verdict, REJ_NO_SLOT)
        # 甲不能再挂：重复订单直接拒绝受理
        with self.assertRaises(ArenaError):
            self.fx.office.hang("M2", "J01")

    def test_author_one_scroll_even_with_free_slots(self):
        # slots 充足时，同一作者的第二卷仍被作者唯一约束挡住
        extra = Path(self.tmp.name) / "sc_dup_author.csv"
        extra.write_text(
            "juan_no,author_name,shang_lian,xia_lian,tong_yin,feng_shang,feng_xia,feng_time\n"
            "K1,甲,白露,清霜,,白露,清霜,2026-11-01T09:00:00\n"
            "K2,甲,白露,晚霞,,白/露,晚/霞,2026-11-01T10:00:00\n",
            encoding="utf-8",
        )
        self.fx.repo.create_task("sc-dup", "sc-product", "doc-0", extra)
        self.fx.office.open_match(
            "M3", SEAL, "wb-v1", "sc-dup", slots=4, deadline=DEADLINE,
            plaque_words=("白露",),
        )
        self.assertTrue(self.fx.office.hang("M3", "K1").accepted)
        v = self.fx.office.hang("M3", "K2")
        self.assertFalse(v.accepted)
        self.assertEqual(v.verdict, REJ_AUTHOR_LIMIT)

    def test_late_seal_rejected_and_replay_counter_restriction(self):
        opened = self.fx.office.open_match(
            "M4", SEAL, "wb-v1", "sc-m1", slots=2, deadline=DEADLINE,
            plaque_words=("白露",),
        )
        # 回放：词册/原文/题匾照前，乙封注移到截止后，甲签发先照原回执发生
        replay = self.fx.office.replay_match("M4-rp", "M4", "sc-rp", ("J01", "J02"))
        verdicts = {v["scroll_no"]: v for v in replay["verdicts"]}
        self.assertTrue(verdicts["J01"]["accepted"])
        self.assertFalse(verdicts["J02"]["accepted"])
        self.assertEqual(verdicts["J02"]["verdict"], REJ_SEAL_LATE)
        self.assertEqual([c["scroll_no"] for c in replay["wall"]["cells"]], ["J01"])
        # 申诉人按本人卷号对照已用词册和截止回执
        appeal = self.fx.office.appeal("M4-rp", "J02")
        self.assertEqual(appeal["wordbook_used"]["task_id"], "wb-v1")
        self.assertEqual(appeal["deadline"], DEADLINE)
        self.assertTrue(appeal["seal_on_record"]["sealed_at"] > DEADLINE)

    def test_appeal_only_exposes_own_scroll(self):
        self.fx.office.open_match(
            "M5", SEAL, "wb-v1", "sc-m1", slots=2, deadline=DEADLINE,
            plaque_words=("白露",),
        )
        self.fx.office.hang("M5", "J01")
        appeal = self.fx.office.appeal("M5", "J01")
        self.assertEqual(appeal["scroll_no"], "J01")
        serialized = json.dumps(appeal, ensure_ascii=False)
        self.assertNotIn("J02", serialized)  # 不含他人卷号

    def test_seal_mismatch_when_sealed_reading_impossible(self):
        bad = Path(self.tmp.name) / "sc_bad_seal.csv"
        bad.write_text(
            "juan_no,author_name,shang_lian,xia_lian,tong_yin,feng_shang,feng_xia,feng_time\n"
            # 甲封了拆开的上联，但清霜无法拆 -> 封注对不上审读台任何读法
            "B1,甲,白露,清霜,,白/露,清霜,2026-11-01T09:00:00\n",
            encoding="utf-8",
        )
        self.fx.repo.create_task("sc-bad", "sc-product", "doc-0", bad)
        self.fx.office.open_match(
            "M6", SEAL, "wb-v1", "sc-bad", slots=2, deadline=DEADLINE,
            plaque_words=("白露",),
        )
        v = self.fx.office.hang("M6", "B1")
        self.assertFalse(v.accepted)
        self.assertEqual(v.verdict, "seal-mismatch")


class PlaqueRulesetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = ArenaFixture(Path(self.tmp.name))

    def tearDown(self):
        self.fx.close()
        self.tmp.cleanup()

    def test_carved_plaque_governs_all_later_scrolls(self):
        self.fx.office.open_match(
            "P1", PLAQUE, "wb-v1", "sc-m1", slots=4, plaque_words=("白露",),
        )
        v_jia = self.fx.office.hang("P1", "J01")
        v_yi = self.fx.office.hang("P1", "J02")
        self.assertTrue(v_jia.accepted)  # 题匾准整词读、甲按时封好整词标注
        self.assertFalse(v_yi.accepted)
        self.assertEqual(v_yi.verdict, REJ_PLAQUE_MISMATCH)
        wall = self.fx.office.wall("P1")
        self.assertEqual([c["scroll_no"] for c in wall["cells"]], ["J01"])

    def test_plaque_must_match_upper_text(self):
        # 匾词拼接后不等于场上联原文：开赛即拒绝，不能刻这样的匾
        with self.assertRaises(ArenaError):
            self.fx.office.open_match(
                "P2", PLAQUE, "wb-v1", "sc-m1", slots=4,
                plaque_words=("白", "露", "霜"),
            )
        # 匾词虽能拼出原文长度，但含词册未收录的词：同样不得刻匾
        with self.assertRaises(ArenaError):
            self.fx.office.open_match(
                "P2b", PLAQUE, "wb-v1", "sc-m1", slots=4,
                plaque_words=("白", "雾"),
            )

    def test_replay_under_plaque_shows_counter_blocked(self):
        self.fx.office.open_match(
            "P3", PLAQUE, "wb-v1", "sc-m1", slots=4, plaque_words=("白露",),
        )
        replay = self.fx.office.replay_match("P3-rp", "P3", "sc-rp", ("J01", "J02"))
        verdicts = {v["scroll_no"]: v for v in replay["verdicts"]}
        self.assertTrue(verdicts["J01"]["accepted"])
        self.assertEqual(verdicts["J02"]["verdict"], REJ_PLAQUE_MISMATCH)


class RevisionAndReopenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = ArenaFixture(Path(self.tmp.name))

    def tearDown(self):
        self.fx.close()
        self.tmp.cleanup()

    def test_old_hung_scroll_keeps_original_wordbook_after_revision(self):
        # v1 词册下挂出甲卷
        self.fx.office.open_match(
            "R1", SEAL, "wb-v1", "sc-m1", slots=2, deadline=DEADLINE,
            plaque_words=("白露",),
        )
        self.assertTrue(self.fx.office.hang("R1", "J01").accepted)

        # 撤整词条：同一甲卷在 v2 场上不合格
        self.fx.office.open_match(
            "R2", SEAL, "wb-v2", "sc-m1", slots=2, deadline=DEADLINE,
            plaque_words=("白露",),
        )
        v2 = self.fx.office.hang("R2", "J01")
        self.assertFalse(v2.accepted)
        self.assertEqual(v2.verdict, "no-valid-reading")

        # 还原词册（v3），只把最后名额交给另一份已签卷 J03，甲也不能再挂
        self.fx.office.open_match(
            "R3", SEAL, "wb-v3", "sc-m1", slots=1, deadline=DEADLINE,
            plaque_words=("白露",),
        )
        self.assertTrue(self.fx.office.hang("R3", "J03").accepted)
        v3_jia = self.fx.office.hang("R3", "J01")
        self.assertEqual(v3_jia.verdict, REJ_NO_SLOT)

        # 旧卷仍按原词册查：R1 的甲卷标注仍引用 v1 补录单 BL-01
        old = self.fx.office.order("R1", "J01")
        self.assertTrue(old["accepted"])
        self.assertEqual(
            old["annotation"]["upper"]["words"][0]["supplement_no"], "BL-01"
        )
        receipt = self.fx.office.appeal("R1", "J01")
        self.assertEqual(receipt["wordbook_used"]["version"], 1)

    def test_reopen_repository_reviews_all_records(self):
        self.fx.office.open_match(
            "O1", SEAL, "wb-v1", "sc-m1", slots=2, deadline=DEADLINE,
            plaque_words=("白露",),
        )
        self.fx.office.hang("O1", "J01")
        self.fx.office.hang("O1", "J02")
        self.fx.close()
        self.fx.repo.close()
        self.fx.cat.close()

        cat2 = Catalog(self.fx.td / "cat.db")
        repo2 = TaskRepository(self.fx.td / "task.db", cat2)
        with ArenaOffice(self.fx.td / "arena.db", repo2) as office2:
            wall = office2.wall("O1")
            self.assertEqual([c["scroll_no"] for c in wall["cells"]], ["J01", "J02"])
            review = office2.get_review("O1", "J02")
            self.assertTrue(review["qualified"])
            self.assertEqual(
                review["readings"][0]["upper"]["words"][0]["word"], "白"
            )
            appeal = office2.appeal("O1", "J01")
            self.assertEqual(appeal["wordbook_used"]["task_id"], "wb-v1")
        repo2.close()
        cat2.close()

    def test_same_intake_entry_accepts_new_wordbook_and_new_scroll_tasks(self):
        # 同一 TaskRepository 入口：新词条词册与正文新卷都是再递一次归一任务
        before = len(self.fx.repo.list_tasks())
        new_wb = self.fx.td / "wb_extra.csv"
        new_wb.write_text(WB_V1 + "秋风,名,平,qiufeng-whole,QF-01,谢签录\n", encoding="utf-8")
        self.fx.repo.create_task("wb-extra", "wb-product", "doc-0", new_wb)
        new_sc = self.fx.td / "sc_extra.csv"
        new_sc.write_text(
            "juan_no,author_name,shang_lian,xia_lian,tong_yin,feng_shang,feng_xia,feng_time\n"
            "E1,戊,白露,秋风,,白露,秋风,2026-11-01T09:00:00\n",
            encoding="utf-8",
        )
        self.fx.repo.create_task("sc-extra", "sc-product", "doc-0", new_sc)
        self.assertEqual(len(self.fx.repo.list_tasks()), before + 2)
        self.fx.office.open_match(
            "E1-match", SEAL, "wb-extra", "sc-extra", slots=1,
            deadline=DEADLINE, plaque_words=("白露",),
        )
        v = self.fx.office.hang("E1-match", "E1")
        self.assertTrue(v.accepted)
        self.assertEqual(v.annotation["lower"]["words"][0]["supplement_no"], "QF-01")


class IntakeContractTest(unittest.TestCase):
    def test_scroll_over_six_chars_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        td = Path(tmp.name)
        (td / "wb.csv").write_text(WB_V1, encoding="utf-8")
        (td / "sc.csv").write_text(
            "juan_no,author_name,shang_lian,xia_lian,tong_yin,feng_shang,feng_xia,feng_time\n"
            "L1,己,白露白露白露白,清霜,,白露,清霜,2026-11-01T09:00:00\n",
            encoding="utf-8",
        )
        cat = Catalog(td / "cat.db")
        cat.publish("wb-product", WB_CONFIG)
        cat.publish("sc-product", SC_CONFIG)
        repo = TaskRepository(td / "task.db", cat)
        repo.create_task("wb", "wb-product", "doc-0", td / "wb.csv")
        repo.create_task("sc", "sc-product", "doc-0", td / "sc.csv")
        office = ArenaOffice(td / "arena.db", repo)
        with self.assertRaises(ArenaError):
            office.load_scroll_bundle("sc")
        office.close()
        repo.close()
        cat.close()
        tmp.cleanup()

    def test_four_authors_in_edition_data(self):
        # 本届数据含四位作者，每联均不超过六字
        tmp = tempfile.TemporaryDirectory()
        td = Path(tmp.name)
        (td / "wb.csv").write_text(WB_V1, encoding="utf-8")
        (td / "sc.csv").write_text(SCROLLS, encoding="utf-8")
        cat = Catalog(td / "cat.db")
        cat.publish("wb-product", WB_CONFIG)
        cat.publish("sc-product", SC_CONFIG)
        repo = TaskRepository(td / "task.db", cat)
        repo.create_task("wb", "wb-product", "doc-0", td / "wb.csv")
        repo.create_task("sc", "sc-product", "doc-0", td / "sc.csv")
        office = ArenaOffice(td / "arena.db", repo)
        bundle = office.load_scroll_bundle("sc")
        self.assertEqual(len({s.author for s in bundle.scrolls}), 4)
        self.assertTrue(all(len(s.upper_text) <= 6 and len(s.lower_text) <= 6
                            for s in bundle.scrolls))
        office.close()
        repo.close()
        cat.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
