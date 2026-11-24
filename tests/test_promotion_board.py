"""Tests for the promotion board and every mandated narrative."""

from __future__ import annotations

import pytest

from extcontract.promotion import (
    pair_key,
    CREDENTIAL_ISSUED,
    CREDENTIAL_REVOKED,
    INTENTION_EXPIRED,
    INTENTION_WITHDRAWN,
    MECHANISM_EARLY_LOCK,
    MECHANISM_LATE_SEAL,
    REASON_LOCKED,
    REASON_NOT_MUTUAL,
    REASON_NOT_QUALIFIED,
    REASON_EXPIRED,
    REVOKE_RULING_DISQUALIFIED,
    StationDirectory,
    pair_label,
)
from extcontract.promotion.board import PromotionBoard
from extcontract.promotion.scenarios import (
    BING,
    DING,
    EAST,
    JIA,
    TWO_STATIONS,
    WEST,
    YI,
    build_directory,
    four_person_preseal_actions,
    run_early_lock_late_withdrawal,
    run_early_lock_tradeoff,
    run_late_seal_baseline,
    run_late_seal_post_expiry_replay,
    run_late_seal_preseal_replay,
    run_two_person_early_lock,
    two_person_reports,
    west_correction_to_jia_bing,
    west_restoration,
)


def labels(credentials):
    return sorted(c["label"] for c in credentials)


# --------------------------------------------------------------------------- mechanism guard

def test_board_runs_only_its_own_mechanism():
    late = PromotionBoard(["甲", "乙"], 1, StationDirectory(TWO_STATIONS),
                          mechanism=MECHANISM_LATE_SEAL)
    assert late.apply_lock("甲", "乙", when=1).reason == "EARLY_LOCK_NOT_OPERATED"

    early = PromotionBoard(["甲", "乙"], 1, StationDirectory(TWO_STATIONS),
                           mechanism=MECHANISM_EARLY_LOCK)
    with pytest.raises(ValueError):
        early.seal(when=1)


# --------------------------------------------------------------------------- scenario 1: two people, one seat

def test_two_person_promotes_then_correction_revokes_and_restore_cannot_repromote():
    result = run_two_person_early_lock()
    board = result["board"]

    history = board.credential_history()
    assert len(history) == 1
    credential = history[0]
    assert credential["label"] == pair_label(pair_key(JIA, YI))
    assert credential["status"] == CREDENTIAL_REVOKED
    assert credential["revoke_reason"] == REVOKE_RULING_DISQUALIFIED
    # The issued version and the evidence it was built on are retained.
    assert credential["version"] == 1
    # The credential keeps the qualification snapshot it was issued with.
    assert set(credential["qualification"]["qualifying_stations"]) == {EAST, WEST}
    # The revocation journal entry carries the failing at-the-time verdict.
    revocation = next(e for e in board.journal() if e["event"] == "CREDENTIAL_REVOKED")
    assert "1 station(s)" in revocation["detail"] and EAST in revocation["detail"]
    # The score is later restored, so the live verdict is eligible again -
    # but the revoked credential stays revoked and consent is now missing.
    assert board.qualification_for((JIA, YI)).qualified is True

    # The seat is released and no active credential remains.
    assert board.active_credentials() == []
    assert board.seats_remaining() == 1

    # After the score is restored the pair is eligible again, but with 乙's
    # consent withdrawn mutual consent is gone: the same pair still loses.
    assert board.qualification_for((JIA, YI)).qualified is True
    assert board.receipt_for("APP-002")["status"] == INTENTION_WITHDRAWN
    last = result["answers"][-1]["receipt"]
    assert last["accepted"] is False and last["reason"] == REASON_NOT_MUTUAL


def test_two_person_qualification_switches_with_each_ruling_version():
    directory = build_directory(TWO_STATIONS, two_person_reports())
    board = PromotionBoard([JIA, YI, BING], 1, directory, mechanism=MECHANISM_EARLY_LOCK)
    assert board.qualification_for((JIA, YI)).qualified is True

    directory.feed(WEST).ingest_report(
        west_correction_to_jia_bing()["rows"], file_id="west-r2"
    )
    verdict = board.qualification_for((JIA, YI))
    assert verdict.qualified is False
    # The last segment is re-attributed to 甲+丙, so 甲+乙 keep exactly four.
    assert verdict.stations[WEST].jumps == 4
    assert verdict.stations[WEST].segment_ids == ["W-1"]
    # The moved segment (its one jump) now belongs to the 甲+丙 pair.
    assert board.qualification_for((JIA, BING)).stations[WEST].jumps == 1

    directory.feed(WEST).ingest_report(
        west_restoration()["rows"], file_id="west-r3"
    )
    assert board.qualification_for((JIA, YI)).qualified is True


