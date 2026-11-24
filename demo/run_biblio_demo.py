"""地方译本索引出版：端到端演示（三人、六册、至多三次修订）。

运行：``python demo/run_biblio_demo.py``
产物：``data/demo_biblio_catalog.db``、``data/demo_biblio_index.db``。

剧情
====
* 甲、乙、丙三位译者都署“远舟”；初刊甲译法文、乙译日文；丙在卷七至卷九的
  第二时期复用同一笔名（区间限定）。
* 六册同时交柜：法文旧印本 V01/V02、日文印本 V03/V04、待印新版法文册 V05、
  删掉语种栏且无印号的旧节印本 V06。
* 编辑部选择“重印配号”办索引：V05 有仅编辑部可见的甲署名印前校样，库里仅一个
  可用印号；旧藏不得凭空改扉页。馆员查问作为不能配号各册的并行入口演示。
* 修订 1：补录一条有出处的“乙译法文”声明 -> 法文册变两人候选；
* 修订 2：撤回该补录 -> 候选恢复；
* 修订 3：具名底稿改归乙，发勘误（旧凭证/旧印号/读者手里的纸都保持原样）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from confcat.biblio import BiblioError
from confcat.catalog import Catalog
from confcat.index_service import (
    Actor,
    BiblioService,
    ROLE_EDITOR,
    ROLE_LIBRARIAN,
    ROLE_PREPRESS,
    ROLE_READER,
)
from confcat.ingest import TaskRepository

CATALOG_DB = ROOT / "data" / "demo_biblio_catalog.db"
INDEX_DB = ROOT / "data" / "demo_biblio_index.db"
for f in (CATALOG_DB, INDEX_DB):
    f.unlink(missing_ok=True)

editor = Actor("沈编辑", ROLE_EDITOR)
prepress = Actor("印前核签员", ROLE_PREPRESS)
librarian = Actor("值班馆员", ROLE_LIBRARIAN)
reader = Actor("持书读者", ROLE_READER)


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def show_ev(tag: str, ev: dict) -> None:
    print(
        f"  {tag}: status={ev['status']} unique={ev['unique']} "
        f"候选={[c['translator_name'] for c in ev['candidates']]} "
        f"可问={ev['answerable_fields']} 缺项={ev['missing_fields']}"
    )


def main() -> int:
    # 1. 用已有任务的归一行+列来源装载声明 ---------------------------------- #
    hr("1) 发布归一配置并跑接入任务，再把笔名声明读入译本目录")
    task_db = ROOT / "data" / "demo_biblio_tasks.db"
    task_db.unlink(missing_ok=True)
    with Catalog(CATALOG_DB) as cat:
        cat.publish_file("biblio-product", ROOT / "samples" / "biblio_config.yaml")
        with TaskRepository(task_db, cat) as repo:
            summary = repo.create_task(
                "biblio-seed", "biblio-product", "doc-0",
                ROOT / "data" / "biblio_declarations.csv",
            )
            print("归一任务行数:", summary.row_count, "列:", list(summary.columns))
            lineage = repo.get_lineage("biblio-seed")
            print("列血缘样例:", [(c["target"], "<-", c["source_column"]) for c in lineage[:3]])

            with BiblioService(INDEX_DB) as svc:
                loaded = svc.load_declarations_from_task(editor, repo, "biblio-seed")
                print("装载声明:", [(d.decl_id, d.translator_name, d.pen_name,
                                    d.original_language,
                                    (d.volume_from, d.volume_to)) for d in loaded])

                # 2. 六册同时交柜 --------------------------------------------- #
                hr("2) 六册同时交柜（每册都开受理凭证，无论能否签出）")
                common_visible = ["original_language"]  # 扉页印了原文语种栏
                svc.add_print_numbers(editor, ["YIN-001"])  # 库里仅一个可用印号
                copies = [
                    # copy_id, volume, edition, 语种, 自带印号, 可见字段, 可印新版
                    ("V01", 1, "初刊印本", "法文", "OLD-101", common_visible, False),
                    ("V02", 2, "初刊印本", "法文", "OLD-102", common_visible, False),
                    ("V03", 3, "初刊印本", "日文", "OLD-103", common_visible, False),
                    ("V04", 4, "初刊印本", "日文", "OLD-104", common_visible, False),
                    ("V05", 5, "待印新版", "法文", None, common_visible, True),
                    ("V06", 6, "旧节印本", "法文", None, [], False),  # 删语种栏、无印号
                ]
                for copy_id, vol, edition, lang, pn, vis, printable in copies:
                    res = svc.register_copy(
                        librarian, copy_id, volume=vol, edition=edition, pen_name="远舟",
                        visible_fields=vis, title=f"卷{vol} {edition}",
                        original_language=lang, print_number=pn,
                        printable_new_edition=printable,
                    )
                    show_ev(f"收册 {copy_id}（{edition}/{lang or '无语种栏'}）",
                            res["evaluation"])
                print("受理凭证:", [r["receipt_id"] for r in svc.list_receipts()])

                # 3. 印前具名校样核验（仅印前岗位，私藏，读者不可见） ------------ #
                hr("3) 印前具名校样核验：V05 底稿号 DR-05 由甲签名，印前岗位核签")
                proof = svc.verify_prepress_proof(
                    prepress, "PRF-05", "V05", draft_no="DR-05",
                    signer_translator_id="TJIA", signer_translator_name="甲",
                )
                print("核签:", proof.proof_id, proof.draft_no, proof.signer_translator_name,
                      "核验人:", proof.verifier, "私藏:", proof.private)
                # 权限：馆员无权核签
                try:
                    svc.verify_prepress_proof(
                        librarian, "PRF-X", "V05", "DR-X", "TJIA", "甲"
                    )
                except BiblioError as e:
                    print("越权核签被拒:", e.reason)

                # 4. 当日签发：重印配号（选定服务）+ 馆员查问并行演示 ------------ #
                hr("4) 当日签发：重印配号给 V05；其余各册走馆员查问，问不出只登记")
                voucher05 = svc.issue_reprint_voucher(librarian, "V05")
                print("V05 重印凭证:", voucher05["voucher_id"],
                      "译者:", voucher05["translator_id"],
                      "印号:", voucher05["print_number"], "状态:", voucher05["status"])
                print("剩余可用印号:", svc.available_print_numbers(), "  # 仅一个，已用尽")
                # 印号用尽后另一新版（若有）不能配号
                try:
                    svc._take_print_number("V99", "V-V99")
                except BiblioError as e:
                    print("再要印号被拒:", e.reason)

                inquiry_outcomes = {}
                for copy_id, *_ in copies:
                    if copy_id == "V05":
                        continue
                    inquiry_outcomes[copy_id] = svc.issue_inquiry(librarian, copy_id)
                for copy_id, out in inquiry_outcomes.items():
                    if out["voucher"]:
                        v = out["voucher"]
                        print(f"  {copy_id} 查问签认: {v['voucher_id']} -> {v['translator_id']}")
                    else:
                        print(f"  {copy_id} 受理查询但不出凭证: {out['pending_reason']}")

                hr("4b) 读者视图：只问手里这版能答的问题，私藏校样不出现")
                for vol in (3, 5, 6):
                    print(f"  卷 {vol} 读者可见:", svc.public_lookup(vol))

                # 5. 修订 1：补录“乙译法文”声明（有出处） --------------------- #
                hr("5) 修订 1：补录乙译法文声明 D4 -> 法文册出现甲乙两个候选")
                svc.submit_revision(
                    editor, "amend", "D4",
                    note="补录：乙译法文，出处《译者通讯》1935 年合订本",
                    pen_name="远舟", translator_id="TYI", translator_name="乙",
                    original_language="法文", volume_from=1, volume_to=6,
                )
                for copy_id in ("V01", "V05", "V06"):
                    show_ev(f"补录后 {copy_id}", svc._evaluate(copy_id).as_dict())
                # 旧凭证仍按当时快照成立（读者手里的纸不变）
                v05_then = svc.get_voucher("V-V05")
                print("V-V05 仍是:", v05_then["translator_id"], v05_then["status"],
                      "（旧凭证不动）")

                # 6. 修订 2：撤回补录 ------------------------------------------ #
                hr("6) 修订 2：撤回 D4 -> 法文册恢复唯一；旧节印本仍问不出语种")
                svc.submit_revision(editor, "withdraw", "D4", note="补录依据不足，撤回")
                for copy_id in ("V01", "V05", "V06"):
                    show_ev(f"撤回后 {copy_id}", svc._evaluate(copy_id).as_dict())
                print("修订日志:", [(r["revision_no"], r["kind"], r["decl_id"])
                                   for r in svc.list_revisions()])

                # 7. 次日带原凭证号调回当时采用的声明版本 ----------------------- #
                hr("7) 次日凭原凭证号调回：V-V05 锁定签发当时的声明版本（其后修订不回改）")
                recall = svc.recall_voucher("V-V05")
                print("凭证号:", recall["voucher"]["voucher_id"],
                      "印号:", recall["voucher"]["print_number"])
                print("签发当时采用的声明版本:",
                      [(d["decl_id"], "v" + str(d["version"]), d["translator_name"],
                        d["withdrawn"]) for d in recall["declaration_versions_then"]])
                # D4 补录(v1)/撤回(v2)两个版本都保留；旧凭证只锁定它签发时的 D1
                old_d4 = svc.get_declaration_version("D4", 1)
                withdrawn_d4 = svc.get_declaration_version("D4", 2)
                print("D4@v1（补录版，仍可独立取回）:", old_d4.translator_name,
                      "withdrawn=", old_d4.withdrawn)
                print("D4@v2（撤回版）:", withdrawn_d4.translator_name,
                      "withdrawn=", withdrawn_d4.withdrawn,
                      "出处仍在:", withdrawn_d4.source.task_id)
                assert [d["decl_id"] for d in recall["declaration_versions_then"]] == ["D1"]

                # 8. 修订 3：改归属发勘误（可追到旧册/旧印号） ------------------ #
                hr("8) 修订 3：乙凭卷五复核依据成为候选，编辑部发勘误")
                # 保护：新译者不在当前候选中时，勘误必须拒绝（不能凭空改扉页）
                try:
                    svc.issue_errata(editor, "V-V05", "TYI", "底稿复核改归乙")
                except BiblioError as e:
                    print("非候选改归属被拒:", e.reason, "（保护：不能凭空改扉页）")
                # 第 3 次修订：乙以卷五区间的法文声明成为 V05 合法候选
                svc.submit_revision(
                    editor, "amend", "D5",
                    note="卷五新版底稿复核：乙为该册译者，出处底稿 DR-05 复核单",
                    pen_name="远舟", translator_id="TYI", translator_name="乙",
                    original_language="法文", volume_from=5, volume_to=5,
                )
                svc.verify_prepress_proof(
                    prepress, "PRF-05B", "V05", draft_no="DR-05",
                    signer_translator_id="TYI", signer_translator_name="乙",
                )
                errata = svc.issue_errata(editor, "V-V05", "TYI", "底稿复核改归乙")
                print("勘误号:", errata["errata_id"])
                print("  旧凭证:", errata["old_voucher"]["voucher_id"],
                      errata["old_voucher"]["translator_id"],
                      "状态:", errata["old_voucher"]["status"],
                      "被取代 ->", errata["old_voucher"]["superseded_by"])
                print("  新凭证:", errata["new_voucher"]["voucher_id"],
                      errata["new_voucher"]["translator_id"],
                      "印号沿用:", errata["new_voucher"]["print_number"])
                print("修订次数:", len(svc.list_revisions()), "/ 3")
                # 读者手里的纸：旧凭证快照原样
                old_paper = svc.get_voucher("V-V05")
                print("读者原纸 V-V05 仍印着:", old_paper["translator_id"],
                      "快照声明数:", len(old_paper["decl_snapshot"]))

                # 9. 从印本印号倒查到书与声明 ------------------------------- #
                hr("9) 从印号倒查：旧册印号 OLD-103 与新版印号 YIN-001")
                trace_old = svc.trace_print_number("OLD-103")
                print("OLD-103 -> 书:", trace_old["copy"]["copy_id"],
                      "凭证:", [v["voucher"]["voucher_id"] for v in trace_old["voucher_chain"]])
                trace_new = svc.trace_print_number("YIN-001")
                print("YIN-001 -> 书:", trace_new["copy"]["copy_id"],
                      "勘误链:", [(e["errata_id"], e["old_translator_id"],
                                  "->", e["new_translator_id"]) for e in trace_new["errata"]])

                # 10. 卷号查卷与第二时期同笔名 ------------------------------- #
                hr("10) 按卷号查卷；丙在卷七至卷九复用“远舟”（区间限定可区分）")
                svc.register_copy(
                    librarian, "V07", volume=7, edition="第二时期印本", pen_name="远舟",
                    visible_fields=["original_language"], title="卷七",
                    original_language="日文", print_number="OLD-107",
                )
                ev07 = svc._evaluate("V07")
                show_ev("V07（卷七）", ev07.as_dict())
                assert ev07.unique and ev07.resolved_translator_id == "TBING"
                vol5 = svc.lookup_by_volume(5)[0]
                print("卷5 馆员视图可签依据:", vol5["signable_basis"],
                      "凭证:", [v["voucher_id"] for v in vol5["vouchers"]])

    hr("全部演示流程完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
