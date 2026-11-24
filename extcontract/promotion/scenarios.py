"""Declarative builders and replays for the mandated promotion scenarios.

The brief's histories are expressed as plain, JSON-shaped *actions* so the same
pre-seal record can be replayed twice with only the timing of one ruling
correction changed:

    {"kind": "report", "station": ..., "file_id": ..., "rows": [...]}
    {"kind": "declare", "applicant": ..., "target": ..., "at": ...,
     "expires_at": ...}
    {"kind": "lock", "applicant": ..., "target": ..., "at": ...}
    {"kind": "withdraw_intention", "applicant": ..., "target": ..., "at": ...}
    {"kind": "withdraw_athlete", "athlete": ..., "at": ...}
    {"kind": "seal", "at": ...}
    {"kind": "expire", "at": ...}
    {"kind": "ruling", "at": ...}

``build_directory`` replays only the report actions; :func:`run` replays the
whole history onto a fresh board.  Nothing here is hidden state — every action
is inspectable, and every answer the board gives is collected.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import (
    MECHANISM_EARLY_LOCK,
    MECHANISM_LATE_SEAL,
    PromotionBoard,
    StationDirectory,
)


# --------------------------------------------------------------------------- row helpers

def segment_row(
    segment_id: str,
    *,
    station: str,
    match: str,
    index: int,
    a: str,
    b: str,
    jumps: int,
    valid: bool = True,
    revision: int = 1,
) -> dict:
    return {
        "row_id": segment_id,
        "revision": revision,
        "values": {
            "station": station,
            "match": match,
            "segment_index": index,
            "jumper_a": a,
            "jumper_b": b,
            "jumps": jumps,
            "valid": 1 if valid else 0,
        },
    }


def corrected_row(row: dict, *, revision: int, **changes: Any) -> dict:
    """A higher-revision ruling on the same segment (e.g. who was on the rope)."""
    new_row = deepcopy(row)
    new_row["revision"] = revision
    new_row["values"].update(changes)
    return new_row


# --------------------------------------------------------------------------- replay

def build_directory(stations: list[str], actions: list[dict]) -> StationDirectory:
    directory = StationDirectory(stations)
    for action in actions:
        if action["kind"] != "report":
            continue
        receipt = directory.feed(action["station"]).ingest_report(
            action["rows"], file_id=action["file_id"]
        )
        if not receipt.accepted:
            raise AssertionError(
                f"report {action['file_id']!r} rejected: {receipt.errors}"
            )
    return directory


def run(
    athletes: list[str],
    seats: int,
    stations: list[str],
    actions: list[dict],
    *,
    mechanism: str,
) -> dict:
    """Replay a full history on fresh state; return board, answers and events.

    Report actions are ingested *in temporal order* as the history unfolds, so
    a higher-revision ruling correction changes the effective rows at exactly
    the point the history says it arrived.
    """
    directory = StationDirectory(stations)
    board = PromotionBoard(athletes, seats, directory, mechanism=mechanism)
    answers: list[dict] = []
    for action in actions:
        kind = action["kind"]
        if kind == "report":
            receipt = directory.feed(action["station"]).ingest_report(
                action["rows"], file_id=action["file_id"]
            )
            answers.append({"action": action, "receipt": receipt.to_json()})
            if not receipt.accepted:
                raise AssertionError(
                    f"report {action['file_id']!r} rejected: {receipt.errors}"
                )
            continue
        if kind == "declare":
            receipt = board.declare(
                action["applicant"],
                action["target"],
                declared_at=action["at"],
                expires_at=action["expires_at"],
            )
            answers.append({"action": action, "receipt": receipt.to_json()})
        elif kind == "lock":
            receipt = board.apply_lock(
                action["applicant"], action["target"], when=action["at"]
            )
            answers.append({"action": action, "receipt": receipt.to_json()})
        elif kind == "withdraw_intention":
            receipt = board.withdraw_intention(
                action["applicant"], action["target"], when=action["at"]
            )
            answers.append({"action": action, "receipt": receipt.to_json()})
        elif kind == "withdraw_athlete":
            answers.append(
                {"action": action, "result": board.withdraw_athlete(action["athlete"], when=action["at"])}
            )
        elif kind == "seal":
            answers.append({"action": action, "result": board.seal(when=action["at"]).to_json()})
        elif kind == "expire":
            answers.append(
                {"action": action, "expired": board.expire(action["at"])}
            )
        elif kind == "ruling":
            answers.append(
                {"action": action, "result": board.apply_ruling_effects(when=action["at"])}
            )
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unknown action kind {kind!r}")
    return {
        "board": board,
        "directory": directory,
        "answers": answers,
        "segments": [s.to_json() for s in directory.all_segments()],
        "qualifications": {
            "+".join(pair): q.to_json()
            for pair, q in sorted(board.qualifications().items())
        },
    }


# ===========================================================================
# Scenario 1 — two athletes, one seat, one station correction (both mechanisms).
# ===========================================================================

EAST = "east"
WEST = "west"
JIA = "甲"
YI = "乙"
BING = "丙"


def two_person_reports() -> list[dict]:
    """East: 甲+乙 jump 5 in one match.  West: initially 5, then corrected to 4."""
    east_rows = [
        segment_row("E-1", station=EAST, match="E-M1", index=1, a=JIA, b=YI, jumps=3),
        segment_row("E-2", station=EAST, match="E-M1", index=2, a=JIA, b=YI, jumps=2),
    ]
    west_rows = [
        # 4 + 1 = 5; when the last segment is re-attributed to 甲+丙 the
        # pair is left with exactly 4 counted jumps at the West station.
        segment_row("W-1", station=WEST, match="W-M1", index=1, a=JIA, b=YI, jumps=4),
        segment_row("W-2", station=WEST, match="W-M1", index=2, a=JIA, b=YI, jumps=1),
    ]
    return [
        {"kind": "report", "station": EAST, "file_id": "east-r1", "rows": east_rows},
        {"kind": "report", "station": WEST, "file_id": "west-r1", "rows": west_rows},
    ]


def west_correction_to_jia_bing() -> dict:
    """Ruling correction: the last West segment was actually 甲+丙, not 甲+乙."""
    original = segment_row(
        "W-2", station=WEST, match="W-M1", index=2, a=JIA, b=YI, jumps=1
    )
    corrected = corrected_row(original, revision=2, jumper_a=JIA, jumper_b=BING)
    return {
        "kind": "report",
        "station": WEST,
        "file_id": "west-r2-correction",
        "rows": [corrected],
    }


def west_restoration() -> dict:
    """The result is restored: W-2 is 甲+乙 again, at ruling revision 3."""
    restored = segment_row(
        "W-2",
        station=WEST,
        match="W-M1",
        index=2,
        a=JIA,
        b=YI,
        jumps=1,
        revision=3,
    )
    return {
        "kind": "report",
        "station": WEST,
        "file_id": "west-r3-restore",
        "rows": [restored],
    }


def two_person_early_lock_history() -> list[dict]:
    """Phase A promotion -> correction revokes -> restore -> 乙 withdraws, still out."""
    return [
        *two_person_reports(),
        {"kind": "declare", "applicant": JIA, "target": YI, "at": 1, "expires_at": 5},
        {"kind": "declare", "applicant": YI, "target": JIA, "at": 1, "expires_at": 5},
        {"kind": "lock", "applicant": JIA, "target": YI, "at": 1},
        west_correction_to_jia_bing() | {"at": 2},
        {"kind": "ruling", "at": 2},
        west_restoration() | {"at": 3},
        {"kind": "ruling", "at": 3},
        # Restore the score, but only 乙 withdraws consent for the pair:
        # eligibility alone can never re-promote a non-mutual pair.
        {"kind": "withdraw_intention", "applicant": YI, "target": JIA, "at": 4},
        {"kind": "lock", "applicant": JIA, "target": YI, "at": 4},
    ]


# ===========================================================================
# Scenario 2 — four athletes, two seats, late seal, three originally-eligible pairs.
# ===========================================================================

DING = "丁"


def four_person_reports() -> list[dict]:
    """All three pairs originally jump 5 at each of East and West.

    Only the 甲+乙 West evidence is ruling-correctable in the replays.
    """
    rows_east = [
        segment_row("E-AB-1", station=EAST, match="E-AB", index=1, a=JIA, b=YI, jumps=5),
        segment_row("E-AC-1", station=EAST, match="E-AC", index=1, a=JIA, b=BING, jumps=5),
        segment_row("E-BD-1", station=EAST, match="E-BD", index=1, a=YI, b=DING, jumps=5),
    ]
    rows_west = [
        segment_row("W-AB-1", station=WEST, match="W-AB", index=1, a=JIA, b=YI, jumps=3),
        segment_row("W-AB-2", station=WEST, match="W-AB", index=2, a=JIA, b=YI, jumps=2),
        segment_row("W-AC-1", station=WEST, match="W-AC", index=1, a=JIA, b=BING, jumps=5),
        segment_row("W-BD-1", station=WEST, match="W-BD", index=1, a=YI, b=DING, jumps=5),
    ]
    return [
        {"kind": "report", "station": EAST, "file_id": "east-r1", "rows": rows_east},
        {"kind": "report", "station": WEST, "file_id": "west-r1", "rows": rows_west},
    ]


def west_ab_correction(revision: int = 2) -> dict:
    """Re-decide only 甲+乙's West evidence: last segment becomes 甲+丙 (4 left)."""
    original = segment_row(
        "W-AB-2", station=WEST, match="W-AB", index=2, a=JIA, b=YI, jumps=2
    )
    corrected = corrected_row(original, revision=revision, jumper_a=JIA, jumper_b=BING)
    return {
        "kind": "report",
        "station": WEST,
        "file_id": "west-r2-ab-correction",
        "rows": [corrected],
        "at": 0,
    }


