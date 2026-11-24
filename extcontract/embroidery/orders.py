"""Counter orders, single-mode workshop work orders, and delivery control.

Two and only two repair trades exist, and a workshop practises exactly one:

``CUT``      local thread cutting.  The middle of the target segment is pulled
             out after a fixing knot secures each of its two ends.  Every knot
             occupies one loop (绳扣) of its hole and shows its knot head on one
             face, so a quote needs two free loops on a face the workshop is
             permitted for and that no collector no-knot zone covers.

``UNTHREAD`` reverse unthreading.  An original thread tail must be recognised
             and reachable, and stitches are undone strictly in reverse order
             from that tail.  A target with later original stitches still on
             the screen cannot be reached; the first protected stitch that
             blocks the reverse path is named on the rejection receipt.

The counter first intakes an order against the current reconstruction.  Only a
CERTAIN target can be signed.  A workshop then quotes the order from its own
work order (its mode, permitted faces, supplies and permit travel together on
one ticket), delivers it, and the delivery note is rechecked against the
original registry.  Delivered faces are never reopened for a later order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .records import FACES, Protection, Registry, ThreadRecord
from .reconstruction import (
    AMBIGUOUS,
    CERTAIN,
    NOT_FOUND,
    Reconstruction,
    TargetResolution,
    reconstruct,
    resolve_target,
    segment_key,
)

CUT = "CUT"
UNTHREAD = "UNTHREAD"
TRADES = (CUT, UNTHREAD)

ACCEPTED = "ACCEPTED"
HELD = "HELD"
BLOCKED = "BLOCKED"
DELIVERED = "DELIVERED"


# ---------------------------------------------------------------------------
# physical screen state
# ---------------------------------------------------------------------------

@dataclass
class Panel:
    """Mutable physical state of one screen: loops, removed stitches, orders."""

    panel_id: str
    registry: Registry
    reconstruction: Reconstruction
    # hole -> face -> free loops remaining at that hole on that face
    loops: dict[str, dict[str, int]]
    removed: set[tuple[str, int]] = field(default_factory=set)
    knots: list[dict[str, Any]] = field(default_factory=list)
    delivered_orders: dict[str, "DeliveryNote"] = field(default_factory=dict)

    @classmethod
    def from_registry(
        cls,
        panel_id: str,
        registry: Registry,
        loops: dict[str, dict[str, int]],
    ) -> "Panel":
        reconstruction = reconstruct(registry)
        normalized = {
            hole: {face: int(count) for face, count in faces.items() if face in FACES}
            for hole, faces in loops.items()
        }
        return cls(
            panel_id=panel_id,
            registry=registry,
            reconstruction=reconstruction,
            loops=normalized,
        )

    def segment_exists(self, key: tuple[str, int]) -> bool:
        return key not in self.removed

    def is_protected(self, key: tuple[str, int]) -> bool:
        return self.reconstruction.is_protected(key)

    def free_loop(self, hole: str, face: str) -> int:
        return self.loops.get(hole, {}).get(face, 0)

    def take_loop(self, hole: str, face: str) -> None:
        available = self.loops.setdefault(hole, {})
        if available.get(face, 0) <= 0:
            raise RuntimeError(f"no free loop at hole {hole!r} on {face}")
        available[face] -= 1

    def thread(self, thread_id: str) -> ThreadRecord:
        return self.registry.threads[thread_id]


# ---------------------------------------------------------------------------
# counter orders and workshop work orders
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CounterOrder:
    """What the counter names: a face, a hole pair, and a colour."""

    order_id: str
    face: str
    hole_a: str
    hole_b: str
    color: str
    note: str = ""

    def resolve(self, panel: Panel) -> TargetResolution:
        return resolve_target(
            panel.reconstruction,
            face=self.face,
            hole_a=self.hole_a,
            hole_b=self.hole_b,
            color=self.color,
        )


@dataclass(frozen=True)
class WorkOrder:
    """One workshop ticket: a single trade, its permit faces and supplies."""

    workshop_id: str
    mode: str
    permitted_faces: frozenset[str]
    supplies: tuple[str, ...]
    permit: str

    def __post_init__(self) -> None:
        if self.mode not in TRADES:
            raise ValueError(f"workshop mode must be one of {TRADES}, got {self.mode!r}")
        if not self.permitted_faces or any(f not in FACES for f in self.permitted_faces):
            raise ValueError("permitted_faces must be a non-empty subset of front/back")
        if not self.supplies or not self.permit:
            raise ValueError("a work order must carry both stocked supplies and a permit")

    def to_json(self) -> dict[str, Any]:
        return {
            "workshop_id": self.workshop_id,
            "mode": self.mode,
            "permitted_faces": sorted(self.permitted_faces),
            "supplies": list(self.supplies),
            "permit": self.permit,
        }


@dataclass(frozen=True)
class Quote:
    """A workshop's feasibility decision for one counter order."""

    order_id: str
    workshop: WorkOrder
    status: str  # ACCEPTED | HELD | BLOCKED
    target_key: tuple[str, int] | None
    target_face: str | None
    reasons: list[str] = field(default_factory=list)
    blocker_key: tuple[str, int] | None = None
    plan: dict[str, Any] | None = None

    @property
    def actionable(self) -> bool:
        return self.status == ACCEPTED

    def to_json(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "workshop": self.workshop.to_json(),
            "status": self.status,
            "target": None
            if self.target_key is None
            else {"thread_id": self.target_key[0], "index": self.target_key[1],
                  "face": self.target_face},
            "blocker": None
            if self.blocker_key is None
            else {"thread_id": self.blocker_key[0], "index": self.blocker_key[1]},
            "reasons": list(self.reasons),
            "plan": None if self.plan is None else dict(self.plan),
        }


