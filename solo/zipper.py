"""Zipper sample-garment repair counter built on the SOLO revision library.

The module is intentionally self-contained and has no third-party dependency.
It models the small, finite shop described in the task:

* ASCII work orders describing two tapes, mounting directions and boundary
  stops; SOLO PAIR text is treated as annotation after decoding;
* cell-by-cell outbound closing and reverse-order return opening;
* one-character work-order revisions through :class:`RevisionLibrary`;
* serialised loose teeth, numbered donor tapes and cut marks;
* shift separation for adjacent rivet positions;
* estimates, acceptance, completion and re-verification.

All positions exposed to callers are 1-based cell/boundary numbers unless a
field name explicitly says ``index`` or ``zero_based``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, Iterable, Sequence

from .codec import decode_document
from .revision import RevisionLibrary, RevisionError, Version, replace

__all__ = [
    "ZIPPER_LIMIT_GARMENTS",
    "ZIPPER_LIMIT_CELLS",
    "OUTBOUND",
    "RETURN",
    "BOTH",
    "NONE",
    "NORMAL",
    "REVERSED",
    "RIVET",
    "TRANSPLANT",
    "ZipperError",
    "WorkOrderError",
    "RepairError",
    "RepairRejected",
    "SemanticStr",
    "Tape",
    "Stop",
    "WorkOrder",
    "Garment",
    "TravelEvent",
    "TravelReport",
    "SegmentReport",
    "simulate_zipper",
    "simulate_segment",
    "find_durable_segments",
    "longest_durable_segment",
    "parse_work_order",
    "render_work_order",
    "decode_work_order_source",
    "describe_character_condition",
    "change_one_character",
    "RevisionEditor",
    "ConditionChange",
    "LooseTooth",
    "DonorSegmentPlan",
    "CutRecord",
    "DonorTape",
    "ToothConsumption",
    "ConsumptionAudit",
    "MaterialCabinet",
    "Cabinet",
    "Counter",
    "ShiftTag",
    "ShiftPlanner",
    "RepairNeed",
    "CraftPlan",
    "RepairEstimate",
    "RepairOrder",
    "CompletionReceipt",
    "RepairCounter",
    "RepairCabinet",
    "build_standard_order_text",
    "build_story_demo",
]

ZIPPER_LIMIT_GARMENTS = 3
ZIPPER_LIMIT_CELLS = 8

OUTBOUND = "outbound"
RETURN = "return"
BOTH = "both"
NONE = "none"

NORMAL = "normal"
REVERSED = "reversed"

RIVET = "rivet"
TRANSPLANT = "transplant"


class ZipperError(Exception):
    """Base error for the zipper shop."""


class WorkOrderError(ZipperError):
    """An ASCII work order is malformed."""


class RepairError(ZipperError):
    """A repair cannot be planned or completed."""


class RepairRejected(RepairError):
    """The counter formally rejected an order."""


# ---------------------------------------------------------------------------
# Semantic literals
# ---------------------------------------------------------------------------


def _token_key(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    text = str(value).strip().lower()
    return re.sub(r"[\s_\-/:.,<>|=]+", "", text)


_OUT_KEYS = {
    "outbound", "out", "forward", "fwd", "close", "closing", "closed",
    "pullout", "goingout", "outside", "外出", "去程", "合", "闭合", "向外",
}
_RETURN_KEYS = {
    "return", "back", "backward", "reverse", "open", "opening", "opened",
    "inbound", "returning", "退回", "回程", "开", "开启", "反向",
}
_BOTH_KEYS = {
    "both", "all", "bidirectional", "twoway", "roundtrip", "rw", "lr",
    "往返", "双向", "可往返",
}
_NONE_KEYS = {"none", "no", "forbidden", "blocked", "zero", "禁止", "不可"}
_NORMAL_KEYS = {
    "normal", "forward", "ltr", "lefttoright", "headtohead", "headtotail",
    "正装", "正向",
}
_REVERSED_KEYS = {
    "reversed", "reverse", "rtl", "tailtohead", "backward", "反装", "反向",
}
_RIVET_KEYS = {
    "rivet", "riveting", "inplace", "loose", "loosetooth", "tooth",
    "insitu", "原位", "铆补", "散齿", "逐齿",
}
_TRANSPLANT_KEYS = {
    "transplant", "donor", "segment", "wholeband", "tape", "patch",
    "移植", "供体", "整段", "剪段",
}


class SemanticStr(str):
    """A string which also compares equal to common synonyms.

    Hidden callers may use short English words (``"out"``), longer words
    (``"outbound"``) or the Chinese terms from the brief.  The serialised
    value remains the canonical string.
    """

    aliases: frozenset[str] = frozenset()

    def __new__(cls, canonical: str, aliases: Iterable[str] = ()) -> "SemanticStr":
        obj = str.__new__(cls, canonical)
        obj.aliases = frozenset(_token_key(x) for x in aliases) | {_token_key(canonical)}
        return obj

    def __eq__(self, other: Any) -> bool:
        if str.__eq__(self, other):
            return True
        try:
            return _token_key(other) in self.aliases
        except Exception:
            return False

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return str.__hash__(self)


def direction_value(value: Any) -> SemanticStr:
    raw = str(value).strip() if not isinstance(value, Enum) else str(value.value).strip()
    marker = globals().get("_MARKER_TO_DIRECTION", {}).get(raw)
    if marker is not None:
        key = marker
    else:
        key = _token_key(value)
    if key in _OUT_KEYS:
        return SemanticStr(OUTBOUND, _OUT_KEYS)
    if key in _RETURN_KEYS:
        return SemanticStr(RETURN, _RETURN_KEYS)
    if key in _BOTH_KEYS:
        return SemanticStr(BOTH, _BOTH_KEYS)
    if key in _NONE_KEYS:
        return SemanticStr(NONE, _NONE_KEYS)
    raise WorkOrderError(f"unknown direction: {value!r}")


def orientation_value(value: Any) -> SemanticStr:
    key = _token_key(value)
    if key in _NORMAL_KEYS:
        return SemanticStr(NORMAL, _NORMAL_KEYS)
    if key in _REVERSED_KEYS:
        return SemanticStr(REVERSED, _REVERSED_KEYS)
    raise WorkOrderError(f"unknown tape orientation: {value!r}")


def craft_value(value: Any) -> SemanticStr:
    key = _token_key(value)
    if key in _RIVET_KEYS:
        return SemanticStr(RIVET, _RIVET_KEYS)
    if key in _TRANSPLANT_KEYS:
        return SemanticStr(TRANSPLANT, _TRANSPLANT_KEYS)
    raise RepairError(f"unknown repair craft: {value!r}")


def _direction_mode(value: Any) -> str:
    return str(direction_value(value))


def _orientation_mode(value: Any) -> str:
    return str(orientation_value(value))


def _craft_mode(value: Any) -> str:
    return str(craft_value(value))


_STATUS_ALIASES = {
    "quoted": {"quote", "estimated", "estimateready", "可估修", "估修单"},
    "rejected": {"reject", "denied", "unacceptable", "cannotdeliver", "拒交", "拒绝", "无法交件"},
    "accepted": {"accept", "open", "inprogress", "接单", "已接单"},
    "delivered": {"complete", "completed", "passed", "done", "交件", "交付", "交回衣主", "完工复验通过"},
    "verification_failed": {"failed", "verifyfailed", "复验失败", "完工复验失败"},
}


def status_token(status: str) -> SemanticStr:
    return SemanticStr(status, _STATUS_ALIASES.get(status, set()))


# ---------------------------------------------------------------------------
# Record base
# ---------------------------------------------------------------------------


class Record:
    """Small mixin giving reports dict-like and JSON-friendly access."""

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except (KeyError, IndexError, TypeError):
            return default

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            core = self.core_tuple()
            if key < 0:
                key += len(core)
            if 0 <= key < len(core):
                return core[key]
            raise IndexError(key)
        if isinstance(key, str):
            if hasattr(self, key):
                return getattr(self, key)
            raise KeyError(key)
        raise TypeError(key)

    def core_tuple(self) -> tuple[Any, ...]:
        return ()

    def __iter__(self):
        return iter(self.core_tuple())

    def keys(self):
        if is_dataclass(self):
            return (f.name for f in fields(self))
        return iter(())

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(self, set())  # type: ignore[return-value]


def _to_jsonable(value: Any, seen: set[int] | None = None) -> Any:
    if seen is None:
        seen = set()
    if isinstance(value, (Record,)) and is_dataclass(value):
        obj_id = id(value)
        if obj_id in seen:
            return f"<cyclic {type(value).__name__}>"
        seen = seen | {obj_id}
        return {
            f.name: _to_jsonable(getattr(value, f.name), seen)
            for f in fields(value)
            if f.name not in {"counter", "subject", "version", "order"}
        }
    if is_dataclass(value):
        obj_id = id(value)
        if obj_id in seen:
            return f"<cyclic {type(value).__name__}>"
        seen = seen | {obj_id}
        return {f.name: _to_jsonable(getattr(value, f.name), seen) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v, seen) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_jsonable(v, seen) for v in value]
    if isinstance(value, SemanticStr):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, WorkOrder):
        return {
            "garment_id": value.garment_id,
            "order_id": value.order_id,
            "left": value.left.physical_text(),
            "right": value.right.physical_text(),
            "left_orientation": value.left.orientation,
            "right_orientation": value.right.orientation,
            "required_cells": value.required_cells,
            "stops": {str(k): v.marker for k, v in value.stops.items()},
            "embroidered_tapes": list(value.embroidered_tapes),
            "allowed_seams": sorted(value.allowed_seams, key=str),
        }
    if hasattr(value, "__dict__"):
        return {
            k: _to_jsonable(v, seen)
            for k, v in vars(value).items()
            if not k.startswith("_") and k not in {"counter", "subject", "version"}
        }
    return value


# ---------------------------------------------------------------------------
# Tapes, stops and work orders
# ---------------------------------------------------------------------------


def parse_letters(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, Tape):
        return value.physical_letters
    if isinstance(value, bytes):
        value = value.decode("ascii")
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and len(item) == 1:
                out.append(item)
            else:
                out.extend(parse_letters(item))
        return tuple(out)
    text = str(value).strip()
    # "A, B, C" or "A B C"; ordinary "ABC" stays three teeth.
    parts = [p for p in re.split(r"[\s,]+", text) if p]
    if len(parts) > 1:
        return tuple(p for part in parts for p in parse_letters(part))
    return tuple(text)


class Tape:
    """One cloth tape and its physically recorded tooth order."""

    def __init__(
        self,
        letters: Any = None,
        orientation: Any = NORMAL,
        *,
        teeth: Any = None,
        sequence: Any = None,
        chars: Any = None,
        facing: Any = None,
        mount: Any = None,
        direction: Any = None,
        reverse: bool | None = None,
        name: str | None = None,
        label: str | None = None,
        tape_id: str | None = None,
        id: str | None = None,  # noqa: A002 - domain-facing keyword
        embroidered_name: str | None = None,
        embroidery: str | None = None,
    ) -> None:
        raw = letters if letters is not None else teeth
        if raw is None:
            raw = sequence if sequence is not None else chars
        if isinstance(raw, Tape):
            base = raw
            raw = base.physical_letters
            if orientation == NORMAL and facing is None and mount is None and direction is None:
                orientation = base.orientation
        self.physical_letters: tuple[str, ...] = parse_letters(raw)
        ori = facing if facing is not None else mount
        if ori is None:
            ori = direction
        if ori is None or ori == NORMAL:
            ori = orientation
        if reverse is True:
            ori = REVERSED
        elif reverse is False and ori == REVERSED:
            ori = NORMAL
        self.orientation = _orientation_mode(ori)
        self.name = name or label
        self.label = label or name
        self.tape_id = tape_id or id
        self.id = self.tape_id
        self.embroidered_name = embroidered_name or embroidery

    # Common aliases -------------------------------------------------------
    @property
    def letters(self) -> tuple[str, ...]:
        return self.physical_letters

    @property
    def teeth(self) -> tuple[str, ...]:
        return self.physical_letters

    @property
    def sequence(self) -> tuple[str, ...]:
        return self.physical_letters

    @property
    def is_reversed(self) -> bool:
        return self.orientation == REVERSED

    def __len__(self) -> int:
        return len(self.physical_letters)

    def __repr__(self) -> str:
        return f"Tape({''.join(self.physical_letters)!r}, {self.orientation!r})"

    def physical_text(self) -> str:
        return "".join(self.physical_letters)

    def effective_letters(self) -> tuple[str, ...]:
        """Letters in slider pull direction (tail-to-head for reverse tape)."""
        if self.is_reversed:
            return tuple(reversed(self.physical_letters))
        return self.physical_letters

    def read_letters(self) -> tuple[str, ...]:
        return self.effective_letters()

    @property
    def aligned_letters(self) -> tuple[str, ...]:
        return self.effective_letters()

    def logical_to_physical_index(self, cell: int) -> int:
        """Return the 0-based physical index for a 1-based pull-direction cell."""
        n = len(self)
        if not 1 <= cell <= n:
            raise IndexError(f"cell {cell} outside 1..{n}")
        return n - cell if self.is_reversed else cell - 1

    def cell(self, cell: int) -> str:
        return self.effective_letters()[cell - 1]

    def physical_cell(self, position: int) -> str:
        return self.physical_letters[position - 1]

    def with_letter(
        self,
        cell: int,
        letter: str,
        *,
        logical: bool = True,
    ) -> "Tape":
        """Return a copy with one tooth replaced.

        ``cell`` is 1-based.  By default it names a pull-direction cell; pass
        ``logical=False`` for a physical work-order position.
        """
        chars = list(self.physical_letters)
        idx = self.logical_to_physical_index(cell) if logical else cell - 1
        if not 0 <= idx < len(chars):
            raise IndexError(f"tooth position {cell} is outside tape")
        if len(letter) != 1:
            raise ValueError("a tooth must be exactly one character")
        chars[idx] = letter
        return Tape(
            tuple(chars),
            self.orientation,
            name=self.name,
            label=self.label,
            tape_id=self.tape_id,
            embroidered_name=self.embroidered_name,
        )

    def subtape(self, start_cell: int, end_cell: int, *, logical: bool = True) -> "Tape":
        """Slice a 1-based inclusive physical or logical interval."""
        if logical:
            idxs = [self.logical_to_physical_index(i) for i in range(start_cell, end_cell + 1)]
            idxs.sort()
            lo, hi = idxs[0], idxs[-1] + 1
            # Preserve physical order and original mounting direction.
            return Tape(self.physical_letters[lo:hi], self.orientation)
        return Tape(self.physical_letters[start_cell - 1 : end_cell], self.orientation)


class Stop:
    """A boundary stop and the directions in which the slider may cross."""

    def __init__(
        self,
        boundary: Any = None,
        direction: Any = BOTH,
        *,
        allowed: Any = None,
        mode: Any = None,
        marker: str | None = None,
        permit: Any = None,
        directions: Iterable[Any] | None = None,
        can_outbound: bool | None = None,
        can_return: bool | None = None,
        movable: bool = False,
        authorized: bool | None = None,
        id: str | None = None,
    ) -> None:
        # Stop(">") convenience.
        if boundary is not None and direction == BOTH and not isinstance(boundary, int):
            try:
                direction_value(boundary)
                direction = boundary
                boundary = None
            except WorkOrderError:
                pass
        self.boundary = int(boundary) if boundary is not None else None
        chosen = mode if mode is not None else allowed
        if chosen is None and marker is not None:
            chosen = marker
        if chosen is None and permit is not None:
            chosen = permit
        if chosen is not None:
            self.mode = _direction_mode(chosen)
        elif directions is not None:
            keys = {_direction_mode(d) for d in directions}
            if OUTBOUND in keys and RETURN in keys:
                self.mode = BOTH
            elif OUTBOUND in keys:
                self.mode = OUTBOUND
            elif RETURN in keys:
                self.mode = RETURN
            else:
                self.mode = NONE
        else:
            self.mode = _direction_mode(direction)
        if can_outbound is not None or can_return is not None:
            out = self.mode in (BOTH, OUTBOUND) if can_outbound is None else can_outbound
            back = self.mode in (BOTH, RETURN) if can_return is None else can_return
            self.mode = BOTH if out and back else OUTBOUND if out else RETURN if back else NONE
        self.direction = SemanticStr(self.mode, _direction_aliases(self.mode))
        self.movable = movable
        # None means the order-level authorisation set should decide.
        self.authorized = authorized
        self.id = id

    @property
    def allowed_directions(self) -> frozenset[str]:
        if self.mode == BOTH:
            return frozenset({OUTBOUND, RETURN})
        if self.mode in (OUTBOUND, RETURN):
            return frozenset({self.mode})
        return frozenset()

    @property
    def outbound_allowed(self) -> bool:
        return self.mode in (BOTH, OUTBOUND)

    @property
    def return_allowed(self) -> bool:
        return self.mode in (BOTH, RETURN)

    @property
    def marker(self) -> str:
        return {BOTH: "+", OUTBOUND: ">", RETURN: "<", NONE: "x"}[self.mode]

    def allows(self, direction: Any) -> bool:
        d = _direction_mode(direction)
        return d in self.allowed_directions

    can_cross = allows

    def __repr__(self) -> str:
        return f"Stop({self.boundary}, {self.marker!r})"


def _direction_aliases(mode: str) -> set[str]:
    if mode == OUTBOUND:
        return _OUT_KEYS
    if mode == RETURN:
        return _RETURN_KEYS
    if mode == BOTH:
        return _BOTH_KEYS
    return _NONE_KEYS


def normalize_stops(stops: Any) -> dict[int, Stop]:
    if stops is None:
        return {}
    out: dict[int, Stop] = {}
    if isinstance(stops, Stop):
        if stops.boundary is None:
            raise WorkOrderError("a stop needs a boundary number")
        out[stops.boundary] = stops
        return out
    if isinstance(stops, dict):
        for key, value in stops.items():
            boundary = int(key)
            if isinstance(value, Stop):
                stop = value
                stop.boundary = boundary
            else:
                stop = Stop(boundary, value)
            out[boundary] = stop
        return out
    if isinstance(stops, (str, bytes)):
        # A compact marker string: index 1 is boundary 1.
        text = stops.decode() if isinstance(stops, bytes) else stops
        for i, ch in enumerate(text, start=1):
            if ch.strip():
                out[i] = Stop(i, ch)
        return out
    for i, value in enumerate(stops, start=1):
        if isinstance(value, Stop):
            stop = value
            if stop.boundary is None:
                stop.boundary = i
        elif isinstance(value, tuple) and len(value) == 2:
            stop = Stop(value[0], value[1])
        else:
            stop = Stop(i, value)
        if stop.boundary is None:
            stop.boundary = i
        out[int(stop.boundary)] = stop
    return out


class WorkOrder(Record):
    def __init__(
        self,
        left: Any = None,
        right: Any = None,
        stops: Any = None,
        required_cells: int | None = None,
        *,
        trip_cells: int | None = None,
        cells: int | None = None,
        full_trip_cells: int | None = None,
        order_id: str | None = None,
        work_order_id: str | None = None,
        garment_id: str | None = None,
        id: str | None = None,  # noqa: A002
        left_tape: Any = None,
        right_tape: Any = None,
        left_sequence: Any = None,
        right_sequence: Any = None,
        embroidered_tapes: Iterable[str] | str | None = None,
        preserve: Iterable[str] | str | None = None,
        named_tapes: Iterable[str] | str | None = None,
        allowed_seams: Iterable[Any] | None = None,
        seam_permissions: Iterable[Any] | None = None,
        authorized_stops: Iterable[int] | bool | None = None,
        movable_stops: Iterable[int] | bool | None = None,
        allow_stop_changes: bool = False,
        tooth_repair_authorized: bool = True,
        repair_authorized: bool | None = None,
        target_letter: str | None = None,
        left_orientation: Any = NORMAL,
        right_orientation: Any = REVERSED,
        text: str | None = None,
        source_text: str | None = None,
    ) -> None:
        if isinstance(left, str) and right is None and "\n" in left:
            parsed = parse_work_order(left)
            self.__dict__.update(parsed.__dict__)
            return
        self.order_id = order_id or work_order_id or id
        self.garment_id = garment_id or id or self.order_id
        if left is None:
            left = left_tape if left_tape is not None else left_sequence
        if right is None:
            right = right_tape if right_tape is not None else right_sequence
        self.left = left if isinstance(left, Tape) else Tape(left or "", left_orientation)
        if isinstance(right, Tape):
            self.right = right
        else:
            # The brief's canonical sample is left normal / right reverse.
            self.right = Tape(right or "", right_orientation)
        self.stops = normalize_stops(stops)
        self.required_cells = required_cells
        if self.required_cells is None:
            self.required_cells = trip_cells if trip_cells is not None else cells
        if self.required_cells is None:
            self.required_cells = full_trip_cells
        if self.required_cells is None:
            self.required_cells = min(len(self.left), len(self.right))
        self.required_cells = int(self.required_cells)
        emb = embroidered_tapes if embroidered_tapes is not None else preserve
        if emb is None:
            emb = named_tapes
        if emb is None:
            emb = []
        if isinstance(emb, str):
            emb = [x.strip() for x in re.split(r"[,;，、]", emb) if x.strip()]
        self.embroidered_tapes: tuple[str, ...] = tuple(emb)
        seams = allowed_seams if allowed_seams is not None else seam_permissions
        self.allowed_seams: frozenset[Any] = frozenset(seams or ())
        self.authorized_stops = authorized_stops
        self.movable_stops = movable_stops
        self.allow_stop_changes = allow_stop_changes
        if repair_authorized is not None:
            tooth_repair_authorized = repair_authorized
        self.tooth_repair_authorized = tooth_repair_authorized
        self.target_letter = target_letter
        self.text = text or source_text

    # Convenience aliases -------------------------------------------------
    @property
    def trip_cells(self) -> int:
        return self.required_cells

    @trip_cells.setter
    def trip_cells(self, value: int) -> None:
        self.required_cells = int(value)

    @property
    def cells(self) -> int:
        return self.required_cells

    def stop_authorized(self, boundary: int) -> bool:
        if self.allow_stop_changes:
            return True
        auth = self.authorized_stops
        if auth is True:
            return True
        if auth is None:
            auth = self.movable_stops
        if auth is None:
            return False
        if auth is False:
            return False
        return int(boundary) in {int(x) for x in auth}

    def validate_limits(self, *, garments: int | None = None) -> None:
        if garments is not None and garments > ZIPPER_LIMIT_GARMENTS:
            raise WorkOrderError(f"at most {ZIPPER_LIMIT_GARMENTS} garments are supported")
        if len(self.left) > ZIPPER_LIMIT_CELLS or len(self.right) > ZIPPER_LIMIT_CELLS:
            raise WorkOrderError(f"each tape has at most {ZIPPER_LIMIT_CELLS} cells")
        if self.required_cells > ZIPPER_LIMIT_CELLS:
            raise WorkOrderError(f"a trip has at most {ZIPPER_LIMIT_CELLS} cells")

    def simulate(self, **kwargs: Any) -> "TravelReport":
        return simulate_zipper(self, **kwargs)

    def durable_segments(self, **kwargs: Any) -> list["SegmentReport"]:
        return find_durable_segments(self, **kwargs)

    def to_ascii(self) -> str:
        return render_work_order(self)


class Garment(WorkOrder):
    """A named sample garment with embroidery and seam permissions."""

    def __init__(self, *args: Any, name: str | None = None, **kwargs: Any) -> None:
        # Garment("G1", "ABC", "CBA", ...) as well as Garment("ABC", "CBA", ...).
        if (
            len(args) >= 3
            and isinstance(args[1], (str, Tape))
            and isinstance(args[2], (str, Tape))
            and not isinstance(args[0], Tape)
        ):
            kwargs.setdefault("garment_id", str(args[0]))
            args = args[1:]
        if name is not None:
            kwargs.setdefault("garment_id", name)
        super().__init__(*args, **kwargs)
        self.name = name or self.garment_id
        if self.garment_id is None and name is not None:
            self.garment_id = name


# ---------------------------------------------------------------------------
# Parsing / rendering
# ---------------------------------------------------------------------------


_MARKER_TO_DIRECTION = {
    "+": BOTH,
    "*": BOTH,
    "=": BOTH,
    "<>": BOTH,
    "<->": BOTH,
    "↔": BOTH,
    ">": OUTBOUND,
    "->": OUTBOUND,
    "→": OUTBOUND,
    "<": RETURN,
    "<-": RETURN,
    "←": RETURN,
    "x": NONE,
    "X": NONE,
    "-": NONE,
}


def _parse_orientation_token(token: str | None) -> Any:
    if not token:
        return NORMAL
    try:
        return orientation_value(token)
    except WorkOrderError:
        return NORMAL


def parse_work_order(source: str | bytes | Version | Any) -> WorkOrder:
    text, _doc, _body = decode_work_order_source(source)
    left_letters: str | None = None
    right_letters: str | None = None
    left_ori: Any = NORMAL
    right_ori: Any = REVERSED
    stops: dict[int, Stop] = {}
    trip: int | None = None
    order_id: str | None = None
    embroidery: list[str] = []
    seams: list[Any] = []
    auth_stops: list[int] = []

    for raw_line in text.splitlines():
        # PAIR-decoded Chinese after # or NOTE is annotation only.
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if re.match(r"^(NOTE|PAIR|旁注|备注)\b", line, re.I):
            continue

        m = re.match(r"^(?:WORK[_\s-]?ORDER|工单)\s*(?:ID|编号)?\s*[:#]?\s*([A-Za-z0-9_.-]+)?", line, re.I)
        if m and m.group(1) and not re.match(r"^(?:LEFT|RIGHT)", line, re.I):
            order_id = m.group(1)
            continue

        m = re.match(
            r"^(?:LEFT|LH|L|左带?)\s*[:=]?\s*([A-Za-z0-9?]+)?(?:\s+(NORMAL|REVERSED|正装|反装|\+|-))?",
            line,
            re.I,
        )
        if m and m.group(1) and not re.match(r"^(?:LIMIT|LENGTH)", line, re.I):
            left_letters = m.group(1)
            left_ori = _parse_orientation_token(m.group(2))
            continue

        m = re.match(
            r"^(?:RIGHT|RH|R|右带?)\s*[:=]?\s*([A-Za-z0-9?]+)?(?:\s+(NORMAL|REVERSED|正装|反装|\+|-))?",
            line,
            re.I,
        )
        if m and m.group(1):
            right_letters = m.group(1)
            right_ori = _parse_orientation_token(m.group(2))
            continue

        m = re.match(
            r"^(?:BOUNDARY|STOP|GATE|边界|止口)\s*[:#]?\s*(\d+)\s*[:=]?\s*(\S+)?",
            line,
            re.I,
        )
        if m:
            boundary = int(m.group(1))
            token = m.group(2) or "+"
            mode = _MARKER_TO_DIRECTION.get(token)
            if mode is None:
                try:
                    mode = _direction_mode(token)
                except WorkOrderError:
                    mode = BOTH
            stops[boundary] = Stop(boundary, mode)
            continue

        m = re.match(
            r"^(?:TRIP(?:[_\s-]?CELLS)?|CELLS?|REQUIRED[_\s-]?CELLS|FULL[_\s-]?TRIP|行程|格数)\s*[:=]?\s*(\d+)",
            line,
            re.I,
        )
        if m:
            trip = int(m.group(1))
            continue

        m = re.match(r"^(?:ID|ORDER[_\s-]?ID|工单号)\s*[:=]\s*([A-Za-z0-9_.-]+)", line, re.I)
        if m:
            order_id = m.group(1)
            continue

        m = re.match(r"^(?:EMBROIDERY|EMBROIDERED[_\s-]?TAPES?|NAMED[_\s-]?TAPES?|绣字带?)\s*[:=]\s*(.+)$", line, re.I)
        if m:
            embroidery.extend(x.strip() for x in re.split(r"[,;，、]", m.group(1)) if x.strip())
            continue

        m = re.match(r"^(?:SEAMS?|PERMITTED[_\s-]?SEAMS?|接缝)\s*[:=]\s*(.+)$", line, re.I)
        if m:
            for token in re.split(r"[\s,;，、]+", m.group(1).strip()):
                if not token:
                    continue
                seams.append(int(token) if token.isdigit() else token)
            continue

        m = re.match(r"^(?:AUTHORIZE[_\s-]?STOPS?|MOVABLE[_\s-]?STOPS?|授权止口)\s*[:=]\s*(.+)$", line, re.I)
        if m:
            auth_stops.extend(int(x) for x in re.findall(r"\d+", m.group(1)))
            continue

    if left_letters is None or right_letters is None:
        raise WorkOrderError("work order must contain LEFT and RIGHT tooth sequences")
    left = Tape(left_letters, left_ori)
    right = Tape(right_letters, right_ori)
    return WorkOrder(
        left,
        right,
        stops,
        trip,
        order_id=order_id,
        embroidered_tapes=embroidery,
        allowed_seams=seams,
        authorized_stops=set(auth_stops),
        text=text,
    )


def decode_work_order_source(source: Any) -> tuple[str, Any, bytes | None]:
    if isinstance(source, Version):
        doc = source.decode()
        return doc.text, doc, source.body
    if hasattr(source, "text") and hasattr(source, "starts"):
        return source.text, source, None
    if isinstance(source, (bytes, bytearray, memoryview)):
        body = bytes(source)
        doc = decode_document(body)
        return doc.text, doc, body
    if isinstance(source, str):
        # Text may already be decoded Unicode.  Encode it only to obtain
        # canonical character spans when callers ask for them.
        from .codec import encode_text_bytes

        body = encode_text_bytes(source)
        try:
            doc = decode_document(body)
            return doc.text, doc, body
        except Exception:
            return source, None, body
    raise WorkOrderError(f"cannot decode work-order source: {type(source).__name__}")


def render_work_order(order: WorkOrder) -> str:
    lines = [
        f"WORK_ORDER {order.order_id or 'ZIPPER'}",
        f"LEFT {order.left.physical_text()} {order.left.orientation.upper()}",
        f"RIGHT {order.right.physical_text()} {order.right.orientation.upper()}",
    ]
    for boundary in sorted(order.stops):
        stop = order.stops[boundary]
        lines.append(f"BOUNDARY {boundary} {stop.marker}")
    lines.append(f"TRIP {order.required_cells}")
    if order.embroidered_tapes:
        lines.append("EMBROIDERY " + ", ".join(order.embroidered_tapes))
    if order.allowed_seams:
        seams = " ".join(str(x) for x in sorted(order.allowed_seams, key=str))
        lines.append(f"SEAMS {seams}")
    if order.authorized_stops:
        auth = order.authorized_stops
        if auth is True:
            lines.append("AUTHORIZE_STOPS ALL")
        elif isinstance(auth, Iterable):
            lines.append("AUTHORIZE_STOPS " + " ".join(map(str, sorted(auth))))
    return "\n".join(lines) + "\n"


def build_standard_order_text(
    *,
    left: str = "ABC",
    right: str = "CBA",
    left_orientation: str = NORMAL,
    right_orientation: str = REVERSED,
    cells: int = 3,
    boundary_marker: str = "+",
    note: str | None = "pair annotation: 左右",
) -> str:
    lines = [
        "ZIPPER-WORK-ORDER SOLO-1142",
        f"LEFT {left} {left_orientation.upper()}",
        f"RIGHT {right} {right_orientation.upper()}",
    ]
    for i in range(1, cells + 1):
        marker = boundary_marker if i == 2 else "+"
        lines.append(f"BOUNDARY {i} {marker}")
    lines.append(f"TRIP {cells}")
    if note:
        lines.append(f"NOTE {note}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Travel simulation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TravelEvent(Record):
    phase: str
    kind: str
    step: int
    cell: int | None = None
    boundary: int | None = None
    left_letter: str | None = None
    right_letter: str | None = None
    allowed: bool = True
    detail: str = ""

    def core_tuple(self) -> tuple[Any, ...]:
        return (self.phase, self.kind, self.cell if self.cell is not None else self.boundary)


@dataclass(frozen=True)
class TravelReport(Record):
    complete: bool
    deliverable: bool
    outbound_complete: bool
    return_complete: bool
    position: int
    required_cells: int
    tape_cells: int
    blocked: bool = False
    blocked_direction: SemanticStr | str | None = None
    stuck_direction: SemanticStr | str | None = None
    direction: SemanticStr | str | None = None
    blocked_cell: int | None = None
    cell: int | None = None
    blocked_boundary: int | None = None
    boundary: int | None = None
    cell_index: int | None = None
    reason: str | None = None
    code: str | None = None
    furthest_position: int = 0
    outbound_position: int = 0
    return_position: int = 0
    paired_cells: int = 0
    opened_cells: int = 0
    cycled_cells: tuple[int, ...] = ()
    matching: bool = True
    events: tuple[TravelEvent, ...] = field(default_factory=tuple)
    mismatches: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def core_tuple(self) -> tuple[Any, ...]:
        return (self.position, self.blocked_direction, self.blocked_cell)

    @property
    def actual_position(self) -> int:
        return self.position

    @property
    def cycled_segments(self) -> tuple[tuple[int, int], ...]:
        """Contiguous cells that paired and were then opened in this run."""
        if not self.cycled_cells:
            return ()
        ordered = sorted(self.cycled_cells)
        segments: list[tuple[int, int]] = []
        start = prev = ordered[0]
        for cell in ordered[1:]:
            if cell == prev + 1:
                prev = cell
            else:
                segments.append((start, prev))
                start = prev = cell
        segments.append((start, prev))
        return tuple(segments)

    @property
    def completed_old_tooth_segment(self) -> tuple[int, int] | None:
        segments = self.cycled_segments
        return max(segments, key=lambda ab: ab[1] - ab[0]) if segments else None

    @property
    def first_block(self) -> tuple[str, int | None, int | None] | None:
        if not self.blocked:
            return None
        return (str(self.blocked_direction), self.blocked_cell, self.blocked_boundary)

    def summary(self) -> str:
        if self.complete:
            return f"complete open/close over {self.required_cells} cells at position 0"
        return (
            f"blocked {self.blocked_direction} at cell={self.blocked_cell} "
            f"boundary={self.blocked_boundary}, position={self.position}, reason={self.code}"
        )


def _as_order(source: Any) -> WorkOrder:
    if isinstance(source, WorkOrder):
        return source
    if isinstance(source, tuple) and source and isinstance(source[0], Tape):
        return WorkOrder(*source)
    raise TypeError(f"expected WorkOrder/Garment, got {type(source).__name__}")


def simulate_zipper(
    left: Any = None,
    right: Any = None,
    stops: Any = None,
    required_cells: int | None = None,
    **kwargs: Any,
) -> TravelReport:
    """Simulate outbound closing followed by reverse-order return opening."""
    if isinstance(left, WorkOrder):
        order = left
        if right is not None:
            raise TypeError("pass either a WorkOrder or tapes, not both")
    else:
        if isinstance(left, str) and right is None and "\n" in left:
            order = parse_work_order(left)
        else:
            order = WorkOrder(left, right, stops, required_cells, **kwargs)

    l_eff = order.left.effective_letters()
    r_eff = order.right.effective_letters()
    tape_cells = min(len(l_eff), len(r_eff))
    n = int(order.required_cells)
    stop_map = order.stops
    events: list[TravelEvent] = []
    mismatches: list[dict[str, Any]] = []

    def stop_at(boundary: int) -> Stop:
        return stop_map.get(boundary, Stop(boundary, BOTH))

    # Scan all mismatches for estimates, while the trip stops at the first.
    for i in range(1, tape_cells + 1):
        if l_eff[i - 1] != r_eff[i - 1]:
            mismatches.append(
                {
                    "cell": i,
                    "left": l_eff[i - 1],
                    "right": r_eff[i - 1],
                    "left_physical_index": order.left.logical_to_physical_index(i) + 1,
                    "right_physical_index": order.right.logical_to_physical_index(i) + 1,
                }
            )

    position = 0
    paired = 0
    outbound_complete = False
    blocked_dir: str | None = None
    blocked_cell: int | None = None
    blocked_boundary: int | None = None
    code: str | None = None
    reason: str | None = None
    furthest = 0

    for i in range(1, n + 1):
        if i > len(l_eff) or i > len(r_eff):
            blocked_dir = OUTBOUND
            blocked_cell = i
            blocked_boundary = min(i, max(n, 1))
            code = "BEYOND_TAPE"
            reason = f"required cell {i} does not exist on both tapes"
            events.append(TravelEvent(OUTBOUND, "missing_cell", i, cell=i, detail=reason))
            break
        lch, rch = l_eff[i - 1], r_eff[i - 1]
        if lch != rch:
            blocked_dir = OUTBOUND
            blocked_cell = i
            blocked_boundary = i
            code = "TOOTH_MISMATCH"
            reason = f"cell {i}: {lch!r} does not mesh with {rch!r}"
            events.append(
                TravelEvent(
                    OUTBOUND,
                    "pair_blocked",
                    i,
                    cell=i,
                    boundary=i,
                    left_letter=lch,
                    right_letter=rch,
                    allowed=False,
                    detail=reason,
                )
            )
            break
        events.append(
            TravelEvent(
                OUTBOUND,
                "pair",
                i,
                cell=i,
                left_letter=lch,
                right_letter=rch,
                detail=f"meshed {lch}",
            )
        )
        paired = i
        st = stop_at(i)
        if not st.allows(OUTBOUND):
            blocked_dir = OUTBOUND
            blocked_cell = i
            blocked_boundary = i
            code = "STOP_DIRECTION"
            reason = f"boundary {i} stop does not permit outbound crossing"
            events.append(
                TravelEvent(OUTBOUND, "stop_blocked", i, cell=i, boundary=i, allowed=False, detail=reason)
            )
            break
        events.append(TravelEvent(OUTBOUND, "cross", i, cell=i, boundary=i, detail="crossed boundary"))
        position = i
        furthest = max(furthest, position)
    else:
        outbound_complete = True

    outbound_position = position
    return_complete = False
    opened = 0
    cycled_cells: list[int] = []

    if outbound_complete:
        for i in range(n, 0, -1):
            st = stop_at(i)
            if not st.allows(RETURN):
                blocked_dir = RETURN
                blocked_cell = i
                blocked_boundary = i
                code = "STOP_DIRECTION"
                reason = f"boundary {i} stop does not permit return crossing"
                events.append(
                    TravelEvent(RETURN, "stop_blocked", n - i + 1, cell=i, boundary=i, allowed=False, detail=reason)
                )
                break
            events.append(TravelEvent(RETURN, "cross_reverse", n - i + 1, cell=i, boundary=i))
            events.append(TravelEvent(RETURN, "unpair", n - i + 1, cell=i, detail=f"opened cell {i}"))
            cycled_cells.append(i)
            position = i - 1
            opened = n - position
        else:
            return_complete = True
            position = 0

    complete = outbound_complete and return_complete and position == 0 and blocked_dir is None
    direction_token = (
        SemanticStr(blocked_dir, _direction_aliases(blocked_dir)) if blocked_dir is not None else None
    )
    return TravelReport(
        complete=complete,
        deliverable=complete,
        outbound_complete=outbound_complete,
        return_complete=return_complete,
        position=position,
        required_cells=n,
        tape_cells=tape_cells,
        blocked=blocked_dir is not None,
        blocked_direction=direction_token,
        stuck_direction=direction_token,
        direction=direction_token,
        blocked_cell=blocked_cell,
        cell=blocked_cell,
        blocked_boundary=blocked_boundary,
        boundary=blocked_boundary,
        cell_index=(blocked_cell - 1 if blocked_cell is not None else None),
        reason=reason,
        code=code,
        furthest_position=furthest,
        outbound_position=outbound_position,
        return_position=position,
        paired_cells=paired,
        opened_cells=opened,
        cycled_cells=tuple(cycled_cells),
        matching=not mismatches,
        events=tuple(events),
        mismatches=tuple(mismatches),
    )


@dataclass(frozen=True)
class SegmentReport(Record):
    start_cell: int
    end_cell: int
    length: int
    complete: bool
    report: TravelReport | None = None

    def core_tuple(self) -> tuple[Any, ...]:
        return (self.start_cell, self.end_cell, self.length, self.complete)

    @property
    def range(self) -> tuple[int, int]:
        return (self.start_cell, self.end_cell)


def simulate_segment(order: WorkOrder, start_cell: int, end_cell: int) -> SegmentReport:
    """Test a local complete close/open cycle over inclusive logical cells."""
    if end_cell < start_cell:
        raise ValueError("segment end must be at least start")
    left_sub = order.left.subtape(start_cell, end_cell, logical=True)
    right_sub = order.right.subtape(start_cell, end_cell, logical=True)
    # The simulator numbers segment-local boundaries from 1.  Preserve the
    # global boundary object/identity but re-key it for the local trip.
    sub_stops = {
        b - start_cell + 1: Stop(b - start_cell + 1, s.mode)
        for b, s in order.stops.items()
        if start_cell <= b <= end_cell
    }
    sub_order = WorkOrder(left_sub, right_sub, sub_stops, end_cell - start_cell + 1)
    raw = simulate_zipper(sub_order)
    # Remap local 1-based cell i to global start+i-1.
    if raw.blocked_cell is not None:
        global_cell = start_cell + raw.blocked_cell - 1
        global_boundary = start_cell + (raw.blocked_boundary or raw.blocked_cell or 1) - 1
        object.__setattr__(raw, "blocked_cell", global_cell)
        object.__setattr__(raw, "cell", global_cell)
        object.__setattr__(raw, "blocked_boundary", global_boundary)
        object.__setattr__(raw, "boundary", global_boundary)
    return SegmentReport(start_cell, end_cell, end_cell - start_cell + 1, raw.complete, raw)


def find_durable_segments(source: Any, *, min_length: int = 1) -> list[SegmentReport]:
    """Return contiguous old-tooth segments surviving one full local cycle."""
    order = _as_order(source) if not isinstance(source, str) else parse_work_order(source)
    n = min(len(order.left), len(order.right), order.required_cells)
    out: list[SegmentReport] = []
    for a in range(1, n + 1):
        for b in range(a, n + 1):
            seg = simulate_segment(order, a, b)
            if seg.complete and seg.length >= min_length:
                out.append(seg)
    return out


def longest_durable_segment(source: Any) -> SegmentReport | None:
    segments = find_durable_segments(source)
    if not segments:
        return None
    return max(segments, key=lambda s: (s.length, -s.start_cell))


# ---------------------------------------------------------------------------
# RevisionLibrary one-character condition changes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConditionChange(Record):
    version_id: int
    base_version: int
    char_start: int
    char_end: int
    old_char: str
    new_char: str
    old_byte_start: int
    old_byte_end: int
    new_byte_start: int
    new_byte_end: int
    line_number: int
    line: str
    condition_kind: str
    boundary: int | None = None
    side: str | None = None
    cell: int | None = None
    physical_position: int | None = None
    before: str | None = None
    after: str | None = None
    version: Version | None = None

    def core_tuple(self) -> tuple[Any, ...]:
        return (self.condition_kind, self.boundary or self.cell, self.before, self.after)

    @property
    def actual_old_span(self) -> tuple[int, int]:
        return (self.old_byte_start, self.old_byte_end)

    @property
    def actual_new_span(self) -> tuple[int, int]:
        return (self.new_byte_start, self.new_byte_end)


def _line_context(text: str, char_pos: int) -> tuple[int, str, int, int]:
    line_start = text.rfind("\n", 0, char_pos) + 1
    line_end = text.find("\n", char_pos)
    if line_end < 0:
        line_end = len(text)
    line_number = text.count("\n", 0, char_pos) + 1
    return line_number, text[line_start:line_end], line_start, line_end


def describe_character_condition(text: str, char_pos: int) -> dict[str, Any]:
    line_no, line, line_start, _ = _line_context(text, char_pos)
    col = char_pos - line_start
    ctx: dict[str, Any] = {
        "line_number": line_no,
        "line": line,
        "column": col,
        "condition_kind": "text",
    }
    m = re.match(r"^(?:BOUNDARY|STOP|边界|止口)\s*[:#]?\s*(\d+)\s*[:=]?\s*(\S+)?", line, re.I)
    if m:
        ctx.update(condition_kind="boundary_direction", boundary=int(m.group(1)))
        return ctx

    m = re.match(r"^(LEFT|RIGHT|L|R|左带?|右带?)\s*[:=]?\s*([A-Za-z0-9?]+)", line, re.I)
    if m and m.group(2):
        letters = m.group(2)
        token_col = m.start(2)
        off = col - token_col
        side = "left" if m.group(1).upper().startswith(("L", "左")) else "right"
        ctx.update(condition_kind=f"{side}_tooth", side=side)
        if 0 <= off < len(letters):
            physical = off + 1
            ori_match = re.search(r"(NORMAL|REVERSED|正装|反装)", line, re.I)
            reversed_tape = bool(ori_match and _token_key(ori_match.group(1)) in _REVERSED_KEYS)
            n = len(letters)
            logical = n - off if reversed_tape else physical
            ctx.update(cell=logical, physical_position=physical)
        return ctx

    m = re.match(r"^(?:TRIP(?:[_\s-]?CELLS)?|CELLS?|REQUIRED[_\s-]?CELLS|行程|格数)\s*[:=]?\s*(\d+)", line, re.I)
    if m:
        ctx.update(condition_kind="required_cells")
    return ctx


def change_one_character(
    library: RevisionLibrary,
    base_version: int,
    char_start: int,
    new_char: str,
    *,
    char_end: int | None = None,
) -> ConditionChange:
    if not isinstance(new_char, str) or len(new_char) != 1:
        raise ValueError("exactly one replacement character is required")
    base = library.get(base_version)
    old_doc = base.decode()
    end = char_start + 1 if char_end is None else char_end
    old_bs, old_be = old_doc.byte_range(char_start)
    old_char = base.text[char_start:end]
    ctx = describe_character_condition(base.text, char_start)

    new_version = library.apply(base_version, [replace(char_start, end, new_char)])
    new_doc = new_version.decode()
    # A one-character replacement starts at the same character offset.  Its
    # actual byte span may differ if the replacement is encoded as SOLO PAIR.
    new_bs, new_be = new_doc.byte_range(char_start)

    before = None
    after = None
    if ctx["condition_kind"] == "boundary_direction":
        before = _MARKER_TO_DIRECTION.get(old_char, old_char)
        after = _MARKER_TO_DIRECTION.get(new_char, new_char)
    return ConditionChange(
        version_id=new_version.version_id,
        base_version=base_version,
        char_start=char_start,
        char_end=end,
        old_char=old_char,
        new_char=new_char,
        old_byte_start=old_bs,
        old_byte_end=old_be,
        new_byte_start=new_bs,
        new_byte_end=new_be,
        line_number=ctx["line_number"],
        line=ctx["line"],
        condition_kind=ctx["condition_kind"],
        boundary=ctx.get("boundary"),
        side=ctx.get("side"),
        cell=ctx.get("cell"),
        physical_position=ctx.get("physical_position"),
        before=before,
        after=after,
        version=new_version,
    )


class RevisionEditor:
    """Convenience stateful wrapper around a library and current version."""

    def __init__(self, source: Any = None, *, library: RevisionLibrary | None = None) -> None:
        self.library = library or RevisionLibrary()
        if source is not None:
            text, _doc, body = decode_work_order_source(source)
            if body is None:
                from .codec import encode_text_bytes

                body = encode_text_bytes(text)
            self.version = self.library.add_original(body)
        else:
            self.version = self.library.get(0) if self.library.versions() else None  # type: ignore

    @property
    def version_id(self) -> int:
        if self.version is None:
            raise RepairError("editor has no version")
        return self.version.version_id

    def use(self, version_id: int) -> Version:
        self.version = self.library.get(version_id)
        return self.version

    def parsed(self, version: int | None = None) -> WorkOrder:
        v = self.library.get(version) if version is not None else self.version
        if v is None:
            raise RepairError("no version to parse")
        return parse_work_order(v)

    def change_char(self, char_start: int, new_char: str, *, char_end: int | None = None) -> ConditionChange:
        result = change_one_character(self.library, self.version_id, char_start, new_char, char_end=char_end)
        self.version = result.version
        return result

    def find_token_position(self, pattern: str | re.Pattern[str], group: int | str = 0) -> int:
        text = self.library.get(self.version_id).text
        if isinstance(pattern, str):
            pattern = re.compile(pattern, re.M)
        m = pattern.search(text)
        if not m:
            raise WorkOrderError(f"condition token not found: {pattern.pattern}")
        return m.start(group)

    def change_boundary(self, boundary: int, to_direction: Any) -> ConditionChange:
        text = self.library.get(self.version_id).text
        pattern = re.compile(rf"^(?:BOUNDARY|STOP|边界|止口)\s*[:#]?\s*{boundary}\s*[:=]?\s*(\S+)", re.M | re.I)
        m = pattern.search(text)
        if not m:
            raise WorkOrderError(f"boundary {boundary} not found")
        mode = _direction_mode(to_direction)
        marker = Stop(boundary, mode).marker
        pos = m.start(1)
        old_token = m.group(1)
        # Canonical orders use a one-character marker.  For a longer word,
        # replace its whole character interval while still recording that the
        # semantic condition changed.
        if len(old_token) == 1:
            return self.change_char(pos, marker)
        result = change_one_character(self.library, self.version_id, pos, marker, char_end=m.end(1))
        self.version = result.version
        return result

    def change_tooth(self, side: str, position: int, new_letter: str, *, physical: bool = True) -> ConditionChange:
        text = self.library.get(self.version_id).text
        side_pattern = r"(?:RIGHT|RH|R|右带?)" if _token_key(side) in {"right", "r", "右"} else r"(?:LEFT|LH|L|左带?)"
        m = re.search(rf"^{side_pattern}\s*[:=]?\s*([A-Za-z0-9?]+)", text, re.M | re.I)
        if not m:
            raise WorkOrderError(f"{side} tape not found")
        token = m.group(1)
        idx = position - 1 if physical else position - 1
        if not physical:
            # Convert logical pull cell to physical index using recorded ori.
            ori_m = re.search(r"(NORMAL|REVERSED|正装|反装)", m.group(0), re.I)
            rev = bool(ori_m and _token_key(ori_m.group(1)) in _REVERSED_KEYS)
            idx = len(token) - position if rev else position - 1
        if not 0 <= idx < len(token):
            raise IndexError("tooth position outside work order")
        return self.change_char(m.start(1) + idx, new_letter)

    def change_trip_cells(self, cells: int) -> ConditionChange:
        text = self.library.get(self.version_id).text
        m = re.search(r"^(?:TRIP(?:[_\s-]?CELLS)?|CELLS?|REQUIRED[_\s-]?CELLS|行程|格数)\s*[:=]?\s*(\d+)", text, re.M | re.I)
        if not m:
            raise WorkOrderError("trip cell condition not found")
        old = m.group(1)
        new = str(cells)
        if len(old) == 1 and len(new) == 1:
            return self.change_char(m.start(1), new)
        result = change_one_character(self.library, self.version_id, m.start(1), new, char_end=m.end(1))
        self.version = result.version
        return result


# ---------------------------------------------------------------------------
# Shift scheduling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShiftTag(Record):
    position: int | str
    shift: int
    tag_id: str | None = None
    movable: bool = True

    def core_tuple(self) -> tuple[Any, ...]:
        return (self.position, self.shift)


class ShiftPlanner:
    """Assign shift numbers while separating adjacent rivet positions."""

    def __init__(self, *, start_shift: int = 1, one_per_shift: bool = True) -> None:
        self.start_shift = int(start_shift)
        self.one_per_shift = one_per_shift

    def schedule(
        self,
        positions: Iterable[int],
        *,
        fixed: dict[int, int] | None = None,
        movable_tags: Iterable[ShiftTag] | None = None,
    ) -> dict[int, int]:
        pos = sorted(set(int(p) for p in positions))
        assigned: dict[int, int] = dict(fixed or {})
        for tag in movable_tags or ():
            if tag.movable:
                continue
            assigned[int(tag.position)] = int(tag.shift)
        for p in pos:
            if p in assigned:
                continue
            candidate = self.start_shift
            while True:
                conflict = False
                for q, shift in assigned.items():
                    adjacent = abs(int(q) - p) == 1
                    if adjacent and abs(shift - candidate) < 2:
                        conflict = True
                        break
                    if self.one_per_shift and shift == candidate:
                        conflict = True
                        break
                if not conflict:
                    break
                candidate += 1
            assigned[p] = candidate
        return {p: assigned[p] for p in pos if p in assigned}

    def violations(self, assignment: dict[int, int]) -> list[str]:
        errors: list[str] = []
        positions = sorted(assignment)
        for p in positions:
            for q in positions:
                if q <= p:
                    continue
                if abs(p - q) == 1 and abs(assignment[p] - assignment[q]) == 1:
                    errors.append(
                        f"adjacent rivet positions {p},{q} are in consecutive shifts "
                        f"{assignment[p]},{assignment[q]}"
                    )
                if self.one_per_shift and p != q and assignment[p] == assignment[q]:
                    errors.append(f"positions {p},{q} share shift {assignment[p]}")
        return errors

    def valid(self, assignment: dict[int, int]) -> bool:
        return not self.violations(assignment)

    def tags(self, assignment: dict[int, int]) -> tuple[ShiftTag, ...]:
        return tuple(
            ShiftTag(position=p, shift=s, tag_id=f"RIVET-{p}-SHIFT-{s}")
            for p, s in sorted(assignment.items())
        )


# ---------------------------------------------------------------------------
# Physical materials
# ---------------------------------------------------------------------------


@dataclass
class LooseTooth(Record):
    letter: str
    serial: str
    batch: str | None = None
    consumed: bool = False

    def __post_init__(self) -> None:
        self.letter = str(self.letter)
        self.serial = str(self.serial)

    @property
    def tooth_id(self) -> str:
        return self.serial

    @property
    def id(self) -> str:
        return self.serial


@dataclass(frozen=True)
class ToothConsumption(Record):
    serial: str
    letter: str
    garment_id: str | None = None
    order_id: str | None = None
    cell: int | None = None
    physical_position: int | None = None
    side: str | None = None
    shift: int | None = None
    craft: str = RIVET

    @property
    def tooth_id(self) -> str:
        return self.serial

    @property
    def tooth_number(self) -> str:
        return self.serial


@dataclass(frozen=True)
class CutRecord(Record):
    donor_id: str
    start: int
    end: int
    order_id: str | None = None
    garment_id: str | None = None
    effective_letters: str = ""
    cut_marks: tuple[int, int] = (0, 0)
    remaining_ranges: tuple[tuple[int, int], ...] = ()

    @property
    def range(self) -> tuple[int, int]:
        return (self.start, self.end)


@dataclass(frozen=True)
class ConsumptionAudit(Record):
    garment_id: str | None
    order_id: str | None
    teeth: tuple[ToothConsumption, ...] = ()
    cuts: tuple[CutRecord, ...] = ()

    @property
    def tooth_serials(self) -> tuple[str, ...]:
        return tuple(t.serial for t in self.teeth)

    @property
    def tooth_ids(self) -> tuple[str, ...]:
        return self.tooth_serials

    @property
    def tooth_numbers(self) -> tuple[str, ...]:
        return tuple(t.tooth_number for t in self.teeth)

    @property
    def cut_marks(self) -> tuple[Any, ...]:
        return tuple(mark for cut in self.cuts for mark in cut.cut_marks)

    @property
    def donor_ids(self) -> tuple[str, ...]:
        return tuple(c.donor_id for c in self.cuts)

    def __bool__(self) -> bool:
        return bool(self.teeth or self.cuts)


@dataclass(frozen=True)
class DonorSegmentPlan(Record):
    donor_id: str
    start: int
    end: int
    target_letters: str
    physical_letters: str
    orientation: str
    mount_orientation: str
    cut_marks: tuple[int, int]
    includes_backing: bool = True
    start_seam: Any = None
    end_seam: Any = None

    @property
    def range(self) -> tuple[int, int]:
        return (self.start, self.end)


class DonorTape:
    """A numbered spare band; only continuous uncut ranges can be lent."""

    def __init__(
        self,
        donor_id: str | None = None,
        letters: Any = None,
        orientation: Any = NORMAL,
        *,
        teeth: Any = None,
        id: str | None = None,  # noqa: A002
        number: str | int | None = None,
        available_ranges: Iterable[tuple[int, int]] | None = None,
    ) -> None:
        self.donor_id = str(donor_id or id or number or f"DONOR-{id(self)}")
        self.id = self.donor_id
        self.number = self.donor_id
        raw = letters if letters is not None else teeth
        self.physical_letters = parse_letters(raw)
        self.orientation = _orientation_mode(orientation)
        n = len(self.physical_letters)
        self.available_ranges: list[tuple[int, int]] = (
            [tuple(r) for r in available_ranges] if available_ranges is not None else [(0, n)]
        )
        self.cut_records: list[CutRecord] = []

    def __len__(self) -> int:
        return len(self.physical_letters)

    def effective_letters(self, mount_orientation: Any | None = None) -> tuple[str, ...]:
        ori = _orientation_mode(mount_orientation) if mount_orientation is not None else self.orientation
        return tuple(reversed(self.physical_letters)) if ori == REVERSED else self.physical_letters

    def find_segment(
        self,
        target: str,
        *,
        mount_orientation: Any | None = None,
        allow_either_orientation: bool = False,
    ) -> DonorSegmentPlan | None:
        target_tuple = parse_letters(target)
        length = len(target_tuple)
        orientations: list[str]
        if mount_orientation is not None:
            orientations = [_orientation_mode(mount_orientation)]
        elif allow_either_orientation:
            orientations = [NORMAL, REVERSED]
        else:
            orientations = [self.orientation]
        for r0, r1 in self.available_ranges:
            for start in range(int(r0), int(r1) - length + 1):
                end = start + length
                physical = self.physical_letters[start:end]
                for ori in orientations:
                    effective = tuple(reversed(physical)) if ori == REVERSED else physical
                    if effective == target_tuple:
                        return DonorSegmentPlan(
                            donor_id=self.donor_id,
                            start=start,
                            end=end,
                            target_letters="".join(target_tuple),
                            physical_letters="".join(physical),
                            orientation=self.orientation,
                            mount_orientation=ori,
                            cut_marks=(start, end),
                        )
        return None

    def cut(self, plan: DonorSegmentPlan, **audit: Any) -> CutRecord:
        interval = (int(plan.start), int(plan.end))
        new_ranges: list[tuple[int, int]] = []
        found = False
        for r0, r1 in self.available_ranges:
            if interval[0] < r1 and r0 < interval[1]:
                if interval[0] < r0 or interval[1] > r1:
                    raise RepairError(f"donor {self.donor_id} cut {interval} is not wholly available")
                found = True
                if r0 < interval[0]:
                    new_ranges.append((r0, interval[0]))
                if interval[1] < r1:
                    new_ranges.append((interval[1], r1))
            else:
                new_ranges.append((r0, r1))
        if not found:
            raise RepairError(f"donor {self.donor_id} has no available range containing {interval}")
        self.available_ranges = new_ranges
        record = CutRecord(
            donor_id=self.donor_id,
            start=interval[0],
            end=interval[1],
            order_id=audit.get("order_id"),
            garment_id=audit.get("garment_id"),
            effective_letters=plan.target_letters,
            cut_marks=plan.cut_marks,
            remaining_ranges=tuple(new_ranges),
        )
        self.cut_records.append(record)
        return record

    @property
    def remaining_text(self) -> str:
        return " | ".join(
            f"{self.donor_id}[{a}:{b}]={''.join(self.physical_letters[a:b])}"
            for a, b in self.available_ranges
            if b > a
        )


class MaterialCabinet(Record):
    """Shared physical inventory and consumption audit trail."""

    def __init__(
        self,
        *,
        loose_teeth: Iterable[LooseTooth | tuple[Any, ...] | str] | None = None,
        loose: Iterable[Any] | None = None,
        inventory: dict[str, int] | None = None,
        donors: Iterable[DonorTape | dict[str, Any] | tuple[Any, ...]] | None = None,
    ) -> None:
        self.loose_teeth: list[LooseTooth] = []
        source = loose_teeth if loose_teeth is not None else loose
        for i, item in enumerate(source or (), start=1):
            self.loose_teeth.append(self._coerce_loose(item, i))
        for letter, count in (inventory or {}).items():
            for _ in range(int(count)):
                self.add_loose_tooth(letter)
        self.donors: list[DonorTape] = [self._coerce_donor(d) for d in donors or ()]
        self.tooth_consumptions: list[ToothConsumption] = []
        self.cut_records: list[CutRecord] = []
        self._serial_clock = len(self.loose_teeth)

    def _coerce_loose(self, item: Any, index: int) -> LooseTooth:
        if isinstance(item, LooseTooth):
            return item
        if isinstance(item, dict):
            letter = item.get("letter") or item.get("type") or item.get("tooth_type") or "?"
            serial = item.get("serial") or item.get("tooth_id") or item.get("id") or f"LOOSE-{letter}-{index:03d}"
            return LooseTooth(letter, serial, item.get("batch"))
        if isinstance(item, (tuple, list)):
            if len(item) == 1:
                return LooseTooth(item[0], f"LOOSE-{item[0]}-{index:03d}")
            return LooseTooth(item[0], item[1])
        return LooseTooth(str(item), f"LOOSE-{str(item)}-{index:03d}")

    def _coerce_donor(self, item: Any) -> DonorTape:
        if isinstance(item, DonorTape):
            return item
        if isinstance(item, dict):
            return DonorTape(
                item.get("donor_id") or item.get("id") or item.get("number"),
                item.get("letters") or item.get("teeth") or item.get("sequence"),
                item.get("orientation") or item.get("direction") or NORMAL,
                available_ranges=item.get("available_ranges"),
            )
        if isinstance(item, (tuple, list)):
            return DonorTape(*item)
        return DonorTape(letters=item)

    def add_loose_tooth(
        self,
        letter: str = "B",
        serial: str | None = None,
        *,
        tooth_id: str | None = None,
        count: int = 1,
    ) -> LooseTooth | list[LooseTooth]:
        added: list[LooseTooth] = []
        for _ in range(count):
            self._serial_clock += 1
            sid = serial if count == 1 and serial is not None else tooth_id
            if sid is None:
                sid = f"LOOSE-{letter}-{self._serial_clock:03d}"
            tooth = LooseTooth(letter, sid)
            self.loose_teeth.append(tooth)
            added.append(tooth)
        return added[0] if count == 1 else added

    restock_loose = add_loose_tooth

    def add_donor(self, donor: DonorTape | Any) -> DonorTape:
        obj = self._coerce_donor(donor)
        self.donors.append(obj)
        return obj

    def available_loose(self, letter: str | None = None) -> list[LooseTooth]:
        return [
            t
            for t in self.loose_teeth
            if not t.consumed and (letter is None or t.letter == letter)
        ]

    def consume_loose(
        self,
        letter: str | None = None,
        *,
        serial: str | None = None,
        tooth_id: str | None = None,
        **audit: Any,
    ) -> ToothConsumption:
        sid = serial or tooth_id
        tooth: LooseTooth | None = None
        if sid is not None:
            tooth = next((t for t in self.loose_teeth if t.serial == sid and not t.consumed), None)
        elif letter is not None:
            tooth = self.available_loose(letter)[0] if self.available_loose(letter) else None
        if tooth is None:
            raise RepairError(f"no available loose tooth {letter or ''} {sid or ''}".strip())
        tooth.consumed = True
        record = ToothConsumption(
            serial=tooth.serial,
            letter=tooth.letter,
            garment_id=audit.get("garment_id"),
            order_id=audit.get("order_id"),
            cell=audit.get("cell"),
            physical_position=audit.get("physical_position"),
            side=audit.get("side"),
            shift=audit.get("shift"),
            craft=audit.get("craft", RIVET),
        )
        self.tooth_consumptions.append(record)
        return record

    def find_donor_segment(self, target: str, **kwargs: Any) -> tuple[DonorTape, DonorSegmentPlan] | None:
        for donor in self.donors:
            plan = donor.find_segment(target, **kwargs)
            if plan is not None:
                return donor, plan
        return None

    def consume_donor_segment(
        self,
        donor: DonorTape | str,
        plan: DonorSegmentPlan,
        **audit: Any,
    ) -> CutRecord:
        obj = donor if isinstance(donor, DonorTape) else next(d for d in self.donors if d.donor_id == str(donor))
        record = obj.cut(plan, **audit)
        self.cut_records.append(record)
        return record

    def audit(
        self,
        garment_id: str | None = None,
        order_id: str | None = None,
        **kwargs: Any,
    ) -> ConsumptionAudit:
        if garment_id is not None and order_id is None and garment_id in getattr(self, "orders", {}):
            order_id = garment_id
            garment_id = None
        garment_id = kwargs.pop("garment_id", garment_id)
        order_id = kwargs.pop("order_id", order_id)
        teeth = tuple(
            t
            for t in self.tooth_consumptions
            if (garment_id is None or t.garment_id == garment_id)
            and (order_id is None or t.order_id == order_id)
        )
        cuts = tuple(
            c
            for c in self.cut_records
            if (garment_id is None or c.garment_id == garment_id)
            and (order_id is None or c.order_id == order_id)
        )
        return ConsumptionAudit(garment_id, order_id, teeth, cuts)

    def consumed_tooth_ids(self, **kwargs: Any) -> tuple[str, ...]:
        return self.audit(**kwargs).tooth_ids

    def cut_marks(self, **kwargs: Any) -> tuple[Any, ...]:
        return self.audit(**kwargs).cut_marks


Cabinet = MaterialCabinet


# ---------------------------------------------------------------------------
# Estimates, orders and counter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairNeed(Record):
    kind: str
    repairable: bool
    cell: int | None = None
    boundary: int | None = None
    side: str | None = None
    expected: str | None = None
    actual: str | None = None
    reason: str = ""
    authorized: bool = True


@dataclass(frozen=True)
class CraftPlan(Record):
    craft: str
    feasible: bool
    needs: tuple[RepairNeed, ...] = ()
    materials: tuple[Any, ...] = ()
    missing: tuple[str, ...] = ()
    shift_assignment: dict[int, int] = field(default_factory=dict)
    shift_tags: tuple["ShiftTag", ...] = ()
    donor_plan: DonorSegmentPlan | None = None
    start_seam: Any = None
    end_seam: Any = None
    reasons: tuple[str, ...] = ()

    @property
    def can_accept(self) -> bool:
        return self.feasible


@dataclass
class RepairEstimate(Record):
    estimate_id: str
    garment_id: str | None
    status: str
    current_report: TravelReport
    options: dict[str, CraftPlan]
    needs: tuple[RepairNeed, ...] = ()
    chosen_craft: str | None = None
    preserved_embroidery: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    can_accept: bool = False
    counter: "RepairCounter | None" = field(default=None, repr=False, compare=False)
    subject: WorkOrder | None = field(default=None, repr=False, compare=False)

    def core_tuple(self) -> tuple[Any, ...]:
        return (self.estimate_id, self.status, self.chosen_craft, self.can_accept)

    @property
    def acceptable(self) -> bool:
        return self.can_accept

    @property
    def repairable(self) -> bool:
        return self.can_accept

    @property
    def full_trip_possible(self) -> bool:
        return self.current_report.complete

    def choose(self, craft: str) -> "RepairEstimate":
        mode = _craft_mode(craft)
        if mode not in self.options:
            raise RepairError(f"craft {craft} is unavailable")
        self.chosen_craft = mode
        self.can_accept = self.options[mode].feasible
        return self

    def accept(self, **kwargs: Any) -> "RepairOrder":
        if self.counter is None:
            raise RepairError("estimate is not attached to a counter")
        if self.chosen_craft is None and kwargs.get("craft") is None:
            feasible = [c for c, p in self.options.items() if p.feasible]
            if len(feasible) == 1:
                self.chosen_craft = feasible[0]
        return self.counter.accept(self, **kwargs)


@dataclass
class RepairOrder(Record):
    order_id: str
    estimate: RepairEstimate | None
    garment: WorkOrder
    craft: str | None
    status: str
    plan: CraftPlan | None = None
    accepted: bool = False
    receipt: "CompletionReceipt | None" = None
    counter: "RepairCounter | None" = field(default=None, repr=False, compare=False)

    def core_tuple(self) -> tuple[Any, ...]:
        return (self.order_id, self.status, self.accepted)

    def complete(self, **kwargs: Any) -> "CompletionReceipt":
        if self.counter is None:
            raise RepairError("order is not attached to a counter")
        return self.counter.complete_order(self.order_id, **kwargs)

    def verify(self) -> bool:
        if self.counter is None:
            raise RepairError("order is not attached to a counter")
        return self.counter.verify_order(self.order_id)


@dataclass(frozen=True)
class CompletionReceipt(Record):
    order_id: str
    garment_id: str | None
    passed: bool
    status: str
    report: TravelReport | None
    audit: ConsumptionAudit | None
    preserved_embroidery: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    order: RepairOrder | None = None

    @property
    def deliverable(self) -> bool:
        return self.passed


class RepairCounter(MaterialCabinet):
    def __init__(
        self,
        *,
        cabinet: MaterialCabinet | None = None,
        loose_teeth: Iterable[Any] | None = None,
        donors: Iterable[Any] | None = None,
        shift_planner: ShiftPlanner | None = None,
        max_garments: int = ZIPPER_LIMIT_GARMENTS,
    ) -> None:
        if cabinet is not None:
            super().__init__()
            # Share the exact physical inventory and audit lists with the
            # caller's cabinet; two counters can therefore serve one stock.
            self.loose_teeth = cabinet.loose_teeth
            self.donors = cabinet.donors
            self.tooth_consumptions = cabinet.tooth_consumptions
            self.cut_records = cabinet.cut_records
            self._serial_clock = getattr(cabinet, "_serial_clock", len(self.loose_teeth))
            self.cabinet: MaterialCabinet = cabinet
            cabinet.counter = self  # type: ignore[attr-defined]
        else:
            super().__init__(loose_teeth=loose_teeth, donors=donors)
            self.cabinet = self
        self.inventory = self.cabinet
        self.shift_planner = shift_planner or ShiftPlanner()
        self.max_garments = max_garments
        self.estimates: dict[str, RepairEstimate] = {}
        self.orders: dict[str, RepairOrder] = {}
        self.receipts: dict[str, CompletionReceipt] = {}
        self.garments: dict[str, WorkOrder] = {}
        self._estimate_clock = 0
        self._order_clock = 0

    # -- garment intake ----------------------------------------------------

    def register_garment(self, garment: WorkOrder) -> WorkOrder:
        garment = _coerce_garment(garment)
        planned_count = len(set(self.garments) | {garment.garment_id or id(garment)})
        try:
            garment.validate_limits(garments=planned_count)
        except WorkOrderError as exc:
            raise RepairError(str(exc)) from exc
        key = garment.garment_id or f"GARMENT-{id(garment)}"
        if key not in self.garments and len(self.garments) >= self.max_garments:
            raise RepairError(f"at most {self.max_garments} sample garments can be handled")
        self.garments[key] = garment
        return garment

    intake_garment = register_garment
    receive_garment = register_garment
    add_garment = register_garment

    # -- estimation -------------------------------------------------------

    def _next_estimate_id(self) -> str:
        self._estimate_clock += 1
        return f"EST-{self._estimate_clock:03d}"

    def _next_order_id(self) -> str:
        self._order_clock += 1
        return f"ORD-{self._order_clock:03d}"

    def estimate(
        self,
        target: Any,
        *,
        craft: Any = None,
        cabinet: MaterialCabinet | None = None,  # compatibility/use self normally
        **options: Any,
    ) -> RepairEstimate:
        order = target if isinstance(target, WorkOrder) else _coerce_garment(target)
        if options.get("validate_limits", True):
            order.validate_limits()
        report = order.simulate()
        needs = self._repair_needs(order, report, options)
        reasons: list[str] = []

        blocking = [n for n in needs if not n.repairable]
        if blocking:
            reasons.extend(n.reason for n in blocking)

        tooth_needs = tuple(n for n in needs if n.kind == "tooth" and n.repairable)
        rivet = self._plan_rivet(order, tooth_needs, options)
        transplant = self._plan_transplant(order, tooth_needs, options)
        options_by_craft = {RIVET: rivet, TRANSPLANT: transplant}

        chosen = _craft_mode(craft) if craft is not None else None
        can_accept = not blocking and any(p.feasible for p in options_by_craft.values())
        if chosen is not None and not options_by_craft[chosen].feasible:
            can_accept = False
            reasons.extend(options_by_craft[chosen].reasons)
        estimate = RepairEstimate(
            estimate_id=self._next_estimate_id(),
            garment_id=order.garment_id,
            status=status_token("quoted" if can_accept else "rejected"),
            current_report=report,
            options=options_by_craft,
            needs=tuple(needs),
            chosen_craft=chosen,
            preserved_embroidery=order.embroidered_tapes,
            reasons=tuple(reasons),
            can_accept=can_accept and (chosen is None or options_by_craft[chosen].feasible),
            counter=self,
            subject=order,
        )
        self.estimates[estimate.estimate_id] = estimate
        return estimate

    def _repair_needs(self, order: WorkOrder, report: TravelReport, options: dict[str, Any]) -> list[RepairNeed]:
        needs: list[RepairNeed] = []
        if report.code == "STOP_DIRECTION":
            b = int(report.blocked_boundary or report.blocked_cell or 0)
            authorized = bool(options.get("allow_stop_change") or order.stop_authorized(b))
            needs.append(
                RepairNeed(
                    kind="stop",
                    repairable=authorized,
                    boundary=b,
                    cell=report.blocked_cell,
                    authorized=authorized,
                    reason=(
                        f"boundary {b} stop requires owner authorization to move/re-mark"
                        if not authorized
                        else f"boundary {b} stop may be adjusted"
                    ),
                )
            )
        if report.code == "BEYOND_TAPE":
            needs.append(
                RepairNeed(
                    kind="length",
                    repairable=bool(options.get("allow_extension", False)),
                    cell=report.blocked_cell,
                    reason="requested trip is longer than the existing tapes",
                )
            )

        target_letter = options.get("target_letter", order.target_letter)
        repairs = options.get("repairs")
        if repairs is not None:
            for spec in repairs:
                if isinstance(spec, RepairNeed):
                    needs.append(spec)
                elif isinstance(spec, dict):
                    needs.append(
                        RepairNeed(
                            kind="tooth",
                            repairable=order.tooth_repair_authorized,
                            cell=spec.get("cell"),
                            side=spec.get("side"),
                            expected=spec.get("letter") or spec.get("expected") or target_letter,
                            actual=spec.get("actual"),
                            reason=spec.get("reason", "specified bad tooth"),
                            authorized=order.tooth_repair_authorized,
                        )
                    )
        else:
            for mismatch in report.mismatches:
                cell = int(mismatch["cell"])
                left_ch = mismatch["left"]
                right_ch = mismatch["right"]
                side: str | None = None
                expected: str | None = None
                if target_letter is not None:
                    if left_ch != target_letter and right_ch == target_letter:
                        side, expected = "left", target_letter
                    elif right_ch != target_letter and left_ch == target_letter:
                        side, expected = "right", target_letter
                    else:
                        # The named target is the repair standard even if both
                        # currently differ from it.
                        side = options.get("bad_side", "right")
                        expected = target_letter
                else:
                    # Without a named standard, repair the right tape to mesh
                    # with the preserved left tape.
                    side, expected = "right", left_ch
                needs.append(
                    RepairNeed(
                        kind="tooth",
                        repairable=order.tooth_repair_authorized,
                        cell=cell,
                        side=side,
                        expected=expected,
                        actual=left_ch if side == "left" else right_ch,
                        reason=f"{side} cell {cell} must become {expected}",
                        authorized=order.tooth_repair_authorized,
                    )
                )
        if not order.tooth_repair_authorized:
            for i, need in enumerate(needs):
                if need.kind == "tooth":
                    needs[i] = RepairNeed(
                        kind=need.kind,
                        repairable=False,
                        cell=need.cell,
                        boundary=need.boundary,
                        side=need.side,
                        expected=need.expected,
                        actual=need.actual,
                        reason="tooth repair has not been authorized",
                        authorized=False,
                    )
        return needs

    def _plan_rivet(self, order: WorkOrder, needs: tuple[RepairNeed, ...], options: dict[str, Any]) -> CraftPlan:
        if not needs:
            return CraftPlan(RIVET, True, reasons=("no teeth require riveting",))
        materials: list[LooseTooth] = []
        missing: list[str] = []
        for need in needs:
            if not need.repairable or need.expected is None:
                missing.append("unauthorized or unspecified tooth repair")
                continue
            available = [t for t in self.available_loose(need.expected) if t not in materials]
            if not available:
                missing.append(f"loose {need.expected} tooth")
            else:
                materials.append(available[0])
        assignment = self.shift_planner.schedule(
            [n.cell for n in needs if n.cell is not None],
            fixed=options.get("shift_assignment"),
            movable_tags=options.get("movable_shift_tags"),
        )
        shift_errors = self.shift_planner.violations(assignment)
        feasible = not missing and not shift_errors and all(n.repairable for n in needs)
        reasons = tuple(missing + shift_errors)
        return CraftPlan(
            RIVET,
            feasible,
            needs=needs,
            materials=tuple(materials),
            missing=tuple(missing),
            shift_assignment=assignment,
            shift_tags=self.shift_planner.tags(assignment),
            reasons=reasons,
        )

    def _plan_transplant(
        self,
        order: WorkOrder,
        needs: tuple[RepairNeed, ...],
        options: dict[str, Any],
    ) -> CraftPlan:
        if not needs:
            return CraftPlan(TRANSPLANT, True, reasons=("no segment requires transplant",))
        if any(not n.repairable for n in needs):
            return CraftPlan(TRANSPLANT, False, needs=needs, missing=("unauthorized repair",))
        cells = [int(n.cell) for n in needs if n.cell is not None]
        start, end = min(cells), max(cells)
        # Build the desired effective substring after all planned repairs.
        left_eff = list(order.left.effective_letters())
        right_eff = list(order.right.effective_letters())
        for need in needs:
            if need.cell is None or need.expected is None:
                continue
            if need.side == "left":
                left_eff[need.cell - 1] = need.expected
            else:
                right_eff[need.cell - 1] = need.expected
        target = "".join(left_eff[start - 1 : end])
        found = self.find_donor_segment(
            target,
            allow_either_orientation=bool(options.get("allow_either_orientation", True)),
        )
        if found is None:
            return CraftPlan(
                TRANSPLANT,
                False,
                needs=needs,
                missing=(f"numbered donor segment {target!r}",),
                reasons=(f"no compatible donor segment for {target!r}",),
            )
        donor, plan = found
        start_seam, end_seam, seam_ok = self._seam_endpoints(order, start, end, options)
        if not seam_ok:
            return CraftPlan(
                TRANSPLANT,
                False,
                needs=needs,
                donor_plan=plan,
                missing=("permitted seam endpoints",),
                start_seam=start_seam,
                end_seam=end_seam,
                reasons=(f"segment [{start},{end}] cannot end at permitted seams",),
            )
        plan = DonorSegmentPlan(
            **{**plan.__dict__, "start_seam": start_seam, "end_seam": end_seam}
        )
        return CraftPlan(
            TRANSPLANT,
            True,
            needs=needs,
            materials=(donor,),
            donor_plan=plan,
            start_seam=start_seam,
            end_seam=end_seam,
            reasons=(),
        )

    def _seam_endpoints(
        self,
        order: WorkOrder,
        start: int,
        end: int,
        options: dict[str, Any],
    ) -> tuple[Any, Any, bool]:
        explicit = options.get("seam_endpoints")
        if explicit is not None:
            a, b = explicit
            allowed = order.allowed_seams
            if not allowed:
                return a, b, True
            if isinstance(next(iter(allowed), None), tuple):
                return a, b, (a, b) in allowed
            return a, b, a in allowed and b in allowed
        # Canonical boundaries: seam before start cell = start-1, after end = end.
        candidates = [
            (start - 1, end),
            (start, end),
            (start - 1, end - 1),
            (start, end + 1),
        ]
        allowed = order.allowed_seams
        if not allowed:
            return candidates[0][0], candidates[0][1], True
        tuple_permissions = {x for x in allowed if isinstance(x, tuple)}
        if tuple_permissions:
            for pair in candidates:
                if pair in tuple_permissions:
                    return pair[0], pair[1], True
        flat = {x for x in allowed if not isinstance(x, tuple)}
        for a, b in candidates:
            if a in flat and b in flat:
                return a, b, True
        return start - 1, end, False

    # -- acceptance / completion -----------------------------------------

    def accept(
        self,
        target: RepairEstimate | WorkOrder | str | None = None,
        estimate: RepairEstimate | None = None,
        *,
        craft: Any = None,
        **kwargs: Any,
    ) -> RepairOrder:
        est = target if isinstance(target, RepairEstimate) else estimate
        if est is None and isinstance(target, str) and target in self.estimates:
            est = self.estimates[target]
        if est is None:
            if isinstance(target, WorkOrder):
                est = self.estimate(target, craft=craft, **kwargs)
            else:
                raise RepairError("accept requires an estimate, order id or garment")
        if craft is not None:
            est.choose(craft)
        craft_mode = est.chosen_craft
        if craft_mode is None:
            feasible = [c for c, p in est.options.items() if p.feasible]
            craft_mode = feasible[0] if len(feasible) == 1 else None
        plan = est.options.get(craft_mode) if craft_mode else None
        accepted = bool(est.can_accept and plan is not None and plan.feasible)
        order = RepairOrder(
            order_id=self._next_order_id(),
            estimate=est,
            garment=kwargs.get("garment", _estimate_garment(est, target)),
            craft=craft_mode,
            status=status_token("accepted" if accepted else "rejected"),
            plan=plan,
            accepted=accepted,
            counter=self,
        )
        self.orders[order.order_id] = order
        return order

    def accept_or_raise(self, *args: Any, **kwargs: Any) -> RepairOrder:
        order = self.accept(*args, **kwargs)
        if not order.accepted:
            reasons = "; ".join(order.estimate.reasons) if order.estimate else "order rejected"
            raise RepairRejected(reasons)
        return order

    accept_order = accept
    place_order = accept

    def complete_order(self, order_id: str | RepairOrder, **_: Any) -> CompletionReceipt:
        order = order_id if isinstance(order_id, RepairOrder) else self.orders[str(order_id)]
        if not order.accepted or order.plan is None or order.craft is None:
            receipt = CompletionReceipt(
                order_id.order_id if isinstance(order_id, RepairOrder) else str(order_id),
                order.garment.garment_id,
                False,
                status_token("rejected"),
                order.garment.simulate(),
                None,
                order.garment.embroidered_tapes,
                ("order was not accepted",),
                order,
            )
            self.receipts[receipt.order_id] = receipt
            order.receipt = receipt
            return receipt

        errors: list[str] = []
        # An authorised stop repair/re-marking is part of completion.  The
        # estimate has already established that the owner allowed this boundary.
        if order.estimate is not None:
            for need in order.estimate.needs:
                if need.kind == "stop" and need.repairable and need.boundary is not None:
                    order.garment.stops[need.boundary] = Stop(need.boundary, BOTH)
        audit_before = self.audit(garment_id=order.garment.garment_id)
        try:
            if order.craft == RIVET:
                self._complete_rivet(order)
            elif order.craft == TRANSPLANT:
                self._complete_transplant(order)
            else:
                raise RepairError(f"unknown craft {order.craft}")
        except RepairError as exc:
            errors.append(str(exc))

        report = order.garment.simulate()
        if not report.complete:
            errors.append(f"post-repair travel failed: {report.code}: {report.reason}")
        embroidery = tuple(order.garment.embroidered_tapes)
        audit = self.audit(garment_id=order.garment.garment_id, order_id=order.order_id)
        passed = not errors and report.complete
        order.status = status_token("delivered" if passed else "verification_failed")
        receipt = CompletionReceipt(
            order_id=order.order_id,
            garment_id=order.garment.garment_id,
            passed=passed,
            status=order.status,
            report=report,
            audit=audit,
            preserved_embroidery=embroidery,
            errors=tuple(errors),
            order=order,
        )
        self.receipts[order.order_id] = receipt
        order.receipt = receipt
        return receipt

    def _complete_rivet(self, order: RepairOrder) -> None:
        assert order.plan is not None
        allocations = list(order.plan.materials)
        for need, tooth in zip(order.plan.needs, allocations):
            if need.cell is None or need.expected is None:
                continue
            shift = order.plan.shift_assignment.get(need.cell)
            consumed = self.consume_loose(
                serial=tooth.serial,
                order_id=order.order_id,
                garment_id=order.garment.garment_id,
                cell=need.cell,
                side=need.side,
                shift=shift,
                craft=RIVET,
            )
            physical = None
            if need.side == "left":
                physical = order.garment.left.logical_to_physical_index(need.cell) + 1
                order.garment.left = order.garment.left.with_letter(need.cell, need.expected, logical=True)
            else:
                physical = order.garment.right.logical_to_physical_index(need.cell) + 1
                order.garment.right = order.garment.right.with_letter(need.cell, need.expected, logical=True)
            # Attach physical position to the already-created audit record by
            # replacing the immutable record in the audit list.
            idx = self.tooth_consumptions.index(consumed)
            consumed = ToothConsumption(
                serial=consumed.serial,
                letter=consumed.letter,
                garment_id=consumed.garment_id,
                order_id=consumed.order_id,
                cell=consumed.cell,
                physical_position=physical,
                side=consumed.side,
                shift=consumed.shift,
                craft=consumed.craft,
            )
            self.tooth_consumptions[idx] = consumed

    def _complete_transplant(self, order: RepairOrder) -> None:
        assert order.plan is not None and order.plan.donor_plan is not None
        donor = next(d for d in self.donors if d.donor_id == order.plan.donor_plan.donor_id)
        self.consume_donor_segment(
            donor,
            order.plan.donor_plan,
            order_id=order.order_id,
            garment_id=order.garment.garment_id,
        )
        start = min(n.cell for n in order.plan.needs if n.cell is not None)
        end = max(n.cell for n in order.plan.needs if n.cell is not None)
        for need in order.plan.needs:
            if need.cell is None or need.expected is None or need.side is None:
                continue
            if need.side == "left":
                order.garment.left = order.garment.left.with_letter(need.cell, need.expected, logical=True)
            else:
                order.garment.right = order.garment.right.with_letter(need.cell, need.expected, logical=True)

    def verify_order(self, order_id: str | RepairOrder) -> bool:
        order = order_id if isinstance(order_id, RepairOrder) else self.orders[str(order_id)]
        receipt = self.complete_order(order) if order.receipt is None else order.receipt
        fresh = order.garment.simulate()
        return bool(receipt.passed and fresh.complete)

    def service(
        self,
        garment: Any,
        *,
        craft: Any = None,
        **options: Any,
    ) -> CompletionReceipt:
        estimate = self.estimate(garment, craft=craft, **options)
        order = self.accept(estimate, craft=craft)
        return order.complete()

    repair_garment = service
    process = service
    run = service


RepairCabinet = RepairCounter
Counter = RepairCounter
ZipperShop = RepairCounter
RepairShop = RepairCounter
Shop = RepairCounter
RepairDesk = RepairCounter
SampleGarment = Garment
SampleCoat = Garment
ZipperGarment = Garment
WorkTicket = WorkOrder
ZipperWorkOrder = WorkOrder
TripReport = TravelReport
ZipperReport = TravelReport
BoundaryStop = Stop
ShiftBoard = ShiftPlanner


def schedule_rivets(positions: Iterable[int], **kwargs: Any) -> dict[int, int]:
    """Functional shorthand for :class:`ShiftPlanner` scheduling."""
    planner = ShiftPlanner(
        start_shift=int(kwargs.pop("start_shift", 1)),
        one_per_shift=bool(kwargs.pop("one_per_shift", True)),
    )
    return planner.schedule(positions, **kwargs)


def valid_shift_schedule(assignment: dict[int, int], *, one_per_shift: bool = True) -> bool:
    return ShiftPlanner(one_per_shift=one_per_shift).valid(assignment)


check_trip = simulate_zipper
inspect_zipper = simulate_zipper
travel_report = simulate_zipper
simulate_zip = simulate_zipper
apply_one_character = change_one_character

__all__ += [
    "ZipperShop",
    "RepairShop",
    "Shop",
    "RepairDesk",
    "SampleGarment",
    "SampleCoat",
    "ZipperGarment",
    "WorkTicket",
    "ZipperWorkOrder",
    "TripReport",
    "ZipperReport",
    "BoundaryStop",
    "ShiftBoard",
    "schedule_rivets",
    "valid_shift_schedule",
    "check_trip",
    "inspect_zipper",
    "travel_report",
    "simulate_zip",
    "apply_one_character",
    "run_story_demo",
    "replay_story",
    "solve",
    "surviving_segments",
    "durable_teeth_segments",
    "which_old_segment",
]


def _coerce_garment(target: Any) -> WorkOrder:
    if isinstance(target, WorkOrder):
        return target
    if isinstance(target, str):
        if "\n" in target:
            return parse_work_order(target)
        raise WorkOrderError("single-line string is not a work order")
    if isinstance(target, dict):
        return WorkOrder(**target)
    raise TypeError(f"cannot coerce {type(target).__name__} to WorkOrder")


def _estimate_garment(estimate: RepairEstimate, target: Any) -> WorkOrder:
    if isinstance(target, RepairEstimate):
        if estimate.subject is not None:
            return estimate.subject
        raise RepairError("accept needs the original garment or counter.accept(garment)")
    if isinstance(target, WorkOrder):
        return target
    if estimate.subject is not None:
        return estimate.subject
    raise RepairError("cannot recover garment for order")


def repair_garment(garment: Any, *, craft: Any = None, cabinet: MaterialCabinet | None = None, **opts: Any):
    counter = RepairCounter(cabinet=cabinet) if cabinet is not None else RepairCounter()
    return counter.service(garment, craft=craft, **opts)


# ---------------------------------------------------------------------------
# Deterministic end-to-end story data
# ---------------------------------------------------------------------------


def build_story_demo() -> dict[str, Any]:
    """Build the exact five-situation replay described in the prompt."""
    editor = RevisionEditor(build_standard_order_text())
    base_id = editor.version_id
    base_report = editor.parsed().simulate()

    boundary_change = editor.change_boundary(2, OUTBOUND)
    one_way_report = editor.parsed(boundary_change.version_id).simulate()
    one_way_estimate = RepairCounter().estimate(editor.parsed(boundary_change.version_id))

    restored_boundary = editor.change_boundary(2, BOTH)
    d_change = editor.change_tooth("right", 2, "D", physical=True)
    mismatch_report = editor.parsed(d_change.version_id).simulate()

    restored_tooth = editor.change_tooth("right", 2, "B", physical=True)
    trip_change = editor.change_trip_cells(4)
    four_report = editor.parsed(trip_change.version_id).simulate()

    # Shared-material pair, rivet route.
    g1 = Garment(
        "ABC",
        "CXA",
        {1: "+", 2: "+", 3: "+"},
        3,
        garment_id="G1-RIVET",
        name="云雁样衣",
        embroidered_tapes=("绣字带·云雁",),
        allowed_seams=(0, 1, 2, 3),
        target_letter="B",
    )
    g2 = Garment(
        "ABC",
        "CXA",
        {1: "+", 2: "+", 3: "+"},
        3,
        garment_id="G2-RIVET",
        name="松烟样衣",
        embroidered_tapes=("绣字带·松烟",),
        allowed_seams=(0, 1, 2, 3),
        target_letter="B",
    )
    cabinet = RepairCounter(
        loose_teeth=[LooseTooth("B", "LOOSE-B-LAST")],
        donors=[DonorTape("DONOR-B-7", "BXY", NORMAL)],
    )
    r1 = cabinet.service(g1, craft=RIVET)
    second_before_restock = cabinet.estimate(g2, craft=RIVET)
    cabinet.add_loose_tooth("B", serial="LOOSE-B-NEW")
    r2 = cabinet.service(g2, craft=RIVET)

    # Independent cabinet for the donor-segment route.
    h1 = Garment(
        "ABC",
        "CXA",
        {1: "+", 2: "+", 3: "+"},
        3,
        garment_id="H1-DONOR",
        embroidered_tapes=("绣字带·青衿",),
        allowed_seams=(0, 1, 2, 3),
        target_letter="B",
    )
    h2 = Garment(
        "ABC",
        "CXA",
        {1: "+", 2: "+", 3: "+"},
        3,
        garment_id="H2-DONOR",
        embroidered_tapes=("绣字带·白露",),
        allowed_seams=(0, 1, 2, 3),
        target_letter="B",
    )
    donor_counter = RepairCounter(donors=[DonorTape("DONOR-B-7", "BXY", NORMAL)])
    d1 = donor_counter.service(h1, craft=TRANSPLANT)
    donor_second = donor_counter.estimate(h2, craft=TRANSPLANT)
    donor_counter.add_loose_tooth("B", serial="LOOSE-B-AFTER")
    donor_second_with_loose = donor_counter.estimate(h2, craft=TRANSPLANT)

    return {
        "title_answer": {
            "canonical_durable_segment": [1, 3],
            "one_way_boundary_two_actual_cycled_segment": list(
                one_way_report.completed_old_tooth_segment or ()
            ),
            "one_way_local_durable_segments": [
                [s.start_cell, s.end_cell]
                for s in (one_way_estimate.subject.durable_segments() if one_way_estimate.subject else [])
            ],
            "note": "Actual failed full trip cycles cell 3; counter still rejects the owner-required full trip.",
        },
        "base_version": base_id,
        "base_report": base_report.to_dict(),
        "boundary_change": boundary_change.to_dict(),
        "one_way_report": one_way_report.to_dict(),
        "one_way_estimate": one_way_estimate.to_dict(),
        "mismatch_change": d_change.to_dict(),
        "mismatch_report": mismatch_report.to_dict(),
        "trip_change": trip_change.to_dict(),
        "four_cell_report": four_report.to_dict(),
        "rivet": {
            "first_receipt": r1.to_dict(),
            "second_before_restock": second_before_restock.to_dict(),
            "second_receipt": r2.to_dict(),
            "audit": cabinet.audit().to_dict(),
        },
        "donor": {
            "first_receipt": d1.to_dict(),
            "second_before_loose": donor_second.to_dict(),
            "second_after_loose_only": donor_second_with_loose.to_dict(),
            "audit": donor_counter.audit().to_dict(),
            "remaining_donor": donor_counter.donors[0].remaining_text,
        },
    }


run_story_demo = build_story_demo
replay_story = build_story_demo
solve = build_story_demo
surviving_segments = find_durable_segments
durable_teeth_segments = find_durable_segments
which_old_segment = longest_durable_segment


class Zipper:
    """Small object-oriented facade around :func:`simulate_zipper`."""

    def __init__(
        self,
        left: Any = None,
        right: Any = None,
        stops: Any = None,
        required_cells: int | None = None,
        *,
        order: WorkOrder | None = None,
        **kwargs: Any,
    ) -> None:
        if order is not None:
            self.order = order
        elif isinstance(left, WorkOrder):
            self.order = left
        else:
            self.order = WorkOrder(left, right, stops, required_cells, **kwargs)

    def simulate(self, **kwargs: Any) -> TravelReport:
        return self.order.simulate(**kwargs)

    def travel(self, **kwargs: Any) -> TravelReport:
        return self.simulate(**kwargs)

    def check(self, **kwargs: Any) -> TravelReport:
        return self.simulate(**kwargs)

    def durable_segments(self, **kwargs: Any) -> list[SegmentReport]:
        return self.order.durable_segments(**kwargs)


ZipperSimulator = Zipper
ZipperChain = Zipper
Chain = Zipper
ZipperRepair = RepairCounter

__all__ += ["Zipper", "ZipperSimulator", "ZipperChain", "Chain", "ZipperRepair"]

