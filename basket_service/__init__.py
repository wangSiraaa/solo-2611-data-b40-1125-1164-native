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
from .quote_service import QuoteService
from .troupe import TroupeService


class Service:
    """Thin façade wiring the quote service, ledger and troupe cabinet onto one database."""

    def __init__(self, path: str = ":memory:", *, busy_timeout_ms: int = 30000):
        self.db = Database(path, busy_timeout_ms=busy_timeout_ms)
        self.quotes = QuoteService(self.db)
        self.ledger = AfterSalesLedger(self.db)
        self.troupe = TroupeService(self.db)

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

    # troupe fitting / cabinet / paired delivery --------------------------
    def set_shop_mode(self, mode):
        return self.troupe.set_shop_mode(mode)

    def get_shop_mode(self):
        return self.troupe.get_shop_mode()

    def register_physical(self, physical_id, side, **kwargs):
        return self.troupe.register_physical(physical_id, side, **kwargs)

    def recover_from_sale(self, physical_id, side, sale_id, unit_id, **kwargs):
        return self.troupe.recover_from_sale(physical_id, side, sale_id, unit_id, **kwargs)

    def get_physical(self, physical_id):
        return self.troupe.get_physical(physical_id)

    def list_physicals(self):
        return self.troupe.list_physicals()

    def stretch(self, physical_id, to_width, **kwargs):
        return self.troupe.stretch(physical_id, to_width, **kwargs)

    def update_fitting(self, physical_id, **kwargs):
        return self.troupe.update_fitting(physical_id, **kwargs)

    def consign(self, physical_id, dancer_id, receipt_id):
        return self.troupe.consign(physical_id, dancer_id, receipt_id)

    def revoke_consignment(self, physical_id):
        return self.troupe.revoke_consignment(physical_id)

    def register_dancer(self, dancer_id, **kwargs):
        return self.troupe.register_dancer(dancer_id, **kwargs)

    def get_dancer(self, dancer_id):
        return self.troupe.get_dancer(dancer_id)

    def list_dancers(self):
        return self.troupe.list_dancers()

    def publish_fitting_slip(self, slip_id, physical_id):
        return self.troupe.publish_fitting_slip(slip_id, physical_id)

    def retract_fitting_slip(self, slip_id):
        return self.troupe.retract_fitting_slip(slip_id)

    def get_fitting_slip(self, slip_id):
        return self.troupe.get_fitting_slip(slip_id)

    def list_fitting_slips(self):
        return self.troupe.list_fitting_slips()

    def register_pad(self, pad_id, **kwargs):
        return self.troupe.register_pad(pad_id, **kwargs)

    def get_pad(self, pad_id):
        return self.troupe.get_pad(pad_id)

    def list_pads(self):
        return self.troupe.list_pads()

    def record_pad_receipt(self, transfer_id, pad_id, from_holder, to_holder, receipt_id):
        return self.troupe.record_pad_receipt(transfer_id, pad_id, from_holder, to_holder, receipt_id)

    def acquire_borrowable_pad(self, transfer_id, pad_id, receipt_id):
        return self.troupe.acquire_borrowable_pad(transfer_id, pad_id, receipt_id)

    def pad_ledger(self, pad_id=None):
        return self.troupe.pad_ledger(pad_id)

    def available_pads(self):
        return self.troupe.available_pads()

    def assign_occupancy(self, physical_id, run_id):
        return self.troupe.assign_occupancy(physical_id, run_id)

    def release_occupancy(self, physical_id, run_id):
        return self.troupe.release_occupancy(physical_id, run_id)

    def create_appointment(self, appointment_id, needs, **kwargs):
        return self.troupe.create_appointment(appointment_id, needs, **kwargs)

    def get_appointment(self, appointment_id):
        return self.troupe.get_appointment(appointment_id)

    def allocate(self, allocation_id, needs, **kwargs):
        return self.troupe.allocate(allocation_id, needs, **kwargs)

    def get_allocation(self, allocation_id):
        return self.troupe.get_allocation(allocation_id)

    def confirm_delivery(self, allocation_id, **kwargs):
        return self.troupe.confirm_delivery(allocation_id, **kwargs)

    def return_delivery(self, binding_id, **kwargs):
        return self.troupe.return_delivery(binding_id, **kwargs)

    def handover_return(self, show_id, **kwargs):
        return self.troupe.handover_return(show_id, **kwargs)

    def list_bindings(self, **kwargs):
        return self.troupe.list_bindings(**kwargs)

    def confirm_return(self, confirmation_id, return_request_id, sale_id):
        return self.troupe.confirm_return(confirmation_id, return_request_id, sale_id)

    def cabinet_view(self):
        return self.troupe.cabinet_view()

    # cross-cutting -------------------------------------------------------
    def run(self, plan):
        """Execute a JSON plan; see ``basket_service.planner``."""

        from .planner import run_plan

        return run_plan(self, plan)


def open_service(path: str = ":memory:", *, busy_timeout_ms: int = 30000) -> Service:
    return Service(path, busy_timeout_ms=busy_timeout_ms)


__all__ = ["Service", "open_service", "BasketError", "Database"]
