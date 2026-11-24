"""End-to-end comic proofing demonstration.

Run:  python3 -m solo.comics.demo

It registers the canonical A/B/C sheet as a real SOLO body in a
:class:`RevisionLibrary`, edits the reply relation through
``RevisionLibrary.apply`` by character position, locates that edit again
through ``Version.decode`` and the original byte span, and then drives the
layout worker's single ``ProofDesk.proof`` entry through both licensed
crafts.  Every proof sheet is real ASCII paper written under
``artifacts/comics/``; rejections carry the stage (proofing vs press) and
the original-page location that explains them.
"""

from __future__ import annotations

import json
import os
import time

from ..codec import encode_text
from ..revision import RevisionLibrary, replace
from .manuscript import natural_key, parse_manuscript, reading_order, validate_manuscript
from .proof import ProofDesk, ProofSheet, Rejection, STAGE_PRESS, STAGE_PROOFING
from .rights import CRAFT_MARGIN, OrderBook, PrintPermit
from .sample import (
    PAGE_ID,
    build_canonical_permit,
    canonical_manuscript_text,
    margin_permit,
    relayout_permit,
    second_page_manuscript_text,
    self_loop_manuscript_text,
    swap_relation,
    version_without_b_print,
)

OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "artifacts",
    "comics",
)


