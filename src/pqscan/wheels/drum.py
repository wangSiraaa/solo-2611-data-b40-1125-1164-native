"""计程鼓（里程鼓）模 10 算术。

鼓跟车身走、只有 0..9 十个刻度，每走一格前进一刻，满十格翻零。
一次相邻观测之间真正走的格数 d 与鼓面读数满足：

    after = (before + d) mod 10
    d >= 0

因此由两个鼓面只能定出 d 的"余数类"：d ≡ (after - before) (mod 10)。
同一段行程表给出该段允许的格数区间 [lo, hi]，可行解为该区间内余数相符的
那些整数；每走满 10 格跨一次零，跨零次数 = d // 10，必须与整段行程一致
（即只能在允许区间内挑余数相符的实际格数，跨零次数随之唯一确定）。
"""
from __future__ import annotations

from ..errors import WheelsError

DRUM_TICKS = 10


def _check_reading(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise WheelsError(f"鼓面读数 {name}={value!r} 必须是整数")
    if not 0 <= value < DRUM_TICKS:
        raise WheelsError(f"鼓面读数 {name}={value} 超出 0..{DRUM_TICKS - 1}")
    return value


def advance(reading: int, ticks: int) -> int:
    """鼓面从 reading 走 ticks 格后的读数（ticks 非负）。"""
    _check_reading(reading, "reading")
    if ticks < 0:
        raise WheelsError(f"走行格数不能为负: {ticks}")
    return (reading + ticks) % DRUM_TICKS


def rollovers(before: int, after: int, ticks: int) -> int:
    """给定实际走行 ticks，校验终值并返回该段跨过零的次数（ticks // 10）。"""
    _check_reading(before, "before")
    _check_reading(after, "after")
    if ticks < 0:
        raise WheelsError(f"走行格数不能为负: {ticks}")
    if advance(before, ticks) != after:
        raise WheelsError(
            f"鼓面 {before} 走 {ticks} 格应到 {advance(before, ticks)}，与观测终值 {after} 不符"
        )
    return ticks // DRUM_TICKS


def feasible_ticks(before: int, after: int, lo: int, hi: int) -> list[int]:
    """行程表允许 [lo, hi] 格时，与首尾鼓面都相符的全部实际格数（升序）。

    d 必须满足 d ≡ (after-before) (mod 10) 且 lo <= d <= hi。
    空列表表示该段读数与行程表互不相容（履历矛盾）。
    """
    _check_reading(before, "before")
    _check_reading(after, "after")
    if lo < 0 or hi < lo:
        raise WheelsError(f"行程表允许区间非法: [{lo}, {hi}]")
    remainder = (after - before) % DRUM_TICKS
    first = lo + ((remainder - lo) % DRUM_TICKS)
    if first > hi:
        return []
    return list(range(first, hi + 1, DRUM_TICKS))


def rollover_options(before: int, after: int, lo: int, hi: int) -> list[int]:
    """可行实际格数各自对应的跨零次数（与 feasible_ticks 同序）。"""
    return [ticks // DRUM_TICKS for ticks in feasible_ticks(before, after, lo, hi)]
