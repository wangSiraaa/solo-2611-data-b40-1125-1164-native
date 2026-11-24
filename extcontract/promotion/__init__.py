"""Domain model for the end-of-season two-person jump-rope promotion.

A promotion is decided from three deliberately separated facts, mirroring the
rest of the toolkit:

1. **What happened on the rope** — :class:`~extcontract.promotion.feed.StationFeed`
   owns each station's read/write contract, ingests ruling-revised segment
   reports, and publishes only the currently effective rows.
2. **Who is eligible** — :mod:`extcontract.promotion.qualification` is a pure,
   row-based function of those effective segments: a pair must jump five
   counted jumps at each of two *different* stations, taking the pair's best
   match per station and only the segments where both athletes are actually on
   the rope together.
3. **Who gets a seat** — :class:`~extcontract.promotion.board.PromotionBoard`
   holds the mutual-consent declarations (time-boxed intentions), the remaining
   seats, and the issued credentials, and runs exactly one operated mechanism.

Time is an integer ordinal (day number); all comparisons are exact so the
whole scenario is deterministic and replayable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Documented envelope (the brief caps the scenario at six athletes/four
# stations; the board refuses to grow beyond it rather than silently scaling).
MAX_ATHLETES = 6
MAX_STATIONS = 4

# A pair must complete this many counted jumps at this many distinct stations.
JUMPS_PER_STATION = 5
REQUIRED_STATIONS = 2

# The single mechanism this installation operates.  The board is constructed
# for one mechanism and refuses the other mechanism's verbs, so an operator
# cannot mix early-lock and late-seal behaviour on the same board.
MECHANISM_EARLY_LOCK = "EARLY_LOCK"
MECHANISM_LATE_SEAL = "LATE_SEAL"
OPERATED_MECHANISM = MECHANISM_LATE_SEAL

# Intention / application receipt statuses.
INTENTION_HELD = "HELD"          # accepted, no formal seat yet
INTENTION_PAIRED = "PAIRED"      # a credential was issued against it
INTENTION_DECLINED = "DECLINED"  # considered at a seal and not selected
INTENTION_EXPIRED = "EXPIRED"    # past its valid window, never selected
INTENTION_WITHDRAWN = "WITHDRAWN"

# Credential statuses.  Revocation never deletes a credential: the issued
# version and its revocation reason are retained forever.
CREDENTIAL_ISSUED = "ISSUED"
CREDENTIAL_REVOKED = "REVOKED"

REVOKE_RULING_DISQUALIFIED = "STATION_RULING_DISQUALIFIED"
REVOKE_ATHLETE_WITHDREW = "ATHLETE_FORMALLY_WITHDREW"

# Application / lock rejection reasons returned to the applicant *at the time
# they apply*, so an applicant always gets a usable same-day answer.
REASON_NOT_MUTUAL = "NO_MUTUAL_CONSENT_YET"
REASON_NOT_QUALIFIED = "PAIR_NOT_QUALIFIED"
REASON_NO_SEAT = "NO_SEAT_AVAILABLE"
REASON_SEALED = "BOARD_ALREADY_SEALED"
REASON_LOCKED = "PAIR_ALREADY_LOCKED"
REASON_WITHDRAWN = "APPLICANT_WITHDREW"
REASON_EXPIRED = "INTENTION_EXPIRED"
REASON_UNKNOWN_ATHLETE = "UNKNOWN_ATHLETE"


def pair_key(a: str, b: str) -> tuple[str, str]:
    """Canonical unordered identity of a pair (display order irrelevant)."""
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def pair_label(key: tuple[str, str]) -> str:
    return f"{key[0]}+{key[1]}"


@dataclass(frozen=True)
class Segment:
    """One effective on-rope segment at one station, after ruling revisions."""

    segment_id: str
    station: str
    match: str
    segment_index: int
    jumper_a: str
    jumper_b: str
    jumps: int
    valid: bool
    revision: int

    @property
    def pair(self) -> tuple[str, str]:
        return pair_key(self.jumper_a, self.jumper_b)

    def to_json(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "station": self.station,
            "match": self.match,
            "segment_index": self.segment_index,
            "jumper_a": self.jumper_a,
            "jumper_b": self.jumper_b,
            "jumps": self.jumps,
            "valid": self.valid,
            "revision": self.revision,
        }


@dataclass
class StationBest:
    """The pair's best match at one station: only same-time-on-rope segments."""

    station: str
    match: str
    jumps: int
    segment_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "match": self.match,
            "jumps": self.jumps,
            "segment_ids": list(self.segment_ids),
        }


