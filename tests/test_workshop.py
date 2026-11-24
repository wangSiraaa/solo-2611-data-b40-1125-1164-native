"""Tests for the wig workshop styling counter (solo.workshop).

The three acceptance situations from the task statement are modelled end to
end:

1. two cells, A red / B black, customer wants left-red right-black,
   as-is only -> deliverable;
2. a re-combing that puts B on top of A in the left cell loses the red,
   and after restoring the combing an all-red order with no dye/clip
   approval is still undeliverable;
3. same order, dye or clip approved on A, tried on and then re-checked
   after the next-day parting change.

Beyond those, the tests cover PAIR margin notes, byte-faithful preserve of
return-as-is roots, one root recolouring every cell it exposes, piece
exclusivity across two wigs, fixed dye duration and the return workflow.
"""

from __future__ import annotations

import unittest

from solo.codec import encode_text
from solo.revision import RevisionLibrary
from solo.workshop import (
    MAX_CELLS,
    MAX_ROOTS,
    CellRequirement,
    ClipAction,
    DyeAction,
    Order,
    Piece,
    PieceInventory,
    Placement,
    StylingCounter,
    WorkshopError,
)


def body(text: str) -> bytes:
    return encode_text(text).data


# Base wig: two cells, each with two layers; A red, B black; both roots
# expose one clip slot.
BASE = (
    "WIG w1\n"
    "K A red\n"
    "K B black\n"
    "S A s1 10\n"
    "S B s1 10\n"
    "B A L L1 0\n"
    "B B L L1 1\n"
    "B A R R1 1\n"
    "B B R R1 0\n"
)


def counter_with(pieces=()) -> StylingCounter:
    counter = StylingCounter(RevisionLibrary(), PieceInventory(list(pieces)))
    counter.register_wig("w1", body(BASE))
    return counter


def counter_two_wigs(pieces=()) -> StylingCounter:
    counter = StylingCounter(RevisionLibrary(), PieceInventory(list(pieces)))
    counter.register_wig("w1", body(BASE))
    counter.register_wig("w2", body(BASE.replace("WIG w1", "WIG w2")))
    return counter


def left_right_order(
    order_id: str,
    *,
    as_is_only: bool = False,
    dye=frozenset(),
    clip=frozenset(),
    protected=frozenset(),
    removal_day: int = 2,
    both_red: bool = False,
) -> Order:
    if both_red:
        reqs = (CellRequirement("L1", "red"), CellRequirement("R1", "red"))
    else:
        reqs = (CellRequirement("L1", "red"), CellRequirement("R1", "black"))
    return Order(
        order_id=order_id,
        wig_id="w1",
        requirements=reqs,
        protected_roots=frozenset(protected),
        removal_day=removal_day,
        as_is_only=as_is_only,
        dye_allowed_roots=frozenset(dye),
        clip_allowed_roots=frozenset(clip),
    )


