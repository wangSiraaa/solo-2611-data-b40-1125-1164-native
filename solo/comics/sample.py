"""Deterministic sample manuscripts and permits for the comic proofing demo.

The canonical sheet is one row of three panels/bubbles: A left, B middle,
C right as an off-page narrator.  The closing ``REPLY`` line holds the only
editorial variable: ``A>B`` (B answers A, read A first) becomes ``A<B``
(A answers B, so the page must read B, A, C).

Bubble A's line deliberately contains PAIR-mode characters, so the sheet
exercises the SOLO codec end to end: it is stored as real SOLO bytes,
patched through :class:`RevisionLibrary`, and located again through
``Version.decode`` ranges.
"""

from __future__ import annotations

from .rights import CRAFT_MARGIN, CRAFT_RELAYOUT, MarginSlot, PrintPermit

__all__ = [
    "PAGE_ID",
    "canonical_manuscript_text",
    "self_loop_manuscript_text",
    "swap_relation",
    "build_canonical_permit",
    "margin_permit",
    "relayout_permit",
    "canonical_margin_slots",
    "version_without_b_print",
]

PAGE_ID = "p1"
_OTHER_PAGE = "p2"

#: The PAIR character in A's line: U+4E2A (个), n = 0x002A.
PAIR_WORD = chr(0x4E2A) + chr(0x4EBA)  # 个人


def canonical_manuscript_text(relation: str = "A>B") -> str:
    if relation not in ("A>B", "A<B"):
        raise ValueError("relation must be 'A>B' or 'A<B'")
    return "\n".join(
        [
            "PAGE p1",
            "# one row, three panels: A left, B middle, C off-page right",
            "PANEL p1a 0 0 20 20",
            "PANEL p1b 22 0 42 20",
            "PANEL p1c 44 0 64 20",
            "ACTOR alice @ 2 18 PANEL p1a",
            "ACTOR bob @ 24 18 PANEL p1b",
            "BUBBLE A PANEL p1a 2 2 16 10",
            "BUBBLE B PANEL p1b 24 2 38 10",
            "BUBBLE C PANEL p1c 46 2 60 10 OFFPAGE",
            f"SAY A ni shi {PAIR_WORD} hao ren",
            "SAY B wo zhi dao",
            "SAY C (narration) jiu zhe yang",
            "TAIL A -> 2 18",
            "TAIL B -> 24 18",
            "TAIL C -> 70 18",
            f"REPLY {relation}",
            "",
        ]
    )


def self_loop_manuscript_text() -> str:
    """The legal canonical sheet with B made to answer itself."""
    text = canonical_manuscript_text("A>B")
    return text.replace("REPLY A>B", "REPLY B>B")


def second_page_manuscript_text() -> str:
    """A sound companion page (p2) of the same volume.

    It carries its own two bubbles so an order book can prove p2 before the
    margin strip of p1 is sold into its layout.
    """
    return "\n".join(
        [
            "PAGE p2",
            "PANEL p2a 0 0 30 30",
            "ACTOR carol @ 2 28 PANEL p2a",
            "ACTOR dave @ 20 28 PANEL p2a",
            "BUBBLE D PANEL p2a 2 2 14 10",
            "BUBBLE E PANEL p2a 18 2 28 10",
            "SAY D ni hao",
            "SAY E zai jian",
            "TAIL D -> 2 28",
            "TAIL E -> 20 28",
            "REPLY D>E",
            "",
        ]
    )


def swap_relation(text: str) -> str:
    """Editor's single-character change: the REPLY mark ``>`` becomes ``<``.

    Only the mark on the ``REPLY A>B`` line is touched; the ``>`` characters
    inside TAIL arrows are different characters at different positions.
    """
    needle = "REPLY A>B"
    if needle not in text:
        raise ValueError("manuscript does not carry the editable A>B relation")
    at = text.index(needle) + len("REPLY A")
    return text[:at] + "<" + text[at + 1 :]


def canonical_margin_slots() -> tuple[MarginSlot, ...]:
    # sAB is deliberately the *only* strip narrow enough to reach both A and
    # B; sA and sB reach one bubble each and sC only the narrator.
    return (
        MarginSlot("sA", PAGE_ID, "left", frozenset({"A"}), (0, 4)),
        MarginSlot("sAB", PAGE_ID, "top", frozenset({"A", "B"}), (20, 0)),
        MarginSlot("sB", PAGE_ID, "right", frozenset({"B"}), (42, 4)),
        MarginSlot("sC", PAGE_ID, "right", frozenset({"C"}), (64, 4)),
    )


def margin_permit(
    *,
    dialogue: frozenset[str] = frozenset({"A", "B", "C"}),
    slots: tuple[MarginSlot, ...] | None = None,
) -> PrintPermit:
    return PrintPermit(
        permit_id="permit-margin",
        page_id=PAGE_ID,
        craft=CRAFT_MARGIN,
        dialogue=dialogue,
        margin_slots=canonical_margin_slots() if slots is None else slots,
    )


def relayout_permit(
    *,
    dialogue: frozenset[str] = frozenset({"A", "B", "C"}),
    redraw_panels: frozenset[str] = frozenset({"p1a", "p1b"}),
    sealed_panels: frozenset[str] = frozenset(),
) -> PrintPermit:
    return PrintPermit(
        permit_id="permit-relayout",
        page_id=PAGE_ID,
        craft=CRAFT_RELAYOUT,
        dialogue=dialogue,
        redraw_panels=redraw_panels,
        sealed_panels=sealed_panels,
    )


def build_canonical_permit(craft: str, **kwargs) -> PrintPermit:
    if craft == CRAFT_MARGIN:
        return margin_permit(**kwargs)
    if craft == CRAFT_RELAYOUT:
        return relayout_permit(**kwargs)
    raise ValueError(f"unknown craft {craft!r}")


def version_without_b_print(permit: PrintPermit) -> PrintPermit:
    """Return a copy of ``permit`` with bubble B's dialogue right withdrawn."""
    dialogue = frozenset(b for b in permit.dialogue if b != "B")
    return PrintPermit(
        permit_id=permit.permit_id + "-no-b",
        page_id=permit.page_id,
        craft=permit.craft,
        dialogue=dialogue,
        margin_slots=permit.margin_slots,
        redraw_panels=permit.redraw_panels,
        sealed_panels=permit.sealed_panels,
    )
