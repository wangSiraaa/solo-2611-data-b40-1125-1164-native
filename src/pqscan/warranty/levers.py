"""在「两架、四只锅」范围内演示三种单点改动如何改变同一张订单：

1. 只补一条架向读数（orientation）：把某挂架缺失的 hot_row 钉死；
2. 只改一件替换库存（inventory）：多给或少给一件合格库存锅；
3. 只改一只可拆见证锅（witness）：换掉/去掉某组的见证锅。

每次都在**全新台账、同一张订单**上重跑，只改动一个输入，然后对比操作单的变化。
三种改动都不读任何拆检结果，符合「不能依据拆开后才知道的结果预先挑分支」。
"""
from __future__ import annotations

from typing import Any

from .decision import StockLedger
from .model import InventoryItem, ProblemConfig, WitnessPot
from .runner import OrderRequest, process_order


def _verdict_and_rows(result: dict[str, Any]) -> dict[str, Any]:
    sheet = result["sheet"]
    return {
        "verdict": sheet["verdict"],
        "rows": {
            d["pot_id"]: {"status": d["status"], "shipped_as": d["shipped_as"],
                          "suspect_possible": d["suspect_possible"]}
            for d in sheet["dispositions"]
        },
        "cuts": [c["group"]["glaze"] + "/" + c["group"]["zone"] for c in sheet["cuts"]],
        "inventory_consumed": sheet["inventory_consumed"],
        "summary": sheet["summary"],
    }


def _run(request: OrderRequest, evidence_paths, problem, inventory, witnesses,
         orientation_overrides=None, base_dir=None) -> dict[str, Any]:
    ledger = StockLedger()
    res = process_order(request, evidence_paths, problem, inventory, witnesses, ledger,
                        orientation_overrides=orientation_overrides, base_dir=base_dir)
    return _verdict_and_rows(res)


def demonstrate_levers(
    request: OrderRequest,
    evidence_paths: list[str],
    problem: ProblemConfig,
    inventory: list[InventoryItem],
    witnesses: list[WitnessPot],
    *,
    extra_orientation: dict[tuple[str, str], int],
    extra_inventory: InventoryItem,
    replacement_witness: WitnessPot,
    removed_witness_id: str,
    base_dir: str | None = None,
) -> dict[str, Any]:
    """对同一张订单分别施加三种单点改动并报告变化。"""
    baseline = _run(request, evidence_paths, problem, inventory, witnesses, base_dir=base_dir)

    # 杠杆 1：只补一条架向读数（整架钉死朝向）。
    l1 = _run(request, evidence_paths, problem, inventory, witnesses,
              orientation_overrides=extra_orientation, base_dir=base_dir)

    # 杠杆 2：只改一件替换库存（追加一件合格库存锅）。
    l2 = _run(request, evidence_paths, problem, inventory + [extra_inventory], witnesses,
              base_dir=base_dir)

    # 杠杆 3：只改一只可拆见证锅（去掉原来的某只看见证锅，换入另一只）。
    swapped_witnesses = [w for w in witnesses if w.pot_id != removed_witness_id]
    swapped_witnesses = swapped_witnesses + [replacement_witness]
    l3 = _run(request, evidence_paths, problem, inventory, swapped_witnesses, base_dir=base_dir)

    return {
        "order": request.to_dict(),
        "baseline": baseline,
        "levers": {
            "orientation_reading_added": {
                "change": {"orientation_overrides": {f"{k[0]}/{k[1]}": v for k, v in extra_orientation.items()}},
                "result": l1,
                "changed": l1["verdict"] != baseline["verdict"],
            },
            "inventory_item_changed": {
                "change": {"added_item": extra_inventory.to_dict()},
                "result": l2,
                "changed": l2["verdict"] != baseline["verdict"] or l2["rows"] != baseline["rows"],
            },
            "witness_pot_changed": {
                "change": {"removed": removed_witness_id, "added": replacement_witness.to_dict()},
                "result": l3,
                "changed": l3["verdict"] != baseline["verdict"] or l3["rows"] != baseline["rows"],
            },
        },
    }
