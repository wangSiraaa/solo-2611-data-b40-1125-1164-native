"""制牌员：把同一块牌的凸点遮挡、可摸边缘与允许翻转，折成可触摸图样集合。

输入是两份归一任务的产物（:class:`PlateBundle` / :class:`BaseBundle`）与一个
方向标记策略；输出 :class:`FabricatedSign`，其 ``source`` 同时挂接点阵任务与
底座任务的实际行、列映射和归档版本，保证每块牌都能回溯到两份任务。
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import FabricatedSign, MarkerPolicy, NoMarkerPolicy, read_sign
from .plates import BaseBundle, PlateBundle


@dataclass
class FabricationOrder:
    """一块牌的制牌工单：哪块刻样配哪个底座。"""

    sign_id: str
    plate_id: str
    base_id: str


def fabricate(
    orders: list[FabricationOrder],
    plate_bundle: PlateBundle,
    base_bundle: BaseBundle,
    policy: MarkerPolicy | None = None,
) -> dict[str, FabricatedSign]:
    """按工单逐块制牌，返回 sign_id -> 可触摸图样集合。"""
    policy = policy or NoMarkerPolicy()
    signs: dict[str, FabricatedSign] = {}
    for order in orders:
        plate = plate_bundle.get(order.plate_id)
        base = base_bundle.get(order.base_id)
        sign = read_sign(
            plate,
            base,
            policy,
            source={
                "sign_id": order.sign_id,
                "dot_task": plate_bundle.sources[order.plate_id],
                "base_task": base_bundle.sources[order.base_id],
                "marker_policy": policy.describe(),
            },
        )
        signs[order.sign_id] = sign
    return signs
