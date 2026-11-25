"""Independent basket pricing and after-sales ledger service.

Quick start::

    from basket_service import open_service

    svc = open_service("data/shop.db")
    quote = svc.quote(basket, coupons, request_id="q1", snapshot_id=snap)
    sale = svc.register_sale("S1", quote=quote)
    result = svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .db import Database
from .errors import BasketError
from .ledger import AfterSalesLedger
from .optics import OpticsService
from .quote_service import QuoteService


class Service:
    """Thin façade wiring the quote service, ledger and optics onto one database."""

    def __init__(self, path: str = ":memory:", *, busy_timeout_ms: int = 30000):
        self.db = Database(path, busy_timeout_ms=busy_timeout_ms)
        self.quotes = QuoteService(self.db)
        self.ledger = AfterSalesLedger(self.db)
        self.optics = OpticsService(self.db)

    # quote surface -------------------------------------------------------
    def publish_snapshot(self, products=None, coupons=None, *, snapshot_id=None) -> str:
        return self.quotes.publish_snapshot(products, coupons, snapshot_id=snapshot_id)

    def get_snapshot(self, snapshot_id: str):
        return self.quotes.get_snapshot(snapshot_id)

    def quote(self, basket, coupons, request_id: Optional[str] = None, snapshot_id: Optional[str] = None):
        return self.quotes.quote(basket, coupons, request_id, snapshot_id)

    def get_quote(self, request_id: str):
        return self.quotes.get_quote(request_id)

    # ledger surface ------------------------------------------------------
    def register_sale(self, sale_id: str, **kwargs):
        return self.ledger.register_sale(sale_id, **kwargs)

    def get_sale(self, sale_id: str):
        return self.ledger.get_sale(sale_id)

    def return_units(self, request_id: str, sale_id: str, unit_ids, grade):
        return self.ledger.return_units(request_id, sale_id, unit_ids, grade)

    def get_return(self, request_id: str):
        return self.ledger.get_return(request_id)

    def adjust_inventory(self, event_id: str, sku: str, quantity: int, bucket: str):
        return self.ledger.adjust_inventory(event_id, sku, quantity, bucket)

    def inventory(self, skus=None):
        return self.ledger.inventory(skus)

    def audit_inventory(self):
        return self.ledger.audit_inventory()

    def audit_sales(self):
        return self.ledger.audit_sales()

    # optics surface ------------------------------------------------------
    def add_telescope(self, *args, **kwargs):
        return self.optics.add_telescope(*args, **kwargs)

    def get_telescope(self, telescope_id):
        return self.optics.get_telescope(telescope_id)

    def add_batch(self, *args, **kwargs):
        return self.optics.add_batch(*args, **kwargs)

    def calibrate_batch(self, *args, **kwargs):
        return self.optics.calibrate_batch(*args, **kwargs)

    def get_batch(self, batch_id):
        return self.optics.get_batch(batch_id)

    def register_piece(self, *args, **kwargs):
        return self.optics.register_piece(*args, **kwargs)

    def get_piece(self, piece_id):
        return self.optics.get_piece(piece_id)

    def piece_path_interval(self, piece_id):
        return self.optics.piece_path_interval(piece_id)

    def update_piece_evidence(self, *args, **kwargs):
        return self.optics.update_piece_evidence(*args, **kwargs)

    def assess_combo(self, *args, **kwargs):
        return self.optics.assess_combo(*args, **kwargs)

    def lock_group(self, *args, **kwargs):
        return self.optics.lock_group(*args, **kwargs)

    def verify_group(self, *args, **kwargs):
        return self.optics.verify_group(*args, **kwargs)

    def unseal_group(self, group_id):
        return self.optics.unseal_group(group_id)

    def get_group(self, group_id):
        return self.optics.get_group(group_id)

    def add_session(self, *args, **kwargs):
        return self.optics.add_session(*args, **kwargs)

    def get_session(self, session_id):
        return self.optics.get_session(session_id)

    def place_order(self, *args, **kwargs):
        return self.optics.place_order(*args, **kwargs)

    def deliver_order(self, order_id):
        return self.optics.deliver_order(order_id)

    def fulfil_session(self, session_id):
        return self.optics.fulfil_session(session_id)

    def cancel_order(self, order_id, reason=""):
        return self.optics.cancel_order(order_id, reason)

    def switch_telescope(self, order_id, new_telescope_id):
        return self.optics.switch_telescope(order_id, new_telescope_id)

    def get_order(self, order_id):
        return self.optics.get_order(order_id)

    def get_delivery(self, order_id):
        return self.optics.get_delivery(order_id)

    def return_pieces(self, order_id):
        return self.optics.return_pieces(order_id)

    def return_piece(self, piece_id):
        return self.optics.return_piece(piece_id)

    def list_orders(self):
        return self.optics.list_orders()

    def list_bookings(self):
        return self.optics.list_bookings()

    # cross-cutting -------------------------------------------------------
    def run(self, plan):
        """Execute a JSON plan; see ``basket_service.planner``."""

        from .planner import run_plan

        return run_plan(self, plan)


def open_service(path: str = ":memory:", *, busy_timeout_ms: int = 30000) -> Service:
    return Service(path, busy_timeout_ms=busy_timeout_ms)


__all__ = ["Service", "open_service", "BasketError", "Database"]
