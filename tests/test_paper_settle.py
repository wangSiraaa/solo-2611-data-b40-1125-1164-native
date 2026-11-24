"""Tests for the worst-case delivery settlement engine.

The station only publishes partial evidence, so settlement is a constraint
problem over every compatible history.  These tests pin the exact numbers of
the mandated scenario and the supporting artifacts (witness + per-line added
lower bound).
"""

from __future__ import annotations

import pytest

from extcontract import (
    Observation,
    lower_bound_with_extra,
    parse_observations,
    settle,
    settle_block,
    verify_arrangement,
)
from extcontract.paper.settle import (
    INTERSECTION,
    NAMED_DELIVERED,
    NAMED_MISSED,
    SET_SIZE,
)

B1 = ["甲", "乙", "丙", "丁"]
LINES = {
    "L": ["甲", "乙", "丙", "丁"],
    "M": ["甲", "乙"],
}


def base_observations(intersection_qty: int = 1) -> list[Observation]:
    return [
        Observation("B1", SET_SIZE, 1, edition="workday", qty=2),
        Observation("B1", SET_SIZE, 1, edition="weekend", qty=2),
        Observation(
            "B1",
            INTERSECTION,
            1,
            edition="workday",
            edition_b="weekend",
            qty=intersection_qty,
        ),
    ]


def test_two_sets_of_two_intersect_one_guarantees_three():
    verdict = settle_block("B1", B1, base_observations(1), LINES)
    assert verdict.consistent
    assert verdict.lower_bound == 3
    assert len(verdict.possible_missers) == 4
    # Any of the four could be the single missed household.
    assert set(verdict.possible_missers) == set(B1)


def test_witness_attains_and_verifies_at_the_lower_bound():
    verdict = settle_block("B1", B1, base_observations(1), LINES)
    check = verify_arrangement(B1, base_observations(1), verdict.witness)
    assert check["valid"]
    assert check["union_size"] == verdict.lower_bound == 3
    # The witness stores one concrete delivered set per edition, each size 2.
    assert all(len(households) == 2 for households in verdict.witness.values())


def test_line_l_adds_one_but_cheap_line_m_adds_none_in_worst_case():
    verdict = settle_block("B1", B1, base_observations(1), LINES)
    assert verdict.line_evaluations["L"].added_lower_bound == 1
    assert verdict.line_evaluations["M"].added_lower_bound == 0
    # The line witness re-checks at exactly the added bound.
    le = verdict.line_evaluations["L"]
    union = frozenset().union(*(frozenset(s) for s in le.witness.values()))
    assert len(frozenset(LINES["L"]) - union) == 1


def test_correcting_intersection_to_two_lowers_the_guarantee():
    verdict = settle_block("B1", B1, base_observations(2), LINES)
    assert verdict.consistent
    assert verdict.lower_bound == 2
    assert verdict.line_evaluations["L"].added_lower_bound == 2


def test_restoring_intersection_to_one_restores_three():
    verdict = settle_block("B1", B1, base_observations(1), LINES)
    assert verdict.lower_bound == 3
    assert verdict.line_evaluations["L"].added_lower_bound == 1


def test_named_miss_in_every_edition_proves_household_and_narrows_missers():
    obs = base_observations(1) + [
        Observation("B1", NAMED_MISSED, 2, edition="workday", household="丁"),
        Observation("B1", NAMED_MISSED, 2, edition="weekend", household="丁"),
    ]
    verdict = settle_block("B1", B1, obs, LINES)
    assert verdict.proven_missed == ["丁"]
    assert verdict.possible_missers == ["丁"]
    # With 丁 forced out of both size-2 sets, the three others are all reached.
    assert verdict.lower_bound == 3


def test_named_miss_in_only_one_edition_is_not_proof_for_targeted_signing():
    obs = base_observations(1) + [
        Observation("B1", NAMED_MISSED, 2, edition="workday", household="丁"),
    ]
    verdict = settle_block("B1", B1, obs, LINES)
    assert verdict.proven_missed == []
    assert "丁" in verdict.possible_missers


