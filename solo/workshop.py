"""Wig workshop styling counter built on the byte-faithful revision library.

Domain mapping (the workshop metaphors sit on top of
:mod:`solo.revision` without changing its byte fidelity)::

    wig / 假发            one SOLO-coded combing-pattern document
    combing pattern / 梳样  ASCII lines describing root -> cell placement,
                          with optional margin notes encoded in PAIR mode
    re-combing / 改梳      RevisionLibrary.apply() of one text-patch batch
    return-as-is root /    preserve() patch pinning the root's line byte
    必须原样交回            ranges, re-verified byte for byte
    front-visible hair /   outermost placement of each cell
    正面可见
    back-only hair /       every deeper placement in the same cell stack
    背面翻看
    workshop dye / 染色    whole-root colour, fixed until make-up-removal day
    rental piece / 发片     coloured clip occupying one exposed clip slot

The combing pattern grammar (line based, every line terminated by ``\\n``)::

    WIG <id>                          document header, exactly one
    K <root> <colour>                 native colour of a root
    S <root> <slot> <length>          clip slot on a root (one slot per line)
    B <root> L|R <cell> <order>       one placement; lower ``order`` is
                                      more outer, order 0 is the front layer
    # <note>                          margin note; PAIR-codepoint notes are
                                      stored in PAIR mode and never parsed
    blank line                        ignored

A root may hold several placements (in different cells).  Colouring a
root therefore recolours *every* cell in which that root is exposed: the
counter never treats the same root in two cells as two pieces of hair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, NamedTuple

from .codec import DecodedDocument
from .revision import (
    PreservePatch,
    RevisionLibrary,
    TextPatch,
    Version,
    preserve,
    replace,
)

__all__ = [
    "WorkshopError",
    "Placement",
    "CellView",
    "Dye",
    "Piece",
    "Rental",
    "CellRequirement",
    "Order",
    "DyeAction",
    "ClipAction",
    "TrialResult",
    "RecombChange",
    "RecombReport",
    "CellVerdict",
    "ReturnReport",
    "CombingPattern",
    "PieceInventory",
    "StylingCounter",
    "LEFT",
    "RIGHT",
    "MAX_ROOTS",
    "MAX_CELLS",
    "MAX_WIGS",
]

MAX_ROOTS = 6
MAX_CELLS = 4
MAX_WIGS = 2

LEFT = "L"
RIGHT = "R"

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkshopError(Exception):
    """The styling counter cannot perform the requested workshop action."""


# ---------------------------------------------------------------------------
# Parsed pattern value types
# ---------------------------------------------------------------------------


class Placement(NamedTuple):
    """One line ``B <root> L|R <cell> <order>`` plus its text span.

    The span fields describe a line as decoded from a published version;
    a re-combing *edit* is built with :meth:`direct` and leaves them at -1.
    """

    root: str
    side: str
    cell: str
    order: int
    line_index: int = -1
    line_start: int = -1  # character offset of the line start in Version.text
    line_end: int = -1  # character offset just past the trailing newline

    @classmethod
    def direct(cls, root: str, side: str, cell: str, order: int) -> "Placement":
        """A re-combing target without an attached version span."""
        return cls(root, side, cell, order, -1, -1, -1)

    def render(self) -> str:
        return f"B {self.root} {self.side} {self.cell} {self.order}\n"


@dataclass(frozen=True)
class CellView:
    """Front/back visibility of one cell under one combing version."""

    cell: str
    # Placements outer -> inner; the first tuple is what the front sees.
    stack: tuple[Placement, ...]
    # (root, effective colour) of the front layer; None for an empty cell.
    front_root: str | None
    front_colour: str | None
    # (root, effective colour) for every layer only reachable from the back.
    back_only: tuple[tuple[str, str], ...]
    # Roots of rentals hidden in this cell by the current stacking.
    hidden_pieces: tuple[str, ...]


@dataclass(frozen=True)
class Dye:
    """A workshop colour fixed onto a whole root until a removal day."""

    root: str
    colour: str
    applied_day: int
    until_day: int


@dataclass(frozen=True)
class Piece:
    """A rental coloured clip with a fixed coverage length."""

    piece_id: str
    colour: str
    length: int
    wig_id: str | None = None  # the wig it may be clipped onto, if dedicated


@dataclass(frozen=True)
class Rental:
    """A piece currently clipped on a root's slot, due back on a day."""

    piece_id: str
    root: str
    slot: str
    colour: str
    length: int
    rented_day: int
    due_day: int