@dataclass(frozen=True)
class DeliveryNote:
    """The receipt issued when an accepted quote is executed."""

    order_id: str
    workshop_id: str
    mode: str
    removed_key: tuple[str, int]
    removed_face: str
    unthreaded_keys: tuple[tuple[str, int], ...]
    knots: tuple[dict[str, Any], ...]
    preserved: tuple[tuple[str, int], ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "workshop_id": self.workshop_id,
            "mode": self.mode,
            "removed": {"thread_id": self.removed_key[0], "index": self.removed_key[1],
                        "face": self.removed_face},
            "unthreaded": [{"thread_id": k[0], "index": k[1]} for k in self.unthreaded_keys],
            "knots": [dict(k) for k in self.knots],
            "preserved_original_stitches": [
                {"thread_id": k[0], "index": k[1]} for k in self.preserved
            ],
        }


# ---------------------------------------------------------------------------
# counter intake
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntakeDecision:
    order_id: str
    status: str  # ACCEPTED | HELD
    resolution: TargetResolution

    @property
    def target_key(self) -> tuple[str, int] | None:
        return self.resolution.key

    def to_json(self) -> dict[str, Any]:
        payload = self.resolution.to_json()
        payload["order_id"] = self.order_id
        payload["intake"] = self.status
        return payload


def intake_order(panel: Panel, order: CounterOrder) -> IntakeDecision:
    """Counter intake: sign only a certain, present, unprotected target."""
    resolution = order.resolve(panel)
    if resolution.status != CERTAIN:
        return IntakeDecision(order.order_id, HELD, resolution)

    key = resolution.key
    if not panel.segment_exists(key):
        held = TargetResolution(
            status=NOT_FOUND,
            segment=None,
            face=None,
            candidates={},
            reasons=[
                f"thread {key[0]!r} segment {key[1]} was already removed by a delivered order; "
                "a delivered embroidery face is never reopened"
            ],
        )
        return IntakeDecision(order.order_id, HELD, held)

    if panel.is_protected(key):
        held = TargetResolution(
            status=AMBIGUOUS,
            segment=resolution.segment,
            face=resolution.face,
            candidates=dict(resolution.candidates),
            reasons=[
                f"thread {key[0]!r} segment {key[1]} on the {resolution.face} is inside a "
                "collector do-not-touch circle; even a correctly located order cannot be signed"
            ],
        )
        return IntakeDecision(order.order_id, HELD, held)

    return IntakeDecision(order.order_id, ACCEPTED, resolution)


# ---------------------------------------------------------------------------
# workshop quotes
# ---------------------------------------------------------------------------

