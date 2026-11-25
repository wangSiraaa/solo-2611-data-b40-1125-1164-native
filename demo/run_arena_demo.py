"""楹联擂台端到端演示：两类归一任务同入口受理 -> 审读 -> 两套赛制 -> 挂卷/回放/改版。

运行：``python demo/run_arena_demo.py``
产物：``data/demo_arena_catalog.db``、``data/demo_arena_tasks.db``、
      ``data/demo_arena.db`` 以及控制台报告（含断言）。

叙事线严格对题面：
1. 白露两种断法（整词名仄 / 拆成形平+名仄），清霜只录整词名平，晚霞只录两词；
2. 封匣制：甲（清霜）、乙（晚霞）各有合式具名封注，墙上两格，名额承诺到卷号；
3. 同场回放：乙封注移到截止后，甲先照原回执签发，到期封匣拦住乙；
4. 题匾制对照：公示匾取整词读，乙的拆词读法不守原匾；
5. 词册改版：撤白露整词条则甲卷不合格；还原后最后名额给丙，甲不能再挂；
   旧卷仍按原词册（补录单 BL-01）查。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from confcat.catalog import Catalog
from confcat.ingest import TaskRepository
from confcat.arena import ArenaOffice, PLAQUE, SEAL

CATALOG_DB = ROOT / "data" / "demo_arena_catalog.db"
TASK_DB = ROOT / "data" / "demo_arena_tasks.db"
ARENA_DB = ROOT / "data" / "demo_arena.db"
for f in (CATALOG_DB, TASK_DB, ARENA_DB):
    f.unlink(missing_ok=True)

DEADLINE = "2026-11-02T00:00:00"


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_verdict(v: dict) -> None:
    flag = "获选上墙" if v["accepted"] else "拒收"
    print(f"  卷 {v['scroll_no']}（{v['author']}）-> {flag}：{v['verdict']}｜{v['reason']}")


def main() -> int:
    # 0. 目录发布：词册与收卷两份归一配置 ---------------------------------- #
    hr("0) 发布两份归一任务配置（词册 / 带卷号参赛原文），同一 TaskRepository 入口")
    with Catalog(CATALOG_DB) as cat:
        cat.publish_file("wordbook-product", ROOT / "samples" / "arena_wordbook_config.yaml")
        cat.publish_file("scroll-product", ROOT / "samples" / "arena_scrolls_config.yaml")

        with TaskRepository(TASK_DB, cat) as repo, ArenaOffice(ARENA_DB, repo) as office:
            # 1) 两类归一任务：换词册/换新卷都是再递一次任务 ---------------- #
            hr("1) 受理归一任务：v1/v2/v3 三版词册 + 正赛卷 + 回放卷")
            repo.create_task("wb-v1", "wordbook-product", "doc-0",
                             ROOT / "data" / "arena" / "wordbook_v1.csv")
            repo.create_task("wb-v2", "wordbook-product", "doc-0",
                             ROOT / "data" / "arena" / "wordbook_v2.csv")
            repo.create_task("wb-v3", "wordbook-product", "doc-0",
                             ROOT / "data" / "arena" / "wordbook_v3.csv")
            repo.create_task("sc-m1", "scroll-product", "doc-0",
                             ROOT / "data" / "arena" / "scrolls_match01.csv")
            repo.create_task("sc-rp", "scroll-product", "doc-0",
                             ROOT / "data" / "arena" / "scrolls_replay.csv")
            print("任务清单:", [t.task_id for t in repo.list_tasks()])

            book = office.load_wordbook("wb-v1")
            bundle = office.load_scroll_bundle("sc-m1")
            print("词册坐标:", book.digest())
            print("本场卷号:", [s.scroll_no for s in bundle.scrolls],
                  "作者:", sorted({s.author for s in bundle.scrolls}))

            # 2) 白露两种断法的溯源（补录单单号 + 真实签录人） -------------- #
            hr("2) 同一句“白露”的两种断法与补录单溯源（沿 TaskRepository 行/列血缘）")
            for row_no in (0, 1, 2):
                entry = book.entry_at(row_no)
                trace = book.trace_entry(row_no)
                print(f"  {entry.headword}｜{entry.word_class}｜词末{entry.final_tone}｜"
                      f"读音{entry.readings} <- 补录单 {trace.supplement_no} / 签录人 {trace.recorder}")
            assert book.trace_entry(0).supplement_no == "BL-01"
            assert book.trace_entry(1).recorder == "柳签录"

            # 3) 封匣制正赛：受理时公布承诺名额，甲乙上墙两格 --------------- #
            hr("3) 封匣制开场：公布最多可承诺不同作者人数，预跑全场审读")
            opened = office.open_match(
                "match-seal", SEAL, "wb-v1", "sc-m1", slots=2,
                deadline=DEADLINE, plaque_words=("白露",),
            )
            print("  赛制:", opened["ruleset"],
                  "| 公示匾:", opened["public_plaque"],
                  "| 承诺作者人数:", opened["max_distinct_authors_promised"])
            print("  审读摘要:", json.dumps(opened["review_summary"], ensure_ascii=False))

            print("\n  按卷号查审读台实际给出的读法：")
            for no in ("J01", "J02"):
                rv = office.get_review("match-seal", no)
                print(f"  {no}:", json.dumps(rv["readings"], ensure_ascii=False))

            hr("3a) 挂卷订单：甲、乙合式封注上墙；丙、丁满额拒收")
            for no in ("J01", "J02", "J03", "J04"):
                show_verdict(office.hang("match-seal", no).as_dict())
            wall = office.wall("match-seal")
            print("\n  获选墙（对外只显示获选卷）:")
            for cell in wall["cells"]:
                print(f"  第{cell['slot_no']}格 卷{cell['scroll_no']} {cell['author']}")
                print("   ", cell["annotation"]["rendered"].replace("\n", "\n    "))
            promises = office.promises("match-seal")["slots"]
            print("  承诺落实:", [(s["slot_no"], s["scroll_no"], s["author"]) for s in promises])
            assert [c["scroll_no"] for c in wall["cells"]] == ["J01", "J02"]

            # 4) 同场回放：乙迟封，甲先签发，到期封匣限制柜台 --------------- #
            hr("4) 同场赛前记录另开回放：词册/原文/题匾照前，乙封注移到截止后")
            replay = office.replay_match(
                "match-replay", "match-seal", "sc-rp", hang_order=("J01", "J02")
            )
            for v in replay["verdicts"]:
                show_verdict(v)
            print("  回放墙上:", [c["scroll_no"] for c in replay["wall"]["cells"]])
            appeal = office.appeal("match-replay", "J02")
            print("  乙按本人卷号查回执: 用词册", appeal["wordbook_used"]["task_id"],
                  "| 截止", appeal["deadline"], "| 裁定", appeal["verdict"])
            assert not replay["verdicts"][1]["accepted"]
            assert replay["verdicts"][1]["verdict"] == "seal-late"

            # 5) 题匾制对照：刻匾限整词读，乙拆词读法不守原匾 --------------- #
            hr("5) 题匾制对照场：赛前刻定整词断法，后来收的卷须守原匾")
            office.open_match("match-plaque", PLAQUE, "wb-v1", "sc-m1",
                              slots=4, plaque_words=("白露",))
            for no in ("J01", "J02"):
                show_verdict(office.hang("match-plaque", no).as_dict())
            assert office.order("match-plaque", "J01")["accepted"]
            assert office.order("match-plaque", "J02")["verdict"] == "plaque-mismatch"

            # 6) 词册改版：撤整词条甲不合格；还原后名额给丙，甲不能再挂 ----- #
            hr("6) 词册改版：撤白露整词条 -> 同一甲卷不合格")
            office.open_match("match-v2", SEAL, "wb-v2", "sc-m1", slots=2,
                              deadline=DEADLINE, plaque_words=("白露",))
            v2 = office.hang("match-v2", "J01")
            show_verdict(v2.as_dict())
            print("  淘汰依据:", office.get_review("match-v2", "J01")["diagnostics"])
            assert not v2.accepted and v2.verdict == "no-valid-reading"

            hr("6a) 还原词册（v3），只把最后名额交给另一份已签卷 J03，甲不能再挂")
            office.open_match("match-v3", SEAL, "wb-v3", "sc-m1", slots=1,
                              deadline=DEADLINE, plaque_words=("白露",))
            show_verdict(office.hang("match-v3", "J03").as_dict())
            show_verdict(office.hang("match-v3", "J01").as_dict())
            assert office.order("match-v3", "J03")["accepted"]
            assert office.order("match-v3", "J01")["verdict"] == "no-slot"

            hr("6b) 已挂出的旧卷仍按原版词册查（v1 补录单 BL-01，非 v3 的 BL-03）")
            old = office.order("match-seal", "J01")
            print("  旧卷裁定:", old["verdict"], "| 旧标注补录单:",
                  old["annotation"]["upper"]["words"][0]["supplement_no"])
            old_receipt = office.appeal("match-seal", "J01")
            print("  回执固定词册版本:", old_receipt["wordbook_used"])
            assert old["annotation"]["upper"]["words"][0]["supplement_no"] == "BL-01"
            assert old_receipt["wordbook_used"]["version"] == 1

    # 7. 重开三库：裁定、墙格、回执全部可回查 ------------------------------- #
    hr("7) 重开目录/任务/赛务三库：获选墙与截止回执仍可查")
    with Catalog(CATALOG_DB) as cat2, TaskRepository(TASK_DB, cat2) as repo2, \
            ArenaOffice(ARENA_DB, repo2) as office2:
        wall = office2.wall("match-seal")
        print("  重开后墙格:", [(c["slot_no"], c["scroll_no"]) for c in wall["cells"]])
        review = office2.get_review("match-seal", "J02")
        print("  重开后乙卷审读读法数:", len(review["readings"]),
              "| 首词:", review["readings"][0]["upper"]["words"][0]["word"])
        assert [c["scroll_no"] for c in wall["cells"]] == ["J01", "J02"]
        assert review["readings"][0]["upper"]["words"][0]["word"] == "白"

    hr("全部楹联擂台演示断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
