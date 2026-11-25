"""Troupe fitting, cabinet allocation and paired delivery.

This subsystem is layered on top of the existing quote service and ledger.  It
uses the *original order* as its source of physical truth: a physical shoe is
recovered from a real sale unit (the unit's real ``unit_id`` and stored ``paid``
amount) or registered as store stock.  It then runs the dance troupe workflow:

* a fitting master (试楦师) records, for every physical shoe, its length, instep
  width and the directions it may be stretched;
* named dancers carry a left/right foot range and a consecutive-run requirement;
* a *fitting slip* publishes which feet a physical shoe may be allocated to;
* the cabinet consumes released slips, resolves competing claims ("grabbing")
  and delivers *whole pairs*, binding both shoes of a pair to one dancer until
  that dancer's run ends;
* tied laces and every applied stretch are persisted, so the state is fully
  inspectable after the shift is reopened.

Two shop modes share ordinary stretching but differ in custody:

* ``PRESTRETCH`` -- the cabinet pre-stretches and locks pairs for the whole run;
  a stretched width never shrinks and a bound (even idle) shift cannot promise
  the shoe elsewhere.  It works from its own physical stock and does not handle
  the troupe's pad transfers.
* ``PERF_TRANSFER`` -- shoes and a movable narrowing pad are handed over per
  show and every handover leaves a return receipt.  A narrowing pad reduces the
  in-shoe width by one step and lives in at most one shoe at a time.

Consent and after-sales gate eligibility: a shoe enters the cabinet only when a
named dancer has deposited it and signed troupe-adjustment consent; revoking
that consent, or returning the origin unit, immediately stops *new* allocation.

The allocation core is an exhaustive, deterministic maximiser for the small
sizes in the brief: it returns the largest number of complete pairs given the
current widths, stretch limits, stretch budget and the available narrowing pads.
Ties may be broken arbitrarily by the caller; this module picks deterministically.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from .db import Database, canonical_json, json_loads, utc_now
from .errors import (
    ALLOCATION_CONFLICT,
    CONSENT_REVOKED,
    DANCER_CONFLICT,
    DEPOSIT_CONFLICT,
    FITTING_CONFLICT,
    INVALID_ALLOCATION,
    INVALID_FOOT,
    INVALID_INPUT,
    INVALID_RECEIPT,
    INVALID_SHOP_MODE,
    PAD_CONFLICT,
    PAD_IN_USE,
    PHYSICAL_CONFLICT,
    PHYSICAL_UNAVAILABLE,
    RETURN_CONFIRMATION_CONFLICT,
    TRANSFER_CONFLICT,
    UNKNOWN_DANCER,
    UNKNOWN_DEPOSIT,
    UNKNOWN_FITTING_SLIP,
    UNKNOWN_PAD,
    UNKNOWN_PHYSICAL,
    WIDTH_IRREVERSIBLE,
    BasketError,
)
from .validation import is_int, require_id

SIDES = ("L", "R")
SHOP_MODES = ("PRESTRETCH", "PERF_TRANSFER")
SHOP = "shop"
TROUPE = "troupe"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def _req_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if not is_int(value) or value < minimum:
        raise BasketError(INVALID_INPUT, f"{field} must be an integer >= {minimum}", {"field": field, "value": value})
    return value


def _opt_int(value: Any, field: str, *, minimum: int = 0) -> Optional[int]:
    if value is None:
        return None
    return _req_int(value, field, minimum=minimum)


def _side(value: Any) -> str:
    if value not in SIDES:
        raise BasketError(INVALID_FOOT, "side must be 'L' or 'R'", {"side": value})
    return value


def _canonical_directions(directions: Any) -> List[str]:
    if directions is None:
        return ["WIDTH"]
    if isinstance(directions, str):
        directions = [directions]
    if not isinstance(directions, Sequence) or isinstance(directions, (str, bytes)):
        raise BasketError(INVALID_INPUT, "directions must be a list of strings")
    result = sorted({d for d in directions if isinstance(d, str) and d})
    if len(result) != len(set(directions)):
        raise BasketError(INVALID_INPUT, "directions must be non-empty strings")
    return result


def _canonical_foot(spec: Any, field: str) -> Optional[Dict[str, Any]]:
    """Canonicalise one foot's fit requirement.

    ``None`` means the foot is already shod (nothing to source).  Otherwise a
    spec is ``{"widths": [int, ...], "length": int|None}``; ``{"width": w}`` is
    accepted as sugar for ``{"widths": [w]}``.
    """

    if spec is None:
        return None
    if not isinstance(spec, Mapping):
        raise BasketError(INVALID_FOOT, f"{field} must be an object or null", {"field": field})
    if "widths" in spec:
        raw = spec["widths"]
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise BasketError(INVALID_FOOT, f"{field}.widths must be a list", {"field": field})
        widths = sorted({_req_int(w, f"{field}.widths", minimum=0) for w in raw})
        if not widths:
            raise BasketError(INVALID_FOOT, f"{field}.widths must not be empty", {"field": field})
    elif "width" in spec:
        widths = [_req_int(spec["width"], f"{field}.width", minimum=0)]
    else:
        raise BasketError(INVALID_FOOT, f"{field} needs width or widths", {"field": field})
    return {"widths": widths, "length": _opt_int(spec.get("length"), f"{field}.length", minimum=0)}


def _canonical_need(raw: Any, index: int) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise BasketError(INVALID_ALLOCATION, "each need must be an object", {"index": index})
    dancer_id = require_id(raw.get("dancer_id"), INVALID_INPUT, f"needs[{index}].dancer_id")
    left = _canonical_foot(raw.get("left"), f"needs[{index}].left")
    right = _canonical_foot(raw.get("right"), f"needs[{index}].right")
    if left is None and right is None:
        raise BasketError(INVALID_ALLOCATION, "a need must require at least one foot", {"dancer_id": dancer_id})
    return {"dancer_id": dancer_id, "left": left, "right": right}


def _normalise_foot(foot: Any) -> Optional[Dict[str, Any]]:
    """Accept a canonical foot, a raw ``{"width"|"widths", "length"}`` dict."""

    if foot is None or (isinstance(foot, Mapping) and set(foot) >= {"widths", "length"}):
        return foot
    if isinstance(foot, Mapping) and ("widths" in foot or "width" in foot):
        if "widths" in foot:
            widths = sorted({int(w) for w in foot["widths"]})
        else:
            widths = [int(foot["width"])]
        return {"widths": widths, "length": foot.get("length")}
    return foot


# ---------------------------------------------------------------------------
# Exhaustive allocation core
# ---------------------------------------------------------------------------
def shoe_options(
    shoe: Mapping[str, Any],
    foot: Mapping[str, Any],
    *,
    pad_available: bool,
    stretch_remaining: int,
) -> List[Dict[str, Any]]:
    """All legal ways a physical shoe can fit one required foot.

    An option records the *raw width* the shoe is stretched to (``raw_width``),
    the number of stretch operations (0 or 1), and whether the single-step
    narrowing pad is placed here (``pad``).  Effective width is
    ``raw_width - pad_step`` when padded.
    """

    if foot["length"] is not None:
        if shoe["length"] is None or shoe["length"] != foot["length"]:
            return []
    current = shoe["width"]
    can_stretch = "WIDTH" in shoe["directions"]
    cap = shoe["stretch_to"] if can_stretch else current
    cap = max(cap, current)
    step = int(shoe.get("pad_step", 1))
    wanted = set(foot["widths"])
    options: List[Dict[str, Any]] = []
    for raw in range(current, cap + 1):
        stretch = 0 if raw == current else 1
        if stretch > stretch_remaining:
            continue
        if raw in wanted:
            options.append({"raw_width": raw, "stretch": stretch, "pad": False})
        if pad_available and (raw - step) in wanted:
            options.append({"raw_width": raw, "stretch": stretch, "pad": True})
    options.sort(key=lambda o: (o["pad"], o["stretch"], o["raw_width"]))
    return options


def plan_pairs(
    shoes: Sequence[Mapping[str, Any]],
    needs: Sequence[Mapping[str, Any]],
    *,
    pads_available: int = 0,
    stretch_budget: int = 0,
) -> Dict[str, Any]:
    """Maximise complete pairs; return one deterministic optimal assignment.

    Only *available* shoes should be passed.  Each shoe supplies at most one
    foot.  A need is a complete pair when every required foot is covered (a
    ``None`` foot counts as already owned).  With equal pair counts the planner
    prefers fewer pads, then fewer stretches, then a lexicographic order -- any
    of which the brief allows ("并列可任选").
    """

    needs = sorted(needs, key=lambda n: n["dancer_id"])
    # Accept both canonical ``{"widths": [...]}`` and the ``{"width": w}`` sugar.
    needs = [dict(n, left=_normalise_foot(n.get("left")), right=_normalise_foot(n.get("right"))) for n in needs]
    shoe_by_id = {s["physical_id"]: s for s in shoes}
    shoe_ids = sorted(shoe_by_id)

    def combos_for(need: Mapping[str, Any], used: Set[str], pads_used: int, stretch_used: int):
        """Yield (chosen_feet, pads_delta, stretch_delta) fully satisfying ``need``."""

        left = need["left"]
        right = need["right"]

        def foot_choices(foot, side):
            if foot is None:
                return [None]
            choices = []
            for pid in shoe_ids:
                if pid in used:
                    continue
                shoe = shoe_by_id[pid]
                if shoe["side"] != side:
                    continue
                for opt in shoe_options(
                    shoe, foot,
                    pad_available=bool(pads_available) and pads_used < pads_available,
                    stretch_remaining=stretch_budget - stretch_used,
                ):
                    choices.append((pid, opt))
            return choices

        left_choices = foot_choices(left, "L")
        right_choices = foot_choices(right, "R")
        results = []
        for lc in left_choices:
            for rc in right_choices:
                if lc is not None and rc is not None and lc[0] == rc[0]:
                    continue
                used_ids = {c[0] for c in (lc, rc) if c is not None}
                if len(used_ids) != (1 if (lc is None) != (rc is None) else (2 if lc is not None else 0)):
                    continue
                pad_delta = sum(1 for c in (lc, rc) if c is not None and c[1]["pad"])
                stretch_delta = sum(c[1]["stretch"] for c in (lc, rc) if c is not None)
                if pads_used + pad_delta > pads_available:
                    continue
                if stretch_used + stretch_delta > stretch_budget:
                    continue
                results.append(((lc, rc), pad_delta, stretch_delta))
        return results

    best: Dict[str, Any] = {"count": 0, "chosen": [], "pads_used": 0, "stretch_used": 0}

    def key_for(chosen, pads_used, stretch_used):
        flat = []
        for need_id, lc, rc in chosen:
            if lc is not None:
                flat.append((need_id, "L", lc[0]))
            if rc is not None:
                flat.append((need_id, "R", rc[0]))
        return (pads_used, stretch_used, tuple(sorted(flat)))

    def dfs(index: int, used: Set[str], pads_used: int, stretch_used: int, chosen: List[Any]) -> None:
        if index == len(needs):
            count = len(chosen)
            if count > best["count"]:
                best.update(count=count, chosen=list(chosen), pads_used=pads_used, stretch_used=stretch_used)
            elif count == best["count"]:
                new_key = key_for(chosen, pads_used, stretch_used)
                old_key = key_for(best["chosen"], best["pads_used"], best["stretch_used"])
                if new_key < old_key:
                    best.update(count=count, chosen=list(chosen), pads_used=pads_used, stretch_used=stretch_used)
            return
        # Upper bound prune.
        if len(chosen) + (len(needs) - index) < best["count"]:
            return
        need = needs[index]
        for (lc, rc), pad_delta, stretch_delta in combos_for(need, used, pads_used, stretch_used):
            newly = {c[0] for c in (lc, rc) if c is not None}
            chosen.append((need["dancer_id"], lc, rc))
            dfs(index + 1, used | newly, pads_used + pad_delta, stretch_used + stretch_delta, chosen)
            chosen.pop()
        dfs(index + 1, used, pads_used, stretch_used, chosen)  # skip this need

    dfs(0, set(), 0, 0, [])

    pairs = []
    for dancer_id, lc, rc in sorted(best["chosen"], key=lambda c: c[0]):
        entry: Dict[str, Any] = {"dancer_id": dancer_id}
        for side, choice in (("L", lc), ("R", rc)):
            if choice is None:
                continue
            pid, opt = choice
            entry[side] = {"physical_id": pid, "raw_width": opt["raw_width"], "stretch": bool(opt["stretch"]), "pad": bool(opt["pad"])}
        pairs.append(entry)
    return {
        "count": best["count"],
        "pairs": pairs,
        "pads_used": best["pads_used"],
        "stretch_used": best["stretch_used"],
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class TroupeService:
    def __init__(self, db: Database):
        self.db = db

    # -- shop mode ---------------------------------------------------------
    def set_shop_mode(self, mode: str) -> str:
        if mode not in SHOP_MODES:
            raise BasketError(INVALID_SHOP_MODE, "shop mode must be PRESTRETCH or PERF_TRANSFER", {"mode": mode})
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO shop_config (key, value) VALUES ('mode', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (mode,),
            )
        return mode

    def get_shop_mode(self) -> str:
        with self.db.connection() as conn:
            row = conn.execute("SELECT value FROM shop_config WHERE key = 'mode'").fetchone()
        return row["value"] if row else "PRESTRETCH"

    # -- physical shoes ----------------------------------------------------
    def register_physical(
        self,
        physical_id: str,
        side: str,
        *,
        width: Any,
        length: Any = None,
        stretch_to: Any = None,
        directions: Optional[Sequence[str]] = None,
        origin: str = "STOCK",
        sale_id: Optional[str] = None,
        unit_id: Optional[str] = None,
        fitter: Optional[str] = None,
    ) -> Dict[str, Any]:
        physical_id = require_id(physical_id, INVALID_INPUT, "physical_id")
        side = _side(side)
        width = _req_int(width, "width", minimum=0)
        length = _opt_int(length, "length", minimum=0)
        stretch_to = width if stretch_to is None else _req_int(stretch_to, "stretch_to", minimum=0)
        if stretch_to < width:
            raise BasketError(WIDTH_IRREVERSIBLE, "stretch_to cannot be below the current width", {"width": width, "stretch_to": stretch_to})
        dirs = _canonical_directions(directions)

        paid: Optional[int] = None
        usable = 1
        if origin == "SALE":
            if sale_id is None or unit_id is None:
                raise BasketError(INVALID_INPUT, "sale-origin physical needs sale_id and unit_id")
            sale_id = require_id(sale_id, INVALID_INPUT, "sale_id")
            unit_id = require_id(unit_id, INVALID_INPUT, "unit_id")
            with self.db.connection() as conn:
                row = conn.execute(
                    "SELECT paid, returned, return_request_id FROM sale_units WHERE sale_id = ? AND unit_id = ?",
                    (sale_id, unit_id),
                ).fetchone()
            if row is None:
                raise BasketError(PHYSICAL_CONFLICT, "origin sale unit is unknown", {"sale_id": sale_id, "unit_id": unit_id})
            paid = row["paid"]
            usable = 0 if row["returned"] else 1
        elif origin != "STOCK":
            raise BasketError(INVALID_INPUT, "origin must be SALE or STOCK", {"origin": origin})

        now = utc_now()
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM phys_shoes WHERE physical_id = ?", (physical_id,)).fetchone()
            if existing is not None:
                same = (
                    existing["side"] == side
                    and existing["length"] == length
                    and existing["width"] == width
                    and existing["stretch_to"] == stretch_to
                    and existing["directions_json"] == canonical_json(dirs)
                    and existing["origin"] == origin
                    and existing["sale_id"] == sale_id
                    and existing["unit_id"] == unit_id
                )
                if not same:
                    raise BasketError(PHYSICAL_CONFLICT, "physical_id already registered with different content", {"physical_id": physical_id})
                return self._physical_view(conn, physical_id)
            conn.execute(
                "INSERT INTO phys_shoes (physical_id, side, length, width, stretch_to, directions_json, origin, sale_id, unit_id, paid, usable, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (physical_id, side, length, width, stretch_to, canonical_json(dirs), origin, sale_id, unit_id, paid, usable, now, now),
            )
            conn.execute(
                "INSERT INTO phys_measurements (physical_id, width, stretch_to, fitter, created_at) VALUES (?, ?, ?, ?, ?)",
                (physical_id, width, stretch_to, fitter, now),
            )
            return self._physical_view(conn, physical_id)

    def recover_from_sale(self, physical_id: str, side: str, sale_id: str, unit_id: str, **kwargs) -> Dict[str, Any]:
        """Materialise a physical shoe from a real sale unit (unit_id + paid)."""

        return self.register_physical(physical_id, side, origin="SALE", sale_id=sale_id, unit_id=unit_id, **kwargs)

    def get_physical(self, physical_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            return self._physical_view(conn, require_id(physical_id, INVALID_INPUT, "physical_id"))

    def _physical_view(self, conn, physical_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM phys_shoes WHERE physical_id = ?", (physical_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_PHYSICAL, "unknown physical shoe", {"physical_id": physical_id})
        cons = conn.execute("SELECT * FROM phys_consignments WHERE physical_id = ?", (physical_id,)).fetchone()
        pads = [r["pad_id"] for r in conn.execute("SELECT pad_id FROM pads WHERE physical_id = ? ORDER BY pad_id", (physical_id,)).fetchall()]
        bindings = [
            dict(r)
            for r in conn.execute(
                "SELECT binding_id, dancer_id, pair_id, run_id, run_until, width_delivered, pad_id, tied_laces, released FROM delivery_bindings WHERE physical_id = ? ORDER BY binding_id",
                (physical_id,),
            ).fetchall()
        ]
        occ = [dict(r) for r in conn.execute("SELECT run_id FROM occupancies WHERE physical_id = ? ORDER BY run_id", (physical_id,)).fetchall()]
        # Cross-check the ledger's view of the origin unit (return state / receipt).
        ledger = None
        if row["sale_id"] is not None:
            unit = conn.execute(
                "SELECT returned, return_request_id, grade, paid FROM sale_units WHERE sale_id = ? AND unit_id = ?",
                (row["sale_id"], row["unit_id"]),
            ).fetchone()
            if unit is not None:
                ledger = dict(unit)
        return {
            "physical_id": row["physical_id"],
            "side": row["side"],
            "length": row["length"],
            "width": row["width"],
            "stretch_to": row["stretch_to"],
            "directions": json_loads(row["directions_json"]),
            "origin": row["origin"],
            "sale_id": row["sale_id"],
            "unit_id": row["unit_id"],
            "paid": row["paid"],
            "usable": bool(row["usable"]),
            "consigned": bool(cons and cons["consented"] and not cons["revoked"]),
            "consignment": dict(cons) if cons else None,
            "pads": pads,
            "bindings": bindings,
            "occupancies": occ,
            "origin_ledger": ledger,
        }

    def stretch(self, physical_id: str, to_width: Any, *, fitter: Optional[str] = None) -> Dict[str, Any]:
        physical_id = require_id(physical_id, INVALID_INPUT, "physical_id")
        to_width = _req_int(to_width, "to_width", minimum=0)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM phys_shoes WHERE physical_id = ?", (physical_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_PHYSICAL, "unknown physical shoe", {"physical_id": physical_id})
            if to_width < row["width"]:
                raise BasketError(WIDTH_IRREVERSIBLE, "a stretched width cannot be reduced", {"current": row["width"], "to_width": to_width})
            if to_width > row["stretch_to"]:
                raise BasketError(WIDTH_IRREVERSIBLE, "stretch exceeds the fitting limit", {"stretch_to": row["stretch_to"], "to_width": to_width})
            conn.execute("UPDATE phys_shoes SET width = ?, updated_at = ? WHERE physical_id = ?", (to_width, utc_now(), physical_id))
            conn.execute(
                "INSERT INTO phys_measurements (physical_id, width, stretch_to, fitter, created_at) VALUES (?, ?, ?, ?, ?)",
                (physical_id, to_width, row["stretch_to"], fitter, utc_now()),
            )
            return self._physical_view(conn, physical_id)

    def update_fitting(self, physical_id: str, *, stretch_to: Any = ..., directions: Any = ..., length: Any = ...) -> Dict[str, Any]:
        """Fitting master's record update, e.g. "this shoe cannot stretch further".

        A stretch limit may never be set below the shoe's current width (widths
        only grow).  Setting ``stretch_to`` equal to the current width encodes
        "cannot stretch further".
        """

        physical_id = require_id(physical_id, INVALID_INPUT, "physical_id")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM phys_shoes WHERE physical_id = ?", (physical_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_PHYSICAL, "unknown physical shoe", {"physical_id": physical_id})
            new_stretch = row["stretch_to"] if stretch_to is ... else _req_int(stretch_to, "stretch_to", minimum=0)
            if new_stretch < row["width"]:
                raise BasketError(WIDTH_IRREVERSIBLE, "stretch limit cannot fall below the current width", {"width": row["width"], "stretch_to": new_stretch})
            new_length = row["length"] if length is ... else _opt_int(length, "length", minimum=0)
            new_dirs = json_loads(row["directions_json"]) if directions is ... else _canonical_directions(directions)
            conn.execute(
                "UPDATE phys_shoes SET stretch_to = ?, length = ?, directions_json = ?, updated_at = ? WHERE physical_id = ?",
                (new_stretch, new_length, canonical_json(new_dirs), utc_now(), physical_id),
            )
            return self._physical_view(conn, physical_id)

    def list_physicals(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            ids = [r["physical_id"] for r in conn.execute("SELECT physical_id FROM phys_shoes ORDER BY physical_id").fetchall()]
            return [self._physical_view(conn, pid) for pid in ids]

    # -- named deposit + troupe-adjustment consent -------------------------
    def consign(self, physical_id: str, dancer_id: str, receipt_id: str) -> Dict[str, Any]:
        physical_id = require_id(physical_id, INVALID_INPUT, "physical_id")
        dancer_id = require_id(dancer_id, INVALID_INPUT, "dancer_id")
        receipt_id = require_id(receipt_id, INVALID_INPUT, "receipt_id")
        now = utc_now()
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM phys_shoes WHERE physical_id = ?", (physical_id,)).fetchone() is None:
                raise BasketError(UNKNOWN_PHYSICAL, "unknown physical shoe", {"physical_id": physical_id})
            existing = conn.execute("SELECT * FROM phys_consignments WHERE physical_id = ?", (physical_id,)).fetchone()
            if existing is not None:
                if existing["dancer_id"] == dancer_id and existing["consent_receipt"] == receipt_id and not existing["revoked"]:
                    return dict(existing)
                raise BasketError(DEPOSIT_CONFLICT, "physical already consigned to another dancer or receipt", {"physical_id": physical_id})
            conn.execute(
                "INSERT INTO phys_consignments (physical_id, dancer_id, consent_receipt, consented, revoked, created_at, updated_at) VALUES (?, ?, ?, 1, 0, ?, ?)",
                (physical_id, dancer_id, receipt_id, now, now),
            )
            return dict(conn.execute("SELECT * FROM phys_consignments WHERE physical_id = ?", (physical_id,)).fetchone())

    def revoke_consignment(self, physical_id: str) -> Dict[str, Any]:
        physical_id = require_id(physical_id, INVALID_INPUT, "physical_id")
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM phys_consignments WHERE physical_id = ?", (physical_id,)).fetchone()
            if existing is None:
                raise BasketError(UNKNOWN_DEPOSIT, "physical has no deposit/consent", {"physical_id": physical_id})
            conn.execute("UPDATE phys_consignments SET revoked = 1, updated_at = ? WHERE physical_id = ?", (utc_now(), physical_id))
            return dict(conn.execute("SELECT * FROM phys_consignments WHERE physical_id = ?", (physical_id,)).fetchone())

    # -- dancers -----------------------------------------------------------
    def register_dancer(self, dancer_id: str, *, name: Optional[str] = None, left: Any = None, right: Any = None, run: Optional[str] = None) -> Dict[str, Any]:
        dancer_id = require_id(dancer_id, INVALID_INPUT, "dancer_id")
        content = {
            "dancer_id": dancer_id,
            "name": name if name is not None else dancer_id,
            "left": _canonical_foot(left, "left"),
            "right": _canonical_foot(right, "right"),
            "run": run,
        }
        if content["left"] is None and content["right"] is None:
            raise BasketError(INVALID_FOOT, "a dancer needs at least one foot spec", {"dancer_id": dancer_id})
        now = utc_now()
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT content_json FROM troupe_dancers WHERE dancer_id = ?", (dancer_id,)).fetchone()
            if existing is not None:
                if existing["content_json"] != canonical_json(content):
                    raise BasketError(DANCER_CONFLICT, "dancer_id already registered with different content", {"dancer_id": dancer_id})
                return json_loads(existing["content_json"])
            conn.execute(
                "INSERT INTO troupe_dancers (dancer_id, content_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (dancer_id, canonical_json(content), now, now),
            )
            return content

    def get_dancer(self, dancer_id: str) -> Dict[str, Any]:
        dancer_id = require_id(dancer_id, INVALID_INPUT, "dancer_id")
        with self.db.connection() as conn:
            row = conn.execute("SELECT content_json FROM troupe_dancers WHERE dancer_id = ?", (dancer_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_DANCER, "unknown dancer", {"dancer_id": dancer_id})
        return json_loads(row["content_json"])

    def list_dancers(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT content_json FROM troupe_dancers ORDER BY dancer_id").fetchall()
        return [json_loads(r["content_json"]) for r in rows]

    # -- fitting slips -----------------------------------------------------
    def publish_fitting_slip(self, slip_id: str, physical_id: str) -> Dict[str, Any]:
        """Publish (or re-release) a fitting slip for a physical shoe.

        The slip pins the fitting master's current attributes and lists the named
        dancers' feet the shoe can reach *without* a pad, so the cabinet knows
        what it may be allocated to.
        """

        slip_id = require_id(slip_id, INVALID_INPUT, "slip_id")
        physical_id = require_id(physical_id, INVALID_INPUT, "physical_id")
        now = utc_now()
        with self.db.transaction() as conn:
            physical = self._physical_view(conn, physical_id)
            dancers = [json_loads(r["content_json"]) for r in conn.execute("SELECT content_json FROM troupe_dancers ORDER BY dancer_id").fetchall()]
            content = {
                "slip_id": slip_id,
                "physical_id": physical_id,
                "side": physical["side"],
                "length": physical["length"],
                "width": physical["width"],
                "stretch_to": physical["stretch_to"],
                "directions": physical["directions"],
                "eligible_feet": self._eligible_feet(physical, dancers),
            }
            existing = conn.execute("SELECT * FROM fitting_slips WHERE slip_id = ?", (slip_id,)).fetchone()
            if existing is not None:
                # A fitting master refreshes the same shoe's slip as its fitting
                # changes; only a slip_id reused for a *different* shoe conflicts.
                if existing["physical_id"] != physical_id:
                    raise BasketError(FITTING_CONFLICT, "slip_id already published for a different physical", {"slip_id": slip_id})
                conn.execute(
                    "UPDATE fitting_slips SET content_json = ?, released = 1, updated_at = ? WHERE slip_id = ?",
                    (canonical_json(content), now, slip_id),
                )
                return self._slip_view(conn, slip_id)
            other = conn.execute("SELECT slip_id FROM fitting_slips WHERE physical_id = ?", (physical_id,)).fetchone()
            if other is not None:
                raise BasketError(FITTING_CONFLICT, "physical already has a fitting slip", {"physical_id": physical_id, "slip_id": other["slip_id"]})
            conn.execute(
                "INSERT INTO fitting_slips (slip_id, physical_id, content_json, released, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                (slip_id, physical_id, canonical_json(content), now, now),
            )
            return self._slip_view(conn, slip_id)

    def retract_fitting_slip(self, slip_id: str) -> Dict[str, Any]:
        slip_id = require_id(slip_id, INVALID_INPUT, "slip_id")
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM fitting_slips WHERE slip_id = ?", (slip_id,)).fetchone() is None:
                raise BasketError(UNKNOWN_FITTING_SLIP, "unknown fitting slip", {"slip_id": slip_id})
            conn.execute("UPDATE fitting_slips SET released = 0, updated_at = ? WHERE slip_id = ?", (utc_now(), slip_id))
            return self._slip_view(conn, slip_id)

    def get_fitting_slip(self, slip_id: str) -> Dict[str, Any]:
        slip_id = require_id(slip_id, INVALID_INPUT, "slip_id")
        with self.db.connection() as conn:
            return self._slip_view(conn, slip_id)

    def list_fitting_slips(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            return [self._slip_view(conn, r["slip_id"]) for r in conn.execute("SELECT slip_id FROM fitting_slips ORDER BY slip_id").fetchall()]

    def _slip_view(self, conn, slip_id: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM fitting_slips WHERE slip_id = ?", (slip_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_FITTING_SLIP, "unknown fitting slip", {"slip_id": slip_id})
        view = json_loads(row["content_json"])
        view["released"] = bool(row["released"])
        return view

    def _eligible_feet(self, physical: Mapping[str, Any], dancers: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        eligible: List[Dict[str, Any]] = []
        side = physical["side"]
        cap = physical["stretch_to"] if "WIDTH" in physical["directions"] else physical["width"]
        reachable = set(range(physical["width"], max(cap, physical["width"]) + 1))
        for dancer in dancers:
            foot = dancer.get("left" if side == "L" else "right")
            if foot is None:
                continue
            if foot["length"] is not None and (physical["length"] is None or physical["length"] != foot["length"]):
                continue
            if reachable & set(foot["widths"]):
                eligible.append({"dancer_id": dancer["dancer_id"], "side": side})
        return eligible

    # -- narrowing pads ----------------------------------------------------
    def register_pad(self, pad_id: str, *, width_step: int = 1, holder: str = SHOP) -> Dict[str, Any]:
        pad_id = require_id(pad_id, INVALID_INPUT, "pad_id")
        width_step = _req_int(width_step, "width_step", minimum=1)
        holder = require_id(holder, INVALID_INPUT, "holder")
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM pads WHERE pad_id = ?", (pad_id,)).fetchone()
            if existing is not None:
                if existing["width_step"] != width_step:
                    raise BasketError(PAD_CONFLICT, "pad_id already registered with a different step", {"pad_id": pad_id})
                return dict(existing)
            now = utc_now()
            conn.execute(
                "INSERT INTO pads (pad_id, width_step, holder, physical_id, created_at, updated_at) VALUES (?, ?, ?, NULL, ?, ?)",
                (pad_id, width_step, holder, now, now),
            )
            return dict(conn.execute("SELECT * FROM pads WHERE pad_id = ?", (pad_id,)).fetchone())

    def get_pad(self, pad_id: str) -> Dict[str, Any]:
        pad_id = require_id(pad_id, INVALID_INPUT, "pad_id")
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM pads WHERE pad_id = ?", (pad_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_PAD, "unknown pad", {"pad_id": pad_id})
        return dict(row)

    def list_pads(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM pads ORDER BY pad_id").fetchall()]

    def record_pad_receipt(self, transfer_id: str, pad_id: str, from_holder: str, to_holder: str, receipt_id: str) -> Dict[str, Any]:
        """Record an external custody move (e.g. a dancer borrowing from the troupe).

        A pad currently placed in a shoe cannot move.  Used to replay the troupe's
        own receipt trail (dancer borrows / returns) independently of the shop.
        """

        transfer_id = require_id(transfer_id, INVALID_INPUT, "transfer_id")
        pad_id = require_id(pad_id, INVALID_INPUT, "pad_id")
        from_holder = require_id(from_holder, INVALID_INPUT, "from_holder")
        to_holder = require_id(to_holder, INVALID_INPUT, "to_holder")
        receipt_id = require_id(receipt_id, INVALID_INPUT, "receipt_id")
        if from_holder == to_holder:
            raise BasketError(INVALID_RECEIPT, "from_holder and to_holder must differ", {"pad_id": pad_id})
        content = {"transfer_id": transfer_id, "pad_id": pad_id, "from": from_holder, "to": to_holder, "receipt_id": receipt_id, "kind": "EXTERNAL"}
        with self.db.transaction() as conn:
            pad = conn.execute("SELECT * FROM pads WHERE pad_id = ?", (pad_id,)).fetchone()
            if pad is None:
                raise BasketError(UNKNOWN_PAD, "unknown pad", {"pad_id": pad_id})
            existing = conn.execute("SELECT content_json FROM pad_transfers WHERE transfer_id = ?", (transfer_id,)).fetchone()
            if existing is not None:
                if existing["content_json"] != canonical_json(content):
                    raise BasketError(TRANSFER_CONFLICT, "transfer_id reused with a different body", {"transfer_id": transfer_id})
                return dict(conn.execute("SELECT * FROM pad_transfers WHERE transfer_id = ?", (transfer_id,)).fetchone())
            if pad["physical_id"] is not None:
                raise BasketError(PAD_IN_USE, "pad is in a shoe; remove it first", {"pad_id": pad_id})
            if pad["holder"] != from_holder:
                raise BasketError(INVALID_RECEIPT, "pad is not held by the declared from_holder", {"pad_id": pad_id, "holder": pad["holder"], "from": from_holder})
            self._append_pad_transfer(conn, transfer_id, pad_id, "MOVE", to_holder, receipt_id, content)
            conn.execute("UPDATE pads SET holder = ?, updated_at = ? WHERE pad_id = ?", (to_holder, utc_now(), pad_id))
            return dict(conn.execute("SELECT * FROM pad_transfers WHERE transfer_id = ?", (transfer_id,)).fetchone())

    def acquire_borrowable_pad(self, transfer_id: str, pad_id: str, receipt_id: str) -> Dict[str, Any]:
        """A pad-transfer counter draws a borrowable pad from the troupe by receipt.

        Only the ``PERF_TRANSFER`` shop does this; a ``PRESTRETCH`` counter works
        from its own physical stock and refuses to handle troupe pad transfers.
        """

        transfer_id = require_id(transfer_id, INVALID_INPUT, "transfer_id")
        pad_id = require_id(pad_id, INVALID_INPUT, "pad_id")
        receipt_id = require_id(receipt_id, INVALID_INPUT, "receipt_id")
        if self.get_shop_mode() != "PERF_TRANSFER":
            raise BasketError(INVALID_SHOP_MODE, "only a pad-transfer counter may draw troupe pads", {"mode": self.get_shop_mode()})
        content = {"transfer_id": transfer_id, "pad_id": pad_id, "from": TROUPE, "to": SHOP, "receipt_id": receipt_id, "kind": "ACQUIRE"}
        with self.db.transaction() as conn:
            pad = conn.execute("SELECT * FROM pads WHERE pad_id = ?", (pad_id,)).fetchone()
            if pad is None:
                raise BasketError(UNKNOWN_PAD, "unknown pad", {"pad_id": pad_id})
            existing = conn.execute("SELECT content_json FROM pad_transfers WHERE transfer_id = ?", (transfer_id,)).fetchone()
            if existing is not None:
                if existing["content_json"] != canonical_json(content):
                    raise BasketError(TRANSFER_CONFLICT, "transfer_id reused with a different body", {"transfer_id": transfer_id})
                return dict(conn.execute("SELECT * FROM pad_transfers WHERE transfer_id = ?", (transfer_id,)).fetchone())
            if pad["physical_id"] is not None:
                raise BasketError(PAD_IN_USE, "pad is in a shoe", {"pad_id": pad_id})
            if pad["holder"] != TROUPE:
                raise BasketError(INVALID_RECEIPT, "pad is not currently borrowable from the troupe", {"pad_id": pad_id, "holder": pad["holder"]})
            self._append_pad_transfer(conn, transfer_id, pad_id, "ACQUIRE", SHOP, receipt_id, content)
            conn.execute("UPDATE pads SET holder = ?, updated_at = ? WHERE pad_id = ?", (SHOP, utc_now(), pad_id))
            return dict(conn.execute("SELECT * FROM pad_transfers WHERE transfer_id = ?", (transfer_id,)).fetchone())

    def _append_pad_transfer(self, conn, transfer_id, pad_id, direction, holder, receipt_id, content):
        seq = conn.execute("SELECT COALESCE(MAX(sequence), 0) + 1 AS n FROM pad_transfers").fetchone()["n"]
        conn.execute(
            "INSERT INTO pad_transfers (transfer_id, pad_id, direction, holder, receipt_id, sequence, content_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (transfer_id, pad_id, direction, holder, receipt_id, seq, canonical_json(content), utc_now()),
        )

    def pad_ledger(self, pad_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            if pad_id is None:
                rows = conn.execute("SELECT * FROM pad_transfers ORDER BY sequence").fetchall()
            else:
                rows = conn.execute("SELECT * FROM pad_transfers WHERE pad_id = ? ORDER BY sequence", (pad_id,)).fetchall()
        return [dict(r) for r in rows]

    def available_pads(self) -> List[str]:
        """Pads the counter may place right now: held by the shop, not in a shoe."""

        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT pad_id FROM pads WHERE holder = ? AND physical_id IS NULL ORDER BY pad_id", (SHOP,)
            ).fetchall()
        return [r["pad_id"] for r in rows]

    # -- occupancy (performance-period reservation) ------------------------
    def assign_occupancy(self, physical_id: str, run_id: str) -> Dict[str, Any]:
        physical_id = require_id(physical_id, INVALID_INPUT, "physical_id")
        run_id = require_id(run_id, INVALID_INPUT, "run_id")
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM phys_shoes WHERE physical_id = ?", (physical_id,)).fetchone() is None:
                raise BasketError(UNKNOWN_PHYSICAL, "unknown physical shoe", {"physical_id": physical_id})
            conn.execute(
                "INSERT INTO occupancies (physical_id, run_id, created_at) VALUES (?, ?, ?) ON CONFLICT(physical_id, run_id) DO NOTHING",
                (physical_id, run_id, utc_now()),
            )
            return {"physical_id": physical_id, "run_id": run_id}

    def release_occupancy(self, physical_id: str, run_id: str) -> Dict[str, Any]:
        physical_id = require_id(physical_id, INVALID_INPUT, "physical_id")
        run_id = require_id(run_id, INVALID_INPUT, "run_id")
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM occupancies WHERE physical_id = ? AND run_id = ?", (physical_id, run_id))
            return {"physical_id": physical_id, "run_id": run_id, "released": True}

    # -- appointments ------------------------------------------------------
    def create_appointment(self, appointment_id: str, needs: Any, *, run: Optional[str] = None, stretch_budget: int = 0) -> Dict[str, Any]:
        appointment_id = require_id(appointment_id, INVALID_INPUT, "appointment_id")
        if not isinstance(needs, Sequence) or isinstance(needs, (str, bytes)):
            raise BasketError(INVALID_ALLOCATION, "needs must be a list")
        canonical = sorted((_canonical_need(n, i) for i, n in enumerate(needs)), key=lambda n: n["dancer_id"])
        if len({n["dancer_id"] for n in canonical}) != len(canonical):
            raise BasketError(INVALID_ALLOCATION, "duplicate dancer in needs")
        stretch_budget = _req_int(stretch_budget, "stretch_budget", minimum=0)
        content = {"appointment_id": appointment_id, "needs": canonical, "run": run, "stretch_budget": stretch_budget}
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT content_json FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()
            if existing is not None:
                if existing["content_json"] != canonical_json(content):
                    raise BasketError(ALLOCATION_CONFLICT, "appointment_id reused with a different body", {"appointment_id": appointment_id})
            else:
                conn.execute("INSERT INTO appointments (appointment_id, content_json, created_at) VALUES (?, ?, ?)", (appointment_id, canonical_json(content), utc_now()))
        return content

    def get_appointment(self, appointment_id: str) -> Dict[str, Any]:
        appointment_id = require_id(appointment_id, INVALID_INPUT, "appointment_id")
        with self.db.connection() as conn:
            row = conn.execute("SELECT content_json FROM appointments WHERE appointment_id = ?", (appointment_id,)).fetchone()
        if row is None:
            raise BasketError(INVALID_ALLOCATION, "unknown appointment", {"appointment_id": appointment_id})
        return json_loads(row["content_json"])

    # -- allocation --------------------------------------------------------
    def allocate(
        self,
        allocation_id: str,
        needs: Any,
        *,
        run: Optional[str] = None,
        stretch_budget: int = 0,
        use_pads: bool = True,
        require_slip: bool = True,
    ) -> Dict[str, Any]:
        allocation_id = require_id(allocation_id, INVALID_INPUT, "allocation_id")
        if not isinstance(needs, Sequence) or isinstance(needs, (str, bytes)):
            raise BasketError(INVALID_ALLOCATION, "needs must be a list")
        canonical = sorted((_canonical_need(n, i) for i, n in enumerate(needs)), key=lambda n: n["dancer_id"])
        if len({n["dancer_id"] for n in canonical}) != len(canonical):
            raise BasketError(INVALID_ALLOCATION, "duplicate dancer in needs")
        stretch_budget = _req_int(stretch_budget, "stretch_budget", minimum=0)

        content = {
            "allocation_id": allocation_id,
            "needs": canonical,
            "run": run,
            "stretch_budget": stretch_budget,
            "use_pads": use_pads,
            "require_slip": require_slip,
        }
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT content_json, result_json FROM cabinet_allocations WHERE allocation_id = ?", (allocation_id,)).fetchone()
            if existing is not None:
                if existing["content_json"] != canonical_json(content):
                    raise BasketError(ALLOCATION_CONFLICT, "allocation_id reused with a different request", {"allocation_id": allocation_id})
                replay = json_loads(existing["result_json"])
                replay["repeated"] = True
                return replay

            available_pads = self.available_pads() if use_pads else []
            needs_ids = {n["dancer_id"] for n in canonical}
            phys_rows = conn.execute("SELECT * FROM phys_shoes ORDER BY physical_id").fetchall()
            slip_physicals = {r["physical_id"] for r in conn.execute("SELECT physical_id FROM fitting_slips WHERE released = 1").fetchall()}
            active_bindings = conn.execute("SELECT physical_id, dancer_id, run_id FROM delivery_bindings WHERE released = 0").fetchall()
            bound_physicals = {r["physical_id"] for r in active_bindings}
            occ_rows = conn.execute("SELECT physical_id, run_id FROM occupancies").fetchall()
            occ_by_phys: Dict[str, Set[str]] = {}
            for r in occ_rows:
                occ_by_phys.setdefault(r["physical_id"], set()).add(r["run_id"])

            shoes: List[Dict[str, Any]] = []
            excluded: List[Dict[str, Any]] = []
            for row in phys_rows:
                pid = row["physical_id"]
                reason = None
                cons = conn.execute("SELECT consented, revoked FROM phys_consignments WHERE physical_id = ?", (pid,)).fetchone()
                if not row["usable"]:
                    reason = "origin unit returned"
                elif cons is None or not cons["consented"] or cons["revoked"]:
                    reason = "no active troupe consent"
                elif require_slip and pid not in slip_physicals:
                    reason = "no released fitting slip"
                elif pid in bound_physicals:
                    reason = "currently delivered/held"
                else:
                    occupied = occ_by_phys.get(pid, set())
                    if run is not None and any(r != run for r in occupied):
                        reason = "occupied by another run"
                if reason is not None:
                    excluded.append({"physical_id": pid, "reason": reason})
                    continue
                shoes.append(
                    {
                        "physical_id": pid,
                        "side": row["side"],
                        "width": row["width"],
                        "stretch_to": row["stretch_to"],
                        "directions": json_loads(row["directions_json"]),
                        "length": row["length"],
                        "pad_step": 1,
                    }
                )

            plan = plan_pairs(shoes, canonical, pads_available=len(available_pads), stretch_budget=stretch_budget)

            # Assign concrete pad ids to the padded positions.
            pad_pool = list(available_pads)
            pad_cursor = 0
            for pair in plan["pairs"]:
                for side in ("L", "R"):
                    if side in pair and pair[side]["pad"]:
                        if pad_cursor >= len(pad_pool):
                            raise BasketError(INVALID_ALLOCATION, "planned more pads than available", {"allocation_id": allocation_id})
                        pair[side]["pad_id"] = pad_pool[pad_cursor]
                        pad_cursor += 1

            result = {
                "ok": True,
                "allocation_id": allocation_id,
                "run": run,
                "shop_mode": self.get_shop_mode(),
                "count": plan["count"],
                "pairs": plan["pairs"],
                "pads_used": plan["pads_used"],
                "stretch_used": plan["stretch_used"],
                "excluded": excluded,
                "confirmed": False,
                "repeated": False,
            }
            conn.execute(
                "INSERT INTO cabinet_allocations (allocation_id, content_json, result_json, confirmed, created_at) VALUES (?, ?, ?, 0, ?)",
                (allocation_id, canonical_json(content), canonical_json(result), utc_now()),
            )
            return result

    def get_allocation(self, allocation_id: str) -> Dict[str, Any]:
        allocation_id = require_id(allocation_id, INVALID_INPUT, "allocation_id")
        with self.db.connection() as conn:
            row = conn.execute("SELECT result_json, confirmed FROM cabinet_allocations WHERE allocation_id = ?", (allocation_id,)).fetchone()
        if row is None:
            raise BasketError(INVALID_ALLOCATION, "unknown allocation", {"allocation_id": allocation_id})
        result = json_loads(row["result_json"])
        result["confirmed"] = bool(row["confirmed"])
        return result

    # -- delivery / binding ------------------------------------------------
    def confirm_delivery(self, allocation_id: str, *, run_until: str = "run-end", tied_laces: bool = True) -> Dict[str, Any]:
        """Commit an allocation: stretch, place pads and bind pairs to dancers.

        The delivery re-checks consent and usability *inside* the transaction so a
        return or revocation that happened after planning still blocks it.  A
        binding keeps the pair with one dancer until the run ends; tied laces and
        the delivered width are persisted for later inspection.
        """

        allocation_id = require_id(allocation_id, INVALID_INPUT, "allocation_id")
        mode = self.get_shop_mode()
        now = utc_now()
        with self.db.transaction() as conn:
            alloc = conn.execute("SELECT result_json, confirmed FROM cabinet_allocations WHERE allocation_id = ?", (allocation_id,)).fetchone()
            if alloc is None:
                raise BasketError(INVALID_ALLOCATION, "unknown allocation", {"allocation_id": allocation_id})
            if alloc["confirmed"]:
                return self._delivery_view(conn, allocation_id)
            result = json_loads(alloc["result_json"])
            if result["count"] == 0:
                raise BasketError(INVALID_ALLOCATION, "nothing to deliver: allocation has no complete pairs", {"allocation_id": allocation_id})
            bindings: List[Dict[str, Any]] = []
            for pair in result["pairs"]:
                pair_id = f"{allocation_id}:{pair['dancer_id']}"
                for side in ("L", "R"):
                    if side not in pair:
                        continue
                    shoe = pair[side]
                    pid = shoe["physical_id"]
                    current = conn.execute("SELECT width, usable FROM phys_shoes WHERE physical_id = ?", (pid,)).fetchone()
                    if current is None:
                        raise BasketError(UNKNOWN_PHYSICAL, "unknown physical shoe", {"physical_id": pid})
                    if not current["usable"]:
                        raise BasketError(PHYSICAL_UNAVAILABLE, "physical became unavailable", {"physical_id": pid})
                    cons = conn.execute("SELECT consented, revoked FROM phys_consignments WHERE physical_id = ?", (pid,)).fetchone()
                    if cons is None or not cons["consented"] or cons["revoked"]:
                        raise BasketError(CONSENT_REVOKED, "consent revoked before delivery", {"physical_id": pid})
                    if conn.execute("SELECT 1 FROM delivery_bindings WHERE physical_id = ? AND released = 0", (pid,)).fetchone():
                        raise BasketError(PHYSICAL_UNAVAILABLE, "physical is already delivered", {"physical_id": pid})
                    if shoe["raw_width"] > current["width"]:
                        conn.execute("UPDATE phys_shoes SET width = ?, updated_at = ? WHERE physical_id = ?", (shoe["raw_width"], now, pid))
                        conn.execute(
                            "INSERT INTO phys_measurements (physical_id, width, stretch_to, fitter, created_at) VALUES (?, ?, ?, NULL, ?)",
                            (pid, shoe["raw_width"], shoe["raw_width"], now),
                        )
                    pad_id = shoe.get("pad_id")
                    if pad_id:
                        pad = conn.execute("SELECT * FROM pads WHERE pad_id = ?", (pad_id,)).fetchone()
                        if pad is None:
                            raise BasketError(UNKNOWN_PAD, "unknown pad", {"pad_id": pad_id})
                        if pad["holder"] != SHOP or (pad["physical_id"] is not None and pad["physical_id"] != pid):
                            raise BasketError(PAD_IN_USE, "pad not available for this shoe", {"pad_id": pad_id})
                        conn.execute("UPDATE pads SET physical_id = ?, updated_at = ? WHERE pad_id = ?", (pid, now, pad_id))
                    binding_id = f"{pair_id}:{side}"
                    conn.execute(
                        "INSERT INTO delivery_bindings (binding_id, physical_id, dancer_id, pair_id, run_id, run_until, width_delivered, pad_id, tied_laces, released, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                        (binding_id, pid, pair["dancer_id"], pair_id, result.get("run"), run_until, shoe["raw_width"], pad_id, 1 if tied_laces else 0, now, now),
                    )
                    bindings.append(
                        {
                            "binding_id": binding_id,
                            "physical_id": pid,
                            "dancer_id": pair["dancer_id"],
                            "side": side,
                            "width_delivered": shoe["raw_width"],
                            "effective_width": shoe["raw_width"] - (1 if pad_id else 0),
                            "pad_id": pad_id,
                            "tied_laces": bool(tied_laces),
                            "run_until": run_until,
                        }
                    )
            conn.execute("UPDATE cabinet_allocations SET confirmed = 1 WHERE allocation_id = ?", (allocation_id,))
        return {
            "ok": True,
            "allocation_id": allocation_id,
            "shop_mode": mode,
            "bindings": bindings,
            "pairs_delivered": len(result["pairs"]),
        }

    def _delivery_view(self, conn, allocation_id: str) -> Dict[str, Any]:
        rows = conn.execute("SELECT * FROM delivery_bindings WHERE pair_id LIKE ? ORDER BY binding_id", (f"{allocation_id}:%",)).fetchall()
        return {
            "ok": True,
            "allocation_id": allocation_id,
            "shop_mode": self.get_shop_mode(),
            "pairs_delivered": len({r["pair_id"] for r in rows}),
            "bindings": [
                {
                    "binding_id": r["binding_id"],
                    "physical_id": r["physical_id"],
                    "dancer_id": r["dancer_id"],
                    "run_until": r["run_until"],
                    "width_delivered": r["width_delivered"],
                    "pad_id": r["pad_id"],
                    "tied_laces": bool(r["tied_laces"]),
                }
                for r in rows
            ],
            "repeated": True,
        }

    def return_delivery(self, binding_id: str, *, on_time: bool = True, receipt_id: Optional[str] = None) -> Dict[str, Any]:
        """Return a single binding (on time or late); frees the shoe and its pad."""

        binding_id = require_id(binding_id, INVALID_INPUT, "binding_id")
        now = utc_now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM delivery_bindings WHERE binding_id = ?", (binding_id,)).fetchone()
            if row is None:
                raise BasketError(INVALID_ALLOCATION, "unknown binding", {"binding_id": binding_id})
            if row["released"]:
                result = {"ok": True, "binding_id": binding_id, "physical_id": row["physical_id"], "on_time": bool(on_time), "late": not on_time, "repeated": True}
                return result
            conn.execute("UPDATE delivery_bindings SET released = 1, updated_at = ? WHERE binding_id = ?", (now, binding_id))
            if row["pad_id"]:
                conn.execute("UPDATE pads SET physical_id = NULL, updated_at = ? WHERE pad_id = ?", (now, row["pad_id"]))
            result = {
                "ok": True,
                "binding_id": binding_id,
                "physical_id": row["physical_id"],
                "dancer_id": row["dancer_id"],
                "on_time": bool(on_time),
                "late": not on_time,
                "receipt_id": receipt_id,
                "released_at": now,
                "shop_mode": self.get_shop_mode(),
                "repeated": False,
            }
            conn.execute(
                "INSERT OR REPLACE INTO return_confirmations (confirmation_id, return_request_id, sale_id, content_json, result_json, created_at)"
                " VALUES (?, '', '', ?, ?, ?)",
                (f"delivery-return:{binding_id}", canonical_json({"binding_id": binding_id, "on_time": bool(on_time)}), canonical_json(result), now),
            )
            return result

    def handover_return(self, show_id: str, *, on_time: bool = True, receipt_id: Optional[str] = None) -> Dict[str, Any]:
        """Per-show handover (PERF_TRANSFER): release bindings and return pads."""

        show_id = require_id(show_id, INVALID_INPUT, "show_id")
        now = utc_now()
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT * FROM delivery_bindings WHERE released = 0 ORDER BY binding_id").fetchall()
            released = []
            for row in rows:
                conn.execute("UPDATE delivery_bindings SET released = 1, updated_at = ? WHERE binding_id = ?", (now, row["binding_id"]))
                if row["pad_id"]:
                    conn.execute("UPDATE pads SET physical_id = NULL, updated_at = ? WHERE pad_id = ?", (now, row["pad_id"]))
                released.append({"binding_id": row["binding_id"], "physical_id": row["physical_id"], "dancer_id": row["dancer_id"]})
            receipt = {
                "ok": True,
                "show_id": show_id,
                "on_time": bool(on_time),
                "late": not on_time,
                "receipt_id": receipt_id,
                "released": released,
                "created_at": now,
            }
            conn.execute(
                "INSERT OR REPLACE INTO return_confirmations (confirmation_id, return_request_id, sale_id, content_json, result_json, created_at)"
                " VALUES (?, '', '', ?, ?, ?)",
                (f"handover:{show_id}", canonical_json({"show_id": show_id, "on_time": bool(on_time)}), canonical_json(receipt), now),
            )
            return receipt

    def list_bindings(self, *, active_only: bool = False) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            query = "SELECT * FROM delivery_bindings"
            if active_only:
                query += " WHERE released = 0"
            query += " ORDER BY binding_id"
            return [dict(r) for r in conn.execute(query).fetchall()]

    # -- after-sales return confirmation -----------------------------------
    def confirm_return(self, confirmation_id: str, return_request_id: str, sale_id: str) -> Dict[str, Any]:
        """Bind an after-sales return to physical shoes and stop new issuance.

        Requires the ledger's return request to exist.  Every physical recovered
        from a returned unit becomes unusable and its consent is revoked, so no
        *new* allocation or delivery can use those shoes.
        """

        confirmation_id = require_id(confirmation_id, INVALID_INPUT, "confirmation_id")
        return_request_id = require_id(return_request_id, INVALID_INPUT, "return_request_id")
        sale_id = require_id(sale_id, INVALID_INPUT, "sale_id")
        now = utc_now()
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM return_confirmations WHERE confirmation_id = ?", (confirmation_id,)).fetchone()
            if existing is not None:
                if existing["return_request_id"] != return_request_id or existing["sale_id"] != sale_id:
                    raise BasketError(RETURN_CONFIRMATION_CONFLICT, "confirmation_id reused with a different body", {"confirmation_id": confirmation_id})
                replay = json_loads(existing["result_json"])
                replay["repeated"] = True
                return replay
            ret = conn.execute("SELECT sale_id FROM return_requests WHERE request_id = ?", (return_request_id,)).fetchone()
            if ret is None:
                raise BasketError(INVALID_RECEIPT, "unknown after-sales return request", {"return_request_id": return_request_id})
            if ret["sale_id"] != sale_id:
                raise BasketError(INVALID_RECEIPT, "return request belongs to a different sale", {"return_request_id": return_request_id})
            returned_units = [r["unit_id"] for r in conn.execute("SELECT unit_id FROM return_items WHERE request_id = ? ORDER BY unit_id", (return_request_id,)).fetchall()]
            affected = []
            for unit_id in returned_units:
                for phys in conn.execute("SELECT physical_id FROM phys_shoes WHERE sale_id = ? AND unit_id = ? AND usable = 1 ORDER BY physical_id", (sale_id, unit_id)).fetchall():
                    conn.execute("UPDATE phys_shoes SET usable = 0, updated_at = ? WHERE physical_id = ?", (now, phys["physical_id"]))
                    conn.execute("UPDATE phys_consignments SET revoked = 1, updated_at = ? WHERE physical_id = ?", (now, phys["physical_id"]))
                    affected.append({"physical_id": phys["physical_id"], "unit_id": unit_id})
            result = {
                "ok": True,
                "confirmation_id": confirmation_id,
                "return_request_id": return_request_id,
                "sale_id": sale_id,
                "returned_units": returned_units,
                "affected_physicals": affected,
                "repeated": False,
            }
            conn.execute(
                "INSERT INTO return_confirmations (confirmation_id, return_request_id, sale_id, content_json, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (confirmation_id, return_request_id, sale_id, canonical_json({"return_request_id": return_request_id, "sale_id": sale_id}), canonical_json(result), now),
            )
            return result

    # -- inspection --------------------------------------------------------
    def cabinet_view(self) -> Dict[str, Any]:
        """Read-only cabinet state, safe to call after reopening the database."""

        with self.db.connection() as conn:
            physicals = [self._physical_view(conn, r["physical_id"]) for r in conn.execute("SELECT physical_id FROM phys_shoes ORDER BY physical_id").fetchall()]
            slips = [self._slip_view(conn, r["slip_id"]) for r in conn.execute("SELECT slip_id FROM fitting_slips ORDER BY slip_id").fetchall()]
            pads = [dict(r) for r in conn.execute("SELECT * FROM pads ORDER BY pad_id").fetchall()]
            bindings = [dict(r) for r in conn.execute("SELECT * FROM delivery_bindings ORDER BY binding_id").fetchall()]
            measurements = [dict(r) for r in conn.execute("SELECT physical_id, width, stretch_to, created_at FROM phys_measurements ORDER BY physical_id, seq").fetchall()]
            occupancies = [dict(r) for r in conn.execute("SELECT physical_id, run_id FROM occupancies ORDER BY physical_id, run_id").fetchall()]
        return {
            "shop_mode": self.get_shop_mode(),
            "physicals": physicals,
            "slips": slips,
            "pads": pads,
            "bindings": bindings,
            "measurements": measurements,
            "occupancies": occupancies,
        }


__all__ = ["TroupeService", "plan_pairs", "shoe_options", "SHOP_MODES", "SHOP", "TROUPE"]
