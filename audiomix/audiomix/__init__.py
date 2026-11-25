"""audiomix：多采样率剪辑时间轴、真实混音档案与车站报站词库。

时间轴/混音档案子系统依赖 NumPy/SciPy；若运行环境缺少这些依赖，
仅降级该子系统，纯标准库的车站报站词库与出声台（``audiomix.announce``）
仍可独立导入与使用。
"""
try:  # 既有混音子系统依赖 NumPy/SciPy，缺失时只降级这一部分
    from .assets import AssetRegistry
    from .timeline import TimelineEditor, Clip, ValidationError
    from .engine import (
        render_reference, render_block, render_full_vectorized,
        validate_product, output_sample_count, BLOCK_SAMPLES, OUTPUT_RATE,
    )
    from .archive import ArchiveStore, block_key_of

    __all__ = [
        "AssetRegistry", "TimelineEditor", "Clip", "ValidationError",
        "render_reference", "render_block", "render_full_vectorized",
        "validate_product", "output_sample_count", "BLOCK_SAMPLES", "OUTPUT_RATE",
        "ArchiveStore", "block_key_of",
    ]
except ModuleNotFoundError as _exc:  # pragma: no cover - 取决于运行环境
    if getattr(_exc, "name", "") not in {"numpy", "scipy"}:
        raise
    import warnings

    # announce 纯标准库即可用：静默降级混音子系统，不打扰报站词库的正常使用。
    warnings.filterwarnings(
        "ignore",
        message=r"未安装 NumPy/SciPy.*",
        category=UserWarning,
    )
    warnings.warn(
        "未安装 NumPy/SciPy，时间轴/混音档案子系统不可用；"
        "audiomix.announce 车站报站词库可正常使用。",
        stacklevel=2,
    )
    __all__ = []
