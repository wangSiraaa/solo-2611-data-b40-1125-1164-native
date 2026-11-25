"""题面全部示例的可复跑场景：原仓入帧、量测还原、计分与裁判时间线。

数据全部为整数格（壶边修正向量固定，校正后壶心落在整数坐标），距离开方
在 :mod:`bitcatalog.curling.geometry` 内走完全平方校验，全程无浮点。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Sequence

from ..warehouse import FrameEvent, ObservationWarehouse
from .geometry import (
    ExposureObs,
    ExposureSolution,
    Point,
    solve_exposure,
)
from .referee import (
    EvidenceFrame,
    MAX_REPLAY_STONES,
    RELEASE,
    SEAL,
)
from .ruler import (
    BUS,
    SELECTOR_CENTER_NAIL,
    SELECTOR_PIN,
    SELECTOR_STONE,
    RulerRecord,
    encode_record,
    readback_frame,
    ruler_product,
)
from .scoring import BLUE, RED, ScoreDesk, naive_per_stone_minimum, score_range

# ---------------------------------------------------------------------------
# 场地台账（世界坐标，中心钉为原点）
# ---------------------------------------------------------------------------

CENTER = Point.of(0, 0)
PIN_0 = 0  # 中心钉同时登记为 0 号定位钉
PIN_1 = 1
WORLD_PINS: dict[int, Point] = {PIN_0: CENTER, PIN_1: Point.of(4, 0)}

# 壶边标记固定在壶心负 y 方向一格，修正向量为 (0, +1)，每次曝光一致。
EDGE_CORRECTION = Point.of(0, 1)

# 实际帧号
FRAME_BASIC = 101
FRAME_A = 201   # 歧义局：红一、蓝一同帧，双钉唯一校正
FRAME_B = 202   # 歧义局：红二 + 仅有的共同定位钉 P1，单钉双假设
FRAME_C = 303   # 下一班补拍：红二、蓝一、中心钉同幅
FRAME_TIE = 404

# 本场景占用的全部实际帧号
ALL_FRAMES = (FRAME_BASIC, FRAME_A, FRAME_B, FRAME_C, FRAME_TIE)

# 歧义局两个相容的方向假设（平移都由 P1 反解，尺度均为 1）
H0_NOTE = "H0 方向未转"
H1_NOTE = "H1 方向转90度"
HYPOTHESES = (
    (Fraction(1), Fraction(0), Fraction(1), H0_NOTE),
    (Fraction(0), Fraction(1), Fraction(1), H1_NOTE),
)


@dataclass
class IngestedFrame:
    frame_id: int
    records: tuple[RulerRecord, ...]
    event_ids: tuple[str, ...]


# 壶号 -> 4 位壶编码：红 1..6 直用编号，蓝 1..6 用 11..16（六壶上限内）
def _stone_code(stone_id: str) -> int:
    if stone_id.startswith("red"):
        return int(stone_id[3:])
    if stone_id.startswith("blue"):
        return 10 + int(stone_id[4:])
    raise ValueError(f"无法识别壶号 {stone_id}")


def _stone_id(code: int) -> str:
    if code >= 11:
        return f"blue{code - 10}"
    return f"red{code}"


class RulerWarehouse:
    """把量尺记录编进真实 payload、入原仓，并按指纹+实际帧号读回核对。"""

    def __init__(self, frame_ids: Sequence[int], db_path: str = ":memory:"):
        self.product = ruler_product(frame_ids=tuple(sorted(set(frame_ids))))
        self.warehouse = ObservationWarehouse(db_path)
        self.warehouse.register_product(self.product)
        self._clock = 1_000_000_000
        self.frames: dict[int, IngestedFrame] = {}

    @property
    def fingerprint(self) -> str:
        return self.product.fingerprint

    def ingest(
        self,
        frame_id: int,
        *,
        pins: Mapping[int, Point] = (),
        stones: Mapping[str, Point] = (),
        center_nail: Point | None = None,
    ) -> IngestedFrame:
        events: list[FrameEvent] = []
        records: list[RulerRecord] = []
        event_ids: list[str] = []
        pins = dict(pins)
        stones = dict(stones)

        def put(kind: int, p: Point, mark_id: int | None) -> None:
            event_id, payload = encode_record(
                self.product, frame_id, kind, p.x, p.y, mark_id=mark_id
            )
            self._clock += 1
            events.append(
                FrameEvent(
                    frame_event_id=event_id,
                    bus=BUS,
                    frame_id=frame_id,
                    occurred_at=self._clock,
                    payload_hex=payload.hex(),
                )
            )
            event_ids.append(event_id)

        for pin_id, p in sorted(pins.items()):
            put(SELECTOR_PIN, p, pin_id)
        for stone_id, edge in sorted(stones.items()):
            put(SELECTOR_STONE, edge, _stone_code(stone_id))
        if center_nail is not None:
            put(SELECTOR_CENTER_NAIL, center_nail, None)

        stats = self.warehouse.ingest(self.product, events)
        if stats.rejected:
            raise AssertionError(f"帧 {frame_id} 出现整帧拒绝：{stats.rejections}")
        # 按产品指纹 + 实际帧号读回，坐标必须与入帧时逐位一致。
        readback = readback_frame(self.warehouse, self.product, frame_id)
        if len(readback) != len(events):
            raise AssertionError(f"帧 {frame_id} 读回条数 {len(readback)} != 入帧 {len(events)}")
        for rec in readback:
            records.append(rec)
        ingested = IngestedFrame(frame_id, tuple(records), tuple(event_ids))
        self.frames[frame_id] = ingested
        return ingested

    def evidence(self, frame_id: int, visible: Sequence[str], *, live: bool = True) -> EvidenceFrame:
        ingested = self.frames[frame_id]
        return EvidenceFrame(
            frame_event_id=ingested.event_ids[0],
            frame_id=frame_id,
            product_fingerprint=self.fingerprint,
            visible=tuple(visible),
            sealed=True,
            live_stones=live,
        )

    def close(self) -> None:
        self.warehouse.close()


def _exposure(frame_id: int, ingested: IngestedFrame) -> ExposureObs:
    pins: dict[int, Point] = {}
    edges: dict[str, Point] = {}
    nail: Point | None = None
    for rec in ingested.records:
        p = Point(rec.x, rec.y)
        if rec.kind == SELECTOR_PIN:
            assert rec.mark_id is not None
            pins[rec.mark_id] = p
        elif rec.kind == SELECTOR_STONE:
            assert rec.mark_id is not None
            edges[_stone_id(rec.mark_id)] = p
        elif rec.kind == SELECTOR_CENTER_NAIL:
            nail = p
    return ExposureObs(frame_id, pins, edges, nail, EDGE_CORRECTION)


# ---------------------------------------------------------------------------
# 示例一：一条线上的三档比分（红 1、3 格；蓝 4 格；圈半径 5）
# ---------------------------------------------------------------------------

CIRCLE_LINE = Fraction(5)


def _line_frame(rw: RulerWarehouse, frame_id: int, red_ds: Sequence[int], blue_ds: Sequence[int]) -> ExposureObs:
    stones: dict[str, Point] = {}
    pins = {PIN_0: CENTER, PIN_1: WORLD_PINS[PIN_1]}
    for i, d in enumerate(red_ds, start=1):
        stones[f"red{i}"] = Point.of(-d, -1)  # 边标记在壶心负 y 一格
    for i, d in enumerate(blue_ds, start=1):
        stones[f"blue{i}"] = Point.of(d, -1)
    ingested = rw.ingest(frame_id, pins=pins, stones=stones, center_nail=CENTER)
    return _exposure(frame_id, ingested)


def line_example(rw: RulerWarehouse, *, blue_at: int, removed: Sequence[str] = ()) -> ExposureSolution:
    """红壶固定 1、3 格；蓝壶距心 ``blue_at`` 格；撤除由计分台另管。"""

    exposure = _line_frame(rw, FRAME_BASIC, red_ds=(1, 3), blue_ds=(blue_at,))
    return solve_exposure(exposure, WORLD_PINS)


# ---------------------------------------------------------------------------
# 示例二：换坐标那局——单钉双相容校正，比分可能 1 或 2
# ---------------------------------------------------------------------------

# 世界盘事实（补拍后取红二较近的实盘，即 H0 为真）：
#   红一 (-1,0) d=1；蓝一 (6,0) d=6；红二 (4,-3) d=5；圈半径 8
CIRCLE_AMBIGUOUS = Fraction(8)
WORLD_RED1 = Point.of(-1, 0)
WORLD_BLUE1 = Point.of(6, 0)
WORLD_RED2_TRUTH = Point.of(4, -3)  # d=5，严格近于蓝一 d=6


def build_ambiguous_end(rw: RulerWarehouse) -> tuple[ExposureSolution, ExposureSolution, list[EvidenceFrame]]:
    # 曝光 A：红一、蓝一同帧；中心钉 + P1 双钉 -> 唯一恒等校正。
    a = rw.ingest(
        FRAME_A,
        pins={PIN_0: CENTER, PIN_1: WORLD_PINS[PIN_1]},
        stones={"red1": WORLD_RED1 - EDGE_CORRECTION, "blue1": WORLD_BLUE1 - EDGE_CORRECTION},
        center_nail=CENTER,
    )
    sol_a = solve_exposure(_exposure(FRAME_A, a), WORLD_PINS)

    # 曝光 B：红二与 P1 同帧；P1 的帧坐标是两幅校正唯一共同可见、且都相容的记号。
    # 红二壶心帧坐标 (0,-3)（边标记再减修正向量），两套假设对它给出不同世界距离。
    frame_p1 = Point.of(0, 0)
    red2_center_frame = Point.of(0, -3)
    b = rw.ingest(
        FRAME_B,
        pins={PIN_1: frame_p1},
        stones={"red2": red2_center_frame - EDGE_CORRECTION},
    )
    sol_b = solve_exposure(_exposure(FRAME_B, b), WORLD_PINS, hypotheses=HYPOTHESES)

    evidence = [
        rw.evidence(FRAME_A, ("pin0", "pin1", "red1", "blue1", "center_nail")),
        rw.evidence(FRAME_B, ("pin1", "red2")),
    ]
    return sol_a, sol_b, evidence


def ambiguous_plates(rw: RulerWarehouse) -> tuple[list, ExposureSolution, ExposureSolution]:
    """合并两次曝光：A 唯一、B 双候选 -> 整盘候选数 = 2。"""

    sol_a, sol_b, _ = build_ambiguous_end(rw)
    plate_a = sol_a.plate()
    plates = []
    for plate_b in sol_b.candidates:
        centers = dict(plate_a.centers)
        centers.update(plate_b.centers)
        merged = type(plate_b)(
            exposure=plate_b.exposure,
            calibration=plate_b.calibration,
            centers=centers,
            center=plate_a.center,
        )
        plates.append(merged)
    return plates, sol_a, sol_b


def supplement_frame(rw: RulerWarehouse) -> tuple[EvidenceFrame, ExposureSolution]:
    """下一班唯一一次机会：红二、蓝一与中心钉拍在同幅，双钉收敛为唯一校正。"""

    ingested = rw.ingest(
        FRAME_C,
        pins={PIN_0: CENTER, PIN_1: WORLD_PINS[PIN_1]},
        stones={
            "red2": WORLD_RED2_TRUTH - EDGE_CORRECTION,
            "blue1": WORLD_BLUE1 - EDGE_CORRECTION,
        },
        center_nail=CENTER,
    )
    sol = solve_exposure(_exposure(FRAME_C, ingested), WORLD_PINS)
    evidence = rw.evidence(FRAME_C, ("pin0", "pin1", "red2", "blue1", "center_nail"))
    return evidence, sol


def resolved_plates_after_supplement(rw: RulerWarehouse):
    """补拍取实盘 H0（红二 d=5 较近）后，把歧义局收敛成的唯一整盘。"""

    plates, sol_a, _ = ambiguous_plates(rw)
    _, sol_c = supplement_frame(rw)
    plate_c = sol_c.plate()
    centers = dict(sol_a.plate().centers)
    centers.update(plate_c.centers)
    merged = type(plate_c)(
        exposure=plate_c.exposure,
        calibration=plate_c.calibration,
        centers=centers,
        center=sol_a.plate().center,
    )
    return [merged]


# ---------------------------------------------------------------------------
# 示例三：最近壶并列回放（每局不超过六壶）
# ---------------------------------------------------------------------------


def tie_end(rw: RulerWarehouse, *, stone_count: int):
    """红一、蓝一并列最近（同距 2）；``stone_count`` 控制是否触发六壶上限。"""

    if stone_count < 2 or stone_count > MAX_REPLAY_STONES + 1:
        raise ValueError("并列回放示例需要 2..7 只壶")
    stones: dict[str, Point] = {}
    red_n = (stone_count + 1) // 2
    blue_n = stone_count - red_n
    # 最近的两只同距 2，其余壶放到 3、4 格，仍在圈内。
    stones["red1"] = Point.of(-2, -1)
    stones["blue1"] = Point.of(2, -1)
    for i in range(2, red_n + 1):
        stones[f"red{i}"] = Point.of(-(2 + i), -1)
    for i in range(2, blue_n + 1):
        stones[f"blue{i}"] = Point.of(2 + i, -1)
    ingested = rw.ingest(
        FRAME_TIE,
        pins={PIN_0: CENTER, PIN_1: WORLD_PINS[PIN_1]},
        stones=stones,
        center_nail=CENTER,
    )
    sol = solve_exposure(_exposure(FRAME_TIE, ingested), WORLD_PINS)
    return sol, tuple(sorted(stones))


__all__ = [
    "CENTER",
    "WORLD_PINS",
    "EDGE_CORRECTION",
    "ALL_FRAMES",
    "FRAME_BASIC",
    "FRAME_A",
    "FRAME_B",
    "FRAME_C",
    "FRAME_TIE",
    "CIRCLE_LINE",
    "CIRCLE_AMBIGUOUS",
    "HYPOTHESES",
    "RulerWarehouse",
    "line_example",
    "build_ambiguous_end",
    "ambiguous_plates",
    "supplement_frame",
    "resolved_plates_after_supplement",
    "tie_end",
    "naive_per_stone_minimum",
    "score_range",
    "ScoreDesk",
    "SEAL",
    "RELEASE",
]
