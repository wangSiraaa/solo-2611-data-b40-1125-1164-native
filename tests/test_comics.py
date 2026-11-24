"""Tests for the comic translation-journal proofing layer.

Run with:  python3 -m unittest discover -s tests -v

The tests pin every requirement in the task statement: registration and
validation (mis-wired tails, dangling references, self loops and cycles),
natural row-major reading order, revision-library edits located through
``Version.decode`` and original byte spans, both licensed crafts with real
readable paper, non-reclaimable rights sold/sealed to another page, and the
proofing-vs-press stage of every rejection.
"""

from __future__ import annotations

import unittest

from solo.codec import MODE_PAIR, encode_text
from solo.comics import (
    Guide,
    Move,
    ProofDesk,
    ProofSheet,
    Rejection,
    STAGE_PRESS,
    STAGE_PROOFING,
    MarginSlot,
    OrderBook,
    PrintPermit,
    build_canonical_permit,
    canonical_manuscript_text,
    natural_key,
    parse_manuscript,
    reading_order,
    second_page_manuscript_text,
    self_loop_manuscript_text,
    swap_relation,
    validate_manuscript,
    version_without_b_print,
)
from solo.comics.manuscript import inversions
from solo.comics.rights import CRAFT_MARGIN, CRAFT_RELAYOUT
from solo.revision import RevisionLibrary, replace

PAGE = "p1"


def edited_library():
    """Register the canonical sheet and publish the > -> < relation edit."""
    lib = RevisionLibrary()
    old_text = canonical_manuscript_text("A>B")
    original = lib.add_original(encode_text(old_text).data)
    mark = old_text.index("REPLY A>B") + len("REPLY A")
    new_version = lib.apply(original.version_id, [replace(mark, mark + 1, "<")])
    return lib, original, new_version, old_text, mark


class ManuscriptRegistrationTests(unittest.TestCase):
    def test_parses_panels_bubbles_speakers_tails_and_relations(self):
        manuscript = parse_manuscript(canonical_manuscript_text())
        page = manuscript.pages[PAGE]
        self.assertEqual(set(page.panels), {"p1a", "p1b", "p1c"})
        self.assertEqual(set(page.bubbles), {"A", "B", "C"})
        self.assertTrue(page.bubbles["C"].offpage)
        # Tail endpoints land exactly on the registered speakers.
        self.assertEqual(page.bubbles["A"].tail, page.actors["alice"].at)
        self.assertEqual(page.bubbles["B"].tail, page.actors["bob"].at)
        # The SAY line carries PAIR-mode characters.
        self.assertIn(chr(0x4E2A), page.says["A"].text)

    def test_pair_characters_round_trip_through_solo_body(self):
        body = encode_text(canonical_manuscript_text()).data
        lib = RevisionLibrary()
        version = lib.add_original(body)
        doc = version.decode()
        pair_index = doc.text.index(chr(0x4E2A))
        self.assertEqual(doc.char_mode(pair_index), MODE_PAIR)
        self.assertEqual(doc.byte_range(pair_index)[1] - doc.byte_range(pair_index)[0], 2)

    def test_syntax_error_is_located_to_a_line(self):
        from solo.comics.manuscript import ManuscriptError

        with self.assertRaises(ManuscriptError) as ctx:
            parse_manuscript("PAGE p1\nBUBBLE Z PANEL nope 0 0 1 1\n")
        self.assertEqual(ctx.exception.line_no, 2)

    def test_page_and_bubble_caps(self):
        from solo.comics.manuscript import ManuscriptError

        second = canonical_manuscript_text().replace("PAGE p1", "PAGE p2")
        two_pages = canonical_manuscript_text() + "\n" + second
        parse_manuscript(two_pages)  # two pages are legal
        three = two_pages + "\nPAGE p3\nPANEL x 0 0 1 1\n"
        with self.assertRaises(ManuscriptError):
            parse_manuscript(three)

    def test_max_eight_bubbles(self):
        from solo.comics.manuscript import ManuscriptError

        lines = ["PAGE p1", "PANEL q 0 0 100 100", "ACTOR s @ 1 99 PANEL q"]
        for i in range(9):
            bid = chr(ord("A") + i)
            lines.append(f"BUBBLE {bid} PANEL q {i} 0 {i + 1} 10")
            lines.append(f"SAY {bid} line {i}")
            lines.append(f"TAIL {bid} -> 1 99")
        with self.assertRaises(ManuscriptError):
            parse_manuscript("\n".join(lines))


