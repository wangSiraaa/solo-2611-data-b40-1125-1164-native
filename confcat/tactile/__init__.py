"""虚构“触读地图展”的制牌与布牌工具（confcat 的触读领域子包）。

两条归一任务复用上层目录与 CSV 管线：
  * 点阵刻样任务（plate 产品，dot 任务行 + 列映射 + 归档产品版本）；
  * 底座朝向任务（base 产品，base 任务行 + 列映射 + 归档产品版本）。

领域模块分工：
  * :mod:`confcat.tactile.domain`  凸点/遮挡/可摸边/方向标记、翻转与触读签名；
  * :mod:`confcat.tactile.plates`  两条归一任务的行 -> 领域对象（含版本血缘）；
  * :mod:`confcat.tactile.fabricator`  制牌员：把同一块牌折成可触摸图样集合；
  * :mod:`confcat.tactile.placement`   布牌员：触读唯一性、混淆两次触读、布展规划；
  * :mod:`confcat.tactile.cli`         人工检查命令。
"""

from .domain import (
    LEFT,
    RIGHT,
    BaseSpec,
    FabricatedSign,
    FeltReading,
    MarkerPolicy,
    AnchorEndPolicy,
    NoMarkerPolicy,
    PlateSpec,
    flip_index,
    read_sign,
    signature_key,
)

__all__ = [
    "LEFT",
    "RIGHT",
    "BaseSpec",
    "FabricatedSign",
    "FeltReading",
    "MarkerPolicy",
    "AnchorEndPolicy",
    "NoMarkerPolicy",
    "PlateSpec",
    "flip_index",
    "read_sign",
    "signature_key",
]
