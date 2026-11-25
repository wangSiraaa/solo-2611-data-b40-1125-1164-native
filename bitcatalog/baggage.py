"""Toy-terminal oversized-baggage carousel built on the bit catalog and warehouse.

This module is the executable answer to the baggage-carousel handoff problem.
It deliberately keeps four concerns separate:

* a tiny three-message bit catalog (toothed advance, switch move-in, tag read);
* replay of the original warehouse frames, retaining product and real frame ids;
* mutually exclusive whole-ring layouts when one read window covers two labels;
* numbered rack allocation and whole-group delivery against paper group tickets.

The delivery policy implemented here is the **numbered group rack** option from
the prompt.  A slot can never be promised or occupied twice, and a bag is never
counted twice on one group.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .catalog import CatalogProduct, build_product
from .warehouse import ObservationWarehouse

BUS = "toy-terminal.baggage"
STEP_FRAME = 1
SWITCH_FRAME = 2
READ_FRAME = 3
BEAT_NS = 1_000_000_000
ORDER_NS = 1_000_000

RING_CODES = {"A": 0, "B": 1}
CODE_RINGS = {v: k for k, v in RING_CODES.items()}


class BaggageError(ValueError):
    """Domain rule violation."""

    def __init__(self, message: str, *, code: str = "baggage_error", **detail: Any):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Small bit catalog
# ---------------------------------------------------------------------------


def baggage_message_definitions() -> list[dict[str, Any]]:
    """The complete small catalog: exactly three message definitions."""

    return [
        {
            "bus": BUS,
            "frame_id": STEP_FRAME,
            "payload_bytes": 2,
            "signals": [
                # Two rings are enough for the requested rerun.
                {"name": "ring_code", "start": 0, "width": 2, "order": "little"},
                # Actual tooth count; the simulator never assumes it is one.
                {"name": "teeth", "start": 2, "width": 4, "order": "little"},
            ],
        },
        {
            "bus": BUS,
            "frame_id": SWITCH_FRAME,
            "payload_bytes": 2,
            "signals": [
                {"name": "bag_no", "start": 0, "width": 4, "order": "little"},
                {"name": "switch_code", "start": 4, "width": 2, "order": "little"},
                {"name": "from_ring_code", "start": 6, "width": 1, "order": "little"},
                {"name": "to_ring_code", "start": 7, "width": 1, "order": "little"},
            ],
        },
        {
            "bus": BUS,
            "frame_id": READ_FRAME,
            "payload_bytes": 4,
            "selector": {
                "name": "read_kind",
                "start": 0,
                "width": 4,
                "order": "little",
            },
            "signals": [
                {"name": "ring_code", "start": 4, "width": 2, "order": "little"},
                {"name": "cell", "start": 8, "width": 3, "order": "little"},
                # selector 1: one exact bag number.
                {
                    "name": "exact_bag_no",
                    "start": 16,
                    "width": 4,
                    "order": "little",
                    "branches": [1],
                },
                # selector 2: the active six-bit set of labels covered by one
                # read window.  Two set bits mean two candidate labels, not two
                # simultaneously established positions.
                {
                    "name": "pair_mask",
                    "start": 16,
                    "width": 6,
                    "order": "little",
                    "branches": [2],
                },
                # An old branch-1 hint.  It is intentionally inactive for a
                # selector-2 covered read; dirty bits here must never be used
                # to guess a bag number.
                {
                    "name": "branch1_hint",
                    "start": 24,
                    "width": 4,
                    "order": "little",
                    "branches": [1],
                },
            ],
        },
    ]


def make_product() -> CatalogProduct:
    return build_product(baggage_message_definitions())


# ---------------------------------------------------------------------------
# Topology and exact ring simulation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Port:
    id: str
    ring: str
    cell: int
    # Inclusive beat set.  ``None`` means open for every simulated beat.
    open_beats: frozenset[int] | None = None

    def is_open(self, beat: int) -> bool:
        return self.open_beats is None or beat in self.open_beats


@dataclass(frozen=True)
class Switch:
    id: str
    code: int
    cells: dict[str, int]


@dataclass(frozen=True)
class Topology:
    rings: dict[str, int]
    ports: dict[str, Port]
    switches: dict[str, Switch]

    def port_at(self, ring: str, cell: int) -> Port | None:
        for port in self.ports.values():
            if port.ring == ring and port.cell == cell:
                return port
        return None

    def switch_by_code(self, code: int) -> Switch:
        for switch in self.switches.values():
            if switch.code == code:
                return switch
        raise BaggageError(f"unknown switch_code {code}", code="unknown_switch")


def canonical_topology() -> Topology:
    """The prompt's four-cell ring with its unique opening at cell zero."""

    return Topology(
        rings={"A": 4},
        ports={"P0": Port("P0", "A", 0, frozenset(range(0, 6)))},
        switches={},
    )


def two_ring_topology() -> Topology:
    """Two four-cell rings, two openings and one A2<->B2 switch."""

    return Topology(
        rings={"A": 4, "B": 4},
        ports={
            "PA": Port("PA", "A", 0, frozenset(range(0, 6))),
            "PB": Port("PB", "B", 0, frozenset(range(0, 6))),
        },
        switches={"X1": Switch("X1", 0, {"A": 2, "B": 2})},
    )


Layout = dict[int, tuple[str, int]]


def occupant_map(layout: Layout) -> dict[tuple[str, int], int]:
    return {(ring, cell): bag for bag, (ring, cell) in layout.items()}


def ring_arrays(layout: Layout, topology: Topology) -> dict[str, list[int | None]]:
    out: dict[str, list[int | None]] = {}
    occupied = occupant_map(layout)
    for ring, size in topology.rings.items():
        out[ring] = [occupied.get((ring, cell)) for cell in range(size)]
    return out


def advance_ring(layout: Layout, topology: Topology, ring: str, teeth: int) -> Layout:
    if ring not in topology.rings:
        raise BaggageError(f"unknown ring {ring!r}", code="unknown_ring")
    if teeth < 0:
        raise BaggageError("teeth must be non-negative", code="bad_teeth")
    size = topology.rings[ring]
    return {
        bag: (r, (cell + teeth) % size if r == ring else cell)
        for bag, (r, cell) in layout.items()
    }