class ValidationTests(unittest.TestCase):
    def _issues(self, text):
        return validate_manuscript(parse_manuscript(text))

    def test_canonical_sheet_is_sound(self):
        self.assertEqual(self._issues(canonical_manuscript_text()), [])

    def test_miswired_tail_points_at_empty_point(self):
        text = canonical_manuscript_text().replace("TAIL A -> 2 18", "TAIL A -> 9 9")
        issues = self._issues(text)
        self.assertTrue(any(i.code == "TAIL_MISWIRED" and i.bubble_id == "A" for i in issues))

    def test_missing_tail(self):
        text = canonical_manuscript_text().replace("TAIL B -> 24 18\n", "")
        issues = self._issues(text)
        self.assertTrue(any(i.code == "MISSING_TAIL" and i.bubble_id == "B" for i in issues))

    def test_dangling_reply_reference_is_rejected_at_parse(self):
        from solo.comics.manuscript import ManuscriptError

        text = canonical_manuscript_text().replace("REPLY A>B", "REPLY A>Z")
        with self.assertRaises(ManuscriptError):
            parse_manuscript(text)

    def test_self_loop_is_a_proofing_fault_traceable_to_the_line(self):
        issues = self._issues(self_loop_manuscript_text())
        issue = next(i for i in issues if i.code == "SELF_LOOP")
        self.assertEqual(issue.bubble_id, "B")
        self.assertIn("answer itself", issue.reason)

    def test_three_node_cycle_is_detected(self):
        # All three bubbles on one row, all on-page.  A>B (B answers A),
        # B>C (C answers B), C<A (A answers C): the reply graph is A->B->C->A.
        text = "\n".join(
            [
                "PAGE p1",
                "PANEL p1a 0 0 20 20",
                "PANEL p1b 22 0 42 20",
                "PANEL p1c 44 0 64 20",
                "ACTOR alice @ 2 18 PANEL p1a",
                "ACTOR bob @ 24 18 PANEL p1b",
                "ACTOR carol @ 46 18 PANEL p1c",
                "BUBBLE A PANEL p1a 2 2 16 10",
                "BUBBLE B PANEL p1b 24 2 38 10",
                "BUBBLE C PANEL p1c 46 2 60 10",
                "SAY A one",
                "SAY B two",
                "SAY C three",
                "TAIL A -> 2 18",
                "TAIL B -> 24 18",
                "TAIL C -> 46 18",
                "REPLY A>B",
                "REPLY B>C",
                "REPLY A<C",
            ]
        )
        issues = self._issues(text)
        self.assertTrue(any(i.code == "REPLY_CYCLE" for i in issues))

    def test_offpage_narrator_must_stay_last(self):
        # A<C forces the off-page narrator before A: illegal.
        text = canonical_manuscript_text("A>B").replace("REPLY A>B", "REPLY A<C")
        issues = self._issues(text)
        self.assertTrue(any(i.code == "NARRATOR_NOT_LAST" for i in issues))


