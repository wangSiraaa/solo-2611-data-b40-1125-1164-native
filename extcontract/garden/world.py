"""Pure garden domain model: soil lineage, two-season history and space.

This module is deliberately independent of the extcontract ingestion layer.
It can be built from plain dictionaries in tests or from the real
``publish()`` snapshots through :mod:`extcontract.garden.ledger`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable

from .families import same_family

RECENT_SEASONS = (-1, -2)  # last season, season-before-last


def exact_fraction(value: Any, *, default: Fraction | None = None) -> Fraction:
    """Parse an exact amount/composition value.

    JSON fixtures should use fraction strings (``"1/2"``).  Integers are also
    accepted; floats are rejected because soil accounting must be exact.
    """

    if value is None:
        if default is not None:
            return default
        raise ValueError("missing exact fraction")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise ValueError("boolean is not a soil quantity")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            if default is not None:
                return default
            raise ValueError("empty fraction string")
        return Fraction(text)
    raise TypeError(f"cannot use {type(value).__name__} as an exact fraction")


def json_fraction(value: Fraction | None) -> str | None:
    return None if value is None else str(value)


@dataclass
class Evidence:
    """Pointer back to a published extcontract row and its field provenance."""

    row_id: str
    revision: int
    sources: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"row_id": self.row_id, "revision": self.revision, "sources": self.sources}


@dataclass
class Lot:
    """A signable place: either remaining field soil or a named sealed box."""

    lot_id: str
    kind: str  # "cell" / "box"
    display_name: str | None = None
    x: int | None = None
    y: int | None = None
    width: int = 1
    sealed_bottom: bool = True
    approved_position: bool = True
    current_position: str | None = None

    def __post_init__(self) -> None:
        allowed = ("cell", "box", "aisle", "stand", "passage", "infrastructure")
        if self.kind not in allowed:
            raise ValueError(f"lot {self.lot_id!r}: kind must be one of {allowed}")
        if self.width <= 0:
            raise ValueError(f"lot {self.lot_id!r}: width must be positive")
        if self.kind == "box" and not self.sealed_bottom:
            raise ValueError(f"box {self.lot_id!r} must have a sealed bottom")

    @property
    def is_box(self) -> bool:
        return self.kind == "box"

    @property
    def is_signable(self) -> bool:
        return self.kind in ("cell", "box")

    def to_json(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "position": self.current_position or self.lot_id,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "sealed_bottom": self.sealed_bottom,
            "approved_position": self.approved_position,
        }


@dataclass
class Aisle:
    aisle_id: str
    allowed: bool = True
    width: int = 2
    # Maximum number of signable units that can be handed through this aisle
    # in one shift.  ``None`` means no extra frontage/capacity restriction.
    handoff_capacity: int | None = None
    # Optional whitelist of lots/stands directly served after a narrowing.
    serves: set[str] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "aisle_id": self.aisle_id,
            "allowed": self.allowed,
            "width": self.width,
            "handoff_capacity": self.handoff_capacity,
            "serves": sorted(self.serves) if self.serves is not None else None,
        }


@dataclass
class Edge:
    left: str
    right: str
    allowed: bool = True
    width: int = 2
    corridor: bool = True

    def endpoints(self) -> tuple[str, str]:
        return (self.left, self.right)

    def other(self, node: str) -> str:
        return self.right if node == self.left else self.left

    def to_json(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "allowed": self.allowed,
            "width": self.width,
            "corridor": self.corridor,
        }


@dataclass
class Eligibility:
    lot_id: str
    household_id: str | None
    crop_family: str
    eligible: bool
    sources: list[str] = field(default_factory=list)
    histories: dict[str, dict[str, str | None]] = field(default_factory=dict)
    missing_seasons: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, dict[str, Evidence]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "household_id": self.household_id,
            "crop_family": self.crop_family,
            "eligible": self.eligible,
            "sources": list(self.sources),
            "histories": self.histories,
            "missing_seasons": self.missing_seasons,
            "conflicts": self.conflicts,
            "reasons": list(self.reasons),
            "evidence": {
                source: {season: ev.to_json() for season, ev in seasons.items()}
                for source, seasons in self.evidence.items()
            },
        }


class GardenWorld:
    """Reconstructed soil lineage, crop histories and permitted spatial graph."""

    def __init__(
        self,
        *,
        season_offsets: Iterable[int] = RECENT_SEASONS,
        max_cells: int = 10,
        max_households: int = 3,
    ) -> None:
        self.season_offsets = tuple(season_offsets)
        self.max_cells = max_cells
        self.max_households = max_households

        self.lots: dict[str, Lot] = {}
        self.aisles: dict[str, Aisle] = {}
        self.edges: dict[frozenset[str], Edge] = {}
        self.adjacency: dict[str, set[str]] = {}
        # Adjacency between spatial position/stand nodes.  Box movement changes
        # which position a box occupies; soil lineage never changes here.
        self.position_adjacency: dict[str, set[str]] = {}
        self.positions: dict[str, tuple[int, int]] = {}
        # Empty means positions are not pre-declared and coordinate targets are
        # accepted by the low-level model.  Once any approved stand is listed,
        # relocation targets must be in this set.
        self.approved_positions: set[str] = set()

        # source/batch id -> season offset -> family name
        self.histories: dict[str, dict[int, str]] = {}
        self.history_evidence: dict[str, dict[int, Evidence]] = {}
        self.source_amounts: dict[str, Fraction] = {}
        self.source_origins: dict[str, str] = {}

        # lot -> source -> amount physically present in the lot
        self.arrivals: dict[str, dict[str, Fraction]] = {}
        # Explicit normalised coverage, if supplied by the division map.
        self.coverage: dict[str, dict[str, Fraction]] = {}
        self.movements: list[dict[str, Any]] = []
        self.validation_errors: list[str] = []

    # ------------------------------------------------------------------ build

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GardenWorld":
        world = cls(
            season_offsets=data.get("season_offsets", RECENT_SEASONS),
            max_cells=data.get("max_cells", 10),
            max_households=data.get("max_households", 3),
        )
        for item in data.get("sources", []):
            world.add_source(
                item["source_id"],
                amount=item.get("amount", 1),
                origin_lot=item.get("origin_lot") or item.get("from_lot"),
            )
        for item in data.get("plantings", []):
            world.add_planting(
                item.get("source_id") or item.get("batch_id"),
                item.get("season_rank", item.get("season_offset", item.get("season"))),
                item.get("crop_family") or item.get("family"),
                evidence=item.get("_evidence"),
            )
        for item in data.get("lots", []):
            world.add_lot(
                item["lot_id"],
                kind=item.get("kind", "cell"),
                display_name=item.get("display_name") or item.get("name"),
                x=item.get("x"),
                y=item.get("y"),
                width=item.get("width", 1),
                sealed_bottom=item.get("sealed_bottom", True),
                approved_position=item.get("approved_position", True),
                current_position=item.get("current_position") or item.get("position"),
            )
        for item in data.get("positions", []):
            if isinstance(item, dict):
                world.add_position(item["position_id"], item.get("x"), item.get("y"))
                if item.get("approved", item.get("allowed", True)):
                    world.approve_position(item["position_id"])
            else:
                world.add_position(item[0], item[1], item[2])
                world.approve_position(item[0])
        for position_id in data.get("approved_positions", []):
            world.approve_position(position_id)
        for a, b in data.get("adjacency", []):
            world.add_adjacency(a, b)
        for a, b in data.get("position_adjacency", []):
            world.add_position_adjacency(a, b)
        for item in data.get("edges", []):
            if isinstance(item, dict):
                world.add_edge(
                    item["left"],
                    item["right"],
                    allowed=item.get("allowed", True),
                    width=item.get("width", 2),
                    corridor=item.get("corridor", True),
                )
            else:
                world.add_edge(item[0], item[1])
        for item in data.get("aisles", []):
            if isinstance(item, str):
                world.add_aisle(item)
            else:
                world.add_aisle(
                    item["aisle_id"],
                    allowed=item.get("allowed", True),
                    width=item.get("width", 2),
                    handoff_capacity=item.get("handoff_capacity"),
                    serves=item.get("serves"),
                )
        for item in data.get("coverage", []):
            world.add_coverage(
                item.get("lot_id") or item.get("lot"),
                item.get("source_id") or item.get("source"),
                fraction=item.get("fraction"),
            )
        for item in data.get("movements", []):
            world.add_movement(
                source=item.get("source_id") or item.get("source") or item.get("batch_id"),
                amount=item["amount"],
                destination=item.get("destination_id")
                or item.get("destination")
                or item.get("to"),
                destination_kind=item.get("destination_kind") or item.get("to_kind"),
                origin_lot=item.get("origin_lot") or item.get("from_lot"),
                movement_id=item.get("movement_id") or item.get("id"),
                sealed_bottom=item.get("sealed_bottom"),
                box_name=item.get("box_name"),
            )
        world.finalize()
        return world

    def add_source(
        self, source_id: str, *, amount: Any = 1, origin_lot: str | None = None
    ) -> "GardenWorld":
        if not source_id:
            raise ValueError("source_id is required")
        self.source_amounts[source_id] = exact_fraction(amount)
        if origin_lot:
            self.source_origins[source_id] = origin_lot
        self.histories.setdefault(source_id, {})
        return self

    def add_planting(
        self,
        source_id: str,
        season_rank: Any,
        family: str,
        *,
        evidence: Evidence | dict | None = None,
    ) -> "GardenWorld":
        if source_id is None:
            raise ValueError("planting requires source_id")
        rank = int(season_rank)
        self.histories.setdefault(source_id, {})[rank] = family
        if evidence is not None:
            ev = evidence if isinstance(evidence, Evidence) else Evidence(**evidence)
            self.history_evidence.setdefault(source_id, {})[rank] = ev
        return self

    def add_lot(self, lot_id: str, kind: str = "cell", **kwargs: Any) -> "GardenWorld":
        self.lots[lot_id] = Lot(lot_id=lot_id, kind=kind, **kwargs)
        self.adjacency.setdefault(lot_id, set())
        self.arrivals.setdefault(lot_id, {})
        return self

    def add_adjacency(self, left: str, right: str) -> "GardenWorld":
        if left == right:
            return self
        self.adjacency.setdefault(left, set()).add(right)
        self.adjacency.setdefault(right, set()).add(left)
        return self

    def add_position(
        self, position_id: str, x: int | None = None, y: int | None = None
    ) -> "GardenWorld":
        if x is not None and y is not None:
            self.positions[position_id] = (int(x), int(y))
        return self

    def approve_position(self, position_id: str) -> "GardenWorld":
        self.approved_positions.add(position_id)
        return self

    def is_approved_position(self, position_id: str) -> bool:
        return not self.approved_positions or position_id in self.approved_positions

    def add_position_adjacency(self, left: str, right: str) -> "GardenWorld":
        if left == right:
            return self
        self.position_adjacency.setdefault(left, set()).add(right)
        self.position_adjacency.setdefault(right, set()).add(left)
        return self

    def lot_position(self, lot_id: str) -> str:
        lot = self.lots[lot_id]
        return lot.current_position or lot_id

    def add_edge(
        self,
        left: str,
        right: str,
        *,
        allowed: bool = True,
        width: int = 2,
        corridor: bool = True,
    ) -> "GardenWorld":
        edge = Edge(left, right, allowed=allowed, width=width, corridor=corridor)
        self.edges[frozenset((left, right))] = edge
        # Corridor edges also imply graph reachability but not necessarily
        # signable-lot adjacency; call add_adjacency for the latter.
        return self

    def add_aisle(
        self,
        aisle_id: str,
        *,
        allowed: bool = True,
        width: int = 2,
        handoff_capacity: int | None = None,
        serves: Iterable[str] | None = None,
    ) -> "GardenWorld":
        self.aisles[aisle_id] = Aisle(
            aisle_id,
            allowed=allowed,
            width=width,
            handoff_capacity=handoff_capacity,
            serves=set(serves) if serves is not None else None,
        )
        return self

    def add_coverage(self, lot_id: str, source_id: str, fraction: Any) -> "GardenWorld":
        value = exact_fraction(fraction, default=Fraction(1))
        self.coverage.setdefault(lot_id, {})[source_id] = value
        return self

    def add_movement(
        self,
        *,
        source: str,
        amount: Any,
        destination: str,
        destination_kind: str | None = None,
        origin_lot: str | None = None,
        movement_id: str | None = None,
        sealed_bottom: bool | None = None,
        box_name: str | None = None,
    ) -> "GardenWorld":
        qty = exact_fraction(amount)
        if qty <= 0:
            raise ValueError(f"movement {movement_id!r}: amount must be positive")
        if source not in self.source_amounts:
            self.add_source(source, amount=0, origin_lot=origin_lot)
        if origin_lot and source not in self.source_origins:
            self.source_origins[source] = origin_lot

        kind = destination_kind
        if kind is None:
            kind = "box" if destination in self.lots and self.lots[destination].is_box else "lot"
        if kind in ("box", "named_box"):
            if destination not in self.lots:
                self.add_lot(
                    destination,
                    kind="box",
                    display_name=box_name or destination,
                    sealed_bottom=True if sealed_bottom is None else sealed_bottom,
                )
            elif box_name and not self.lots[destination].display_name:
                self.lots[destination].display_name = box_name
        elif destination not in self.lots:
            self.add_lot(destination, kind="cell")

        self.arrivals.setdefault(destination, {}).setdefault(source, Fraction(0))
        self.arrivals[destination][source] += qty
        self.movements.append(
            {
                "movement_id": movement_id,
                "source_id": source,
                "origin_lot": origin_lot,
                "amount": str(qty),
                "destination_id": destination,
                "destination_kind": kind,
            }
        )
        return self

    # ------------------------------------------------------------- finalize

    def finalize(self) -> "GardenWorld":
        self.validation_errors = self.validate_soil_accounting()
        self.rebuild_coordinate_adjacency()
        return self

    def rebuild_coordinate_adjacency(self) -> "GardenWorld":
        """Rebuild coordinate position adjacency after a box relocation."""

        # Remove only coordinate-generated edges is unnecessary because sets are
        # additive and Manhattan neighbours remain valid spatial facts.
        positions: dict[str, tuple[int, int]] = dict(self.positions)
        for lot in self.lots.values():
            if lot.x is not None and lot.y is not None:
                pos = lot.current_position or lot.lot_id
                positions[pos] = (lot.x, lot.y)
        nodes = list(positions)
        for i, a in enumerate(nodes):
            ax, ay = positions[a]
            for b in nodes[i + 1 :]:
                bx, by = positions[b]
                if abs(ax - bx) + abs(ay - by) == 1:
                    self.add_position_adjacency(a, b)
        return self

    def validate_soil_accounting(self) -> list[str]:
        errors: list[str] = []
        totals: dict[str, Fraction] = {}
        for mv in self.movements:
            totals[mv["source_id"]] = totals.get(mv["source_id"], Fraction(0)) + exact_fraction(
                mv["amount"]
            )
        for source, declared in self.source_amounts.items():
            moved = totals.get(source, Fraction(0))
            # Sources with no movement rows are allowed when explicit coverage
            # was supplied; otherwise every declared batch must be accounted.
            if source not in totals and declared == 0:
                continue
            if moved != declared:
                errors.append(
                    f"soil batch {source!r}: destinations total {moved} but declared "
                    f"amount is {declared}"
                )
        for mv in self.movements:
            dest = mv["destination_id"]
            if dest not in self.lots:
                errors.append(f"movement {mv.get('movement_id')!r}: unknown destination {dest!r}")
                continue
            if mv["destination_kind"] in ("box", "named_box") and not self.lots[dest].is_box:
                errors.append(f"destination {dest!r} is not a named box")
        cells = [lot for lot in self.lots.values() if lot.kind == "cell"]
        if len(cells) > self.max_cells:
            errors.append(f"too many cells: {len(cells)} > {self.max_cells}")
        return errors

    def require_valid_soil_accounting(self) -> None:
        if self.validation_errors:
            raise ValueError("; ".join(self.validation_errors))

    # ------------------------------------------------------------ composition

    def composition_of(self, lot_id: str) -> dict[str, Fraction]:
        """Return normalised source composition for a cell or box."""

        if lot_id in self.coverage:
            raw = self.coverage[lot_id]
        else:
            raw = self.arrivals.get(lot_id, {})
        total = sum(raw.values(), Fraction(0))
        if total == 0:
            return {}
        return {source: amount / total for source, amount in sorted(raw.items()) if amount > 0}

    def source_ids(self, lot_id: str) -> list[str]:
        return list(self.composition_of(lot_id))

    def resolve_lot(self, identifier: str) -> str | None:
        """Resolve a stable id or a current display name to the stable id."""

        if identifier in self.lots:
            return identifier
        folded = str(identifier).strip().casefold()
        for lot_id, lot in self.lots.items():
            names = {str(lot.display_name or "").casefold(), lot_id.casefold()}
            if folded in names:
                return lot_id
            digits = "".join(ch for ch in str(identifier) if ch.isdigit())
            lot_digits = "".join(ch for ch in lot_id if ch.isdigit())
            if digits and digits == lot_digits and ("箱" in str(identifier) or lot.is_box):
                return lot_id
        return None

    # ------------------------------------------------------------ eligibility

    def assess_lot(
        self,
        lot_id: str,
        crop_family: str,
        *,
        household_id: str | None = None,
        seasons: Iterable[int] | None = None,
    ) -> Eligibility:
        offsets = tuple(seasons) if seasons is not None else self.season_offsets
        comp = self.composition_of(lot_id)
        result = Eligibility(
            lot_id=lot_id,
            household_id=household_id,
            crop_family=crop_family,
            eligible=True,
            sources=list(comp),
        )
        if lot_id not in self.lots:
            result.eligible = False
            result.reasons.append(f"unknown lot {lot_id!r}")
            return result
        if not comp:
            result.eligible = False
            result.reasons.append(f"lot {lot_id!r} has no reconstructable soil composition")
            return result

        for source in comp:
            hist = self.histories.get(source, {})
            result.histories[source] = {
                str(offset): hist.get(offset) for offset in offsets
            }
            result.evidence[source] = {
                str(offset): self.history_evidence.get(source, {}).get(offset)
                for offset in offsets
                if offset in self.history_evidence.get(source, {})
            }
            for offset in offsets:
                if offset not in hist or hist[offset] in (None, ""):
                    result.missing_seasons.append({"source_id": source, "season_rank": offset})
                    result.reasons.append(
                        f"source {source!r}: season rank {offset} is missing"
                    )
                    continue
                planted = hist[offset]
                if same_family(planted, crop_family):
                    conflict = {
                        "source_id": source,
                        "season_rank": offset,
                        "planted_family": planted,
                        "reported_family": crop_family,
                    }
                    result.conflicts.append(conflict)
                    result.reasons.append(
                        f"source {source!r} planted {planted!r} in season {offset}"
                    )
        if result.missing_seasons or result.conflicts:
            result.eligible = False
        return result

    def eligible_lots(
        self,
        crop_family: str,
        *,
        mode: str | None = None,
        household_id: str | None = None,
        seasons: Iterable[int] | None = None,
    ) -> list[Eligibility]:
        out: list[Eligibility] = []
        for lot_id in sorted(self.lots):
            lot = self.lots[lot_id]
            if mode:
                if lot.kind != ("box" if mode == "box" else "cell"):
                    continue
            elif not lot.is_signable:
                continue
            ev = self.assess_lot(
                lot_id, crop_family, household_id=household_id, seasons=seasons
            )
            if ev.eligible:
                out.append(ev)
        return out

    # -------------------------------------------------------------- geometry

    def signable_adjacency(self, left: str, right: str) -> bool:
        if right in self.adjacency.get(left, set()):
            return True
        lp = self.lot_position(left) if left in self.lots else left
        rp = self.lot_position(right) if right in self.lots else right
        if rp in self.position_adjacency.get(lp, set()):
            return True
        # Coordinate fallback using current coordinates (box moves update x/y).
        ll = self.lots.get(left)
        rl = self.lots.get(right)
        if ll and rl and None not in (ll.x, ll.y, rl.x, rl.y):
            return abs(ll.x - rl.x) + abs(ll.y - rl.y) == 1
        return False

    def connected(self, lot_ids: Iterable[str]) -> bool:
        ids = list(lot_ids)
        if len(ids) <= 1:
            return True
        wanted = set(ids)
        seen = {ids[0]}
        stack = [ids[0]]
        while stack:
            cur = stack.pop()
            for nxt in wanted:
                if nxt not in seen and self.signable_adjacency(cur, nxt):
                    seen.add(nxt)
                    stack.append(nxt)
        return seen == wanted

    def route(
        self,
        start: str,
        *,
        target_aisle: str | None = None,
        required_width: int = 1,
        blocked: set[str] | None = None,
        allowed_nodes: set[str] | None = None,
    ) -> tuple[str, ...] | None:
        """BFS a permitted route from ``start`` to an allowed aisle."""

        blocked = set(blocked or ())
        if start in blocked:
            return None
        if self._is_aisle_allowed(start, target_aisle=target_aisle, required_width=required_width):
            return (start,)
        queue: list[tuple[str, ...]] = [(start,)]
        seen = {start}
        while queue:
            path = queue.pop(0)
            cur = path[-1]
            for nxt in self._route_neighbours(cur, required_width):
                if nxt in seen or nxt in blocked:
                    continue
                if allowed_nodes is not None and nxt not in allowed_nodes:
                    continue
                new_path = path + (nxt,)
                if self._is_aisle_allowed(
                    nxt, target_aisle=target_aisle, required_width=required_width
                ):
                    return new_path
                seen.add(nxt)
                queue.append(new_path)
        return None

    def _route_neighbours(self, node: str, width: int) -> list[str]:
        out: list[str] = []
        edge_ids = [e for key, e in self.edges.items() if node in key]
        for edge in edge_ids:
            if not edge.allowed or edge.width < width:
                continue
            out.append(edge.other(node))
        # Coordinate grid fallback when no explicit edge touches the node.
        if not edge_ids:
            coordinates: dict[str, tuple[int, int]] = dict(self.positions)
            for lot_id, lot in self.lots.items():
                if lot.x is not None and lot.y is not None:
                    coordinates[lot.current_position or lot_id] = (lot.x, lot.y)
            here = coordinates.get(node)
            if here is not None:
                hx, hy = here
                for other, (x, y) in coordinates.items():
                    if other != node and abs(hx - x) + abs(hy - y) == 1:
                        out.append(other)
        return out

    def _is_aisle_allowed(
        self, node: str, *, target_aisle: str | None, required_width: int = 1
    ) -> bool:
        aisle = self.aisles.get(node)
        if aisle is None or not aisle.allowed or aisle.width < required_width:
            return False
        if target_aisle is not None and node != target_aisle:
            return False
        return True

    def access_for_group(
        self,
        lot_ids: Iterable[str],
        *,
        mode: str,
        target_aisle: str | None = None,
        blocked: set[str] | None = None,
        aisle_usage: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Check one shared permitted aisle for a connected allocation.

        For boxes every selected box needs a carry route.  For land one route
        from the connected boundary group is enough.  Aisle frontage capacity
        can additionally limit the number of units handed in a shift.
        """

        ids = list(lot_ids)
        routes: dict[str, tuple[str, ...]] = {}
        aisle_id: str | None = None
        errors: list[str] = []
        blocked = set(blocked or ())
        group = set(ids)

        targets = ids if mode == "box" else ids[:1]
        for lot_id in targets:
            lot = self.lots.get(lot_id)
            width = lot.width if lot else 1
            start = self.lot_position(lot_id) if lot else lot_id
            # A box may be carried through another box selected by the same
            # order after that first box is removed; permit group nodes.
            route_positions = self.route(
                start,
                target_aisle=target_aisle,
                required_width=width,
                blocked={self.lot_position(x) for x in (blocked - group) if x in self.lots},
                allowed_nodes=None,
            )
            if route_positions is None:
                errors.append(f"{lot_id}: no permitted route to an allowed aisle")
                continue
            route_tuple = (lot_id,) + tuple(route_positions)
            routes[lot_id] = route_tuple
            end = route_positions[-1]
            if aisle_id is None:
                aisle_id = end
            elif end != aisle_id and mode == "land":
                # A household asked for one aisle; keep the first shared one.
                pass

        if not errors and aisle_id is not None:
            aisle = self.aisles.get(aisle_id)
            if aisle and aisle.serves is not None:
                served = any(
                    lot_id in aisle.serves
                    or (self.lots.get(lot_id) and self.lots[lot_id].current_position in aisle.serves)
                    for lot_id in ids
                )
                if not served:
                    errors.append(f"aisle {aisle_id!r} is narrowed and does not serve this place")
            if aisle and aisle.handoff_capacity is not None:
                already = (aisle_usage or {}).get(aisle_id, 0)
                if already + len(ids) > aisle.handoff_capacity:
                    errors.append(
                        f"aisle {aisle_id!r} can hand {aisle.handoff_capacity} place(s); "
                        f"{already} already used and {len(ids)} requested"
                    )
        return {
            "ok": not errors,
            "aisle_id": aisle_id,
            "routes": {k: list(v) for k, v in routes.items()},
            "errors": errors,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "max_cells": self.max_cells,
            "max_households": self.max_households,
            "season_offsets": list(self.season_offsets),
            "lots": {k: v.to_json() for k, v in self.lots.items()},
            "aisles": {k: v.to_json() for k, v in self.aisles.items()},
            "composition": {
                lot: {source: str(amount) for source, amount in self.composition_of(lot).items()}
                for lot in self.lots
            },
            "histories": {
                source: {str(season): family for season, family in seasons.items()}
                for source, seasons in self.histories.items()
            },
            "validation_errors": list(self.validation_errors),
        }
