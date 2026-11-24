"""题面两个桌面案例的可重放构造。

这里把“甲乙各五 / 一车六 / 乙见证一”和“甲乙同时无雪 + 丙堆位 + 岔口观测”
固化成函数。演示和单元测试共用同一套构造，避免文档里的例子与代码行为漂移。
"""

from __future__ import annotations

from typing import Any

from .catalog import Catalog
from .street import (
    BEFORE_FORK,
    METHOD_BAN,
    METHOD_OBSERVE,
    StreetOffice,
)
from .warehouse import IN, OUT, UNLOAD, Warehouse


def exact_unload_warehouse(specs: dict[str, int] | None = None) -> tuple[Warehouse, dict[str, str]]:
    """造空车质量精确为 1000kg 的确认卸雪车次。

    ``specs`` 形如 ``{"T1": 6, "T2": 1}``；返回 Warehouse 和别名到真实
    ``TRIPxxxx`` 车次号的映射。
    """

    catalog = Catalog()
    catalog.define_mass("tractor", "T1", 1000, 1000)
    wb = Warehouse(catalog)
    ids: dict[str, str] = {}
    for i, (alias, net) in enumerate((specs or {"T1": 6}).items(), start=1):
        tin, tout = f"{alias}-IN", f"{alias}-OUT"
        base_ts = 100 * i
        wb.add_ticket({
            "ticket_id": tin, "tractor": "T1", "catalog_seq": 0,
            "ts": base_ts, "direction": IN, "gross": 1000 + int(net),
        })
        wb.add_ticket({
            "ticket_id": tout, "tractor": "T1", "catalog_seq": 0,
            "ts": base_ts + 10, "direction": OUT, "gross": 1000,
        })
        ev = wb.preview(UNLOAD, tin, tout, notes=f"exact tabletop net {net}")
        confirmed = wb.confirm(ev.trip_id)
        ids[alias] = confirmed.trip_id
    return wb, ids


def _three_segment_office(wb: Warehouse | None = None) -> StreetOffice:
    office = StreetOffice(wb)
    office.add_segments("甲", "乙", "丙")
    for sid in ("甲", "乙", "丙"):
        office.add_stall(f"摊-{sid}", sid)
    return office


# ---------------------------------------------------------------------------
# 案例一：五、五、六与三类单点反事实
# ---------------------------------------------------------------------------

def build_witness_case(*, replaced: bool = False) -> dict[str, Any]:
    """同一笔甲段交场承诺；只替换乙段装车见证。"""

    wb, trip_ids = exact_unload_warehouse({"T1": 6})
    office = _three_segment_office(wb)
    t1 = trip_ids["T1"]
    office.set_initial_snow("甲", 5)
    office.set_initial_snow("乙", 5)
    office.set_initial_snow("丙", 0)
    office.reference_confirmed_trip(t1)
    # 装车顺序：先甲后乙。甲的数量没有直接见证，只给非负/不超过净重上界。
    office.add_pickup("装-甲-无见证", t1, "甲", lo=0, hi=6)
    witness_b = 5 if replaced else 1
    office.record_loading_witness(
        "见证-乙", t1, "乙", witness_b,
        note="counterfactual witness" if replaced else "field signed witness",
    )
    rec = office.submit_release_order(
        "SR-见证", segments=["甲"], snow_line=1, method="none",
        trip_ids=[t1], title="甲段清晨腾街单（装车见证分支）",
    )
    return {"office": office, "order": rec, "trip_ids": trip_ids,
            "witness_b": witness_b}


def build_ban_case(*, revoked: bool = False, c_rented: bool = False) -> dict[str, Any]:
    """同一笔甲段承诺，靠前夜禁倒并改道丙执行。"""

    wb, trip_ids = exact_unload_warehouse({"T1": 6})
    office = _three_segment_office(wb)
    t1 = trip_ids["T1"]
    office.set_initial_snow("甲", 5)
    office.set_initial_snow("乙", 5)
    office.set_initial_snow("丙", 0)
    office.add_slot("丙位-1kg", "丙", 1)
    if c_rented:
        office.prior_rent_slot("丙位-1kg", "别队先租凭据", 1, lease_id="先租-丙")
    office.reference_confirmed_trip(t1)
    office.add_pickup("装-甲", t1, "甲", lo=0, hi=6)
    office.record_loading_witness("见证-乙", t1, "乙", 1)

    office.create_storage_permit(
        "ST-暂存", 1, holder="邻队", possible_segments=["甲", "丙"],
        title="邻队一公斤暂存单",
    )
    ban_error: Exception | None = None
    try:
        office.sign_no_dump_order(
            "ND-禁倒甲", permit_id="ST-暂存", protected_segments=["甲"],
            destination_segment="丙", slot_id="丙位-1kg",
        )
        office.sign_delivered_slot("ND-禁倒甲")
        if revoked:
            office.revoke_no_dump_order("ND-禁倒甲", reason="只撤这一块禁倒权")
    except Exception as exc:  # 场景需要把拒单也返回给演示/测试
        ban_error = exc

    rec = office.submit_release_order(
        "SR-禁倒", segments=["甲"], snow_line=1, method=METHOD_BAN,
        permit_ids=["ST-暂存"], no_dump_order_id="ND-禁倒甲",
        trip_ids=[t1], title="甲段清晨腾街单（禁倒改道分支）",
    )
    return {"office": office, "order": rec, "trip_ids": trip_ids,
            "ban_error": ban_error}