class ReadingOrderTests(unittest.TestCase):
    def test_natural_order_is_row_major_left_to_right(self):
        page = parse_manuscript(canonical_manuscript_text()).pages[PAGE]
        self.assertEqual(reading_order(page), ["A", "B", "C"])

    def test_relation_flip_moves_b_before_a_keeps_c_last(self):
        page = parse_manuscript(canonical_manuscript_text("A<B")).pages[PAGE]
        self.assertEqual(reading_order(page), ["B", "A", "C"])

    def test_independent_short_lines_read_in_parallel_natural_order(self):
        text = "\n".join(
            [
                "PAGE p1",
                "PANEL q 0 0 100 100",
                "ACTOR s1 @ 1 90 PANEL q",
                "ACTOR s2 @ 3 90 PANEL q",
                "BUBBLE A PANEL q 0 0 10 10",
                "BUBBLE B PANEL q 20 0 30 10",
                "SAY A one",
                "SAY B two",
                "TAIL A -> 1 90",
                "TAIL B -> 3 90",
            ]
        )
        page = parse_manuscript(text).pages[PAGE]
        self.assertEqual(reading_order(page), ["A", "B"])
        self.assertEqual(inversions(page, reading_order(page)), [])

    def test_rows_read_top_to_bottom(self):
        text = "\n".join(
            [
                "PAGE p1",
                "PANEL q 0 0 100 100",
                "ACTOR s @ 1 90 PANEL q",
                "BUBBLE LOW PANEL q 0 40 10 50",
                "BUBBLE HIGH PANEL q 0 0 10 10",
                "SAY LOW lower",
                "SAY HIGH upper",
                "TAIL LOW -> 1 90",
                "TAIL HIGH -> 1 90",
            ]
        )
        page = parse_manuscript(text).pages[PAGE]
        self.assertEqual(reading_order(page), ["HIGH", "LOW"])


class RevisionEditTests(unittest.TestCase):
    def test_apply_edits_relation_by_character_position(self):
        lib, v0, v1, old_text, mark = edited_library()
        self.assertEqual(v0.text[mark : mark + 1], ">")
        self.assertEqual(v1.text[mark : mark + 1], "<")
        self.assertEqual(v1.text, swap_relation(old_text))

    def test_edit_is_traceable_through_decode_and_original_byte_span(self):
        lib, v0, v1, _old_text, mark = edited_library()
        record = v1.records[0]
        # Character position of the edit.
        self.assertEqual((record.old_char_start, record.old_char_end), (mark, mark + 1))
        # The original body span the mark occupied.
        old_doc = v0.decode()
        self.assertEqual(
            (record.old_byte_start, record.old_byte_end), old_doc.byte_range(mark)
        )
        # The new version decodes the replacement at the new span.
        new_doc = v1.decode()
        self.assertEqual(new_doc.text[record.new_char_start : record.new_char_end], "<")
        self.assertEqual(
            v1.body[record.new_byte_start : record.new_byte_end], b"<"
        )

    def test_untouched_bytes_are_identical_between_versions(self):
        lib, v0, v1, _old_text, mark = edited_library()
        doc = v0.decode()
        b0, b1 = doc.byte_range(mark)
        before = v0.body[:b0]
        after = v0.body[b1:]
        self.assertTrue(v1.body.startswith(before))
        self.assertTrue(v1.body.endswith(after))
        self.assertEqual(len(v1.body), len(v0.body))  # one ASCII char for one ASCII char

    def test_library_verifies_every_version(self):
        lib, v0, v1, _old, _mark = edited_library()
        self.assertTrue(lib.verify(v0.version_id))
        self.assertTrue(lib.verify(v1.version_id))


