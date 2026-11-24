"""Typed embroidery registry rows recovered from *published effective rows*.

The ingestion library owns revision resolution: by the time we look at
``library.publish()`` every ``row_id`` appears once, at its highest accepted
revision.  This module turns those flat TEXT rows into typed needle-road
records and performs the semantic validation that the permissive TEXT contract
intentionally does not.

Faces are the two sides of a double-sided screen: ``"front"`` (正面) and
``"back"`` (背面).  A segment lies on exactly one face; passing through a hole
switches face.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..errors import ContractError

FRONT = "front"
BACK = "back"
FACES = (FRONT, BACK)
OTHER_FACE = {FRONT: BACK, BACK: FRONT}


class RegistryError(ContractError):
    """A published registry row is structurally unusable."""


@dataclass(frozen=True)
class Segment:
    """One needle segment of a thread, lying on exactly one face.

    ``sequence`` is the full-screen flow sequence: across the whole piece, no
    two segments share it, and it determines last-covering precedence when
    same-coloured segments overlap between the same two holes.
    """

    thread_id: str
    color: str
    index: int  # 0-based position within the thread
    hole_a: str
    hole_b: str
    sequence: int

    @property
    def holes(self) -> tuple[str, str]:
        return self.hole_a, self.hole_b


@dataclass(frozen=True)
class ThreadRecord:
    """A physical thread: ordered holes, its segment flow, and optional start."""

    thread_id: str
    color: str
    holes: tuple[str, ...]
    segments: tuple[Segment, ...]
    start_face: str | None  # None == starting-end face not recorded
    tail_hole: str

    def segment_at(self, index: int) -> Segment:
        return self.segments[index]

    def face_of(self, start_face: str, index: int) -> str:
        """Face of segment ``index`` given the thread's starting-end face."""
        face = start_face
        for _ in range(index):
            face = OTHER_FACE[face]
        return face


@dataclass(frozen=True)
class Observation:
    """A side view: one coloured segment exists between two holes on a face."""

    row_id: str
    color: str
    hole_a: str
    hole_b: str
    observed_face: str

    def matches(self, segment: Segment, face: str) -> bool:
        return (
            segment.color == self.color
            and face == self.observed_face
            and _same_hole_pair((segment.hole_a, segment.hole_b), (self.hole_a, self.hole_b))
        )


@dataclass(frozen=True)
class Protection:
    """A collector circle marking an original stitch that must be preserved."""

    row_id: str
    thread_id: str
    hole_a: str
    hole_b: str
    protected_face: str


@dataclass(frozen=True)
class NoKnotZone:
    """A face region (set of hole positions) on which knot heads are banned."""

    row_id: str
    no_knot_face: str
    zone_holes: frozenset[str]

    def forbids(self, face: str, hole: str) -> bool:
        return face == self.no_knot_face and hole in self.zone_holes


@dataclass
class Registry:
    """All effective rows for one embroidery piece."""

    piece_id: str
    threads: dict[str, ThreadRecord] = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)
    protections: list[Protection] = field(default_factory=list)
    no_knot_zones: list[NoKnotZone] = field(default_factory=list)
    row_revisions: dict[str, int] = field(default_factory=dict)

    def all_segments(self) -> list[Segment]:
        return [seg for thread in self.threads.values() for seg in thread.segments]

    def is_protected(self, thread_id: str, holes: tuple[str, str], face: str) -> bool:
        return any(
            p.thread_id == thread_id
            and p.protected_face == face
            and _same_hole_pair((p.hole_a, p.hole_b), holes)
            for p in self.protections
        )

    def knot_forbidden(self, face: str, hole: str) -> bool:
        return any(zone.forbids(face, hole) for zone in self.no_knot_zones)


# ---------------------------------------------------------------------------
# parsing from a publish() snapshot
# ---------------------------------------------------------------------------

def registry_from_publish(published: dict[str, Any], *, piece_id: str | None = None) -> Registry:
    """Parse a ``reader-records/v1`` snapshot into a :class:`Registry`.

    All rows must share one ``piece_id`` (or be filtered to the given one).
    Rows missing columns (legally null under the TEXT contract) are reported as
    registry errors rather than silently dropped.
    """
    if not isinstance(published, dict) or published.get("format") != "reader-records/v1":
        raise RegistryError("expected a reader-records/v1 publish() snapshot")

    rows = published.get("rows", [])
    chosen_piece = piece_id
    parsed: list[tuple[str, int, dict[str, Any]]] = []
    for row in rows:
        values = row.get("values", {})
        row_piece = values.get("piece_id")
        if chosen_piece is None and row_piece is not None:
            chosen_piece = row_piece
        if row_piece != chosen_piece:
            continue
        try:
            revision = int(row["revision"])
        except (KeyError, TypeError, ValueError):
            revision = 1
        parsed.append((row["row_id"], revision, values))

    registry = Registry(piece_id=chosen_piece or "")
    for row_id, revision, values in parsed:
        record_type = values.get("record_type")
        handler = _ROW_HANDLERS.get(record_type)
        if handler is None:
            raise RegistryError(
                f"row {row_id!r}: unknown record_type {record_type!r}; expected one of "
                f"THREAD / OBSERVATION / PROTECT / NOKNOT"
            )
        handler(registry, row_id, values)
        registry.row_revisions[row_id] = revision

    _validate_cross_rows(registry)
    return registry


