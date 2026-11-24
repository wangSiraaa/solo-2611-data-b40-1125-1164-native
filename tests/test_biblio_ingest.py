"""编目端从已有接入任务装载声明的集成测试（归一行 + 列血缘）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from confcat.catalog import Catalog
from confcat.ingest import TaskRepository
from confcat.index_service import Actor, BiblioService, ROLE_EDITOR

CONFIG = """
ingest:
  source_uri: file:///decls.csv
  column_mapping:
    decl_id: decl_id
    pen_name: pen
    translator_id: translator
    translator_name: tname
    original_language: lang
    volume_from: vol_start
    volume_to: vol_end
    note: src_note
"""

CSV = """decl_id,pen,translator,tname,lang,vol_start,vol_end,src_note
D1,远舟,TJIA,甲,法文,1,6,初刊署名页
D2,远舟,TYI,乙,日文,1,6,初刊署名页
"""


class LoadFromTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cat_db, self.task_db, self.idx_db = d / "c.db", d / "t.db", d / "i.db"
        self.csv = d / "decls.csv"
        self.csv.write_text(CSV, encoding="utf-8")
        self.cat = Catalog(self.cat_db)
        self.cat.publish("biblio-product", CONFIG)

    def tearDown(self):
        self.cat.close()
        self.tmp.cleanup()

    def test_load_carries_row_and_column_lineage(self):
        with TaskRepository(self.task_db, self.cat) as repo:
            repo.create_task("seed", "biblio-product", "doc-0", self.csv)
            with BiblioService(self.idx_db) as svc:
                loaded = svc.load_declarations_from_task(
                    Actor("编", ROLE_EDITOR), repo, "seed"
                )
                self.assertEqual([d.decl_id for d in loaded], ["D1", "D2"])
                d1 = svc.get_declaration_version("D1", 1)
                # 行号、任务号、原 CSV 列血缘全部连得到
                self.assertEqual(d1.source.task_id, "seed")
                self.assertEqual(d1.source.row_no, 0)
                self.assertEqual(d1.source.columns["pen_name"], "pen")
                self.assertEqual(d1.source.columns["original_language"], "lang")
                self.assertEqual(d1.original_language, "法文")
                self.assertEqual((d1.volume_from, d1.volume_to), (1, 6))
                # 重复装载不产生新版本
                again = svc.load_declarations_from_task(
                    Actor("编", ROLE_EDITOR), repo, "seed"
                )
                self.assertEqual(again, [])
                self.assertEqual(svc._next_version("D1"), 2)


if __name__ == "__main__":
    unittest.main()