def switch_bag(
    layout: Layout,
    topology: Topology,
    bag: int,
    switch_code: int,
    from_ring: str,
    to_ring: str,
) -> Layout:
    if bag not in layout:
        raise BaggageError(f"bag {bag} is not on a ring", code="bag_not_on_ring")
    switch = topology.switch_by_code(switch_code)
    actual_ring, cell = layout[bag]
    if actual_ring != from_ring:
        raise BaggageError(
            f"bag {bag} reported from {from_ring}, actually on {actual_ring}",
            code="bad_switch_origin",
        )
    if cell != switch.cells.get(from_ring):
        raise BaggageError(
            f"bag {bag} at {actual_ring}{cell}, not at switch {switch.id}",
            code="not_at_switch",
            bag=bag,
            cell=cell,
            required=switch.cells.get(from_ring),
        )
    if to_ring not in switch.cells or from_ring not in switch.cells:
        raise BaggageError(
            f"switch {switch.id} does not connect {from_ring}->{to_ring}",
            code="bad_switch_route",
        )
    target_cell = switch.cells[to_ring]
    for other, (other_ring, other_cell) in layout.items():
        if other != bag and other_ring == to_ring and other_cell == target_cell:
            raise BaggageError(
                f"switch target {to_ring}{target_cell} occupied by bag {other}",
                code="switch_target_occupied",
            )
    new_layout = dict(layout)
    new_layout[bag] = (to_ring, target_cell)
    return new_layout


# ---------------------------------------------------------------------------
# Frame construction and warehouse replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimedFrame:
    beat: int
    order: int
    event: dict[str, Any]


def _event(event_id: str, frame_id: int, beat: int, order: int, payload: bytes) -> TimedFrame:
    return TimedFrame(
        beat=beat,
        order=order,
        event={
            "frame_event_id": event_id,
            "bus": BUS,
            "frame_id": frame_id,
            # order keeps same-beat read/switch/step frames in source order.
            "occurred_at_ns": beat * BEAT_NS + order * ORDER_NS,
            "payload_hex": payload.hex(),
        },
    )


def encode_step(product: CatalogProduct, event_id: str, beat: int, ring: str, teeth: int, order: int = 50) -> TimedFrame:
    msg = product.message(BUS, STEP_FRAME)
    encoded = msg.encode({"ring_code": RING_CODES[ring], "teeth": teeth})
    return _event(event_id, STEP_FRAME, beat, order, encoded.payload)


def encode_switch(
    product: CatalogProduct,
    event_id: str,
    beat: int,
    bag: int,
    switch_code: int,
    from_ring: str,
    to_ring: str,
    order: int = 20,
) -> TimedFrame:
    msg = product.message(BUS, SWITCH_FRAME)
    encoded = msg.encode(
        {
            "bag_no": bag,
            "switch_code": switch_code,
            "from_ring_code": RING_CODES[from_ring],
            "to_ring_code": RING_CODES[to_ring],
        }
    )
    return _event(event_id, SWITCH_FRAME, beat, order, encoded.payload)


def encode_exact_read(
    product: CatalogProduct,
    event_id: str,
    beat: int,
    ring: str,
    cell: int,
    bag: int,
    order: int = 0,
) -> TimedFrame:
    msg = product.message(BUS, READ_FRAME)
    encoded = msg.encode(
        {
            "ring_code": RING_CODES[ring],
            "cell": cell,
            "exact_bag_no": bag,
            "branch1_hint": 0,
        },
        selector=1,
    )
    return _event(event_id, READ_FRAME, beat, order, encoded.payload)


def encode_covered_read(
    product: CatalogProduct,
    event_id: str,
    beat: int,
    ring: str,
    cell: int,
    candidate_bags: Iterable[int],
    *,
    dirty_inactive_hint: int | None = 15,
    order: int = 4,
) -> TimedFrame:
    bags = tuple(candidate_bags)
    mask = sum(1 << (bag - 1) for bag in bags)
    msg = product.message(BUS, READ_FRAME)
    encoded = msg.encode(
        {"ring_code": RING_CODES[ring], "cell": cell, "pair_mask": mask},
        selector=2,
    )
    body = bytearray(encoded.payload)
    if dirty_inactive_hint is not None:
        # Deliberately write the branch-1-only hint bits while selector=2.
        # A conforming replay must not expose or use this nibble as a bag no.
        body[3] = (body[3] & 0xF0) | (dirty_inactive_hint & 0x0F)
    return _event(event_id, READ_FRAME, beat, order, bytes(body))


@dataclass(frozen=True)
class StoredBaggageFrame:
    event_id: str
    frame_id: int
    beat: int
    order: int
    product_sha256: str
    payload_hex: str
    selector: int | None
    raw: dict[str, int]


def ingest_timed(
    warehouse: ObservationWarehouse,
    product: CatalogProduct,
    frames: Iterable[TimedFrame],
) -> None:
    timed = list(frames)
    warehouse.ingest(product, [frame.event for frame in timed])


def load_stored_frames(
    warehouse: ObservationWarehouse,
    product: CatalogProduct,
    frames: Iterable[TimedFrame],
) -> list[StoredBaggageFrame]:
    out: list[StoredBaggageFrame] = []
    for frame in sorted(frames, key=lambda f: (f.beat, f.order, f.event["frame_event_id"])):
        event_id = frame.event["frame_event_id"]
        stored = warehouse.frame(product, event_id)
        if stored.status != "ok":
            raise BaggageError(
                f"{event_id} rejected by original warehouse: {stored.error_code}",
                code=stored.error_code or "rejected",
            )
        out.append(
            StoredBaggageFrame(
                event_id=event_id,
                frame_id=stored.frame_id,
                beat=frame.beat,
                order=frame.order,
                product_sha256=product.fingerprint,
                payload_hex=stored.payload_hex,
                selector=stored.selector,
                raw={name: raw for name, (raw, _value) in stored.signals.items()},
            )
        )
    return out


