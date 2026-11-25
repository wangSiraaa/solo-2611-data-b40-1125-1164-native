"""果园层错误类型。与 confcat 的合同错误分开，便于上层区分。"""

from __future__ import annotations


class OrchardError(Exception):
    """果园层所有错误的基类。"""


class ColdRevisionConflict(OrchardError):
    """同一个冷天既被记为发生、又被记为没发生（同片区同一天）。

    ``day`` 与 ``region`` 指明冲突日；``occurred_ref`` / ``absent_ref`` 分别是
    “发生”与“没发生”两条断言的来源（任务 + 行号），即“缺的是哪一天哪一边”。
    """

    def __init__(self, region: str, day: int, occurred_ref, absent_ref):
        self.region = region
        self.day = day
        self.occurred_ref = occurred_ref
        self.absent_ref = absent_ref
        super().__init__(
            f"冷天冲突：片区 {region!r} 第 {day} 天同时被记为发生"
            f"（{occurred_ref.task_id}#{occurred_ref.row_index}）与没发生"
            f"（{absent_ref.task_id}#{absent_ref.row_index}）"
        )


class LayoutInfeasible(OrchardError):
    """在给定通道/搬运上限下无法把盆树换到目标邻位。"""

    def __init__(self, message: str, detail: dict | None = None):
        self.detail = detail or {}
        super().__init__(message)
