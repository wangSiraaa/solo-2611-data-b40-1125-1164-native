"""量测端：定位钉 / 壶边标记 -> 整盘相容的壶心世界坐标。

核心纪律（题面合同）：

* 量尺两次曝光之间可能换了原点或方向，因此**每次曝光独立求一套校正**；
  同一次曝光里的所有壶共用这一套校正，不允许各壶各自挑一个最有利的
  校正，也不允许把不同曝光下"每壶各自最小距离"拼起来比较。
* 校正 = 带统一尺度的刚体变换 ``world = k·R·local + t``，参数全部用
  :class:`fractions.Fraction` 精确表示（构造时校验长度平方为有理数平方，
  杜绝浮点）。
* 两枚已知定位钉 -> 唯一校正；只有一枚共同定位钉时，方向/原点的剩余
  自由度体现为**两个相容校正假设**，由调用方（场景）给出候选，本模块
  负责用观测逐一验证、拒掉不相容者。
* 壶边标记先在帧坐标内按固定边缘向量修正到壶心，再随同一套校正变换到
  世界（桌面）坐标。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Mapping, NamedTuple, Sequence

# 桌面世界坐标的中心钉就是原点；定位钉的世界坐标由场地台账给出。


class Point(NamedTuple):
    x: Fraction
    y: Fraction

    @classmethod
    def of(cls, x: object, y: object) -> "Point":
        return cls(Fraction(x), Fraction(y))

    def __add__(self, other: "Point") -> "Point":  # type: ignore[override]
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: "Point") -> "Point":
        return Point(self.x - other.x, self.y - other.y)

    def __mul__(self, k: Fraction | int) -> "Point":
        k = Fraction(k)
        return Point(self.x * k, self.y * k)

    __rmul__ = __mul__

    def norm2(self) -> Fraction:
        return self.x * self.x + self.y * self.y

    def dist(self, other: "Point") -> Fraction:
        """精确距离；要求平方距离本身是有理数平方（场景数据都满足）。"""

        return exact_sqrt((self - other).norm2())


def exact_sqrt(value: Fraction) -> Fraction:
    """有理数的精确平方根，非完全平方直接拒绝（不退回浮点）。"""

    num_sqrt = math.isqrt(value.numerator)
    den_sqrt = math.isqrt(value.denominator)
    if num_sqrt * num_sqrt != value.numerator or den_sqrt * den_sqrt != value.denominator:
        raise ValueError(f"{value} 不是有理数的完全平方，无法在精确算术中开方")
    return Fraction(num_sqrt, den_sqrt)


WORLD_ORIGIN = Point(Fraction(0), Fraction(0))


class CalibrationError(ValueError):
    """观测与校正不相容。"""


@dataclass(frozen=True)
class Calibration:
    """一次曝光的一套校正：world = k·R(c,s)·local + t。"""

    exposure: int
    scale: Fraction
    cos: Fraction
    sin: Fraction
    tx: Fraction
    ty: Fraction
    basis: str  # 该校正的证据来源，供回执/申诉阅读
    pin_checks: tuple[tuple[int, Point, Point], ...] = field(default_factory=tuple)

    def apply(self, p: Point) -> Point:
        rx = self.cos * p.x - self.sin * p.y
        ry = self.sin * p.x + self.cos * p.y
        return Point(self.scale * rx + self.tx, self.scale * ry + self.ty)

    def describe(self) -> str:
        return (
            f"曝光{self.exposure} 校正<{self.basis}> "
            f"k={fraction(self.scale)} R(c={fraction(self.cos)}, s={fraction(self.sin)}) "
            f"t=({fraction(self.tx)},{fraction(self.ty)})"
        )


def fraction(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _rigid_from_two(
    p1: Point, p2: Point, q1: Point, q2: Point
) -> tuple[Fraction, Fraction, Fraction, Fraction, Fraction]:
    """由两组对应点求 (k, cos, sin, tx, ty)，使 q = k·R·p + t。"""

    d = p2 - p1
    e = q2 - q1
    ld = exact_sqrt(d.norm2())
    le = exact_sqrt(e.norm2())
    k = le / ld
    cos = (d.x * e.x + d.y * e.y) / (ld * le)
    sin = (d.x * e.y - d.y * e.x) / (ld * le)
    cal = Calibration(0, k, cos, sin, Fraction(0), Fraction(0), "tmp")
    t = q1 - Point(cal.apply(p1).x, cal.apply(p1).y)
    return k, cos, sin, t.x, t.y


def calibrate_from_pins(
    exposure: int,
    local_pins: Mapping[int, Point],
    world_pins: Mapping[int, Point],
    *,
    basis: str = "双定位钉唯一解",
) -> Calibration:
    """两枚以上共同定位钉 -> 唯一校正，并用全部钉互验。"""

    common = sorted(set(local_pins) & set(world_pins))
    if len(common) < 2:
        raise CalibrationError(f"曝光{exposure} 至少需要两枚共同定位钉，实际 {common}")
    first, second = common[0], common[1]
    k, cos, sin, tx, ty = _rigid_from_two(
        local_pins[first], local_pins[second], world_pins[first], world_pins[second]
    )
    return _verified(Calibration(exposure, k, cos, sin, tx, ty, basis), local_pins, world_pins)


def calibrate_hypotheses(
    exposure: int,
    local_pins: Mapping[int, Point],
    world_pins: Mapping[int, Point],
    hypotheses: Sequence[tuple[Fraction, Fraction, Fraction, str]],
) -> tuple[Calibration, ...]:
    """单钉曝光：给定 (cos, sin, scale, 说明) 候选，平移由该钉反解并验证。

    候选之间的差别就是两次曝光之间可能发生的原点/方向变化；与该枚共同
    定位钉不相容的候选被剔除。
    """

    common = sorted(set(local_pins) & set(world_pins))
    if len(common) != 1:
        raise CalibrationError(f"单钉假设路径要求恰有一枚共同钉，实际 {common}")
    pin_id = common[0]
    accepted: list[Calibration] = []
    for cos, sin, scale, note in hypotheses:
        p = local_pins[pin_id]
        rx = cos * p.x - sin * p.y
        ry = sin * p.x + cos * p.y
        q = world_pins[pin_id]
        cal = Calibration(
            exposure,
            scale,
            cos,
            sin,
            q.x - scale * rx,
            q.y - scale * ry,
            f"单钉相容假设:{note}",
        )
        try:
            accepted.append(_verified(cal, local_pins, world_pins))
        except CalibrationError:
            continue
    return tuple(accepted)


def _verified(
    cal: Calibration,
    local_pins: Mapping[int, Point],
    world_pins: Mapping[int, Point],
) -> Calibration:
    checks: list[tuple[int, Point, Point]] = []
    for pin_id in sorted(set(local_pins) & set(world_pins)):
        mapped = cal.apply(local_pins[pin_id])
        expected = world_pins[pin_id]
        if mapped != expected:
            raise CalibrationError(
                f"曝光{cal.exposure} 定位钉 {pin_id} 校正后 {mapped} 与台账 {expected} 不符"
            )
        checks.append((pin_id, mapped, expected))
    return Calibration(
        cal.exposure, cal.scale, cal.cos, cal.sin, cal.tx, cal.ty,
        cal.basis, tuple(checks),
    )


@dataclass(frozen=True)
class ExposureObs:
    """一次曝光的全部原始读数（帧坐标，单位格）。"""

    frame_id: int
    pins: Mapping[int, Point]
    stone_edges: Mapping[str, Point]
    center_nail: Point | None
    edge_correction: Point  # 壶边标记 -> 壶心的固定帧坐标修正向量


@dataclass(frozen=True)
class Plate:
    """一套校正下还原出的整盘壶心（世界坐标）。"""

    exposure: int
    calibration: Calibration
    centers: Mapping[str, Point]
    center: Point

    def distance_to_center(self, stone_id: str) -> Fraction:
        return self.centers[stone_id].dist(self.center)


def reconstruct_plate(
    exposure: ExposureObs,
    calibration: Calibration,
    *,
    center: Point = WORLD_ORIGIN,
) -> Plate:
    """用同一套校正把整盘壶心一次性还原到世界坐标。

    中心钉是场地台账固定的世界原点，不要求每幅帧都拍到（单钉歧义帧就
    拍不到）；若帧里带了中心钉读数，则额外要求它在校正后正好落在原点。
    """

    if calibration.exposure != exposure.frame_id:
        raise CalibrationError(
            f"校正属于曝光 {calibration.exposure}，不能套到曝光 {exposure.frame_id}"
        )
    centers: dict[str, Point] = {}
    for stone_id, edge in exposure.stone_edges.items():
        local_center = edge + exposure.edge_correction
        centers[stone_id] = calibration.apply(local_center)
    if exposure.center_nail is not None:
        mapped_nail = calibration.apply(exposure.center_nail)
        if mapped_nail != center:
            raise CalibrationError(
                f"曝光{exposure.frame_id} 中心钉校正后 {mapped_nail} != 世界原点 {center}"
            )
    return Plate(exposure.frame_id, calibration, centers, center)


@dataclass(frozen=True)
class ExposureSolution:
    exposure: int
    candidates: tuple[Plate, ...]

    @property
    def unique(self) -> bool:
        return len(self.candidates) == 1

    def plate(self) -> Plate:
        if not self.unique:
            raise CalibrationError(
                f"曝光{self.exposure} 存在 {len(self.candidates)} 套相容校正，不能指定唯一壶心盘"
            )
        return self.candidates[0]


def solve_exposure(
    exposure: ExposureObs,
    world_pins: Mapping[int, Point],
    *,
    hypotheses: Sequence[tuple[Fraction, Fraction, Fraction, str]] | None = None,
) -> ExposureSolution:
    """还原一整盘相容壶心：双钉唯一解，或单钉下的多候选整盘解。

    注意每个候选本身都是"整盘"——候选内部所有壶共用同一套校正；候选
    之间才允许方向/原点不同。
    """

    if len(set(exposure.pins) & set(world_pins)) >= 2:
        cal = calibrate_from_pins(exposure.frame_id, exposure.pins, world_pins)
        return ExposureSolution(exposure.frame_id, (reconstruct_plate(exposure, cal),))
    if hypotheses is None:
        raise CalibrationError(f"曝光{exposure.frame_id} 只有单钉且未提供方向假设")
    cals = calibrate_hypotheses(exposure.frame_id, exposure.pins, world_pins, hypotheses)
    if not cals:
        raise CalibrationError(f"曝光{exposure.frame_id} 没有任何假设通过定位钉验证")
    return ExposureSolution(
        exposure.frame_id, tuple(reconstruct_plate(exposure, cal) for cal in cals)
    )