class MarginCraftTests(unittest.TestCase):
    def setUp(self):
        self.lib, self.v0, self.v1, _old, _mark = edited_library()
        self.desk = ProofDesk(self.lib)
        self.permit = build_canonical_permit(CRAFT_MARGIN)

    def test_old_version_proofs_without_guides_in_natural_order(self):
        sheet = self.desk.proof(self.v0.version_id, self.permit, OrderBook("o1"))
        self.assertIsInstance(sheet, ProofSheet)
        self.assertEqual(sheet.guides, ())
        self.assertEqual(sheet.readback(), ("A", "B", "C"))

    def test_new_version_uses_the_narrow_two_bubble_strip(self):
        book = OrderBook("o1")
        sheet = self.desk.proof(self.v1.version_id, self.permit, book)
        self.assertIsInstance(sheet, ProofSheet)
        self.assertEqual(sheet.readback(), ("B", "A", "C"))
        self.assertEqual({g.slot_id for g in sheet.guides}, {"sAB"})
        labels = {g.bubble_id: g.label for g in sheet.guides}
        self.assertEqual(labels, {"B": "1", "A": "2"})
        # Every guide points at a bubble the strip actually reaches.
        for guide in sheet.guides:
            self.assertIn(guide.bubble_id, guide.reaches)

    def test_paper_rendering_contains_guides_and_colophon_readback(self):
        sheet = self.desk.proof(self.v1.version_id, self.permit, OrderBook("o1"))
        rendered = sheet.render()
        self.assertIn("mark 1 -> bubble B", rendered)
        self.assertIn("mark 2 -> bubble A", rendered)
        self.assertIn("COLOPHON readback=B A C", rendered)
        self.assertIn(chr(0x4E2A), rendered)

    def test_sold_strip_blocks_recheck_at_press_and_cannot_be_reclaimed(self):
        book = OrderBook("o1")
        first = self.desk.proof(self.v1.version_id, self.permit, book)
        self.assertIsInstance(first, ProofSheet)
        strip = next(s for s in self.permit.margin_slots if s.slot_id == "sAB")
        book.sell_strip(strip, "p2")
        rejection = self.desk.proof(self.v1.version_id, self.permit, book)
        self.assertIsInstance(rejection, Rejection)
        self.assertEqual(rejection.stage, STAGE_PRESS)
        self.assertEqual(rejection.code, "MARGIN_STRIP_SOLD")
        self.assertEqual(rejection.blocked_by_page, "p2")
        # The signed other page cannot be taken back.
        self.assertFalse(book.reclaim_strip("sAB"))
        self.assertEqual(book.strip_buyer("sAB"), "p2")

    def test_old_version_still_proofs_after_strip_sale(self):
        book = OrderBook("o1")
        strip = next(s for s in self.permit.margin_slots if s.slot_id == "sAB")
        book.sell_strip(strip, "p2")
        sheet = self.desk.proof(self.v0.version_id, self.permit, book)
        self.assertIsInstance(sheet, ProofSheet)
        self.assertEqual(sheet.readback(), ("A", "B", "C"))

    def test_missing_slot_is_a_press_rejection(self):
        # A permit with no strip reaching B cannot guide the inverted pair.
        slots = tuple(s for s in self.permit.margin_slots if s.slot_id not in ("sAB", "sB"))
        permit = PrintPermit("weak", PAGE, CRAFT_MARGIN, frozenset("ABC"), slots)
        rejection = self.desk.proof(self.v1.version_id, permit, OrderBook("o1"))
        self.assertIsInstance(rejection, Rejection)
        self.assertEqual(rejection.stage, STAGE_PRESS)
        self.assertEqual(rejection.code, "NO_MARGIN_SLOT")


