"""生成保修接单用的证据夹具（两炉、四只锅以内，覆盖题目要求的三种「改一笔」演示）。

炉 L1（furnace-alpha）：
  挂架 H1：两排挂位(排 0/1)，见证片读数 CH1 记录 hot_row=0（读数确定）。
    - 锅 A10 在排 0，底釉 GX（疑似致裂组之一）；
    - 锅 A20 在排 1，底釉 GX。
    因 hot_row=0 已确定：A10 处 HIGH、A20 处 LOW。若疑似组是 GX/HIGH，则 A10 有风险、
    A20 无风险 —— 对应题目「读数确定甲在右时，可保甲，但不能拿甲的无裂结果放行乙」。
  挂架 H2：两排挂位，见证片读数缺失（hot_row=NULL）。
    - 锅 B10 在排 0，底釉 GY；
    - 锅 B20 在排 1，底釉 GY。
    朝向不定 => 两只锅都可能落 HIGH 或 LOW，两只都不能仅凭记录断言无风险。

炉 L2（furnace-beta，另一份「炉号及锅号不同的小单」）：
  挂架 H9：hot_row NULL，两只锅 C10/C20 在排 0/1，底釉 GZ。
  用于演示第二张订单、回单溯源与「已交出的替换锅/已拆见证锅不能再用」。

疑似致裂组：GX/HIGH、GY/HIGH、GZ/HIGH。
  —— 即这些底釉只要落在高温区就有暗裂风险；落在低温区安全。

库存替换锅：R1（已合格，group 任意，作为合格库存），R2（第二件备用）。
见证锅：W-GY-HIGH（代表 GY/HIGH，可拆，阴性清组、阳性隔离）、W-GZ-HIGH。
"""
from __future__ import annotations

import json
import os
from typing import Any

from .evidence_io import scan_furnace, write_evidence_raw


def _row(furnace: str, hanger: str, pot: str, row: int, slot: int,
         glaze: str | None, hot_row: int | None, plate: str | None) -> dict[str, Any]:
    return {
        "furnace_id": furnace, "hanger_id": hanger, "pot_id": pot,
        "row": row, "slot": slot, "glaze_batch": glaze,
        "hot_row": hot_row, "witness_plate_id": plate,
    }


# 行组划分：把每只锅独放一组，便于观察 pqscan 的逐行组剪枝与溯源。
ALPHA_ROWS = [
    [_row("furnace-alpha", "H1", "A10", 0, 1, "GX", 0, "CH1")],
    [_row("furnace-alpha", "H1", "A20", 1, 2, "GX", 0, "CH1")],
    [_row("furnace-alpha", "H2", "B10", 0, 1, "GY", None, "CH2")],
    [_row("furnace-alpha", "H2", "B20", 1, 2, "GY", None, "CH2")],
]
BETA_ROWS = [
    [_row("furnace-beta", "H9", "C10", 0, 1, "GZ", None, "CH9")],
    [_row("furnace-beta", "H9", "C20", 1, 2, "GZ", None, "CH9")],
]

PROBLEM = {"problem_groups": [
    {"glaze": "GX", "zone": "high"},
    {"glaze": "GY", "zone": "high"},
    {"glaze": "GZ", "zone": "high"},
]}

INVENTORY = [
    {"item_id": "R1", "glaze_batch": "GX", "zone": "low", "source": {"note": "库存合格锅 R1"}},
    {"item_id": "R2", "glaze_batch": "GY", "zone": "low", "source": {"note": "库存合格锅 R2"}},
]

WITNESSES = [
    {"pot_id": "W-GY-HIGH", "glaze_batch": "GY", "zone": "high", "source": {"note": "可拆见证锅"}},
    {"pot_id": "W-GZ-HIGH", "glaze_batch": "GZ", "zone": "high", "source": {"note": "可拆见证锅"}},
]

ORDER_ALPHA = {"order_id": "ORD-A", "furnace_id": "furnace-alpha", "pot_ids": ["A10", "A20", "B10", "B20"], "method": "auto"}
ORDER_BETA = {"order_id": "ORD-B", "furnace_id": "furnace-beta", "pot_ids": ["C10", "C20"], "method": "auto"}


def build(base_dir: str) -> dict[str, Any]:
    """写出原始证据、经 pqscan 扫描的结果 Parquet，以及配套 JSON。

    路径在夹具内部一律用**相对 base_dir 的文件名**，使提交到仓库的夹具可移植：
    扫描写入的 _source_file 溯源也只含文件名，加载时相对配置文件目录解析。
    """
    os.makedirs(base_dir, exist_ok=True)
    prev = os.getcwd()
    os.chdir(base_dir)
    try:
        raw_name = "evidence_raw.parquet"
        alpha_name = "scanned_alpha.parquet"
        beta_name = "scanned_beta.parquet"
        write_evidence_raw(raw_name, ALPHA_ROWS + BETA_ROWS)
        scan_alpha = scan_furnace(raw_name, "furnace-alpha", alpha_name)
        scan_beta = scan_furnace(raw_name, "furnace-beta", beta_name)

        cfg = {
            "problem": PROBLEM,
            "inventory": INVENTORY,
            "witnesses": WITNESSES,
            "orders": [ORDER_ALPHA, ORDER_BETA],
            "evidence_for_order": {"ORD-A": [alpha_name], "ORD-B": [beta_name]},
        }
        cfg_name = "warranty_config.json"
        with open(cfg_name, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    finally:
        os.chdir(prev)

    raw = os.path.join(base_dir, raw_name)
    return {
        "raw": raw,
        "scanned": {"ORD-A": os.path.join(base_dir, alpha_name),
                    "ORD-B": os.path.join(base_dir, beta_name)},
        "config": os.path.join(base_dir, cfg_name),
        "scan_alpha": scan_alpha,
        "scan_beta": scan_beta,
    }


if __name__ == "__main__":
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    info = build(os.path.join(here, "fixtures", "warranty"))
    print(json.dumps(info, ensure_ascii=False, indent=2))