@dataclass
class PairQualification:
    """Eligibility verdict for one pair, with every station linked to evidence."""

    pair: tuple[str, str]
    qualified: bool
    stations: dict[str, StationBest]
    qualifying_stations: list[str]

    @property
    def label(self) -> str:
        return pair_label(self.pair)

    def to_json(self) -> dict[str, Any]:
        return {
            "pair": list(self.pair),
            "label": self.label,
            "qualified": self.qualified,
            "required_jumps_per_station": JUMPS_PER_STATION,
            "required_stations": REQUIRED_STATIONS,
            "stations": {k: v.to_json() for k, v in sorted(self.stations.items())},
            "qualifying_stations": list(self.qualifying_stations),
        }


@dataclass
class Intention:
    """One directed, time-boxed mutual-consent declaration."""

    application_no: str
    applicant: str
    target: str
    declared_at: int
    expires_at: int
    status: str = INTENTION_HELD
    credential_id: str | None = None

    @property
    def pair(self) -> tuple[str, str]:
        return pair_key(self.applicant, self.target)

    def valid_at(self, when: int) -> bool:
        return (
            self.status == INTENTION_HELD
            and self.declared_at <= when <= self.expires_at
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "application_no": self.application_no,
            "applicant": self.applicant,
            "target": self.target,
            "pair": list(self.pair),
            "declared_at": self.declared_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "credential_id": self.credential_id,
        }


@dataclass
class Credential:
    """A formal seat.  Revocation appends a reason; the issued version stays."""

    credential_id: str
    version: int
    pair: tuple[str, str]
    seat_index: int
    issued_at: int
    application_nos: list[str]
    qualification: PairQualification
    status: str = CREDENTIAL_ISSUED
    revoked_at: int | None = None
    revoke_reason: str | None = None
    revoke_detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "version": self.version,
            "pair": list(self.pair),
            "label": pair_label(self.pair),
            "seat_index": self.seat_index,
            "issued_at": self.issued_at,
            "application_nos": list(self.application_nos),
            "status": self.status,
            "revoked_at": self.revoked_at,
            "revoke_reason": self.revoke_reason,
            "revoke_detail": self.revoke_detail,
            "qualification": self.qualification.to_json(),
        }


# Sub-modules are imported after the domain objects they depend on.
from .feed import (  # noqa: E402
    IngestReceipt,
    StationDirectory,
    StationFeed,
    segment_reader_contract,
    segment_writer_contract,
)
from .qualification import evaluate_all, evaluate_pair, reevaluate  # noqa: E402
from .board import (  # noqa: E402
    ApplicationReceipt,
    BoardSnapshot,
    PromotionBoard,
    SealResult,
)

__all__ = [
    "MAX_ATHLETES",
    "MAX_STATIONS",
    "JUMPS_PER_STATION",
    "REQUIRED_STATIONS",
    "MECHANISM_EARLY_LOCK",
    "MECHANISM_LATE_SEAL",
    "OPERATED_MECHANISM",
    "INTENTION_HELD",
    "INTENTION_PAIRED",
    "INTENTION_DECLINED",
    "INTENTION_EXPIRED",
    "INTENTION_WITHDRAWN",
    "CREDENTIAL_ISSUED",
    "CREDENTIAL_REVOKED",
    "REVOKE_RULING_DISQUALIFIED",
    "REVOKE_ATHLETE_WITHDREW",
    "REASON_NOT_MUTUAL",
    "REASON_NOT_QUALIFIED",
    "REASON_NO_SEAT",
    "REASON_SEALED",
    "REASON_LOCKED",
    "REASON_WITHDRAWN",
    "REASON_EXPIRED",
    "REASON_UNKNOWN_ATHLETE",
    "pair_key",
    "pair_label",
    "Segment",
    "StationBest",
    "PairQualification",
    "Intention",
    "Credential",
    "StationFeed",
    "StationDirectory",
    "IngestReceipt",
    "segment_writer_contract",
    "segment_reader_contract",
    "evaluate_pair",
    "evaluate_all",
    "reevaluate",
    "ApplicationReceipt",
    "SealResult",
    "BoardSnapshot",
    "PromotionBoard",
]
