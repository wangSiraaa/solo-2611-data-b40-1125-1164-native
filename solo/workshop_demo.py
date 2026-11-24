"""End-to-end demonstration of the wig workshop styling counter.

Run:  python3 -m solo.workshop_demo

It walks the three acceptance situations from the task statement against a
real :class:`RevisionLibrary`, printing the front/back visibility, the
re-combing spans (text + original bytes) and the dye/rental/return evidence.
"""

from __future__ import annotations

from .codec import encode_text
from .revision import RevisionLibrary
from .workshop import (
    CellRequirement,
    Order,
    Piece,
    PieceInventory,
    Placement,
    StylingCounter,
)

PATTERN = (
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

SWAP_PARTING = {
    "B": (
        Placement.direct("B", "L", "L1", 0),
        Placement.direct("B", "R", "R1", 0),
    ),
    "A": (
        Placement.direct("A", "L", "L1", 1),
        Placement.direct("A", "R", "R1", 1),
    ),
}

ORIGINAL_PARTING = {
    "B": (
        Placement.direct("B", "L", "L1", 1),
        Placement.direct("B", "R", "R1", 0),
    ),
    "A": (
        Placement.direct("A", "L", "L1", 0),
        Placement.direct("A", "R", "R1", 1),
    ),
}


def show(counter: StylingCounter, title: str) -> None:
    print(f"-- {title}")
    for view in counter.all_views("w1"):
        back = ", ".join(f"{root}={colour}" for root, colour in view.back_only) or "-"
        print(
            f"   cell {view.cell}: front={view.front_root}:{view.front_colour} "
            f"back-only=[{back}]"
        )


def main() -> int:
    print("== scenario 1: as-is left-red / right-black ==")
    counter = StylingCounter(RevisionLibrary())
    counter.register_wig("w1", encode_text(PATTERN).data)
    order = Order(
        "o1",
        "w1",
        (CellRequirement("L1", "red"), CellRequirement("R1", "black")),
        frozenset(),
        removal_day=2,
        as_is_only=True,
    )
    counter.place_order(order)
    trial = counter.fit("o1")
    print(f"   feasible={trial.feasible} reason={trial.reason}")
    show(counter, "original combing")

    print("\n== scenario 2a: re-comb only, B covers A on the left ==")
    report = counter.recomb("w1", SWAP_PARTING)
    print(f"   published v{report.base_version} -> v{report.new_version}")
    for change in report.changes:
        print(
            f"   root {change.root} {change.old_placement.cell}:{change.old_placement.order} "
            f"-> {change.new_placement.cell}:{change.new_placement.order} "
            f"chars={change.char_span} bytes={change.byte_span}"
        )
    show(counter, "after the parting change")
    verdicts = counter.review("w1", "o1")
    print("   review:", [(v.cell, v.wanted, v.seen, v.ok) for v in verdicts])

    print("\n== scenario 2b: restore the combing, then order all-red unapproved ==")
    counter.recomb("w1", ORIGINAL_PARTING)
    counter.place_order(
        Order(
            "o2",
            "w1",
            (CellRequirement("L1", "red"), CellRequirement("R1", "red")),
            frozenset(),
            removal_day=2,
        )
    )
    trial = counter.fit("o2")
    print(f"   feasible={trial.feasible} reason={trial.reason}")

    print("\n== scenario 3: approved dye/clip on A, next-day parting change ==")
    counter3 = StylingCounter(
        RevisionLibrary(),
        PieceInventory([Piece("p-pink", "pink", 8), Piece("p-red", "red", 8)]),
    )
    counter3.register_wig("w1", encode_text(PATTERN).data)
    counter3.place_order(
        Order(
            "o3",
            "w1",
            (CellRequirement("L1", "pink"), CellRequirement("R1", "black")),
            frozenset(),
            removal_day=2,
            dye_allowed_roots=frozenset({"A"}),
            clip_allowed_roots=frozenset({"A"}),
        )
    )
    clip_trial = counter3.fit("o3")
    print(f"   clip trial feasible={clip_trial.feasible} actions={clip_trial.clip_actions}")
    counter3.confirm("o3")
    show(counter3, "clip confirmed")
    counter3.advance_day(1)
    buried = counter3.recomb("w1", SWAP_PARTING)
    print(f"   hidden rentals after parting change: {buried.hidden_rentals}")
    show(counter3, "day 2, B covers A")
    back = counter3.return_wig("o3", day=2)
    print(f"   returned pieces={back.returned_pieces}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