class AcceptanceScenarioTests(unittest.TestCase):
    def test_scenario_1_as_is_left_red_right_black_delivers(self):
        counter = counter_with()
        counter.place_order(left_right_order("o1", as_is_only=True))
        trial = counter.fit("o1")
        self.assertTrue(trial.feasible, trial.reason)
        self.assertEqual(trial.dye_actions, ())
        self.assertEqual(trial.clip_actions, ())
        views = {v.cell: v for v in trial.front_views}
        self.assertEqual(views["L1"].front_root, "A")
        self.assertEqual(views["L1"].front_colour, "red")
        self.assertEqual(views["R1"].front_root, "B")
        self.assertEqual(views["R1"].front_colour, "black")
        # The inner layer is back-only hair, never a second front item.
        self.assertEqual(views["L1"].back_only, (("B", "black"),))

    def test_scenario_2a_recomb_b_over_a_loses_the_red(self):
        counter = counter_with()
        counter.place_order(left_right_order("o1", as_is_only=True))
        # Re-comb only: B now sits on top of A in the left cell.
        report = counter.recomb(
            "w1",
            {
                "B": (
                    Placement.direct("B", "L", "L1", 0),
                    Placement.direct("B", "R", "R1", 0),
                ),
                "A": (
                    Placement.direct("A", "L", "L1", 1),
                    Placement.direct("A", "R", "R1", 1),
                ),
            },
        )
        # Only the left cell's stacking swaps: B moves to the outer layer
        # and A moves under it.  The right cell already matches the targets.
        self.assertEqual(len(report.changes), 2)
        changed_roots = {c.root for c in report.changes}
        self.assertEqual(changed_roots, {"A", "B"})
        change = next(c for c in report.changes if c.root == "B" and c.new_placement.cell == "L1")
        # The change points at the decoded text span and its original bytes.
        base = counter.library.get(report.base_version)
        doc = base.decode()
        self.assertEqual(doc.text[change.char_span[0] : change.char_span[1]], change.old_placement.render())
        self.assertEqual(
            base.body[change.byte_span[0] : change.byte_span[1]],
            doc.text[change.char_span[0] : change.char_span[1]].encode("ascii"),
        )
        views = {v.cell: v for v in report.front_views}
        self.assertEqual(views["L1"].front_root, "B")
        self.assertEqual(views["L1"].front_colour, "black")
        # A is no longer front-visible; it can only be seen from the back.
        self.assertIn(("A", "red"), views["L1"].back_only)
        verdicts = counter.review("w1", "o1")
        self.assertEqual([(v.cell, v.ok) for v in verdicts], [("L1", False), ("R1", True)])

    def test_scenario_2b_restore_then_both_red_without_approval_still_fails(self):
        counter = counter_with()
        # First really change the parting, then restore it through another
        # library batch: the restored text matches the original layout.
        swap = {
            "B": (
                Placement.direct("B", "L", "L1", 0),
                Placement.direct("B", "R", "R1", 0),
            ),
            "A": (
                Placement.direct("A", "L", "L1", 1),
                Placement.direct("A", "R", "R1", 1),
            ),
        }
        counter.recomb("w1", swap)
        counter.recomb(
            "w1",
            {
                "B": (
                    Placement.direct("B", "L", "L1", 1),
                    Placement.direct("B", "R", "R1", 0),
                ),
                "A": (
                    Placement.direct("A", "L", "L1", 0),
                    Placement.direct("A", "R", "R1", 1),
                ),
            },
        )
        restored = counter.pattern("w1")
        self.assertEqual([p.order for p in restored.stack("L1")], [0, 1])
        self.assertEqual(restored.stack("L1")[0].root, "A")
        counter.place_order(left_right_order("o2", both_red=True))
        trial = counter.fit("o2")
        self.assertFalse(trial.feasible)
        self.assertIn("R1", trial.reason)
        with self.assertRaises(WorkshopError):
            counter.confirm("o2")

    def test_scenario_3_dye_and_clip_trials_then_next_day_recomb_review(self):
        pieces = [Piece("p-pink", "pink", 8), Piece("p-red", "red", 8)]
        counter = counter_with(pieces)
        order = Order(
            "o3",
            "w1",
            (CellRequirement("L1", "pink"), CellRequirement("R1", "black")),
            frozenset(),
            removal_day=2,
            dye_allowed_roots=frozenset({"A"}),
            clip_allowed_roots=frozenset({"A"}),
        )
        counter.place_order(order)

        # Clip trial: a pink piece occupies A's one exposed slot.
        clip_trial = counter.fit("o3")
        self.assertTrue(clip_trial.feasible, clip_trial.reason)
        self.assertEqual(
            clip_trial.clip_actions,
            (ClipAction("p-pink", "A", "s1", "pink"),),
        )
        counter.confirm("o3")
        views = {v.cell: v for v in counter.all_views("w1")}
        self.assertEqual(views["L1"].front_colour, "pink")
        # The piece cannot be lent to anyone else before return.
        self.assertEqual(counter.inventory.on_loan(), ("p-pink",))

        # Next day the parting changes: B covers A in the left cell.
        counter.advance_day(1)
        report = counter.recomb(
            "w1",
            {
                "B": (
                    Placement.direct("B", "L", "L1", 0),
                    Placement.direct("B", "R", "R1", 0),
                ),
                "A": (
                    Placement.direct("A", "L", "L1", 1),
                    Placement.direct("A", "R", "R1", 1),
                ),
            },
        )
        # The rental is now buried: the customer no longer sees pink.
        self.assertEqual(report.hidden_rentals, ("A",))
        verdicts = {v.cell: v.ok for v in counter.review("w1", "o3")}
        self.assertEqual(verdicts, {"L1": False, "R1": True})

        # Dye trial on a fresh same-shape order: dye is whole-root and
        # survives being covered; it stays visible from the back.
        counter.return_wig("o3", day=2)
        # Restore the original parting before the dye fitting so A is the
        # exposed front root again.
        counter.recomb(
            "w1",
            {
                "B": (
                    Placement.direct("B", "L", "L1", 1),
                    Placement.direct("B", "R", "R1", 0),
                ),
                "A": (
                    Placement.direct("A", "L", "L1", 0),
                    Placement.direct("A", "R", "R1", 1),
                ),
            },
            day=2,
        )
        dye_order = Order(
            "o3b",
            "w1",
            (CellRequirement("L1", "pink"), CellRequirement("R1", "black")),
            frozenset(),
            removal_day=3,
            dye_allowed_roots=frozenset({"A"}),
            clip_allowed_roots=frozenset({"A"}),
        )
        # Remove the pink piece option so the planner must choose dye.
        counter.inventory.retire("p-pink")
        counter.place_order(dye_order)
        dye_trial = counter.fit("o3b", day=2)
        self.assertTrue(dye_trial.feasible, dye_trial.reason)
        self.assertEqual(dye_trial.dye_actions, (DyeAction("A", "pink"),))
        counter.confirm("o3b", day=2)
        views = {v.cell: v for v in counter.all_views("w1", day=2)}
        self.assertEqual(views["L1"].front_colour, "pink")
        # Cover A again: the fixed dye cannot revert, and shows from the back.
        buried = counter.recomb(
            "w1",
            {
                "B": (
                    Placement.direct("B", "L", "L1", 0),
                    Placement.direct("B", "R", "R1", 0),
                ),
                "A": (
                    Placement.direct("A", "L", "L1", 1),
                    Placement.direct("A", "R", "R1", 1),
                ),
            },
            day=2,
        )
        left = next(v for v in buried.front_views if v.cell == "L1")
        self.assertEqual(left.front_colour, "black")
        self.assertIn(("A", "pink"), left.back_only)
        counter.return_wig("o3b", day=3)


