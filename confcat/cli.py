"""命令行入口：目录可独立用于人工检查。

示例
====
::

    python -m confcat.cli publish ./data/catalog.db orders-product samples/orders_config.yaml
    python -m confcat.cli products ./data/catalog.db
    python -m confcat.cli show ./data/catalog.db orders-product doc-0
    python -m confcat.cli query ./data/catalog.db orders-product doc-0 ingest/column_mapping
    python -m confcat.cli verify ./data/catalog.db orders-product
    python -m confcat.cli tasks ./data/tasks.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .catalog import Catalog
from .codec import path_str
from .index_service import (
    Actor,
    BiblioService,
    ROLE_EDITOR,
    ROLE_LIBRARIAN,
    ROLE_PREPRESS,
)
from .ingest import TaskRepository

_ROLES = {
    "editor": ROLE_EDITOR,
    "prepress": ROLE_PREPRESS,
    "librarian": ROLE_LIBRARIAN,
}


def _actor(role: str, name: str) -> Actor:
    if role not in _ROLES:
        raise SystemExit(f"未知岗位 {role!r}，可选: {', '.join(_ROLES)}")
    return Actor(name=name, role=_ROLES[role])


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="confcat", description="受限 YAML 配置目录与消费仓")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pub = sub.add_parser("publish", help="发布 YAML 为新产品版本")
    p_pub.add_argument("db")
    p_pub.add_argument("product")
    p_pub.add_argument("yaml_path")
    p_pub.add_argument("--docs", nargs="*", help="多文档命名，顺序对应文档序号")

    p_prod = sub.add_parser("products", help="列出产品与版本")
    p_prod.add_argument("db")

    p_docs = sub.add_parser("docs", help="列出某产品文档")
    p_docs.add_argument("db")
    p_docs.add_argument("product")
    p_docs.add_argument("--version", type=int)

    p_show = sub.add_parser("show", help="导出文档有效配置（普通 JSON 视图）")
    p_show.add_argument("db")
    p_show.add_argument("product")
    p_show.add_argument("doc")
    p_show.add_argument("--version", type=int)

    p_q = sub.add_parser("query", help="查询路径有效值与来源证据")
    p_q.add_argument("db")
    p_q.add_argument("product")
    p_q.add_argument("doc")
    p_q.add_argument("path")
    p_q.add_argument("--version", type=int)

    p_v = sub.add_parser("verify", help="重算哈希并回查配置链完整性")
    p_v.add_argument("db")
    p_v.add_argument("product")
    p_v.add_argument("--version", type=int)

    p_run = sub.add_parser("run-task", help="创建接入任务（消费仓）")
    p_run.add_argument("catalog_db")
    p_run.add_argument("task_db")
    p_run.add_argument("task_id")
    p_run.add_argument("product")
    p_run.add_argument("doc")
    p_run.add_argument("csv_path")
    p_run.add_argument("--version", type=int)

    p_tasks = sub.add_parser("tasks", help="列出消费仓任务")
    p_tasks.add_argument("task_db")
    p_tasks.add_argument("catalog_db")

    p_rows = sub.add_parser("rows", help="逐行查看任务结果")
    p_rows.add_argument("task_db")
    p_rows.add_argument("catalog_db")
    p_rows.add_argument("task_id")

    # ----- 地方译本索引出版 ------------------------------------------------ #
    def _biblio(name: str, help_text: str):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("index_db", help="译本索引库 SQLite 路径")
        p.add_argument("--actor", default="librarian", choices=list(_ROLES))
        p.add_argument("--actor-name", default="cli")
        return p

    p_bload = _biblio("biblio-load", "从已有接入任务装载笔名声明（编辑岗）")
    p_bload.add_argument("catalog_db")
    p_bload.add_argument("task_db")
    p_bload.add_argument("task_id")

    p_brev = _biblio("biblio-revise", "提交一条声明修订（amend/withdraw，编辑岗）")
    p_brev.add_argument("kind", choices=["amend", "withdraw"])
    p_brev.add_argument("decl_id")
    p_brev.add_argument("note")
    p_brev.add_argument("--pen-name")
    p_brev.add_argument("--translator-id")
    p_brev.add_argument("--translator-name")
    p_brev.add_argument("--language")
    p_brev.add_argument("--volume-from", type=int)
    p_brev.add_argument("--volume-to", type=int)

    p_bcopy = _biblio("biblio-receive", "收册并开受理凭证（馆员岗）")
    p_bcopy.add_argument("copy_id")
    p_bcopy.add_argument("volume", type=int)
    p_bcopy.add_argument("edition")
    p_bcopy.add_argument("pen_name")
    p_bcopy.add_argument("--title", default="")
    p_bcopy.add_argument("--language")
    p_bcopy.add_argument("--print-number")
    p_bcopy.add_argument("--visible", nargs="*", default=[],
                         help="该版扉页实际可见的区分字段，如 original_language")
    p_bcopy.add_argument("--printable", action="store_true")

    p_bproof = _biblio("biblio-proof", "印前具名校样核验（印前岗位）")
    p_bproof.add_argument("proof_id")
    p_bproof.add_argument("copy_id")
    p_bproof.add_argument("draft_no")
    p_bproof.add_argument("translator_id")
    p_bproof.add_argument("translator_name")

    p_bpool = _biblio("biblio-pool", "向印号池加入可用印号")
    p_bpool.add_argument("numbers", nargs="+")

    p_breprint = _biblio("biblio-reprint", "重印配号签发证（馆员岗）")
    p_breprint.add_argument("copy_id")

    p_binq = _biblio("biblio-inquiry", "馆员查问：能分则签，问不出登记缺项")
    p_binq.add_argument("copy_id")

    p_berrata = _biblio("biblio-errata", "事后改归属发勘误（编辑岗）")
    p_berrata.add_argument("old_voucher_id")
    p_berrata.add_argument("new_translator_id")
    p_berrata.add_argument("reason")

    p_bvol = _biblio("biblio-lookup-volume", "按卷号查可签依据/待查原因（馆员视图）")
    p_bvol.add_argument("volume", type=int)

    p_bpub = _biblio("biblio-public", "读者视图：只给该版能答的信息")
    p_bpub.add_argument("volume", type=int)

    p_bvoucher = _biblio("biblio-recall", "凭原凭证号调回当时采用的声明版本")
    p_bvoucher.add_argument("voucher_id")

    p_btrace = _biblio("biblio-trace", "从印号倒查书、凭证、声明版本与勘误")
    p_btrace.add_argument("print_number")

    args = parser.parse_args(argv)

    if args.cmd == "publish":
        with Catalog(args.db) as cat:
            info = cat.publish_file(args.product, Path(args.yaml_path), doc_names=args.docs)
        _print(
            {
                "product": info.product,
                "version": info.version,
                "docs": list(info.docs),
                "digest": info.digest,
            }
        )
        return 0

    biblio_cmds = {
        "biblio-load", "biblio-revise", "biblio-receive", "biblio-proof",
        "biblio-pool", "biblio-reprint", "biblio-inquiry", "biblio-errata",
        "biblio-lookup-volume", "biblio-public", "biblio-recall", "biblio-trace",
    }
    if args.cmd in biblio_cmds:
        return _run_biblio(args)

    is_task_cmd = args.cmd in ("run-task", "tasks", "rows")
    catalog_db = args.catalog_db if is_task_cmd else args.db
    with Catalog(catalog_db) as cat:
        if args.cmd == "products":
            _print(
                [
                    {
                        "product": p.product,
                        "version": p.version,
                        "docs": list(p.docs),
                        "source_path": p.source_path,
                        "digest": p.digest,
                        "created_at": p.created_at,
                    }
                    for p in cat.list_products()
                ]
            )
        elif args.cmd == "docs":
            _print(cat.list_docs(args.product, args.version))
        elif args.cmd == "show":
            _print(cat.export_plain(args.product, args.doc, args.version))
        elif args.cmd == "query":
            r = cat.query(args.product, args.doc, args.path, args.version)
            _print(
                {
                    "product": r.product,
                    "version": r.version,
                    "doc": r.doc_name,
                    "path": path_str(r.path),
                    "value": r.value,
                    "provenance": r.provenance.as_dict() if r.provenance else None,
                }
            )
        elif args.cmd == "verify":
            _print(cat.verify(args.product, args.version))
        elif is_task_cmd:
            with TaskRepository(args.task_db, cat) as repo:
                if args.cmd == "run-task":
                    summary = repo.create_task(
                        args.task_id, args.product, args.doc, args.csv_path, args.version
                    )
                    _print(summary.__dict__ | {"partitions": summary.partitions})
                elif args.cmd == "tasks":
                    _print([s.__dict__ for s in repo.list_tasks()])
                else:
                    _print(
                        {
                            "summary": repo.get_task(args.task_id).__dict__,
                            "params": repo.get_params(args.task_id),
                            "lineage": repo.get_lineage(args.task_id),
                            "rows": repo.get_rows(args.task_id),
                        }
                    )
    return 0


def _run_biblio(args) -> int:
    actor = _actor(args.actor, args.actor_name)
    with BiblioService(args.index_db) as svc:
        if args.cmd == "biblio-load":
            with Catalog(args.catalog_db) as cat2, TaskRepository(args.task_db, cat2) as repo:
                out = svc.load_declarations_from_task(actor, repo, args.task_id)
                _print([d.as_dict() for d in out])
        elif args.cmd == "biblio-revise":
            out = svc.submit_revision(
                actor, args.kind, args.decl_id, args.note,
                pen_name=args.pen_name, translator_id=args.translator_id,
                translator_name=args.translator_name,
                original_language=args.language,
                volume_from=args.volume_from, volume_to=args.volume_to,
            )
            _print(out.as_dict())
        elif args.cmd == "biblio-receive":
            out = svc.register_copy(
                actor, args.copy_id, args.volume, args.edition, args.pen_name,
                visible_fields=args.visible, title=args.title,
                original_language=args.language, print_number=args.print_number,
                printable_new_edition=args.printable,
            )
            _print(out)
        elif args.cmd == "biblio-proof":
            out = svc.verify_prepress_proof(
                actor, args.proof_id, args.copy_id, args.draft_no,
                args.translator_id, args.translator_name,
            )
            _print(out.__dict__)
        elif args.cmd == "biblio-pool":
            svc.add_print_numbers(actor, args.numbers)
            _print({"available": svc.available_print_numbers()})
        elif args.cmd == "biblio-reprint":
            _print(svc.issue_reprint_voucher(actor, args.copy_id))
        elif args.cmd == "biblio-inquiry":
            _print(svc.issue_inquiry(actor, args.copy_id))
        elif args.cmd == "biblio-errata":
            _print(svc.issue_errata(actor, args.old_voucher_id,
                                    args.new_translator_id, args.reason))
        elif args.cmd == "biblio-lookup-volume":
            _print(svc.lookup_by_volume(args.volume))
        elif args.cmd == "biblio-public":
            _print(svc.public_lookup(args.volume))
        elif args.cmd == "biblio-recall":
            _print(svc.recall_voucher(args.voucher_id))
        else:  # biblio-trace
            _print(svc.trace_print_number(args.print_number))
    return 0


if __name__ == "__main__":
    sys.exit(main())