class RelayoutCraftTests(unittest.TestCase):
    def setUp(self):
        self.lib, self.v0, self.v1, _old, _mark = edited_library()
        self.desk = ProofDesk(self.lib)
        self.permit = build_canonical_permit(CRAFT_RELAYOUT)

    def test_old_version_needs_no_move(self):
        sheet = self.desk.proof(self.v0.version_id, self.permit, OrderBook("o1"))
        self.assertIsInstance(sheet, ProofSheet)
        self.assertEqual(sheet.moves, ())
        self.assertEqual(sheet.readback(), ("A", "B", "C"))

    def test_new_version_swaps_the_two_panels_and_reconnects_tails(self):
        book = OrderBook("o1")
        sheet = self.desk.proof(self.v1.version_id, self.permit, book)
        self.assertIsInstance(sheet, ProofSheet)
        self.assertEqual(sheet.readback(), ("B", "A", "C"))
        by_id = {m.bubble_id: m for m in sheet.moves}
        self.assertEqual(by_id["A"].from_panel, "p1a")
        self.assertEqual(by_id["A"].to_panel, "p1b")
        self.assertEqual(by_id["B"].to_panel, "p1a")
        # Tails reconnect to the bubble's own original speaker anchor.
        page = parse_manuscript(self.lib.get(self.v1.version_id).text).pages[PAGE]
        self.assertEqual(tuple(by_id["A"].new_tail), tuple(page.actors["alice"].at))
        self.assertEqual(tuple(by_id["B"].new_tail), tuple(page.actors["bob"].at))
        # Redraw of the two panels is consumed; C's panel is untouched.
        self.assertTrue(book.redraw_used(PAGE, "p1a"))
        self.assertTrue(book.redraw_used(PAGE, "p1b"))
        self.assertFalse(book.redraw_used(PAGE, "p1c"))

    def test_rendered_visual_layout_reads_b_a_c(self):
        sheet = self.desk.proof(self.v1.version_id, self.permit, OrderBook("o1"))
        rendered = sheet.render()
        self.assertIn("VISUAL LAYOUT AFTER REDRAW", rendered)
        visual = [line for line in rendered.splitlines() if line.strip().startswith("[")]
        self.assertEqual([line.strip()[1] for line in visual], ["B", "A", "C"])

    def test_sealed_panels_freeze_artwork_and_block_at_press(self):
        book = OrderBook("o1")
        book.sign_panel(PAGE, "p1a")
        book.sign_panel(PAGE, "p1b")
        rejection = self.desk.proof(self.v1.version_id, self.permit, book)
        self.assertIsInstance(rejection, Rejection)
        self.assertEqual(rejection.stage, STAGE_PRESS)
        self.assertEqual(rejection.code, "PANEL_SEALED")
        # Seals are permanent: a second attempt is blocked identically.
        again = self.desk.proof(self.v1.version_id, self.permit, book)
        self.assertEqual(again.code, "PANEL_SEALED")

    def test_relayout_without_redraw_licence_blocked_at_press(self):
        permit = PrintPermit("no-redraw", PAGE, CRAFT_RELAYOUT, frozenset("ABC"), frozenset(), frozenset())
        rejection = self.desk.proof(self.v1.version_id, permit, OrderBook("o1"))
        self.assertEqual(rejection.stage, STAGE_PRESS)
        self.assertEqual(rejection.code, "REDRAW_NOT_LICENSED")

    def test_c_panel_never_needs_redraw(self):
        book = OrderBook("o1")
        sheet = self.desk.proof(self.v1.version_id, self.permit, book)
        self.assertNotIn("C", {m.bubble_id for m in sheet.moves})


class FaultStageTests(unittest.TestCase):
    def setUp(self):
        self.lib, self.v0, self.v1, _old, _mark = edited_library()
        self.desk = ProofDesk(self.lib)

    def test_self_loop_is_blocked_at_proofing(self):
        loop_lib = RevisionLibrary()
        loop_version = loop_lib.add_original(encode_text(self_loop_manuscript_text()).data)
        result = ProofDesk(loop_lib).proof(
            loop_version.version_id,
            build_canonical_permit(CRAFT_MARGIN),
            OrderBook("o-loop"),
        )
        self.assertIsInstance(result, Rejection)
        self.assertEqual(result.stage, STAGE_PROOFING)
        self.assertEqual(result.code, "SELF_LOOP")
        self.assertIsNotNone(result.byte_span)  # traceable to the original page

    def test_withdrawn_dialogue_right_is_blocked_at_press(self):
        for craft in (CRAFT_MARGIN, CRAFT_RELAYOUT):
            permit = version_without_b_print(build_canonical_permit(craft))
            result = self.desk.proof(self.v1.version_id, permit, OrderBook(f"o-{craft}"))
            self.assertIsInstance(result, Rejection, craft)
            self.assertEqual(result.stage, STAGE_PRESS, craft)
            self.assertEqual(result.code, "DIALOGUE_NOT_LICENSED", craft)
            self.assertEqual(result.bubble_id, "B")

    def test_sound_sheet_means_proofing_passed_before_press_check(self):
        # The withdrawn-right manuscript itself validates cleanly.
        page = parse_manuscript(self.lib.get(self.v1.version_id).text).pages[PAGE]
        self.assertEqual(validate_manuscript(parse_manuscript(self.lib.get(self.v1.version_id).text)), [])
        self.assertEqual(reading_order(page), ["B", "A", "C"])