@dataclass(frozen=True)
class CellRequirement:
    """The colour the customer named for one front-visible cell."""

    cell: str
    colour: str


@dataclass(frozen=True)
class Order:
    """A customer order: named visible cells, protected roots, removal day."""

    order_id: str
    wig_id: str
    requirements: tuple[CellRequirement, ...]
    protected_roots: frozenset[str]
    removal_day: int
    as_is_only: bool = False
    dye_allowed_roots: frozenset[str] = frozenset()
    clip_allowed_roots: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Actions and result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DyeAction:
    root: str
    colour: str


@dataclass(frozen=True)
class ClipAction:
    piece_id: str
    root: str
    slot: str
    colour: str


@dataclass(frozen=True)
class TrialResult:
    """Outcome of a fitting.  ``feasible`` False means the order cannot ship."""

    order_id: str
    feasible: bool
    reason: str
    dye_actions: tuple[DyeAction, ...] = ()
    clip_actions: tuple[ClipAction, ...] = ()
    front_views: tuple[CellView, ...] = ()
    day: int = 0


@dataclass(frozen=True)
class RecombChange:
    """One root whose direction changed, with text and original byte spans."""

    root: str
    old_placement: Placement
    new_placement: Placement
    char_span: tuple[int, int]  # half-open character span patched in the base
    byte_span: tuple[int, int]  # original body bytes carrying that line
    record_kind: str = "replace"


@dataclass(frozen=True)
class RecombReport:
    """Result of a re-combing batch published through RevisionLibrary.apply."""

    wig_id: str
    base_version: int
    new_version: int
    changes: tuple[RecombChange, ...]
    front_views: tuple[CellView, ...]
    hidden_rentals: tuple[str, ...]
    batch_preserves: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class CellVerdict:
    cell: str
    wanted: str | None
    seen: str | None
    ok: bool


@dataclass(frozen=True)
class ReturnReport:
    """What happened when the wig came back on/after the removal day."""

    order_id: str
    day: int
    returned_pieces: tuple[str, ...]
    cleared_dyes: tuple[str, ...]
    cell_verdicts: tuple[CellVerdict, ...]
    protected_verified: bool
    protected_byte_spans: tuple[tuple[int, int], ...]


# ---------------------------------------------------------------------------
# Pattern parsing
# ---------------------------------------------------------------------------


def _line_iter(text: str):
    """Yield (line_index, start_char, end_char_excl_newline, line)."""
    start = 0
    index = 0
    for end, ch in enumerate(text):
        if ch == "\n":
            yield index, start, end, text[start:end]
            start = end + 1
            index += 1
    if start < len(text):
        # A final line without newline is accepted on read but never emitted
        # by the counter itself (every rendered line is newline terminated).
        yield index, start, len(text), text[start:]