def parse_command(frame: StoredBaggageFrame) -> dict[str, Any]:
    """Interpret only active raw signals returned by the original warehouse."""

    raw = frame.raw
    if frame.frame_id == STEP_FRAME:
        return {
            "kind": "step",
            "event_id": frame.event_id,
            "beat": frame.beat,
            "order": frame.order,
            "ring": CODE_RINGS[raw["ring_code"]],
            "teeth": raw["teeth"],
        }
    if frame.frame_id == SWITCH_FRAME:
        return {
            "kind": "switch",
            "event_id": frame.event_id,
            "beat": frame.beat,
            "order": frame.order,
            "bag": raw["bag_no"],
            "switch_code": raw["switch_code"],
            "from_ring": CODE_RINGS[raw["from_ring_code"]],
            "to_ring": CODE_RINGS[raw["to_ring_code"]],
        }
    if frame.frame_id == READ_FRAME:
        base = {
            "kind": "read",
            "event_id": frame.event_id,
            "beat": frame.beat,
            "order": frame.order,
            "read_kind": frame.selector,
            "ring": CODE_RINGS[raw["ring_code"]],
            "cell": raw["cell"],
            # Explicitly record that inactive branch bits were not read.
            "inactive_branch_bits_used": False,
        }
        if frame.selector == 1:
            if "exact_bag_no" not in raw or "branch1_hint" not in raw:
                raise BaggageError("selector-1 read is missing active branch-1 signals")
            return {**base, "read_type": "exact", "bag": raw["exact_bag_no"]}
        if frame.selector == 2:
            if "pair_mask" not in raw:
                raise BaggageError("selector-2 read is missing pair_mask")
            if "exact_bag_no" in raw or "branch1_hint" in raw:
                raise BaggageError("inactive branch-1 signal leaked into observations")
            candidates = tuple(bag for bag in range(1, 7) if raw["pair_mask"] & (1 << (bag - 1)))
            return {**base, "read_type": "covered_pair", "candidates": candidates}
        raise BaggageError(f"unsupported read_kind {frame.selector}", code="bad_selector")
    raise BaggageError(f"unknown baggage frame_id {frame.frame_id}", code="unknown_message")


def commands_for(frames: Iterable[StoredBaggageFrame], beat: int, kind: str | None = None) -> list[dict[str, Any]]:
    commands = [parse_command(frame) for frame in frames if frame.beat == beat]
    commands.sort(key=lambda c: c["order"] if "order" in c else 0)
    if kind is not None:
        commands = [c for c in commands if c["kind"] == kind]
    return commands


# ---------------------------------------------------------------------------
# Ambiguous whole-ring handoff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutAlternative:
    alt_id: str
    ambiguity_id: str
    placements: tuple[tuple[int, str, int], ...]
    source_event_id: str

    def layout(self) -> Layout:
        return {bag: (ring, cell) for bag, ring, cell in self.placements}


@dataclass(frozen=True)
class Handoff:
    product_sha256: str
    source_event_ids: tuple[str, ...]
    alternatives: tuple[LayoutAlternative, ...]
    port_options: dict[int, tuple[str, ...]]
    port_passes: dict[str, dict[int, tuple[dict[str, Any], ...]]]
    exclusive_warning: str

    def as_dict(self, topology: Topology) -> dict[str, Any]:
        return {
            "product_sha256": self.product_sha256,
            "source_event_ids": list(self.source_event_ids),
            "exclusive_warning": self.exclusive_warning,
            "alternatives": [
                {
                    "alt_id": alt.alt_id,
                    "ambiguity_id": alt.ambiguity_id,
                    "source_event_id": alt.source_event_id,
                    "rings": ring_arrays(alt.layout(), topology),
                    "placements": [
                        {"bag": bag, "ring": ring, "cell": cell}
                        for bag, ring, cell in alt.placements
                    ],
                }
                for alt in self.alternatives
            ],
            "port_options": {str(bag): list(ports) for bag, ports in sorted(self.port_options.items())},
            "port_passes": {
                alt_id: {
                    str(bag): list(passes)
                    for bag, passes in sorted(bag_passes.items())
                }
                for alt_id, bag_passes in self.port_passes.items()
            },
        }


def build_covered_pair_alternatives(
    known_layout: Layout,
    topology: Topology,
    read_command: dict[str, Any],
    *,
    ambiguity_id: str = "AMB-1",
) -> tuple[LayoutAlternative, ...]:
    """Return the two exclusive whole-ring arrangements for a covered pair.

    The read window starts at the reported cell and also covers the next cell
    in the ring's forward direction.  The active ``pair_mask`` supplies the two
    candidate labels; their left/right order is the occluded piece.
    """

    if read_command["read_type"] != "covered_pair":
        raise BaggageError("expected covered_pair read", code="bad_read")
    ring = read_command["ring"]
    first_cell = read_command["cell"]
    candidates = tuple(read_command["candidates"])
    if len(candidates) != 2:
        raise BaggageError("this handoff expects exactly two covered labels", code="bad_pair")
    size = topology.rings[ring]
    second_cell = (first_cell + 1) % size

    alternatives: list[LayoutAlternative] = []
    for index, ordered in enumerate((candidates, candidates[::-1])):
        layout = dict(known_layout)
        layout[ordered[0]] = (ring, first_cell)
        layout[ordered[1]] = (ring, second_cell)
        placements = tuple(
            (bag, pos[0], pos[1]) for bag, pos in sorted(layout.items())
        )
        alternatives.append(
            LayoutAlternative(
                alt_id=f"{ambiguity_id}-L{index + 1}",
                ambiguity_id=ambiguity_id,
                placements=placements,
                source_event_id=read_command["event_id"],
            )
        )
    return tuple(alternatives)


def project_ports(
    alternative: LayoutAlternative,
    topology: Topology,
    commands: Iterable[dict[str, Any]],
    *,
    horizon: int = 4,
) -> dict[int, tuple[dict[str, Any], ...]]:
    """Project every port each bag passes under one exclusive arrangement."""

    by_beat: dict[int, list[dict[str, Any]]] = {}
    for command in commands:
        by_beat.setdefault(command["beat"], []).append(command)

    layout = alternative.layout()
    passes: dict[int, list[dict[str, Any]]] = {bag: [] for bag in layout}
    for beat in range(horizon):
        for bag, (ring, cell) in sorted(layout.items()):
            port = topology.port_at(ring, cell)
            if port is not None and port.is_open(beat):
                passes[bag].append({"beat": beat, "port": port.id})

        # The beat's reads have already happened.  Switch before that beat's
        # tooth advance in our declared toy-terminal phase order.
        for command in by_beat.get(beat, []):
            if command["kind"] == "switch":
                layout = switch_bag(
                    layout,
                    topology,
                    command["bag"],
                    command["switch_code"],
                    command["from_ring"],
                    command["to_ring"],
                )
        for command in by_beat.get(beat, []):
            if command["kind"] == "step":
                layout = advance_ring(layout, topology, command["ring"], command["teeth"])
    return {bag: tuple(events) for bag, events in passes.items()}


