"""Equipment handover for the astronomy club, integrated with the ledger.

The club borrows eyepieces and adapter rings that members bought.  A piece
enters the pool only when the lender signs it off against a *valid sale unit*
(:meth:`OpticsService.register_piece`); the counter trusts the per-unit
after-sales row, not a model number, so two identical-looking pieces stay
distinct physical items ("同型号不得互相顶名").  Each piece carries its borrow
contract: return (退货), repair (送修) and give-back (归还) deadlines.

Focus model
-----------
``net focus demand = eyepiece focal position - total optical path`` over every
inserted piece.  The focus wheel travels an inclusive integer interval
``[travel_lo, travel_hi]`` and may be adjusted on site; both endpoints count.
An eyepiece's optical path is its shoulder measurement interval
``[measured_lo, measured_hi]`` plus the zero-point bias shared by its barrel
batch.  Adapter rings have an integer optical path and a directional interface.

A delivery is legal only if the *whole* net interval fits inside the wheel
travel for **every compatible calibration** (a robust check), never merely one
point estimate.

Delivery strategy (one of two, chosen)
--------------------------------------
Pieces are pre-locked into **verified barrel groups**: telescope + eyepiece +
ordered adapter chain, focus-verified by a signed trial.  A verified sealed
group may be handed to a duty officer as a whole, but no member may be lent in
isolation while the group is sealed; unsealing forces re-verification.  The
rejected alternative (loose pieces with a live trial that consumes a real
observing window) is deliberately not implemented.

Observatory scheduling
----------------------
Sessions are time intervals; two consecutive sessions may overlap.  Every
piece is exclusively booked over its session interval (strict overlap: touching
endpoints do not conflict).  Once a piece has been checked in it cannot be
promised to another instrument while that booking stays active.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .db import Database, canonical_json, json_loads, utc_now
from .errors import (
    FOCUS_UNREACHABLE,
    GROUP_NOT_VERIFIED,
    GROUP_SEALED,
    INCOMPATIBLE_CHAIN,
    INVALID_OPTIC,
    OPTIC_LIMIT,
    ORDER_CONFLICT,
    ORDER_STATE,
    PIECE_BOOKED,
    PIECE_IMPERSONATION,
    PIECE_UNAVAILABLE,
    SCOPE_BOOKED,
    UNKNOWN_BATCH,
    UNKNOWN_GROUP,
    UNKNOWN_ORDER,
    UNKNOWN_PIECE,
    UNKNOWN_SESSION,
    UNKNOWN_TELESCOPE,
    UNIT_INELIGIBLE,
    BasketError,
)
from .validation import is_int, require_id

MAX_TELESCOPES = 3
MAX_PIECES = 6
MAX_SESSIONS = 3
KINDS = ("eyepiece", "adapter")


# --------------------------------------------------------------------------- #
# small validation helpers
# --------------------------------------------------------------------------- #
def _err(code: str, message: str, **details: Any) -> None:
    raise BasketError(code, message, details or None)


def _int(value: Any, field: str, *, minimum: Optional[int] = None) -> int:
    if not is_int(value):
        _err(INVALID_OPTIC, f"{field} must be an integer", field=field, value=value)
    if minimum is not None and value < minimum:
        _err(INVALID_OPTIC, f"{field} must be >= {minimum}", field=field, value=value)
    return value


def _opt_int(value: Any, field: str, *, minimum: Optional[int] = None) -> Optional[int]:
    if value is None:
        return None
    return _int(value, field, minimum=minimum)


def _iface(value: Any, field: str) -> str:
    return require_id(value, INVALID_OPTIC, field)


def _overlaps(a_lo: int, a_hi: int, b_lo: int, b_hi: int) -> bool:
    """Strict interval overlap: merely touching endpoints do not conflict."""

    return a_lo < b_hi and b_lo < a_hi


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# the service
# --------------------------------------------------------------------------- #
class OpticsService:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------ telescopes
    def add_telescope(
        self,
        telescope_id: str,
        out_iface: str,
        travel_lo: int,
        travel_hi: int,
    ) -> Dict[str, Any]:
        telescope_id = require_id(telescope_id, INVALID_OPTIC, "telescope_id")
        out_iface = _iface(out_iface, "out_iface")
        travel_lo = _int(travel_lo, "travel_lo")
        travel_hi = _int(travel_hi, "travel_hi")
        if travel_lo > travel_hi:
            _err(INVALID_OPTIC, "travel_lo must not exceed travel_hi")
        content = {"telescope_id": telescope_id, "out_iface": out_iface,
                   "travel_lo": travel_lo, "travel_hi": travel_hi}
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM telescopes WHERE telescope_id = ?", (telescope_id,)).fetchone()
            if row is not None:
                stored = {"telescope_id": row["telescope_id"], "out_iface": row["out_iface"],
                          "travel_lo": row["travel_lo"], "travel_hi": row["travel_hi"]}
                if stored != content:
                    _err(INVALID_OPTIC, "telescope_id reused with different content", telescope_id=telescope_id)
                return stored
            count = conn.execute("SELECT COUNT(*) AS n FROM telescopes").fetchone()["n"]
            if count >= MAX_TELESCOPES:
                _err(OPTIC_LIMIT, f"at most {MAX_TELESCOPES} telescopes", limit=MAX_TELESCOPES)
            conn.execute(
                "INSERT INTO telescopes (telescope_id, out_iface, travel_lo, travel_hi, created_at) VALUES (?, ?, ?, ?, ?)",
                (telescope_id, out_iface, travel_lo, travel_hi, utc_now()),
            )
        return content

    def get_telescope(self, telescope_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM telescopes WHERE telescope_id = ?", (telescope_id,)).fetchone()
        if row is None:
            _err(UNKNOWN_TELESCOPE, "unknown telescope", telescope_id=telescope_id)
        return {"telescope_id": row["telescope_id"], "out_iface": row["out_iface"],
                "travel_lo": row["travel_lo"], "travel_hi": row["travel_hi"]}

    # ---------------------------------------------------------- barrel batches
    def add_batch(
        self,
        batch_id: str,
        scale_lo: int,
        scale_hi: int,
        cards: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Register a barrel-shoulder batch and recover its shared bias.

        The two-end scale bounds the bias within ``[scale_lo, scale_hi]``; each
        known test-focus card gives a point estimate.  The recovered bias is the
        intersection, an inclusive integer interval representing *all* compatible
        calibrations.
        """

        batch_id = require_id(batch_id, INVALID_OPTIC, "batch_id")
        scale_lo = _int(scale_lo, "scale_lo")
        scale_hi = _int(scale_hi, "scale_hi")
        if scale_lo > scale_hi:
            _err(INVALID_OPTIC, "scale_lo must not exceed scale_hi")
        canonical_cards = self._validate_cards(cards)
        bias_lo, bias_hi = self._recover_bias(scale_lo, scale_hi, canonical_cards)
        content = {
            "batch_id": batch_id,
            "bias_lo": bias_lo,
            "bias_hi": bias_hi,
            "scale_lo": scale_lo,
            "scale_hi": scale_hi,
            "cards": canonical_cards,
        }
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM barrel_batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if row is not None:
                stored = {
                    "batch_id": row["batch_id"],
                    "bias_lo": row["bias_lo"],
                    "bias_hi": row["bias_hi"],
                    "scale_lo": row["scale_lo"],
                    "scale_hi": row["scale_hi"],
                    "cards": json_loads(row["card_json"]),
                }
                if stored != content:
                    _err("CALIBRATION_CONFLICT", "batch_id reused with different content", batch_id=batch_id)
                return stored
            conn.execute(
                "INSERT INTO barrel_batches (batch_id, bias_lo, bias_hi, scale_lo, scale_hi, card_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, bias_lo, bias_hi, scale_lo, scale_hi, canonical_json(canonical_cards), utc_now()),
            )
        return content

    def _validate_cards(self, cards: Any) -> List[Dict[str, int]]:
        if cards is None or isinstance(cards, (str, bytes, Mapping)):
            _err(INVALID_OPTIC, "cards must be a list")
        if not isinstance(cards, Sequence):
            _err(INVALID_OPTIC, "cards must be a list")
        out: List[Dict[str, int]] = []
        for index, card in enumerate(cards):
            if not isinstance(card, Mapping):
                _err(INVALID_OPTIC, "card must be an object", index=index)
            focal = _int(card.get("focal_position"), f"cards[{index}].focal_position")
            ref_path = _int(card.get("ref_path"), f"cards[{index}].ref_path")
            wheel = _int(card.get("wheel"), f"cards[{index}].wheel")
            out.append({"focal_position": focal, "ref_path": ref_path, "wheel": wheel})
        return out

    def _recover_bias(self, scale_lo: int, scale_hi: int, cards: Sequence[Mapping[str, int]]) -> Tuple[int, int]:
        points = [c["focal_position"] - c["ref_path"] - c["wheel"] for c in cards]
        lo = scale_lo
        hi = scale_hi
        if points:
            lo = max(lo, min(points))
            hi = min(hi, max(points))
        if lo > hi:
            _err("CALIBRATION_INCONSISTENT",
                 "two-end scale and test cards admit no common bias",
                 scale_lo=scale_lo, scale_hi=scale_hi, card_points=points)
        return lo, hi

    def calibrate_batch(self, batch_id: str, cards: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """Re-open calibration: recover the bias again from signed test cards."""

        batch_id = require_id(batch_id, INVALID_OPTIC, "batch_id")
        canonical_cards = self._validate_cards(cards)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM barrel_batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if row is None:
                _err(UNKNOWN_BATCH, "unknown batch", batch_id=batch_id)
            bias_lo, bias_hi = self._recover_bias(row["scale_lo"], row["scale_hi"], canonical_cards)
            conn.execute(
                "UPDATE barrel_batches SET bias_lo = ?, bias_hi = ?, card_json = ? WHERE batch_id = ?",
                (bias_lo, bias_hi, canonical_json(canonical_cards), batch_id),
            )
        return {"batch_id": batch_id, "bias_lo": bias_lo, "bias_hi": bias_hi, "cards": canonical_cards}

    def get_batch(self, batch_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM barrel_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if row is None:
            _err(UNKNOWN_BATCH, "unknown batch", batch_id=batch_id)
        return {"batch_id": row["batch_id"], "bias_lo": row["bias_lo"], "bias_hi": row["bias_hi"],
                "scale_lo": row["scale_lo"], "scale_hi": row["scale_hi"], "cards": json_loads(row["card_json"])}

    # ----------------------------------------------------------------- pieces
    def register_piece(
        self,
        piece_id: str,
        kind: str,
        model: str,
        sale_id: str,
        unit_id: str,
        *,
        batch_id: Optional[str] = None,
        focal_position: Optional[int] = None,
        in_iface: Optional[str] = None,
        out_iface: Optional[str] = None,
        nominal_path: Optional[int] = None,
        measured_lo: Optional[int] = None,
        measured_hi: Optional[int] = None,
        return_deadline: Optional[int] = None,
        repair_deadline: Optional[int] = None,
        lend_return_deadline: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Intake one physical piece signed on a valid, unreturned sale unit."""

        piece_id = require_id(piece_id, INVALID_OPTIC, "piece_id")
        model = require_id(model, INVALID_OPTIC, "model")
        sale_id = require_id(sale_id, INVALID_OPTIC, "sale_id")
        unit_id = require_id(unit_id, INVALID_OPTIC, "unit_id")
        if kind not in KINDS:
            _err(INVALID_OPTIC, "kind must be eyepiece or adapter", kind=kind)

        if kind == "eyepiece":
            if batch_id is None:
                _err(INVALID_OPTIC, "an eyepiece needs a barrel batch")
            batch_id = require_id(batch_id, INVALID_OPTIC, "batch_id")
            focal_position = _int(focal_position, "focal_position")
            in_iface = _iface(in_iface, "in_iface")
            if measured_lo is None or measured_hi is None:
                _err(INVALID_OPTIC, "an eyepiece needs shoulder measurements")
            measured_lo = _int(measured_lo, "measured_lo")
            measured_hi = _int(measured_hi, "measured_hi")
            if measured_lo > measured_hi:
                _err(INVALID_OPTIC, "measured_lo must not exceed measured_hi")
            out_iface = None
            nominal_path = None
        else:
            in_iface = _iface(in_iface, "in_iface")
            out_iface = _iface(out_iface, "out_iface")
            nominal_path = _int(nominal_path, "nominal_path", minimum=0)
            batch_id = None
            focal_position = None
            measured_lo = None
            measured_hi = None

        deadlines = {
            "return_deadline": _opt_int(return_deadline, "return_deadline", minimum=0),
            "repair_deadline": _opt_int(repair_deadline, "repair_deadline", minimum=0),
            "lend_return_deadline": _opt_int(lend_return_deadline, "lend_return_deadline", minimum=0),
        }

        content = {
            "piece_id": piece_id, "kind": kind, "model": model, "sale_id": sale_id, "unit_id": unit_id,
            "batch_id": batch_id, "focal_position": focal_position, "in_iface": in_iface,
            "out_iface": out_iface, "nominal_path": nominal_path, "measured_lo": measured_lo,
            "measured_hi": measured_hi, **deadlines,
        }

        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM optic_pieces WHERE piece_id = ?", (piece_id,)).fetchone()
            if existing is not None:
                stored = self._piece_dict(existing)
                comparable = {k: stored[k] for k in content}
                if comparable != content:
                    _err(INVALID_OPTIC, "piece_id reused with different content", piece_id=piece_id)
                return self._piece_result(conn, piece_id)
            count = conn.execute("SELECT COUNT(*) AS n FROM optic_pieces").fetchone()["n"]
            if count >= MAX_PIECES:
                _err(OPTIC_LIMIT, f"at most {MAX_PIECES} pieces", limit=MAX_PIECES)

            if batch_id is not None:
                if conn.execute("SELECT 1 FROM barrel_batches WHERE batch_id = ?", (batch_id,)).fetchone() is None:
                    _err(UNKNOWN_BATCH, "unknown batch", batch_id=batch_id)

            # The lender must sign a real, unreturned unit on a valid sale.
            unit = conn.execute(
                "SELECT su.sku, su.returned FROM sale_units su JOIN sales s ON s.sale_id = su.sale_id "
                "WHERE su.sale_id = ? AND su.unit_id = ?",
                (sale_id, unit_id),
            ).fetchone()
            if unit is None:
                _err(UNIT_INELIGIBLE, "unit_id is not part of the named sale", sale_id=sale_id, unit_id=unit_id)
            if unit["returned"]:
                _err(PIECE_UNAVAILABLE, "sale unit has been returned and cannot be lent", unit_id=unit_id)
            if unit["sku"] != model:
                _err(INVALID_OPTIC, "piece model must match the sale unit's sku",
                     unit_sku=unit["sku"], model=model)

            # No two pieces may impersonate the same physical unit.
            clash = conn.execute(
                "SELECT piece_id FROM optic_pieces WHERE sale_id = ? AND unit_id = ?", (sale_id, unit_id)
            ).fetchone()
            if clash is not None and clash["piece_id"] != piece_id:
                _err(PIECE_IMPERSONATION, "sale unit already backs another piece",
                     sale_id=sale_id, unit_id=unit_id, piece_id=clash["piece_id"])

            conn.execute(
                "INSERT INTO optic_pieces (piece_id, kind, model, batch_id, group_id, focal_position, in_iface, out_iface, "
                "nominal_path, measured_lo, measured_hi, status, sale_id, unit_id, return_deadline, repair_deadline, "
                "lend_return_deadline, created_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'IN_STOCK', ?, ?, ?, ?, ?, ?)",
                (piece_id, kind, model, batch_id, focal_position, in_iface, out_iface, nominal_path,
                 measured_lo, measured_hi, sale_id, unit_id, deadlines["return_deadline"],
                 deadlines["repair_deadline"], deadlines["lend_return_deadline"], utc_now()),
            )
            return self._piece_result(conn, piece_id)

    def _piece_result(self, conn, piece_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM optic_pieces WHERE piece_id = ?", (piece_id,)).fetchone()
        return self._piece_dict(row)

    def _piece_dict(self, row) -> Dict[str, Any]:
        return {
            "piece_id": row["piece_id"],
            "kind": row["kind"],
            "model": row["model"],
            "batch_id": row["batch_id"],
            "group_id": row["group_id"],
            "focal_position": row["focal_position"],
            "in_iface": row["in_iface"],
            "out_iface": row["out_iface"],
            "nominal_path": row["nominal_path"],
            "measured_lo": row["measured_lo"],
            "measured_hi": row["measured_hi"],
            "status": row["status"],
            "sale_id": row["sale_id"],
            "unit_id": row["unit_id"],
            "return_deadline": row["return_deadline"],
            "repair_deadline": row["repair_deadline"],
            "lend_return_deadline": row["lend_return_deadline"],
        }

    def get_piece(self, piece_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM optic_pieces WHERE piece_id = ?", (piece_id,)).fetchone()
        if row is None:
            _err(UNKNOWN_PIECE, "unknown piece", piece_id=piece_id)
        return self._piece_dict(row)

    def update_piece_evidence(
        self, piece_id: str, measured_lo: int, measured_hi: int
    ) -> Dict[str, Any]:
        """Re-record an eyepiece's shoulder-measurement evidence.

        Only allowed while the piece is in stock and not sealed into a group, so
        a locked group's stored focus interval can never silently go stale.
        """

        piece_id = require_id(piece_id, INVALID_OPTIC, "piece_id")
        measured_lo = _int(measured_lo, "measured_lo")
        measured_hi = _int(measured_hi, "measured_hi")
        if measured_lo > measured_hi:
            _err(INVALID_OPTIC, "measured_lo must not exceed measured_hi")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM optic_pieces WHERE piece_id = ?", (piece_id,)).fetchone()
            if row is None:
                _err(UNKNOWN_PIECE, "unknown piece", piece_id=piece_id)
            if row["kind"] != "eyepiece":
                _err(INVALID_OPTIC, "only an eyepiece carries shoulder evidence", piece_id=piece_id)
            if row["group_id"] is not None:
                _err(GROUP_SEALED, "unseal the group before changing shoulder evidence",
                     piece_id=piece_id, group_id=row["group_id"])
            conn.execute("UPDATE optic_pieces SET measured_lo = ?, measured_hi = ? WHERE piece_id = ?",
                         (measured_lo, measured_hi, piece_id))
            result = self._piece_result(conn, piece_id)
            result["path_interval"] = list(self._piece_path_interval(conn, conn.execute(
                "SELECT * FROM optic_pieces WHERE piece_id = ?", (piece_id,)).fetchone()))
        return result

    def assess_combo(
        self, telescope_id: str, eyepiece_id: str, adapter_ids: Sequence[str]
    ) -> Dict[str, Any]:
        """Read-only feasibility of a candidate combo under all calibrations."""

        telescope_id = require_id(telescope_id, INVALID_OPTIC, "telescope_id")
        eyepiece_id = require_id(eyepiece_id, INVALID_OPTIC, "eyepiece_id")
        if isinstance(adapter_ids, (str, bytes, Mapping)) or not isinstance(adapter_ids, Sequence):
            _err(INVALID_OPTIC, "adapter_ids must be a list")
        adapter_ids = [require_id(a, INVALID_OPTIC, f"adapter_ids[{i}]") for i, a in enumerate(adapter_ids)]
        if len(set(adapter_ids)) != len(adapter_ids):
            _err(INVALID_OPTIC, "an adapter may appear only once in a chain")
        with self.db.connection() as conn:
            telescope = conn.execute("SELECT * FROM telescopes WHERE telescope_id = ?",
                                     (telescope_id,)).fetchone()
            if telescope is None:
                _err(UNKNOWN_TELESCOPE, "unknown telescope", telescope_id=telescope_id)
            pieces = self._load_pieces(conn, [eyepiece_id, *adapter_ids])
            if pieces[eyepiece_id]["kind"] != "eyepiece":
                _err(INVALID_OPTIC, "combo eyepiece is not an eyepiece", piece_id=eyepiece_id)
            for aid in adapter_ids:
                if pieces[aid]["kind"] != "adapter":
                    _err(INVALID_OPTIC, "combo chain contains a non-adapter", piece_id=aid)
            self._check_chain(conn, telescope, pieces[eyepiece_id], [pieces[a] for a in adapter_ids])
            total_lo, total_hi, net_lo, net_hi = self._focus_interval(
                conn, pieces[eyepiece_id], [pieces[a] for a in adapter_ids])
        deliverable = telescope["travel_lo"] <= net_lo and net_hi <= telescope["travel_hi"]
        return {
            "telescope_id": telescope_id,
            "eyepiece_id": eyepiece_id,
            "adapter_ids": adapter_ids,
            "total_lo": total_lo,
            "total_hi": total_hi,
            "net_lo": net_lo,
            "net_hi": net_hi,
            "travel_lo": telescope["travel_lo"],
            "travel_hi": telescope["travel_hi"],
            "deliverable": deliverable,
            "needed_travel": net_hi,
        }

    def piece_path_interval(self, piece_id: str) -> Tuple[int, int]:
        """Optical path interval after insertion, using the piece's batch bias."""

        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM optic_pieces WHERE piece_id = ?", (piece_id,)).fetchone()
            if row is None:
                _err(UNKNOWN_PIECE, "unknown piece", piece_id=piece_id)
            return self._piece_path_interval(conn, row)

    def _piece_path_interval(self, conn, row) -> Tuple[int, int]:
        if row["kind"] == "adapter":
            return row["nominal_path"], row["nominal_path"]
        batch = conn.execute("SELECT bias_lo, bias_hi FROM barrel_batches WHERE batch_id = ?",
                             (row["batch_id"],)).fetchone()
        if batch is None:
            _err(UNKNOWN_BATCH, "eyepiece batch is missing", piece_id=row["piece_id"])
        return row["measured_lo"] + batch["bias_lo"], row["measured_hi"] + batch["bias_hi"]

    # ---------------------------------------------------------------- groups
    def lock_group(
        self,
        group_id: str,
        telescope_id: str,
        eyepiece_id: str,
        adapter_ids: Sequence[str],
    ) -> Dict[str, Any]:
        """Pre-lock pieces into a sealed barrel group (unverified until a trial)."""

        group_id = require_id(group_id, INVALID_OPTIC, "group_id")
        telescope_id = require_id(telescope_id, INVALID_OPTIC, "telescope_id")
        eyepiece_id = require_id(eyepiece_id, INVALID_OPTIC, "eyepiece_id")
        if isinstance(adapter_ids, (str, bytes, Mapping)) or not isinstance(adapter_ids, Sequence):
            _err(INVALID_OPTIC, "adapter_ids must be a list")
        adapter_ids = [require_id(a, INVALID_OPTIC, f"adapter_ids[{i}]") for i, a in enumerate(adapter_ids)]
        if len(set(adapter_ids)) != len(adapter_ids):
            _err(INVALID_OPTIC, "an adapter may appear only once in a chain")
        if eyepiece_id in adapter_ids:
            _err(INVALID_OPTIC, "an eyepiece cannot also be an adapter in the same chain")

        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM optic_groups WHERE group_id = ?", (group_id,)).fetchone()
            if existing is not None:
                return self._group_result(conn, group_id)

            telescope = conn.execute("SELECT * FROM telescopes WHERE telescope_id = ?", (telescope_id,)).fetchone()
            if telescope is None:
                _err(UNKNOWN_TELESCOPE, "unknown telescope", telescope_id=telescope_id)

            pieces = self._load_pieces(conn, [eyepiece_id, *adapter_ids])
            if pieces[eyepiece_id]["kind"] != "eyepiece":
                _err(INVALID_OPTIC, "group eyepiece is not an eyepiece", piece_id=eyepiece_id)
            for aid in adapter_ids:
                if pieces[aid]["kind"] != "adapter":
                    _err(INVALID_OPTIC, "group chain contains a non-adapter", piece_id=aid)
            for piece in pieces.values():
                if piece["status"] != "IN_STOCK":
                    _err(PIECE_UNAVAILABLE, "piece is not in stock", piece_id=piece["piece_id"])
                if piece["group_id"] is not None:
                    _err(GROUP_SEALED, "piece already belongs to a sealed group; unseal and re-verify first",
                         piece_id=piece["piece_id"], group_id=piece["group_id"])

            self._check_chain(conn, telescope, pieces[eyepiece_id], [pieces[a] for a in adapter_ids])

            total_lo, total_hi, net_lo, net_hi = self._focus_interval(conn, pieces[eyepiece_id],
                                                                      [pieces[a] for a in adapter_ids])
            if not (telescope["travel_lo"] <= net_lo and net_hi <= telescope["travel_hi"]):
                _err(FOCUS_UNREACHABLE, "group cannot be guaranteed to reach focus on this telescope",
                     net_lo=net_lo, net_hi=net_hi, travel_lo=telescope["travel_lo"],
                     travel_hi=telescope["travel_hi"])

            conn.execute(
                "INSERT INTO optic_groups (group_id, telescope_id, eyepiece_id, adapter_json, total_lo, total_hi, "
                "net_lo, net_hi, travel_lo, travel_hi, verified, sealed, verify_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, '{}', ?)",
                (group_id, telescope_id, eyepiece_id, canonical_json(adapter_ids), total_lo, total_hi,
                 net_lo, net_hi, telescope["travel_lo"], telescope["travel_hi"], utc_now()),
            )
            for piece_id in [eyepiece_id, *adapter_ids]:
                conn.execute("UPDATE optic_pieces SET group_id = ? WHERE piece_id = ?", (group_id, piece_id))
            return self._group_result(conn, group_id)

    def verify_group(self, group_id: str, *, observe_wheel: int, technician: str) -> Dict[str, Any]:
        """Signed focus trial: record evidence that the sealed group focuses."""

        group_id = require_id(group_id, INVALID_OPTIC, "group_id")
        observe_wheel = _int(observe_wheel, "observe_wheel")
        technician = require_id(technician, INVALID_OPTIC, "technician")
        with self.db.transaction() as conn:
            group = conn.execute("SELECT * FROM optic_groups WHERE group_id = ?", (group_id,)).fetchone()
            if group is None:
                _err(UNKNOWN_GROUP, "unknown group", group_id=group_id)
            net_lo, net_hi = group["net_lo"], group["net_hi"]
            if not (net_lo <= observe_wheel <= net_hi):
                _err(FOCUS_UNREACHABLE, "observed wheel setting lies outside the guaranteed focus interval",
                     observe_wheel=observe_wheel, net_lo=net_lo, net_hi=net_hi)
            evidence = {
                "group_id": group_id,
                "observe_wheel": observe_wheel,
                "technician": technician,
                "net_lo": net_lo,
                "net_hi": net_hi,
                "total_lo": group["total_lo"],
                "total_hi": group["total_hi"],
                "signed_at": utc_now(),
            }
            conn.execute("UPDATE optic_groups SET verified = 1, sealed = 1, verify_json = ? WHERE group_id = ?",
                         (canonical_json(evidence), group_id))
            return self._group_result(conn, group_id)

    def unseal_group(self, group_id: str) -> Dict[str, Any]:
        """Break the seal so members may be re-grouped; forces re-verification."""

        group_id = require_id(group_id, INVALID_OPTIC, "group_id")
        with self.db.transaction() as conn:
            group = conn.execute("SELECT * FROM optic_groups WHERE group_id = ?", (group_id,)).fetchone()
            if group is None:
                _err(UNKNOWN_GROUP, "unknown group", group_id=group_id)
            active = conn.execute(
                "SELECT 1 FROM optic_bookings WHERE order_id IN "
                "(SELECT order_id FROM optic_orders WHERE group_id = ? AND status != 'CANCELLED') "
                "AND active = 1 LIMIT 1",
                (group_id,),
            ).fetchone()
            if active is not None:
                _err(GROUP_SEALED, "cannot unseal a group with active bookings", group_id=group_id)
            conn.execute("UPDATE optic_groups SET sealed = 0, verified = 0 WHERE group_id = ?", (group_id,))
            conn.execute("UPDATE optic_pieces SET group_id = NULL WHERE group_id = ?", (group_id,))
            return self._group_result(conn, group_id)

    def _check_chain(self, conn, telescope, eyepiece, adapters: Sequence[Mapping[str, Any]]) -> None:
        """Head-to-tail interface match: scope -> adapters -> eyepiece."""

        expected = telescope["out_iface"]
        for adapter in adapters:
            if adapter["in_iface"] != expected:
                _err(INCOMPATIBLE_CHAIN, "adapter input does not match the previous output",
                     piece_id=adapter["piece_id"], expected=expected, got=adapter["in_iface"])
            expected = adapter["out_iface"]
        if eyepiece["in_iface"] != expected:
            _err(INCOMPATIBLE_CHAIN, "eyepiece input does not match the chain output",
                 piece_id=eyepiece["piece_id"], expected=expected, got=eyepiece["in_iface"])

    def _focus_interval(
        self, conn, eyepiece, adapters: Sequence[Mapping[str, Any]]
    ) -> Tuple[int, int, int, int]:
        shoulder_lo, shoulder_hi = self._piece_path_interval(conn, eyepiece)
        adapter_sum = sum(a["nominal_path"] for a in adapters)
        total_lo = shoulder_lo + adapter_sum
        total_hi = shoulder_hi + adapter_sum
        focal = eyepiece["focal_position"]
        net_lo = focal - total_hi
        net_hi = focal - total_lo
        return total_lo, total_hi, net_lo, net_hi

    def _live_group_focus(self, conn, group, telescope_id: Optional[str] = None) -> Dict[str, Any]:
        """Recompute a group's compatibility and focus from *current* evidence.

        Calibration can be re-opened, so a stored verdict is never trusted at
        booking or delivery time; the net interval is recomputed from the live
        batch bias and the current telescope travel.  ``telescope_id`` overrides
        the group's original mount for temporary re-mounts.
        """

        test_id = telescope_id or group["telescope_id"]
        telescope = conn.execute("SELECT * FROM telescopes WHERE telescope_id = ?",
                                 (test_id,)).fetchone()
        if telescope is None:
            _err(UNKNOWN_TELESCOPE, "telescope is missing", telescope_id=test_id)
        adapter_ids = json_loads(group["adapter_json"])
        pieces = self._load_pieces(conn, [group["eyepiece_id"], *adapter_ids])
        eyepiece = pieces[group["eyepiece_id"]]
        adapters = [pieces[a] for a in adapter_ids]
        if eyepiece["kind"] != "eyepiece" or any(a["kind"] != "adapter" for a in adapters):
            _err(INCOMPATIBLE_CHAIN, "group contents are no longer a valid eyepiece/adapter chain",
                 group_id=group["group_id"])
        self._check_chain(conn, telescope, eyepiece, adapters)
        total_lo, total_hi, net_lo, net_hi = self._focus_interval(conn, eyepiece, adapters)
        if not (telescope["travel_lo"] <= net_lo and net_hi <= telescope["travel_hi"]):
            _err(FOCUS_UNREACHABLE, "under current calibration the group cannot be guaranteed to focus",
                 group_id=group["group_id"], net_lo=net_lo, net_hi=net_hi,
                 travel_lo=telescope["travel_lo"], travel_hi=telescope["travel_hi"])
        return {
            "telescope_id": telescope["telescope_id"],
            "total_lo": total_lo,
            "total_hi": total_hi,
            "net_lo": net_lo,
            "net_hi": net_hi,
            "travel_lo": telescope["travel_lo"],
            "travel_hi": telescope["travel_hi"],
            "piece_ids": [group["eyepiece_id"], *adapter_ids],
        }

    def _load_pieces(self, conn, piece_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        ids = list(dict.fromkeys(piece_ids))
        rows = conn.execute(
            f"SELECT * FROM optic_pieces WHERE piece_id IN ({','.join('?' for _ in ids)})", ids
        ).fetchall() if ids else []
        found = {row["piece_id"]: self._piece_dict(row) for row in rows}
        missing = [pid for pid in ids if pid not in found]
        if missing:
            _err(UNKNOWN_PIECE, "unknown piece", piece_ids=missing)
        return found

    def _group_result(self, conn, group_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM optic_groups WHERE group_id = ?", (group_id,)).fetchone()
        adapters = json_loads(row["adapter_json"])
        pieces = [row["eyepiece_id"], *adapters]
        return {
            "group_id": row["group_id"],
            "telescope_id": row["telescope_id"],
            "eyepiece_id": row["eyepiece_id"],
            "adapter_ids": adapters,
            "piece_ids": pieces,
            "total_lo": row["total_lo"],
            "total_hi": row["total_hi"],
            "net_lo": row["net_lo"],
            "net_hi": row["net_hi"],
            "travel_lo": row["travel_lo"],
            "travel_hi": row["travel_hi"],
            "verified": bool(row["verified"]),
            "sealed": bool(row["sealed"]),
            "verify": json_loads(row["verify_json"]),
        }

    def get_group(self, group_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            if conn.execute("SELECT 1 FROM optic_groups WHERE group_id = ?", (group_id,)).fetchone() is None:
                _err(UNKNOWN_GROUP, "unknown group", group_id=group_id)
            return self._group_result(conn, group_id)

    # --------------------------------------------------------------- sessions
    def add_session(self, session_id: str, seq: int, starts_at: int, ends_at: int) -> Dict[str, Any]:
        session_id = require_id(session_id, INVALID_OPTIC, "session_id")
        seq = _int(seq, "seq", minimum=1)
        starts_at = _int(starts_at, "starts_at")
        ends_at = _int(ends_at, "ends_at")
        if starts_at > ends_at:
            _err(INVALID_OPTIC, "starts_at must not exceed ends_at")
        content = {"session_id": session_id, "seq": seq, "starts_at": starts_at, "ends_at": ends_at}
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM optic_sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is not None:
                stored = {"session_id": row["session_id"], "seq": row["seq"],
                          "starts_at": row["starts_at"], "ends_at": row["ends_at"]}
                if stored != content:
                    _err(INVALID_OPTIC, "session_id reused with different content", session_id=session_id)
                return stored
            count = conn.execute("SELECT COUNT(*) AS n FROM optic_sessions").fetchone()["n"]
            if count >= MAX_SESSIONS:
                _err(OPTIC_LIMIT, f"at most {MAX_SESSIONS} sessions", limit=MAX_SESSIONS)
            if conn.execute("SELECT 1 FROM optic_sessions WHERE seq = ?", (seq,)).fetchone() is not None:
                _err(INVALID_OPTIC, "session seq already used", seq=seq)
            conn.execute(
                "INSERT INTO optic_sessions (session_id, seq, starts_at, ends_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, seq, starts_at, ends_at, utc_now()),
            )
        return content

    def get_session(self, session_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM optic_sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            _err(UNKNOWN_SESSION, "unknown session", session_id=session_id)
        return {"session_id": row["session_id"], "seq": row["seq"],
                "starts_at": row["starts_at"], "ends_at": row["ends_at"]}

    # ----------------------------------------------------------------- orders
    def place_order(
        self,
        order_id: str,
        sale_id: str,
        group_id: str,
        session_id: str,
        slot_lo: int,
        slot_hi: int,
    ) -> Dict[str, Any]:
        order_id = require_id(order_id, INVALID_OPTIC, "order_id")
        sale_id = require_id(sale_id, INVALID_OPTIC, "sale_id")
        group_id = require_id(group_id, INVALID_OPTIC, "group_id")
        session_id = require_id(session_id, INVALID_OPTIC, "session_id")
        slot_lo = _int(slot_lo, "slot_lo")
        slot_hi = _int(slot_hi, "slot_hi")
        if slot_lo > slot_hi:
            _err(INVALID_OPTIC, "slot_lo must not exceed slot_hi")

        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM optic_orders WHERE order_id = ?", (order_id,)).fetchone()
            if existing is not None:
                _err(ORDER_CONFLICT, "order_id already exists", order_id=order_id)
            sale = conn.execute("SELECT 1 FROM sales WHERE sale_id = ?", (sale_id,)).fetchone()
            if sale is None:
                _err(UNIT_INELIGIBLE, "order references an unknown sale", sale_id=sale_id)
            group = conn.execute("SELECT * FROM optic_groups WHERE group_id = ?", (group_id,)).fetchone()
            if group is None:
                _err(UNKNOWN_GROUP, "unknown group", group_id=group_id)
            if not group["verified"] or not group["sealed"]:
                _err(GROUP_NOT_VERIFIED, "group must be verified and sealed before handover", group_id=group_id)
            session = conn.execute("SELECT * FROM optic_sessions WHERE session_id = ?", (session_id,)).fetchone()
            if session is None:
                _err(UNKNOWN_SESSION, "unknown session", session_id=session_id)
            if not (session["starts_at"] <= slot_lo and slot_hi <= session["ends_at"]):
                _err(INVALID_OPTIC, "focus slot must lie inside the session",
                     slot_lo=slot_lo, slot_hi=slot_hi, session=(session["starts_at"], session["ends_at"]))

            # Re-verify against live calibration: a re-opened batch bias must be
            # able to invalidate a previously fine group before it is promised.
            live = self._live_group_focus(conn, group)
            group_pieces = live["piece_ids"]
            self._assert_pieces_free(conn, group_pieces, session["starts_at"], session["ends_at"],
                                     telescope_id=live["telescope_id"])

            content = {
                "order_id": order_id, "sale_id": sale_id, "telescope_id": group["telescope_id"],
                "group_id": group_id, "session_id": session_id, "slot_lo": slot_lo, "slot_hi": slot_hi,
            }
            conn.execute(
                "INSERT INTO optic_orders (order_id, sale_id, telescope_id, group_id, session_id, slot_lo, slot_hi, "
                "status, content_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)",
                (order_id, sale_id, group["telescope_id"], group_id, session_id, slot_lo, slot_hi,
                 canonical_json(content), utc_now()),
            )
            for role_index, piece_id in enumerate(group_pieces):
                role = "eyepiece" if role_index == 0 else "adapter"
                conn.execute("INSERT INTO optic_order_pieces (order_id, piece_id, role) VALUES (?, ?, ?)",
                             (order_id, piece_id, role))
                conn.execute(
                    "INSERT INTO optic_bookings (order_id, piece_id, telescope_id, session_id, starts_at, ends_at, "
                    "delivered, active, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?)",
                    (order_id, piece_id, group["telescope_id"], session_id,
                     session["starts_at"], session["ends_at"], utc_now()),
                )
            return self._order_result(conn, order_id)

    def _assert_pieces_free(
        self, conn, piece_ids: Sequence[str], starts_at: int, ends_at: int,
        *, telescope_id: str, exclude_order: Optional[str] = None,
    ) -> None:
        for piece_id in piece_ids:
            piece = conn.execute("SELECT * FROM optic_pieces WHERE piece_id = ?", (piece_id,)).fetchone()
            if piece is None:
                _err(UNKNOWN_PIECE, "unknown piece", piece_id=piece_id)
            if piece["status"] == "RETURNED":
                _err(PIECE_UNAVAILABLE, "piece has been returned to its lender", piece_id=piece_id)
            query = ("SELECT b.* FROM optic_bookings b WHERE b.piece_id = ? AND b.active = 1")
            params: List[Any] = [piece_id]
            if exclude_order is not None:
                query += " AND b.order_id != ?"
                params.append(exclude_order)
            for booking in conn.execute(query, params).fetchall():
                # Once checked in, a piece is physically held until it is
                # returned -- it may not be promised to another instrument even
                # if the next session's window no longer overlaps.  A merely
                # *reserved* piece only conflicts when the session intervals
                # overlap (touching endpoints do not conflict).
                if booking["delivered"]:
                    _err(PIECE_BOOKED, "piece is checked in and not yet returned",
                         piece_id=piece_id, order_id=booking["order_id"])
                if _overlaps(starts_at, ends_at, booking["starts_at"], booking["ends_at"]):
                    _err(PIECE_BOOKED, "piece is already booked in an overlapping session",
                         piece_id=piece_id, order_id=booking["order_id"],
                         booked=(booking["starts_at"], booking["ends_at"]),
                         requested=(starts_at, ends_at))
        # A telescope is unavailable while an overlapping session holds it, or
        # while a checked-in order is still physically mounted on it.
        query = ("SELECT DISTINCT order_id, starts_at, ends_at, delivered FROM optic_bookings "
                 "WHERE telescope_id = ? AND active = 1")
        params = [telescope_id]
        if exclude_order is not None:
            query += " AND order_id != ?"
            params.append(exclude_order)
        for booking in conn.execute(query, params).fetchall():
            if booking["delivered"] or _overlaps(starts_at, ends_at, booking["starts_at"], booking["ends_at"]):
                _err(SCOPE_BOOKED, "telescope is already assigned to an overlapping or active session",
                     telescope_id=telescope_id, order_id=booking["order_id"])

    def deliver_order(self, order_id: str) -> Dict[str, Any]:
        order_id = require_id(order_id, INVALID_OPTIC, "order_id")
        with self.db.transaction() as conn:
            order = conn.execute("SELECT * FROM optic_orders WHERE order_id = ?", (order_id,)).fetchone()
            if order is None:
                _err(UNKNOWN_ORDER, "unknown order", order_id=order_id)
            if order["status"] == "CANCELLED":
                _err(ORDER_STATE, "cancelled order cannot be delivered", order_id=order_id)
            if order["status"] == "DELIVERED":
                return self._delivery_result(conn, order_id)
            group = conn.execute("SELECT * FROM optic_groups WHERE group_id = ?", (order["group_id"],)).fetchone()
            if group is None:
                _err(UNKNOWN_GROUP, "order group is missing", group_id=order["group_id"])
            if not group["verified"] or not group["sealed"]:
                _err(GROUP_NOT_VERIFIED, "group is no longer verified/sealed", group_id=order["group_id"])
            live = self._live_group_focus(conn, group, telescope_id=order["telescope_id"])
            group_pieces = live["piece_ids"]
            conn.execute("UPDATE optic_orders SET status = 'DELIVERED' WHERE order_id = ?", (order_id,))
            conn.execute("UPDATE optic_bookings SET delivered = 1 WHERE order_id = ?", (order_id,))
            receipt = {
                "delivery_id": f"delivery-{order_id}",
                "order_id": order_id,
                "sale_id": order["sale_id"],
                "telescope_id": order["telescope_id"],
                "group_id": order["group_id"],
                "session_id": order["session_id"],
                "slot_lo": order["slot_lo"],
                "slot_hi": order["slot_hi"],
                "focus_lo": live["net_lo"],
                "focus_hi": live["net_hi"],
                "travel_lo": live["travel_lo"],
                "travel_hi": live["travel_hi"],
                "pieces": group_pieces,
            }
            conn.execute(
                "INSERT INTO optic_deliveries (delivery_id, order_id, telescope_id, group_id, sale_id, session_id, "
                "focus_lo, focus_hi, receipt_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (receipt["delivery_id"], order_id, order["telescope_id"], order["group_id"], order["sale_id"],
                 order["session_id"], live["net_lo"], live["net_hi"], canonical_json(receipt), utc_now()),
            )
            return receipt

    def fulfil_session(self, session_id: str) -> List[Dict[str, Any]]:
        """Check in every open order of a session, in focus-slot order."""

        session_id = require_id(session_id, INVALID_OPTIC, "session_id")
        with self.db.connection() as conn:
            if conn.execute("SELECT 1 FROM optic_sessions WHERE session_id = ?", (session_id,)).fetchone() is None:
                _err(UNKNOWN_SESSION, "unknown session", session_id=session_id)
            order_ids = [row["order_id"] for row in conn.execute(
                "SELECT order_id FROM optic_orders WHERE session_id = ? AND status = 'OPEN' ORDER BY slot_lo, order_id",
                (session_id,)).fetchall()]
        return [self.deliver_order(oid) for oid in order_ids]

    def cancel_order(self, order_id: str, reason: str = "") -> Dict[str, Any]:
        order_id = require_id(order_id, INVALID_OPTIC, "order_id")
        with self.db.transaction() as conn:
            order = conn.execute("SELECT * FROM optic_orders WHERE order_id = ?", (order_id,)).fetchone()
            if order is None:
                _err(UNKNOWN_ORDER, "unknown order", order_id=order_id)
            if order["status"] == "DELIVERED":
                _err(ORDER_STATE, "a delivered order cannot be cancelled; process a return instead",
                     order_id=order_id)
            if order["status"] == "CANCELLED":
                return self._order_result(conn, order_id)
            conn.execute("UPDATE optic_orders SET status = 'CANCELLED' WHERE order_id = ?", (order_id,))
            conn.execute("UPDATE optic_bookings SET active = 0 WHERE order_id = ?", (order_id,))
            result = self._order_result(conn, order_id)
        result["cancel_reason"] = reason
        return result

    def switch_telescope(self, order_id: str, new_telescope_id: str) -> Dict[str, Any]:
        """Temporary re-mount: continue on another telescope if it still focuses."""

        order_id = require_id(order_id, INVALID_OPTIC, "order_id")
        new_telescope_id = require_id(new_telescope_id, INVALID_OPTIC, "new_telescope_id")
        with self.db.transaction() as conn:
            order = conn.execute("SELECT * FROM optic_orders WHERE order_id = ?", (order_id,)).fetchone()
            if order is None:
                _err(UNKNOWN_ORDER, "unknown order", order_id=order_id)
            if order["status"] != "OPEN":
                _err(ORDER_STATE, "only an open order can switch telescopes", order_id=order_id,
                     status=order["status"])
            telescope = conn.execute("SELECT * FROM telescopes WHERE telescope_id = ?",
                                     (new_telescope_id,)).fetchone()
            if telescope is None:
                _err(UNKNOWN_TELESCOPE, "unknown telescope", telescope_id=new_telescope_id)
            session = conn.execute("SELECT * FROM optic_sessions WHERE session_id = ?",
                                   (order["session_id"],)).fetchone()
            group = conn.execute("SELECT * FROM optic_groups WHERE group_id = ?", (order["group_id"],)).fetchone()
            if group is None:
                _err(UNKNOWN_GROUP, "order group is missing", group_id=order["group_id"])
            adapter_ids = json_loads(group["adapter_json"])
            pieces = self._load_pieces(conn, [group["eyepiece_id"], *adapter_ids])
            adapters = [pieces[a] for a in adapter_ids]
            self._check_chain(conn, telescope, pieces[group["eyepiece_id"]], adapters)
            total_lo, total_hi, net_lo, net_hi = self._focus_interval(
                conn, pieces[group["eyepiece_id"]], adapters)
            if not (telescope["travel_lo"] <= net_lo and net_hi <= telescope["travel_hi"]):
                _err(FOCUS_UNREACHABLE, "net focus interval no longer fits the new telescope's travel",
                     net_lo=net_lo, net_hi=net_hi, travel_lo=telescope["travel_lo"],
                     travel_hi=telescope["travel_hi"])
            self._assert_pieces_free(conn, list(pieces), session["starts_at"], session["ends_at"],
                                     telescope_id=new_telescope_id, exclude_order=order_id)
            conn.execute("UPDATE optic_orders SET telescope_id = ? WHERE order_id = ?",
                         (new_telescope_id, order_id))
            conn.execute("UPDATE optic_bookings SET telescope_id = ? WHERE order_id = ?",
                         (new_telescope_id, order_id))
            result = self._order_result(conn, order_id)
        result["switched_from"] = order["telescope_id"]
        result["continued"] = True
        return result

    def get_order(self, order_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            if conn.execute("SELECT 1 FROM optic_orders WHERE order_id = ?", (order_id,)).fetchone() is None:
                _err(UNKNOWN_ORDER, "unknown order", order_id=order_id)
            return self._order_result(conn, order_id)

    def _order_result(self, conn, order_id: str) -> Dict[str, Any]:
        order = conn.execute("SELECT * FROM optic_orders WHERE order_id = ?", (order_id,)).fetchone()
        pieces = [dict(row) for row in conn.execute(
            "SELECT piece_id, role FROM optic_order_pieces WHERE order_id = ? ORDER BY role DESC, piece_id",
            (order_id,)).fetchall()]
        delivery = conn.execute("SELECT receipt_json FROM optic_deliveries WHERE order_id = ?",
                                (order_id,)).fetchone()
        return {
            "order_id": order["order_id"],
            "sale_id": order["sale_id"],
            "telescope_id": order["telescope_id"],
            "group_id": order["group_id"],
            "session_id": order["session_id"],
            "slot_lo": order["slot_lo"],
            "slot_hi": order["slot_hi"],
            "status": order["status"],
            "pieces": pieces,
            "delivery": json_loads(delivery["receipt_json"]) if delivery is not None else None,
        }

    def get_delivery(self, order_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT receipt_json FROM optic_deliveries WHERE order_id = ?",
                               (order_id,)).fetchone()
        if row is None:
            _err(UNKNOWN_ORDER, "no delivery for order", order_id=order_id)
        return json_loads(row["receipt_json"])

    def _delivery_result(self, conn, order_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT receipt_json FROM optic_deliveries WHERE order_id = ?",
                           (order_id,)).fetchone()
        if row is not None:
            return json_loads(row["receipt_json"])
        # Order is marked DELIVERED but a receipt is somehow missing.
        _err(UNKNOWN_ORDER, "delivered order has no receipt", order_id=order_id)

    # -------------------------------------------------------------- returns
    def return_pieces(self, order_id: str) -> List[Dict[str, Any]]:
        """Check a delivered group back in; pieces become lendable again.

        Ending the booking ends its exclusivity, so the group may be lent to a
        later, non-overlapping session (possibly another telescope).  A piece
        whose underlying sale unit was returned is marked RETURNED and stays
        unavailable.
        """

        order_id = require_id(order_id, INVALID_OPTIC, "order_id")
        with self.db.transaction() as conn:
            order = conn.execute("SELECT * FROM optic_orders WHERE order_id = ?", (order_id,)).fetchone()
            if order is None:
                _err(UNKNOWN_ORDER, "unknown order", order_id=order_id)
            if order["status"] != "DELIVERED":
                _err(ORDER_STATE, "only a delivered order can be returned", order_id=order_id,
                     status=order["status"])
            conn.execute("UPDATE optic_bookings SET active = 0 WHERE order_id = ?", (order_id,))
            group = conn.execute("SELECT * FROM optic_groups WHERE group_id = ?", (order["group_id"],)).fetchone()
            piece_ids = [group["eyepiece_id"], *json_loads(group["adapter_json"])]
            results: List[Dict[str, Any]] = []
            for piece_id in piece_ids:
                piece = conn.execute("SELECT * FROM optic_pieces WHERE piece_id = ?", (piece_id,)).fetchone()
                unit = conn.execute(
                    "SELECT returned FROM sale_units WHERE sale_id = ? AND unit_id = ?",
                    (piece["sale_id"], piece["unit_id"]),
                ).fetchone()
                new_status = "RETURNED" if (unit is not None and unit["returned"]) else "IN_STOCK"
                conn.execute("UPDATE optic_pieces SET status = ? WHERE piece_id = ?", (new_status, piece_id))
                results.append({"piece_id": piece_id, "status": new_status})
        return results

    def return_piece(self, piece_id: str) -> Dict[str, Any]:
        """Return a single piece to its lender (outside any active booking)."""

        piece_id = require_id(piece_id, INVALID_OPTIC, "piece_id")
        with self.db.transaction() as conn:
            piece = conn.execute("SELECT * FROM optic_pieces WHERE piece_id = ?", (piece_id,)).fetchone()
            if piece is None:
                _err(UNKNOWN_PIECE, "unknown piece", piece_id=piece_id)
            active = conn.execute(
                "SELECT 1 FROM optic_bookings WHERE piece_id = ? AND active = 1 LIMIT 1", (piece_id,)
            ).fetchone()
            if active is not None:
                _err(PIECE_BOOKED, "cannot return a piece with an active booking", piece_id=piece_id)
            if piece["group_id"] is not None:
                _err(GROUP_SEALED, "unseal the group before returning a single piece",
                     piece_id=piece_id, group_id=piece["group_id"])
            conn.execute("UPDATE optic_pieces SET status = 'RETURNED' WHERE piece_id = ?", (piece_id,))
            return {"piece_id": piece_id, "status": "RETURNED"}

    def list_orders(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            ids = [row["order_id"] for row in conn.execute(
                "SELECT order_id FROM optic_orders ORDER BY created_at, order_id").fetchall()]
            return [self._order_result(conn, oid) for oid in ids]

    def list_bookings(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT order_id, piece_id, telescope_id, session_id, starts_at, ends_at, delivered, active "
                "FROM optic_bookings ORDER BY piece_id, starts_at").fetchall()]


__all__ = ["OpticsService", "MAX_TELESCOPES", "MAX_PIECES", "MAX_SESSIONS"]