def test_early_lock_rejects_before_consent_eligibility_or_seat():
    directory = build_directory(TWO_STATIONS, two_person_reports())
    board = PromotionBoard([JIA, YI, BING], 1, directory, mechanism=MECHANISM_EARLY_LOCK)

    only_one = board.apply_lock(JIA, target=YI, when=1)
    assert only_one.reason == REASON_NOT_MUTUAL

    board.declare(JIA, YI, declared_at=1, expires_at=3)
    board.declare(YI, JIA, declared_at=1, expires_at=3)
    # Damage the West evidence first: mutual consent exists, eligibility not.
    directory.feed(WEST).ingest_report(
        west_correction_to_jia_bing()["rows"], file_id="west-r2"
    )
    not_qualified = board.apply_lock(JIA, target=YI, when=2)
    assert not_qualified.reason == REASON_NOT_QUALIFIED
    assert board.seats_remaining() == 1


# --------------------------------------------------------------------------- scenario 2: four people, two seats, late seal

def test_first_applicant_gets_no_formal_seat_before_the_seal():
    result = run_late_seal_baseline()
    board = result["board"]
    first = board.receipt_for("APP-001")
    assert first["formal_seat"] is False
    # After the day-4 expiry the terminal status is EXPIRED; the seal-time
    # answer (the usable result at the deadline) was DECLINED and is journaled.
    assert first["status"] == INTENTION_EXPIRED
    # The price of waiting under late seal: the earliest request is the one
    # the maximum matching cannot keep.
    decline = next(
        e for e in board.journal()
        if e["event"] == "INTENTION_DECLINED" and e["application_no"] == "APP-001"
    )
    assert decline["reason"] == "NOT_IN_MAXIMUM_MATCHING"


def test_seal_matches_maximum_two_teams_and_issues_two_credentials():
    result = run_late_seal_baseline()
    board = result["board"]
    seal = next(a["result"] for a in result["answers"] if a["action"]["kind"] == "seal")
    assert seal["seats_used"] == 2
    assert labels(board.active_credentials()) == [
        pair_label(pair_key(YI, DING)),
        pair_label(pair_key(JIA, BING)),
    ]
    # All six declarations are on file and queryable by their application no.
    for number in range(1, 7):
        assert board.receipt_for(f"APP-{number:03d}") is not None


def test_replay_pre_seal_correction_keeps_two_teams_but_reasons_change():
    result = run_late_seal_preseal_replay()
    board = result["board"]
    assert labels(board.active_credentials()) == [
        pair_label(pair_key(YI, DING)),
        pair_label(pair_key(JIA, BING)),
    ]
    # 甲+乙 never held a credential in this replay; it is declined at the seal
    # for ineligibility rather than for losing the matching.
    decline = next(
        e for e in board.journal()
        if e["event"] == "INTENTION_DECLINED" and e["application_no"] == "APP-001"
    )
    assert decline["reason"] == "NOT_ELIGIBLE_AT_SEAL"
    assert board.qualification_for((JIA, YI)).qualified is False


def test_replay_post_expiry_correction_cannot_back_sign_anyone():
    result = run_late_seal_post_expiry_replay()
    board = result["board"]
    # The two sealed credentials are untouched: the correction only concerns
    # the pair that was already declined.
    assert labels(board.active_credentials()) == [
        pair_label(pair_key(YI, DING)),
        pair_label(pair_key(JIA, BING)),
    ]
    ruling = next(e for e in board.journal() if e["event"] == "RULING_RECONCILED")
    assert ruling["revoked"] == [] and ruling["backfilled"] == []
    # The original receipts still answer by application number, now expired.
    assert board.receipt_for("APP-001")["status"] == INTENTION_EXPIRED
    assert board.seats_remaining() == 0


def test_both_replays_start_from_identical_pre_seal_records():
    pre = four_person_preseal_actions()
    a = run_late_seal_preseal_replay()
    b = run_late_seal_post_expiry_replay()
    # Same six application numbers and same reports before the divergence.
    assert [
        (x["action"].get("file_id"), x["action"].get("applicant"))
        for x in a["answers"]
        if x["action"]["kind"] in ("declare",)
    ] == [
        (x["action"].get("file_id"), x["action"].get("applicant"))
        for x in b["answers"]
        if x["action"]["kind"] in ("declare",)
    ]
    assert len(pre) == 8


