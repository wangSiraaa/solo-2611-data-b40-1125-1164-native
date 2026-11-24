"""近义模块名：从 runner 的 results Parquet 执行弹簧检验。"""

from .inspection import inspect as inspect
from .inspection import inspect_spring_batch as inspect_spring_batch
from .inspection import read_selected_result as read_selected_result
from .inspection import run_inspection as run_inspection
from .inspection import run_spring_inspection as run_spring_inspection

__all__ = [
    "inspect",
    "inspect_spring_batch",
    "read_selected_result",
    "run_inspection",
    "run_spring_inspection",
]