def make_handoff(
    product: CatalogProduct,
    topology: Topology,
    alternatives: tuple[LayoutAlternative, ...],
    stored_frames: list[StoredBaggageFrame],
    *,
    horizon: int = 4,
) -> Handoff:
    commands = [parse_command(frame) for frame in stored_frames]
    port_passes: dict[str, dict[int, tuple[dict[str, Any], ...]]] = {}
    options: dict[int, set[str]] = {}
    source_ids: list[str] = []
    for alt in alternatives:
        passes = project_ports(alt, topology, commands, horizon=horizon)
        port_passes[alt.alt_id] = passes
        for bag, events in passes.items():
            options.setdefault(bag, set()).update(event["port"] for event in events)
        source_ids.append(alt.source_event_id)

    # Invariant: every alternative is a complete arrangement with the same
    # physical bag count.  Flattening them would invent extra bags.
    counts = {len(alt.placements) for alt in alternatives}
    if len(counts) != 1:
        raise BaggageError("alternatives do not describe the same bag set", code="bad_handoff")
    return Handoff(
        product_sha256=product.fingerprint,
        source_event_ids=tuple(sorted(set(source_ids))),
        alternatives=alternatives,
        port_options={bag: tuple(sorted(ports)) for bag, ports in sorted(options.items())},
        port_passes={
            alt_id: {str(bag): events for bag, events in bag_passes.items()}
            for alt_id, bag_passes in port_passes.items()
        },
        exclusive_warning=(
            "互斥整圈摆法只能分别推演；不得把不同摆法中的同号袋合并计数，"
            "也不得把两个候选身份当成同时存在的两袋。"
        ),
    )


# ---------------------------------------------------------------------------
# Group tickets, numbered racks and delivery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupTicket:
    group_id: str
    passenger: str
    bag_numbers: tuple[int, ...]
    depart_beat: int

    def __post_init__(self) -> None:
        if len(self.bag_numbers) != len(set(self.bag_numbers)):
            raise BaggageError(
                f"ticket {self.group_id} lists a bag more than once",
                code="duplicate_ticket_bag",
            )

    @property
    def bag_set(self) -> frozenset[int]:
        return frozenset(self.bag_numbers)


@dataclass(frozen=True)
class SlotDecision:
    beat: int
    slot: str
    bag: int
    group_id: str
    allowed: bool
    status: str
    reason: str | None = None


class RackBank:
    def __init__(self, slots_by_port: dict[str, list[str]]):
        self.slots_by_port = {port: list(slots) for port, slots in slots_by_port.items()}
        self.slot_port = {
            slot: port for port, slots in slots_by_port.items() for slot in slots
        }
        self.occupied_by_bag: dict[int, str] = {}
        self.bag_by_slot: dict[str, int] = {}
        self.promises: dict[str, tuple[int, str, int]] = {}
        self.events: list[dict[str, Any]] = []

    def _log(self, decision: SlotDecision) -> None:
        self.events.append(
            {
                "beat": decision.beat,
                "slot": decision.slot,
                "bag": decision.bag,
                "group": decision.group_id,
                "allowed": decision.allowed,
                "status": decision.status,
                "reason": decision.reason,
            }
        )

    def contest_same_slot(
        self,
        beat: int,
        slot: str,
        requests: Iterable[tuple[int, str, int]],
    ) -> tuple[SlotDecision, list[SlotDecision]]:
        """Choose one winner for an unoccupied slot.

        Request tuple is ``(bag, group_id, depart_beat)``.  Urgency wins, then
        stable group and bag ids.  The slot is promised only to the winner.
        """

        ranked = sorted(requests, key=lambda r: (r[2], r[1], r[0]))
        if not ranked:
            raise BaggageError("empty slot contest", code="bad_contest")
        winner_bag, winner_group, winner_depart = ranked[0]
        winner = self.reserve(beat, slot, winner_bag, winner_group, winner_depart)
        losers: list[SlotDecision] = []
        for bag, group, depart in ranked[1:]:
            decision = SlotDecision(
                beat=beat,
                slot=slot,
                bag=bag,
                group_id=group,
                allowed=False,
                status="contest_lost",
                reason=f"priority went to bag {winner_bag} / group {winner_group} (depart beat {winner_depart})",
            )
            losers.append(decision)
            self._log(decision)
        return winner, losers

    def reserve(
        self,
        beat: int,
        slot: str,
        bag: int,
        group_id: str,
        depart_beat: int,
    ) -> SlotDecision:
        if slot not in self.slot_port:
            decision = SlotDecision(beat, slot, bag, group_id, False, "unknown_slot", "slot is not numbered in this rack")
            self._log(decision)
            return decision
        occupant = self.bag_by_slot.get(slot)
        if occupant is not None and occupant != bag:
            decision = SlotDecision(
                beat, slot, bag, group_id, False, "slot_occupied_denied",
                f"slot is already physically occupied by bag {occupant}",
            )
            self._log(decision)
            return decision
        promised = self.promises.get(slot)
        if promised is not None and promised[0] != bag:
            decision = SlotDecision(
                beat, slot, bag, group_id, False, "slot_already_promised_denied",
                f"slot was already promised to bag {promised[0]}",
            )
            self._log(decision)
            return decision
        self.promises[slot] = (bag, group_id, depart_beat)
        decision = SlotDecision(beat, slot, bag, group_id, True, "promised")
        self._log(decision)
        return decision

    def arrive(self, beat: int, port: str, bag: int, group_id: str) -> SlotDecision:
        existing_slot = self.occupied_by_bag.get(bag)
        if existing_slot is not None:
            decision = SlotDecision(beat, existing_slot, bag, group_id, True, "already_racked")
            self._log(decision)
            return decision

        promised_slot: str | None = None
        for slot, (promised_bag, _group, _depart) in self.promises.items():
            if promised_bag == bag:
                promised_slot = slot
                break
        if promised_slot is not None:
            slot = promised_slot
            if slot in self.bag_by_slot:
                raise BaggageError("promised slot unexpectedly occupied", code="double_slot")
        else:
            for candidate in self.slots_by_port[port]:
                if candidate not in self.bag_by_slot and candidate not in self.promises:
                    slot = candidate
                    break
            else:
                raise BaggageError(f"no free rack slot at {port}", code="rack_full", port=port)

        self.bag_by_slot[slot] = bag
        self.occupied_by_bag[bag] = slot
        self.promises.pop(slot, None)
        decision = SlotDecision(beat, slot, bag, group_id, True, "racked")
        self._log(decision)
        return decision

    def release_bag(self, bag: int) -> str | None:
        slot = self.occupied_by_bag.pop(bag, None)
        if slot is not None:
            self.bag_by_slot.pop(slot, None)
        return slot

    def occupancy(self) -> dict[str, int | None]:
        return {slot: self.bag_by_slot.get(slot) for slot in sorted(self.slot_port)}


