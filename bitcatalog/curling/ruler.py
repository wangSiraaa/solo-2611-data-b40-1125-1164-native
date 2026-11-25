"""桌上冰壶俯拍量尺目录（bitcatalog 上的小目录）。

把一台俯拍量尺相机的每一次"曝光"拆成若干条位域记录，复用主仓的
:class:`~bitcatalog.catalog.CatalogProduct` /
:class:`~bitcatalog.warehouse.ObservationWarehouse`：

* 同一物理帧号（``frame_id``，即实际帧号）下可以有多条记录，靠帧事件号
  ``frame_event_id`` 区分；记录类型由 selector 分支决定。
* 三个"活动分支"对应三类量尺读数：

  ===============  =========  ============================
  分支 selector    记录类型   分支内才活动的字段
  ===============  =========  ============================
  1                定位钉     ``pin_id``
  2                壶边标记   ``stone_id``
  3                中心钉     （仅共用的横纵坐标）
  ===============  =========  ============================

  横纵坐标 ``local_x`` / ``local_y`` 是三个分支共用的普通信号；``pin_id``
  与 ``stone_id`` 分支集合不相交，按目录合同可以复用同一批物理位。
* 目录经 :func:`build_product` 编译后得到内容指纹；原仓只认指纹 + 实际帧号
  读回，目录定义被改动即换指纹，旧帧仍按旧产品长期复查。

坐标全部走有符号定点量（``factor = 1/1000``），即帧内坐标的千分之一格，
配合 :class:`fractions.Fraction` 做精确比较，不引入二进制浮点。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from ..catalog import CatalogProduct, build_product, fraction_text

BUS = "overhead-ruler"

SELECTOR_PIN = 1
SELECTOR_STONE = 2
SELECTOR_CENTER_NAIL = 3

# 位布局（5 字节 / 40 位小端）：
#   selector          bits  0..2   width 3
#   local_x (signed)  bits  3..18  width 16
#   local_y (signed)  bits 19..34  width 16
#   pin_id / stone_id bits 35..38  width 4（互斥分支复用同四位）
PAYLOAD_BYTES = 5
SCALE = Fraction(1, 1000)

_RULER_MESSAGES: tuple[dict[str, Any], ...] = (
    {
        "bus": BUS,
        "frame_id": 0,  # frame_id 由每次曝光的实际帧号承载，定义处仅占位，建产品时按帧号复制
        "payload_bytes": PAYLOAD_BYTES,
        "selector": {"name": "mark_kind", "start": 0, "width": 3, "order": "little"},
        "signals": [
            {
                "name": "local_x",
                "start": 3,
                "width": 16,
                "order": "little",
                "signed": True,
                "factor": fraction_text(SCALE),
                "offset": "0",
            },
            {
                "name": "local_y",
                "start": 19,
                "width": 16,
                "order": "little",
                "signed": True,
                "factor": fraction_text(SCALE),
                "offset": "0",
            },
            {
                "name": "pin_id",
                "start": 35,
                "width": 4,
                "order": "little",
                "signed": False,
                "branches": [SELECTOR_PIN],
            },
            {
                "name": "stone_id",
                "start": 35,
                "width": 4,
                "order": "little",
                "signed": False,
                "branches": [SELECTOR_STONE],
            },
        ],
    },
)


def ruler_product(*, frame_ids: int | tuple[int, ...] = (0,)) -> CatalogProduct:
    """编译量尺目录产品。

    目录按实际帧号分别建消息（同一 bus、不同 ``frame_id``），保证原仓能
    "按实际帧号读回"；不传帧号时给出占位的 0 号消息，仅用于查看布局。
    """

    if isinstance(frame_ids, int):
        frame_ids = (frame_ids,)
    messages = []
    for frame_id in frame_ids:
        message = {**_RULER_MESSAGES[0], "frame_id": int(frame_id)}
        messages.append(message)
    return build_product(messages)


def _raw(value: Fraction | int | float, *, field: str) -> int:
    if not isinstance(value, Fraction):
        value = Fraction(value)
    physical = value / SCALE  # raw * SCALE = value
    if physical.denominator != 1:
        raise ValueError(f"{field}={value} 在千分之一格定点量下不可表示")
    raw = physical.numerator
    if not -(1 << 15) <= raw <= (1 << 15) - 1:
        raise ValueError(f"{field} 的定点 raw {raw} 超出 16 位有符号范围")
    return raw


@dataclass(frozen=True)
class RulerRecord:
    """一条量尺读数记录（已还原为物理坐标，单位：格）。"""

    frame_event_id: str
    frame_id: int
    kind: int
    x: Fraction
    y: Fraction
    mark_id: int | None = None

    @property
    def kind_name(self) -> str:
        return {
            SELECTOR_PIN: "pin",
            SELECTOR_STONE: "stone",
            SELECTOR_CENTER_NAIL: "center_nail",
        }.get(self.kind, f"kind{self.kind}")

    def as_json(self) -> dict[str, Any]:
        return {
            "frame_event_id": self.frame_event_id,
            "frame_id": self.frame_id,
            "kind": self.kind,
            "kind_name": self.kind_name,
            "x": fraction_text(self.x),
            "y": fraction_text(self.y),
            "mark_id": self.mark_id,
        }


def encode_record(
    product: CatalogProduct,
    frame_id: int,
    kind: int,
    x: Fraction | int,
    y: Fraction | int,
    *,
    mark_id: int | None = None,
) -> tuple[str, bytes]:
    """把一条读数编码为真实 payload；返回 ``(frame_event_id, payload)``。

    ``frame_event_id`` 由帧号、分支与读数身份确定性生成，同一物理帧号下
    多条记录不会在原仓里互相覆盖。
    """

    message = product.message(BUS, frame_id)
    raws: dict[str, int] = {
        "local_x": _raw(Fraction(x), field="local_x"),
        "local_y": _raw(Fraction(y), field="local_y"),
    }
    if kind == SELECTOR_PIN:
        if mark_id is None:
            raise ValueError("定位钉记录需要 pin_id")
        raws["pin_id"] = mark_id
    elif kind == SELECTOR_STONE:
        if mark_id is None:
            raise ValueError("壶边标记记录需要 stone_id")
        raws["stone_id"] = mark_id
    elif kind == SELECTOR_CENTER_NAIL:
        pass
    else:
        raise ValueError(f"未知量尺分支 selector={kind}")
    encoded = message.encode(raws, selector=kind)
    identity = mark_id if mark_id is not None else "nail"
    frame_event_id = f"f{frame_id:06d}-k{kind}-{identity}"
    return frame_event_id, encoded.payload


def decode_frame(product: CatalogProduct, frame_id: int, payload: bytes) -> RulerRecord:
    """按产品指纹对应的目录，把一条原帧 payload 读回成量尺记录。"""

    decoded = product.message(BUS, frame_id).decode(payload)
    by_name = decoded.by_name()
    mark_id: int | None = None
    if decoded.selector == SELECTOR_PIN:
        mark_id = by_name["pin_id"].raw
    elif decoded.selector == SELECTOR_STONE:
        mark_id = by_name["stone_id"].raw
    return RulerRecord(
        frame_event_id="",
        frame_id=frame_id,
        kind=decoded.selector,  # type: ignore[arg-type]
        x=by_name["local_x"].value,
        y=by_name["local_y"].value,
        mark_id=mark_id,
    )


def readback_frame(
    warehouse: Any,
    product: CatalogProduct,
    frame_id: int,
) -> tuple[RulerRecord, ...]:
    """用原仓入帧，按产品指纹和实际帧号读回该物理帧的全部读数记录。

    只取 ``status='ok'`` 的帧；被整帧拒绝的 payload 不会出现在这里。读回
    走产品自身的编译提取路径，目录指纹不一致则产品不同、互不可见。
    """

    conn = warehouse.connection
    product_id = warehouse._product_id(product)
    rows = conn.execute(
        "SELECT frame_event_id, payload FROM frames"
        " WHERE product_id = ? AND frame_id = ? AND status = 'ok'"
        " ORDER BY frame_event_id",
        (product_id, frame_id),
    ).fetchall()
    out: list[RulerRecord] = []
    for row in rows:
        record = decode_frame(product, frame_id, row["payload"])
        object.__setattr__(record, "frame_event_id", row["frame_event_id"])
        out.append(record)
    return tuple(out)
