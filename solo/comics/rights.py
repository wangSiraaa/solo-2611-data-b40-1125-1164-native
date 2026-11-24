"""Printing permits and the layout worker's order book.

A :class:`PrintPermit` is what the layout worker brings to the proofing
desk alongside a manuscript version.  Two crafts are licensed, matching the
task statement:

* ``margin``  -- keep the artwork and every bubble in place; the permitted
  margin slots may carry reading numbers/guides.  A margin slot is a narrow
  strip that geometrically reaches only the bubbles listed in ``reaches``.
  A strip already sold to another page of the same volume cannot be used
  here, and a sale cannot be reclaimed once that other page has been
  proofed against it.

* ``relayout`` -- bubbles inside panels listed in ``redraw_panels`` may be
  moved and their tails reconnected to the original speaker, so the
  dialogue flows in natural reading order.  A panel whose artwork the
  painter has already signed off (``sealed_panels``) is frozen in place and
  loses its redraw licence.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["MarginSlot", "PrintPermit", "OrderBook", "CRAFT_MARGIN", "CRAFT_RELAYOUT"]

CRAFT_MARGIN = "margin"
CRAFT_RELAYOUT = "relayout"
CRAFTS = (CRAFT_MARGIN, CRAFT_RELAYOUT)


@dataclass(frozen=True)
class MarginSlot:
    """A narrow margin strip that can carry a guide to some bubbles.

    ``reaches`` is the exact set of bubbles a mark printed in this strip can
    point the reader to; it is deliberately narrow so a strip sold to
    another page cannot silently guide this page's dialogue.
    """

    slot_id: str
    page_id: str
    side: str  # "left" | "right" | "top" | "bottom"
    reaches: frozenset[str]
    position: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class PrintPermit:
    permit_id: str
    page_id: str
    craft: str
    # Per-bubble permission to print that bubble's dialogue.
    dialogue: frozenset[str]
    margin_slots: tuple[MarginSlot, ...] = ()
    redraw_panels: frozenset[str] = frozenset()
    sealed_panels: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.craft not in CRAFTS:
            raise ValueError(f"unknown craft {self.craft!r}; expected one of {CRAFTS}")
        if self.craft == CRAFT_RELAYOUT and self.margin_slots:
            raise ValueError("a relayout permit does not license margin guides")
        if self.craft == CRAFT_MARGIN and (self.redraw_panels or self.sealed_panels):
            raise ValueError("a margin permit does not license panel redraw")
        for slot in self.margin_slots:
            if slot.page_id != self.page_id:
                raise ValueError(f"margin slot {slot.slot_id} belongs to page {slot.page_id}")

    def can_print(self, bubble_id: str) -> bool:
        return bubble_id in self.dialogue

    def slot(self, slot_id: str) -> MarginSlot | None:
        for slot in self.margin_slots:
            if slot.slot_id == slot_id:
                return slot
        return None


@dataclass
class _StripSale:
    slot_id: str
    owner_page: str
    buyer_page: str


@dataclass
class _PanelSeal:
    page_id: str
    panel_id: str


class OrderBook:
    """Committed, non-reclaimable rights for one print order (same volume).

    Everything recorded here has already been handed to the printer, so the
    desk must be able to prove, on re-check, that it cannot take a committed
    right back.
    """

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        self._strip_sales: list[_StripSale] = []
        self._seals: list[_PanelSeal] = []
        self._consumed_redraw: set[tuple[str, str]] = set()
        self._proofed_pages: set[str] = set()

    # -- margin strips ----------------------------------------------------

    def sell_strip(self, slot: MarginSlot, buyer_page: str) -> None:
        """Sell a strip of ``slot.page_id`` to another page of this volume."""
        if buyer_page == slot.page_id:
            raise ValueError("a page cannot buy its own margin strip")
        if self.strip_buyer(slot.slot_id) is not None:
            raise ValueError(f"strip {slot.slot_id} is already sold")
        self._strip_sales.append(_StripSale(slot.slot_id, slot.page_id, buyer_page))

    def strip_buyer(self, slot_id: str) -> str | None:
        for sale in self._strip_sales:
            if sale.slot_id == slot_id:
                return sale.buyer_page
        return None

    def strip_is_available(self, slot: MarginSlot, page_id: str) -> bool:
        """A strip is usable by ``page_id`` only while nobody else owns it."""
        buyer = self.strip_buyer(slot.slot_id)
        return buyer is None or buyer == page_id

    def reclaim_strip(self, slot_id: str) -> bool:
        """Committed sales are permanent; this always fails."""
        return False

    def strip_sales(self) -> tuple[_StripSale, ...]:
        return tuple(self._strip_sales)

    # -- redraw / seals ---------------------------------------------------

    def sign_panel(self, page_id: str, panel_id: str) -> None:
        """Painter sign-off: the panel's artwork is frozen at its position."""
        seal = _PanelSeal(page_id, panel_id)
        if seal not in self._seals:
            self._seals.append(seal)

    def is_sealed(self, page_id: str, panel_id: str) -> bool:
        return any(s.page_id == page_id and s.panel_id == panel_id for s in self._seals)

    def seals(self) -> tuple[tuple[str, str], ...]:
        return tuple((s.page_id, s.panel_id) for s in self._seals)

    def consume_redraw(self, page_id: str, panel_id: str) -> None:
        self._consumed_redraw.add((page_id, panel_id))

    def redraw_used(self, page_id: str, panel_id: str) -> bool:
        return (page_id, panel_id) in self._consumed_redraw

    # -- proofed pages ----------------------------------------------------

    def mark_proofed(self, page_id: str) -> None:
        self._proofed_pages.add(page_id)

    def was_proofed(self, page_id: str) -> bool:
        return page_id in self._proofed_pages

    def proofed_pages(self) -> frozenset[str]:
        return frozenset(self._proofed_pages)