class DeliveryDesk:
    def __init__(self, tickets: Iterable[GroupTicket], rack: RackBank):
        self.tickets = {ticket.group_id: ticket for ticket in tickets}
        self.rack = rack
        self.delivered_groups: set[str] = set()
        self.delivered_bags: set[int] = set()
        self.events: list[dict[str, Any]] = []

    def deliver_if_complete(self, beat: int, group_id: str) -> dict[str, Any]:
        ticket = self.tickets[group_id]
        if group_id in self.delivered_groups:
            event = {
                "beat": beat,
                "group": group_id,
                "allowed": False,
                "status": "duplicate_delivery_blocked",
                "message": "group was already handed over; same bags cannot count twice",
            }
            self.events.append(event)
            return event
        missing = [bag for bag in ticket.bag_numbers if bag not in self.rack.occupied_by_bag]
        if missing:
            event = {
                "beat": beat,
                "group": group_id,
                "allowed": False,
                "status": "not_complete",
                "missing": missing,
                "depart_beat": ticket.depart_beat,
            }
            self.events.append(event)
            return event
        if beat > ticket.depart_beat:
            event = {
                "beat": beat,
                "group": group_id,
                "allowed": False,
                "status": "missed_departure",
                "depart_beat": ticket.depart_beat,
            }
            self.events.append(event)
            return event

        slots = [self.rack.release_bag(bag) for bag in ticket.bag_numbers]
        self.delivered_groups.add(group_id)
        self.delivered_bags.update(ticket.bag_numbers)
        event = {
            "beat": beat,
            "group": group_id,
            "passenger": ticket.passenger,
            "allowed": True,
            "status": "delivered",
            "bags": list(ticket.bag_numbers),
            "slots": slots,
            "depart_beat": ticket.depart_beat,
            "message": f"{ticket.passenger} 凭 {group_id} 组单领齐 {list(ticket.bag_numbers)} 号袋",
        }
        self.events.append(event)
        return event

    def wrong_claim_attempt(
        self,
        beat: int,
        claimant: str,
        presented_group_id: str,
        presented_bags: Iterable[int],
        target_bag: int,
    ) -> dict[str, Any]:
        allowed_set = frozenset(presented_bags)
        if target_bag in self.delivered_bags:
            status = "duplicate_bag_blocked"
            message = f"{target_bag} 号袋已经交付，不能再领一次"
            allowed = False
        elif target_bag not in allowed_set:
            status = "misclaim_blocked"
            message = f"{claimant} 的 {presented_group_id} 组单不含 {target_bag} 号袋，拒绝误领"
            allowed = False
        else:
            status = "claim_allowed"
            message = "claim matches the presented group ticket"
            allowed = True
        event = {
            "beat": beat,
            "claimant": claimant,
            "presented_group": presented_group_id,
            "target_bag": target_bag,
            "allowed": allowed,
            "status": status,
            "message": message,
        }
        self.events.append(event)
        return event


# ---------------------------------------------------------------------------
# Scenario results and canonical examples
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    case_id: str
    title: str
    policy: str
    topology: dict[str, Any]
    deadline_beat: int | None
    complete_beat: int | None
    delivered: bool
    tickets: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    handoff: Handoff | None = None
    checks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        topology = _topology_from_dict(self.topology)
        handoff = self.handoff.as_dict(topology) if self.handoff is not None else None
        return {
            "case_id": self.case_id,
            "title": self.title,
            "policy": self.policy,
            "topology": self.topology,
            "deadline_beat": self.deadline_beat,
            "complete_beat": self.complete_beat,
            "delivered": self.delivered,
            "tickets": self.tickets,
            "rows": self.rows,
            "events": self.events,
            "handoff": handoff,
            "checks": self.checks,
        }


def _topology_from_dict(raw: dict[str, Any]) -> Topology:
    rings = raw["rings"]
    ports = {
        pid: Port(pid, p["ring"], p["cell"], frozenset(p.get("open_beats", [])) or None)
        for pid, p in raw.get("ports", {}).items()
    }
    switches = {
        sid: Switch(sid, s["code"], dict(s["cells"]))
        for sid, s in raw.get("switches", {}).items()
    }
    return Topology(rings=dict(rings), ports=ports, switches=switches)


def topology_dict(topology: Topology) -> dict[str, Any]:
    return {
        "rings": dict(topology.rings),
        "ports": {
            pid: {
                "ring": port.ring,
                "cell": port.cell,
                "open_beats": sorted(port.open_beats) if port.open_beats is not None else None,
            }
            for pid, port in topology.ports.items()
        },
        "switches": {
            sid: {"code": switch.code, "cells": dict(switch.cells)}
            for sid, switch in topology.switches.items()
        },
    }