class CrossPageRightsTests(unittest.TestCase):
    def _two_page_library(self):
        # One manuscript, two pages, five bubbles -- inside the 2-page/8-bubble cap.
        sheet = canonical_manuscript_text("A<B") + second_page_manuscript_text()
        lib = RevisionLibrary()
        version = lib.add_original(encode_text(sheet).data)
        return lib, version

    def test_strip_sold_to_a_real_second_page_blocks_the_first(self):
        lib, version = self._two_page_library()
        manuscript = parse_manuscript(version.text)
        self.assertEqual(set(manuscript.pages), {"p1", "p2"})
        desk = ProofDesk(lib)
        book = OrderBook("order-volume-7")

        # The other page (p2) is proofed first and handed to the printer.
        p2_permit = PrintPermit(
            "permit-p2", "p2", CRAFT_MARGIN, frozenset({"D", "E"}), ()
        )
        p2_sheet = desk.proof(version.version_id, p2_permit, book)
        self.assertIsInstance(p2_sheet, ProofSheet)
        self.assertEqual(p2_sheet.readback(), ("D", "E"))
        self.assertTrue(book.was_proofed("p2"))

        strip = MarginSlot("sAB", "p1", "top", frozenset({"A", "B"}))
        book.sell_strip(strip, "p2")
        self.assertEqual(book.strip_buyer("sAB"), "p2")

        p1_permit = PrintPermit(
            "permit-p1", "p1", CRAFT_MARGIN, frozenset({"A", "B", "C"}), (strip,)
        )
        rejection = desk.proof(version.version_id, p1_permit, book)
        self.assertIsInstance(rejection, Rejection)
        self.assertEqual((rejection.stage, rejection.code), (STAGE_PRESS, "MARGIN_STRIP_SOLD"))
        self.assertEqual(rejection.blocked_by_page, "p2")
        # Re-checking the same order cannot take the committed strip back.
        self.assertFalse(book.reclaim_strip("sAB"))
        self.assertTrue(book.was_proofed("p2"))

    def test_narrow_strip_reach_is_exactly_the_two_guided_bubbles(self):
        strip = MarginSlot("sAB", "p1", "top", frozenset({"A", "B"}))
        self.assertEqual(strip.reaches, frozenset({"A", "B"}))
        self.assertNotIn("C", strip.reaches)


class RightsTests(unittest.TestCase):
    def test_permit_rejects_wrong_craft_combination(self):
        with self.assertRaises(ValueError):
            PrintPermit(
                "bad",
                PAGE,
                CRAFT_MARGIN,
                frozenset("ABC"),
                margin_slots=(),
                redraw_panels=frozenset({"p1a"}),
            )

    def test_strip_cannot_be_sold_twice(self):
        book = OrderBook("o1")
        slot = MarginSlot("s", PAGE, "top", frozenset({"A"}))
        book.sell_strip(slot, "p2")
        with self.assertRaises(ValueError):
            book.sell_strip(slot, "p3")

    def test_proofed_page_is_recorded(self):
        lib, v0, _v1, _old, _mark = edited_library()
        book = OrderBook("o1")
        desk = ProofDesk(lib)
        desk.proof(v0.version_id, build_canonical_permit(CRAFT_MARGIN), book)
        self.assertTrue(book.was_proofed(PAGE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