def _write_sheet(sheet: ProofSheet) -> str:
    path = os.path.join(
        OUT_DIR,
        f"proof_v{sheet.version_id}_{sheet.craft}_{sheet.order_id}.txt",
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(sheet.render() + "\n")
    return path


def _rejection_dict(rejection: Rejection) -> dict:
    return {
        "stage": rejection.stage,
        "code": rejection.code,
        "reason": rejection.reason,
        "page": rejection.page_id,
        "bubble": rejection.bubble_id,
        "line": rejection.line_no,
        "char_span": list(rejection.char_span) if rejection.char_span else None,
        "byte_span": list(rejection.byte_span) if rejection.byte_span else None,
        "blocked_by_page": rejection.blocked_by_page,
    }


def build_edited_library(log=print) -> tuple[RevisionLibrary, int, int, dict]:
    """Register v0, then publish the single-symbol relation edit as v1."""
    lib = RevisionLibrary()
    old_text = canonical_manuscript_text("A>B")
    original = lib.add_original(encode_text(old_text).data)
    assert original.version_id == 0

    # The editor edits by *character* position on the pinned version; the
    # relation mark is the '>' of the trailing REPLY line, not the '>' used
    # by TAIL arrows earlier in the sheet.
    mark = old_text.index("REPLY A>B") + len("REPLY A")
    log(f"editor: relation mark '>' at char {mark} of v{original.version_id}")
    new_version = lib.apply(original.version_id, [replace(mark, mark + 1, "<")])

    # Locate the edit again from the actual decoded text and original spans.
    decoded = new_version.decode()
    record = new_version.records[0]
    evidence = {
        "edited_char_span": [record.old_char_start, record.old_char_end],
        "original_byte_span": [record.old_byte_start, record.old_byte_end],
        "new_byte_span": [record.new_byte_start, record.new_byte_end],
        "decoded_at_span": decoded.text[record.new_char_start : record.new_char_end],
        "old_at_span": original.text[record.old_char_start : record.old_char_end],
    }
    log(f"editor: trace -> {evidence}")
    assert decoded.text == swap_relation(old_text)
    return lib, original.version_id, new_version.version_id, evidence


def demo(log=print) -> dict:
    os.makedirs(OUT_DIR, exist_ok=True)
    started = time.time()
    lib, v0, v1, edit_evidence = build_edited_library(log)
    desk = ProofDesk(lib)

    old_page = parse_manuscript(lib.get(v0).text).pages[PAGE_ID]
    new_page = parse_manuscript(lib.get(v1).text).pages[PAGE_ID]
    log(f"v0 natural/reading order: {reading_order(old_page)}")
    log(f"v1 natural paper order:   {sorted(new_page.bubbles, key=lambda b: natural_key(new_page, b))}")
    log(f"v1 required reading order: {reading_order(new_page)}")

    manifest: dict = {
        "edit": edit_evidence,
        "sheets": [],
        "rejections": [],
    }

    # -- craft 1: margin guides -------------------------------------------
    log("== craft: margin guides ==")
    book = OrderBook("order-margin-1")
    permit = margin_permit()
    old_sheet = desk.proof(v0, permit, book)
    assert isinstance(old_sheet, ProofSheet)
    log(f"v0 -> {old_sheet.readback()} (no guide needed: {not old_sheet.guides})")
    manifest["sheets"].append({"version": v0, "path": _write_sheet(old_sheet), "readback": old_sheet.readback()})

    new_sheet = desk.proof(v1, permit, book)
    assert isinstance(new_sheet, ProofSheet)
    log(f"v1 -> {new_sheet.readback()} via guides {[(g.slot_id, g.label, g.bubble_id) for g in new_sheet.guides]}")
    manifest["sheets"].append({"version": v1, "path": _write_sheet(new_sheet), "readback": new_sheet.readback()})

    # Re-check the same order after the only strip reaching A and B is sold
    # to the other page of the volume.  That other page (p2) is a real page
    # of the same manuscript and is proofed first, so the sale is committed
    # and cannot be reclaimed; the new version must then stop at press.
    narrow = next(slot for slot in permit.margin_slots if slot.slot_id == "sAB")
    two_page_lib = RevisionLibrary()
    two_page_version = two_page_lib.add_original(
        encode_text(canonical_manuscript_text("A<B") + second_page_manuscript_text()).data
    )
    p2_permit = PrintPermit("permit-p2", "p2", CRAFT_MARGIN, frozenset({"D", "E"}), ())
    p2_sheet = ProofDesk(two_page_lib).proof(two_page_version.version_id, p2_permit, book)
    assert isinstance(p2_sheet, ProofSheet) and book.was_proofed("p2")
    log(f"other page p2 proofed and handed to printer: {p2_sheet.readback()}")
    book.sell_strip(narrow, "p2")
    log("rights: narrow strip sAB (reaches only A,B) sold to page p2; re-checking the same order")
    blocked = desk.proof(v1, permit, book)
    assert isinstance(blocked, Rejection) and blocked.stage == STAGE_PRESS
    assert book.reclaim_strip("sAB") is False
    log(f"re-check -> {blocked.summary()}")
    manifest["rejections"].append({"scenario": "margin-strip-sold", **_rejection_dict(blocked)})

    # The old version still proofs because it needs no guide at all.
    old_again = desk.proof(v0, permit, book)
    assert isinstance(old_again, ProofSheet)
    log(f"v0 re-check still proofs: {old_again.readback()}")

    # -- craft 2: natural relayout ----------------------------------------
    log("== craft: natural relayout ==")
    book2 = OrderBook("order-relayout-1")
    rpermit = relayout_permit()
    old_relayout = desk.proof(v0, rpermit, book2)
    assert isinstance(old_relayout, ProofSheet) and not old_relayout.moves
    manifest["sheets"].append(
        {"version": v0, "path": _write_sheet(old_relayout), "readback": old_relayout.readback()}
    )

    new_relayout = desk.proof(v1, rpermit, book2)
    assert isinstance(new_relayout, ProofSheet)
    log(f"v1 relayout moves: {[(m.bubble_id, m.from_panel, m.to_panel) for m in new_relayout.moves]}")
    log(f"v1 paper readback: {new_relayout.readback()}")
    manifest["sheets"].append(
        {"version": v1, "path": _write_sheet(new_relayout), "readback": new_relayout.readback()}
    )
    assert set(book2.seals()) == set()  # signing only happens explicitly below
    assert book2.redraw_used(PAGE_ID, "p1a") and book2.redraw_used(PAGE_ID, "p1b")

    # Painter now signs the two redrawn panels.  A further relayout request
    # on the new version is frozen at press; the sign-off cannot be undone.
    book2.sign_panel(PAGE_ID, "p1a")
    book2.sign_panel(PAGE_ID, "p1b")
    sealed = desk.proof(v1, rpermit, book2)
    assert isinstance(sealed, Rejection) and sealed.code == "PANEL_SEALED" and sealed.stage == STAGE_PRESS
    log(f"after painter sign-off -> {sealed.summary()}")
    manifest["rejections"].append({"scenario": "panels-sealed", **_rejection_dict(sealed)})

    # -- fault 1: self-loop manuscript ------------------------------------
    log("== fault: B answers itself ==")
    loop_lib = RevisionLibrary()
    loop_version = loop_lib.add_original(encode_text(self_loop_manuscript_text()).data)
    loop_issues = validate_manuscript(parse_manuscript(loop_version.text))
    assert any(i.code == "SELF_LOOP" for i in loop_issues)
    loop_desk = ProofDesk(loop_lib)
    loop_result = loop_desk.proof(loop_version.version_id, margin_permit(), OrderBook("order-loop"))
    assert isinstance(loop_result, Rejection) and loop_result.stage == STAGE_PROOFING
    log(f"self-loop -> {loop_result.summary()}")
    manifest["rejections"].append({"scenario": "self-loop", **_rejection_dict(loop_result)})

    # -- fault 2: legal sheet, B's dialogue print right withdrawn ----------
    log("== fault: B dialogue print right withdrawn ==")
    no_b = version_without_b_print(build_canonical_permit(CRAFT_MARGIN))
    press_block = desk.proof(v1, no_b, OrderBook("order-no-b"))
    assert isinstance(press_block, Rejection) and press_block.stage == STAGE_PRESS
    log(f"withdrawn right -> {press_block.summary()}")
    manifest["rejections"].append({"scenario": "dialogue-right-withdrawn", **_rejection_dict(press_block)})

    manifest["elapsed_seconds"] = round(time.time() - started, 3)
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    log(f"== comic artifacts written to {OUT_DIR} ==")
    return manifest


def main() -> int:
    demo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
