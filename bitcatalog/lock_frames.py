"""内河展厅缩尺船闸的帧目录与门位控制校验。

在 bitcatalog 的位域目录协议上开一条 ``inland-lock`` 总线，四个小目录消息：

| frame_id | 消息 | 说明 |
| --- | --- | --- |
| 10 | ``gate_cmd`` | 闸门命令：目标门位 + 两侧尺读数（开门时必须平水） |
| 11 | ``valve_cmd`` | 阀门命令：阀向（进水/泄水/关）+ 脉冲格数 |
| 12 | ``level_gauge`` | 水位尺遥测：容器号 + 尺读数 |
| 13 | ``ship_phase`` | 放船阶段：船号、阶段、过闸拍 |

门位控制的活动字段
==================

``gate_cmd`` 用 selector 区分命令分支：``0=关``、``1=开``。开门分支独占一组
物理位携带 ``left_level`` / ``right_level``，关门分支这两个信号不活动（不
占观测行）。目录的同时活动/位重叠证明保证互斥分支可复用位段；此外
:func:`validate_lock_frame` 在语义层再校验三件事：

1. ``gate_cmd`` 开门分支两侧尺读数必须相等，否则 ``gate_level_mismatch``；
2. ``valve_cmd`` 的关阀分支（selector=0）脉冲格数必须为 0，任何穿过关闭阀
   的转移以 ``valve_closed_transfer`` 整帧拒绝；
3. ``level_gauge`` 读数不得越过容器容量，越界以 ``level_out_of_range`` 拒绝。

校验通过的真实帧按普通 bitcatalog 事件入观测仓，可长期复查、按窗口聚合。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .catalog import CatalogProduct, DecodeError, build_product
from .lock import INFINITE_CAPACITY, LockError, LockTopology
from .warehouse import FrameEvent, ObservationWarehouse

BUS = "inland-lock"

FRAME_GATE_CMD = 10
FRAME_VALVE_CMD = 11
FRAME_LEVEL_GAUGE = 12
FRAME_SHIP_PHASE = 13

# selector 取值（gate_cmd / valve_cmd 共用语义：0 关，1 开/动作）
SELECTOR_CLOSED = 0
SELECTOR_OPEN = 1
SELECTOR_VALVE_CLOSE = 0
SELECTOR_VALVE_ACTIVE = 1

# 容器编号（level_gauge.pool_id）
POOL_IDS = {
    "chamber": 1,
    "upstream": 2,
    "downstream": 3,
    "side": 4,
    "makeup_source": 5,
    "drain_sink": 6,
}
POOL_NAMES = {value: key for key, value in POOL_IDS.items()}

# 阶段编号（ship_phase.phase）
PHASE_IDS = {
    "wait_upstream": 1,
    "wait_downstream": 2,
    "in_chamber": 3,
    "passed": 4,
}


# ---------------------------------------------------------------------------
# 目录定义（小目录，全部 8..16 位字段，人工可核对）
# ---------------------------------------------------------------------------


def lock_catalog_definitions() -> list[dict[str, Any]]:
    return [
        # --- frame 10: 闸门命令 ----------------------------------------
        {
            "bus": BUS,
            "frame_id": FRAME_GATE_CMD,
            "payload_bytes": 4,
            "selector": {"name": "gate_cmd_sel", "start": 0, "width": 1, "order": "little"},
            "signals": [
                # 两个分支都活动：闸门编号与目标门位
                {"name": "gate_id", "start": 1, "width": 2, "order": "little",
                 "factor": "1", "offset": "0"},
                {"name": "commanded_open", "start": 3, "width": 1, "order": "little",
                 "factor": "1", "offset": "0"},
                # 仅开门分支活动：开门瞬间两侧水位尺（平水证据）
                {"name": "left_level", "start": 8, "width": 8, "order": "little",
                 "factor": "1", "offset": "0", "branches": [SELECTOR_OPEN]},
                {"name": "right_level", "start": 16, "width": 8, "order": "little",
                 "factor": "1", "offset": "0", "branches": [SELECTOR_OPEN]},
                # 仅关门分支活动：关门原因码（0=正常，1=闭闸，2=超时）
                {"name": "close_reason", "start": 8, "width": 8, "order": "little",
                 "factor": "1", "offset": "0", "branches": [SELECTOR_CLOSED]},
            ],
        },
        # --- frame 11: 阀门命令 ----------------------------------------
        {
            "bus": BUS,
            "frame_id": FRAME_VALVE_CMD,
            "payload_bytes": 4,
            "selector": {"name": "valve_cmd_sel", "start": 0, "width": 1, "order": "little"},
            "signals": [
                {"name": "valve_id", "start": 1, "width": 3, "order": "little",
                 "factor": "1", "offset": "0"},
                # 阀向：1=正向(a->b)，2=反向(b->a)；仅动作分支活动
                {"name": "valve_direction", "start": 4, "width": 2, "order": "little",
                 "factor": "1", "offset": "0", "branches": [SELECTOR_VALVE_ACTIVE]},
                # 本拍脉冲格数；关阀分支必须为 0（物理上不存在该信号）
                {"name": "pulse_amount", "start": 8, "width": 8, "order": "little",
                 "factor": "1", "offset": "0", "branches": [SELECTOR_VALVE_ACTIVE]},
                # 关阀分支复用同一段位记录关阀原因，与 pulse_amount 互斥
                {"name": "valve_close_reason", "start": 8, "width": 8, "order": "little",
                 "factor": "1", "offset": "0", "branches": [SELECTOR_VALVE_CLOSE]},
            ],
        },
        # --- frame 12: 水位尺遥测（无分支） ----------------------------
        {
            "bus": BUS,
            "frame_id": FRAME_LEVEL_GAUGE,
            "payload_bytes": 3,
            "signals": [
                {"name": "pool_id", "start": 0, "width": 4, "order": "little",
                 "factor": "1", "offset": "0"},
                {"name": "gauge_level", "start": 4, "width": 8, "order": "little",
                 "factor": "1", "offset": "0"},
                {"name": "gauge_valid", "start": 12, "width": 1, "order": "little",
                 "factor": "1", "offset": "0"},
            ],
        },
        # --- frame 13: 放船阶段（无分支） ------------------------------
        {
            "bus": BUS,
            "frame_id": FRAME_SHIP_PHASE,
            "payload_bytes": 4,
            "signals": [
                {"name": "ship_id", "start": 0, "width": 4, "order": "little",
                 "factor": "1", "offset": "0"},
                {"name": "phase", "start": 4, "width": 3, "order": "little",
                 "factor": "1", "offset": "0"},
                {"name": "beat", "start": 8, "width": 12, "order": "little",
                 "factor": "1", "offset": "0"},
                {"name": "pass_beat", "start": 20, "width": 12, "order": "little",
                 "factor": "1", "offset": "0"},
            ],
        },
    ]


def build_lock_product() -> CatalogProduct:
    """发布船闸帧目录产品（内容寻址，与主目录产品互不干扰）。"""

    return build_product(lock_catalog_definitions())


# ---------------------------------------------------------------------------
# 语义校验：门位控制的活动字段
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameValidation:
    accepted: bool
    code: str | None = None
    message: str | None = None
    detail: Mapping[str, Any] = frozenset()

    def as_json(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "code": self.code,
            "message": self.message,
            "detail": dict(self.detail),
        }


def validate_lock_frame(
    product: CatalogProduct,
    payload: bytes,
    *,
    frame_id: int,
    topology: LockTopology | None = None,
) -> FrameValidation:
    """对一帧真实字节做目录解码 + 船闸语义校验。

    目录层错误（长度/未知消息/编码）由 bitcatalog 自身在入库时整帧拒绝；
    本函数只负责通过解码后的**活动字段**检查门位控制规则。
    """

    try:
        decoded = product.message(BUS, frame_id).decode(payload)
    except DecodeError as exc:
        return FrameValidation(False, exc.code, str(exc), exc.detail)

    values = {signal.name: signal.raw for signal in decoded.signals}

    if frame_id == FRAME_GATE_CMD:
        if decoded.selector == SELECTOR_OPEN:
            left, right = values["left_level"], values["right_level"]
            if left != right:
                return FrameValidation(
                    False,
                    "gate_level_mismatch",
                    f"开门帧两侧尺读数不平：left={left}，right={right}",
                    {"left_level": left, "right_level": right, "gate_id": values["gate_id"]},
                )
        else:
            # 关门分支：left_level/right_level 根本不活动，验证它们确实没有观测行
            if "left_level" in values or "right_level" in values:
                return FrameValidation(
                    False, "inactive_field_present", "关门分支不得携带平水尺字段",
                    {"active": sorted(values)},
                )
        return FrameValidation(True)

    if frame_id == FRAME_VALVE_CMD:
        if decoded.selector == SELECTOR_VALVE_CLOSE:
            # 关阀分支脉冲信号不活动；若目录误把它解出来即属目录违约
            if "pulse_amount" in values:
                return FrameValidation(
                    False, "valve_closed_transfer",
                    "关阀帧出现脉冲格数字段：拒绝穿过关闭阀的转移",
                    {"valve_id": values["valve_id"]},
                )
            return FrameValidation(True)
        amount = values["pulse_amount"]
        if amount <= 0:
            return FrameValidation(
                False, "bad_pulse", "动作阀脉冲格数必须 >= 1",
                {"pulse_amount": amount, "valve_id": values["valve_id"]},
            )
        if values["valve_direction"] not in (1, 2):
            return FrameValidation(
                False, "bad_valve_direction", "阀向必须是 1(正向) 或 2(反向)",
                {"valve_direction": values["valve_direction"]},
            )
        return FrameValidation(True)

    if frame_id == FRAME_LEVEL_GAUGE:
        if not values["gauge_valid"]:
            return FrameValidation(False, "gauge_invalid", "水位尺帧标记为无效",
                                   {"pool_id": values["pool_id"]})
        pool_id = values["pool_id"]
        pool_name = POOL_NAMES.get(pool_id)
        if pool_name is None:
            return FrameValidation(False, "unknown_pool", f"未知容器编号 {pool_id}",
                                   {"pool_id": pool_id})
        if topology is not None and pool_name in topology.capacities:
            cap = topology.capacity(pool_name)
            if cap != INFINITE_CAPACITY and values["gauge_level"] > cap:
                return FrameValidation(
                    False, "level_out_of_range",
                    f"容器 {pool_name} 尺读数 {values['gauge_level']} 超出容量 {cap}",
                    {"pool": pool_name, "level": values["gauge_level"], "capacity": cap},
                )
        return FrameValidation(True)

    if frame_id == FRAME_SHIP_PHASE:
        if values["phase"] not in PHASE_IDS.values():
            return FrameValidation(False, "unknown_phase", f"未知阶段码 {values['phase']}",
                                   {"phase": values["phase"]})
        return FrameValidation(True)

    return FrameValidation(False, "unknown_frame", f"船闸目录未定义 frame_id={frame_id}",
                           {"frame_id": frame_id})


# ---------------------------------------------------------------------------
# 入库桥接：校验通过的帧才作为事件进入观测仓
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestionResult:
    accepted: int
    rejected_semantic: tuple[dict[str, Any], ...]
    frame_event_ids: tuple[str, ...]


def encode_gate_frame(
    product: CatalogProduct,
    *,
    gate_id: int,
    open: bool,
    left_level: int | None = None,
    right_level: int | None = None,
    close_reason: int = 0,
) -> bytes:
    """构造一帧闸门命令真实字节（写后由编译路径自检）。"""

    selector = SELECTOR_OPEN if open else SELECTOR_CLOSED
    raws: dict[str, int] = {"gate_id": gate_id, "commanded_open": 1 if open else 0}
    if open:
        if left_level is None or right_level is None:
            raise LockError("开门帧必须携带两侧尺读数", code="missing_gauge_level")
        raws.update(left_level=left_level, right_level=right_level)
    else:
        raws["close_reason"] = close_reason
    return product.message(BUS, FRAME_GATE_CMD).encode(raws, selector=selector).payload


def encode_valve_frame(
    product: CatalogProduct,
    *,
    valve_id: int,
    direction: int | None,
    pulse_amount: int | None = None,
    close_reason: int = 0,
) -> bytes:
    """构造一帧阀门命令真实字节；``direction=None`` 表示关阀。"""

    if direction is None:
        raws = {"valve_id": valve_id, "valve_close_reason": close_reason}
        return product.message(BUS, FRAME_VALVE_CMD).encode(raws, selector=SELECTOR_VALVE_CLOSE).payload
    if pulse_amount is None:
        raise LockError("动作阀帧必须给出脉冲格数", code="missing_pulse")
    raws = {"valve_id": valve_id, "valve_direction": direction, "pulse_amount": pulse_amount}
    return product.message(BUS, FRAME_VALVE_CMD).encode(raws, selector=SELECTOR_VALVE_ACTIVE).payload


def encode_gauge_frame(product: CatalogProduct, *, pool: str, level: int, valid: bool = True) -> bytes:
    if pool not in POOL_IDS:
        raise LockError(f"未知容器 {pool!r}", code="unknown_pool", pool=pool)
    raws = {"pool_id": POOL_IDS[pool], "gauge_level": level, "gauge_valid": 1 if valid else 0}
    return product.message(BUS, FRAME_LEVEL_GAUGE).encode(raws).payload


def encode_ship_frame(
    product: CatalogProduct, *, ship_id: int, phase: str, beat: int, pass_beat: int = 0
) -> bytes:
    if phase not in PHASE_IDS:
        raise LockError(f"未知阶段 {phase!r}", code="unknown_phase", phase=phase)
    raws = {"ship_id": ship_id, "phase": PHASE_IDS[phase], "beat": beat, "pass_beat": pass_beat}
    return product.message(BUS, FRAME_SHIP_PHASE).encode(raws).payload


def ingest_lock_frames(
    warehouse: ObservationWarehouse,
    product: CatalogProduct,
    events: Sequence[FrameEvent | Mapping[str, Any]],
    *,
    topology: LockTopology | None = None,
) -> IngestionResult:
    """先做船闸语义校验，再把合格帧交给观测仓（不合格帧不入库）。

    观测仓自身仍会再做一次目录层整帧校验，两层拒绝互相独立、可复查。
    """

    accepted_events: list[FrameEvent | Mapping[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    ids: list[str] = []
    for raw_event in events:
        event = FrameEvent.coerce(raw_event)
        payload = bytes.fromhex(event.payload_hex) if isinstance(event.payload_hex, str) else bytes(event.payload_hex)
        result = validate_lock_frame(product, payload, frame_id=event.frame_id, topology=topology)
        if not result.accepted:
            rejected.append(
                {
                    "frame_event_id": event.frame_event_id,
                    "frame_id": event.frame_id,
                    "code": result.code,
                    "message": result.message,
                    "detail": dict(result.detail),
                }
            )
            continue
        accepted_events.append(raw_event)
        ids.append(event.frame_event_id)
    warehouse.ingest(product, accepted_events)
    return IngestionResult(
        accepted=len(accepted_events),
        rejected_semantic=tuple(rejected),
        frame_event_ids=tuple(ids),
    )
