"""Allocation, signing, box movement and shift hand-over planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Iterable

from .families import family_key
from .world import GardenWorld


@dataclass
class Applicant:
    household_id: str
    crop_family: str
    places: int = 2
    mode: str | None = None  # "land", "box", or None for planner preference
    shift: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "household_id": self.household_id,
            "crop_family": self.crop_family,
            "places": self.places,
            "mode": self.mode,
            "shift": self.shift,
        }


@dataclass
class Booking:
    order_id: str
    household_id: str
    crop_family: str
    mode: str
    lot_ids: list[str]
    shift: int
    fulfillment_shift: int
    aisle_id: str | None
    routes: dict[str, list[str]]
    moves_required: int
    status: str = "SIGNED"  # SIGNED | HANDED_OVER | CANCELLED
    boundary: list[str] = field(default_factory=list)
    box_ids: list[str] = field(default_factory=list)
    source_ids: dict[str, list[str]] = field(default_factory=dict)
    eligibility: dict[str, Any] = field(default_factory=dict)
    created_at_revisions: dict[str, Any] = field(default_factory=dict)
    handed_shift: int | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "household_id": self.household_id,
            "crop_family": self.crop_family,
            "mode": self.mode,
            "status": self.status,
            "shift": self.shift,
            "fulfillment_shift": self.fulfillment_shift,
            "boundary": list(self.boundary),
            "box_ids": list(self.box_ids),
            "lot_ids": list(self.lot_ids),
            "adjacent": True,
            "aisle_id": self.aisle_id,
            "routes": {k: list(v) for k, v in self.routes.items()},
            "moves_required": self.moves_required,
            "source_ids": {k: list(v) for k, v in self.source_ids.items()},
            "eligibility": self.eligibility,
            "revisions": self.created_at_revisions,
            "handed_shift": self.handed_shift,
            "notes": list(self.notes),
        }


class GardenPlanner:
    """Finite allocation engine for at most ten cells and three households."""

    def __init__(
        self,
        world: GardenWorld | dict[str, Any] | None = None,
        *,
        move_capacity: int = 2,
        max_households: int | None = None,
        prefer: str = "land",
        enforce_shift_capacity: bool = True,
    ) -> None:
        if world is None:
            world = GardenWorld()
        elif isinstance(world, dict):
            world = GardenWorld.from_dict(world)
        self.world = world
        self.move_capacity = move_capacity
        self.max_households = max_households or world.max_households
        self.prefer = prefer
        self.enforce_shift_capacity = enforce_shift_capacity

        self.households: dict[str, Applicant] = {}
        self.bookings: dict[str, Booking] = {}
        self.box_moves: list[dict[str, Any]] = []
        self.moves_used: dict[int, int] = {}
        self.moves_reserved: dict[int, int] = {}
        self._order_seq = 0

    @classmethod
    def from_publish(cls, snapshots: dict[str, Any], **kwargs: Any) -> "GardenPlanner":
        """Build from ``{"planting": publish(), "map": ..., "soil": ...}``."""

        from .ledger import world_from_snapshots

        return cls(world_from_snapshots(snapshots), **kwargs)

    # ------------------------------------------------------------- households

    def register_household(
        self,
        household_id: str,
        crop_family: str = "豆",
        *,
        places: int = 2,
        mode: str | None = None,
        shift: int = 1,
    ) -> Applicant:
        if household_id not in self.households:
            active_households = {
                b.household_id for b in self.bookings.values() if b.status != "CANCELLED"
            }
            known = set(self.households) | active_households
            if len(known) >= self.max_households:
                raise OverflowError(f"at most {self.max_households} households are allowed")
        applicant = Applicant(
            household_id=household_id,
            crop_family=crop_family,
            places=places,
            mode=mode,
            shift=shift,
        )
        self.households[household_id] = applicant
        return applicant

    # ------------------------------------------------------------- occupancy

    def active_bookings(self) -> list[Booking]:
        return [b for b in self.bookings.values() if b.status != "CANCELLED"]

    def booked_lots(self) -> set[str]:
        out: set[str] = set()
        for booking in self.active_bookings():
            out.update(booking.lot_ids)
        return out

    def occupancy_by_boundary(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for booking in self.active_bookings():
            if booking.mode != "land":
                continue
            for lot_id in booking.boundary or booking.lot_ids:
                out[lot_id] = {
                    "order_id": booking.order_id,
                    "household_id": booking.household_id,
                    "boundary": ";".join(booking.boundary or booking.lot_ids),
                    "status": booking.status,
                }
        return dict(sorted(out.items()))

    def occupancy_by_box(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for booking in self.active_bookings():
            if booking.mode != "box":
                continue
            for box_id in booking.box_ids or booking.lot_ids:
                out[box_id] = {
                    "order_id": booking.order_id,
                    "household_id": booking.household_id,
                    "real_box_id": box_id,
                    "status": booking.status,
                }
        return dict(sorted(out.items()))

    def lookup(self, lot_id: str) -> dict[str, Any] | None:
        resolved = self.world.resolve_lot(lot_id) or lot_id
        return self.occupancy_by_boundary().get(resolved) or self.occupancy_by_box().get(resolved)

    # ------------------------------------------------------------- capacity

    def move_slots_used(self, shift: int) -> int:
        return self.moves_used.get(shift, 0)

    def move_slots_available(self, shift: int, *, include_reserved: bool = True) -> int:
        used = self.moves_used.get(shift, 0)
        reserved = self.moves_reserved.get(shift, 0) if include_reserved else 0
        return max(0, self.move_capacity - used - reserved)

    # ------------------------------------------------------------- planning

    def candidate_groups(
        self,
        crop_family: str,
        *,
        places: int = 2,
        mode: str = "land",
        household_id: str | None = None,
        include_booked: bool = False,
    ) -> list[tuple[str, ...]]:
        wanted_kind = "cell" if mode == "land" else "box"
        booked = set() if include_booked else self.booked_lots()
        eligible = {
            e.lot_id: e
            for e in self.world.eligible_lots(crop_family, mode=mode, household_id=household_id)
            if e.lot_id not in booked
        }
        ids = sorted(eligible)
        groups: list[tuple[str, ...]] = []
        for combo in combinations(ids, places):
            if self.world.connected(combo):
                groups.append(combo)
        # Stable arranger choice: first lexicographic connected boundary/set.
        groups.sort(key=lambda g: tuple(self._sort_key(x) for x in g))
        return groups

    @staticmethod
    def _sort_key(lot_id: str) -> tuple[int, str]:
        digits = "".join(ch for ch in lot_id if ch.isdigit())
        return (int(digits) if digits else 10**9, lot_id)

    def plan(
        self,
        household_or_id: Applicant | str | None = None,
        crop_family: str | None = None,
        *,
        places: int = 2,
        mode: str | None = None,
        household_id: str | None = None,
        shift: int = 1,
        fulfillment_shift: int | None = None,
        target_aisle: str | None = None,
    ) -> dict[str, Any]:
        """Return the first feasible arrangement and all diagnostics."""

        applicant = self._as_applicant(
            household_or_id,
            crop_family=crop_family,
            places=places,
            mode=mode,
            household_id=household_id,
            shift=shift,
        )
        # A signed order is the hand-over ticket fulfilled by the following
        # shift unless the caller explicitly names a fulfillment shift.
        if fulfillment_shift is None:
            fulfillment_shift = applicant.shift + 1
        modes = self._mode_order(applicant.mode)
        booked = self.booked_lots()
        all_diagnostics: list[dict[str, Any]] = []
        feasible: list[dict[str, Any]] = []

        for candidate_mode in modes:
            assessments = {
                e.lot_id: e.to_json()
                for e in self.world.eligible_lots(
                    applicant.crop_family,
                    mode=candidate_mode,
                    household_id=applicant.household_id,
                )
            }
            groups = self.candidate_groups(
                applicant.crop_family,
                places=applicant.places,
                mode=candidate_mode,
                household_id=applicant.household_id,
            )
            aisle_usage = self._aisle_usage(fulfillment_shift)
            for group in groups:
                access = self.world.access_for_group(
                    group,
                    mode=candidate_mode,
                    target_aisle=target_aisle,
                    blocked=booked,
                    aisle_usage=aisle_usage,
                )
                moves_required = len(group) if candidate_mode == "box" else 0
                slots_left = self.move_slots_available(fulfillment_shift)
                capacity_ok = (
                    not self.enforce_shift_capacity
                    or candidate_mode == "land"
                    or moves_required <= slots_left
                )
                item = {
                    "household_id": applicant.household_id,
                    "crop_family": applicant.crop_family,
                    "mode": candidate_mode,
                    "lot_ids": list(group),
                    "adjacent": True,
                    "access": access,
                    "moves_required": moves_required,
                    "move_slots_available": slots_left,
                    "capacity_ok": capacity_ok,
                    "source_ids": {
                        lot_id: self.world.source_ids(lot_id) for lot_id in group
                    },
                    "eligible": {lot_id: assessments[lot_id] for lot_id in group},
                }
                if access["ok"] and capacity_ok:
                    feasible.append(item)
                else:
                    item["rejection_reasons"] = list(access["errors"])
                    if not capacity_ok:
                        item["rejection_reasons"].append(
                            f"needs {moves_required} carry slot(s), only {slots_left} remain "
                            f"in shift {fulfillment_shift}"
                        )
                    all_diagnostics.append(item)

        status = "SIGNABLE" if feasible else "NO_FEASIBLE_ALLOCATION"
        selected = feasible[0] if feasible else None
        reasons: list[str] = []
        if selected is None:
            reasons = self._summary_rejection_reasons(applicant, modes, all_diagnostics)
        return {
            "status": status,
            "accepted": bool(feasible),
            "applicant": applicant.to_json(),
            "fulfillment_shift": fulfillment_shift,
            "selected": selected,
            "feasible": feasible,
            "diagnostics": all_diagnostics,
            "reasons": reasons,
        }

    def _as_applicant(
        self,
        household_or_id: Applicant | str | None,
        *,
        crop_family: str | None,
        places: int,
        mode: str | None,
        household_id: str | None,
        shift: int,
    ) -> Applicant:
        if isinstance(household_or_id, Applicant):
            return household_or_id
        hid = household_or_id or household_id or f"H{len(self.households) + 1}"
        if hid in self.households:
            old = self.households[hid]
            return Applicant(
                household_id=hid,
                crop_family=crop_family or old.crop_family,
                places=places if places != 2 or crop_family is not None else old.places,
                mode=mode if mode is not None else old.mode,
                shift=shift if shift != 1 else old.shift,
            )
        return Applicant(
            household_id=hid,
            crop_family=crop_family or "豆",
            places=places,
            mode=mode,
            shift=shift,
        )

    _LAND_MODES = {"land", "field", "plot", "cell", "bed", "地", "土地", "田", "畦", "田面"}
    _BOX_MODES = {"box", "boxes", "container", "箱", "箱子", "封底箱"}

    def _mode_order(self, mode: str | None) -> list[str]:
        if mode is not None:
            folded = str(mode).strip().casefold()
            if folded in self._LAND_MODES or str(mode).strip() in self._LAND_MODES:
                return ["land"]
            if folded in self._BOX_MODES or str(mode).strip() in self._BOX_MODES:
                return ["box"]
        if mode == "land":
            return ["land"]
        if mode == "box":
            return ["box"]
        return ["land", "box"] if self.prefer == "land" else ["box", "land"]

    def _aisle_usage(self, shift: int) -> dict[str, int]:
        usage: dict[str, int] = {}
        for booking in self.active_bookings():
            if booking.fulfillment_shift != shift or not booking.aisle_id:
                continue
            usage[booking.aisle_id] = usage.get(booking.aisle_id, 0) + len(booking.lot_ids)
        return usage

    def _summary_rejection_reasons(
        self,
        applicant: Applicant,
        modes: list[str],
        diagnostics: list[dict[str, Any]],
    ) -> list[str]:
        reasons: list[str] = []
        booked = self.booked_lots()
        for mode in modes:
            eligible = self.world.eligible_lots(
                applicant.crop_family, mode=mode, household_id=applicant.household_id
            )
            free = [e.lot_id for e in eligible if e.lot_id not in booked]
            if len(free) < applicant.places:
                reasons.append(
                    f"{mode}: only {len(free)} eligible unbooked place(s) "
                    f"{free}; need {applicant.places}"
                )
            elif not self.candidate_groups(
                applicant.crop_family,
                places=applicant.places,
                mode=mode,
                household_id=applicant.household_id,
            ):
                reasons.append(f"{mode}: eligible places are not connected")
        if diagnostics:
            reasons.extend(diagnostics[0].get("rejection_reasons", []))
        # Deduplicate while preserving order.
        return list(dict.fromkeys(reasons))

    # ------------------------------------------------------------- signing

    def sign_application(
        self,
        household_or_id: Applicant | str | None = None,
        crop_family: str | None = None,
        *,
        places: int = 2,
        mode: str | None = None,
        household_id: str | None = None,
        shift: int = 1,
        fulfillment_shift: int | None = None,
        order_id: str | None = None,
        target_aisle: str | None = None,
        revisions: dict[str, Any] | None = None,
        reserve_capacity: bool = True,
    ) -> dict[str, Any]:
        applicant = self._as_applicant(
            household_or_id,
            crop_family=crop_family,
            places=places,
            mode=mode,
            household_id=household_id,
            shift=shift,
        )
        # Enforce the three-household bound before doing allocation work.
        try:
            self.register_household(
                applicant.household_id,
                applicant.crop_family,
                places=applicant.places,
                mode=applicant.mode,
                shift=applicant.shift,
            )
        except OverflowError as exc:
            return {
                "status": "REJECTED",
                "accepted": False,
                "applicant": applicant.to_json(),
                "plan": None,
                "reasons": [str(exc)],
            }
        plan = self.plan(
            applicant,
            target_aisle=target_aisle,
            fulfillment_shift=fulfillment_shift,
        )
        if not plan["accepted"]:
            return {
                "status": "REJECTED",
                "accepted": False,
                "applicant": applicant.to_json(),
                "plan": plan,
                "reasons": plan["reasons"],
            }
        selected = plan["selected"]
        assert selected is not None
        self._order_seq += 1
        oid = order_id or f"HO-{self._order_seq:03d}"
        if oid in self.bookings:
            raise ValueError(f"order_id {oid!r} already exists")
        fulfill = plan["fulfillment_shift"]
        lot_ids = selected["lot_ids"]
        booking = Booking(
            order_id=oid,
            household_id=applicant.household_id,
            crop_family=applicant.crop_family,
            mode=selected["mode"],
            lot_ids=lot_ids,
            shift=applicant.shift,
            fulfillment_shift=fulfill,
            aisle_id=selected["access"].get("aisle_id"),
            routes=selected["access"].get("routes", {}),
            moves_required=selected["moves_required"],
            boundary=list(lot_ids) if selected["mode"] == "land" else [],
            box_ids=list(lot_ids) if selected["mode"] == "box" else [],
            source_ids=selected["source_ids"],
            eligibility={lot: data["eligible"] for lot, data in selected["eligible"].items()},
            created_at_revisions=revisions or {},
        )
        self.bookings[oid] = booking
        if reserve_capacity and booking.moves_required:
            self.moves_reserved[fulfill] = (
                self.moves_reserved.get(fulfill, 0) + booking.moves_required
            )
        return {
            "status": "SIGNED",
            "accepted": True,
            "order": booking.to_json(),
            "plan": plan,
        }

    # Common concise aliases.
    apply = sign_application
    accept_application = sign_application
    book = sign_application
    check_application = plan
    preview = plan
    submit = sign_application

    def eligible_units(self, crop_family: str = "豆", **kwargs: Any):
        return [e.to_json() for e in self.world.eligible_lots(crop_family, **kwargs)]

    def available_units(self, crop_family: str = "豆", **kwargs: Any):
        booked = self.booked_lots()
        return [
            item
            for item in self.eligible_units(crop_family, **kwargs)
            if item["lot_id"] not in booked
        ]

    def query(self, lot_or_household: str | None = None) -> dict[str, Any]:
        if lot_or_household is not None:
            occupied = self.lookup(lot_or_household)
            if occupied is not None:
                return occupied
            household_orders = [
                b.to_json()
                for b in self.active_bookings()
                if b.household_id == lot_or_household
            ]
            if household_orders:
                return {"household_id": lot_or_household, "orders": household_orders}
        return self.who_can_be_served()

    # ------------------------------------------------------------- box moves

    def path_between(
        self, start: str, target: str, *, required_width: int = 1
    ) -> tuple[str, ...] | None:
        # Reuse world BFS but accept any target rather than an aisle node.
        if start == target:
            return (start,)
        queue: list[tuple[str, ...]] = [(start,)]
        seen = {start}
        while queue:
            path = queue.pop(0)
            cur = path[-1]
            for nxt in self.world._route_neighbours(cur, required_width):  # noqa: SLF001
                if nxt in seen:
                    continue
                new_path = path + (nxt,)
                if nxt == target:
                    return new_path
                seen.add(nxt)
                queue.append(new_path)
        return None

    def move_box(
        self,
        box_id: str,
        target: str | tuple[int, int] | None = None,
        *,
        to: str | tuple[int, int] | None = None,
        x: int | None = None,
        y: int | None = None,
        shift: int = 1,
        consume_slot: bool = True,
    ) -> dict[str, Any]:
        target = target if target is not None else to
        if x is not None and y is not None:
            target = (x, y)
        resolved_box = self.world.resolve_lot(box_id)
        if resolved_box is None or not self.world.lots[resolved_box].is_box:
            return {"status": "REJECTED", "reasons": [f"unknown box {box_id!r}"]}
        box_id = resolved_box
        if self.lookup(box_id):
            return {"status": "REJECTED", "reasons": [f"box {box_id!r} is already signed/handed"]}
        lot = self.world.lots[box_id]
        start_position = self.world.lot_position(box_id)

        target_position: str
        target_xy: tuple[int | None, int | None] = (None, None)
        if isinstance(target, tuple):
            tx, ty = target
            target_position = f"coord:{tx},{ty}"
            target_xy = (tx, ty)
        elif target is not None:
            target_position = str(target)
            if target_position in self.world.positions:
                tx, ty = self.world.positions[target_position]
                target_xy = (tx, ty)
            else:
                stand = self.world.lots.get(target_position)
                if stand is not None:
                    target_xy = (stand.x, stand.y)
        else:
            return {"status": "REJECTED", "reasons": ["move_box requires a target"]}

        if self.world.approved_positions and not self.world.is_approved_position(target_position):
            return {
                "status": "REJECTED",
                "box_id": box_id,
                "reasons": [f"position {target_position!r} is not an approved box stand"],
            }

        width = lot.width
        path = self.path_between(start_position, target_position, required_width=width)
        if path is None and target_xy[0] is not None:
            # Coordinate target may not be a named node; try nearest explicit
            # route node is intentionally not guessed.  Callers should record a
            # stand id and its connecting edge.
            pass
        if path is None:
            return {
                "status": "REJECTED",
                "box_id": box_id,
                "reasons": [f"no passage accommodating width {width} from {start_position} to {target_position}"],
            }
        occupied = {
            self.world.lot_position(other)
            for other, other_lot in self.world.lots.items()
            if other != box_id and other_lot.kind == "box"
        }
        if target_position in occupied:
            return {"status": "REJECTED", "box_id": box_id, "reasons": [f"target {target_position} is occupied"]}
        cost = 1 if consume_slot else 0
        available = self.move_slots_available(shift)
        if cost and cost > available:
            return {
                "status": "REJECTED",
                "box_id": box_id,
                "reasons": [f"shift {shift} has {available} move slot(s) available; needs {cost}"],
            }

        old_position = start_position
        lot.current_position = target_position
        if target_xy[0] is not None:
            lot.x, lot.y = target_xy  # type: ignore[assignment]
        self.world.rebuild_coordinate_adjacency()
        if cost:
            self.moves_used[shift] = self.moves_used.get(shift, 0) + cost
        record = {
            "box_id": box_id,
            "real_box_id": box_id,
            "from": old_position,
            "to": target_position,
            "path": list(path),
            "shift": shift,
            "slots_consumed": cost,
            "slots_remaining": self.move_slots_available(shift),
        }
        self.box_moves.append(record)
        return {"status": "MOVED", "accepted": True, "move": record}

    # ------------------------------------------------------------- hand-over

    def hand_over(self, order_id: str, *, shift: int | None = None) -> dict[str, Any]:
        booking = self.bookings.get(order_id)
        if booking is None:
            return {"status": "REJECTED", "reasons": [f"unknown order {order_id!r}"]}
        if booking.status == "HANDED_OVER":
            return {"status": "REPLAY", "order": booking.to_json()}
        actual_shift = booking.fulfillment_shift if shift is None else shift
        required = booking.moves_required
        if required and self.enforce_shift_capacity:
            # Release this order's prior reservation before checking actual.
            reserved_shift = booking.fulfillment_shift
            self.moves_reserved[reserved_shift] = max(
                0, self.moves_reserved.get(reserved_shift, 0) - required
            )
            available = self.move_capacity - self.moves_used.get(actual_shift, 0)
            if required > available:
                # Restore reservation because hand-over failed.
                self.moves_reserved[reserved_shift] = (
                    self.moves_reserved.get(reserved_shift, 0) + required
                )
                return {
                    "status": "REJECTED",
                    "order_id": order_id,
                    "reasons": [
                        f"hand-over needs {required} carry slot(s), shift {actual_shift} "
                        f"has {available}"
                    ],
                }
        if required:
            self.moves_reserved[booking.fulfillment_shift] = max(
                0, self.moves_reserved.get(booking.fulfillment_shift, 0) - required
            )
            self.moves_used[actual_shift] = self.moves_used.get(actual_shift, 0) + required
        booking.status = "HANDED_OVER"
        booking.handed_shift = actual_shift
        return {"status": "HANDED_OVER", "order": booking.to_json()}

    # ------------------------------------------------------------- queries

    def who_can_be_served(
        self,
        applicants: Iterable[Applicant | dict[str, Any] | str] | None = None,
        **plan_kwargs: Any,
    ) -> dict[str, Any]:
        if applicants is None:
            items = list(self.households.values())
        else:
            items = []
            for item in applicants:
                if isinstance(item, Applicant):
                    items.append(item)
                elif isinstance(item, dict):
                    places = item.get(
                        "places",
                        item.get("place_count", item.get("units", item.get("count", 2))),
                    )
                    items.append(
                        Applicant(
                            household_id=item.get(
                                "household_id", item.get("household", item.get("id"))
                            ),
                            crop_family=item.get("crop_family", item.get("family", "豆")),
                            places=int(places),
                            mode=item.get("mode", item.get("kind")),
                            shift=int(item.get("shift", plan_kwargs.get("shift", 1))),
                        )
                    )
                else:
                    items.append(
                        Applicant(
                            household_id=str(item),
                            crop_family=plan_kwargs.get("crop_family", "豆"),
                            places=plan_kwargs.get("places", 2),
                            mode=plan_kwargs.get("mode"),
                            shift=plan_kwargs.get("shift", 1),
                        )
                    )
        results = []
        for applicant in items:
            result = self.plan(applicant)
            results.append(
                {
                    "household_id": applicant.household_id,
                    "crop_family": applicant.crop_family,
                    "can_sign": result["accepted"],
                    "status": result["status"],
                    "selected": result["selected"]["lot_ids"] if result["selected"] else None,
                    "mode": result["selected"]["mode"] if result["selected"] else None,
                    "aisle_id": result["selected"]["access"]["aisle_id"]
                    if result["selected"]
                    else None,
                    "moves_required": result["selected"]["moves_required"]
                    if result["selected"]
                    else None,
                    "reasons": result["reasons"],
                }
            )
        return {
            "max_households": self.max_households,
            "household_count": len(set(self.households) | {b.household_id for b in self.active_bookings()}),
            "results": results,
            "boundary_occupancy": self.occupancy_by_boundary(),
            "box_occupancy": self.occupancy_by_box(),
        }

    def next_shift_report(self, shift: int = 2) -> dict[str, Any]:
        outstanding = [
            b.to_json()
            for b in self.active_bookings()
            if b.status == "SIGNED" and b.fulfillment_shift == shift
        ]
        return {
            "shift": shift,
            "move_capacity": self.move_capacity,
            "moves_used": self.moves_used.get(shift, 0),
            "moves_reserved": self.moves_reserved.get(shift, 0),
            "moves_available": self.move_slots_available(shift),
            "outstanding_orders": outstanding,
            "boundary_occupancy": self.occupancy_by_boundary(),
            "box_occupancy": self.occupancy_by_box(),
            "box_positions": {
                box_id: self.world.lot_position(box_id)
                for box_id, lot in self.world.lots.items()
                if lot.is_box
            },
        }

    def handover_order(self, order_id: str) -> dict[str, Any] | None:
        booking = self.bookings.get(order_id)
        return booking.to_json() if booking else None

    def to_json(self) -> dict[str, Any]:
        return {
            "move_capacity": self.move_capacity,
            "max_households": self.max_households,
            "households": {k: v.to_json() for k, v in self.households.items()},
            "bookings": {k: v.to_json() for k, v in self.bookings.items()},
            "box_moves": list(self.box_moves),
            "moves_used": dict(self.moves_used),
            "moves_reserved": dict(self.moves_reserved),
            "world": self.world.to_json(),
        }


# Descriptive aliases used by some task vocabularies.
SpringGarden = GardenPlanner
GardenService = GardenPlanner
Garden = GardenPlanner
AllotmentPlanner = GardenPlanner