def build_route_case(*, point_removed: bool = False, late: bool = False) -> dict[str, Any]:
    """同一笔甲段承诺，靠岔口前观测和可达装车点执行。"""

    wb, trip_ids = exact_unload_warehouse({"T1": 6, "T2": 1})
    office = _three_segment_office(wb)
    t1 = trip_ids["T1"]
    t2_alias = "T2"
    t2 = trip_ids[t2_alias]
    office.set_initial_snow("甲", 5)
    office.set_initial_snow("乙", 5)
    office.set_initial_snow("丙", 0)
    office.reference_confirmed_trip(t1)
    office.add_pickup("装-甲", t1, "甲", lo=0, hi=6)
    office.record_loading_witness("见证-乙", t1, "乙", 1)

    office.create_storage_permit(
        "ST-暂存", 1, holder="邻队", possible_segments=["甲", "丙"],
        title="邻队一公斤暂存单",
    )
    office.add_loading_point("LP-甲", "甲", arrival_time=4, closure_time=5)
    if point_removed:
        office.set_point_reachable("LP-甲", False)
    office.record_observation(
        "OBS-岔口", "甲", timing="AFTER_FORK" if late else BEFORE_FORK,
        visible=not late,
    )
    rec = office.submit_release_order(
        "SR-岔口", segments=["甲"], snow_line=1, method=METHOD_OBSERVE,
        permit_ids=["ST-暂存"], capacity=1, observation_id="OBS-岔口",
        trip_ids=[t1], final_trip_id=t2, title="甲段清晨腾街单（现场观测分支）",
    )
    return {"office": office, "order": rec, "trip_ids": trip_ids}


# ---------------------------------------------------------------------------
# 案例二：甲乙同时无雪、丙位、先租凭据与观测迟到
# ---------------------------------------------------------------------------

def build_fork_market_case(
    *,
    c_rented: bool = False,
    late_observation: bool = False,
    sign_ban: bool = False,
) -> dict[str, Any]:
    """构造甲乙同时无雪订单。

    单点反事实：

    * ``c_rented=True``：只增加丙位已租凭据；禁倒单不能签，但观测方案仍可执行；
    * ``late_observation=True``：只把观测推迟到过岔口；观测方案不能保证整单。
    """

    wb, trip_ids = exact_unload_warehouse({"T2": 1})
    office = _three_segment_office(wb)
    t2 = trip_ids["T2"]
    for sid in ("甲", "乙", "丙"):
        office.set_initial_snow(sid, 0)
    office.add_slot("丙位-1kg", "丙", 1)
    if c_rented:
        office.prior_rent_slot(
            "丙位-1kg", "别人的先租凭据", 1, lease_id="LSE-别人",
            receipt_id="RCV-先租",
        )

    office.create_storage_permit(
        "ST-邻队", 1, holder="邻队", possible_segments=["甲", "乙"],
        title="邻队已签的一公斤暂存单",
    )
    office.add_loading_point("LP-甲", "甲", arrival_time=3, closure_time=5)
    office.add_loading_point("LP-乙", "乙", arrival_time=4, closure_time=5)

    ban_error: Exception | None = None
    ban_order_id: str | None = None
    if sign_ban and not c_rented:
        ban_order_id = "ND-前夜"
        office.sign_no_dump_order(
            ban_order_id, permit_id="ST-邻队",
            protected_segments=["甲", "乙"], destination_segment="丙",
            slot_id="丙位-1kg",
        )
        office.sign_delivered_slot(ban_order_id)
    elif sign_ban and c_rented:
        try:
            office.sign_no_dump_order(
                "ND-前夜", permit_id="ST-邻队",
                protected_segments=["甲", "乙"], destination_segment="丙",
                slot_id="丙位-1kg",
            )
        except Exception as exc:
            ban_error = exc

    obs_id = "OBS-落堆"
    office.record_observation(
        obs_id, None, timing="AFTER_FORK" if late_observation else BEFORE_FORK,
        visible=not late_observation,
    )
    rec = office.submit_release_order(
        "SR-早市", stalls=["摊-甲", "摊-乙"], snow_line=1,
        method="auto", permit_ids=["ST-邻队"], capacity=1,
        observation_id=obs_id, forecast_segment="乙",
        no_dump_order_id=ban_order_id, final_trip_id=t2,
        title="甲乙两段同时无雪早市单",
    )
    return {"office": office, "order": rec, "trip_ids": trip_ids,
            "ban_error": ban_error, "ban_order_id": ban_order_id}


def replay_fork_worlds(case: dict[str, Any]) -> list[dict[str, Any]]:
    """重放甲、乙两种落堆实况（观测及时，末车按所见跟进）。"""

    office: StreetOffice = case["office"]
    return [
        office.replay_morning(
            "SR-早市", permit_id="ST-邻队", landing_segment="甲",
            observed=True, forecast_segment="乙",
            load_trip_id=case["trip_ids"]["T2"],
        ),
        office.replay_morning(
            "SR-早市", permit_id="ST-邻队", landing_segment="乙",
            observed=True, forecast_segment="乙",
            load_trip_id=case["trip_ids"]["T2"],
        ),
    ]