# Shared pre-seal record: reports, then day-by-day declarations.  Intentions
# stay usable through the day-3 seal and expire immediately afterwards.
def four_person_preseal_actions() -> list[dict]:
    return [
        *four_person_reports(),
        # Day 1: the first application is the doubly-signed 甲+乙 request.
        {"kind": "declare", "applicant": JIA, "target": YI, "at": 1, "expires_at": 3},
        {"kind": "declare", "applicant": YI, "target": JIA, "at": 1, "expires_at": 3},
        # Day 2: 甲+丙 and 乙+丁 arrive.
        {"kind": "declare", "applicant": JIA, "target": BING, "at": 2, "expires_at": 3},
        {"kind": "declare", "applicant": BING, "target": JIA, "at": 2, "expires_at": 3},
        {"kind": "declare", "applicant": YI, "target": DING, "at": 2, "expires_at": 3},
        {"kind": "declare", "applicant": DING, "target": YI, "at": 2, "expires_at": 3},
    ]


def four_person_baseline_actions() -> list[dict]:
    """Seal on day 3; intentions expire after it; no correction."""
    return [
        *four_person_preseal_actions(),
        {"kind": "seal", "at": 3},
        {"kind": "expire", "at": 4},
    ]


def four_person_replay_preseal_correction() -> list[dict]:
    """Correction lands BEFORE the seal; seal still happens at day 3."""
    correction = west_ab_correction()
    correction["at"] = 2
    return [
        *four_person_preseal_actions(),
        correction,
        {"kind": "ruling", "at": 2},
        {"kind": "seal", "at": 3},
        {"kind": "expire", "at": 4},
    ]


