"""Recover every simultaneously-valid threading world from effective rows.

A thread pierces holes in its recorded order; the segment between two
consecutive holes lies on exactly one face, and every hole crossing switches
face.  When a thread's starting-end face is recorded, all its segment faces are
forced.  When it is not recorded, exactly two threadings remain (start on the
front or the back).

An observation says only that *a* segment of a given colour runs between two
holes on a given face - it never names the thread.  It is an existence claim,
not an identity claim: when two same-coloured segments fit the same view, the
one with the greatest full-screen flow sequence covers the place last, so it is
the unique owner of the observation.  The same observation is therefore never
claimed by two threads, and each world stores exactly one owner per
observation - the injective rule the registry demands.

Reconstruction enumerates the bounded unknown-start combinations (the
documented envelope is at most three threads, so at most eight combinations),
keeps those in which every observation and every collector protection circle
holds, and deduplicates worlds by their structural content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any

from .records import (
    FACES,
    OTHER_FACE,
    Registry,
    Segment,
    _same_hole_pair,
)

#: intake / resolution statuses
CERTAIN = "CERTAIN"
AMBIGUOUS = "AMBIGUOUS"
NOT_FOUND = "NOT_FOUND"
UNSATISFIABLE = "UNSATISFIABLE"


def segment_key(thread_id: str, index: int) -> tuple[str, int]:
    """Stable identity of a segment within a piece."""
    return thread_id, index


@dataclass(frozen=True)
class World:
    """One complete threading that all effective rows can hold at once."""

    start_faces: dict[str, str]
    segment_faces: dict[tuple[str, int], str]
    claims: dict[str, tuple[str, int]]  # observation row_id -> unique owner key
    protections: dict[tuple[str, int], str]  # segment key -> protected face

    def face_of(self, thread_id: str, index: int) -> str:
        return self.segment_faces[(thread_id, index)]

    def canonical(self) -> tuple:
        return (
            tuple(sorted(self.start_faces.items())),
            tuple(sorted(self.claims.items())),
        )


@dataclass
class Reconstruction:
    """Result of reconstructing one piece."""

    registry: Registry
    worlds: list[World] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def satisfiable(self) -> bool:
        return bool(self.worlds)

    def face_in_all_worlds(self, thread_id: str, index: int) -> str | None:
        """The segment face if every world agrees, otherwise ``None``."""
        if not self.worlds:
            return None
        faces = {w.face_of(thread_id, index) for w in self.worlds}
        return next(iter(faces)) if len(faces) == 1 else None

    def segment(self, key: tuple[str, int]) -> Segment:
        thread_id, index = key
        return self.registry.threads[thread_id].segment_at(index)

    def is_protected(self, key: tuple[str, int]) -> bool:
        return any(key in w.protections for w in self.worlds)

    def protection_face(self, key: tuple[str, int]) -> str | None:
        faces = {w.protections[key] for w in self.worlds if key in w.protections}
        if not faces:
            return None
        return next(iter(faces)) if len(faces) == 1 else None


def reconstruct(registry: Registry) -> Reconstruction:
    """Enumerate every threading world compatible with the effective rows."""
    result = Reconstruction(registry=registry)
    if not registry.threads:
        result.notes.append("registry contains no THREAD rows")
        return result

    unknown = [t.thread_id for t in registry.threads.values() if t.start_face is None]
    fixed = {
        t.thread_id: t.start_face
        for t in registry.threads.values()
        if t.start_face is not None
    }
    protection_map = _protection_map(registry)

    seen: set[tuple] = set()
    for choice in product(FACES, repeat=len(unknown)):
        start_faces = dict(fixed)
        start_faces.update(dict(zip(unknown, choice)))

        segment_faces = _segment_faces(registry, start_faces)

        # A protection circle is only honoured when the segment really lies on
        # the circled face in this threading; otherwise the world cannot hold.
        if not _protections_hold(segment_faces, protection_map):
            continue

        # Every observation must fit at least one segment; the last-covering
        # (max full-screen sequence) matching segment is its sole owner, so no
        # two same-coloured threads can each claim the same view.
        claims: dict[str, tuple[str, int]] = {}
        fits = True
        for observation in registry.observations:
            matching = [
                seg
                for seg in registry.all_segments()
                if observation.matches(seg, segment_faces[segment_key(seg.thread_id, seg.index)])
            ]
            if not matching:
                fits = False
                break
            owner = max(matching, key=lambda s: s.sequence)
            claims[observation.row_id] = segment_key(owner.thread_id, owner.index)
        if not fits:
            continue

        world = World(
            start_faces=dict(start_faces),
            segment_faces=dict(segment_faces),
            claims=claims,
            protections=dict(protection_map),
        )
        marker = world.canonical()
        if marker not in seen:
            seen.add(marker)
            result.worlds.append(world)

    if not result.worlds:
        result.notes.append(
            "no threading satisfies the needle roads, observations and protections together"
        )
    result.worlds.sort(key=lambda w: w.canonical())
    return result


@dataclass(frozen=True)
class TargetResolution:
    """How the counter's (face, hole pair, colour) request settles."""

    status: str  # CERTAIN / AMBIGUOUS / NOT_FOUND / UNSATISFIABLE
    segment: Segment | None
    face: str | None
    candidates: dict[tuple, str]  # segment key -> face, across worlds
    reasons: list[str]

    @property
    def key(self) -> tuple[str, int] | None:
        return segment_key(self.segment.thread_id, self.segment.index) if self.segment else None

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "segment": None
            if self.segment is None
            else {
                "thread_id": self.segment.thread_id,
                "color": self.segment.color,
                "index": self.segment.index,
                "holes": [self.segment.hole_a, self.segment.hole_b],
                "sequence": self.segment.sequence,
                "face": self.face,
            },
            "candidate_keys": [list(k) for k in sorted(self.candidates)],
            "reasons": list(self.reasons),
        }


