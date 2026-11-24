"""Pure eligibility rules over effective station segments.

No board state, no intentions, no seats live here: given the segments that are
currently effective (the published feed rows), decide which pairs qualify.

The counting rule, exactly as briefed:

* only segments where **both** athletes of the pair are actually on the rope
  together contribute — a segment jumped by 甲+丙 never adds to the 甲+乙 pair;
* within one match, the pair's same-time-on-rope valid segments are summed
  (a substitute's segments are simply absent from that sum);
* at one station the pair's **best match** counts;
* the pair must reach :data:`JUMPS_PER_STATION` counted jumps at each of at
  least :data:`REQUIRED_STATIONS` **different** stations.

The returned :class:`PairQualification` keeps the qualifying stations, the
chosen best match, and every contributing segment id linked together, so the
promotion desk never sees a bare yes/no without evidence.
"""

from __future__ import annotations

from collections import defaultdict

from . import (
    JUMPS_PER_STATION,
    REQUIRED_STATIONS,
    PairQualification,
    Segment,
    StationBest,
    pair_key,
)


def evaluate_pair(
    pair: tuple[str, str], segments: list[Segment]
) -> PairQualification:
    """Evaluate one pair against the effective segments of all stations."""
    target = pair_key(*pair)
    # station -> match -> (jumps, [segment ids])
    per_match: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for segment in segments:
        if not segment.valid or segment.pair != target:
            continue
        per_match[segment.station][segment.match].append(segment)

    station_bests: dict[str, StationBest] = {}
    for station, matches in per_match.items():
        best: StationBest | None = None
        for match, segs in matches.items():
            ordered = sorted(segs, key=lambda s: (s.segment_index, s.segment_id))
            total = sum(s.jumps for s in ordered)
            candidate = StationBest(
                station=station,
                match=match,
                jumps=total,
                segment_ids=[s.segment_id for s in ordered],
            )
            if best is None or (
                candidate.jumps,
                candidate.match,
            ) > (best.jumps, best.match):
                best = candidate
        if best is not None:
            station_bests[station] = best

    qualifying = sorted(
        station
        for station, best in station_bests.items()
        if best.jumps >= JUMPS_PER_STATION
    )
    return PairQualification(
        pair=target,
        qualified=len(qualifying) >= REQUIRED_STATIONS,
        stations=station_bests,
        qualifying_stations=qualifying,
    )


def evaluate_all(segments: list[Segment]) -> dict[tuple[str, str], PairQualification]:
    """Evaluate every pair that appears on any effective valid segment."""
    pairs = {s.pair for s in segments if s.valid}
    return {pair: evaluate_pair(pair, segments) for pair in sorted(pairs)}


def reevaluate(
    pair: tuple[str, str], segments: list[Segment]
) -> PairQualification:
    """Recompute a verdict after a ruling correction (same pure rule)."""
    return evaluate_pair(pair, segments)
