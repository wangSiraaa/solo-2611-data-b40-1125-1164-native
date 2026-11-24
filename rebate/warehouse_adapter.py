"""过磅事实仓 -> 返券柜台 的显式适配器。

柜台不直接猜磅单，而是只消费 Warehouse 的 *真实确认车次* 与 *可回查净重
版本*：

* 只有 ``CONFIRMED`` 运输会被导入；草稿、被取代草稿、不可行评估都不算数。
* 一条确认运输的当前评估给出净重区间 ``[net_lo, net_hi]``；迟到的目录
  更正经 :meth:`Warehouse.reopen` 产生带 ``predecessor_trip`` 的 REVIEW
  运输，适配器按评估链顺序展开成同一车次的多个净重 *版本*，旧版本在柜台
  里归档（``active=0``），与 Warehouse 的不可覆盖历史一一对应。

箱号、转交、允许袋重与自愿局部称量是收集侧事实，由调用方按
``counter.add_box`` / ``add_transfer`` / ``add_bag`` / ``add_partial``
登记；本适配器只负责把过磅事实 *接* 进去，绝不替住户虚构重量。
"""

from __future__ import annotations

from typing import Iterable

from .counter import Counter


def _confirmed_trip_ids(warehouse) -> list[str]:
    return [
        t["trip_id"]
        for t in warehouse.trips()
        if t["status"] == "CONFIRMED"
    ]


def net_version_chain(warehouse, root_trip_id: str) -> list[dict]:
    """把一条确认运输的评估链展开成净重版本序列。

    返回 ``[{"trip_id", "version", "net":[lo,hi], "source"}]``，顺序即
    原始确认运输到各次 REVIEW；每条都带可回查的前序身份。
    """
    history = warehouse.evaluation_history(root_trip_id)
    chain: list[dict] = []
    version = 0
    for rec in history:
        if not rec["feasible"] or rec["net_lo"] is None:
            continue
        version += 1
        chain.append(
            {
                "trip_id": rec["trip_id"],
                "version": version,
                "net": [int(rec["net_lo"]), int(rec["net_hi"])],
                "source": rec["trip_id"],
            }
        )
    return chain


def import_confirmed_trips(
    counter: Counter,
    warehouse,
    trip_ids: Iterable[str] | None = None,
    *,
    box_for_trip: dict[str, str] | None = None,
) -> dict[str, int]:
    """把 Warehouse 的确认车次净重（含 REVIEW 版本链）导入柜台。

    ``box_for_trip`` 可选地为每条车次登记一个承载箱（收集侧最常见的
    一车一箱）；更复杂的跨楼串收/封箱转车由调用方自行 ``add_box`` 与
    ``add_transfer``。返回 ``{root_trip_id: 最新版本号}``。
    """
    box_for_trip = box_for_trip or {}
    if trip_ids is None:
        trip_ids = _confirmed_trip_ids(warehouse)
    latest: dict[str, int] = {}
    for root in trip_ids:
        chain = net_version_chain(warehouse, root)
        if not chain:
            continue
        root_box = box_for_trip.get(root)
        last_version = 0
        for i, entry in enumerate(chain):
            lo, hi = entry["net"]
            # 同一逻辑车次在柜台共用 root trip_id，版本逐版追加。
            last_version = counter.add_trip_version(
                root, lo, hi, source=entry["source"]
            )
            if i == 0 and root_box is not None:
                counter.add_box(root_box, root, include_in_trip=True)
        latest[root] = last_version
    return latest
