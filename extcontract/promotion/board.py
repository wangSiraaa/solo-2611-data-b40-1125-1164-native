"""The promotion desk: mutual consent, seats, credentials, and one mechanism.

A board is constructed for **exactly one** mechanism and refuses the other
mechanism's verbs, so an installation cannot mix the two:

* :data:`MECHANISM_EARLY_LOCK` — a doubly-signed, already-eligible pair locks
  both athletes and one seat on the spot.  A later ruling that disqualifies one
  of their stations, or a formal athlete withdrawal, revokes the credential and
  releases the seat; the released seat is re-offered only to intentions that
  are still valid then (no back-signing).
* :data:`MECHANISM_LATE_SEAL` (the operated mechanism) — declarations are
  time-boxed intentions and nobody holds a formal seat before the seal.  At
  the deadline the desk runs a maximum-cardinality matching over the
  still-usable, still-qualified intentions and issues that many credentials.

State is append-only in the places the brief requires it:

* every intention keeps its application number, so a signed or declined
  receipt is retrievable by that number forever;
* revoked credentials are never deleted — the issued version, the revocation
  time, and the reason are retained in the credential history.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from . import (
    CREDENTIAL_ISSUED,
    CREDENTIAL_REVOKED,
    INTENTION_DECLINED,
    INTENTION_EXPIRED,
    INTENTION_HELD,
    INTENTION_PAIRED,
    INTENTION_WITHDRAWN,
    MAX_ATHLETES,
    MECHANISM_EARLY_LOCK,
    MECHANISM_LATE_SEAL,
    REASON_LOCKED,
    REASON_NO_SEAT,
    REASON_NOT_MUTUAL,
    REASON_NOT_QUALIFIED,
    REASON_SEALED,
    REASON_UNKNOWN_ATHLETE,
    REVOKE_ATHLETE_WITHDREW,
    REVOKE_RULING_DISQUALIFIED,
    Credential,
    Intention,
    PairQualification,
    pair_key,
    pair_label,
)
from .feed import StationDirectory
from .qualification import evaluate_pair


@dataclass
class ApplicationReceipt:
    """The answer an applicant can use on the day they apply."""

    application_no: str
    pair: tuple[str, str]
    submitted_at: int
    accepted: bool
    status: str
    formal_seat: bool
    reason: str | None
    credential_id: str | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "application_no": self.application_no,
            "pair": list(self.pair),
            "label": pair_label(self.pair),
            "submitted_at": self.submitted_at,
            "accepted": self.accepted,
            "status": self.status,
            "formal_seat": self.formal_seat,
            "reason": self.reason,
            "credential_id": self.credential_id,
            "detail": self.detail,
        }


@dataclass
class SealResult:
    sealed_at: int
    matched: list[Credential]
    declined_pairs: list[tuple[str, str]]
    seats_used: int
    seats_remaining: int

    def to_json(self) -> dict[str, Any]:
        return {
            "sealed_at": self.sealed_at,
            "matched": [c.to_json() for c in self.matched],
            "declined_pairs": [list(p) for p in self.declined_pairs],
            "seats_used": self.seats_used,
            "seats_remaining": self.seats_remaining,
        }


@dataclass
class BoardSnapshot:
    """Deep, restorable point-in-time board state (for sealed-before replays)."""

    mechanism: str
    athletes: tuple[str, ...]
    total_seats: int
    sealed: bool
    sealed_at: int | None
    seq: int
    intentions: dict[str, Intention]
    credentials: dict[str, Credential]
    journal: list[dict]
    next_application: int
    next_credential: int


class PromotionBoard:
    """Stateful promotion desk bound to one station directory and one mechanism."""

    def __init__(
        self,
        athletes: list[str],
        seats: int,
        directory: StationDirectory,
        *,
        mechanism: str = MECHANISM_LATE_SEAL,
    ):
        if mechanism not in (MECHANISM_EARLY_LOCK, MECHANISM_LATE_SEAL):
            raise ValueError(f"unknown mechanism {mechanism!r}")
        if not athletes or len(athletes) > MAX_ATHLETES:
            raise ValueError(f"between 1 and {MAX_ATHLETES} athletes are supported")
        if len(set(athletes)) != len(athletes):
            raise ValueError("duplicate athlete ids")
        if not isinstance(seats, int) or seats < 1:
            raise ValueError("seats must be a positive integer")
        self.mechanism = mechanism
        self.athletes = tuple(sorted(athletes))
        self.total_seats = seats
        self.directory = directory
        self.sealed = False
        self.sealed_at: int | None = None
        self._intentions: dict[str, Intention] = {}
        self._credentials: dict[str, Credential] = {}
        self._journal: list[dict] = []
        self._seq = 0
        self._next_application = 0
        self._next_credential = 0

    # ------------------------------------------------------------------ utils

    def _record(self, when: int, event_type: str, detail: dict) -> dict:
        self._seq += 1
        event = {"seq": self._seq, "at": when, "event": event_type, **detail}
        self._journal.append(event)
        return event

    def _check_athlete(self, athlete: str) -> str | None:
        if athlete not in self.athletes:
            return REASON_UNKNOWN_ATHLETE
        return None

    def _segments(self) -> list:
        return self.directory.all_segments()

    def qualification_for(self, pair: tuple[str, str]) -> PairQualification:
        """Recompute the pair's verdict from the currently effective rows."""
        return evaluate_pair(pair_key(*pair), self._segments())

    def qualifications(self) -> dict[tuple[str, str], PairQualification]:
        from .qualification import evaluate_all

        return evaluate_all(self._segments())

    # ------------------------------------------------------------- intentions

    def declare(
        self,
        applicant: str,
        target: str,
        *,
        declared_at: int,
        expires_at: int,
    ) -> ApplicationReceipt:
        """Register one directed, time-boxed mutual-consent declaration."""
        pair = pair_key(applicant, target)
        unknown = self._check_athlete(applicant) or self._check_athlete(target)
        if unknown is not None:
            return ApplicationReceipt(
                application_no="",
                pair=pair,
                submitted_at=declared_at,
                accepted=False,
                status="REJECTED",
                formal_seat=False,
                reason=unknown,
            )
        if applicant == target:
            return ApplicationReceipt(
                application_no="",
                pair=pair,
                submitted_at=declared_at,
                accepted=False,
                status="REJECTED",
                formal_seat=False,
                reason="SELF_DECLARATION",
            )
        if not isinstance(expires_at, int) or expires_at < declared_at:
            return ApplicationReceipt(
                application_no="",
                pair=pair,
                submitted_at=declared_at,
                accepted=False,
                status="REJECTED",
                formal_seat=False,
                reason="INVALID_VALIDITY_WINDOW",
            )
        if self.mechanism == MECHANISM_LATE_SEAL and self.sealed:
            return ApplicationReceipt(
                application_no="",
                pair=pair,
                submitted_at=declared_at,
                accepted=False,
                status="REJECTED",
                formal_seat=False,
                reason=REASON_SEALED,
            )
        # Idempotent re-declaration: the same directed declaration already on
        # file returns its original receipt rather than minting a new number.
        for intention in self._intentions.values():
            if (
                intention.applicant == applicant
                and intention.target == target
                and intention.status in (INTENTION_HELD, INTENTION_PAIRED)
            ):
                return self._receipt_from(intention)

        self._next_application += 1
        application_no = f"APP-{self._next_application:03d}"
        intention = Intention(
            application_no=application_no,
            applicant=applicant,
            target=target,
            declared_at=declared_at,
            expires_at=expires_at,
        )
        self._intentions[application_no] = intention
        self._record(
            declared_at,
            "INTENTION_DECLARED",
            {
                "application_no": application_no,
                "applicant": applicant,
                "target": target,
                "expires_at": expires_at,
            },
        )
        return self._receipt_from(intention)

    def _receipt_from(self, intention: Intention) -> ApplicationReceipt:
        formal = intention.status == INTENTION_PAIRED
        return ApplicationReceipt(
            application_no=intention.application_no,
            pair=intention.pair,
            submitted_at=intention.declared_at,
            accepted=True,
            status=intention.status,
            formal_seat=formal,
            reason=None if formal or intention.status == INTENTION_HELD else intention.status,
            credential_id=intention.credential_id,
        )

    def receipt_for(self, application_no: str) -> dict | None:
        """Look up a signed or declined receipt by its original application no."""
        intention = self._intentions.get(application_no)
        if intention is None:
            return None
        receipt = self._receipt_from(intention).to_json()
        receipt["journal"] = [
            event for event in self._journal if event.get("application_no") == application_no
        ]
        return receipt

    def intentions(self) -> list[dict]:
        return [self._intentions[k].to_json() for k in sorted(self._intentions)]

    def _mutual(self, pair: tuple[str, str], when: int) -> list[Intention]:
        """Both directed declarations for a pair, iff both are held and valid."""
        held = [
            i
            for i in self._intentions.values()
            if i.pair == pair and i.valid_at(when)
        ]
        directions = {(i.applicant, i.target) for i in held}
        if len(directions) == 2:
            return sorted(held, key=lambda i: i.application_no)
        return []

    def expire(self, when: int) -> list[str]:
        """Move every out-of-window intention to EXPIRED.

        A DECLINED intention is a terminal answer, but once its validity window
        closes the *reason it can never be back-signed* is expiry: the status
        becomes EXPIRED while the original decline stays in the journal.
        PAIRED/WITHDRAWN intentions are bound elsewhere and do not expire.
        """
        expired = []
        for intention in self._intentions.values():
            if intention.status not in (INTENTION_HELD, INTENTION_DECLINED):
                continue
            if intention.expires_at < when:
                intention.status = INTENTION_EXPIRED
                expired.append(intention.application_no)
                self._record(
                    when,
                    "INTENTION_EXPIRED",
                    {"application_no": intention.application_no, "pair": list(intention.pair)},
                )
        return expired

    # ------------------------------------------------------------- withdrawals

    def withdraw_intention(
        self, applicant: str, target: str, *, when: int
    ) -> ApplicationReceipt:
        """Withdraw one still-held declaration (formal withdrawal, on the record)."""
        pair = pair_key(applicant, target)
        for intention in self._intentions.values():
            if intention.applicant == applicant and intention.target == target:
                if intention.status == INTENTION_PAIRED:
                    # A paired declaration is bound to an active credential;
                    # the chapter withdrawal path is withdraw_athlete().
                    return ApplicationReceipt(
                        application_no=intention.application_no,
                        pair=pair,
                        submitted_at=when,
                        accepted=False,
                        status=INTENTION_PAIRED,
                        formal_seat=True,
                        reason="CREDENTIAL_ACTIVE_USE_FORMAL_WITHDRAWAL",
                        credential_id=intention.credential_id,
                    )
                if intention.status == INTENTION_HELD:
                    intention.status = INTENTION_WITHDRAWN
                    self._record(
                        when,
                        "INTENTION_WITHDRAWN",
                        {
                            "application_no": intention.application_no,
                            "applicant": applicant,
                            "target": target,
                        },
                    )
                return self._receipt_from(intention)
        return ApplicationReceipt(
            application_no="",
            pair=pair,
            submitted_at=when,
            accepted=False,
            status="REJECTED",
            formal_seat=False,
            reason=REASON_UNKNOWN_ATHLETE,
        )

    def withdraw_athlete(self, athlete: str, *, when: int) -> dict:
        """Formal chapter withdrawal: drop intentions, revoke credentials, re-offer."""
        if self._check_athlete(athlete) is not None:
            return {"athlete": athlete, "accepted": False, "reason": REASON_UNKNOWN_ATHLETE}
        for intention in self._intentions.values():
            if athlete in intention.pair and intention.status == INTENTION_HELD:
                intention.status = INTENTION_WITHDRAWN
                self._record(
                    when,
                    "INTENTION_WITHDRAWN",
                    {
                        "application_no": intention.application_no,
                        "pair": list(intention.pair),
                        "via": "athlete_withdrawal",
                    },
                )
        revoked = []
        for credential in self._credentials.values():
            if (
                credential.status == CREDENTIAL_ISSUED
                and athlete in credential.pair
            ):
                self._revoke(
                    credential,
                    when,
                    REVOKE_ATHLETE_WITHDREW,
                    f"athlete {athlete} formally withdrew",
                )
                revoked.append(credential.credential_id)
        backfilled = self._backfill(when)
        self._record(
            when,
            "ATHLETE_WITHDREW",
            {"athlete": athlete, "revoked": revoked, "backfilled": backfilled},
        )
        return {
            "athlete": athlete,
            "accepted": True,
            "revoked_credentials": revoked,
            "backfilled_credentials": backfilled,
        }

    # ------------------------------------------------------------- early locking

    def apply_lock(
        self, applicant: str, target: str, *, when: int
    ) -> ApplicationReceipt:
        """EARLY_LOCK only: a doubly-signed eligible pair locks seat and athletes."""
        pair = pair_key(applicant, target)
        if self.mechanism != MECHANISM_EARLY_LOCK:
            return ApplicationReceipt(
                application_no="",
                pair=pair,
                submitted_at=when,
                accepted=False,
                status="REJECTED",
                formal_seat=False,
                reason="EARLY_LOCK_NOT_OPERATED",
            )
        existing = self._active_credential(pair)
        if existing is not None:
            apps = self._application_nos(pair)
            intention = self._intentions[apps[0]] if apps else None
            receipt = (
                self._receipt_from(intention)
                if intention is not None
                else ApplicationReceipt(
                    application_no="",
                    pair=pair,
                    submitted_at=when,
                    accepted=True,
                    status=CREDENTIAL_ISSUED,
                    formal_seat=True,
                    reason=REASON_LOCKED,
                    credential_id=existing.credential_id,
                )
            )
            receipt.reason = REASON_LOCKED
            receipt.credential_id = existing.credential_id
            return receipt

        unknown = self._check_athlete(applicant) or self._check_athlete(target)
        if unknown is not None:
            return self._rejected(pair, when, unknown)
        mutual = self._mutual(pair, when)
        if not mutual:
            return self._rejected(pair, when, REASON_NOT_MUTUAL)
        qualification = self.qualification_for(pair)
        if not qualification.qualified:
            return self._rejected(
                pair,
                when,
                REASON_NOT_QUALIFIED,
                detail=self._failure_detail(qualification),
            )
        if self._athlete_locked(applicant) or self._athlete_locked(target):
            return self._rejected(pair, when, REASON_LOCKED)
        if self.seats_remaining() <= 0:
            return self._rejected(pair, when, REASON_NO_SEAT)
        credential = self._issue(pair, when, [i.application_no for i in mutual], qualification)
        self._record(
            when,
            "PAIR_LOCKED",
            {
                "pair": list(pair),
                "credential_id": credential.credential_id,
                "application_nos": list(credential.application_nos),
            },
        )
        return self._receipt_from(self._intentions[credential.application_nos[0]])

    # ------------------------------------------------------------- late sealing

    def seal(self, *, when: int) -> SealResult:
        """LATE_SEAL only: maximum matching over the intentions still usable."""
        if self.mechanism != MECHANISM_LATE_SEAL:
            raise ValueError("seal() is only available under LATE_SEAL")
        if self.sealed:
            raise ValueError(f"board already sealed at {self.sealed_at}")
        self.expire(when)
        edges = self._eligible_edges(when)
        chosen = self._maximum_matching(edges)
        chosen = chosen[: self.seats_remaining()]

        matched: list[Credential] = []
        chosen_pairs = set()
        for pair, intentions in chosen:
            qualification = self.qualification_for(pair)
            credential = self._issue(
                pair, when, [i.application_no for i in intentions], qualification
            )
            matched.append(credential)
            chosen_pairs.add(pair)
            self._record(
                when,
                "PAIR_SEALED",
                {
                    "pair": list(pair),
                    "credential_id": credential.credential_id,
                    "application_nos": list(credential.application_nos),
                },
            )

        declined: list[tuple[str, str]] = []
        eligible_pairs = {pair for pair, _ in edges}
        for intention in list(self._intentions.values()):
            if intention.status != INTENTION_HELD or not intention.valid_at(when):
                continue
            pair = intention.pair
            if pair in chosen_pairs:
                continue
            intention.status = INTENTION_DECLINED
            reason = (
                "NOT_IN_MAXIMUM_MATCHING"
                if pair in eligible_pairs
                else "NOT_ELIGIBLE_AT_SEAL"
            )
            declined.append(pair)
            self._record(
                when,
                "INTENTION_DECLINED",
                {
                    "application_no": intention.application_no,
                    "pair": list(pair),
                    "reason": reason,
                },
            )
        self.sealed = True
        self.sealed_at = when
        self._record(
            when,
            "BOARD_SEALED",
            {
                "matched_pairs": [list(c.pair) for c in matched],
                "declined_pairs": [list(p) for p in sorted(set(declined))],
            },
        )
        return SealResult(
            sealed_at=when,
            matched=matched,
            declined_pairs=sorted(set(declined)),
            seats_used=self.seats_used(),
            seats_remaining=self.seats_remaining(),
        )

    def _eligible_edges(
        self, when: int
    ) -> list[tuple[tuple[str, str], list[Intention]]]:
        """Mutual, currently-valid, currently-qualified edges between free athletes."""
        pairs = {i.pair for i in self._intentions.values() if i.valid_at(when)}
        edges: list[tuple[tuple[str, str], list[Intention]]] = []
        for pair in sorted(pairs):
            mutual = self._mutual(pair, when)
            if len(mutual) != 2:
                continue
            if self._athlete_locked(pair[0]) or self._athlete_locked(pair[1]):
                continue
            if not self.qualification_for(pair).qualified:
                continue
            edges.append((pair, mutual))
        # Deterministic edge order: earliest declaration first, then the
        # earliest application number, then pair label.
        edges.sort(
            key=lambda e: (
                min(i.declared_at for i in e[1]),
                min(i.application_no for i in e[1]),
                pair_label(e[0]),
            )
        )
        return edges

    def _maximum_matching(
        self, edges: list[tuple[tuple[str, str], list[Intention]]]
    ) -> list[tuple[tuple[str, str], list[Intention]]]:
        """Maximum-cardinality matching over an undirected athlete graph.

        The consent graph is a *general* undirected graph (three athletes can
        all consent pairwise), so a bipartite Kuhn pass is invalid: an athlete
        can be a left endpoint of one edge and a right endpoint of another and
        end up with two credentials.  The scenario envelope is at most
        :data:`MAX_ATHLETES` athletes, so an exact subset-DP maximum matching is
        both simple and exhaustive.  Among all maximum matchings the
        lexicographically smallest sorted list of pair labels is returned,
        which makes every seal deterministic and replay-stable.
        """
        edge_by_pair = {pair: intentions for pair, intentions in edges}
        candidates = sorted(edge_by_pair, key=pair_label)
        athletes = sorted({athlete for pair in candidates for athlete in pair})
        index = {athlete: i for i, athlete in enumerate(athletes)}
        bit_pairs = [(index[a], index[b], (a, b)) for a, b in candidates]

        best: list[tuple[str, str]] = []
        size = len(athletes)
        full = (1 << size) - 1
        # memo[used_mask] = lexicographically smallest maximal pair list that
        # can still be built from the un-used athletes.
        memo: dict[int, list[tuple[str, str]]] = {}

        def solve(mask: int) -> list[tuple[str, str]]:
            if mask == full:
                return []
            if mask in memo:
                return memo[mask]
            chosen: list[tuple[str, str]] = []
            for left, right, pair in bit_pairs:
                bit_l, bit_r = 1 << left, 1 << right
                if mask & bit_l or mask & bit_r:
                    continue
                tail = solve(mask | bit_l | bit_r)
                candidate = sorted([pair, *tail], key=pair_label)
                if not chosen or _matching_preferred(candidate, chosen):
                    chosen = candidate
            memo[mask] = chosen
            return chosen

        best = solve(0)
        return [(pair, edge_by_pair[pair]) for pair in best]

    # ------------------------------------------------- ruling-driven re-decisions
    def apply_ruling_effects(self, *, when: int) -> dict:
        """Re-read every effective segment and reconcile credentials and seats.

        A ruling correction that makes an issued pair fail the two-station rule
        revokes that credential (version and reason retained) and releases its
        seat.  Released seats are then offered only to intentions that are
        valid *now*; declined or expired receipts can never be back-signed.
        """
        revoked: list[dict] = []
        for credential in list(self._credentials.values()):
            if credential.status != CREDENTIAL_ISSUED:
                continue
            qualification = self.qualification_for(credential.pair)
            if qualification.qualified:
                continue
            self._revoke(
                credential,
                when,
                REVOKE_RULING_DISQUALIFIED,
                self._failure_detail(qualification),
            )
            revoked.append(
                {
                    "credential_id": credential.credential_id,
                    "pair": list(credential.pair),
                    "detail": credential.revoke_detail,
                }
            )
        backfilled = self._backfill(when)
        self._record(
            when,
            "RULING_RECONCILED",
            {"revoked": revoked, "backfilled": backfilled},
        )
        return {"revoked": revoked, "backfilled": backfilled}

    def _backfill(self, when: int) -> list[str]:
        """Fill open seats from intentions valid at ``when`` only — never back-sign.

        Under LATE_SEAL this is automatic after the seal (the desk itself holds
        the matching authority).  Under EARLY_LOCK seats are only granted
        through explicit :meth:`apply_lock` requests, so this routine does
        nothing there — a re-lock whose intentions have expired fails at the
        desk instead of being silently revived.
        """
        if self.mechanism == MECHANISM_EARLY_LOCK:
            return []
        if not self.sealed:
            return []
        issued: list[str] = []
        while self.seats_remaining() > 0:
            edges = [
                (pair, intentions)
                for pair, intentions in self._eligible_edges(when)
                if not self._active_credential(pair)
            ]
            if not edges:
                break
            chosen = self._maximum_matching(edges)
            if not chosen:
                break
            pair, intentions = chosen[0]
            qualification = self.qualification_for(pair)
            credential = self._issue(
                pair, when, [i.application_no for i in intentions], qualification
            )
            issued.append(credential.credential_id)
            self._record(
                when,
                "SEAT_BACKFILLED",
                {
                    "pair": list(pair),
                    "credential_id": credential.credential_id,
                    "application_nos": list(credential.application_nos),
                },
            )
        return issued

    # ------------------------------------------------------------- credentials

    def _issue(
        self,
        pair: tuple[str, str],
        when: int,
        application_nos: list[str],
        qualification: PairQualification,
    ) -> Credential:
        self._next_credential += 1
        seat_index = self.seats_used()
        credential = Credential(
            credential_id=f"CRED-{self._next_credential:03d}",
            version=1,
            pair=pair,
            seat_index=seat_index,
            issued_at=when,
            application_nos=sorted(application_nos),
            qualification=qualification,
        )
        self._credentials[credential.credential_id] = credential
        for application_no in application_nos:
            intention = self._intentions[application_no]
            intention.status = INTENTION_PAIRED
            intention.credential_id = credential.credential_id
        return credential

    def _revoke(
        self, credential: Credential, when: int, reason: str, detail: str
    ) -> None:
        credential.status = CREDENTIAL_REVOKED
        credential.revoked_at = when
        credential.revoke_reason = reason
        credential.revoke_detail = detail
        for application_no in credential.application_nos:
            intention = self._intentions.get(application_no)
            if intention is not None and intention.credential_id == credential.credential_id:
                intention.credential_id = None
                intention.status = INTENTION_HELD if reason == REVOKE_RULING_DISQUALIFIED else INTENTION_WITHDRAWN
        self._record(
            when,
            "CREDENTIAL_REVOKED",
            {
                "credential_id": credential.credential_id,
                "pair": list(credential.pair),
                "reason": reason,
                "detail": detail,
            },
        )

    def _active_credential(self, pair: tuple[str, str]) -> Credential | None:
        for credential in self._credentials.values():
            if credential.status == CREDENTIAL_ISSUED and credential.pair == pair:
                return credential
        return None

    def _athlete_locked(self, athlete: str) -> bool:
        return any(
            c.status == CREDENTIAL_ISSUED and athlete in c.pair
            for c in self._credentials.values()
        )

    def _application_nos(self, pair: tuple[str, str]) -> list[str]:
        return sorted(
            i.application_no
            for i in self._intentions.values()
            if i.pair == pair
        )

    @staticmethod
    def _failure_detail(qualification: PairQualification) -> str:
        missing = [
            f"{station}={best.jumps}"
            for station, best in sorted(qualification.stations.items())
        ]
        counted = ", ".join(missing) if missing else "no shared valid segments"
        return (
            f"pair {pair_label(qualification.pair)} reaches 5 counted jumps at "
            f"{len(qualification.qualifying_stations)} station(s) "
            f"({counted}); needs 2 different stations"
        )

    @staticmethod
    def _rejected(
        pair: tuple[str, str],
        when: int,
        reason: str,
        *,
        detail: str | None = None,
    ) -> ApplicationReceipt:
        return ApplicationReceipt(
            application_no="",
            pair=pair,
            submitted_at=when,
            accepted=False,
            status="REJECTED",
            formal_seat=False,
            reason=reason,
            detail=detail,
        )

    # ------------------------------------------------------------- views

    def seats_used(self) -> int:
        return sum(
            1 for c in self._credentials.values() if c.status == CREDENTIAL_ISSUED
        )

    def seats_remaining(self) -> int:
        return self.total_seats - self.seats_used()

    def active_credentials(self) -> list[dict]:
        return [
            c.to_json()
            for c in sorted(
                (c for c in self._credentials.values() if c.status == CREDENTIAL_ISSUED),
                key=lambda c: c.credential_id,
            )
        ]

    def credential_history(self) -> list[dict]:
        """Every credential ever issued, including revoked versions and reasons."""
        return [
            c.to_json()
            for c in sorted(self._credentials.values(), key=lambda c: c.credential_id)
        ]

    def journal(self) -> list[dict]:
        return copy.deepcopy(self._journal)

    # ------------------------------------------------------------- replay

    def snapshot(self) -> BoardSnapshot:
        return BoardSnapshot(
            mechanism=self.mechanism,
            athletes=self.athletes,
            total_seats=self.total_seats,
            sealed=self.sealed,
            sealed_at=self.sealed_at,
            seq=self._seq,
            intentions=copy.deepcopy(self._intentions),
            credentials=copy.deepcopy(self._credentials),
            journal=copy.deepcopy(self._journal),
            next_application=self._next_application,
            next_credential=self._next_credential,
        )

    def restore(self, snapshot: BoardSnapshot) -> None:
        self.mechanism = snapshot.mechanism
        self.athletes = snapshot.athletes
        self.total_seats = snapshot.total_seats
        self.sealed = snapshot.sealed
        self.sealed_at = snapshot.sealed_at
        self._seq = snapshot.seq
        self._intentions = copy.deepcopy(snapshot.intentions)
        self._credentials = copy.deepcopy(snapshot.credentials)
        self._journal = copy.deepcopy(snapshot.journal)
        self._next_application = snapshot.next_application
        self._next_credential = snapshot.next_credential


def _matching_preferred(
    candidate: list[tuple[str, str]], incumbent: list[tuple[str, str]]
) -> bool:
    """True if ``candidate`` beats ``incumbent``: more teams, then smaller labels."""
    if len(candidate) != len(incumbent):
        return len(candidate) > len(incumbent)
    return [pair_label(p) for p in candidate] < [pair_label(p) for p in incumbent]