class CombingPattern:
    """Parsed combing pattern of one wig at one version of its document."""

    def __init__(self, wig_id: str, doc: DecodedDocument, version_id: int):
        self.wig_id = wig_id
        self.doc = doc
        self.version_id = version_id
        self.placements: list[Placement] = []
        self.native_colours: dict[str, str] = {}
        self.slots: dict[str, dict[str, int]] = {}  # root -> slot -> length
        self._by_root: dict[str, list[Placement]] = {}
        self._parse(doc.text)

    # -- parsing ----------------------------------------------------------

    def _parse(self, text: str) -> None:
        header: str | None = None
        seen_roots: set[str] = set()
        for line_index, start, end, line in _line_iter(text):
            if not line.strip():
                continue
            if line.startswith("#"):
                # Margin note; PAIR-mode notes are simply never interpreted.
                continue
            parts = line.split()
            tag = parts[0]
            if tag == "WIG":
                if header is not None:
                    raise WorkshopError("duplicate WIG header in combing pattern")
                if len(parts) != 2:
                    raise WorkshopError(f"bad WIG header line: {line!r}")
                header = parts[1]
                continue
            if tag == "K":
                if len(parts) != 3:
                    raise WorkshopError(f"bad colour line: {line!r}")
                _, root, colour = parts
                if root in self.native_colours:
                    raise WorkshopError(f"duplicate native colour for root {root}")
                self.native_colours[root] = colour
                seen_roots.add(root)
                continue
            if tag == "S":
                if len(parts) != 4:
                    raise WorkshopError(f"bad slot line: {line!r}")
                _, root, slot, length_text = parts
                try:
                    length = int(length_text)
                except ValueError:
                    raise WorkshopError(f"slot length must be an integer: {line!r}") from None
                if length <= 0:
                    raise WorkshopError(f"slot length must be positive: {line!r}")
                slots = self.slots.setdefault(root, {})
                if slot in slots:
                    raise WorkshopError(f"duplicate slot {slot} on root {root}")
                slots[slot] = length
                seen_roots.add(root)
                continue
            if tag == "B":
                if len(parts) != 5:
                    raise WorkshopError(f"bad placement line: {line!r}")
                _, root, side, cell, order_text = parts
                if side not in (LEFT, RIGHT):
                    raise WorkshopError(f"parting side must be L or R: {line!r}")
                try:
                    order = int(order_text)
                except ValueError:
                    raise WorkshopError(f"stack order must be an integer: {line!r}") from None
                if order < 0:
                    raise WorkshopError(f"stack order must be >= 0: {line!r}")
                placement = Placement(root, side, cell, order, line_index, start, end + 1)
                self.placements.append(placement)
                self._by_root.setdefault(root, []).append(placement)
                seen_roots.add(root)
                continue
            raise WorkshopError(f"unknown combing-pattern line: {line!r}")

        if header is None:
            raise WorkshopError("combing pattern has no WIG header")
        if header != self.wig_id:
            raise WorkshopError(
                f"WIG header {header!r} does not match registered wig {self.wig_id!r}"
            )
        if len(seen_roots) > MAX_ROOTS:
            raise WorkshopError(f"a wig carries at most {MAX_ROOTS} roots")
        cells = {p.cell for p in self.placements}
        if len(cells) > MAX_CELLS:
            raise WorkshopError(f"a wig has at most {MAX_CELLS} cells")

        # Every placed root needs a native colour line.
        for root in self._by_root:
            if root not in self.native_colours:
                raise WorkshopError(f"root {root} has placements but no K colour line")

        # Within one cell the stacking orders must be unique and dense from 0.
        by_cell: dict[str, list[Placement]] = {}
        for p in self.placements:
            by_cell.setdefault(p.cell, []).append(p)
        for cell, stack in by_cell.items():
            orders = sorted(p.order for p in stack)
            if orders != list(range(len(orders))):
                raise WorkshopError(
                    f"cell {cell} stacking orders must be unique and dense from 0: {orders}"
                )

    # -- access -----------------------------------------------------------

    @property
    def roots(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_root))

    @property
    def cells(self) -> tuple[str, ...]:
        return tuple(sorted({p.cell for p in self.placements}))

    def placements_of(self, root: str) -> tuple[Placement, ...]:
        return tuple(self._by_root.get(root, ()))

    def stack(self, cell: str) -> tuple[Placement, ...]:
        """Placements of a cell ordered outer -> inner (by stack order)."""
        return tuple(sorted((p for p in self.placements if p.cell == cell), key=lambda p: p.order))

    def line_byte_span(self, placement: Placement) -> tuple[int, int]:
        """Original body byte range carrying one placement line."""
        start = int(self.doc.starts[placement.line_start])
        # line_end is exclusive and points at the first char after the line.
        last_char = placement.line_end - 1
        end = int(self.doc.ends[last_char])
        return start, end

    def colour_line_span(self, root: str) -> tuple[int, int]:
        """Character span of the ``K`` line that names a root."""
        for line_index, start, end, line in _line_iter(self.doc.text):
            if line.startswith("K ") and line.split()[1] == root:
                return start, end + 1
        raise WorkshopError(f"no colour line found for root {root}")

    def slot_line_span(self, root: str, slot: str) -> tuple[int, int]:
        """Character span of the ``S`` line declaring one clip slot."""
        for _line_index, start, end, line in _line_iter(self.doc.text):
            parts = line.split()
            if len(parts) == 4 and parts[0] == "S" and parts[1] == root and parts[2] == slot:
                return start, end + 1
        raise WorkshopError(f"no slot line found for {root}/{slot}")


# ---------------------------------------------------------------------------
# Rental inventory
# ---------------------------------------------------------------------------