def resolve_target(
    reconstruction: Reconstruction,
    *,
    face: str,
    hole_a: str,
    hole_b: str,
    color: str,
) -> TargetResolution:
    """Locate the counter's requested segment across every valid world.

    Same-coloured overlap rule: within one world, every fitting segment is a
    candidate and the one with the greatest full-screen flow sequence covers
    the place last, so it owns the request.  If all worlds settle on the same
    segment the resolution is CERTAIN; if some worlds contain no fitting
    segment, or different worlds settle on different owners, it is AMBIGUOUS
    and the counter can only leave the order on hold.
    """
    if face not in FACES:
        raise ValueError(f"face must be front/back, got {face!r}")
    if not reconstruction.satisfiable:
        return TargetResolution(
            status=UNSATISFIABLE,
            segment=None,
            face=None,
            candidates={},
            reasons=list(reconstruction.notes) or ["registry has no valid threading world"],
        )

    per_world: list[Segment] = []
    absent_worlds = 0
    all_candidates: dict[tuple[str, int], str] = {}
    for world in reconstruction.worlds:
        fitting = [
            seg
            for seg in reconstruction.registry.all_segments()
            if seg.color == color
            and _same_hole_pair((seg.hole_a, seg.hole_b), (hole_a, hole_b))
            and world.face_of(seg.thread_id, seg.index) == face
        ]
        if not fitting:
            absent_worlds += 1
            continue
        for seg in fitting:
            all_candidates[segment_key(seg.thread_id, seg.index)] = face
        per_world.append(max(fitting, key=lambda s: s.sequence))

    if not per_world:
        return TargetResolution(
            status=NOT_FOUND,
            segment=None,
            face=None,
            candidates={},
            reasons=[
                f"no world contains a {color} segment between {hole_a!r} and {hole_b!r} "
                f"on the {face}"
            ],
        )

    owner_keys = {segment_key(seg.thread_id, seg.index) for seg in per_world}
    if len(owner_keys) == 1 and absent_worlds == 0:
        segment = per_world[0]
        return TargetResolution(
            status=CERTAIN,
            segment=segment,
            face=face,
            candidates=all_candidates,
            reasons=[
                f"all {len(reconstruction.worlds)} world(s) settle on thread "
                f"{segment.thread_id!r} segment {segment.index} (flow sequence "
                f"{segment.sequence}); last-covering rule applied"
            ],
        )

    detail = (
        "some valid worlds contain no such segment on the named face"
        if absent_worlds
        else "valid worlds disagree on the last-covering owner"
    )
    return TargetResolution(
        status=AMBIGUOUS,
        segment=None,
        face=None,
        candidates=all_candidates,
        reasons=[
            detail
            + ": "
            + ", ".join(
                f"thread {reconstruction.segment(k).thread_id!r} segment {k[1]} "
                f"(sequence {reconstruction.segment(k).sequence})"
                for k in sorted(owner_keys)
            )
        ],
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _segment_faces(
    registry: Registry, start_faces: dict[str, str]
) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for thread in registry.threads.values():
        face = start_faces[thread.thread_id]
        for index, segment in enumerate(thread.segments):
            out[(thread.thread_id, index)] = face
            face = OTHER_FACE[face]
    return out


def _protection_map(registry: Registry) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for protection in registry.protections:
        thread = registry.threads[protection.thread_id]
        for segment in thread.segments:
            if _same_hole_pair(
                (segment.hole_a, segment.hole_b), (protection.hole_a, protection.hole_b)
            ):
                out[(thread.thread_id, segment.index)] = protection.protected_face
    return out


def _protections_hold(
    segment_faces: dict[tuple[str, int], str],
    protection_map: dict[tuple[str, int], str],
) -> bool:
    return all(segment_faces[key] == protected_face for key, protected_face in protection_map.items())
