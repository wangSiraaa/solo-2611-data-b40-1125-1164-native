"""Ornamental-fish boarding ledger and weekend pickup desk.

This module is a domain layer on top of :mod:`solo.revision`.  The old duty
book is a SOLO document whose machine-relevant lines are ASCII.  PAIR segments
may carry marginal annotation glyphs; those spans are decoded and spanned by
:meth:`Version.decode`, but comments are never interpreted as records.

The important epistemic rule is that an ambiguous scoop or split is *not*
resolved by choosing the neatest fish number.  The replayer keeps every
identity history consistent with counts, recognised fish numbers, window
observations and named tail-spot evidence.  The front desk then combines that
identity report with today's extractable compartments, mandates and returned
receipts.

Physical handover is modelled as a sealed opaque transport box.  The desk has
no operation that opens such a box to inspect a tail spot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable, Iterator, Sequence

from .codec import DecodedDocument, encode_text
from .revision import RevisionLibrary, Version, replace

__all__ = [
    "MAX_FISH",
    "MAX_TANKS",
    "MAX_TRANSFERS",
    "FishDeskError",
    "LedgerParseError",
    "LedgerContradiction",
    "DuplicateReceiptFish",
    "MandateError",
    "PickupBlocked",
    "Fish",
    "Loc",
    "LedgerLine",
    "ParsedLedger",
    "parse_ledger",
    "RevisionLedger",
    "FishState",
    "MoveTrace",
    "TrackedWorld",
    "FilterStat",
    "FogGroup",
    "HouseholdRow",
    "IdentityReport",
    "ReplayResult",
    "replay_ledger",
    "LedgerBuilder",
    "SlotUse",
    "NetRack",
    "slot_report",
    "Receipt",
    "ReceiptBook",
    "Mandate",
    "MandateBook",
    "PickupCase",
    "PickupDecision",
    "OpaqueTransportBox",
    "Handover",
    "FrontDesk",
    "FishBoarding",
    "BoardingService",
    "receipt_fish_conflicts",
    "FishLedger",
    "BoardingDesk",
    "FishDesk",
    "canonical_ledger_text",
    "slot_pressure_ledger_text",
]

MAX_FISH = 4
MAX_TANKS = 3
MAX_TRANSFERS = 6

TRANSFER_TAGS = frozenset({"MOVE", "MERGE", "SCOOP", "SPLIT", "SHIFTNET"})


# ---------------------------------------------------------------------------
# Errors and basic value types


class FishDeskError(Exception):
    """Base class for boarding-ledger and pickup-desk failures."""


class LedgerParseError(FishDeskError):
    """A duty-book line does not follow the ASCII ledger grammar."""


class DuplicateReceiptFish(FishDeskError):
    """Two receipts attempt to point at the same original fish."""


class MandateError(FishDeskError):
    """A mandate is unknown, revoked, or outside its authorised target."""


class PickupBlocked(FishDeskError):
    """Raised by :meth:`FrontDesk.settle`; the decision explains the hold."""

    def __init__(self, decision: "PickupDecision") -> None:
        self.decision = decision
        super().__init__(f"{decision.case_id}: {decision.reason_code} {decision.reason}")


@dataclass(frozen=True)
class Fish:
    fish_id: str
    owner: str
    spot: str


@dataclass(frozen=True)
class Loc:
    """A fish's current tank and optional extractable net/box compartment."""

    tank: str
    unit: str | None = None

    @property
    def lot(self) -> str:
        """Stable hashable key: tank plus NUL plus unit (empty if open)."""
        return f"{self.tank}\0{self.unit or ''}"

    @property
    def label(self) -> str:
        return self.unit if self.unit is not None else f"OPEN:{self.tank}"

    def plain_label(self) -> str:
        """A tank alias used when a tank has no separate compartments."""
        return self.tank if self.unit is None else (self.unit or "")


# ---------------------------------------------------------------------------
# Parsed SOLO ledger


@dataclass(frozen=True)
class LedgerLine:
    index: int
    tag: str
    text: str
    start_char: int
    end_char: int
    start_byte: int
    end_byte: int
    data: tuple[tuple[str, object], ...] = ()

    def get(self, key: str, default: object = None) -> object:
        for k, v in self.data:
            if k == key:
                return v
        return default

    def require(self, key: str) -> object:
        value = self.get(key)
        if value is None:
            raise LedgerParseError(f"line {self.index} missing {key}: {self.text!r}")
        return value


def _line_iter(text: str) -> Iterator[tuple[int, int, int, str]]:
    """Yield (line_index, start_char, content_end_char, content)."""
    start = 0
    index = 0
    for end, ch in enumerate(text):
        if ch == "\n":
            yield index, start, end, text[start:end]
            start = end + 1
            index += 1
    if start < len(text):
        yield index, start, len(text), text[start:]


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _parse_scope(token: str) -> tuple[str, str]:
    if token.startswith("OPEN:"):
        tank = token.split(":", 1)[1]
        if not tank:
            raise LedgerParseError(f"bad open-water scope {token!r}")
        return "open", tank
    if token.startswith("NET:"):
        net = token.split(":", 1)[1]
        if not net:
            raise LedgerParseError(f"bad net scope {token!r}")
        return "net", net
    return "tank", token


def _parse_at_time(parts: list[str], line: str) -> tuple[str, int]:
    try:
        at = parts.index("AT")
    except ValueError:
        raise LedgerParseError(f"missing AT time: {line!r}") from None
    if at + 1 >= len(parts):
        raise LedgerParseError(f"missing time after AT: {line!r}")
    return parts[at + 1], at