class PatternAndVisibilityTests(unittest.TestCase):
    def test_pair_margin_note_is_ignored_but_stored_in_pair_mode(self):
        note = chr(0x4E2D) + chr(0x5DFF)  # both inside U+4E00..U+5DFF
        text = "# " + note + "\n" + BASE
        raw = body(text)
        counter = StylingCounter(RevisionLibrary())
        version = counter.register_wig("w1", raw)
        # The note really was stored as PAIR bytes, yet never parsed as data.
        self.assertIn(b"\xff\x01", raw)
        self.assertEqual(counter.pattern("w1").roots, ("A", "B"))
        self.assertEqual(version.decode().text, text)

    def test_front_and_back_layers_are_distinguished(self):
        counter = counter_with()
        view = counter.front_view("w1", "R1")
        self.assertEqual(view.front_root, "B")
        self.assertEqual(view.back_only, (("A", "red"),))

    def test_limits_enforced(self):
        counter = StylingCounter(RevisionLibrary())
        lines = ["WIG big\n"]
        lines += [f"K R{i} c{i}\n" for i in range(MAX_ROOTS + 1)]
        lines += [f"B R{i} L C{i % MAX_CELLS} 0\n" for i in range(MAX_ROOTS + 1)]
        with self.assertRaises(WorkshopError):
            counter.register_wig("big", body("".join(lines)))
        counter.register_wig("w1", body(BASE))
        counter.register_wig("w2", body(BASE.replace("WIG w1", "WIG w2")))
        with self.assertRaises(WorkshopError):
            counter.register_wig("w3", body(BASE.replace("WIG w1", "WIG w3")))

    def test_non_dense_stack_is_rejected(self):
        bad = BASE.replace("B B L L1 1\n", "B B L L1 7\n")
        counter = StylingCounter(RevisionLibrary())
        with self.assertRaises(WorkshopError):
            counter.register_wig("w1", body(bad))