def evaluate(panel: Panel, order: CounterOrder, work_order: WorkOrder) -> Quote:
    """Return the workshop's actionable quote or a structured hold/blocker."""
    intake = intake_order(panel, order)
    if intake.status != ACCEPTED:
        return Quote(
            order_id=order.order_id,
            workshop=work_order,
            status=HELD,
            target_key=intake.target_key,
            target_face=intake.resolution.face,
            reasons=intake.resolution.reasons,
        )

    key = intake.target_key
    face = intake.resolution.face
    if work_order.mode == CUT:
        return _quote_cut(panel, order.order_id, work_order, key, face)
    return _quote_unthread(panel, order.order_id, work_order, key, face)


def _quote_cut(
    panel: Panel,
    order_id: str,
    work_order: WorkOrder,
    key: tuple[str, int],
    face: str,
) -> Quote:
    segment = panel.reconstruction.segment(key)
    holes = (segment.hole_a, segment.hole_b)

    if face not in work_order.permitted_faces:
        return Quote(
            order_id, work_order, HELD, key, face,
            [f"the knot face {face!r} is outside this workshop's permit "
             f"{sorted(work_order.permitted_faces)}"],
        )

    missing_loops = [
        hole for hole in holes if panel.free_loop(hole, face) <= 0
    ]
    forbidden = [hole for hole in holes if panel.registry.knot_forbidden(face, hole)]
    if forbidden:
        return Quote(
            order_id, work_order, HELD, key, face,
            [f"a no-knot zone forbids exposing a knot head on the {face} at hole(s) "
             f"{sorted(set(forbidden))}"],
        )
    if missing_loops:
        return Quote(
            order_id, work_order, HELD, key, face,
            [f"hole(s) {sorted(set(missing_loops))} have no free loop left on the {face}; "
             "each fixing knot needs one loop and the last loop of a shared hole is reserved "
             "by the earlier delivered order"],
        )

    plan = {
        "mode": CUT,
        "knot_face": face,
        "knots": [
            {"hole": segment.hole_a, "face": face},
            {"hole": segment.hole_b, "face": face},
        ],
        "remove_middle_between": [segment.hole_a, segment.hole_b],
    }
    return Quote(
        order_id, work_order, ACCEPTED, key, face,
        [f"two loops secured on the {face} at {segment.hole_a!r}/{segment.hole_b!r}; "
         "fix both ends, then withdraw the middle"],
        plan=plan,
    )


def _quote_unthread(
    panel: Panel,
    order_id: str,
    work_order: WorkOrder,
    key: tuple[str, int],
    face: str,
) -> Quote:
    thread_id, target_index = key
    thread = panel.thread(thread_id)

    # Reverse unthreading starts at the recognised original tail and walks the
    # recorded needle road in reverse; only stitches still on the screen are
    # traversed.  The registry invariant guarantees the tail is the last hole.
    remaining_later = [
        (thread_id, index)
        for index in range(target_index + 1, len(thread.segments))
        if panel.segment_exists((thread_id, index))
    ]
    for later_key in remaining_later:
        if panel.is_protected(later_key):
            return Quote(
                order_id, work_order, BLOCKED, key, face,
                [f"reverse unthreading from tail {thread.tail_hole!r} reaches protected thread "
                 f"{later_key[0]!r} segment {later_key[1]} before the target; the original "
                 "stitch must be preserved, so the target cannot be undone"],
                blocker_key=later_key,
            )

    reverse_order = list(reversed(remaining_later)) + [key]
    plan = {
        "mode": UNTHREAD,
        "tail_hole": thread.tail_hole,
        "reverse_order": [
            {"thread_id": stitch_key[0], "index": stitch_key[1]}
            for stitch_key in reverse_order
        ],
    }
    return Quote(
        order_id, work_order, ACCEPTED, key, face,
        [f"tail {thread.tail_hole!r} is reachable; {len(reverse_order)} stitch(es) will be undone "
         "in strict reverse order"],
        plan=plan,
    )


# ---------------------------------------------------------------------------
# delivery and original-stitch recheck
# ---------------------------------------------------------------------------