def _parse_int(value: str, line: str, what: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise LedgerParseError(f"{what} must be an integer: {line!r}") from None


@dataclass(frozen=True)
class ParsedLedger:
    ledger_id: str
    text: str
    lines: tuple[LedgerLine, ...]
    fish: tuple[Fish, ...]
    version_id: int | None = None
    body_length: int = 0

    @property
    def fish_ids(self) -> tuple[str, ...]:
        return tuple(f.fish_id for f in self.fish)

    def fish_by_id(self) -> dict[str, Fish]:
        return {f.fish_id: f for f in self.fish}

    def line_by_tag(self, tag: str) -> tuple[LedgerLine, ...]:
        return tuple(line for line in self.lines if line.tag == tag)

    def find_line_containing(self, needle: str) -> LedgerLine:
        matches = [line for line in self.lines if needle in line.text]
        if len(matches) != 1:
            raise FishDeskError(
                f"expected exactly one ledger line containing {needle!r}, found {len(matches)}"
            )
        return matches[0]


def parse_ledger(
    text: str,
    doc: DecodedDocument | None = None,
    *,
    version_id: int | None = None,
    body_length: int = 0,
) -> ParsedLedger:
    """Parse ASCII ledger lines and attach decoded char/body spans.

    When ``doc`` is supplied by :meth:`Version.decode`, every line carries the
    exact character interval and original-body byte interval needed to build a
    :class:`~solo.revision.TextPatch`.
    """
    starts = doc.starts if doc is not None else None
    ends = doc.ends if doc is not None else None
    if doc is not None and (starts is None or ends is None):
        raise LedgerParseError("decoded document does not contain character ranges")

    lines: list[LedgerLine] = []
    fish: list[Fish] = []
    fish_seen: set[str] = set()
    ledger_id: str | None = None
    tanks: set[str] = set()
    transfer_count = 0

    for index, start, end, raw_line in _line_iter(text):
        line = raw_line.strip()
        if not line:
            continue

        if starts is not None and ends is not None:
            start_byte = int(starts[start])
            end_byte = int(ends[end - 1])
        else:
            start_byte = -1
            end_byte = -1

        if line.startswith("#"):
            # Comments remain addressable so an erratum can later restore or
            # replace the annotation through RevisionLibrary.
            lines.append(
                LedgerLine(
                    index=index,
                    tag="COMMENT",
                    text=raw_line,
                    start_char=start,
                    end_char=end,
                    start_byte=start_byte,
                    end_byte=end_byte,
                    data=(("comment", line),),
                )
            )
            continue

        parts = line.split()
        tag = parts[0]
        data: dict[str, object] = {}

        def need_len(n: int) -> None:
            if len(parts) < n:
                raise LedgerParseError(f"bad {tag} line: {raw_line!r}")

        if tag == "LEDGER":
            need_len(2)
            ledger_id = parts[1]
            data["ledger_id"] = ledger_id

        elif tag == "TANK":
            # TANK <tank> CAP <n>
            need_len(4)
            if parts[2] != "CAP":
                raise LedgerParseError(f"bad TANK line: {raw_line!r}")
            cap = _parse_int(parts[3], raw_line, "CAP")
            if cap < 0:
                raise LedgerParseError("tank capacity must be non-negative")
            tanks.add(parts[1])
            data.update(tank=parts[1], capacity=cap)

        elif tag == "NET":
            # NET <net> TANK <tank> [OWNER <owner>]
            need_len(4)
            if parts[2] != "TANK":
                raise LedgerParseError(f"bad NET line: {raw_line!r}")
            tanks.add(parts[3])
            data.update(net=parts[1], tank=parts[3])
            if "OWNER" in parts:
                oi = parts.index("OWNER")
                if oi + 1 >= len(parts):
                    raise LedgerParseError(f"missing OWNER value: {raw_line!r}")
                data["owner"] = parts[oi + 1]

        elif tag == "FISH":
            # FISH <id> OWNER <owner> SPOT <spot> TANK <tank> [NET <net>] AT <time>
            need_len(9)
            fid = parts[1]
            try:
                owner_i = parts.index("OWNER")
                spot_i = parts.index("SPOT")
                tank_i = parts.index("TANK")
            except ValueError:
                raise LedgerParseError(f"bad FISH line: {raw_line!r}") from None
            owner = parts[owner_i + 1]
            spot = parts[spot_i + 1]
            tank = parts[tank_i + 1]
            net = None
            if "NET" in parts:
                net_i = parts.index("NET")
                net = parts[net_i + 1]
            at_time, at_i = _parse_at_time(parts, raw_line)
            if fid in fish_seen:
                raise LedgerParseError(f"duplicate fish id {fid}")
            fish_seen.add(fid)
            fish.append(Fish(fid, owner, spot))
            tanks.add(tank)
            data.update(
                fish_id=fid,
                owner=owner,
                spot=spot,
                tank=tank,
                net=net,
                time=at_time,
                at_index=at_i,
            )

        elif tag == "MOVE":
            # MOVE <ids> FROM <src> TO <dst> AT <time> [ID <event>]
            need_len(6)
            ids = _split_csv(parts[1])
            if parts[2] != "FROM" or "TO" not in parts:
                raise LedgerParseError(f"bad MOVE line: {raw_line!r}")
            from_i = 2
            to_i = parts.index("TO")
            src = parts[from_i + 1]
            dst = parts[to_i + 1]
            at_time, _ = _parse_at_time(parts, raw_line)
            eid = _optional_value(parts, "ID", f"L{index}")
            scope_kind, scope_id = _parse_scope(src)
            tanks.add(dst)
            if scope_kind == "tank":
                tanks.add(scope_id)
            data.update(
                event_id=eid,
                fish_ids=ids,
                source=src,
                source_kind=scope_kind,
                source_id=scope_id,
                dest=dst,
                time=at_time,
            )
            transfer_count += 1

        elif tag == "MERGE":
            # MERGE <src1,src2,...> INTO <dst> AT <time> [ID <eid>] [KEEP-NETS]
            need_len(4)
            if parts[2] != "INTO":
                raise LedgerParseError(f"bad MERGE line: {raw_line!r}")
            sources = _split_csv(parts[1])
            dst = parts[3]
            at_time, _ = _parse_at_time(parts, raw_line)
            eid = _optional_value(parts, "ID", f"L{index}")
            tanks.update(sources)
            tanks.add(dst)
            data.update(
                event_id=eid,
                sources=sources,
                dest=dst,
                release_nets="KEEP-NETS" not in parts,
                time=at_time,
            )
            transfer_count += 1

        elif tag == "SCOOP":
            # SCOOP <n> FROM <scope> TO <dst> [SPOT <spot>] AT <time> [ID <eid>]
            need_len(6)
            n = _parse_int(parts[1], raw_line, "scoop count")
            if parts[2] != "FROM" or "TO" not in parts:
                raise LedgerParseError(f"bad SCOOP line: {raw_line!r}")
            from_i = 2
            to_i = parts.index("TO")
            source = parts[from_i + 1]
            dst = parts[to_i + 1]
            spot = "*"
            if "SPOT" in parts:
                si = parts.index("SPOT")
                spot = parts[si + 1]
            at_time, _ = _parse_at_time(parts, raw_line)
            eid = _optional_value(parts, "ID", f"L{index}")
            kind, sid = _parse_scope(source)
            if kind == "tank":
                tanks.add(sid)
            elif kind == "open":
                tanks.add(sid)
            tanks.add(dst)
            data.update(
                event_id=eid,
                count=n,
                spot=spot,
                source=source,
                source_kind=kind,
                source_id=sid,
                dest=dst,
                time=at_time,
            )
            transfer_count += 1

        elif tag == "SPLIT":
            # SPLIT <scope> INTO <dst:n,dst:n,...> AT <time> [ID <eid>]
            need_len(4)
            scope = parts[1]
            if parts[2] != "INTO":
                raise LedgerParseError(f"bad SPLIT line: {raw_line!r}")
            allocations: list[tuple[str, int]] = []
            for item in _split_csv(parts[3]):
                if ":" not in item:
                    raise LedgerParseError(f"bad split allocation {item!r}: {raw_line!r}")
                dst, count_text = item.split(":", 1)
                allocations.append((dst, _parse_int(count_text, raw_line, "allocation count")))
            at_time, _ = _parse_at_time(parts, raw_line)
            eid = _optional_value(parts, "ID", f"L{index}")
            kind, sid = _parse_scope(scope)
            if kind in {"tank", "open"}:
                tanks.add(sid)
            for dst, _n in allocations:
                tanks.add(dst)
            data.update(
                event_id=eid,
                source=scope,
                source_kind=kind,
                source_id=sid,
                allocations=tuple(allocations),
                time=at_time,
            )
            transfer_count += 1

        elif tag == "SHIFTNET":
            # SHIFTNET <net> FROM <tank> TO <dst> AT <time> [ID <eid>]
            need_len(6)
            net = parts[1]
            if parts[2] != "FROM" or "TO" not in parts:
                raise LedgerParseError(f"bad SHIFTNET line: {raw_line!r}")
            from_i = 2
            to_i = parts.index("TO")
            src = parts[from_i + 1]
            dst = parts[to_i + 1]
            at_time, _ = _parse_at_time(parts, raw_line)
            eid = _optional_value(parts, "ID", f"L{index}")
            tanks.add(src)
            tanks.add(dst)
            data.update(event_id=eid, net=net, source=src, dest=dst, time=at_time)
            transfer_count += 1

        elif tag == "COUNT":
            # COUNT <tank> <n> AT <time>
            need_len(5)
            tank = parts[1]
            n = _parse_int(parts[2], raw_line, "count")
            at_time, _ = _parse_at_time(parts, raw_line)
            tanks.add(tank)
            data.update(tank=tank, count=n, time=at_time)

        elif tag == "SEE":
            # SEE <ids> IN <scope> AT <time>
            need_len(5)
            ids = _split_csv(parts[1])
            if parts[2] != "IN":
                raise LedgerParseError(f"bad SEE line: {raw_line!r}")
            scope = parts[3]
            at_time, _ = _parse_at_time(parts, raw_line)
            kind, sid = _parse_scope(scope)
            if kind in {"tank", "open"}:
                tanks.add(sid)
            data.update(fish_ids=ids, scope=scope, source_kind=kind, source_id=sid, time=at_time)

        elif tag == "WITNESS":
            # WITNESS <event> DEST <dst> SPOT <spot> [BY <name>] AT <time>
            need_len(6)
            eid = parts[1]
            if parts[2] != "DEST" or "SPOT" not in parts:
                raise LedgerParseError(f"bad WITNESS line: {raw_line!r}")
            dst = parts[parts.index("DEST") + 1]
            spot = parts[parts.index("SPOT") + 1]
            by = None
            if "BY" in parts:
                by = parts[parts.index("BY") + 1]
            at_time, _ = _parse_at_time(parts, raw_line)
            tanks.add(dst)
            data.update(event_id=eid, dest=dst, spot=spot, witness=by, time=at_time)

        elif tag == "WINDOW":
            # WINDOW <event> OUT <obs,csv> AT <time>
            # WINDOW <event> IN <dst> <obs,csv> [COMPLETE] AT <time>
            need_len(4)
            eid = parts[1]
            side = parts[2]
            if side not in {"OUT", "IN"}:
                raise LedgerParseError(f"WINDOW side must be OUT or IN: {raw_line!r}")
            complete = "COMPLETE" in parts
            at_index = parts.index("AT") if "AT" in parts else len(parts)
            if side == "OUT":
                if at_index < 4:
                    raise LedgerParseError(f"missing OUT observations: {raw_line!r}")
                obs_text = parts[3]
                dst = None
            else:
                if at_index < 5:
                    raise LedgerParseError(f"missing IN destination/observations: {raw_line!r}")
                dst = parts[3]
                obs_text = parts[4]
                tanks.add(dst)
            observations = _split_csv(obs_text)
            at_time, _ = _parse_at_time(parts, raw_line)
            data.update(
                event_id=eid,
                side=side,
                dest=dst,
                observations=observations,
                complete=complete,
                time=at_time,
            )

        elif tag == "WINDOWGAP":
            # WINDOWGAP <event> OUT|IN [dst] REASON <text...> AT <time>
            need_len(3)
            eid = parts[1]
            side = parts[2]
            if side not in {"OUT", "IN"}:
                raise LedgerParseError(f"WINDOWGAP side must be OUT or IN: {raw_line!r}")
            dst = None
            if side == "IN":
                if len(parts) < 4:
                    raise LedgerParseError(f"missing IN destination: {raw_line!r}")
                dst = parts[3]
                tanks.add(dst)
            at_time, _ = _parse_at_time(parts, raw_line)
            data.update(event_id=eid, side=side, dest=dst, time=at_time)

        else:
            raise LedgerParseError(f"unknown ledger tag {tag!r} in line {index}: {raw_line!r}")

        lines.append(
            LedgerLine(
                index=index,
                tag=tag,
                text=raw_line,
                start_char=start,
                end_char=end,
                start_byte=start_byte,
                end_byte=end_byte,
                data=tuple(sorted(data.items(), key=lambda kv: kv[0])),
            )
        )

    if ledger_id is None:
        raise LedgerParseError("ledger has no LEDGER header")
    if len(fish) > MAX_FISH:
        raise LedgerParseError(f"at most {MAX_FISH} fish are supported")
    if len(tanks) > MAX_TANKS:
        raise LedgerParseError(f"at most {MAX_TANKS} tanks are supported, found {sorted(tanks)}")
    if transfer_count > MAX_TRANSFERS:
        raise LedgerParseError(f"at most {MAX_TRANSFERS} transfers are supported")
    event_ids: set[str] = set()
    for line in lines:
        if line.tag in TRANSFER_TAGS:
            event_id = str(line.get("event_id"))
            if event_id in event_ids:
                raise LedgerParseError(f"duplicate transfer event id {event_id}")
            event_ids.add(event_id)

    return ParsedLedger(
        ledger_id=ledger_id,
        text=text,
        lines=tuple(lines),
        fish=tuple(fish),
        version_id=version_id,
        body_length=body_length,
    )


def _optional_value(parts: list[str], key: str, default: str) -> str:
    if key in parts:
        i = parts.index(key)
        if i + 1 < len(parts):
            return parts[i + 1]
    return default


# ---------------------------------------------------------------------------
# Revision-library backed ledger document


class RevisionLedger:
    """A SOLO duty book whose errata are published via RevisionLibrary."""

    def __init__(
        self,
        ledger_id: str,
        text: str | None = None,
        body: bytes | bytearray | memoryview | None = None,
    ) -> None:
        if (text is None) == (body is None):
            raise FishDeskError("provide exactly one of text or body")
        self.ledger_id = ledger_id
        self.library = RevisionLibrary()
        if body is None:
            assert text is not None
            raw = encode_text(text).data
        else:
            raw = bytes(body)
        self.original_version = self.library.add_original(raw).version_id
        self.current_version = self.original_version

    @classmethod
    def from_text(cls, text: str) -> "RevisionLedger":
        parsed = parse_ledger(text)
        return cls(parsed.ledger_id, text=text)

    def version(self, version_id: int | None = None) -> Version:
        return self.library.get(self.current_version if version_id is None else version_id)

    def document(self, version_id: int | None = None) -> DecodedDocument:
        return self.version(version_id).decode()

    def parse(self, version_id: int | None = None) -> ParsedLedger:
        vid = self.current_version if version_id is None else version_id
        version = self.library.get(vid)
        doc = version.decode()  # explicit Version.decode char/body spans
        return parse_ledger(version.text, doc, version_id=vid, body_length=len(version.body))

    def replay(self, version_id: int | None = None) -> "ReplayResult":
        return replay_ledger(self.parse(version_id))

    def replace_span(
        self,
        start_char: int,
        end_char: int,
        new_text: str,
        *,
        base_version: int | None = None,
    ) -> Version:
        base_id = self.current_version if base_version is None else base_version
        # Validate replacement encodability early with a domain-specific error.
        try:
            encode_text(new_text)
        except Exception as exc:  # codec EncodeError has stable type elsewhere
            raise FishDeskError(f"erratum text is not SOLO-encodable: {exc}") from exc
        version = self.library.apply(base_id, [replace(start_char, end_char, new_text)])
        if base_version is None:
            self.current_version = version.version_id
        return version

    def replace_line_containing(
        self,
        needle: str,
        new_text: str,
        *,
        base_version: int | None = None,
        publish: bool = True,
    ) -> tuple[Version, LedgerLine]:
        """Apply one character-span text erratum through RevisionLibrary."""
        base_id = self.current_version if base_version is None else base_version
        parsed = self.parse(base_id)
        old_line = parsed.find_line_containing(needle)
        version = self.replace_span(
            old_line.start_char,
            old_line.end_char,
            new_text,
            base_version=base_id,
        )
        if publish and base_version is None:
            self.current_version = version.version_id
        return version, old_line

    def restore_original_pointer(self) -> None:
        """Point subsequent reads at immutable original version zero."""
        self.current_version = self.original_version


# ---------------------------------------------------------------------------
# Possible-history replayer


@dataclass(frozen=True)
class FishState:
    fish_id: str
    tank: str
    unit: str | None = None


@dataclass(frozen=True)
class MoveTrace:
    event_id: str
    assignments: tuple[tuple[str, str, str | None], ...]

    @property
    def fish_ids(self) -> frozenset[str]:
        return frozenset(item[0] for item in self.assignments)

    def loc_of(self, fish_id: str) -> Loc | None:
        for fid, tank, unit in self.assignments:
            if fid == fish_id:
                return Loc(tank, unit)
        return None

    def for_dest(self, dest: str, fish_by_id: dict[str, Fish]) -> frozenset[str]:
        return frozenset(fid for fid, tank, _unit in self.assignments if tank == dest)


@dataclass(frozen=True)
class TrackedWorld:
    positions: tuple[FishState, ...]
    moves: tuple[MoveTrace, ...] = ()

    def state(self, fish_id: str) -> FishState:
        for pos in self.positions:
            if pos.fish_id == fish_id:
                return pos
        raise FishDeskError(f"world lost fish {fish_id}")

    def loc(self, fish_id: str) -> Loc:
        pos = self.state(fish_id)
        return Loc(pos.tank, pos.unit)

    def matches_scope(self, pos: FishState, kind: str, scope_id: str) -> bool:
        if kind == "tank":
            return pos.tank == scope_id
        if kind == "open":
            return pos.tank == scope_id and pos.unit is None
        if kind == "net":
            return pos.unit == scope_id
        raise LedgerParseError(f"unknown scope kind {kind}")

    def occupants(self, kind: str, scope_id: str) -> tuple[FishState, ...]:
        return tuple(p for p in self.positions if self.matches_scope(p, kind, scope_id))

    def find_move(self, event_id: str) -> MoveTrace | None:
        for move in reversed(self.moves):
            if move.event_id == event_id:
                return move
        return None


@dataclass(frozen=True)
class FilterStat:
    line_index: int
    tag: str
    before: int
    after: int
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class FogGroup:
    fish_ids: tuple[str, ...]
    lots: tuple[str, ...]
    assignments: tuple[tuple[tuple[str, str], ...], ...]


@dataclass(frozen=True)
class HouseholdRow:
    owner: str
    fish_ids: tuple[str, ...]
    certain_locations: dict[str, Loc]
    possible_locations: dict[str, tuple[Loc, ...]]
    deliverable_lots: tuple[str, ...]
    mixed_with: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class IdentityReport:
    viable: bool
    world_count: int
    certain_locations: dict[str, Loc]
    possible_locations: dict[str, tuple[Loc, ...]]
    lot_contents: dict[str, frozenset[str]]
    uncertain_lots: frozenset[str]
    mixed_owner_lots: dict[str, frozenset[str]]
    fog_groups: tuple[FogGroup, ...]
    households: tuple[HouseholdRow, ...]
    filters: tuple[FilterStat, ...]

    def certain_lot_contents(self, lot: str) -> frozenset[str] | None:
        """Return exact contents if lot is stable in every surviving world."""
        key = _lot_key(lot)
        if key in self.uncertain_lots:
            return None
        return self.lot_contents.get(key)

    def summary(self) -> dict[str, object]:
        return {
            "viable": self.viable,
            "world_count": self.world_count,
            "certain_locations": {
                fid: loc.label for fid, loc in sorted(self.certain_locations.items())
            },
            "possible_locations": {
                fid: [loc.label for loc in locs]
                for fid, locs in sorted(self.possible_locations.items())
            },
            "fog_groups": [
                {"fish": list(g.fish_ids), "lots": list(g.lots)} for g in self.fog_groups
            ],
            "households": [
                {
                    "owner": h.owner,
                    "fish": list(h.fish_ids),
                    "status": h.status,
                    "deliverable_lots": list(h.deliverable_lots),
                    "mixed_with": list(h.mixed_with),
                }
                for h in self.households
            ],
        }


@dataclass(frozen=True)
class ReplayResult:
    ledger: ParsedLedger
    worlds: tuple[TrackedWorld, ...]
    filters: tuple[FilterStat, ...]
    contradictions: tuple[str, ...]

    @property
    def viable(self) -> bool:
        return bool(self.worlds)

    def identity_report(self) -> IdentityReport:
        return _build_identity_report(self)

    def raise_if_contradictory(self) -> None:
        if not self.worlds:
            raise LedgerContradiction(
                "no identity history survives ledger checks: " + " | ".join(self.contradictions[-8:])
            )


class LedgerContradiction(FishDeskError):
    """All possible identity histories were excluded by records."""


def _lot_key(lot: str) -> str:
    """Normalise a desk target into the stored ``tank\\0unit`` key."""
    if lot.startswith("OPEN:"):
        tank = lot.split(":", 1)[1]
        return f"{tank}\0"
    if "\0" in lot:
        return lot
    # Unit ids are globally unique and are stored as the second component.
    # IdentityReport.lot_contents keys are resolved by _resolve_target_lot.
    return lot


def _resolve_target_lot(report: IdentityReport, target: str) -> str:
    if target.startswith("OPEN:"):
        tank = target.split(":", 1)[1]
        return f"{tank}\0"
    # A bare tank target means open water when that lot exists.
    open_key = f"{target}\0"
    if open_key in report.lot_contents:
        return open_key
    # Otherwise target may be a net/unit id appearing in a compound key.
    matches = [key for key in report.lot_contents if key.split("\0", 1)[1] == target]
    if len(matches) == 1:
        return matches[0]
    if target in report.lot_contents:
        return target
    return open_key


def replay_ledger(ledger: ParsedLedger) -> ReplayResult:
    fish_by_id = ledger.fish_by_id()
    unknown = sorted(
        fid
        for line in ledger.lines
        for fid in (line.get("fish_ids", ()) or ())  # type: ignore[arg-type]
        if fid not in fish_by_id
    )
    if unknown:
        raise LedgerParseError(f"record names fish without FISH intake line: {unknown}")

    initial = tuple(
        sorted(
            (
                FishState(f.fish_id, str(line.get("tank")), line.get("net"))
                for f in ledger.fish
                for line in ledger.lines
                if line.tag == "FISH" and line.get("fish_id") == f.fish_id
            ),
            key=lambda p: p.fish_id,
        )
    )
    worlds: list[TrackedWorld] = [TrackedWorld(initial)]
    filters: list[FilterStat] = []
    contradictions: list[str] = []

    for line in ledger.lines:
        before = len(worlds)
        reasons: list[str] = []
        try:
            if line.tag in TRANSFER_TAGS:
                next_worlds: list[TrackedWorld] = []
                for world in worlds:
                    generated, why = _apply_transfer(world, line, fish_by_id)
                    if generated:
                        next_worlds.extend(generated)
                    elif why:
                        reasons.append(why)
                worlds = _dedupe_worlds(next_worlds)
            elif line.tag == "COUNT":
                tank = str(line.get("tank"))
                expected = int(line.get("count"))  # type: ignore[arg-type]
                kept = []
                for world in worlds:
                    actual = sum(1 for p in world.positions if p.tank == tank)
                    if actual == expected:
                        kept.append(world)
                    else:
                        reasons.append(f"COUNT {tank}={expected} saw {actual}")
                worlds = _dedupe_worlds(kept)
            elif line.tag == "SEE":
                ids = tuple(line.get("fish_ids", ()))  # type: ignore[arg-type]
                kind = str(line.get("source_kind"))
                sid = str(line.get("source_id"))
                kept = []
                for world in worlds:
                    missing = [
                        fid
                        for fid in ids
                        if fid not in fish_by_id
                        or not world.matches_scope(world.state(fid), kind, sid)
                    ] if all(fid in fish_by_id for fid in ids) else ["UNKNOWN_FISH"]
                    if not missing:
                        kept.append(world)
                    else:
                        reasons.append(f"SEE {ids} not in {sid}: {missing}")
                worlds = _dedupe_worlds(kept)
            elif line.tag == "WITNESS":
                kept = []
                eid = str(line.get("event_id"))
                dest = str(line.get("dest"))
                spot = str(line.get("spot"))
                for world in worlds:
                    move = world.find_move(eid)
                    if move is None:
                        reasons.append(f"missing event {eid} for WITNESS")
                        continue
                    ids = move.for_dest(dest, fish_by_id)
                    if any(fish_by_id[fid].spot == spot for fid in ids):
                        kept.append(world)
                    else:
                        reasons.append(f"no {spot} fish witnessed into {dest} on {eid}")
                worlds = _dedupe_worlds(kept)
            elif line.tag == "WINDOW":
                kept = []
                eid = str(line.get("event_id"))
                side = str(line.get("side"))
                dest = line.get("dest")
                observations = tuple(line.get("observations", ()))  # type: ignore[arg-type]
                complete = bool(line.get("complete"))
                for world in worlds:
                    move = world.find_move(eid)
                    if move is None:
                        reasons.append(f"missing event {eid} for WINDOW")
                        continue
                    if side == "OUT":
                        target = set(move.fish_ids)
                    else:
                        target = set(move.for_dest(str(dest), fish_by_id))
                    if _window_matches(
                        observations, target, fish_by_id, complete=complete
                    ):
                        kept.append(world)
                    else:
                        reasons.append(
                            f"WINDOW {eid} {side} {dest or ''} {observations} cannot match"
                        )
                worlds = _dedupe_worlds(kept)
            elif line.tag == "WINDOWGAP":
                # An explicit gap is evidence that evidence is absent; it must
                # not accidentally become a tail-spot constraint.
                continue
            else:
                # Header/definition lines do not filter histories.
                continue
        except (KeyError, ValueError) as exc:
            raise LedgerParseError(f"cannot replay line {line.index}: {line.text!r}") from exc

        after = len(worlds)
        if line.tag in TRANSFER_TAGS or line.tag in {"COUNT", "SEE", "WITNESS", "WINDOW"}:
            # Keep only a few distinct reasons; duplicate combinatorial branches
            # otherwise produce noisy reports.
            distinct_reasons = tuple(sorted(set(reasons))[:12])
            filters.append(FilterStat(line.index, line.tag, before, after, distinct_reasons))
            if after == 0 and distinct_reasons:
                contradictions.extend(distinct_reasons)

    return ReplayResult(ledger, tuple(worlds), tuple(filters), tuple(dict.fromkeys(contradictions)))


def _dedupe_worlds(worlds: Iterable[TrackedWorld]) -> list[TrackedWorld]:
    # Deduplicate the complete trajectory, not just final positions: two paths
    # may converge on the same current tank layout while remaining distinct
    # possible group-transfer histories that the duty desk must not erase.
    seen: set[TrackedWorld] = set()
    out: list[TrackedWorld] = []
    for world in worlds:
        if world not in seen:
            seen.add(world)
            out.append(world)
    return out


def _apply_transfer(
    world: TrackedWorld,
    line: LedgerLine,
    fish_by_id: dict[str, Fish],
) -> tuple[list[TrackedWorld], str | None]:
    tag = line.tag
    if tag == "MOVE":
        ids = tuple(line.get("fish_ids", ()))  # type: ignore[arg-type]
        kind = str(line.get("source_kind"))
        sid = str(line.get("source_id"))
        dst = str(line.get("dest"))
        try:
            selected = [world.state(fid) for fid in ids]
        except FishDeskError:
            return [], "unknown moved fish"
        if not all(world.matches_scope(p, kind, sid) for p in selected):
            return [], f"MOVE {ids} not all in scope {sid}"
        return [_move_world(world, {p.fish_id for p in selected}, dst, None, line)], None

    if tag == "MERGE":
        sources = tuple(line.get("sources", ()))  # type: ignore[arg-type]
        selected = [p for p in world.positions if p.tank in sources]
        overlap = False  # tank membership is unique, so sources cannot overlap.
        dst = str(line.get("dest"))
        release = bool(line.get("release_nets"))
        if release:
            return [_move_world(world, {p.fish_id for p in selected}, dst, None, line)], None
        unit_map = {p.fish_id: p.unit for p in selected}
        return [
            _move_world(
                world,
                {p.fish_id for p in selected},
                dst,
                None,
                line,
                unit_map=unit_map,
            )
        ], None

    if tag == "SHIFTNET":
        net = str(line.get("net"))
        src = str(line.get("source"))
        dst = str(line.get("dest"))
        selected = [p for p in world.positions if p.unit == net]
        if not selected or any(p.tank != src for p in selected):
            return [], f"net {net} not wholly at {src}"
        return [_move_world(world, {p.fish_id for p in selected}, dst, net, line)], None

    if tag == "SCOOP":
        n = int(line.get("count"))  # type: ignore[arg-type]
        spot = str(line.get("spot"))
        kind = str(line.get("source_kind"))
        sid = str(line.get("source_id"))
        dst = str(line.get("dest"))
        candidates = [p.fish_id for p in world.occupants(kind, sid)]
        if spot != "*":
            candidates = [fid for fid in candidates if fish_by_id[fid].spot == spot]
        if n < 0 or len(candidates) < n:
            return [], f"SCOOP n={n} but {len(candidates)} matching candidates in {sid}"
        out = []
        for combo in combinations(candidates, n):
            out.append(_move_world(world, set(combo), dst, None, line))
        return out, None

    if tag == "SPLIT":
        kind = str(line.get("source_kind"))
        sid = str(line.get("source_id"))
        allocations = tuple(line.get("allocations", ()))  # type: ignore[arg-type]
        candidates = [p.fish_id for p in world.occupants(kind, sid)]
        total = sum(n for _dst, n in allocations)
        if total > len(candidates):
            return [], f"SPLIT asks {total} fish but {len(candidates)} in {sid}"
        out = []
        for groups in _allocations(candidates, allocations):
            assignments: list[tuple[str, str, str | None]] = []
            moved: set[str] = set()
            for (dst, _n), combo in zip(allocations, groups):
                for fid in combo:
                    moved.add(fid)
                    assignments.append((fid, dst, None))
            new_positions = tuple(
                sorted(
                    (
                        FishState(p.fish_id, _dst_for(p.fish_id, assignments, p.tank), None)
                        if p.fish_id in moved
                        else p
                        for p in world.positions
                    ),
                    key=lambda p: p.fish_id,
                )
            )
            trace = MoveTrace(str(line.get("event_id")), tuple(assignments))
            out.append(TrackedWorld(new_positions, world.moves + (trace,)))
        return out, None

    return [], f"unknown transfer {tag}"


def _same_unit_or_none(selected: Sequence[FishState]) -> str | None:
    units = {p.unit for p in selected if p.unit is not None}
    return next(iter(units)) if len(units) == 1 else None


def _dst_for(
    fish_id: str,
    assignments: Sequence[tuple[str, str, str | None]],
    default: str,
) -> str:
    for fid, dst, _unit in assignments:
        if fid == fish_id:
            return dst
    return default


def _move_world(
    world: TrackedWorld,
    moved: set[str],
    dst: str,
    unit: str | None,
    line: LedgerLine,
    *,
    unit_map: dict[str, str | None] | None = None,
) -> TrackedWorld:
    def next_unit(fid: str) -> str | None:
        return unit_map[fid] if unit_map is not None else unit

    assignments = tuple((fid, dst, next_unit(fid)) for fid in sorted(moved))
    new_positions = tuple(
        sorted(
            (
                FishState(p.fish_id, dst, next_unit(p.fish_id))
                if p.fish_id in moved
                else p
                for p in world.positions
            ),
            key=lambda p: p.fish_id,
        )
    )
    trace = MoveTrace(str(line.get("event_id")), assignments)
    return TrackedWorld(new_positions, world.moves + (trace,))


def _allocations(
    candidates: Sequence[str],
    allocations: Sequence[tuple[str, int]],
) -> Iterator[tuple[tuple[str, ...], ...]]:
    """Yield every disjoint ordered allocation of candidate fish."""
    if not allocations:
        yield ()
        return
    (_dst, n), *rest = allocations
    for combo in combinations(candidates, n):
        remaining = [fid for fid in candidates if fid not in combo]
        for tail in _allocations(remaining, rest):  # type: ignore[arg-type]
            yield (combo,) + tail


def _window_matches(
    observations: Sequence[str],
    target_fish: set[str],
    fish_by_id: dict[str, Fish],
    *,
    complete: bool,
) -> bool:
    if complete and len(observations) != len(target_fish):
        return False
    if len(observations) > len(target_fish):
        return False

    # Bipartite injection: an observed F1 can only match F1; a spot token can
    # match any target fish carrying that spot.
    obs_options: list[set[str]] = []
    for token in observations:
        if token in fish_by_id:
            if token not in target_fish:
                return False
            obs_options.append({token})
        else:
            choices = {
                fid
                for fid in target_fish
                if fid in fish_by_id and fish_by_id[fid].spot == token
            }
            if not choices:
                return False
            obs_options.append(choices)

    def can_match(i: int, used: set[str]) -> bool:
        if i == len(obs_options):
            return True
        for fid in obs_options[i] - used:
            if can_match(i + 1, used | {fid}):
                return True
        return False

    if not can_match(0, set()):
        return False
    if complete:
        # Completeness plus equal lengths means every target fish was observed.
        return len(observations) == len(target_fish)
    return True


# ---------------------------------------------------------------------------
# Identity report construction


def _build_identity_report(result: ReplayResult) -> IdentityReport:
    ledger = result.ledger
    fish_ids = ledger.fish_ids
    if not result.worlds:
        return IdentityReport(
            viable=False,
            world_count=0,
            certain_locations={},
            possible_locations={},
            lot_contents={},
            uncertain_lots=frozenset(),
            mixed_owner_lots={},
            fog_groups=(),
            households=(),
            filters=result.filters,
        )

    possible: dict[str, set[Loc]] = {fid: set() for fid in fish_ids}
    lot_sets: dict[str, list[frozenset[str]]] = {}
    for world in result.worlds:
        for fid in fish_ids:
            possible[fid].add(world.loc(fid))
        lots: dict[str, set[str]] = {}
        for pos in world.positions:
            key = Loc(pos.tank, pos.unit).lot
            lots.setdefault(key, set()).add(pos.fish_id)
        for key, occupants in lots.items():
            lot_sets.setdefault(key, []).append(frozenset(occupants))

    certain_locations = {
        fid: next(iter(locs))
        for fid, locs in possible.items()
        if len(locs) == 1
    }
    possible_locations = {
        fid: tuple(sorted(locs, key=lambda loc: loc.label))
        for fid, locs in possible.items()
    }

    all_lot_keys = set(lot_sets)
    # Include empty occupancy in worlds where a lot is absent.  This matters for
    # a compartment that moves between tanks.
    for world in result.worlds:
        present = {Loc(p.tank, p.unit).lot for p in world.positions}
        for key in all_lot_keys:
            if key not in present:
                lot_sets[key].append(frozenset())

    lot_contents: dict[str, frozenset[str]] = {}
    uncertain: set[str] = set()
    for key, sets in lot_sets.items():
        unique = set(sets)
        if len(unique) == 1:
            lot_contents[key] = next(iter(unique))
        else:
            uncertain.add(key)
            # Expose the union as a conservative contents description.
            lot_contents[key] = frozenset().union(*unique)

    fish_by_id = ledger.fish_by_id()
    mixed_owner_lots: dict[str, frozenset[str]] = {}
    for key, occupants in lot_contents.items():
        owners = {fish_by_id[fid].owner for fid in occupants if fid in fish_by_id}
        if len(owners) > 1:
            mixed_owner_lots[key] = occupants

    fog_groups = _build_fog_groups(possible_locations, lot_contents, uncertain, result.worlds)
    households = _build_households(
        ledger.fish,
        certain_locations,
        possible_locations,
        lot_contents,
        uncertain,
        mixed_owner_lots,
        fog_groups,
    )

    return IdentityReport(
        viable=True,
        world_count=len(result.worlds),
        certain_locations=certain_locations,
        possible_locations=possible_locations,
        lot_contents=lot_contents,
        uncertain_lots=frozenset(uncertain),
        mixed_owner_lots=mixed_owner_lots,
        fog_groups=fog_groups,
        households=tuple(households),
        filters=result.filters,
    )


def _build_fog_groups(
    possible_locations: dict[str, tuple[Loc, ...]],
    lot_contents: dict[str, frozenset[str]],
    uncertain_lots: set[str],
    worlds: Sequence[TrackedWorld],
) -> tuple[FogGroup, ...]:
    # Bipartite connected components between uncertain fish and uncertain lots.
    uncertain_fish = {fid for fid, locs in possible_locations.items() if len(locs) > 1}
    adjacency: dict[str, set[str]] = {f"F:{fid}": set() for fid in uncertain_fish}
    for key in uncertain_lots:
        adjacency[f"L:{key}"] = set()

    for fid in uncertain_fish:
        for loc in possible_locations[fid]:
            key = loc.lot
            lot_node = f"L:{key}"
            if lot_node in adjacency:
                adjacency[f"F:{fid}"].add(lot_node)
                adjacency[lot_node].add(f"F:{fid}")

    seen: set[str] = set()
    groups: list[FogGroup] = []
    for node in list(adjacency):
        if node in seen:
            continue
        stack = [node]
        component: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            component.add(cur)
            stack.extend(adjacency.get(cur, ()))
        fish = tuple(sorted(n[2:] for n in component if n.startswith("F:")))
        lots = tuple(sorted(n[2:].replace("\0", ":") for n in component if n.startswith("L:")))
        assignments = _possible_assignments(fish, lots, worlds)
        groups.append(FogGroup(fish, lots, assignments))
    return tuple(g for g in groups if g.fish_ids)


def _possible_assignments(
    fish_ids: Sequence[str],
    lots: Sequence[str],
    worlds: Sequence[TrackedWorld],
) -> tuple[tuple[tuple[str, str], ...], ...]:
    raw_lots = {lot.replace(":", "\0", 1) if ":" in lot and "\0" not in lot else lot for lot in lots}
    out = set()
    for world in worlds:
        assignment = []
        for fid in fish_ids:
            loc_key = world.loc(fid).lot
            if loc_key in raw_lots:
                assignment.append((fid, loc_key.replace("\0", ":")))
        if assignment:
            out.add(tuple(sorted(assignment)))
    return tuple(sorted(out))


def _build_households(
    fish: Sequence[Fish],
    certain: dict[str, Loc],
    possible: dict[str, tuple[Loc, ...]],
    lot_contents: dict[str, frozenset[str]],
    uncertain_lots: set[str],
    mixed_owner_lots: dict[str, frozenset[str]],
    fog_groups: Sequence[FogGroup],
) -> list[HouseholdRow]:
    by_owner: dict[str, list[Fish]] = {}
    for f in fish:
        by_owner.setdefault(f.owner, []).append(f)

    fish_fog: dict[str, FogGroup] = {}
    for group in fog_groups:
        for fid in group.fish_ids:
            fish_fog[fid] = group

    fish_owner = {f.fish_id: f.owner for f in fish}
    rows: list[HouseholdRow] = []
    for owner, owner_fish in sorted(by_owner.items()):
        ids = tuple(f.fish_id for f in sorted(owner_fish, key=lambda f: f.fish_id))
        owner_set = set(ids)
        deliverable: list[str] = []
        for key, occupants in lot_contents.items():
            if key in uncertain_lots:
                continue
            if occupants and occupants <= owner_set:
                unit = key.split("\0", 1)[1]
                deliverable.append(unit if unit else key.split("\0", 1)[0])

        mixed_with: set[str] = set()
        for key, occupants in mixed_owner_lots.items():
            if any(fid in owner_set for fid in occupants):
                mixed_with.update(fish_owner[fid] for fid in occupants if fish_owner[fid] != owner)
        for fid in ids:
            if fid in fish_fog:
                group = fish_fog[fid]
                for other in group.fish_ids:
                    if other != fid:
                        mixed_with.add(fish_owner.get(other, other))

        all_certain = all(fid in certain for fid in ids)
        has_mix = bool(mixed_with)
        if all_certain and not has_mix:
            status = "located"
        elif all_certain and has_mix:
            status = "located_but_shared"
        else:
            status = "identity_mixed"
        rows.append(
            HouseholdRow(
                owner=owner,
                fish_ids=ids,
                certain_locations={fid: certain[fid] for fid in ids if fid in certain},
                possible_locations={fid: possible[fid] for fid in ids},
                deliverable_lots=tuple(sorted(set(deliverable))),
                mixed_with=tuple(sorted(mixed_with)),
                status=status,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Ledger builder and compartment slot accounting


class LedgerBuilder:
    """Small fluent ASCII ledger writer used by new boarding scenarios."""

    def __init__(self, ledger_id: str) -> None:
        self.ledger_id = ledger_id
        self.lines: list[str] = [f"LEDGER {ledger_id}"]

    def raw(self, line: str) -> "LedgerBuilder":
        self.lines.append(line)
        return self

    def comment(self, text: str) -> "LedgerBuilder":
        self.lines.append(f"# {text}")
        return self

    def tank(self, tank: str, capacity: int) -> "LedgerBuilder":
        self.lines.append(f"TANK {tank} CAP {capacity}")
        return self

    def net(self, net: str, tank: str, owner: str | None = None) -> "LedgerBuilder":
        owner_part = f" OWNER {owner}" if owner else ""
        self.lines.append(f"NET {net} TANK {tank}{owner_part}")
        return self

    def fish(
        self,
        fid: str,
        owner: str,
        spot: str,
        tank: str,
        at: str,
        net: str | None = None,
    ) -> "LedgerBuilder":
        net_part = f" NET {net}" if net else ""
        self.lines.append(
            f"FISH {fid} OWNER {owner} SPOT {spot} TANK {tank}{net_part} AT {at}"
        )
        return self

    def shift_net(self, eid: str, net: str, src: str, dst: str, at: str) -> "LedgerBuilder":
        self.lines.append(f"SHIFTNET {net} FROM {src} TO {dst} ID {eid} AT {at}")
        return self

    def merge(
        self,
        eid: str,
        sources: Sequence[str],
        dst: str,
        at: str,
        *,
        keep_nets: bool = False,
    ) -> "LedgerBuilder":
        keep = " KEEP-NETS" if keep_nets else ""
        self.lines.append(
            f"MERGE {','.join(sources)} INTO {dst} ID {eid} AT {at}{keep}"
        )
        return self

    def split(
        self,
        eid: str,
        source: str,
        allocations: dict[str, int] | Sequence[tuple[str, int]],
        at: str,
    ) -> "LedgerBuilder":
        if isinstance(allocations, dict):
            allocations = tuple(allocations.items())
        alloc = ",".join(f"{dst}:{n}" for dst, n in allocations)
        self.lines.append(f"SPLIT {source} INTO {alloc} ID {eid} AT {at}")
        return self

    def scoop(
        self,
        eid: str,
        n: int,
        source: str,
        dst: str,
        at: str,
        *,
        spot: str | None = None,
    ) -> "LedgerBuilder":
        spot_part = f" SPOT {spot}" if spot else ""
        self.lines.append(
            f"SCOOP {n} FROM {source} TO {dst}{spot_part} ID {eid} AT {at}"
        )
        return self

    def count(self, tank: str, n: int, at: str) -> "LedgerBuilder":
        self.lines.append(f"COUNT {tank} {n} AT {at}")
        return self

    def see(self, fish_ids: Sequence[str], scope: str, at: str) -> "LedgerBuilder":
        self.lines.append(f"SEE {','.join(fish_ids)} IN {scope} AT {at}")
        return self

    def witness(
        self,
        eid: str,
        dst: str,
        spot: str,
        at: str,
        *,
        by: str | None = None,
    ) -> "LedgerBuilder":
        by_part = f" BY {by}" if by else ""
        self.lines.append(
            f"WITNESS {eid} DEST {dst} SPOT {spot}{by_part} AT {at}"
        )
        return self

    def window(
        self,
        eid: str,
        side: str,
        observations: Sequence[str],
        at: str,
        *,
        dst: str | None = None,
        complete: bool = False,
    ) -> "LedgerBuilder":
        comp = " COMPLETE" if complete else ""
        obs = ",".join(observations)
        if side == "OUT":
            self.lines.append(f"WINDOW {eid} OUT {obs}{comp} AT {at}")
        else:
            if dst is None:
                raise FishDeskError("IN window needs dst")
            self.lines.append(f"WINDOW {eid} IN {dst} {obs}{comp} AT {at}")
        return self

    def window_gap(
        self,
        eid: str,
        side: str,
        at: str,
        *,
        dst: str | None = None,
    ) -> "LedgerBuilder":
        dest = f" {dst}" if dst else ""
        self.lines.append(f"WINDOWGAP {eid} {side}{dest} AT {at}")
        return self

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


@dataclass(frozen=True)
class SlotUse:
    tank: str
    capacity: int | None
    nets: tuple[str, ...]
    free: int | None
    violations: tuple[str, ...] = ()
    peak: int = 0
    peak_nets: tuple[str, ...] = ()
    peak_free: int | None = None


def slot_report(ledger: ParsedLedger | ReplayResult) -> tuple[SlotUse, ...]:
    """Check net-compartment occupancy, atomic shifts and peak slot use."""
    parsed = ledger.ledger if isinstance(ledger, ReplayResult) else ledger
    capacities = {
        str(line.get("tank")): int(line.get("capacity"))  # type: ignore[arg-type]
        for line in parsed.line_by_tag("TANK")
    }
    net_tank: dict[str, str] = {}
    for line in parsed.line_by_tag("NET"):
        net_tank[str(line.get("net"))] = str(line.get("tank"))
    for line in parsed.line_by_tag("FISH"):
        net = line.get("net")
        if net is not None:
            net_tank[str(net)] = str(line.get("tank"))

    tank_violations: dict[str, list[str]] = {tank: [] for tank in capacities}
    peaks: dict[str, tuple[str, ...]] = {}

    def snapshot() -> None:
        by_tank: dict[str, list[str]] = {}
        for net, tank in net_tank.items():
            by_tank.setdefault(tank, []).append(net)
            tank_violations.setdefault(tank, [])
        for tank, nets in by_tank.items():
            ordered = tuple(sorted(nets))
            previous = peaks.get(tank, ())
            if len(ordered) >= len(previous):
                peaks[tank] = ordered
            cap = capacities.get(tank)
            if cap is not None and len(nets) > cap:
                tank_violations[tank].append(
                    f"tank {tank} uses {len(nets)} nets but CAP {cap}"
                )

    snapshot()
    for line in parsed.lines:
        if line.tag != "SHIFTNET":
            continue
        net = str(line.get("net"))
        src = str(line.get("source"))
        dst = str(line.get("dest"))
        tank_violations.setdefault(src, [])
        tank_violations.setdefault(dst, [])
        if net_tank.get(net) != src:
            tank_violations[src].append(
                f"{net} shift says FROM {src}, records place it at {net_tank.get(net)}"
            )
        # Whole-net handover is atomic: source slot frees and destination slot
        # is occupied together, so a move never needs two free slots.
        net_tank[net] = dst
        snapshot()

    all_tanks = sorted(set(capacities) | set(net_tank.values()))
    result: list[SlotUse] = []
    for tank in all_tanks:
        final_nets = tuple(sorted(net for net, t in net_tank.items() if t == tank))
        cap = capacities.get(tank)
        peak_nets = peaks.get(tank, final_nets)
        result.append(
            SlotUse(
                tank=tank,
                capacity=cap,
                nets=final_nets,
                free=None if cap is None else cap - len(final_nets),
                violations=tuple(sorted(set(tank_violations.get(tank, ())))),
                peak=len(peak_nets),
                peak_nets=peak_nets,
                peak_free=None if cap is None else cap - len(peak_nets),
            )
        )
    return tuple(result)


class NetRack:
    """Live finite-slot rack for extractable household nets.

    A shift is checked against the destination before the source slot is freed
    only for reporting; physically the whole net is handed across, so the
    operation is atomic and never needs two empty slots.
    """

    def __init__(self, capacities: dict[str, int] | None = None) -> None:
        self.capacities = dict(capacities or {})
        self.net_tank: dict[str, str] = {}
        self.net_owner: dict[str, str] = {}

    def set_capacity(self, tank: str, capacity: int) -> None:
        if capacity < 0:
            raise FishDeskError("net capacity must be non-negative")
        self.capacities[tank] = capacity

    def used(self, tank: str) -> int:
        return sum(1 for t in self.net_tank.values() if t == tank)

    def free(self, tank: str) -> int | None:
        cap = self.capacities.get(tank)
        return None if cap is None else cap - self.used(tank)

    def reserve(self, net: str, tank: str, owner: str | None = None) -> None:
        if net in self.net_tank:
            raise FishDeskError(f"net {net} is already reserved at {self.net_tank[net]}")
        cap = self.capacities.get(tank)
        if cap is not None and self.used(tank) >= cap:
            raise FishDeskError(f"no free extractable-net slot in tank {tank} (CAP {cap})")
        self.net_tank[net] = tank
        if owner is not None:
            self.net_owner[net] = owner

    def shift(self, net: str, dest: str, *, expected_source: str | None = None) -> None:
        if net not in self.net_tank:
            raise FishDeskError(f"unknown net {net}; reserve it before shifting")
        source = self.net_tank[net]
        if expected_source is not None and source != expected_source:
            raise FishDeskError(
                f"net {net} is at {source}, not expected FROM {expected_source}"
            )
        if source == dest:
            return
        cap = self.capacities.get(dest)
        if cap is not None and self.used(dest) >= cap:
            raise FishDeskError(
                f"cannot shift {net} to {dest}: no free slot (CAP {cap})"
            )
        self.net_tank[net] = dest

    def lots(self) -> tuple[str, ...]:
        return tuple(sorted(self.net_tank))


# ---------------------------------------------------------------------------
# Receipts, mandates, sealed boxes and front desk


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    fish_id: str
    owner: str
    returned: bool = False
    used: bool = False


class ReceiptBook:
    def __init__(self) -> None:
        self._receipts: dict[str, Receipt] = {}
        self._fish_to_receipt: dict[str, str] = {}

    def issue(self, receipt_id: str, fish_id: str, owner: str) -> Receipt:
        if receipt_id in self._receipts:
            raise FishDeskError(f"duplicate receipt id {receipt_id}")
        if fish_id in self._fish_to_receipt:
            raise DuplicateReceiptFish(
                f"fish {fish_id} already has receipt {self._fish_to_receipt[fish_id]}"
            )
        receipt = Receipt(receipt_id, fish_id, owner)
        self._receipts[receipt_id] = receipt
        self._fish_to_receipt[fish_id] = receipt_id
        return receipt

    def get(self, receipt_id: str) -> Receipt:
        try:
            return self._receipts[receipt_id]
        except KeyError:
            raise FishDeskError(f"unknown receipt {receipt_id}") from None

    def return_receipt(self, receipt_id: str) -> Receipt:
        old = self.get(receipt_id)
        new = Receipt(old.receipt_id, old.fish_id, old.owner, True, old.used)
        self._receipts[receipt_id] = new
        return new

    def mark_used(self, receipt_id: str) -> Receipt:
        old = self.get(receipt_id)
        if not old.returned:
            raise FishDeskError("a receipt must be returned before write-off")
        new = Receipt(old.receipt_id, old.fish_id, old.owner, True, True)
        self._receipts[receipt_id] = new
        return new

    def all(self) -> tuple[Receipt, ...]:
        return tuple(self._receipts[k] for k in sorted(self._receipts))


def receipt_fish_conflicts(
    receipts: Iterable[Receipt | tuple[str, str] | dict[str, str]],
) -> dict[str, tuple[str, ...]]:
    """Return fish ids pointed to by more than one receipt id."""
    by_fish: dict[str, list[str]] = {}
    for item in receipts:
        if isinstance(item, Receipt):
            rid, fid = item.receipt_id, item.fish_id
        elif isinstance(item, dict):
            rid, fid = str(item["receipt_id"]), str(item["fish_id"])
        else:
            rid, fid = item
        by_fish.setdefault(fid, []).append(rid)
    return {fid: tuple(rids) for fid, rids in by_fish.items() if len(rids) > 1}


@dataclass(frozen=True)
class Mandate:
    mandate_id: str
    owner: str
    holder: str
    target: str | None = None
    active: bool = True

    def covers(self, owner: str, holder: str | None, target: str) -> bool:
        return (
            self.active
            and self.owner == owner
            and self.holder == holder
            and self.target in (None, "*", target)
        )


class MandateBook:
    def __init__(self) -> None:
        self._mandates: dict[str, Mandate] = {}

    def grant(
        self,
        mandate_id: str,
        owner: str,
        holder: str,
        target: str | None = None,
    ) -> Mandate:
        mandate = Mandate(mandate_id, owner, holder, target, True)
        self._mandates[mandate_id] = mandate
        return mandate

    def get(self, mandate_id: str) -> Mandate:
        try:
            return self._mandates[mandate_id]
        except KeyError:
            raise MandateError(f"unknown mandate {mandate_id}") from None

    def revoke(self, mandate_id: str) -> Mandate:
        old = self.get(mandate_id)
        revoked = Mandate(old.mandate_id, old.owner, old.holder, old.target, False)
        self._mandates[mandate_id] = revoked
        return revoked


@dataclass(frozen=True)
class PickupCase:
    case_id: str
    owner: str
    receipt_ids: tuple[str, ...]
    target: str
    holder: str | None = None
    mandate_id: str | None = None
    day: str = "weekend"


@dataclass(frozen=True)
class PickupDecision:
    allowed: bool
    case_id: str
    reason_code: str
    reason: str
    owner: str | None = None
    fish_ids: tuple[str, ...] = ()
    target: str | None = None
    box_id: str | None = None
    household: str | None = None

    def summary(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "case_id": self.case_id,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "owner": self.owner,
            "fish_ids": list(self.fish_ids),
            "target": self.target,
            "box_id": self.box_id,
            "household": self.household,
        }


@dataclass(frozen=True)
class OpaqueTransportBox:
    box_id: str
    lot: str
    fish_ids: frozenset[str]
    owner: str | None
    sealed: bool = True

    def open_at_front_desk(self) -> None:
        raise FishDeskError(
            f"{self.box_id} is a sealed opaque transport box; the front desk must not "
            "open it to inspect tail spots"
        )


@dataclass(frozen=True)
class Handover:
    case_id: str
    box: OpaqueTransportBox
    receipt_ids: tuple[str, ...]
    day: str


class FrontDesk:
    def __init__(
        self,
        receipts: ReceiptBook | None = None,
        mandates: MandateBook | None = None,
        available_lots: Iterable[str] = (),
    ) -> None:
        self.receipts = receipts or ReceiptBook()
        self.mandates = mandates or MandateBook()
        self.available_lots = set(available_lots)
        self.deliveries: list[Handover] = []
        self.delivered_fish: set[str] = set()
        self._box_number = 0

    def set_available_lots(self, lots: Iterable[str]) -> None:
        self.available_lots = set(lots)

    def evaluate(self, case: PickupCase, replay: ReplayResult) -> PickupDecision:
        def deny(code: str, reason: str) -> PickupDecision:
            return PickupDecision(False, case.case_id, code, reason, owner=case.owner, target=case.target)

        # Receipt checks first: returned and not already written off.
        receipt_fish: list[str] = []
        for rid in case.receipt_ids:
            try:
                receipt = self.receipts.get(rid)
            except FishDeskError:
                return deny("UNKNOWN_RECEIPT", f"receipt {rid} does not exist")
            if receipt.owner != case.owner:
                return deny("RECEIPT_OWNER_MISMATCH", f"receipt {rid} belongs to {receipt.owner}")
            if not receipt.returned:
                return deny("RECEIPT_NOT_RETURNED", f"receipt {rid} has not been returned")
            if receipt.used or receipt.fish_id in self.delivered_fish:
                return deny("RECEIPT_ALREADY_WRITTEN_OFF", f"receipt {rid} was already written off")
            receipt_fish.append(receipt.fish_id)

        expected = frozenset(receipt_fish)
        if len(expected) != len(receipt_fish):
            return deny("DUPLICATE_FISH_ON_CASE", "one pickup case lists the same fish twice")
        if expected & self.delivered_fish:
            return deny("FISH_ALREADY_DELIVERED", "one receipt fish already has a completed handover")

        # Mandate / presenter check.
        if case.mandate_id:
            try:
                mandate = self.mandates.get(case.mandate_id)
            except MandateError:
                return deny("UNKNOWN_MANDATE", f"mandate {case.mandate_id} is unknown")
            if not mandate.active:
                return deny("MANDATE_REVOKED", f"mandate {case.mandate_id} has been revoked")
            if not mandate.covers(case.owner, case.holder, case.target):
                return deny("MANDATE_SCOPE_MISMATCH", "mandate does not cover this owner/holder/target")
        elif case.holder != case.owner:
            return deny("MANDATE_REQUIRED", f"{case.holder!r} needs a mandate to collect for {case.owner!r}")

        # Today's extractable compartment / pickup lot.
        if case.target not in self.available_lots:
            return deny(
                "LOT_NOT_AVAILABLE",
                f"target {case.target} is not an extractable pickup lot on {case.day}",
            )

        if not replay.viable:
            return deny("LEDGER_CONTRADICTION", "all identity histories were excluded by counts/sightings")

        report = replay.identity_report()
        lot_key = _resolve_target_lot(report, case.target)
        if lot_key in report.uncertain_lots:
            possible = sorted(report.lot_contents.get(lot_key, frozenset()))
            return deny(
                "IDENTITY_MIXED",
                f"identity in {case.target} remains mixed; possible fish union={possible}",
            )
        contents = report.lot_contents.get(lot_key)
        if contents is None:
            return deny("LOT_EMPTY_OR_UNKNOWN", f"no surviving history contains lot {case.target}")
        if contents != expected:
            fish_by_id = replay.ledger.fish_by_id()
            content_owners = {fish_by_id[fid].owner for fid in contents if fid in fish_by_id}
            if len(content_owners) > 1:
                return deny(
                    "IDENTITY_MIXED",
                    f"sealed lot {case.target} is shared by households {sorted(content_owners)}; "
                    f"it contains {sorted(contents)}, receipts name {sorted(expected)} and the "
                    "desk cannot open the opaque box to separate tail spots",
                )
            return deny(
                "LOT_CONTENT_MISMATCH",
                f"sealed lot {case.target} contains {sorted(contents)}, receipts name {sorted(expected)}",
            )

        return PickupDecision(
            True,
            case.case_id,
            "OK",
            "identity, mandate, receipt and compartment all match",
            owner=case.owner,
            fish_ids=tuple(sorted(expected)),
            target=case.target,
            box_id=f"PLANNED-BOX-{case.case_id}",
            household=case.owner,
        )

    def settle(self, case: PickupCase, replay: ReplayResult) -> Handover:
        decision = self.evaluate(case, replay)
        if not decision.allowed:
            raise PickupBlocked(decision)
        self._box_number += 1
        box = OpaqueTransportBox(
            box_id=f"BOX-{self._box_number:02d}-{case.case_id}",
            lot=case.target,
            fish_ids=frozenset(decision.fish_ids),
            owner=case.owner,
        )
        for rid in case.receipt_ids:
            self.receipts.mark_used(rid)
        self.delivered_fish.update(decision.fish_ids)
        handover = Handover(case.case_id, box, tuple(case.receipt_ids), case.day)
        self.deliveries.append(handover)
        return handover

    def attempt(self, case: PickupCase, replay: ReplayResult) -> PickupDecision:
        """Evaluate and, on success, write off and settle the case."""
        decision = self.evaluate(case, replay)
        if decision.allowed:
            handover = self.settle(case, replay)
            return PickupDecision(
                True,
                decision.case_id,
                "WRITTEN_OFF",
                "sealed opaque box handed over and receipt written off",
                owner=decision.owner,
                fish_ids=decision.fish_ids,
                target=decision.target,
                box_id=handover.box.box_id,
                household=decision.household,
            )
        return decision


class FishBoarding:
    """One facade for intake, merge/split/scoop, revision replay and pickup.

    The class deliberately keeps the ledger builder, SOLO revision ledger and
    front-desk receipt/mandate books separate: records and counter paperwork
    change for different reasons, but a caller wanting the natural verbs can
    use this single object.
    """

    def __init__(self, ledger_id: str, available_lots: Iterable[str] = ()) -> None:
        self.ledger_id = ledger_id
        self.builder = LedgerBuilder(ledger_id)
        self.rack = NetRack()
        self.receipts = ReceiptBook()
        self.mandates = MandateBook()
        self.desk = FrontDesk(self.receipts, self.mandates, available_lots)
        self.ledger: RevisionLedger | None = None

    # -- ledger definition / husbandry verbs -------------------------------
    def tank(self, tank: str, capacity: int) -> "FishBoarding":
        self.rack.set_capacity(tank, capacity)
        self.builder.tank(tank, capacity)
        return self

    def reserve_net(self, net: str, tank: str, owner: str | None = None) -> "FishBoarding":
        self.rack.reserve(net, tank, owner)
        self.builder.net(net, tank, owner)
        return self

    def intake(
        self,
        fish_id: str,
        owner: str,
        spot: str,
        tank: str,
        at: str,
        *,
        net: str | None = None,
        receipt_id: str | None = None,
    ) -> "FishBoarding":
        if net is not None and net not in self.rack.net_tank:
            self.reserve_net(net, tank, owner)
        self.builder.fish(fish_id, owner, spot, tank, at, net=net)
        if receipt_id is not None:
            self.receipts.issue(receipt_id, fish_id, owner)
        return self

    def return_receipt(self, receipt_id: str) -> "FishBoarding":
        self.receipts.return_receipt(receipt_id)
        return self

    def grant_mandate(
        self, mandate_id: str, owner: str, holder: str, target: str | None = None
    ) -> "FishBoarding":
        self.mandates.grant(mandate_id, owner, holder, target)
        return self

    def merge(
        self, event_id: str, sources: Sequence[str], dest: str, at: str
    ) -> "FishBoarding":
        self.builder.merge(event_id, sources, dest, at)
        return self

    def split(
        self,
        event_id: str,
        source: str,
        allocations: dict[str, int] | Sequence[tuple[str, int]],
        at: str,
    ) -> "FishBoarding":
        self.builder.split(event_id, source, allocations, at)
        return self

    def scoop(
        self,
        event_id: str,
        count: int,
        source: str,
        dest: str,
        at: str,
        *,
        spot: str | None = None,
    ) -> "FishBoarding":
        self.builder.scoop(event_id, count, source, dest, at, spot=spot)
        return self

    def shift_net(
        self, event_id: str, net: str, source: str, dest: str, at: str
    ) -> "FishBoarding":
        self.rack.shift(net, dest, expected_source=source)
        self.builder.shift_net(event_id, net, source, dest, at)
        return self

    def count(self, tank: str, n: int, at: str) -> "FishBoarding":
        self.builder.count(tank, n, at)
        return self

    def see(self, fish_ids: Sequence[str], scope: str, at: str) -> "FishBoarding":
        self.builder.see(fish_ids, scope, at)
        return self

    def witness(
        self, event_id: str, dest: str, spot: str, at: str, *, by: str | None = None
    ) -> "FishBoarding":
        self.builder.witness(event_id, dest, spot, at, by=by)
        return self

    def window(
        self,
        event_id: str,
        side: str,
        observations: Sequence[str],
        at: str,
        *,
        dest: str | None = None,
        complete: bool = False,
    ) -> "FishBoarding":
        self.builder.window(
            event_id, side, observations, at, dst=dest, complete=complete
        )
        return self

    def window_gap(
        self, event_id: str, side: str, at: str, *, dest: str | None = None
    ) -> "FishBoarding":
        self.builder.window_gap(event_id, side, at, dst=dest)
        return self

    def set_available_lots(self, lots: Iterable[str]) -> "FishBoarding":
        self.desk.set_available_lots(lots)
        return self

    # -- publication / replay / errata ------------------------------------
    def publish(self) -> RevisionLedger:
        self.ledger = RevisionLedger.from_text(self.builder.render())
        return self.ledger

    def require_ledger(self) -> RevisionLedger:
        if self.ledger is None:
            return self.publish()
        return self.ledger

    def replay(self, version_id: int | None = None) -> ReplayResult:
        return self.require_ledger().replay(version_id)

    def apply_erratum(
        self, needle: str, new_line: str
    ) -> tuple[Version, LedgerLine]:
        ledger = self.require_ledger()
        return ledger.replace_line_containing(needle, new_line)

    # -- front desk --------------------------------------------------------
    def evaluate_pickup(self, case: PickupCase) -> PickupDecision:
        return self.desk.evaluate(case, self.replay())

    def pickup(self, case: PickupCase) -> PickupDecision:
        """Evaluate and, when legal, seal/hand over and write the receipt off."""
        return self.desk.attempt(case, self.replay())


# Readable aliases for callers/tests that name the service differently.
FishLedger = RevisionLedger
BoardingDesk = FrontDesk
FishDesk = FrontDesk
BoardingService = FishBoarding


# ---------------------------------------------------------------------------
# Canonical text fixtures


def canonical_ledger_text(*, clear_witness: bool = True) -> str:
    b = LedgerBuilder("CANONICAL-WEEKEND")
    b.comment("PAIR marker 一丁丂; core records below stay ASCII")
    b.fish("F1", "JIA", "WHITE", "A", "09:00")
    b.fish("F2", "YI", "PLAIN", "B", "09:01")
    b.merge("E-MERGE", ["A", "B"], "C", "10:00")
    b.count("C", 2, "10:05")
    b.split("E-SPLIT", "C", {"A": 1, "B": 1}, "11:00")
    if clear_witness:
        b.witness("E-SPLIT", "A", "WHITE", "11:01", by="LIN")
    else:
        b.raw("# ERRATUM_UNSEEN which fish entered A; no tail-spot observation")
    b.count("A", 1, "11:05")
    b.count("B", 1, "11:06")
    return b.render()


def slot_pressure_ledger_text(*, extra_nets: bool = False) -> str:
    b = LedgerBuilder("SLOT-PRESSURE-RELIEF" if extra_nets else "SLOT-PRESSURE-TIGHT")
    for tank in ("A", "B", "C"):
        b.tank(tank, 4 if extra_nets or tank != "C" else 2)
    b.net("N1", "A", "JIA")
    b.net("N2", "B", "YI")
    b.fish("F1", "JIA", "WHITE", "A", "09:00", net="N1")
    b.fish("F2", "YI", "PLAIN", "B", "09:01", net="N2")
    if extra_nets:
        b.net("N3", "C", "BING")
        b.net("N4", "C", "DING")
        b.fish("F3", "BING", "WHITE", "C", "09:02", net="N3")
        b.fish("F4", "DING", "WHITE", "C", "09:03", net="N4")
    else:
        # C has only two private slots, already needed by N1/N2 when they
        # arrive.  F3/F4 therefore remain in open water and are same-spot mixed.
        b.fish("F3", "BING", "WHITE", "C", "09:02")
        b.fish("F4", "DING", "WHITE", "C", "09:03")
    b.shift_net("E1", "N1", "A", "C", "10:00")
    b.shift_net("E2", "N2", "B", "C", "10:05")
    b.count("C", 4, "10:10")
    b.shift_net("E3", "N1", "C", "A", "11:00")
    b.shift_net("E4", "N2", "C", "B", "11:05")
    b.count("A", 1, "11:10")
    b.count("B", 1, "11:11")
    b.count("C", 2, "11:12")
    return b.render()