class DyeAndClipRuleTests(unittest.TestCase):
    def test_one_root_dye_recolours_every_cell_it_exposes_once(self):
        # Root A is the outer layer of two cells.
        text = (
            "WIG w1\n"
            "K A red\n"
            "K B black\n"
            "S A s1 10\n"
            "B A L L1 0\n"
            "B B L L1 1\n"
            "B A R R1 0\n"
            "B B R R1 1\n"
        )
        counter = StylingCounter(RevisionLibrary())
        counter.register_wig("w1", body(text))
        order = Order(
            "o",
            "w1",
            (CellRequirement("L1", "pink"), CellRequirement("R1", "pink")),
            frozenset(),
            removal_day=3,
            dye_allowed_roots=frozenset({"A"}),
        )
        counter.place_order(order)
        trial = counter.fit("o")
        self.assertTrue(trial.feasible, trial.reason)
        # One root -> exactly one dye action even though two cells change.
        self.assertEqual(trial.dye_actions, (DyeAction("A", "pink"),))
        counter.confirm("o")
        views = {v.cell: v.front_colour for v in counter.all_views("w1")}
        self.assertEqual(views, {"L1": "pink", "R1": "pink"})

    def test_dye_is_fixed_until_removal_day_and_then_lifts(self):
        counter = counter_with()
        order = Order(
            "o",
            "w1",
            (CellRequirement("L1", "pink"), CellRequirement("R1", "black")),
            frozenset(),
            removal_day=2,
            dye_allowed_roots=frozenset({"A"}),
        )
        counter.place_order(order)
        counter.confirm("o")
        self.assertEqual(counter.front_view("w1", "L1", day=2).front_colour, "pink")
        report = counter.return_wig("o", day=2)
        self.assertEqual(report.cleared_dyes, ("A",))
        self.assertEqual(counter.front_view("w1", "L1", day=2).front_colour, "red")
        with self.assertRaises(WorkshopError):
            counter.return_wig("o", day=1)

    def test_fixed_dye_blocks_a_different_colour(self):
        counter = counter_with()
        order = Order(
            "o",
            "w1",
            (CellRequirement("L1", "pink"), CellRequirement("R1", "black")),
            frozenset(),
            removal_day=4,
            dye_allowed_roots=frozenset({"A"}),
        )
        counter.place_order(order)
        counter.confirm("o")
        other = Order(
            "o2",
            "w1",
            (CellRequirement("L1", "blue"), CellRequirement("R1", "black")),
            frozenset(),
            removal_day=4,
            dye_allowed_roots=frozenset({"A"}),
        )
        counter.place_order(other)
        self.assertFalse(counter.fit("o2").feasible)

    def test_piece_needs_an_exposed_slot_and_fitting_length(self):
        # Slot length 5 rejects a piece of length 9.
        text = BASE.replace("S A s1 10", "S A s1 5")
        counter = StylingCounter(RevisionLibrary(), PieceInventory([Piece("p", "pink", 9)]))
        counter.register_wig("w1", body(text))
        order = Order(
            "o",
            "w1",
            (CellRequirement("L1", "pink"), CellRequirement("R1", "black")),
            frozenset(),
            removal_day=2,
            clip_allowed_roots=frozenset({"A"}),
        )
        counter.place_order(order)
        self.assertFalse(counter.fit("o").feasible)

    def test_piece_cannot_serve_a_second_wig_before_return(self):
        counter = counter_two_wigs([Piece("p", "pink", 5, wig_id="w1")])
        order1 = Order(
            "o1",
            "w1",
            (CellRequirement("L1", "pink"), CellRequirement("R1", "black")),
            frozenset(),
            removal_day=2,
            clip_allowed_roots=frozenset({"A"}),
        )
        counter.place_order(order1)
        counter.confirm("o1")
        order2 = Order(
            "o2",
            "w2",
            (CellRequirement("L1", "pink"), CellRequirement("R1", "black")),
            frozenset(),
            removal_day=2,
            clip_allowed_roots=frozenset({"A"}),
        )
        counter.place_order(order2)
        self.assertFalse(counter.fit("o2").feasible)
        counter.return_wig("o1", day=2)
        self.assertTrue(counter.inventory.available("p", "w1"))
        # Dedicated pieces never fit another wig even when free.
        self.assertFalse(counter.inventory.available("p", "w2"))