def four_person_replay_post_expiry_correction() -> list[dict]:
    """Same correction lands only AFTER intentions expired; no back-signing."""
    correction = west_ab_correction()
    correction["at"] = 5
    return [
        *four_person_preseal_actions(),
        {"kind": "seal", "at": 3},
        {"kind": "expire", "at": 4},
        correction,
        {"kind": "ruling", "at": 5},
    ]


FOUR_ATHLETES = [JIA, YI, BING, DING]
TWO_STATIONS = [EAST, WEST]


def run_late_seal_baseline() -> dict:
    return run(
        FOUR_ATHLETES, 2, TWO_STATIONS, four_person_baseline_actions(),
        mechanism=MECHANISM_LATE_SEAL,
    )


def run_late_seal_preseal_replay() -> dict:
    return run(
        FOUR_ATHLETES, 2, TWO_STATIONS,
        four_person_replay_preseal_correction(),
        mechanism=MECHANISM_LATE_SEAL,
    )


def run_late_seal_post_expiry_replay() -> dict:
    return run(
        FOUR_ATHLETES, 2, TWO_STATIONS,
        four_person_replay_post_expiry_correction(),
        mechanism=MECHANISM_LATE_SEAL,
    )


def run_two_person_early_lock() -> dict:
    return run(
        [JIA, YI, BING], 1, TWO_STATIONS, two_person_early_lock_history(),
        mechanism=MECHANISM_EARLY_LOCK,
    )