def run_toothed_case(
    warehouse: ObservationWarehouse,
    product: CatalogProduct,
    *,
    case_id: str,
    title: str,
    right_cell: int,
    depart_beat: int,
    horizon: int = 4,
) -> CaseResult:
    topology = canonical_topology()
    initial: Layout = {1: ("A", 0), 2: ("A", right_cell)}
    ticket = GroupTicket("TWIN", "左/右板旅客", (1, 2), depart_beat)

    timed: list[TimedFrame] = []
    for beat in range(horizon):
        timed.append(encode_step(product, f"{case_id}-STEP-A-{beat}", beat, "A", 1))
    ingest_timed(warehouse, product, timed)
    stored = load_stored_frames(warehouse, product, timed)

    layout = dict(initial)
    collected: set[int] = set()
    complete_beat: int | None = None
    rows: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    for beat in range(horizon):
        before = ring_arrays(layout, topology)
        port_bags = [
            bag
            for bag, (ring, cell) in layout.items()
            if topology.port_at(ring, cell) is not None
            and topology.port_at(ring, cell).is_open(beat)  # type: ignore[union-attr]
        ]
        for bag in port_bags:
            collected.add(bag)
            layout.pop(bag, None)
            events.append({"beat": beat, "status": "recognized_at_open_port", "bag": bag})
        if len(collected) == len(ticket.bag_set) and complete_beat is None:
            complete_beat = beat
            events.append(
                {
                    "beat": beat,
                    "status": "group_complete",
                    "group": ticket.group_id,
                    "bags": sorted(collected),
                }
            )

        commands = [parse_command(frame) for frame in stored if frame.beat == beat]
        teeth_used: list[int] = []
        for command in commands:
            if command["kind"] == "step":
                teeth_used.append(command["teeth"])
                layout = advance_ring(layout, topology, command["ring"], command["teeth"])
        rows.append(
            {
                "beat": beat,
                "before_advance": before,
                "port_bags": port_bags,
                "collected": sorted(collected),
                "actual_teeth_from_frames": teeth_used,
                "after_advance": ring_arrays(layout, topology),
            }
        )

    delivered = complete_beat is not None and complete_beat <= depart_beat
    if not delivered:
        events.append(
            {
                "beat": depart_beat,
                "status": "missed_departure",
                "depart_beat": depart_beat,
                "complete_beat": complete_beat,
                "message": f"旅客已于第 {depart_beat} 拍离厅，整组未交齐",
            }
        )
    return CaseResult(
        case_id=case_id,
        title=title,
        policy="simple-open-port (canonical timing check; rack policy used in two-ring case)",
        topology=topology_dict(topology),
        deadline_beat=depart_beat,
        complete_beat=complete_beat,
        delivered=delivered,
        tickets=[
            {
                "group_id": ticket.group_id,
                "passenger": ticket.passenger,
                "bag_numbers": list(ticket.bag_numbers),
                "depart_beat": ticket.depart_beat,
            }
        ],
        rows=rows,
        events=events,
        checks=[
            "第 0 拍为开场可领拍",
            "前进格数取自仓库中的实际 teeth 原始值，不按拍数想当然",
        ],
    )