class PieceInventory:
    """Coloured rental pieces.  A piece serves one wig until it is returned."""

    def __init__(self, pieces: Iterable[Piece] = ()) -> None:
        self._pieces: dict[str, Piece] = {}
        self._out: set[str] = set()
        for piece in pieces:
            self.add(piece)

    def add(self, piece: Piece) -> None:
        if piece.piece_id in self._pieces:
            raise WorkshopError(f"duplicate piece {piece.piece_id}")
        self._pieces[piece.piece_id] = piece

    def get(self, piece_id: str) -> Piece:
        try:
            return self._pieces[piece_id]
        except KeyError:
            raise WorkshopError(f"unknown piece {piece_id}") from None

    def piece_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._pieces))

    def available(self, piece_id: str, wig_id: str) -> bool:
        """A piece is lendable only when free and dedicated to this wig."""
        piece = self.get(piece_id)
        if piece.wig_id is not None and piece.wig_id != wig_id:
            return False
        return piece_id not in self._out

    def rent(self, piece_id: str, wig_id: str) -> Piece:
        piece = self.get(piece_id)
        if piece_id in self._out:
            raise WorkshopError(f"piece {piece_id} is already on loan")
        if piece.wig_id is not None and piece.wig_id != wig_id:
            raise WorkshopError(
                f"piece {piece_id} is dedicated to wig {piece.wig_id}, not {wig_id}"
            )
        self._out.add(piece_id)
        return piece

    def give_back(self, piece_id: str) -> None:
        self._out.discard(piece_id)

    def on_loan(self) -> tuple[str, ...]:
        return tuple(sorted(self._out))

    def retire(self, piece_id: str) -> None:
        """Remove a piece from the catalogue (it must not be on loan)."""
        if piece_id in self._out:
            raise WorkshopError(f"piece {piece_id} is on loan and cannot be retired")
        self._pieces.pop(piece_id, None)


# ---------------------------------------------------------------------------
# Per-wig mutable workshop state
# ---------------------------------------------------------------------------


@dataclass
class _WigRecord:
    wig_id: str
    library: RevisionLibrary
    original_version: int
    current_version: int
    pattern: CombingPattern
    dyes: dict[str, Dye] = field(default_factory=dict)
    rentals: dict[str, Rental] = field(default_factory=dict)  # slot key -> rental
    slot_piece: dict[str, str] = field(default_factory=dict)  # slot key -> piece id

    def slot_key(self, root: str, slot: str) -> str:
        return f"{self.wig_id}:{root}:{slot}"


# ---------------------------------------------------------------------------
# The styling counter
# ---------------------------------------------------------------------------