def test_named_delivery_forces_inclusion():
    obs = base_observations(1) + [
        Observation("B1", NAMED_DELIVERED, 2, edition="workday", household="甲"),
        Observation("B1", NAMED_DELIVERED, 2, edition="weekend", household="甲"),
    ]
    verdict = settle_block("B1", B1, obs, LINES)
    check = verify_arrangement(B1, obs, verdict.witness)
    assert check["valid"]
    assert "甲" in check["union"]


def test_contradictory_evidence_is_inconsistent_not_credited():
    obs = base_observations(1) + [
        Observation("B1", NAMED_DELIVERED, 2, edition="workday", household="丁"),
        Observation("B1", NAMED_MISSED, 2, edition="workday", household="丁"),
    ]
    verdict = settle_block("B1", B1, obs, LINES)
    assert not verdict.consistent
    assert verdict.conflicts
    assert verdict.lower_bound == 0
    assert verdict.line_evaluations == {}


def test_unsatisfiable_intersection_is_detected():
    # Two size-2 sets inside four households cannot intersect in three.
    verdict = settle_block("B1", B1, base_observations(3), LINES)
    assert not verdict.consistent


def test_roster_violation_is_rejected():
    obs = [Observation("B1", NAMED_DELIVERED, 1, edition="workday", household="壬")]
    verdict = settle_block("B1", B1, obs, LINES)
    assert not verdict.consistent
    assert any("壬" in c for c in verdict.conflicts)


def test_settlement_is_per_block_and_blocks_are_disjoint():
    universes = {"B1": B1, "B2": ["戊", "己"]}
    obs = base_observations(1) + [
        Observation("B2", SET_SIZE, 1, edition="workday", qty=1),
    ]
    verdicts = settle(universes, obs, {"B1": LINES})
    assert verdicts["B1"].lower_bound == 3
    assert verdicts["B2"].lower_bound == 1
    assert "L" in verdicts["B1"].line_evaluations
    assert verdicts["B2"].line_evaluations == {}


def test_projection_with_extra_sets_counts_worst_case_union():
    obs = base_observations(1)
    # Booking line L (all four) guarantees the full roster in every history.
    assert lower_bound_with_extra(B1, obs, [frozenset(LINES["L"])]) == 4
    # The cheap line M may add nothing (its households can already be covered).
    assert lower_bound_with_extra(B1, obs, [frozenset(LINES["M"])]) == 3
    # A singleton outside every worst-case union is still only a worst-case add.
    assert lower_bound_with_extra(B1, obs, [frozenset(["丁"])]) == 3


def test_parse_published_rows_round_trip():
    rows = [
        {"values": {
            "block": "B1", "edition": "workday", "round": "1",
            "measure": "set_size", "household": None, "qty": "2"}},
        {"values": {
            "block": "B1", "edition": "workday|weekend", "round": "1",
            "measure": "intersection", "household": None, "qty": "1"}},
        {"values": {
            "block": "B1", "edition": "workday", "round": "2",
            "measure": "named_missed", "household": "丁", "qty": None}},
    ]
    observations = parse_observations(rows)
    assert observations[0].qty == 2
    assert observations[1].edition == "workday"
    assert observations[1].edition_b == "weekend"
    assert observations[2].household == "丁"


def test_parse_rejects_unknown_measure_and_bad_rows():
    from extcontract.errors import ContractError

    with pytest.raises(ContractError):
        parse_observations([
            {"values": {"block": "B1", "edition": "e", "round": 1,
                        "measure": "telepathy", "household": None, "qty": None}}
        ])
    with pytest.raises(ContractError):
        parse_observations([
            {"values": {"block": "B1", "measure": SET_SIZE, "round": 1}}
        ])


def test_json_snapshot_is_reviewable():
    verdict = settle_block("B1", B1, base_observations(1), LINES)
    payload = verdict.to_json()
    assert payload["lower_bound"] == 3
    assert payload["line_evaluations"]["M"]["added_lower_bound"] == 0
    assert isinstance(payload["witness"], dict)