def deliver(panel: Panel, order: CounterOrder, work_order: WorkOrder) -> DeliveryNote:
    """Execute an accepted quote and freeze the delivery on the panel."""
    if order.order_id in panel.delivered_orders:
        raise RuntimeError(
            f"order {order.order_id!r} is already delivered; delivered faces are not reopened"
        )
    quote = evaluate(panel, order, work_order)
    if not quote.actionable:
        raise RuntimeError(
            f"order {order.order_id!r} cannot be delivered by {work_order.workshop_id!r}: "
            + "; ".join(quote.reasons)
        )

    key = quote.target_key
    face = quote.target_face
    segment = panel.reconstruction.segment(key)
    knots: list[dict[str, Any]] = []
    unthreaded: list[tuple[str, int]] = []

    if work_order.mode == CUT:
        knot_face = quote.plan["knot_face"]
        for hole in (segment.hole_a, segment.hole_b):
            panel.take_loop(hole, knot_face)
            knot = {"hole": hole, "face": knot_face, "order_id": order.order_id}
            panel.knots.append(knot)
            knots.append(knot)
        panel.removed.add(key)
    else:
        # Execute exactly the quote's reverse-order plan: every listed stitch
        # is still on the screen at quote time, and undone original stitches
        # can no longer be claimed as preserved.
        for step in quote.plan["reverse_order"]:
            suffix_key = (step["thread_id"], step["index"])
            if not panel.segment_exists(suffix_key):
                raise RuntimeError(
                    f"unthread plan targets missing stitch thread {suffix_key[0]!r} "
                    f"segment {suffix_key[1]}"
                )
            panel.removed.add(suffix_key)
            unthreaded.append(suffix_key)

    preserved = tuple(
        segment_key(seg.thread_id, seg.index)
        for seg in panel.registry.all_segments()
        if panel.segment_exists(segment_key(seg.thread_id, seg.index))
    )
    note = DeliveryNote(
        order_id=order.order_id,
        workshop_id=work_order.workshop_id,
        mode=work_order.mode,
        removed_key=key,
        removed_face=face,
        unthreaded_keys=tuple(unthreaded),
        knots=tuple(knots),
        preserved=preserved,
    )
    panel.delivered_orders[order.order_id] = note
    return note


def _protection_segment_keys(panel: Panel) -> list[tuple[Protection, tuple[str, int]]]:
    out: list[tuple[Protection, tuple[str, int]]] = []
    for protection in panel.registry.protections:
        thread = panel.registry.threads[protection.thread_id]
        for segment in thread.segments:
            if (segment.hole_a, segment.hole_b) == (protection.hole_a, protection.hole_b) or (
                segment.hole_b,
                segment.hole_a,
            ) == (protection.hole_a, protection.hole_b):
                out.append((protection, segment_key(thread.thread_id, segment.index)))
    return out


def recheck_originals(panel: Panel, note: DeliveryNote) -> dict[str, Any]:
    """Recheck the delivered note against the still-held original registry.

    Every preserved collector circle must still be on the screen with its
    original thread; every removed segment - including stitches undone on the
    reverse unthread path - must actually be gone.
    """
    violations: list[str] = []

    held_circles: list[dict[str, Any]] = []
    for protection, key in _protection_segment_keys(panel):
        if panel.segment_exists(key):
            held_circles.append(
                {
                    "thread_id": protection.thread_id,
                    "holes": [protection.hole_a, protection.hole_b],
                    "face": protection.protected_face,
                }
            )
        else:
            violations.append(
                f"protected original stitch thread {protection.thread_id!r} segment {key[1]} "
                f"({protection.hole_a}-{protection.hole_b}) is missing"
            )

    gone = list(dict.fromkeys([note.removed_key, *note.unthreaded_keys]))
    for key in gone:
        if panel.segment_exists(key):
            violations.append(
                f"delivery claims thread {key[0]!r} segment {key[1]} was removed but it remains"
            )

    for knot in note.knots:
        if panel.free_loop(knot["hole"], knot["face"]) < 0:
            violations.append(f"loop accounting broken at {knot['hole']!r}/{knot['face']}")

    return {
        "order_id": note.order_id,
        "ok": not violations,
        "removed_confirmed": [
            {"thread_id": key[0], "index": key[1]} for key in gone
        ],
        "preserved_circles_held": held_circles,
        "violations": violations,
    }
