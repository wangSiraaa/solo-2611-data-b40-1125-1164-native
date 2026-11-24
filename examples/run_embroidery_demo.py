"""End-to-end demonstration of the double-sided embroidery screen repair.

Run with:

    PYTHONPATH=. python3 examples/run_embroidery_demo.py

The story stays inside the counter envelope (five/six holes, three threads,
two counter orders) and walks through every beat of the brief:

  1. register the needle roads, side views, preservation circles and no-knot
     zone through the real ``IngestionLibrary.ingest`` revision machinery, then
     rebuild every simultaneously-valid threading from ``publish()``;
  2. resolve the counter's blue front QR request - same-coloured overlapping
     segments follow the last-covering full-screen sequence;
  3. correct the PQ side observation to "front": the original blue front QR
     order is no longer findable and is only held, never signed;
  4. restore the observation, then show a preservation circle blocks signing
     even when the location is judged correctly;
  5. the cut shop quotes, delivers and rechecks: the front blue error stitch
     is removed while the back PQ original thread is preserved;
  6. the adjacent order on the same holes must use the last loop of those
     holes, which the first delivery has consumed - it is held until recheck;
  7. on a fresh screen the unthread shop quotes the same target: a protected
     later stitch blocks reverse unthreading and a typed blocker receipt is
     issued; where the path is clear it undoes the suffix in reverse order;
  8. delivered faces are never reopened for later orders.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extcontract import IngestionLibrary  # noqa: E402
from extcontract.embroidery import (  # noqa: E402
    ACCEPTED,
    BLOCKED,
    CUT,
    HELD,
    UNTHREAD,
    CounterOrder,
    Panel,
    WorkOrder,
    deliver,
    embroidery_contract,
    evaluate,
    intake_order,
    noknot_row,
    observation_row,
    protection_row,
    reconstruct,
    registry_document,
    registry_from_publish,
    recheck_originals,
    thread_row,
)

PIECE = "S-1"
HOLES = ["P", "Q", "R", "S", "T"]


def banner(number: int, title: str) -> None:
    print(f"\n=== {number}. {title} ===")


def show(label: str, payload) -> None:
    if hasattr(payload, "to_json"):
        payload = payload.to_json()
    print(f"{label}: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}")


def base_rows():
    return [
        # blue thread, starting end unknown: P-Q-R, tail at R
        thread_row("T-B", PIECE, thread_id="B", color="blue",
                   holes=["P", "Q", "R"], sequences=[10, 11]),
        # red thread 1, starts front: QR front, RS back
        thread_row("T-R1", PIECE, thread_id="R1", color="red",
                   holes=["Q", "R", "S"], sequences=[20, 21], start_face="front"),
        # red thread 2, starting end unknown: S-R-T
        thread_row("T-R2", PIECE, thread_id="R2", color="red",
                   holes=["S", "R", "T"], sequences=[30, 31]),
        # physical side observations
        observation_row("O1", PIECE, color="blue", hole_a="P", hole_b="Q",
                        observed_face="back"),
        observation_row("O2", PIECE, color="red", hole_a="R", hole_b="S",
                        observed_face="back"),
        # collector circles
        protection_row("P-PQ", PIECE, thread_id="B", hole_a="P", hole_b="Q",
                       protected_face="back"),
        protection_row("P-RS", PIECE, thread_id="R1", hole_a="R", hole_b="S",
                       protected_face="back"),
        # no knot heads may show on the back at any named hole
        noknot_row("N-BACK", PIECE, no_knot_face="back", zone_holes=list(HOLES)),
    ]


def one_loop_each():
    # Brief-exact physical inventory: Q and R each have exactly one loop, on
    # the front where knot heads are allowed; P has no loop and the back is a
    # no-knot zone.  S and T keep one loop per face for other repair work.
    return {
        "P": {"front": 0, "back": 0},
        "Q": {"front": 1, "back": 0},
        "R": {"front": 1, "back": 0},
        "S": {"front": 1, "back": 1},
        "T": {"front": 1, "back": 1},
    }


def build_panel():
    writer = embroidery_contract(role="writer")
    reader = embroidery_contract(role="reader")
    library = IngestionLibrary(reader)
    accepted = library.ingest(writer, registry_document("F1", base_rows()))
    assert accepted.accepted, accepted.errors
    registry = registry_from_publish(library.publish(), piece_id=PIECE)
    return library, writer, reader, Panel.from_registry("panel-1", registry, one_loop_each())


def main() -> int:
    # 1. ingestion + reconstruction -----------------------------------------
    banner(1, "ingest registry rows and rebuild all simultaneously-valid threadings")
    library, writer, reader, panel = build_panel()
    print("published effective rows:", [r["row_id"] for r in library.publish()["rows"]])
    reconstruction = panel.reconstruction
    print("valid worlds:", len(reconstruction.worlds))
    for i, world in enumerate(reconstruction.worlds):
        print(
            f"  world {i}: start_faces={dict(sorted(world.start_faces.items()))} "
            f"claims={dict(sorted(world.claims.items()))}"
        )
    assert len(reconstruction.worlds) == 2
    assert all(w.claims["O1"] == ("B", 0) for w in reconstruction.worlds)
    # the two red threads never each claim O2 inside one world
    assert all(len(set(w.claims.values())) == len(w.claims) for w in reconstruction.worlds)

    # 2. counter target resolution ------------------------------------------
    banner(2, "counter request: blue segment between Q and R on the front")
    blue_qr = CounterOrder("ORD-1", "front", "Q", "R", "blue")
    intake = intake_order(panel, blue_qr)
    show("intake", intake)
    assert intake.status == ACCEPTED and intake.target_key == ("B", 1)

    # 3. corrected observation: PQ is actually on the front -----------------
    banner(3, "correction F2: PQ observed on the front - the old order loses its target")
    correction = registry_document(
        "F2",
        [observation_row("O1", PIECE, color="blue", hole_a="P", hole_b="Q",
                         observed_face="front", revision=2)],
    )
    assert library.ingest(writer, correction).accepted
    corrected_registry = registry_from_publish(library.publish(), piece_id=PIECE)
    print("O1 effective revision:", corrected_registry.row_revisions["O1"])
    corrected = reconstruct(corrected_registry)
    print("satisfiable after correction:", corrected.satisfiable, "-", corrected.notes)
    assert not corrected.satisfiable
    corrected_panel = Panel.from_registry("panel-1b", corrected_registry, one_loop_each())
    lost = intake_order(corrected_panel, blue_qr)
    show("corrected intake", lost)
    assert lost.status == HELD

    # 4. restore the observation; a preservation circle blocks signing ------
    banner(4, "restore O1, then circle front QR as do-not-touch")
    restore = registry_document(
        "F3",
        [observation_row("O1", PIECE, color="blue", hole_a="P", hole_b="Q",
                         observed_face="back", revision=3)],
    )
    assert library.ingest(writer, restore).accepted
    protected_library = IngestionLibrary(reader)
    assert protected_library.ingest(
        writer, registry_document("F1", base_rows())
    ).accepted
    assert protected_library.ingest(
        writer,
        registry_document(
            "F-EXTRA",
            [protection_row("P-QR", PIECE, thread_id="B", hole_a="Q", hole_b="R",
                            protected_face="front")],
        ),
    ).accepted
    protected_registry = registry_from_publish(protected_library.publish(), piece_id=PIECE)
    protected_panel = Panel.from_registry("panel-1c", protected_registry, one_loop_each())
    blocked_intake = intake_order(protected_panel, blue_qr)
    show("protected intake", blocked_intake)
    assert blocked_intake.status == HELD and "do-not-touch" in blocked_intake.resolution.reasons[0]

    # 5. cut shop: quote, deliver, recheck originals ------------------------
    banner(5, "cut shop delivers ORD-1; back PQ original thread is preserved")
    cut_shop = WorkOrder(
        "cut-front-house", CUT, frozenset({"front"}),
        ("small scissors", "knot silk"), "PERMIT-CUT-01",
    )
    show("work order", cut_shop)
    quote = evaluate(panel, blue_qr, cut_shop)
    show("quote", quote)
    assert quote.actionable
    note = deliver(panel, blue_qr, cut_shop)
    show("delivery note", note)
    recheck = recheck_originals(panel, note)
    show("original recheck", recheck)
    assert recheck["ok"]
    assert panel.segment_exists(("B", 0)) and not panel.segment_exists(("B", 1))
    assert panel.free_loop("Q", "front") == 0 and panel.free_loop("R", "front") == 0

    # 6. adjacent order needs the last loop of the same holes ---------------
    banner(6, "adjacent red QR order after delivery: last loops are already taken")
    red_qr = CounterOrder("ORD-2", "front", "Q", "R", "red")
    show("adjacent intake", intake_order(panel, red_qr))
    adjacent = evaluate(panel, red_qr, cut_shop)
    show("adjacent quote", adjacent)
    assert adjacent.status == HELD and "free loop" in adjacent.reasons[0]

    # 7. unthread shop on a fresh screen ------------------------------------
    banner(7, "unthread shop: protected later stitch issues a blocker receipt")
    _, _, _, fresh = build_panel()
    unthread_shop = WorkOrder(
        "unthread-house", UNTHREAD, frozenset({"front", "back"}),
        ("reverse tweezers",), "PERMIT-UNTHREAD-01",
    )
    show("work order", unthread_shop)
    red_middle = CounterOrder("ORD-R", "front", "Q", "R", "red")
    blocked_quote = evaluate(fresh, red_middle, unthread_shop)
    show("blocked quote", blocked_quote)
    assert blocked_quote.status == BLOCKED and blocked_quote.blocker_key == ("R1", 1)

    # with the protected later circle lifted, the same trade can undo the
    # blue suffix (QR) from the reachable tail R while keeping back PQ.
    unprotected_rows = [r for r in base_rows() if r["row_id"] != "P-RS"]
    open_library = IngestionLibrary(reader)
    assert open_library.ingest(
        writer, registry_document("F1-OPEN", unprotected_rows)
    ).accepted
    open_registry = registry_from_publish(open_library.publish(), piece_id=PIECE)
    open_panel = Panel.from_registry("panel-2", open_registry, one_loop_each())
    open_quote = evaluate(open_panel, blue_qr, unthread_shop)
    show("open-path quote", open_quote)
    assert open_quote.actionable
    unthread_note = deliver(open_panel, blue_qr, unthread_shop)
    show("unthread delivery", unthread_note)
    assert unthread_note.unthreaded_keys == (("B", 1),)
    assert open_panel.segment_exists(("B", 0))
    assert recheck_originals(open_panel, unthread_note)["ok"]

    # 8. delivered faces are never reopened ---------------------------------
    banner(8, "a delivered embroidery face cannot be reopened for a later order")
    show("repeat intake", intake_order(panel, blue_qr))
    try:
        deliver(panel, blue_qr, cut_shop)
    except RuntimeError as exc:
        print("re-delivery refused:", exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("re-delivery must be refused")

    print("\nAll embroidery demo assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