def test_late_seal_expires_and_blocks_declarations_after_the_seal():
    result = run_late_seal_baseline()
    board = result["board"]
    late = board.declare(JIA, YI, declared_at=9, expires_at=10)
    assert late.accepted is False and late.reason == "BOARD_ALREADY_SEALED"
    board.expire(4)
    # The status becomes EXPIRED (it can never be back-signed), but the
    # original seal-time decline remains in the journal for the application no.
    assert board.receipt_for("APP-001")["status"] == INTENTION_EXPIRED
    events = [e["event"] for e in board.journal()
              if e.get("application_no") == "APP-001"]
    assert "INTENTION_DECLINED" in events and "INTENTION_EXPIRED" in events


# --------------------------------------------------------------------------- early-lock tradeoff and late withdrawal

def test_early_lock_tradeoff_locks_immediately_then_reoffers_on_disqualification():
    result = run_early_lock_tradeoff()
    board = result["board"]
    history = board.credential_history()
    assert [(c["label"], c["status"]) for c in history] == [
        (pair_label(pair_key(JIA, YI)), CREDENTIAL_REVOKED),
        (pair_label(pair_key(JIA, BING)), CREDENTIAL_ISSUED),
    ]
    # The day-2 甲+丙 lock attempt while 甲 was locked was refused.
    refused = next(
        a["receipt"] for a in result["answers"]
        if a["action"].get("at") == 2 and a["action"]["kind"] == "lock"
    )
    assert refused["reason"] == REASON_LOCKED
    # The later lock consumes the released seat using still-valid intentions.
    assert board.seats_remaining() == 1


def test_early_lock_late_withdrawal_leaves_seat_unfillable():
    result = run_early_lock_late_withdrawal()
    board = result["board"]
    assert board.credential_history()[0]["status"] == CREDENTIAL_REVOKED
    assert board.active_credentials() == []
    assert board.seats_remaining() == 2
    last = result["answers"][-1]["receipt"]
    assert last["reason"] in (REASON_EXPIRED, REASON_NOT_MUTUAL)


# --------------------------------------------------------------------------- snapshot / replay mechanics

def test_snapshot_restore_reproduces_state_for_a_second_replay():
    result = run_late_seal_baseline()
    board = result["board"]
    snap = board.snapshot()
    before = board.credential_history()

    board.restore(snap)
    assert board.credential_history() == before
    assert board.sealed is True and board.sealed_at == 3


# --------------------------------------------------------------------------- athlete withdrawal

def test_formal_athlete_withdrawal_revokes_credential_and_releases_seat():
    result = run_late_seal_baseline()
    board = result["board"]
    outcome = board.withdraw_athlete(DING, when=6)
    assert outcome["accepted"] is True
    revoked_id = outcome["revoked_credentials"][0]
    credential = next(c for c in board.credential_history() if c["credential_id"] == revoked_id)
    assert credential["status"] == CREDENTIAL_REVOKED
    assert credential["revoke_reason"] == "ATHLETE_FORMALLY_WITHDREW"
    assert board.seats_remaining() == 1
    # No back-signing: every remaining intention was already declined at seal.
    assert outcome["backfilled_credentials"] == []


def test_matching_never_issues_two_credentials_to_one_athlete():
    # A pairwise-consenting triangle is a general (non-bipartite) graph:
    # the maximum matching has exactly one pair, never two sharing an athlete.
    from extcontract.promotion.scenarios import segment_row

    stations = ["east", "west"]
    pairs = [(JIA, YI), (JIA, BING), (YI, BING)]
    directory = StationDirectory(stations)
    rows = []
    sid = 0
    for a, b in pairs:
        for station in stations:
            sid += 1
            rows.append(
                segment_row(
                    f"T{sid}", station=station, match=f"M{a}{b}{station}",
                    index=1, a=a, b=b, jumps=5,
                )
            )
    for station in stations:
        directory.feed(station).ingest_report(
            [r for r in rows if r["values"]["station"] == station],
            file_id=f"{station}-r1",
        )
    board = PromotionBoard([JIA, YI, BING], 3, directory, mechanism=MECHANISM_LATE_SEAL)
    for a, b in pairs:
        board.declare(a, b, declared_at=1, expires_at=3)
        board.declare(b, a, declared_at=1, expires_at=3)
    result = board.seal(when=3)
    used = [athlete for credential in result.matched for athlete in credential.pair]
    assert len(used) == len(set(used))
    assert len(result.matched) == 1


def test_idempotent_redeclaration_returns_original_application_number():
    directory = build_directory(TWO_STATIONS, two_person_reports())
    board = PromotionBoard([JIA, YI, BING], 1, directory, mechanism=MECHANISM_EARLY_LOCK)
    first = board.declare(JIA, YI, declared_at=1, expires_at=5)
    second = board.declare(JIA, YI, declared_at=1, expires_at=5)
    assert first.application_no == second.application_no
