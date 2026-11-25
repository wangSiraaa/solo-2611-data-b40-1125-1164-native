"""计分台：犯规撤除、比赛圈半径与标准壶赛计分规则。

量测端只负责还原壶心；**哪些壶因犯规被撤除、比赛圈半径是多少**由计分台
另管，二者不共享状态。规则（题面合同）：

1. 仅圈内有效壶计入：壶心距中心严格小于比赛圈半径（圈上不算入）。
2. 离心最近的一方得分；该方每有一壶**严格近于**最近的对方壶得一分。
3. 最近双方同距 -> 本局记零。
4. 对方没有圈内壶时，本方所有圈内壶各计一分；双方都无壶记零。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Sequence

from .geometry import Plate, fraction

RED = "red"
BLUE = "blue"
COLORS = (RED, BLUE)


class ScoringError(ValueError):
    pass


@dataclass(frozen=True)
class ScoreCase:
    """一套完整校正盘在一个计分台配置下的计分结论与逐壶证据。"""

    plate_index: int
    scoring_side: str | None
    score: int
    nearest_red: Fraction | None
    nearest_blue: Fraction | None
    in_circle: Mapping[str, Fraction]
    removed: tuple[str, ...]
    outside: tuple[str, ...]
    reason: str

    def as_json(self) -> dict[str, object]:
        return {
            "plate_index": self.plate_index,
            "scoring_side": self.scoring_side,
            "score": self.score,
            "nearest_red": fraction(self.nearest_red) if self.nearest_red is not None else None,
            "nearest_blue": fraction(self.nearest_blue) if self.nearest_blue is not None else None,
            "in_circle": {k: fraction(v) for k, v in self.in_circle.items()},
            "removed": list(self.removed),
            "outside": list(self.outside),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ScoreDesk:
    """计分台配置：比赛圈半径 + 因犯规撤除的壶号（与量测分离）。"""

    circle_radius: Fraction
    removed: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.circle_radius <= 0:
            raise ScoringError("比赛圈半径必须为正")

    @classmethod
    def create(cls, circle_radius: object, removed: Sequence[str] = ()) -> "ScoreDesk":
        return cls(Fraction(circle_radius), frozenset(removed))

    def with_removal(self, *stone_ids: str) -> "ScoreDesk":
        return ScoreDesk(self.circle_radius, self.removed | frozenset(stone_ids))


def _color_of(stone_id: str) -> str:
    if stone_id.startswith(RED):
        return RED
    if stone_id.startswith(BLUE):
        return BLUE
    raise ScoringError(f"壶号 {stone_id} 无法识别红/蓝（需以 red/blue 开头）")


def score_plate(plate: Plate, desk: ScoreDesk, *, plate_index: int = 0) -> ScoreCase:
    """对一整盘（同一校正下的所有壶心）执行一次计分。"""

    distances = {stone_id: plate.distance_to_center(stone_id) for stone_id in plate.centers}
    removed = tuple(sorted(s for s in distances if s in desk.removed))
    outside = tuple(sorted(s for s, d in distances.items() if s not in desk.removed and d >= desk.circle_radius))
    in_circle = {
        s: d
        for s, d in distances.items()
        if s not in desk.removed and d < desk.circle_radius
    }

    def nearest(side: str) -> Fraction | None:
        values = [d for s, d in in_circle.items() if _color_of(s) == side]
        return min(values) if values else None

    nearest_red = nearest(RED)
    nearest_blue = nearest(BLUE)

    if nearest_red is None and nearest_blue is None:
        return ScoreCase(plate_index, None, 0, nearest_red, nearest_blue,
                         in_circle, removed, outside, "双方圈内均无有效壶，记零")
    if nearest_red is None:
        n = sum(1 for s in in_circle if _color_of(s) == BLUE)
        return ScoreCase(plate_index, BLUE, n, nearest_red, nearest_blue,
                         in_circle, removed, outside, f"红方圈内无壶，蓝方 {n} 只圈内壶全计")
    if nearest_blue is None:
        n = sum(1 for s in in_circle if _color_of(s) == RED)
        return ScoreCase(plate_index, RED, n, nearest_red, nearest_blue,
                         in_circle, removed, outside, f"蓝方圈内无壶，红方 {n} 只圈内壶全计")
    if nearest_red == nearest_blue:
        return ScoreCase(plate_index, None, 0, nearest_red, nearest_blue,
                         in_circle, removed, outside,
                         f"最近红壶 {fraction(nearest_red)} 与最近蓝壶同距，并列记零")

    if nearest_red < nearest_blue:
        side, nearest_opponent = RED, nearest_blue
    else:
        side, nearest_opponent = BLUE, nearest_red
    scoring = [
        s for s, d in in_circle.items()
        if _color_of(s) == side and d < nearest_opponent
    ]
    return ScoreCase(
        plate_index, side, len(scoring), nearest_red, nearest_blue,
        in_circle, removed, outside,
        f"{side} 方更近，{len(scoring)} 只壶严格近于最近对方壶 "
        f"{fraction(nearest_opponent)}",
    )


@dataclass(frozen=True)
class ScoreRange:
    """跨所有相容校正盘的可证明分数区间。"""

    side: str
    guaranteed: int
    best: int
    cases: tuple[ScoreCase, ...]
    settled: bool

    def as_json(self) -> dict[str, object]:
        return {
            "side": self.side,
            "guaranteed": self.guaranteed,
            "best": self.best,
            "settled": self.settled,
            "score": self.guaranteed if self.settled else None,
            "cases": [c.as_json() for c in self.cases],
        }


def score_range(plates: Sequence[Plate], desk: ScoreDesk) -> ScoreRange:
    """每一盘整体计分后再汇总：保证分 = 所有相容盘的公共下限。

    禁止把各壶在不同校正下的最小距离拼成一盘——比较必须在同一校正下
    完成，本函数逐盘调用 :func:`score_plate`，从未跨盘取过单壶极值。
    """

    if not plates:
        raise ScoringError("至少需要一个相容校正盘")
    cases = tuple(score_plate(plate, desk, plate_index=i) for i, plate in enumerate(plates))
    sides = {c.scoring_side for c in cases}
    if len(sides) != 1:
        raise ScoringError(f"相容盘对得分方都不一致：{sides}，连保证分都无法给出")
    side = next(iter(sides))
    scores = [c.score for c in cases]
    guaranteed, best = min(scores), max(scores)
    return ScoreRange(side, guaranteed, best, cases, settled=guaranteed == best)


def naive_per_stone_minimum(plates: Sequence[Plate], desk: ScoreDesk) -> ScoreCase:
    """反面教材：每只壶各自挑使自己距离最小的校正，再拼一盘计分。

    题面明确禁止这种拼法。保留它是为了在测试里证明：它会给出与整盘
    :func:`score_range` 不同（且无校正支撑）的分数。
    """

    all_ids = sorted({s for p in plates for s in p.centers})
    stitched: dict[str, Fraction] = {}
    for stone_id in all_ids:
        stitched[stone_id] = min(p.distance_to_center(stone_id) for p in plates)

    kept = {s: d for s, d in stitched.items()
            if s not in desk.removed and d < desk.circle_radius}
    reds = sorted((d, s) for s, d in kept.items() if _color_of(s) == RED)
    blues = sorted((d, s) for s, d in kept.items() if _color_of(s) == BLUE)
    if not reds and not blues:
        return ScoreCase(-1, None, 0, None, None, kept,
                         tuple(sorted(desk.removed)), (), "拼盘：无圈内壶")
    if not reds:
        return ScoreCase(-1, BLUE, len(blues), None, blues[0][0], kept,
                         tuple(sorted(desk.removed)), (), "拼盘：红无壶")
    if not blues:
        return ScoreCase(-1, RED, len(reds), reds[0][0], None, kept,
                         tuple(sorted(desk.removed)), (), "拼盘：蓝无壶")
    nr, nb = reds[0][0], blues[0][0]
    if nr == nb:
        return ScoreCase(-1, None, 0, nr, nb, kept,
                         tuple(sorted(desk.removed)), (), "拼盘：并列")
    if nr < nb:
        n = sum(1 for d, _ in reds if d < nb)
        return ScoreCase(-1, RED, n, nr, nb, kept,
                         tuple(sorted(desk.removed)), (),
                         "拼盘（禁止使用）：各壶各自最小距离")
    n = sum(1 for d, _ in blues if d < nr)
    return ScoreCase(-1, BLUE, n, nr, nb, kept,
                     tuple(sorted(desk.removed)), (),
                     "拼盘（禁止使用）：各壶各自最小距离")
