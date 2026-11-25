"""Typed errors for the basket quote and after-sales ledger service."""

from __future__ import annotations

from typing import Any, Dict, Optional


class BasketError(Exception):
    """A deterministic, machine readable failure.

    Every public operation either commits a complete result or raises one of
    these before any partially visible state is written.
    """

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message, "details": self.details}}

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        return f"BasketError({self.code!r}, {self.message!r})"


# Input / contract failures -------------------------------------------------
INVALID_BASKET = "INVALID_BASKET"
INVALID_COUPON = "INVALID_COUPON"
INVALID_INPUT = "INVALID_INPUT"
INVALID_QUOTE = "INVALID_QUOTE"
INVALID_GRADE = "INVALID_GRADE"

# Quote service failures ----------------------------------------------------
UNKNOWN_SNAPSHOT = "UNKNOWN_SNAPSHOT"
SNAPSHOT_CONFLICT = "SNAPSHOT_CONFLICT"
REQUEST_CONFLICT = "REQUEST_CONFLICT"
QUOTE_CONFLICT = "QUOTE_CONFLICT"

# Ledger failures -----------------------------------------------------------
UNKNOWN_QUOTE = "UNKNOWN_QUOTE"
UNKNOWN_SALE = "UNKNOWN_SALE"
SALE_CONFLICT = "SALE_CONFLICT"
QUOTE_MISMATCH = "QUOTE_MISMATCH"
UNIT_INELIGIBLE = "UNIT_INELIGIBLE"
UNIT_ALREADY_RETURNED = "UNIT_ALREADY_RETURNED"
OVERLAPPING_RETURN = "OVERLAPPING_RETURN"
INVENTORY_CONFLICT = "INVENTORY_CONFLICT"

# Optics handover failures ---------------------------------------------------
INVALID_OPTIC = "INVALID_OPTIC"
OPTIC_LIMIT = "OPTIC_LIMIT"
UNKNOWN_PIECE = "UNKNOWN_PIECE"
UNKNOWN_TELESCOPE = "UNKNOWN_TELESCOPE"
UNKNOWN_SESSION = "UNKNOWN_SESSION"
UNKNOWN_GROUP = "UNKNOWN_GROUP"
UNKNOWN_BATCH = "UNKNOWN_BATCH"
UNKNOWN_COMBO = "UNKNOWN_COMBO"
UNKNOWN_ORDER = "UNKNOWN_ORDER"
UNKNOWN_DELIVERY = "UNKNOWN_DELIVERY"
PIECE_UNAVAILABLE = "PIECE_UNAVAILABLE"
PIECE_IMPERSONATION = "PIECE_IMPERSONATION"
GROUP_SEALED = "GROUP_SEALED"
GROUP_NOT_VERIFIED = "GROUP_NOT_VERIFIED"
CALIBRATION_CONFLICT = "CALIBRATION_CONFLICT"
CALIBRATION_INCONSISTENT = "CALIBRATION_INCONSISTENT"
UNKNOWN_CALIBRATION = "UNKNOWN_CALIBRATION"
INCOMPATIBLE_CHAIN = "INCOMPATIBLE_CHAIN"
FOCUS_UNREACHABLE = "FOCUS_UNREACHABLE"
PIECE_BOOKED = "PIECE_BOOKED"
SCOPE_BOOKED = "SCOPE_BOOKED"
ORDER_CONFLICT = "ORDER_CONFLICT"
ORDER_STATE = "ORDER_STATE"
DELIVERY_CONFLICT = "DELIVERY_CONFLICT"