def _handle_thread(registry: Registry, row_id: str, v: dict[str, Any]) -> None:
    thread_id = _require(v, "thread_id", row_id)
    if thread_id in registry.threads:
        raise RegistryError(f"row {row_id!r}: duplicate thread_id {thread_id!r}")
    color = _require(v, "color", row_id)
    holes = _require_json_list(v, "holes_json", row_id, str)
    if len(holes) < 2:
        raise RegistryError(f"row {row_id!r}: thread {thread_id!r} must pierce at least two holes")
    if len(set(holes)) != len(holes):
        raise RegistryError(f"row {row_id!r}: thread {thread_id!r} revisits a hole: {holes}")
    sequences = _require_json_list(v, "seq_json", row_id, int)
    if len(sequences) != len(holes) - 1:
        raise RegistryError(
            f"row {row_id!r}: thread {thread_id!r} has {len(holes) - 1} segments but "
            f"{len(sequences)} flow sequences"
        )
    if any(seq <= 0 for seq in sequences):
        raise RegistryError(f"row {row_id!r}: flow sequences must be positive integers")
    start_face = v.get("start_face")
    if start_face is not None and start_face not in FACES:
        raise RegistryError(f"row {row_id!r}: start_face {start_face!r} is not front/back")
    tail_hole = v.get("tail_hole")
    if tail_hole is not None and tail_hole != holes[-1]:
        raise RegistryError(
            f"row {row_id!r}: tail_hole {tail_hole!r} does not match last hole {holes[-1]!r}"
        )
    segments = tuple(
        Segment(
            thread_id=thread_id,
            color=color,
            index=i,
            hole_a=holes[i],
            hole_b=holes[i + 1],
            sequence=sequences[i],
        )
        for i in range(len(holes) - 1)
    )
    registry.threads[thread_id] = ThreadRecord(
        thread_id=thread_id,
        color=color,
        holes=tuple(holes),
        segments=segments,
        start_face=start_face,
        tail_hole=holes[-1],
    )


def _handle_observation(registry: Registry, row_id: str, v: dict[str, Any]) -> None:
    registry.observations.append(
        Observation(
            row_id=row_id,
            color=_require(v, "color", row_id),
            hole_a=_require(v, "hole_a", row_id),
            hole_b=_require(v, "hole_b", row_id),
            observed_face=_require_face(v, "observed_face", row_id),
        )
    )


def _handle_protection(registry: Registry, row_id: str, v: dict[str, Any]) -> None:
    registry.protections.append(
        Protection(
            row_id=row_id,
            thread_id=_require(v, "thread_id", row_id),
            hole_a=_require(v, "hole_a", row_id),
            hole_b=_require(v, "hole_b", row_id),
            protected_face=_require_face(v, "protected_face", row_id),
        )
    )


def _handle_noknot(registry: Registry, row_id: str, v: dict[str, Any]) -> None:
    zone_holes = _require_json_list(v, "zone_holes_json", row_id, str)
    registry.no_knot_zones.append(
        NoKnotZone(
            row_id=row_id,
            no_knot_face=_require_face(v, "no_knot_face", row_id),
            zone_holes=frozenset(zone_holes),
        )
    )


_ROW_HANDLERS = {
    "THREAD": _handle_thread,
    "OBSERVATION": _handle_observation,
    "PROTECT": _handle_protection,
    "NOKNOT": _handle_noknot,
}


def _validate_cross_rows(registry: Registry) -> None:
    seen_sequences: dict[int, str] = {}
    for segment in registry.all_segments():
        owner = seen_sequences.get(segment.sequence)
        if owner is not None:
            raise RegistryError(
                f"flow sequence {segment.sequence} is used by both thread {owner!r} and "
                f"thread {segment.thread_id!r}; full-screen sequences must be unique"
            )
        seen_sequences[segment.sequence] = segment.thread_id
    for protection in registry.protections:
        if protection.thread_id not in registry.threads:
            raise RegistryError(
                f"protection row {protection.row_id!r}: unknown thread {protection.thread_id!r}"
            )
        thread = registry.threads[protection.thread_id]
        if not any(
            _same_hole_pair((s.hole_a, s.hole_b), (protection.hole_a, protection.hole_b))
            for s in thread.segments
        ):
            raise RegistryError(
                f"protection row {protection.row_id!r}: thread {protection.thread_id!r} has no "
                f"segment between {protection.hole_a!r} and {protection.hole_b!r}"
            )


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _same_hole_pair(a: tuple[str, str], b: tuple[str, str]) -> bool:
    return (a[0] == b[0] and a[1] == b[1]) or (a[0] == b[1] and a[1] == b[0])


def _require(values: dict[str, Any], key: str, row_id: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise RegistryError(f"row {row_id!r}: missing or empty {key}")
    return value


def _require_face(values: dict[str, Any], key: str, row_id: str) -> str:
    value = _require(values, key, row_id)
    if value not in FACES:
        raise RegistryError(f"row {row_id!r}: {key} {value!r} is not front/back")
    return value


def _require_json_list(values: dict[str, Any], key: str, row_id: str, item_type: type) -> list:
    raw = values.get(key)
    if not isinstance(raw, str):
        raise RegistryError(f"row {row_id!r}: missing {key}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"row {row_id!r}: {key} is not JSON: {exc}") from None
    if not isinstance(parsed, list) or not parsed or any(not isinstance(x, item_type) for x in parsed):
        raise RegistryError(f"row {row_id!r}: {key} must be a non-empty JSON list of {item_type.__name__}")
    if item_type is int and any(isinstance(x, bool) for x in parsed):
        raise RegistryError(f"row {row_id!r}: {key} must contain integers, not booleans")
    return parsed