def run_two_ring_rack_case(
    warehouse: ObservationWarehouse,
    product: CatalogProduct,
) -> CaseResult:
    case_id = "TWO-RING-RACK"
    topology = two_ring_topology()
    tickets = [
        GroupTicket("T1", "林岚", (1, 2), depart_beat=2),
        GroupTicket("T2", "陈默", (3, 4), depart_beat=3),
        GroupTicket("T3", "周禾", (5, 6), depart_beat=1),
    ]
    ticket_by_bag = {bag: ticket for ticket in tickets for bag in ticket.bag_numbers}

    # Physical truth used by the toy simulator.  The service does not infer bag
    # ids from this truth at a port; exact read frames perform recognition.
    truth: Layout = {
        1: ("A", 0),
        2: ("A", 2),
        3: ("A", 3),
        4: ("B", 1),
        5: ("B", 0),
        6: ("B", 3),
    }

    timed: list[TimedFrame] = []
    # Beat 0 reads: four exact anchors and one covered pair at A2/A3.
    timed.extend(
        [
            encode_exact_read(product, f"{case_id}-R1", 0, "A", 0, 1, order=0),
            encode_exact_read(product, f"{case_id}-R4", 0, "B", 1, 4, order=1),
            encode_exact_read(product, f"{case_id}-R5", 0, "B", 0, 5, order=2),
            encode_exact_read(product, f"{case_id}-R6", 0, "B", 3, 6, order=3),
            encode_covered_read(product, f"{case_id}-R23", 0, "A", 2, (2, 3), order=4),
        ]
    )
    # Beat 1 resolves the pair and recognizes the B-ring arrival.
    timed.extend(
        [
            encode_exact_read(product, f"{case_id}-R3-T1", 1, "A", 0, 3, order=0),
            encode_exact_read(product, f"{case_id}-R6-T1", 1, "B", 0, 6, order=1),
            encode_switch(product, f"{case_id}-SW4", 1, 4, switch_code=0, from_ring="B", to_ring="A", order=20),
        ]
    )
    timed.append(encode_exact_read(product, f"{case_id}-R2-T2", 2, "A", 0, 2, order=0))
    timed.append(encode_exact_read(product, f"{case_id}-R4-T3", 3, "A", 0, 4, order=0))
    for beat in range(3):
        timed.append(encode_step(product, f"{case_id}-STEP-A-{beat}", beat, "A", 1))
        timed.append(encode_step(product, f"{case_id}-STEP-B-{beat}", beat, "B", 1))

    ingest_timed(warehouse, product, timed)
    stored = load_stored_frames(warehouse, product, timed)
    stored_by_id = {frame.event_id: frame for frame in stored}

    beat0_reads = [parse_command(stored_by_id[f"{case_id}-R23"])]
    covered = beat0_reads[0]
    known_layout = {
        1: ("A", 0),
        4: ("B", 1),
        5: ("B", 0),
        6: ("B", 3),
    }
    alternatives = build_covered_pair_alternatives(known_layout, topology, covered)
    handoff = make_handoff(product, topology, alternatives, stored, horizon=4)

    rack = RackBank({"PA": ["PA-01", "PA-02", "PA-03"], "PB": ["PB-01", "PB-02", "PB-03"]})
    desk = DeliveryDesk(tickets, rack)

    events: list[dict[str, Any]] = []
    winner, losers = rack.contest_same_slot(
        beat=0,
        slot="PA-03",
        requests=[
            (2, "T1", 2),
            (4, "T2", 3),
        ],
    )
    contest_events = [asdict_slot(winner), *[asdict_slot(item) for item in losers]]
    events.extend(contest_events)

    layout = dict(truth)
    rows: list[dict[str, Any]] = []
    resolved_alternative: str | None = None

    for beat in range(4):
        start_arrays = ring_arrays(layout, topology)
        beat_events: list[dict[str, Any]] = []
        if beat == 0:
            beat_events.extend(contest_events)
        read_commands = commands_for(stored, beat, "read")

        # Reads first.  Exact port reads cause physical removal into a rack.
        for command in sorted(read_commands, key=lambda c: c["event_id"]):
            if command["read_type"] == "exact":
                bag = command["bag"]
                reported_pos = (command["ring"], command["cell"])
                if layout.get(bag) != reported_pos:
                    raise BaggageError(
                        f"stored read {command['event_id']} disagrees with replayed ring",
                        code="read_replay_mismatch",
                    )
                if beat == 1 and bag == 3 and resolved_alternative is None:
                    resolved_alternative = "AMB-1-L1"
                    beat_events.append(
                        {
                            "beat": beat,
                            "status": "ambiguity_resolved",
                            "message": "第1拍 PA 精确读到3号：保留 AMB-1-L1，排除互斥的 AMB-1-L2",
                        }
                    )
                port = topology.port_at(command["ring"], command["cell"])
                if port is not None and port.is_open(beat):
                    ticket = ticket_by_bag[bag]
                    decision = rack.arrive(beat, port.id, bag, ticket.group_id)
                    beat_events.append(asdict_slot(decision))
                    if decision.allowed and decision.status in {"racked", "already_racked"}:
                        # A racked bag has left the moving ring.
                        layout.pop(bag, None)
            else:
                beat_events.append(
                    {
                        "beat": beat,
                        "status": "covered_read_handoff",
                        "event_id": command["event_id"],
                        "candidates": list(command["candidates"]),
                        "message": "只形成互斥整圈摆法，不据此提前拣袋",
                    }
                )

        # A same-beat audit: once bag 3 occupies PA-02, that slot cannot be
        # promised to bag 4.
        if beat == 1:
            denied = rack.reserve(beat, "PA-02", 4, "T2", 3)
            beat_events.append(asdict_slot(denied))
            wrong = desk.wrong_claim_attempt(
                beat,
                claimant="赵误",
                presented_group_id="T2",
                presented_bags=(3, 4),
                target_bag=6,
            )
            beat_events.append(wrong)

        # Deliver any now-complete group after all same-beat reads.
        for ticket in tickets:
            if all(bag in rack.occupied_by_bag for bag in ticket.bag_numbers):
                beat_events.append(desk.deliver_if_complete(beat, ticket.group_id))

        # Switch phase: only a stored switch frame at the physical switch cell
        # can change rings.
        for command in commands_for(stored, beat, "switch"):
            layout = switch_bag(
                layout,
                topology,
                command["bag"],
                command["switch_code"],
                command["from_ring"],
                command["to_ring"],
            )
            beat_events.append(
                {
                    "beat": beat,
                    "status": "switch_moved",
                    "bag": command["bag"],
                    "route": f"{command['from_ring']}->{{{command['to_ring']}}}",
                    "event_id": command["event_id"],
                }
            )

        # Toothed advance uses actual decoded teeth.
        step_info: list[dict[str, Any]] = []
        for command in commands_for(stored, beat, "step"):
            before = ring_arrays(layout, topology)
            layout = advance_ring(layout, topology, command["ring"], command["teeth"])
            step_info.append(
                {"ring": command["ring"], "teeth": command["teeth"], "before": before[command["ring"]]}
            )

        events.extend(beat_events)
        rows.append(
            {
                "beat": beat,
                "rings_at_beat_start": start_arrays,
                "step_frames": step_info,
                "rings_after_phase": ring_arrays(layout, topology),
                "rack_occupancy": rack.occupancy(),
                "rack_promises": {slot: value[0] for slot, value in sorted(rack.promises.items())},
                "events": beat_events,
            }
        )

    # Post-delivery duplicate attempt.
    duplicate = desk.deliver_if_complete(4, "T1")
    events.append(duplicate)

    delivered_groups = set(desk.delivered_groups)
    delivered = delivered_groups == {ticket.group_id for ticket in tickets}
    complete_beat = max(
        event["beat"] for event in desk.events if event.get("status") == "delivered"
    )
    checks = [
        "读标 selector=2 时，仓库观测中没有 exact_bag_no/branch1_hint；脏分支位未参与袋号判断",
        "AMB-1-L1 与 AMB-1-L2 互斥；每份摆法都恰有6袋，未把候选合并成12袋",
        "4号袋第0拍齿步后到 B2，第1拍凭道岔帧 B2→A2；不在道岔格的换环会被 switch_bag 拒绝",
        "PA-03 争用按离厅拍优先给 T1/2号袋；PA-02 被3号袋占住后拒绝再许给4号袋",
        "每组袋号唯一；重复交 T1 组被 duplicate_delivery_blocked 拦截",
        "赵误持 T2 组单索6号袋被 misclaim_blocked 拦截",
    ]
    return CaseResult(
        case_id=case_id,
        title="两圈、两开口、六袋：互斥摆法交接 + 编号整组架交付",
        policy="numbered-group-rack",
        topology=topology_dict(topology),
        deadline_beat=max(ticket.depart_beat for ticket in tickets),
        complete_beat=complete_beat,
        delivered=delivered,
        tickets=[
            {
                "group_id": ticket.group_id,
                "passenger": ticket.passenger,
                "bag_numbers": list(ticket.bag_numbers),
                "depart_beat": ticket.depart_beat,
            }
            for ticket in tickets
        ],
        rows=rows,
        events=events,
        handoff=handoff,
        checks=checks,
    )


def asdict_slot(decision: SlotDecision) -> dict[str, Any]:
    return {
        "beat": decision.beat,
        "slot": decision.slot,
        "bag": decision.bag,
        "group": decision.group_id,
        "allowed": decision.allowed,
        "status": decision.status,
        "reason": decision.reason,
    }


# ---------------------------------------------------------------------------
# End-to-end rerun and Markdown report
# ---------------------------------------------------------------------------