class StylingCounter:
    """Workshop counter over per-wig byte-faithful revision libraries.

    A :class:`RevisionLibrary` holds one real original, so each wig gets its
    own library: the first wig may reuse a library handed in by the caller,
    every further wig opens an internal one.  Every state-changing styling
    decision is either a library revision (re-combing) or a recorded workshop
    action (dye/rental) whose effect is re-derived from the current combing
    version before each answer.
    """

    def __init__(
        self,
        library: RevisionLibrary | None = None,
        inventory: PieceInventory | None = None,
    ) -> None:
        self._root_library = library
        self.inventory = inventory or PieceInventory()
        self._wigs: dict[str, _WigRecord] = {}
        self._orders: dict[str, Order] = {}
        self._confirmed: dict[str, tuple[DyeAction, ...]] = {}
        self._day = 0

    # -- intake -----------------------------------------------------------

    def register_wig(self, wig_id: str, body: bytes | bytearray | memoryview) -> Version:
        """Decode and register a wig's original combing-pattern document."""
        if len(self._wigs) >= MAX_WIGS:
            raise WorkshopError(f"the counter holds at most {MAX_WIGS} wigs")
        if wig_id in self._wigs:
            raise WorkshopError(f"wig {wig_id} is already registered")
        if not self._wigs and self._root_library is not None and len(self._root_library) == 0:
            library = self._root_library
        else:
            library = RevisionLibrary()
        version = library.add_original(body)
        pattern = CombingPattern(wig_id, version.decode(), version.version_id)
        self._wigs[wig_id] = _WigRecord(
            wig_id=wig_id,
            library=library,
            original_version=version.version_id,
            current_version=version.version_id,
            pattern=pattern,
        )
        return version

    def library_of(self, wig_id: str) -> RevisionLibrary:
        """The revision library that stores the wig's document lineage."""
        return self._record(wig_id).library

    @property
    def library(self) -> RevisionLibrary:
        """The first wig's library (convenience for single-wig counters)."""
        if not self._wigs:
            if self._root_library is None:
                raise WorkshopError("no wig has been registered yet")
            return self._root_library
        return next(iter(self._wigs.values())).library

    def _record(self, wig_id: str) -> _WigRecord:
        try:
            return self._wigs[wig_id]
        except KeyError:
            raise WorkshopError(f"unknown wig {wig_id}") from None

    def pattern(self, wig_id: str) -> CombingPattern:
        return self._record(wig_id).pattern

    def version(self, wig_id: str) -> Version:
        rec = self._record(wig_id)
        return rec.library.get(rec.current_version)

    @property
    def day(self) -> int:
        return self._day

    def advance_day(self, days: int = 1) -> int:
        if days <= 0:
            raise WorkshopError("days must be positive")
        self._day += days
        return self._day

    # -- visibility -------------------------------------------------------

    def _effective_colour(self, rec: _WigRecord, root: str, day: int) -> str:
        """Dye wins over native colour while it is fixed; rentals are separate."""
        dye = rec.dyes.get(root)
        if dye is not None and day <= dye.until_day:
            return dye.colour
        return rec.pattern.native_colours[root]

    def _rental_on(self, rec: _WigRecord, root: str) -> Rental | None:
        for key in sorted(rec.rentals):
            rental = rec.rentals[key]
            if rental.root == root:
                return rental
        return None

    def front_view(self, wig_id: str, cell: str, day: int | None = None) -> CellView:
        """Restore the outer->inner stack and decide front vs back hair."""
        rec = self._record(wig_id)
        if day is None:
            day = self._day
        stack = rec.pattern.stack(cell)
        if not stack:
            return CellView(cell, (), None, None, (), ())
        front = stack[0]
        rental = self._rental_on(rec, front.root)
        if rental is not None and day <= rental.due_day:
            front_colour = rental.colour
        else:
            front_colour = self._effective_colour(rec, front.root, day)
        back_only = tuple(
            (p.root, self._effective_colour(rec, p.root, day)) for p in stack[1:]
        )
        hidden = tuple(
            rental.root
            for rental in rec.rentals.values()
            if any(p.root == rental.root for p in stack[1:])
        )
        return CellView(cell, stack, front.root, front_colour, back_only, hidden)

    def all_views(self, wig_id: str, day: int | None = None) -> tuple[CellView, ...]:
        rec = self._record(wig_id)
        return tuple(self.front_view(wig_id, cell, day) for cell in rec.pattern.cells)

    # -- fitting / trial --------------------------------------------------

    def place_order(self, order: Order) -> Order:
        rec = self._record(order.wig_id)
        if order.order_id in self._orders:
            raise WorkshopError(f"order {order.order_id} already exists")
        if order.removal_day < self._day:
            raise WorkshopError("removal day is already past")
        roots = set(rec.pattern.roots)
        for root in order.protected_roots:
            if root not in roots:
                raise WorkshopError(f"protected root {root} is not on wig {order.wig_id}")
        for req in order.requirements:
            if req.cell not in rec.pattern.cells:
                raise WorkshopError(f"cell {req.cell} does not exist on wig {order.wig_id}")
        bad = order.dye_allowed_roots & order.protected_roots
        if bad:
            raise WorkshopError(f"protected roots cannot be dye-approved: {sorted(bad)}")
        bad = order.clip_allowed_roots & order.protected_roots
        if bad:
            raise WorkshopError(f"protected roots cannot be clip-approved: {sorted(bad)}")
        for root in order.dye_allowed_roots | order.clip_allowed_roots:
            if root not in roots:
                raise WorkshopError(f"approved root {root} is not on wig {order.wig_id}")
        self._orders[order.order_id] = order
        return order

    def _check_clip(
        self,
        rec: _WigRecord,
        order: Order,
        root: str,
        wanted: str,
        used_pieces: set[str],
        used_slots: set[str],
        day: int,
    ) -> ClipAction | None:
        if root not in order.clip_allowed_roots or root in order.protected_roots:
            return None
        slots = rec.pattern.slots.get(root, {})
        for slot, length in sorted(slots.items()):
            key = rec.slot_key(root, slot)
            if key in rec.rentals or key in used_slots:
                continue
            # Deterministic: lowest piece id among matching free pieces.
            for piece_id in self.inventory.piece_ids():
                piece = self.inventory.get(piece_id)
                if piece_id in used_pieces:
                    continue
                if piece.colour != wanted or piece.length > length:
                    continue
                if not self.inventory.available(piece_id, rec.wig_id):
                    continue
                return ClipAction(piece_id, root, slot, wanted)
        return None

    def fit(self, order_id: str, day: int | None = None) -> TrialResult:
        """Try the order on.  Plans clips first, then dyes; never mutates state."""
        if day is None:
            day = self._day
        order = self._orders.get(order_id)
        if order is None:
            raise WorkshopError(f"unknown order {order_id}")
        rec = self._record(order.wig_id)
        views = self.all_views(order.wig_id, day)
        wanted = {req.cell: req.colour for req in order.requirements}

        if order.as_is_only:
            verdicts = [
                view.front_colour == wanted.get(view.cell)
                for view in views
                if view.cell in wanted
            ]
            ok = all(verdicts) and bool(verdicts or not wanted)
            return TrialResult(
                order_id,
                ok,
                "as-is colours already match" if ok else "as-is trial: a named cell shows the wrong colour",
                front_views=views,
                day=day,
            )

        dye_actions: list[DyeAction] = []
        clip_actions: list[ClipAction] = []
        used_pieces: set[str] = set()
        used_slots: set[str] = set()
        reasons: list[str] = []

        for view in views:
            if view.cell not in wanted:
                continue
            target = wanted[view.cell]
            if view.front_colour == target:
                continue
            root = view.front_root
            existing = rec.dyes.get(root)
            if existing is not None and day <= existing.until_day and existing.colour != target:
                reasons.append(
                    f"cell {view.cell}: root {root} is fixed {existing.colour} until day "
                    f"{existing.until_day}, cannot show {target}"
                )
                continue
            clip = self._check_clip(rec, order, root, target, used_pieces, used_slots, day)
            if clip is not None:
                clip_actions.append(clip)
                used_pieces.add(clip.piece_id)
                used_slots.add(rec.slot_key(clip.root, clip.slot))
                continue
            if (
                root in order.dye_allowed_roots
                and root not in order.protected_roots
                and (existing is None or existing.colour == target)
            ):
                action = DyeAction(root, target)
                if action not in dye_actions:
                    dye_actions.append(action)
                continue
            reasons.append(
                f"cell {view.cell}: front root {root} cannot be turned {target} "
                f"(no exposed approved clip slot, no dye approval, or root protected)"
            )

        feasible = not reasons
        # Simulate the planned actions before promising the customer anything.
        if feasible:
            simulated = self._simulate(rec, dye_actions, clip_actions, day)
            for cell, target in wanted.items():
                if simulated[cell] != target:
                    feasible = False
                    reasons.append(
                        f"cell {cell}: planned actions still show {simulated[cell]}, not {target}"
                    )

        return TrialResult(
            order_id,
            feasible,
            "; ".join(reasons) if reasons else "all named cells match under the planned actions",
            tuple(dye_actions),
            tuple(clip_actions),
            views,
            day,
        )

    def _simulate(
        self,
        rec: _WigRecord,
        dyes: Iterable[DyeAction],
        clips: Iterable[ClipAction],
        day: int,
    ) -> dict[str, str]:
        """Effective front colours after applying planned actions in memory."""
        dye_map = {a.root: a.colour for a in dyes}
        clip_roots = {a.root: a.colour for a in clips}
        out: dict[str, str] = {}
        for cell in rec.pattern.cells:
            view = self.front_view(rec.wig_id, cell, day)
            root = view.front_root
            if root is None:
                out[cell] = None  # type: ignore[assignment]
            elif root in clip_roots:
                out[cell] = clip_roots[root]
            elif root in dye_map:
                out[cell] = dye_map[root]
            else:
                out[cell] = view.front_colour
        return out

    # -- confirmation -----------------------------------------------------

    def confirm(self, order_id: str, day: int | None = None) -> TrialResult:
        """Commit the fitting: rent pieces and fix dyes until removal day."""
        if day is None:
            day = self._day
        trial = self.fit(order_id, day)
        if not trial.feasible:
            raise WorkshopError(f"cannot confirm order {order_id}: {trial.reason}")
        order = self._orders[order_id]
        rec = self._record(order.wig_id)
        for action in trial.clip_actions:
            piece = self.inventory.rent(action.piece_id, rec.wig_id)
            key = rec.slot_key(action.root, action.slot)
            rec.rentals[key] = Rental(
                piece_id=piece.piece_id,
                root=action.root,
                slot=action.slot,
                colour=action.colour,
                length=piece.length,
                rented_day=day,
                due_day=order.removal_day,
            )
            rec.slot_piece[key] = piece.piece_id
        for action in trial.dye_actions:
            rec.dyes[action.root] = Dye(
                root=action.root,
                colour=action.colour,
                applied_day=day,
                until_day=order.removal_day,
            )
        self._confirmed[order_id] = trial.dye_actions
        return trial

    # -- re-combing through RevisionLibrary.apply -------------------------

    def recomb(
        self,
        wig_id: str,
        edits: dict[str, "Placement | tuple[Placement, ...]"],
        day: int | None = None,
    ) -> RecombReport:
        """Publish a re-combing batch.

        ``edits`` maps a root to the placement (or one placement per cell it
        currently occupies) it must now hold.  The batch goes through
        :meth:`RevisionLibrary.apply` as text patches on the placement lines,
        with preserve patches pinning every line that names a protected root
        so a protected root can never be re-directed.
        """
        if day is None:
            day = self._day
        rec = self._record(wig_id)
        base = rec.library.get(rec.current_version)
        doc = base.decode()
        pattern = CombingPattern(wig_id, doc, base.version_id)

        protected = set()
        for order in self._orders.values():
            if order.wig_id == wig_id:
                protected.update(order.protected_roots)

        # Up-front validation of every edit before a single patch is built,
        # so an invalid re-combing can never reach the library half done.
        def as_tuple(targets: "Placement | tuple[Placement, ...]") -> tuple[Placement, ...]:
            return targets if isinstance(targets, tuple) else (targets,)

        normalized: dict[str, tuple[Placement, ...]] = {}
        for root, raw_targets in edits.items():
            if root not in pattern.roots:
                raise WorkshopError(f"cannot recomb unknown root {root}")
            if root in protected:
                raise WorkshopError(
                    f"root {root} must be returned as-is; re-combing it is forbidden"
                )
            target_tuple = as_tuple(raw_targets)
            for target in target_tuple:
                if target.root != root:
                    raise WorkshopError("a re-combing edit may only redirect its own root")
            currents = pattern.placements_of(root)
            if len(target_tuple) != len(currents):
                raise WorkshopError(
                    f"root {root} occupies {len(currents)} cell(s) but the edit names "
                    f"{len(target_tuple)} target placement(s)"
                )
            normalized[root] = target_tuple

        # The resulting stacking of every cell must stay unique and dense.
        def replacement_for(placement: Placement) -> Placement:
            target_tuple = normalized.get(placement.root)
            if target_tuple is None:
                return placement
            by_cell = {t.cell: t for t in target_tuple}
            return by_cell.get(placement.cell, target_tuple[0])

        for cell in pattern.cells:
            new_stack = [replacement_for(p) for p in pattern.stack(cell)]
            orders = sorted(p.order for p in new_stack)
            if orders != list(range(len(orders))):
                raise WorkshopError(
                    f"re-combing would leave cell {cell} with non-dense stack orders {orders}"
                )

        patches: list[TextPatch | PreservePatch] = []
        changes: list[RecombChange] = []
        for root, target_tuple in normalized.items():
            target_by_cell = {t.cell: t for t in target_tuple}
            for old in pattern.placements_of(root):
                new_placement = target_by_cell.get(old.cell, target_tuple[0])
                if (
                    new_placement.side == old.side
                    and new_placement.cell == old.cell
                    and new_placement.order == old.order
                ):
                    continue
                patches.append(replace(old.line_start, old.line_end, new_placement.render()))
                byte_span = pattern.line_byte_span(old)
                changes.append(
                    RecombChange(
                        root=root,
                        old_placement=old,
                        new_placement=new_placement,
                        char_span=(old.line_start, old.line_end),
                        byte_span=byte_span,
                    )
                )

        # Preserve every line (colour, slot, placement) belonging to a
        # protected root.  Preserve patches use original byte spans, so the
        # library independently verifies byte-for-byte fidelity.
        preserve_spans: list[tuple[int, int]] = []
        for root in sorted(protected):
            char_span = pattern.colour_line_span(root)
            start_byte = int(doc.starts[char_span[0]])
            end_byte = int(doc.ends[char_span[1] - 1])
            patches.append(preserve(start_byte, end_byte))
            preserve_spans.append((start_byte, end_byte))
            for slot in pattern.slots.get(root, {}):
                char_span = pattern.slot_line_span(root, slot)
                start_byte = int(doc.starts[char_span[0]])
                end_byte = int(doc.ends[char_span[1] - 1])
                patches.append(preserve(start_byte, end_byte))
                preserve_spans.append((start_byte, end_byte))
            for placement in pattern.placements_of(root):
                start_byte, end_byte = pattern.line_byte_span(placement)
                patches.append(preserve(start_byte, end_byte))
                preserve_spans.append((start_byte, end_byte))

        if not any(isinstance(p, TextPatch) for p in patches):
            raise WorkshopError("re-combing changes nothing")

        new_version = rec.library.apply(base.version_id, patches)

        new_pattern = CombingPattern(wig_id, new_version.decode(), new_version.version_id)
        rec.current_version = new_version.version_id
        rec.pattern = new_pattern

        views = self.all_views(wig_id, day)
        hidden = tuple(sorted({h for view in views for h in view.hidden_pieces}))
        return RecombReport(
            wig_id=wig_id,
            base_version=base.version_id,
            new_version=new_version.version_id,
            changes=tuple(changes),
            front_views=views,
            hidden_rentals=hidden,
            batch_preserves=tuple(sorted(preserve_spans)),
        )

    def review(self, wig_id: str, order_id: str, day: int | None = None) -> tuple[CellVerdict, ...]:
        """Re-check a confirmed order against the current combing version."""
        if day is None:
            day = self._day
        order = self._orders[order_id]
        wanted = {req.cell: req.colour for req in order.requirements}
        verdicts: list[CellVerdict] = []
        for view in self.all_views(wig_id, day):
            target = wanted.get(view.cell)
            if target is None:
                continue
            verdicts.append(
                CellVerdict(view.cell, target, view.front_colour, view.front_colour == target)
            )
        return tuple(verdicts)

    # -- return -----------------------------------------------------------

    def return_wig(self, order_id: str, day: int | None = None) -> ReturnReport:
        """Take the wig back: free pieces, lift expired dyes, verify preserves."""
        if day is None:
            day = self._day
        order = self._orders.get(order_id)
        if order is None:
            raise WorkshopError(f"unknown order {order_id}")
        if day < order.removal_day:
            raise WorkshopError(
                f"removal day is {order.removal_day}; cannot return on day {day}"
            )
        rec = self._record(order.wig_id)

        returned: list[str] = []
        for key, rental in list(rec.rentals.items()):
            if rental.due_day <= day:
                self.inventory.give_back(rental.piece_id)
                returned.append(rental.piece_id)
                del rec.rentals[key]
                rec.slot_piece.pop(key, None)

        cleared: list[str] = []
        for root, dye in list(rec.dyes.items()):
            if dye.until_day <= day:
                del rec.dyes[root]
                cleared.append(root)

        wanted = {req.cell: req.colour for req in order.requirements}
        verdicts = tuple(
            CellVerdict(view.cell, wanted.get(view.cell), view.front_colour, view.front_colour == wanted.get(view.cell))
            for view in self.all_views(order.wig_id, day)
            if view.cell in wanted
        )

        # Re-verify protected roots against the original spans: every batch
        # pinned them with preserve() and the library kept the bytes verbatim.
        original = rec.library.get(rec.original_version)
        current = rec.library.get(rec.current_version)
        protected_spans: list[tuple[int, int]] = []
        ok = True
        if order.protected_roots:
            original_pattern = CombingPattern(order.wig_id, original.decode(), original.version_id)
            current_doc = current.decode()
            current_pattern = CombingPattern(order.wig_id, current_doc, current.version_id)
            for root in sorted(order.protected_roots):
                start, end = original_pattern.colour_line_span(root)
                ob0, ob1 = (int(original_pattern.doc.starts[start]), int(original_pattern.doc.ends[end - 1]))
                cstart, cend = current_pattern.colour_line_span(root)
                cb0, cb1 = (int(current_doc.starts[cstart]), int(current_doc.ends[cend - 1]))
                protected_spans.append((ob0, ob1))
                if original.body[ob0:ob1] != current.body[cb0:cb1]:
                    ok = False
                for placement in original_pattern.placements_of(root):
                    b0, b1 = original_pattern.line_byte_span(placement)
                    protected_spans.append((b0, b1))
                    match = next(
                        (p for p in current_pattern.placements_of(root) if p.cell == placement.cell),
                        None,
                    )
                    if match is None:
                        ok = False
                        continue
                    mb0, mb1 = current_pattern.line_byte_span(match)
                    if original.body[b0:b1] != current.body[mb0:mb1]:
                        ok = False

        return ReturnReport(
            order_id=order_id,
            day=day,
            returned_pieces=tuple(sorted(returned)),
            cleared_dyes=tuple(sorted(cleared)),
            cell_verdicts=verdicts,
            protected_verified=ok,
            protected_byte_spans=tuple(sorted(set(protected_spans))),
        )
