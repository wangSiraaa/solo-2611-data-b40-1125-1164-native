"""Nib-grinding workshop layered on top of sales, the ledger and the planner.

The physical model
------------------
A nib is described by a *candidate hypothesis* ``(kind, base, side_wide)`` plus
the hand that holds it:

* a **straight** nib writes its own base width in both directions: ``(b, b)``;
* a **side** nib writes one step wide "顺向" (``b + 1``) and one step narrow
  "逆向" (``b - 1``); which physical direction is wide is fixed on the piece
  but *swaps when the hand swaps* ("换手对调两向");
* every width is an integer, and **grinding once subtracts one from the base
  width** for the whole piece.  Metal removed cannot be put back, so the grind
  count only ever rises and is shared by every order on that piece.

A sale unit is registered with the finite catalogue of hypotheses its model
admits (e.g. the known right-hand base-2 nib admits ``straight 2`` and the two
``side 2`` orientations; the ambiguous piece ``N`` admits ``straight 2`` and
``side 3`` wide-in-V).  Directional try-writes ("试写台") are recorded against
a specific piece and, at their grind level, retain exactly the hypotheses that
can explain them.  Because trials only ever *remove* hypotheses, a guarantee
computed on the surviving set stays valid when more evidence arrives.

Delivery guarantee
------------------
The counter signs an order only when the customer's two-direction interval,
its deadline and the shop's one-grind-slot-per-shift schedule can together be
met **for every surviving hypothesis** — it must not have to know the nib type
to promise the result.  Two operating strategies are supported:

* ``PREGRIND``  – grind now, within the range that can be guaranteed without
  disambiguating the type (good for urgent, same-shift orders);
* ``REMEASURE`` – keep the original width, spend one whole shift letting the
  owner try-write both directions, then decide (good for "return it as it is"
  orders, and it never removes metal until the type is known).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .db import Database, canonical_json, json_loads, utc_now
from .errors import (
    INVALID_INPUT,
    NIB_EVIDENCE_MISMATCH,
    NIB_ORDER_CONFLICT,
    NIB_ORDER_STATE,
    NIB_ORDER_UNKNOWN,
    NIB_REQUIREMENT_VIOLATED,
    NIB_SLOT_CONFLICT,
    NIB_UNIT_CONFLICT,
    NIB_UNIT_DELIVERED,
    NIB_UNIT_NOT_IN_SHOP,
    NIB_UNIT_RETURNED,
    NIB_UNKNOWN,
    BasketError,
)
from .validation import is_int, require_id

H = "H"
V = "V"
HANDS = ("RIGHT", "LEFT")
STRAIGHT = "STRAIGHT"
SIDE = "SIDE"
STRATEGIES = ("PREGRIND", "REMEASURE")

# Order lifecycle -----------------------------------------------------------
OPEN = "OPEN"
AWAITING_REMEASURE = "AWAITING_REMEASURE"
SIGNED = "SIGNED"
HUNG = "HUNG"
REFUSED = "REFUSED"
DELIVERED = "DELIVERED"


def _flip(direction: str) -> str:
    return V if direction == H else H


# ---------------------------------------------------------------------------
# Pure geometry helpers (exported so tests and demos never use service code to
# compute the expected answer).
# ---------------------------------------------------------------------------
def normalise_candidate(raw: Mapping[str, Any], index: int = 0) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise BasketError(INVALID_INPUT, "candidate must be an object", {"index": index})
    kind = raw.get("kind")
    if kind not in (STRAIGHT, SIDE):
        raise BasketError(INVALID_INPUT, "candidate.kind must be STRAIGHT or SIDE", {"index": index})
    base = raw.get("base")
    minimum = 1 if kind == STRAIGHT else 2
    if not is_int(base) or base < minimum:
        raise BasketError(INVALID_INPUT, f"candidate.base must be an integer >= {minimum}", {"index": index})
    if kind == STRAIGHT:
        return {"kind": STRAIGHT, "base": base, "side_wide": None}
    side_wide = raw.get("side_wide")
    if side_wide not in (H, V):
        raise BasketError(INVALID_INPUT, "side candidate needs side_wide 'H' or 'V'", {"index": index})
    return {"kind": SIDE, "base": base, "side_wide": side_wide}


def shape_of(candidate: Mapping[str, Any], hand: str, grinds: int) -> Optional[Dict[str, int]]:
    """Return ``{"H": w, "V": w}`` or ``None`` when that many grinds destroy it.

    A side nib needs a base of at least 2 (so its narrow direction stays >= 1);
    once grinding would push the base below that, the piece can no longer take
    that shape.  Metal removed is never recovered.
    """

    base = candidate["base"] - grinds
    if candidate["kind"] == STRAIGHT:
        if base < 1:
            return None
        return {H: base, V: base}
    if base < 2:
        return None
    wide = candidate["side_wide"]
    if hand == "LEFT":
        wide = _flip(wide)
    return {wide: base + 1, _flip(wide): base - 1}


def reachable_shapes(candidate: Mapping[str, Any], hand: str) -> List[Dict[str, Any]]:
    """Every shape the piece can reach, one entry per admissible grind level."""

    out: List[Dict[str, Any]] = []
    for grinds in range(0, candidate["base"]):
        shape = shape_of(candidate, hand, grinds)
        if shape is not None:
            out.append({"grinds": grinds, "H": shape[H], "V": shape[V]})
    return out


def satisfies(shape: Optional[Mapping[str, int]], req: Mapping[str, Tuple[int, int]]) -> bool:
    if shape is None:
        return False
    return all(req[d][0] <= shape[d] <= req[d][1] for d in (H, V))


def min_grinds_for_candidate(
    candidate: Mapping[str, Any],
    hand: str,
    grinds_done: int,
    req: Mapping[str, Tuple[int, int]],
    max_extra: int,
) -> Optional[int]:
    """Smallest extra grind count (>=0) making *this* hypothesis meet ``req``."""

    for extra in range(0, max_extra + 1):
        if satisfies(shape_of(candidate, hand, grinds_done + extra), req):
            return extra
    return None


def guarantee_grinds(
    candidates: Sequence[Mapping[str, Any]],
    hand: str,
    grinds_done: int,
    req: Mapping[str, Tuple[int, int]],
    max_extra: int,
) -> Optional[int]:
    """Smallest extra grind count making *every* surviving hypothesis meet req."""

    if not candidates:
        return None
    for extra in range(0, max_extra + 1):
        if all(satisfies(shape_of(c, hand, grinds_done + extra), req) for c in candidates):
            return extra
    return None


# ---------------------------------------------------------------------------
# Requirement / candidate parsing
# ---------------------------------------------------------------------------
def _require_interval(value: Any, name: str) -> List[int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise BasketError(INVALID_INPUT, f"{name} must be a [min, max] pair", {"field": name})
    lo, hi = value
    if not is_int(lo) or not is_int(hi) or lo < 1 or hi < lo:
        raise BasketError(INVALID_INPUT, f"{name} must be positive integers with min <= max", {"field": name})
    return [int(lo), int(hi)]


def parse_requirement(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise BasketError(INVALID_INPUT, "requirement must be an object with hand, h, v")
    hand = raw.get("hand", "RIGHT")
    if hand not in HANDS:
        raise BasketError(INVALID_INPUT, "requirement.hand must be RIGHT or LEFT", {"hand": hand})
    return {"hand": hand, "h": _require_interval(raw.get("h"), "requirement.h"), "v": _require_interval(raw.get("v"), "requirement.v")}


def _req_map(requirement: Mapping[str, Any]) -> Dict[str, Tuple[int, int]]:
    return {H: tuple(requirement["h"]), V: tuple(requirement["v"])}


class NibService:
    def __init__(self, db: Database, ledger):
        self.db = db
        self.ledger = ledger

    # ------------------------------------------------------------- registry
    def _sale_unit(self, conn, sale_id: str, unit_id: str) -> Mapping[str, Any]:
        row = conn.execute(
            "SELECT unit_id, sku, returned FROM sale_units WHERE sale_id = ? AND unit_id = ?",
            (sale_id, unit_id),
        ).fetchone()
        if row is None:
            # A sale that exists but lacks the unit, or no sale at all.
            sale = conn.execute("SELECT sale_id FROM sales WHERE sale_id = ?", (sale_id,)).fetchone()
            if sale is None:
                raise BasketError("UNKNOWN_SALE", "unknown sale", {"sale_id": sale_id})
            raise BasketError(NIB_UNKNOWN, "unit is not part of the sale", {"sale_id": sale_id, "unit_id": unit_id})
        if row["returned"]:
            raise BasketError(NIB_UNIT_RETURNED, "unit was refunded and cannot be used for a nib order", {"sale_id": sale_id, "unit_id": unit_id})
        return row

    def register_nib(
        self,
        sale_id: str,
        unit_id: str,
        *,
        owner_id: str,
        candidates: Sequence[Mapping[str, Any]],
        hand: str = "RIGHT",
        model: Optional[str] = None,
        allowed_h: Sequence[int] = (1, 10),
        allowed_v: Sequence[int] = (1, 10),
        return_deadline: int = 999,
        initial_trials: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        sale_id = require_id(sale_id, INVALID_INPUT, "sale_id")
        unit_id = require_id(unit_id, INVALID_INPUT, "unit_id")
        owner_id = require_id(owner_id, INVALID_INPUT, "owner_id")
        if hand not in HANDS:
            raise BasketError(INVALID_INPUT, "hand must be RIGHT or LEFT", {"hand": hand})
        norm = [normalise_candidate(c, i) for i, c in enumerate(candidates or [])]
        if not norm:
            raise BasketError(INVALID_INPUT, "a nib needs at least one candidate hypothesis")
        seen = set()
        deduped = []
        for cand in norm:
            key = canonical_json(cand)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(cand)
        allowed_h_i = _require_interval(allowed_h, "allowed_h")
        allowed_v_i = _require_interval(allowed_v, "allowed_v")
        if not is_int(return_deadline) or return_deadline < 1:
            raise BasketError(INVALID_INPUT, "return_deadline must be a positive integer shift")
        content = {
            "model": model,
            "owner_id": owner_id,
            "hand": hand,
            "candidates": deduped,
            "allowed_h": list(allowed_h_i),
            "allowed_v": list(allowed_v_i),
            "return_deadline": return_deadline,
        }
        with self.db.transaction() as conn:
            self._sale_unit(conn, sale_id, unit_id)
            existing = conn.execute(
                "SELECT content_json FROM nib_units WHERE sale_id = ? AND unit_id = ?", (sale_id, unit_id)
            ).fetchone()
            if existing is not None:
                if existing["content_json"] != canonical_json(content):
                    raise BasketError(NIB_UNIT_CONFLICT, "nib already registered with different facts", {"sale_id": sale_id, "unit_id": unit_id})
                return self._nib_result(conn, sale_id, unit_id, repeated=True)
            conn.execute(
                "INSERT INTO nib_units (sale_id, unit_id, model, owner_id, known_base, hand, holder, returned, committed_grinds, delivered_order_id, content_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'SHOP', 0, 0, NULL, ?, ?)",
                (sale_id, unit_id, model, owner_id, min(c["base"] for c in deduped), hand, canonical_json(content), utc_now()),
            )
            if initial_trials:
                for trial in initial_trials:
                    self._insert_trial(conn, sale_id, unit_id, trial, grind_level=0, shift_no=0, source="SALE")
            return self._nib_result(conn, sale_id, unit_id, repeated=False)

    def record_trial(
        self,
        sale_id: str,
        unit_id: str,
        *,
        hand: str,
        direction: str,
        width: Any,
        shift_no: int = 0,
        source: str = "MANUAL",
    ) -> Dict[str, Any]:
        if hand not in HANDS:
            raise BasketError(INVALID_INPUT, "trial.hand must be RIGHT or LEFT", {"hand": hand})
        if direction not in (H, V):
            raise BasketError(INVALID_INPUT, "trial.direction must be H or V", {"direction": direction})
        if not is_int(width) or width < 1:
            raise BasketError(INVALID_INPUT, "trial.width must be a positive integer", {"width": width})
        if not is_int(shift_no) or shift_no < 0:
            raise BasketError(INVALID_INPUT, "trial.shift_no must be a non-negative integer")
        with self.db.transaction() as conn:
            self._sale_unit(conn, sale_id, unit_id)
            nib = self._load_nib(conn, sale_id, unit_id)
            if nib["holder"] != "SHOP":
                raise BasketError(NIB_UNIT_NOT_IN_SHOP, "unit has left the shop; cannot add a try-write", {"holder": nib["holder"]})
            grinds = nib["committed_grinds"]
            self._insert_trial(conn, sale_id, unit_id, {"hand": hand, "direction": direction, "width": width}, grinds, shift_no, source)
            surviving = self._surviving(conn, sale_id, unit_id)
            if not surviving:
                # Roll the transaction back and report the contradiction.
                raise BasketError(
                    NIB_EVIDENCE_MISMATCH,
                    "trial cannot be explained by any candidate nib type",
                    {"sale_id": sale_id, "unit_id": unit_id, "hand": hand, "direction": direction, "width": width},
                )
            return self._nib_result(conn, sale_id, unit_id, repeated=False)

    def get_nib(self, sale_id: str, unit_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            self._load_nib(conn, sale_id, unit_id)
            return self._nib_result(conn, sale_id, unit_id, repeated=False)

    def _load_nib(self, conn, sale_id: str, unit_id: str) -> Mapping[str, Any]:
        row = conn.execute(
            "SELECT * FROM nib_units WHERE sale_id = ? AND unit_id = ?", (sale_id, unit_id)
        ).fetchone()
        if row is None:
            raise BasketError(NIB_UNKNOWN, "nib is not registered for this sale unit", {"sale_id": sale_id, "unit_id": unit_id})
        # A refund after registration must also block the piece: re-read the live
        # sale unit rather than trusting the registration-time snapshot.
        live = conn.execute(
            "SELECT returned FROM sale_units WHERE sale_id = ? AND unit_id = ?", (sale_id, unit_id)
        ).fetchone()
        if live is not None and live["returned"]:
            return {**dict(row), "returned": 1}
        return row

    def _insert_trial(self, conn, sale_id: str, unit_id: str, trial: Mapping[str, Any], grind_level: int, shift_no: int, source: str) -> None:
        hand = trial.get("hand", "RIGHT")
        direction = trial.get("direction")
        width = trial.get("width")
        if hand not in HANDS or direction not in (H, V) or not is_int(width):
            raise BasketError(INVALID_INPUT, "invalid trial", {"trial": dict(trial)})
        conn.execute(
            "INSERT INTO nib_trials (sale_id, unit_id, hand, direction, width, grind_level, shift_no, source, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sale_id, unit_id, hand, direction, width, grind_level, shift_no, source, utc_now()),
        )

    def _trials(self, conn, sale_id: str, unit_id: str) -> List[Mapping[str, Any]]:
        return conn.execute(
            "SELECT hand, direction, width, grind_level, source FROM nib_trials WHERE sale_id = ? AND unit_id = ? ORDER BY seq",
            (sale_id, unit_id),
        ).fetchall()

    def _surviving(self, conn, sale_id: str, unit_id: str) -> List[Dict[str, Any]]:
        nib = self._load_nib(conn, sale_id, unit_id)
        content = json_loads(nib["content_json"])
        trials = self._trials(conn, sale_id, unit_id)
        current = nib["committed_grinds"]
        out: List[Dict[str, Any]] = []
        for cand in content["candidates"]:
            # A hypothesis already ground away no longer physically exists.
            if shape_of(cand, content["hand"], current) is None:
                continue
            ok = True
            for trial in trials:
                shape = shape_of(cand, trial["hand"], trial["grind_level"])
                if shape is None or shape[trial["direction"]] != trial["width"]:
                    ok = False
                    break
            if ok:
                out.append(cand)
        return out

    def _nib_result(self, conn, sale_id: str, unit_id: str, *, repeated: bool) -> Dict[str, Any]:
        nib = self._load_nib(conn, sale_id, unit_id)
        content = json_loads(nib["content_json"])
        surviving = self._surviving(conn, sale_id, unit_id)
        grinds = nib["committed_grinds"]
        hand = content["hand"]
        original_available = grinds == 0
        delivery = conn.execute("SELECT * FROM nib_deliveries WHERE order_id = ?", (nib["delivered_order_id"],)).fetchone() if nib["delivered_order_id"] else None
        return {
            "ok": True,
            "sale_id": sale_id,
            "unit_id": unit_id,
            "model": content["model"],
            "owner_id": content["owner_id"],
            "hand": hand,
            "holder": nib["holder"],
            "delivered_order_id": nib["delivered_order_id"],
            "grinds_done": grinds,
            "candidates": content["candidates"],
            "surviving": surviving,
            "reachable": {canonical_json(c): reachable_shapes(c, hand) for c in surviving},
            "current_shapes": [shape_of(c, hand, grinds) for c in surviving],
            "original_width_available": original_available,
            "original_shapes": [shape_of(c, hand, 0) for c in content["candidates"]],
            "allowed_h": content["allowed_h"],
            "allowed_v": content["allowed_v"],
            "return_deadline": content["return_deadline"],
            "delivered": delivery is not None,
            "repeated": repeated,
        }

    # ----------------------------------------------------------- query owner
    def query_nib(self, sale_id: str, unit_id: str) -> Dict[str, Any]:
        """Who physically holds the piece right now, and is the original width intact?"""

        with self.db.connection() as conn:
            nib = self._load_nib(conn, sale_id, unit_id)
            result = self._nib_result(conn, sale_id, unit_id, repeated=False)
            delivery = (
                conn.execute("SELECT * FROM nib_deliveries WHERE order_id = ?", (nib["delivered_order_id"],)).fetchone()
                if nib["delivered_order_id"]
                else None
            )
        result["owner_note"] = (
            "held by the shop" if nib["holder"] == "SHOP" else f"delivered to {nib['holder']}"
        )
        # A repeated query on the same sale number returns the same original
        # delivery record (the actual-width evidence retained at hand-over).
        result["delivery_record"] = (
            None
            if delivery is None
            else {
                "order_id": delivery["order_id"],
                "shift_no": delivery["shift_no"],
                "measured_h": delivery["measured_h"],
                "measured_v": delivery["measured_v"],
                "evidence": json_loads(delivery["evidence_json"]),
            }
        )
        return result

    # ------------------------------------------------------------- orders
    def _free_slots(self, conn, lo: int, hi: int) -> List[int]:
        used = {
            row["shift_no"]
            for row in conn.execute("SELECT shift_no FROM nib_shifts WHERE shift_no BETWEEN ? AND ?", (lo, hi)).fetchall()
        }
        return [s for s in range(lo, hi + 1) if s not in used]

    def open_nib_order(
        self,
        order_id: str,
        sale_id: str,
        unit_id: str,
        *,
        strategy: str,
        requirement: Mapping[str, Any],
        deadline_shift: int,
        opened_shift: int,
        claimant_id: str,
    ) -> Dict[str, Any]:
        order_id = require_id(order_id, INVALID_INPUT, "order_id")
        sale_id = require_id(sale_id, INVALID_INPUT, "sale_id")
        unit_id = require_id(unit_id, INVALID_INPUT, "unit_id")
        claimant_id = require_id(claimant_id, INVALID_INPUT, "claimant_id")
        if strategy not in STRATEGIES:
            raise BasketError(INVALID_INPUT, "strategy must be PREGRIND or REMEASURE", {"strategy": strategy})
        if not is_int(opened_shift) or not is_int(deadline_shift) or opened_shift < 1 or deadline_shift < opened_shift:
            raise BasketError(INVALID_INPUT, "shifts must be positive with opened_shift <= deadline_shift")
        req = parse_requirement(requirement)
        req_map = _req_map(req)
        body = {
            "sale_id": sale_id,
            "unit_id": unit_id,
            "strategy": strategy,
            "requirement": req,
            "deadline_shift": deadline_shift,
            "opened_shift": opened_shift,
            "claimant_id": claimant_id,
        }
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM nib_orders WHERE order_id = ?", (order_id,)).fetchone()
            if existing is not None:
                if existing["decision_json"] and json_loads(existing["decision_json"]).get("body") != body:
                    raise BasketError(NIB_ORDER_CONFLICT, "order_id reused with a different body", {"order_id": order_id})
                return self._order_result(conn, order_id, repeated=True)
            nib = self._load_nib(conn, sale_id, unit_id)
            content = json_loads(nib["content_json"])
            surviving = self._surviving(conn, sale_id, unit_id)
            grinds = nib["committed_grinds"]
            hand = req["hand"]

            refusal = self._refusal_reason(conn, nib, content, surviving, req_map, opened_shift, deadline_shift, claimant_id)
            free = self._free_slots(conn, opened_shift, deadline_shift)
            if refusal is not None:
                status, decision = REFUSED, self._decision(REFUSED, reason=refusal, free_slots=free)
            elif strategy == "PREGRIND":
                status, decision = self._decide_pregrind(surviving, hand, grinds, req_map, free)
            else:
                status, decision = self._decide_remeasure(surviving, hand, grinds, req_map, free)
            # Reserve every shift the accepted decision intends to use so no
            # other order can be promised the same single grind slot.
            for shift in decision.get("reserved_shifts", []):
                self._reserve_shift(conn, int(shift), order_id, decision["reserved_kind"])
            self._persist_order(conn, order_id, body, status, decision)
            return self._order_result(conn, order_id, repeated=False)

    def _refusal_reason(self, conn, nib, content, surviving, req_map, opened_shift, deadline_shift, claimant_id) -> Optional[str]:
        if nib["returned"]:
            return "unit was refunded"
        if nib["holder"] != "SHOP":
            return f"unit already delivered to {nib['holder']}"
        if nib["delivered_order_id"]:
            return "unit already delivered under another order"
        # A piece committed to an active order must not be promised to a second
        # customer ("已交的同一支不能签给两人").
        active = conn.execute(
            "SELECT order_id, status, claimant_id FROM nib_orders WHERE sale_id = ? AND unit_id = ? AND status IN (?, ?) ORDER BY order_id",
            (nib["sale_id"], nib["unit_id"], SIGNED, AWAITING_REMEASURE),
        ).fetchall()
        if active:
            holder_order = active[0]
            return f"unit is committed to active order {holder_order['order_id']} ({holder_order['status']})"
        if not surviving:
            return "no nib type explains the try-writes"
        # Store promise: the customer's interval must sit inside the allowed band
        # and the deadline must be within the promised return deadline.
        (ah_lo, ah_hi) = content["allowed_h"]
        (av_lo, av_hi) = content["allowed_v"]
        if not (ah_lo <= req_map[H][0] and req_map[H][1] <= ah_hi):
            return f"horizontal requirement {list(req_map[H])} outside allowed {content['allowed_h']}"
        if not (av_lo <= req_map[V][0] and req_map[V][1] <= av_hi):
            return f"vertical requirement {list(req_map[V])} outside allowed {content['allowed_v']}"
        if deadline_shift > content["return_deadline"]:
            return f"deadline shift {deadline_shift} past promised {content['return_deadline']}"
        return None

    def _decide_pregrind(self, surviving, hand, grinds, req_map, free):
        max_extra = len(free)
        k = guarantee_grinds(surviving, hand, grinds, req_map, max_extra)
        if k is None:
            # Under the pregrand policy the counter will not measure the type, so
            # an un-guaranteeable request is suspended rather than refused.
            return HUNG, self._decision(
                HUNG,
                reason="cannot guarantee both directions across every surviving type without identifying it; order suspended",
                free_slots=free,
                surviving=surviving,
            )
        shifts = free[:k]
        plan = {
            "extra_grinds": k,
            "shifts": shifts,
            "guaranteed_shapes": [
                {"candidate": c, "shape": shape_of(c, hand, grinds + k)} for c in surviving
            ],
        }
        decision = self._decision(SIGNED, reason="guaranteed without identifying the type", free_slots=free, plan=plan, surviving=surviving)
        decision["reserved_shifts"] = shifts
        decision["reserved_kind"] = "GRIND"
        return SIGNED, decision

    def _decide_remeasure(self, surviving, hand, grinds, req_map, free):
        per_candidate = {
            canonical_json(c): min_grinds_for_candidate(c, hand, grinds, req_map, len(free))
            for c in surviving
        }
        feasible = [v for v in per_candidate.values() if v is not None]
        if not feasible:
            return REFUSED, self._decision(REFUSED, reason="no surviving type can ever meet the requirement", free_slots=free, surviving=surviving)
        best = min(feasible)
        if len(surviving) == 1:
            # Type is already pinned by existing trials: no remeasure needed.
            k = best
            if k > len(free):
                return REFUSED, self._decision(REFUSED, reason="necessary grinds do not fit before the deadline", free_slots=free, surviving=surviving)
            shifts = free[:k]
            plan = {"extra_grinds": k, "shifts": shifts, "remeasured": False,
                    "guaranteed_shapes": [{"candidate": surviving[0], "shape": shape_of(surviving[0], hand, grinds + k)}]}
            decision = self._decision(SIGNED, reason="type known; grind to size", free_slots=free, plan=plan, surviving=surviving)
            decision["reserved_shifts"] = shifts
            decision["reserved_kind"] = "GRIND"
            return SIGNED, decision
        # Ambiguous: one whole shift for a two-direction try-write, then the
        # identified type can be ground.  Best case needs `best` grinds.
        needed = 1 + best
        if needed > len(free):
            return REFUSED, self._decision(
                REFUSED,
                reason="remeasure plus the necessary grind cannot fit before the deadline",
                free_slots=free,
                surviving=surviving,
            )
        remeasure_shift = free[0]
        plan = {
            "remeasure_shift": remeasure_shift,
            "min_grinds_after_measure": best,
            "per_candidate_min_grinds": per_candidate,
        }
        decision = self._decision(
            AWAITING_REMEASURE,
            reason="type ambiguous; occupy one shift for a two-direction try-write",
            free_slots=free,
            plan=plan,
            surviving=surviving,
        )
        # Reserve the measuring shift now.  The best-case grind shifts stay
        # unreserved until the type is known (reserving too many would over-book
        # the single slot for a type that may need fewer).
        decision["reserved_shifts"] = [remeasure_shift]
        decision["reserved_kind"] = "REMEASURE"
        return AWAITING_REMEASURE, decision

    def _decision(self, status: str, **kwargs) -> Dict[str, Any]:
        decision = {"status": status}
        decision.update(kwargs)
        return decision

    def _persist_order(self, conn, order_id: str, body: Mapping[str, Any], status: str, decision: Dict[str, Any]) -> None:
        decision = dict(decision)
        decision["body"] = body
        req = body["requirement"]
        signed = 1 if status == SIGNED else 0
        conn.execute(
            "INSERT INTO nib_orders (order_id, sale_id, unit_id, strategy, require_hand, req_h_min, req_h_max, req_v_min, req_v_max,"
            " deadline_shift, opened_shift, claimant_id, status, signed, plan_json, decision_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order_id,
                body["sale_id"],
                body["unit_id"],
                body["strategy"],
                req["hand"],
                req["h"][0],
                req["h"][1],
                req["v"][0],
                req["v"][1],
                body["deadline_shift"],
                body["opened_shift"],
                body["claimant_id"],
                status,
                signed,
                canonical_json(decision.get("plan", {})),
                canonical_json(decision),
                utc_now(),
            ),
        )

    def _update_decision(self, conn, order_id: str, status: str, decision: Dict[str, Any]) -> None:
        row = conn.execute("SELECT decision_json FROM nib_orders WHERE order_id = ?", (order_id,)).fetchone()
        body = json_loads(row["decision_json"])["body"]
        decision = dict(decision)
        decision["body"] = body
        signed = 1 if status == SIGNED else 0
        conn.execute(
            "UPDATE nib_orders SET status = ?, signed = ?, plan_json = ?, decision_json = ? WHERE order_id = ?",
            (status, signed, canonical_json(decision.get("plan", {})), canonical_json(decision), order_id),
        )

    def _order_row(self, conn, order_id: str) -> Mapping[str, Any]:
        row = conn.execute("SELECT * FROM nib_orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            raise BasketError(NIB_ORDER_UNKNOWN, "unknown nib order", {"order_id": order_id})
        return row

    def _order_result(self, conn, order_id: str, *, repeated: bool) -> Dict[str, Any]:
        row = self._order_row(conn, order_id)
        decision = json_loads(row["decision_json"])
        delivery = conn.execute("SELECT * FROM nib_deliveries WHERE order_id = ?", (order_id,)).fetchone()
        grinds = conn.execute("SELECT * FROM nib_grinds WHERE order_id = ? ORDER BY seq", (order_id,)).fetchall()
        return {
            "ok": True,
            "order_id": order_id,
            "sale_id": row["sale_id"],
            "unit_id": row["unit_id"],
            "strategy": row["strategy"],
            "claimant_id": row["claimant_id"],
            "requirement": decision["body"]["requirement"],
            "deadline_shift": row["deadline_shift"],
            "opened_shift": row["opened_shift"],
            "status": row["status"],
            "signed": bool(row["signed"]),
            "decision": {k: v for k, v in decision.items() if k != "body"},
            "grinds": [{"shift_no": g["shift_no"], "grinds_before": g["grinds_before"], "grinds_after": g["grinds_after"]} for g in grinds],
            "delivery": None
            if delivery is None
            else {
                "shift_no": delivery["shift_no"],
                "measured_h": delivery["measured_h"],
                "measured_v": delivery["measured_v"],
                "evidence": json_loads(delivery["evidence_json"]),
            },
            "repeated": repeated,
        }

    def get_nib_order(self, order_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            return self._order_result(conn, order_id, repeated=False)

    def _reserve_shift(self, conn, shift_no: int, order_id: str, kind: str) -> None:
        row = conn.execute("SELECT order_id, kind FROM nib_shifts WHERE shift_no = ?", (shift_no,)).fetchone()
        if row is not None:
            if row["order_id"] == order_id and row["kind"] == kind:
                return  # idempotent for the order that already holds it
            raise BasketError(
                NIB_SLOT_CONFLICT,
                "the single grind slot of this shift is already taken",
                {"shift_no": shift_no, "held_by": row["order_id"], "kind": row["kind"]},
            )
        conn.execute(
            "INSERT INTO nib_shifts (shift_no, order_id, kind, created_at) VALUES (?, ?, ?, ?)",
            (shift_no, order_id, kind, utc_now()),
        )

    def reserve_slot(self, shift_no: int, order_id: str, kind: str = "GRIND") -> Dict[str, Any]:
        """Explicitly occupy a shift's slot (models a locked order holding it)."""

        if not is_int(shift_no) or shift_no < 1:
            raise BasketError(INVALID_INPUT, "shift_no must be a positive integer")
        if kind not in ("GRIND", "REMEASURE"):
            raise BasketError(INVALID_INPUT, "kind must be GRIND or REMEASURE")
        with self.db.transaction() as conn:
            self._reserve_shift(conn, shift_no, order_id, kind)
        return {"ok": True, "shift_no": shift_no, "order_id": order_id, "kind": kind}

    def remeasure_nib(self, order_id: str, *, shift_no: int, h_width: Any, v_width: Any, hand: Optional[str] = None) -> Dict[str, Any]:
        order_id = require_id(order_id, INVALID_INPUT, "order_id")
        for width, name in ((h_width, "h_width"), (v_width, "v_width")):
            if not is_int(width) or width < 1:
                raise BasketError(INVALID_INPUT, f"{name} must be a positive integer")
        if not is_int(shift_no) or shift_no < 1:
            raise BasketError(INVALID_INPUT, "shift_no must be a positive integer")
        with self.db.transaction() as conn:
            row = self._order_row(conn, order_id)
            if row["status"] == DELIVERED:
                return self._order_result(conn, order_id, repeated=True)
            if row["status"] != AWAITING_REMEASURE:
                raise BasketError(NIB_ORDER_STATE, "order is not awaiting a remeasurement", {"order_id": order_id, "status": row["status"]})
            if shift_no < row["opened_shift"] or shift_no > row["deadline_shift"]:
                raise BasketError(NIB_ORDER_STATE, "remeasurement shift is outside the order window", {"shift_no": shift_no})
            self._reserve_shift(conn, shift_no, order_id, "REMEASURE")
            nib = self._load_nib(conn, row["sale_id"], row["unit_id"])
            if nib["returned"]:
                raise BasketError(NIB_UNIT_RETURNED, "unit was refunded; cannot remeasure it", {"sale_id": row["sale_id"], "unit_id": row["unit_id"]})
            content = json_loads(nib["content_json"])
            hand = hand or row["require_hand"] or content["hand"]
            grinds = nib["committed_grinds"]
            self._insert_trial(conn, row["sale_id"], row["unit_id"], {"hand": hand, "direction": H, "width": h_width}, grinds, shift_no, "REMEASURE")
            self._insert_trial(conn, row["sale_id"], row["unit_id"], {"hand": hand, "direction": V, "width": v_width}, grinds, shift_no, "REMEASURE")
            surviving = self._surviving(conn, row["sale_id"], row["unit_id"])
            req = json_loads(row["decision_json"])["body"]["requirement"]
            req_map = _req_map(req)
            free = self._free_slots(conn, shift_no + 1, row["deadline_shift"])
            if not surviving:
                decision = self._decision(REFUSED, reason="two-direction reading contradicts every candidate", free_slots=free)
                self._update_decision(conn, order_id, REFUSED, decision)
                return self._order_result(conn, order_id, repeated=False)
            if len(surviving) > 1:
                decision = self._decision(HUNG, reason="still ambiguous after measuring both directions", free_slots=free, surviving=surviving)
                self._update_decision(conn, order_id, HUNG, decision)
                return self._order_result(conn, order_id, repeated=False)
            cand = surviving[0]
            k = min_grinds_for_candidate(cand, hand, grinds, req_map, len(free))
            if k is None:
                decision = self._decision(REFUSED, reason="measured type cannot reach the requested widths", free_slots=free, surviving=surviving)
                self._update_decision(conn, order_id, REFUSED, decision)
                return self._order_result(conn, order_id, repeated=False)
            plan = {
                "extra_grinds": k,
                "shifts": free[:k],
                "remeasured": True,
                "remeasure_shift": shift_no,
                "guaranteed_shapes": [{"candidate": cand, "shape": shape_of(cand, hand, grinds + k)}],
            }
            # Lock the grind shifts we just promised; they cannot be re-sold.
            for grind_shift in plan["shifts"]:
                self._reserve_shift(conn, int(grind_shift), order_id, "GRIND")
            decision = self._decision(SIGNED, reason="identified type by remeasure; deliverable before deadline", free_slots=free, plan=plan, surviving=surviving)
            self._update_decision(conn, order_id, SIGNED, decision)
            return self._order_result(conn, order_id, repeated=False)

    def grind_nib(self, order_id: str, *, shift_no: int) -> Dict[str, Any]:
        order_id = require_id(order_id, INVALID_INPUT, "order_id")
        if not is_int(shift_no) or shift_no < 1:
            raise BasketError(INVALID_INPUT, "shift_no must be a positive integer")
        with self.db.transaction() as conn:
            row = self._order_row(conn, order_id)
            if row["status"] == DELIVERED:
                raise BasketError(NIB_ORDER_STATE, "order already delivered", {"order_id": order_id})
            if row["status"] != SIGNED:
                raise BasketError(NIB_ORDER_STATE, "only a signed order can be ground", {"order_id": order_id, "status": row["status"]})
            if shift_no < row["opened_shift"] or shift_no > row["deadline_shift"]:
                raise BasketError(NIB_ORDER_STATE, "grind shift is outside the order window", {"shift_no": shift_no})
            decision = json_loads(row["decision_json"])
            plan = decision.get("plan", {})
            target = plan.get("extra_grinds", 0)
            done = conn.execute("SELECT COUNT(*) AS n FROM nib_grinds WHERE order_id = ?", (order_id,)).fetchone()["n"]
            if done >= target:
                raise BasketError(NIB_ORDER_STATE, "the planned grinds for this order are already done", {"order_id": order_id, "planned": target})
            if shift_no not in plan.get("shifts", []):
                raise BasketError(NIB_SLOT_CONFLICT, "shift is not one this order reserved", {"order_id": order_id, "shift_no": shift_no, "planned": plan.get("shifts", [])})
            nib = self._load_nib(conn, row["sale_id"], row["unit_id"])
            if nib["returned"]:
                raise BasketError(NIB_UNIT_RETURNED, "unit was refunded; cannot grind it", {"sale_id": row["sale_id"], "unit_id": row["unit_id"]})
            surviving = self._surviving(conn, row["sale_id"], row["unit_id"])
            new_grinds = nib["committed_grinds"] + 1
            for cand in surviving:
                if cand["base"] - new_grinds < 1:
                    raise BasketError(NIB_REQUIREMENT_VIOLATED, "grinding would destroy a surviving nib type", {"candidate": cand})
            self._reserve_shift(conn, shift_no, order_id, "GRIND")
            conn.execute(
                "UPDATE nib_units SET committed_grinds = ? WHERE sale_id = ? AND unit_id = ?",
                (new_grinds, row["sale_id"], row["unit_id"]),
            )
            conn.execute(
                "INSERT INTO nib_grinds (sale_id, unit_id, order_id, shift_no, grinds_before, grinds_after, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["sale_id"], row["unit_id"], order_id, shift_no, nib["committed_grinds"], new_grinds, utc_now()),
            )
            return self._order_result(conn, order_id, repeated=False)

    def deliver_nib(self, order_id: str, *, shift_no: int, measured_h: Any, measured_v: Any) -> Dict[str, Any]:
        order_id = require_id(order_id, INVALID_INPUT, "order_id")
        for width, name in ((measured_h, "measured_h"), (measured_v, "measured_v")):
            if not is_int(width) or width < 0:
                raise BasketError(INVALID_INPUT, f"{name} must be a non-negative integer")
        if not is_int(shift_no) or shift_no < 1:
            raise BasketError(INVALID_INPUT, "shift_no must be a positive integer")
        with self.db.transaction() as conn:
            row = self._order_row(conn, order_id)
            existing = conn.execute("SELECT * FROM nib_deliveries WHERE order_id = ?", (order_id,)).fetchone()
            if existing is not None:
                if existing["measured_h"] != measured_h or existing["measured_v"] != measured_v:
                    raise BasketError(NIB_UNIT_CONFLICT, "delivery already recorded with different widths", {"order_id": order_id})
                return self._order_result(conn, order_id, repeated=True)
            if row["status"] != SIGNED:
                raise BasketError(NIB_ORDER_STATE, "only a signed order can be delivered", {"order_id": order_id, "status": row["status"]})
            if shift_no > row["deadline_shift"]:
                raise BasketError(NIB_ORDER_STATE, "delivery is past the deadline", {"shift_no": shift_no, "deadline_shift": row["deadline_shift"]})
            if shift_no < row["opened_shift"]:
                raise BasketError(NIB_ORDER_STATE, "delivery precedes the order opening", {"shift_no": shift_no, "opened_shift": row["opened_shift"]})
            decision = json_loads(row["decision_json"])
            plan = decision.get("plan", {})
            target = plan.get("extra_grinds", 0)
            done = conn.execute("SELECT COUNT(*) AS n FROM nib_grinds WHERE order_id = ?", (order_id,)).fetchone()["n"]
            if done < target:
                raise BasketError(NIB_REQUIREMENT_VIOLATED, "planned grinds are not complete", {"planned": target, "done": done})
            nib = self._load_nib(conn, row["sale_id"], row["unit_id"])
            if nib["returned"]:
                raise BasketError(NIB_UNIT_RETURNED, "unit was refunded; cannot deliver it", {"sale_id": row["sale_id"], "unit_id": row["unit_id"]})
            if nib["holder"] != "SHOP":
                raise BasketError(NIB_UNIT_DELIVERED, "unit already handed to someone else", {"holder": nib["holder"]})
            surviving = self._surviving(conn, row["sale_id"], row["unit_id"])
            grinds = nib["committed_grinds"]
            # The width pair is what the customer's hand will write, so the
            # consistency check uses the requirement hand, not the default one.
            hand = decision["body"]["requirement"]["hand"]
            measured = {H: measured_h, V: measured_v}
            consistent = [c for c in surviving if shape_of(c, hand, grinds) == measured]
            if not consistent:
                raise BasketError(
                    NIB_EVIDENCE_MISMATCH,
                    "measured widths do not match any surviving nib type at the current grind",
                    {"measured": measured, "surviving": surviving, "grinds_done": grinds},
                )
            req = _req_map(decision["body"]["requirement"])
            if not satisfies(measured, req):
                raise BasketError(NIB_REQUIREMENT_VIOLATED, "measured widths violate the signed requirement", {"measured": measured, "requirement": decision["body"]["requirement"]})
            evidence = {
                "measured": measured,
                "grinds_done": grinds,
                "consistent_candidates": consistent,
                "requirement": decision["body"]["requirement"],
                "original_width_available": grinds == 0,
            }
            conn.execute(
                "INSERT INTO nib_deliveries (order_id, sale_id, unit_id, shift_no, measured_h, measured_v, realized_json, evidence_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    row["sale_id"],
                    row["unit_id"],
                    shift_no,
                    measured_h,
                    measured_v,
                    canonical_json(measured),
                    canonical_json(evidence),
                    utc_now(),
                ),
            )
            conn.execute(
                "UPDATE nib_units SET holder = ?, delivered_order_id = ? WHERE sale_id = ? AND unit_id = ?",
                (row["claimant_id"], order_id, row["sale_id"], row["unit_id"]),
            )
            conn.execute("UPDATE nib_orders SET status = ? WHERE order_id = ?", (DELIVERED, order_id))
            return self._order_result(conn, order_id, repeated=False)

    def nib_availability(self, lo: int = 1, hi: int = 8) -> Dict[str, Any]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT shift_no, order_id, kind FROM nib_shifts WHERE shift_no BETWEEN ? AND ? ORDER BY shift_no", (lo, hi)).fetchall()
        used = {r["shift_no"]: {"order_id": r["order_id"], "kind": r["kind"]} for r in rows}
        return {"slots": {s: used.get(s) for s in range(lo, hi + 1)}}
