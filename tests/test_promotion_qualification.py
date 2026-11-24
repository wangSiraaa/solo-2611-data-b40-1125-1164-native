"""Tests for the pure eligibility rule."""

from __future__ import annotations

from extcontract.promotion import (
    JUMPS_PER_STATION,
    REQUIRED_STATIONS,
    Segment,
    evaluate_pair,
    pair_key,
)


def make(seg_id, station, match, index, a, b, jumps, valid=True, revision=1):
    return Segment(seg_id, station, match, index, a, b, jumps, valid, revision)


def test_only_segments_where_both_are_on_the_rope_count():
    # 甲+丙 segments must never add to the 甲+乙 pair.
    segments = [
        make("E1", "east", "M1", 1, "甲", "乙", 3),
        make("E2", "east", "M1", 2, "甲", "乙", 2),  # 甲+乙 reach 5 at east
        make("E3", "east", "M1", 3, "甲", "丙", 5),  # substitute on the rope
        make("W1", "west", "M2", 1, "甲", "乙", 5),
    ]
    verdict = evaluate_pair(pair_key("甲", "乙"), segments)
    assert verdict.stations["east"].jumps == 5
    assert verdict.stations["east"].segment_ids == ["E1", "E2"]
    assert verdict.qualified is True
    assert verdict.qualifying_stations == ["east", "west"]
    # The substitute segment is attributed to the 甲+丙 pair instead.
    other = evaluate_pair(pair_key("甲", "丙"), segments)
    assert other.stations["east"].jumps == 5


def test_within_one_match_segments_accumulate_but_void_ones_do_not():
    segments = [
        make("E1", "east", "M1", 1, "甲", "乙", 3),
        make("E2", "east", "M1", 2, "甲", "乙", 2),
        make("E3", "east", "M1", 3, "甲", "乙", 4, valid=False),
        make("W1", "west", "M2", 1, "甲", "乙", 5),
    ]
    verdict = evaluate_pair(pair_key("甲", "乙"), segments)
    assert verdict.stations["east"].jumps == 5
    assert verdict.stations["east"].segment_ids == ["E1", "E2"]


def test_best_match_per_station_is_taken():
    segments = [
        make("E-a1", "east", "MA", 1, "甲", "乙", 2),
        make("E-a2", "east", "MA", 2, "甲", "乙", 2),  # MA = 4
        make("E-b1", "east", "MB", 1, "甲", "乙", 5),  # MB = 5 (best)
        make("W1", "west", "MW", 1, "甲", "乙", 5),
    ]
    verdict = evaluate_pair(pair_key("甲", "乙"), segments)
    assert verdict.stations["east"].match == "MB"
    assert verdict.stations["east"].jumps == 5
    assert verdict.qualified is True


def test_two_different_stations_are_required_one_station_never_qualifies():
    segments = [
        make("E1", "east", "M1", 1, "甲", "乙", 5),
        make("E2", "east", "M2", 1, "甲", "乙", 5),
    ]
    verdict = evaluate_pair(pair_key("甲", "乙"), segments)
    assert verdict.qualified is False
    assert len(verdict.qualifying_stations) == 1


def test_four_jumps_after_correction_falls_under_threshold():
    segments = [
        make("E1", "east", "M1", 1, "甲", "乙", 5),
        make("W1", "west", "M2", 1, "甲", "乙", 3),
        make("W2", "west", "M2", 2, "甲", "丙", 2),  # corrected: not 甲+乙
    ]
    verdict = evaluate_pair(pair_key("甲", "乙"), segments)
    assert verdict.stations["west"].jumps == 3
    assert verdict.qualified is False
    assert verdict.qualifying_stations == ["east"]


def test_display_order_is_irrelevant():
    segments = [
        make("E1", "east", "M1", 1, "乙", "甲", 5),
        make("W1", "west", "M2", 1, "甲", "乙", 5),
    ]
    assert evaluate_pair(("甲", "乙"), segments).qualified
    assert evaluate_pair(("乙", "甲"), segments).pair == pair_key("甲", "乙")


def test_constants_match_the_brief():
    assert JUMPS_PER_STATION == 5
    assert REQUIRED_STATIONS == 2
