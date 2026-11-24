"""接单执行：把扫描结果 + 疑拟工况 + 库存/见证锅清单，按订单逐张出操作单与回单。

回单（receipt）在操作单基础上追加：
- 每只锅用到的**来源记录**（pqscan 结果 Parquet 的 _source_file/_source_row_group/
  _source_row_in_group）以及该来源行是否真的存在（provenance_audit）；
- 交给本单的替换库存、本单拆检报废的见证锅，以及它们**还能否再用**（跨单台账判定）。

跨单台账 StockLedger 保证：一件库存锅一经交出、一只见证锅一经拆检，后续订单不得再用。
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

import pyarrow.parquet as pq

from .decision import OperationSheet, StockLedger, decide
from .model import InventoryItem, ProblemConfig, WitnessPot
from .reconstruct import PotRecords, load_evidence, select_order_pots


@dataclass
class OrderRequest:
    order_id: str
    furnace_id: str
    pot_ids: list[str]
    method: str = "auto"

    def to_dict(self) -> dict[str, Any]:
        return {"order_id": self.order_id, "furnace_id": self.furnace_id,
                "pot_ids": list(self.pot_ids), "method": self.method}


def _resolve(path: str, base_dir: str) -> str:
    """相对路径按 base_dir 解析；绝对路径原样返回。"""
    return path if os.path.isabs(path) else os.path.join(base_dir, path)


def _provenance_audit(pots: list[PotRecords], base_dirs: list[str]) -> dict[str, Any]:
    """核对回单里每条来源记录（原始 Parquet 的文件 + 行组 + 组内行）确实存在且自洽。

    来源列由 pqscan 扫描时写入，指向**原始证据 Parquet**；可能是相对文件名，
    这里依次按 base_dirs 解析，确认该行组存在、行号在组内、且该行确实是这只锅。
    """
    cache: dict[str, Any] = {}

    def _loader(path: str):
        candidates = [path] if os.path.isabs(path) else [os.path.join(b, path) for b in base_dirs]
        for cand in candidates:
            if cand in cache:
                return cache[cand]
        for cand in candidates:
            try:
                pf = pq.ParquetFile(cand)
                cache[cand] = pf
                return pf
            except Exception:  # noqa: BLE001 - 仅用于核对标记不可读
                cache[cand] = None
        return None

    audit: dict[str, Any] = {}
    for pot in pots:
        checks = []
        for r in pot.rows:
            pf = _loader(r.source_file)
            if pf is None:
                checks.append({**r.provenance(), "exists": False, "reason": "来源文件不可读"})
                continue
            n_rg = pf.num_row_groups
            if not (0 <= r.source_row_group < n_rg):
                checks.append({**r.provenance(), "exists": False, "reason": "行组越界"})
                continue
            rg_rows = pf.metadata.row_group(r.source_row_group).num_rows
            if not (0 <= r.source_row_in_group < rg_rows):
                checks.append({**r.provenance(), "exists": False, "reason": "组内行号越界"})
                continue
            actual = pf.read_row_group(r.source_row_group, columns=["pot_id"]).column("pot_id")[r.source_row_in_group].as_py()
            match = actual == pot.pot_id
            checks.append({
                **r.provenance(),
                "exists": True,
                "pot_id_matches": match,
                "actual_pot_id": actual,
            })
        if not checks:
            checks.append({"exists": False, "reason": "该锅号在扫描结果中没有命中行"})
        audit[pot.pot_id] = {
            "hanger_id": pot.hanger_id,
            "rows": checks,
            "all_exist": all(c.get("exists") and c.get("pot_id_matches", True) for c in checks),
        }
    return audit


def process_order(
    request: OrderRequest,
    evidence_paths: list[str],
    problem: ProblemConfig,
    inventory: list[InventoryItem],
    witnesses: list[WitnessPot],
    ledger: StockLedger,
    orientation_overrides: dict[tuple[str, str], int] | None = None,
    base_dir: str | None = None,
) -> dict[str, Any]:
    """处理一张订单：重建证据 → 决策 → 提交台账 → 生成回单。

    orientation_overrides 供「事后补一条架向读数」的演示使用；为空则纯用扫描证据。
    base_dir 用于解析相对证据路径与相对溯源文件名（夹具可移植所需）。
    """
    resolved_evidence = [_resolve(p, base_dir) if base_dir else p for p in evidence_paths]
    all_pots, hangers = load_evidence(resolved_evidence, orientation_overrides=orientation_overrides)
    furnace_pots = [p for p in all_pots if p.furnace_id == request.furnace_id]
    order_pots = select_order_pots(furnace_pots, request.pot_ids, request.furnace_id)

    # 台账扣减：其他订单已交出/已拆检的不得占用。
    avail_inventory = ledger.available_inventory(inventory)
    avail_witnesses = ledger.available_witnesses(witnesses)

    sheet: OperationSheet = decide(
        request.order_id, order_pots, problem, avail_inventory, avail_witnesses,
        method=request.method,
    )
    ledger.commit(sheet)

    reuse = []
    for item_id in sheet.inventory_consumed:
        reuse.append(ledger.reuse_verdict(item_id=item_id))
    for wid in sheet.witnesses_scrapped:
        reuse.append(ledger.reuse_verdict(witness_pot_id=wid))

    # 来源溯源列多为相对文件名；用证据文件所在目录（及显式 base_dir）解析核对。
    audit_dirs = [base_dir] if base_dir else []
    audit_dirs += list(dict.fromkeys(os.path.dirname(os.path.abspath(p)) for p in resolved_evidence))
    audit = _provenance_audit(order_pots, audit_dirs)
    # 挂架朝向证据（证明「同一挂架不能分别选朝向」这一约束的来源）。
    used_hangers = sorted({p.hanger_id for p in order_pots if p.hanger_id})
    hanger_view = [
        hangers[(request.furnace_id, h)].to_dict()
        for h in used_hangers
        if (request.furnace_id, h) in hangers
    ]

    return {
        "order": request.to_dict(),
        "verdict": sheet.verdict,
        "summary": sheet.summary,
        "sheet": sheet.to_dict(),
        "receipt": {
            "source_records_by_pot": {
                d.pot_id: d.source_records for d in sheet.dispositions
            },
            "provenance_audit": audit,
            "hanger_orientation_evidence": hanger_view,
            "inventory_consumed": {
                item_id: {
                    "source": sheet.replacement_sources.get(item_id),
                    "reusable_after_this_order": next(
                        (r["reusable"] for r in reuse if r["id"] == item_id), None),
                }
                for item_id in sheet.inventory_consumed
            },
            "witnesses_scrapped": {
                wid: {"reusable_after_this_order": next(
                    (r["reusable"] for r in reuse if r["id"] == wid), None)}
                for wid in sheet.witnesses_scrapped
            },
            "reuse_checks": reuse,
        },
    }


def run_orders(
    orders: list[OrderRequest],
    evidence_for_order: dict[str, list[str]],
    problem: ProblemConfig,
    inventory: list[InventoryItem],
    witnesses: list[WitnessPot],
    orientation_overrides_by_order: dict[str, dict[tuple[str, str], int]] | None = None,
    base_dir: str | None = None,
) -> dict[str, Any]:
    """按给定顺序处理多张订单，共用一份台账；返回全部回单与最终台账。

    orientation_overrides_by_order 允许对个别订单补一条架向读数（演示用）。
    收尾时给出全库存/全见证锅的「还能否再用」总表，回答售后最关心的两个问题：
    已交出去的替换锅、已预拆的见证锅，是否还能许给别单。
    """
    overrides = orientation_overrides_by_order or {}
    ledger = StockLedger()
    receipts = []
    for req in orders:
        receipts.append(process_order(
            req, evidence_for_order[req.order_id], problem, inventory, witnesses, ledger,
            orientation_overrides=overrides.get(req.order_id), base_dir=base_dir,
        ))

    reuse_report = {
        "inventory_items": {
            it.item_id: ledger.reuse_verdict(item_id=it.item_id) for it in inventory
        },
        "witness_pots": {
            wp.pot_id: ledger.reuse_verdict(witness_pot_id=wp.pot_id) for wp in witnesses
        },
    }
    return {
        "orders": [r["order"] for r in receipts],
        "receipts": receipts,
        "ledger": {
            "consumed_items": dict(ledger.consumed_items),
            "scrapped_witnesses": dict(ledger.scrapped_witnesses),
        },
        "reuse_report": reuse_report,
    }
