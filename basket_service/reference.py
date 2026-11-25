"""Deterministic external reference implementations used by the self checks.

These are deliberately independent from the production optimizer and ledger:

* ``exhaustive_best`` enumerates every assignment (recursively over coupons in
  coupon-id order and every legal unit subset) and retains the minimal total
  with the contract's two-level tie-break; it is feasible for <= 10 units and
  8 coupons.
* ``expected_allocation`` recomputes the proportional split from scratch with
  Python's ``fractions.Fraction`` instead of the service's integer arithmetic.
* ``expected_return`` derives a refund from the original paid amount only.

They are exported from the package so graders and ``--self-check`` can import
them; the production path never calls them.
"""

from __future__ import annotations

import itertools
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Sequence, Tuple


def exhaustive_best(
    units: Sequence[Mapping[str, Any]], coupons: Sequence[Mapping[str, Any]]
) -> Tuple[int, Tuple[str, ...], Tuple[Tuple[str, ...], ...], Dict[str, List[str]]]:
    """Brute-force best quote and its tie-break signature.

    Returns ``(total, used_coupon_ids, unit_lists, mapping)`` where the
    signature is ``(sorted coupon ids, tuple of sorted unit-id lists in coupon
    order)``.  Enumeration order follows coupon_id order and unit_id-ordered
    combinations, so the first optimum encountered is the required one.
    """

    ordered_units = sorted(units, key=lambda u: u["unit_id"])
    ordered_coupons = sorted(coupons, key=lambda c: c["coupon_id"])
    by_sku: Dict[str, List[str]] = {}
    price = {}
    sku_of = {}
    for unit in ordered_units:
        by_sku.setdefault(unit["sku"], []).append(unit["unit_id"])
        price[unit["unit_id"]] = unit["price"]
        sku_of[unit["unit_id"]] = unit["sku"]
    for ids in by_sku.values():
        ids.sort()
    base = sum(price.values())

    candidates: List[List[Tuple[str, ...]]] = []
    for coupon in ordered_coupons:
        skus = sorted(coupon["requires"])
        groups = [list(itertools.combinations(by_sku[sku], coupon["requires"][sku])) for sku in skus]
        options: List[Tuple[str, ...]] = []
        for combo in itertools.product(*groups):
            ids = tuple(sorted(itertools.chain.from_iterable(combo)))
            if len(set(ids)) != len(ids):
                continue
            if sum(price[i] for i in ids) < coupon["D"]:
                continue
            options.append(ids)
        options.sort()
        candidates.append(options)

    best_value = [None, None]  # (total, signature)

    def walk(position: int, used: frozenset, chosen: Tuple[Tuple[str, ...], ...], discount: int):
        if position == len(ordered_coupons):
            total = base - discount
            used_ids = tuple(ordered_coupons[i]["coupon_id"] for i in range(len(ordered_coupons)) if chosen[i])
            signature = (used_ids, tuple(chosen[i] for i in range(len(ordered_coupons)) if chosen[i]))
            if best_value[0] is None or total < best_value[0] or (total == best_value[0] and signature < best_value[1]):
                best_value[0] = total
                best_value[1] = signature
            return
        # option: skip
        walk(position + 1, used, chosen + ((),), discount)
        for option in candidates[position]:
            if any(unit in used for unit in option):
                continue
            walk(position + 1, used | set(option), chosen + (option,), discount + ordered_coupons[position]["D"])

    walk(0, frozenset(), (), 0)
    assert best_value[0] is not None
    total, signature = best_value
    used_ids, unit_lists = signature
    mapping = {coupon_id: list(ids) for coupon_id, ids in zip(used_ids, unit_lists)}
    return total, used_ids, unit_lists, mapping


def expected_allocation(
    units: Sequence[Mapping[str, Any]], mapping: Mapping[str, Sequence[str]], discounts: Mapping[str, int]
) -> List[Dict[str, Any]]:
    """Fraction-based proportional allocation, independent of the service code."""

    price = {u["unit_id"]: u["price"] for u in units}
    sku = {u["unit_id"]: u["sku"] for u in units}
    owner: Dict[str, str] = {}
    exact: Dict[str, Fraction] = {}
    discount_by_unit: Dict[str, int] = {}
    for coupon_id, ids in mapping.items():
        total_price = sum(price[i] for i in ids)
        for unit_id in ids:
            owner[unit_id] = coupon_id
            exact[unit_id] = Fraction(discounts[coupon_id] * price[unit_id], total_price)
        # Allocate this coupon's leftover cents independently; leftovers never
        # cross coupon boundaries.
        coupon_leftover = discounts[coupon_id] - sum(int(exact[i]) for i in ids)
        ranked = sorted(ids, key=lambda unit_id: (-(exact[unit_id] - int(exact[unit_id])), unit_id))
        for unit_id in ids:
            discount_by_unit[unit_id] = int(exact[unit_id])
        for unit_id in ranked[:coupon_leftover]:
            discount_by_unit[unit_id] += 1
    report = []
    for unit_id in sorted(price):
        discount = discount_by_unit.get(unit_id, 0)
        report.append(
            {
                "unit_id": unit_id,
                "sku": sku[unit_id],
                "price": price[unit_id],
                "coupon_id": owner.get(unit_id),
                "discount": discount,
                "paid": price[unit_id] - discount,
            }
        )
    return report


def expected_return(paid_by_unit: Mapping[str, int], unit_ids: Sequence[str]) -> Tuple[int, Dict[str, int]]:
    """Refunds follow the original per-unit paid amount, never a new quote."""

    per_unit = {unit_id: paid_by_unit[unit_id] for unit_id in unit_ids}
    return sum(per_unit.values()), per_unit


# ---------------------------------------------------------------------------
# Nib geometry reference (independent of basket_service.nib).
# ---------------------------------------------------------------------------
def nib_shape(kind: str, base: int, hand: str, side_wide: str, grinds: int) -> Dict[str, int]:
    """Idealised width pair after ``grinds``, straight from the workshop rules.

    Straight: same width in both directions.  Side: wide direction is one more,
    narrow direction one less; a different hand swaps the two.  Grinding once
    removes one from the base width, which raises ``ValueError`` once the nib
    can no longer exist (straight base < 1, side base < 2).
    """

    effective = base - grinds
    if kind == "STRAIGHT":
        if effective < 1:
            raise ValueError("straight nib ground away")
        return {"H": effective, "V": effective}
    if effective < 2:
        raise ValueError("side nib ground away")
    other = "V" if side_wide == "H" else "H"
    wide = side_wide if hand == "RIGHT" else other
    narrow = other if wide == side_wide else side_wide
    return {wide: effective + 1, narrow: effective - 1}


def nib_best_kind_for_pairs(observations: Sequence[Tuple[str, str, int, int]], base_candidates):
    """Brute-force the hypotheses that explain every (hand, dir, width) reading.

    ``base_candidates`` is a list of ``(kind, base, side_wide)`` tuples.  This
    walks every admissible grind level independently, so it never shares the
    production helper's interval arithmetic.
    """

    survivors = []
    for kind, base, side_wide in base_candidates:
        ok = True
        for hand, direction, width, grinds in observations:
            try:
                shape = nib_shape(kind, base, hand, side_wide, grinds)
            except ValueError:
                ok = False
                break
            if shape[direction] != width:
                ok = False
                break
        if ok:
            survivors.append({"kind": kind, "base": base, "side_wide": None if kind == "STRAIGHT" else side_wide})
    return survivors