class ProtectedRootTests(unittest.TestCase):
    def test_protected_root_recomb_is_rejected_and_library_unchanged(self):
        counter = counter_with()
        counter.place_order(left_right_order("o1", protected={"B"}))
        before = len(counter.library)
        with self.assertRaises(WorkshopError):
            counter.recomb("w1", {"B": Placement.direct("B", "L", "L1", 0)})
        self.assertEqual(len(counter.library), before)
        self.assertEqual(counter.version("w1").version_id, 0)

    def test_protected_bytes_are_verified_on_return(self):
        counter = counter_with()
        counter.place_order(left_right_order("o1", protected={"B"}))
        # Re-combing A is allowed; B's lines are pinned by preserve patches
        # and its placements stay exactly where they were.  A moves to the
        # back of the right cell while B keeps its fixed outer order.
        report = counter.recomb(
            "w1",
            {
                "A": (
                    Placement.direct("A", "R", "L1", 0),
                    Placement.direct("A", "R", "R1", 1),
                )
            },
        )
        self.assertTrue(report.batch_preserves)
        self.assertEqual({c.root for c in report.changes}, {"A"})
        # Every published batch that touches the document preserves B bytes.
        original = counter.library.get(0).body
        current = counter.version("w1").body
        self.assertIn(b"B B L L1 1", original)
        self.assertIn(b"B B L L1 1", current)
        self.assertIn(b"B B R R1 0", current)
        back = counter.return_wig("o1", day=2)
        self.assertTrue(back.protected_verified)
        self.assertTrue(back.protected_byte_spans)


class LibraryIntegrationTests(unittest.TestCase):
    def test_recomb_publishes_real_versions_and_reopens(self):
        counter = counter_with()
        report = counter.recomb(
            "w1",
            {
                "A": (
                    Placement.direct("A", "L", "L1", 1),
                    Placement.direct("A", "R", "R1", 1),
                ),
                "B": (
                    Placement.direct("B", "L", "L1", 0),
                    Placement.direct("B", "R", "R1", 0),
                ),
            },
        )
        self.assertEqual(report.base_version, 0)
        self.assertEqual(report.new_version, 1)
        version = counter.version("w1")
        self.assertTrue(counter.library.verify(version.version_id))
        self.assertEqual(version.decode().text, version.text)

    def test_failed_recomb_publishes_nothing(self):
        counter = counter_with()
        # Two edits would collide on the same outer order of L1.
        with self.assertRaises(WorkshopError):
            counter.recomb(
                "w1",
                {
                    "A": (Placement.direct("A", "L", "L1", 0),),
                    "B": (Placement.direct("B", "L", "L1", 0),),
                },
            )
        self.assertEqual(len(counter.library), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
