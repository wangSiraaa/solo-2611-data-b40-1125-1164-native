"""保修接单：用 pqscan 扫出的证据 Parquet 按锅号调取底釉与温区证据，出接单/拒签操作单。

公开入口见 warranty.cli 与 warranty.runner。
"""
from .decision import (
    ACCEPT, ACCEPT_PENDING_CUT, METHODS, REFUSE,
    OperationSheet, StockLedger, decide, resolve_after_cut,
)
from .model import (
    Group, InventoryItem, ProblemConfig, PotEvidence, WitnessPot, WarrantyError,
    problem_config_from_dict, inventory_from_list, witnesses_from_list,
)
from .reconstruct import HangerOrientation, PotRecords, load_evidence, select_order_pots
from .runner import OrderRequest, process_order, run_orders

__all__ = [
    "ACCEPT", "ACCEPT_PENDING_CUT", "METHODS", "REFUSE",
    "OperationSheet", "StockLedger", "decide", "resolve_after_cut",
    "Group", "InventoryItem", "ProblemConfig", "PotEvidence", "WitnessPot", "WarrantyError",
    "problem_config_from_dict", "inventory_from_list", "witnesses_from_list",
    "HangerOrientation", "PotRecords", "load_evidence", "select_order_pots",
    "OrderRequest", "process_order", "run_orders",
]