# An extra early-lock illustration of the tradeoff and the late-withdrawal trap.
def early_lock_tradeoff_history() -> list[dict]:
    return [
        *four_person_reports(),
        # Day 1: 甲+乙 double-sign and lock a seat immediately.
        {"kind": "declare", "applicant": JIA, "target": YI, "at": 1, "expires_at": 6},
        {"kind": "declare", "applicant": YI, "target": JIA, "at": 1, "expires_at": 6},
        {"kind": "lock", "applicant": JIA, "target": YI, "at": 1},
        # Day 2: 甲 would have preferred 丙, but 甲 is already locked.
        {"kind": "declare", "applicant": JIA, "target": BING, "at": 2, "expires_at": 6},
        {"kind": "declare", "applicant": BING, "target": JIA, "at": 2, "expires_at": 6},
        {"kind": "lock", "applicant": JIA, "target": BING, "at": 2},
        # Day 5: a ruling disqualifies 甲+乙; the seat is released.  甲+丙's
        # declarations are still valid, so an explicit lock request takes the
        # released seat straight away.
        west_ab_correction(revision=2) | {"at": 5},
        {"kind": "ruling", "at": 5},
        {"kind": "lock", "applicant": JIA, "target": BING, "at": 5},
    ]


def run_early_lock_tradeoff() -> dict:
    return run(
        FOUR_ATHLETES, 2, TWO_STATIONS, early_lock_tradeoff_history(),
        mechanism=MECHANISM_EARLY_LOCK,
    )


def early_lock_late_withdrawal_history() -> list[dict]:
    """The late-withdrawal trap: by the time the seat is freed, no partner remains."""
    return [
        *four_person_reports(),
        # Day 1: 甲+乙 lock; 甲+丙 and 乙+丁 also declare but lose the race.
        {"kind": "declare", "applicant": JIA, "target": YI, "at": 1, "expires_at": 3},
        {"kind": "declare", "applicant": YI, "target": JIA, "at": 1, "expires_at": 3},
        {"kind": "lock", "applicant": JIA, "target": YI, "at": 1},
        {"kind": "declare", "applicant": JIA, "target": BING, "at": 1, "expires_at": 3},
        {"kind": "declare", "applicant": BING, "target": JIA, "at": 1, "expires_at": 3},
        # Day 4: the would-be partner's declarations have already expired.
        {"kind": "expire", "at": 4},
        # Day 6: a ruling disqualifies 甲+乙 and releases the seat - nobody's
        # intentions are still valid, so the seat cannot be re-offered.
        west_ab_correction(revision=2) | {"at": 6},
        {"kind": "ruling", "at": 6},
        {"kind": "lock", "applicant": JIA, "target": BING, "at": 6},
    ]


def run_early_lock_late_withdrawal() -> dict:
    return run(
        FOUR_ATHLETES, 2, TWO_STATIONS, early_lock_late_withdrawal_history(),
        mechanism=MECHANISM_EARLY_LOCK,
    )