def run_demo(out_dir: str | Path) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    db_path = out / "baggage.db"
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db_path) + suffix)
        if path.exists():
            path.unlink()

    product = make_product()
    product_path = out / "baggage_catalog.json"
    product.save(product_path)

    results: list[CaseResult] = []
    with ObservationWarehouse(db_path) as warehouse:
        results.append(
            run_toothed_case(
                warehouse,
                product,
                case_id="CANONICAL-DEFAULT",
                title="例1：左0右2，留到第2拍可领齐",
                right_cell=2,
                depart_beat=2,
            )
        )
        results.append(
            run_toothed_case(
                warehouse,
                product,
                case_id="RIGHT-AT-ONE",
                title="例2：右袋改在1号格，第3拍到口",
                right_cell=1,
                depart_beat=3,
            )
        )
        results.append(
            run_toothed_case(
                warehouse,
                product,
                case_id="EARLY-DEPARTURE",
                title="例3：恢复0/2但第1拍离厅，交不齐",
                right_cell=2,
                depart_beat=1,
            )
        )
        results.append(run_two_ring_rack_case(warehouse, product))
        counts = warehouse.counts(product)
        frame_identities = [
            {
                "frame_event_id": stored.frame_event_id,
                "bus": stored.bus,
                "frame_id": stored.frame_id,
                "status": stored.status,
                "selector": stored.selector,
                "payload_hex": stored.payload_hex,
                "product_sha256": product.fingerprint,
            }
            for stored in warehouse.frames(product)
        ]
        stored_product_sha = product.fingerprint

    report = {
        "entry": "python3.11 tools/baggage_demo.py",
        "product_sha256": stored_product_sha,
        "product_file": str(product_path),
        "warehouse_db": str(db_path),
        "warehouse_counts": counts,
        "stored_frames": frame_identities,
        "frame_identity_note": "每帧保留 frame_event_id、bus、实 frame_id、原始 payload_hex 与绑定产品 SHA-256",
        "results": [result.as_dict() for result in results],
    }
    (out / "baggage_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    markdown = render_markdown(report)
    (out / "baggage_report.md").write_text(markdown, encoding="utf-8")
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 大件行李转盘复跑报告")
    lines.append("")
    lines.append(f"复跑入口：`{report['entry']}`")
    lines.append("")
    lines.append(f"目录产品 SHA-256：`{report['product_sha256']}`")
    lines.append(f"原仓：`{report['warehouse_db']}`；收帧计数：`{report['warehouse_counts']}`")
    lines.append("")
    lines.append(report["frame_identity_note"])
    frame_id_counts: dict[int, int] = {}
    for frame in report.get("stored_frames", []):
        frame_id_counts[frame["frame_id"]] = frame_id_counts.get(frame["frame_id"], 0) + 1
    if frame_id_counts:
        lines.append(
            "实帧号分布："
            + "；".join(f"frame_id={fid} {count} 帧" for fid, count in sorted(frame_id_counts.items()))
        )
    lines.append("")

    for result in report["results"]:
        lines.append(f"## {result['case_id']} — {result['title']}")
        lines.append("")
        outcome = "领齐/交付成功" if result["delivered"] else "未领齐（按离厅时刻判失败）"
        lines.append(
            f"策略：`{result['policy']}`；最迟/实际齐套拍："
            f"`deadline={result['deadline_beat']}, complete={result['complete_beat']}`；结论：**{outcome}**。"
        )
        topo = result["topology"]
        ring_text = "、".join(f"{ring}圈{size}格" for ring, size in topo["rings"].items())
        port_text = "；".join(
            f"{pid}@{p['ring']}{p['cell']}号格/开放拍={p['open_beats']}"
            for pid, p in topo["ports"].items()
        )
        lines.append(f"- 拓扑：{ring_text}；开口：{port_text}")
        if result.get("tickets"):
            ticket_text = "；".join(
                f"{t['group_id']}({t['passenger']})袋号{t['bag_numbers']}/离厅第{t['depart_beat']}拍"
                for t in result["tickets"]
            )
            lines.append(f"- 实物组单：{ticket_text}")
        lines.append("")
        if result.get("handoff"):
            handoff = result["handoff"]
            lines.append("### 读标遮两标签的互斥交接")
            lines.append("")
            lines.append(handoff["exclusive_warning"])
            lines.append("")
            for alt in handoff["alternatives"]:
                lines.append(f"- `{alt['alt_id']}`（来源 `{alt['source_event_id']}`）：")
                for ring, cells in alt["rings"].items():
                    lines.append(f"  - {ring} 圈：`{cells}`")
            lines.append(
                "- 各袋可经过的领取口："
                + "；".join(f"{bag}号→{ports}" for bag, ports in handoff["port_options"].items())
            )
            lines.append("")
        lines.append("### 逐拍说明")
        lines.append("")
        for row in result["rows"]:
            lines.append(f"**第 {row['beat']} 拍**")
            lines.append("")
            if "before_advance" in row:
                lines.append(f"- 推进前环上袋位：`{row['before_advance']}`")
                lines.append(f"- 开口袋：`{row['port_bags']}`；已领/已认袋：`{row['collected']}`")
                lines.append(f"- 仓库齿步：`{row['actual_teeth_from_frames']}`；推进后：`{row['after_advance']}`")
            else:
                lines.append(f"- 拍初环位：`{row['rings_at_beat_start']}`")
                if row["step_frames"]:
                    lines.append(f"- 实际齿步：`{row['step_frames']}`")
                lines.append(f"- 拍末环位：`{row['rings_after_phase']}`")
                lines.append(f"- 暂存架：`{row['rack_occupancy']}`")
                if row.get("rack_promises"):
                    lines.append(f"- 已许诺未占架位：`{row['rack_promises']}`")
                for event in row["events"]:
                    lines.append(f"  - {_event_text(event)}")
            lines.append("")
        if result.get("events") and "before_advance" in (result["rows"][0] if result["rows"] else {}):
            lines.append("事件：")
            for event in result["events"]:
                lines.append(f"- {_event_text(event)}")
            lines.append("")
        elif result.get("events"):
            last_beat = max((row["beat"] for row in result["rows"]), default=-1)
            post_events = [event for event in result["events"] if event.get("beat", -1) > last_beat]
            if post_events:
                lines.append("拍后核对事件：")
                for event in post_events:
                    lines.append(f"- {_event_text(event)}")
                lines.append("")
        if result.get("checks"):
            lines.append("核对点：")
            for check in result["checks"]:
                lines.append(f"- {check}")
            lines.append("")
    return "\n".join(lines)


def _event_text(event: dict[str, Any]) -> str:
    if "message" in event:
        base = event["message"]
    else:
        base = event.get("status", "event")
    extras = []
    for key in ("beat", "slot", "bag", "group", "missing", "reason", "route", "candidates", "claimant", "target_bag"):
        if key in event and key not in ("beat",):
            extras.append(f"{key}={event[key]}")
    if "beat" in event and not event.get("message"):
        extras.insert(0, f"beat={event['beat']}")
    if extras:
        return f"`{base}` ({', '.join(extras)})"
    return f"`{base}`"
