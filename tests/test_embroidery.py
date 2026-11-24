"""Tests for the double-sided embroidery repair domain.

The fixtures stay inside the documented envelope: at most six holes, three
threads and two counter orders per screen.
"""

from __future__ import annotations

import pytest

from extcontract import IngestionLibrary
from extcontract.embroidery import (
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
    resolve_target,
    thread_row,
)
from extcontract.embroidery.reconstruction import AMBIGUOUS, CERTAIN

PIECE = "S-1"
HOLES = ["P", "Q", "R", "S", "T"]


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------

def _library_with(rows, file_id="F1", library=None, writer=None, reader=None):
    writer = writer or embroidery_contract(role="writer")
    reader = reader or embroidery_contract(role="reader")
    library = library or IngestionLibrary(reader)
    result = library.ingest(writer, registry_document(file_id, rows))
    assert result.accepted, result.errors
    return library, writer, reader


def _base_rows():
    return [
        # blue thread, starting end unknown: P-Q-R
        thread_row("T-B", PIECE, thread_id="B", color="blue", holes=["P", "Q", "R"],
                   sequences=[10, 11]),
        # red thread 1, starts on the front: Q-R-S => QR front, RS back
        thread_row("T-R1", PIECE, thread_id="R1", color="red", holes=["Q", "R", "S"],
                   sequences=[20, 21], start_face="front"),
        # red thread 2, starting end unknown: S-R-T
        thread_row("T-R2", PIECE, thread_id="R2", color="red", holes=["S", "R", "T"],
                   sequences=[30, 31]),
        # physical side views
        observation_row("O1", PIECE, color="blue", hole_a="P", hole_b="Q",
                        observed_face="back"),
        observation_row("O2", PIECE, color="red", hole_a="R", hole_b="S",
                        observed_face="back"),
        # collector circles: back PQ blue must be kept; back RS red must be kept
        protection_row("P-PQ", PIECE, thread_id="B", hole_a="P", hole_b="Q",
                       protected_face="back"),
        protection_row("P-RS", PIECE, thread_id="R1", hole_a="R", hole_b="S",
                       protected_face="back"),
        # no knot heads may show anywhere on the back
        noknot_row("N-BACK", PIECE, no_knot_face="back", zone_holes=list(HOLES)),
    ]


def _loops():
    # The brief's physical inventory: Q and R each carry exactly one loop, on
    # the front, where knot heads are allowed; the back is a no-knot zone.
    # P has no loop; S/T keep one loop per face for other repair work.
    return {
        "P": {"front": 0, "back": 0},
        "Q": {"front": 1, "back": 0},
        "R": {"front": 1, "back": 0},
        "S": {"front": 1, "back": 1},
        "T": {"front": 1, "back": 1},
    }


def _panel(registry=None):
    if registry is None:
        library, _, _ = _library_with(_base_rows())
        registry = registry_from_publish(library.publish(), piece_id=PIECE)
    return Panel.from_registry("panel-1", registry, _loops())


# ---------------------------------------------------------------------------
# ingestion: registry rows travel through the real IngestionLibrary
# ---------------------------------------------------------------------------

def test_registry_ingested_and_reconstructed_from_published_rows():
    library, _, _ = _library_with(_base_rows())
    registry = registry_from_publish(library.publish(), piece_id=PIECE)
    assert set(registry.threads) == {"B", "R1", "R2"}
    reconstruction = reconstruct(registry)
    assert reconstruction.satisfiable
    # B is forced to start on the back by O1 and the PQ protection circle.
    assert all(w.start_faces["B"] == "back" for w in reconstruction.worlds)
    # R2's unknown start yields two worlds; the variant where it starts on the
    # front cannot satisfy the RS-back observation (R1 would no longer be the
    # last-covering red RS-back segment), so it is excluded.
    r2_starts = {w.start_faces["R2"] for w in reconstruction.worlds}
    assert r2_starts == {"back", "front"}
    assert len(reconstruction.worlds) == 2
    # the red RT segment's face is precisely what the two worlds disagree on
    assert reconstruction.face_in_all_worlds("R2", 1) is None


def test_observation_claim_is_injective_same_colour_threads_cannot_share_one_view():
    panel = _panel()
    for world in panel.reconstruction.worlds:
        # one observation -> exactly one owner segment; no segment is ever
        # credited with the same view twice
        assert len(world.claims) == 2
        assert len(set(world.claims.values())) == 2
        # the two red threads never each claim O2 inside one world
        o2_owner = world.claims["O2"]
        assert o2_owner in [("R1", 1), ("R2", 0)]
    owners = {w.claims["O2"] for w in panel.reconstruction.worlds}
    assert owners == {("R1", 1), ("R2", 0)}

    # two identical observations of the same view: each gets its own owner
    # mapping but the same-coloured threads still cannot both claim one row
    rows = _base_rows() + [
        observation_row("O3", PIECE, color="red", hole_a="R", hole_b="S",
                        observed_face="back")
    ]
    library, _, _ = _library_with(rows, file_id="F-DUP-VIEW")
    registry = registry_from_publish(library.publish(), piece_id=PIECE)
    dup_worlds = reconstruct(registry).worlds
    for world in dup_worlds:
        assert world.claims["O2"] == world.claims["O3"]
        assert len(set(world.claims.values())) == 2


def test_duplicate_flow_sequence_rejected_at_registry_level():
    rows = _base_rows()
    rows[0] = thread_row("T-B", PIECE, thread_id="B", color="blue",
                         holes=["P", "Q", "R"], sequences=[20, 11])
    library, _, _ = _library_with(rows)
    with pytest.raises(Exception) as exc:
        registry_from_publish(library.publish(), piece_id=PIECE)
    assert "flow sequence 20" in str(exc.value)


def test_observation_revision_correction_goes_through_ingestion():
    library, writer, reader = _library_with(_base_rows())
    # correction: PQ is on the front
    corrected = registry_document(
        "F2",
        [observation_row("O1", PIECE, color="blue", hole_a="P", hole_b="Q",
                         observed_face="front", revision=2)],
    )
    assert library.ingest(writer, corrected).accepted
    registry = registry_from_publish(library.publish(), piece_id=PIECE)
    assert registry.row_revisions["O1"] == 2
    reconstruction = reconstruct(registry)
    # back-PQ protection can no longer hold in any threading
    assert not reconstruction.satisfiable


def test_same_revision_conflict_rejects_whole_file():
    library, writer, _ = _library_with(_base_rows())
    conflicting = registry_document(
        "F-BAD",
        [observation_row("O1", PIECE, color="blue", hole_a="P", hole_b="Q",
                         observed_face="front", revision=1)],
    )
    result = library.ingest(writer, conflicting)
    assert not result.accepted
    # the published, effective rows are untouched
    registry = registry_from_publish(library.publish(), piece_id=PIECE)
    assert reconstruct(registry).satisfiable


# ---------------------------------------------------------------------------
# target resolution: face + hole pair + colour, last-covering precedence
# ---------------------------------------------------------------------------

def test_blue_qr_front_resolves_certainly_to_blue_thread_segment_1():
    panel = _panel()
    resolution = resolve_target(
        panel.reconstruction, face="front", hole_a="Q", hole_b="R", color="blue"
    )
    assert resolution.status == CERTAIN
    assert resolution.key == ("B", 1)
    assert resolution.face == "front"


def test_same_colour_overlap_takes_greatest_full_screen_sequence():
    # two red threads both place a red QR segment on the front; the higher
    # flow sequence covers the place last and deterministically owns the view
    rows = [
        thread_row("T-A", PIECE, thread_id="A", color="red", holes=["Q", "R"],
                   sequences=[5], start_face="front"),
        thread_row("T-C", PIECE, thread_id="C", color="red", holes=["Q", "R"],
                   sequences=[9], start_face="front"),
        observation_row("O", PIECE, color="red", hole_a="Q", hole_b="R",
                        observed_face="front"),
    ]
    library, _, _ = _library_with(rows, file_id="F-OV")
    registry = registry_from_publish(library.publish(), piece_id=PIECE)
    panel = Panel.from_registry("p", registry, _loops())
    # all starts are fixed, so there is exactly one valid world
    assert len(panel.reconstruction.worlds) == 1
    assert panel.reconstruction.worlds[0].claims["O"] == ("C", 0)
    resolution = resolve_target(
        panel.reconstruction, face="front", hole_a="Q", hole_b="R", color="red"
    )
    assert resolution.status == CERTAIN and resolution.key == ("C", 0)
    # the lower-sequence same-coloured segment is recorded as a candidate but
    # does not own the place
    assert set(resolution.candidates) == {("A", 0), ("C", 0)}

    # if the high-sequence thread's start face is unknown and starts on the
    # back in some worlds, those worlds lose the front covering entirely and
    # the request can no longer be signed
    rows[1] = thread_row("T-C", PIECE, thread_id="C", color="red",
                         holes=["Q", "R"], sequences=[9])
    library2, _, _ = _library_with(rows, file_id="F-OV2")
    registry2 = registry_from_publish(library2.publish(), piece_id=PIECE)
    panel2 = Panel.from_registry("p2", registry2, _loops())
    assert len(panel2.reconstruction.worlds) == 2
    partial = resolve_target(
        panel2.reconstruction, face="front", hole_a="Q", hole_b="R", color="red"
    )
    assert partial.status == AMBIGUOUS


def test_unknown_face_worlds_make_target_ambiguous_and_order_is_held():
    # red R2 has an unknown start: an RT-back view exists only in some worlds
    panel = _panel()
    resolution = resolve_target(
        panel.reconstruction, face="back", hole_a="R", hole_b="T", color="red"
    )
    assert resolution.status == AMBIGUOUS
    order = CounterOrder("O-X", "back", "R", "T", "red")
    assert intake_order(panel, order).status == HELD


# ---------------------------------------------------------------------------
# counter intake: preservation circles and delivered faces
# ---------------------------------------------------------------------------

def test_protected_target_cannot_be_signed_even_when_location_is_certain():
    library, _, _ = _library_with(_base_rows())
    extra = registry_document(
        "F-EXTRA",
        [protection_row("P-QR", PIECE, thread_id="B", hole_a="Q", hole_b="R",
                        protected_face="front")],
    )
    writer = embroidery_contract(role="writer")
    reader = embroidery_contract(role="reader")
    library2 = IngestionLibrary(reader)
    assert library2.ingest(writer, registry_document("F1", _base_rows())).accepted
    assert library2.ingest(writer, extra).accepted
    registry = registry_from_publish(library2.publish(), piece_id=PIECE)
    panel = Panel.from_registry("p", registry, _loops())
    order = CounterOrder("O-A", "front", "Q", "R", "blue")
    decision = intake_order(panel, order)
    assert decision.status == HELD
    assert "do-not-touch" in decision.resolution.reasons[0]


def test_delivered_face_is_never_reopened():
    panel = _panel()
    order = CounterOrder("O-A", "front", "Q", "R", "blue")
    shop = WorkOrder("cut-front", CUT, frozenset({"front"}), ("scissors", "knot-silk"), "PERMIT-1")
    note = deliver(panel, order, shop)
    assert note.mode == CUT
    # re-intaking the same target now finds only a removed original stitch
    assert intake_order(panel, order).status == HELD
    with pytest.raises(RuntimeError):
        deliver(panel, order, shop)


# ---------------------------------------------------------------------------
# cut workshop: permits, loops, no-knot zones
# ---------------------------------------------------------------------------

def test_cut_shop_accepts_when_loops_and_permit_allow():
    panel = _panel()
    order = CounterOrder("O-A", "front", "Q", "R", "blue")
    shop = WorkOrder("cut-front", CUT, frozenset({"front"}), ("scissors",), "PERMIT-1")
    quote = evaluate(panel, order, shop)
    assert quote.status == ACCEPTED and quote.actionable
    assert quote.plan["knots"] == [
        {"hole": "Q", "face": "front"},
        {"hole": "R", "face": "front"},
    ]


def test_cut_shop_held_when_face_outside_permit():
    panel = _panel()
    order = CounterOrder("O-A", "front", "Q", "R", "blue")
    shop = WorkOrder("cut-back", CUT, frozenset({"back"}), ("scissors",), "PERMIT-2")
    quote = evaluate(panel, order, shop)
    assert quote.status == HELD and "permit" in quote.reasons[0]


def test_cut_shop_held_inside_no_knot_zone():
    # request the back PQ stitch without its protection circle, on a back face
    rows = [r for r in _base_rows() if r["row_id"] != "P-PQ"]
    library, _, _ = _library_with(rows, file_id="F-NK")
    registry = registry_from_publish(library.publish(), piece_id=PIECE)
    panel = Panel.from_registry("p", registry, _loops())
    order = CounterOrder("O-Z", "back", "P", "Q", "blue")
    shop = WorkOrder("cut-back", CUT, frozenset({"back"}), ("scissors",), "PERMIT-2")
    quote = evaluate(panel, order, shop)
    assert quote.status == HELD and "no-knot zone" in quote.reasons[0]


def test_cut_delivery_consumes_one_loop_per_knot_and_blocks_adjacent_order():
    panel = _panel()
    order_a = CounterOrder("O-A", "front", "Q", "R", "blue")
    order_b = CounterOrder("O-B", "front", "Q", "R", "red")
    cut = WorkOrder("cut-front", CUT, frozenset({"front"}), ("scissors",), "PERMIT-1")

    # before the first delivery the adjacent order still has loops to use
    assert evaluate(panel, order_b, cut).status == ACCEPTED

    note = deliver(panel, order_a, cut)
    assert panel.free_loop("Q", "front") == 0
    assert panel.free_loop("R", "front") == 0
    recheck = recheck_originals(panel, note)
    assert recheck["ok"]
    # last loops of the shared holes now belong to order A's knots
    later = evaluate(panel, order_b, cut)
    assert later.status == HELD and "free loop" in later.reasons[0]


# ---------------------------------------------------------------------------
# unthread workshop: reverse order, reachable tail, blocker receipt
# ---------------------------------------------------------------------------

def test_unthread_shop_blocked_by_protected_later_stitch():
    panel = _panel()
    # red R1: Q-R (front) target, R-S (back) is a protected later stitch
    order = CounterOrder("O-B", "front", "Q", "R", "red")
    shop = WorkOrder("unthread-1", UNTHREAD, frozenset({"front", "back"}),
                     ("tweezers",), "PERMIT-3")
    quote = evaluate(panel, order, shop)
    assert quote.status == BLOCKED
    assert quote.blocker_key == ("R1", 1)
    assert "protected" in quote.reasons[0]


def test_unthread_delivery_removes_suffix_and_keeps_prefix():
    # blue B: target QR (index 1) from reachable tail R; PQ stays preserved
    panel = _panel()
    order = CounterOrder("O-A", "front", "Q", "R", "blue")
    shop = WorkOrder("unthread-1", UNTHREAD, frozenset({"front", "back"}),
                     ("tweezers",), "PERMIT-3")
    quote = evaluate(panel, order, shop)
    assert quote.status == ACCEPTED
    note = deliver(panel, order, shop)
    assert note.removed_key == ("B", 1)
    assert panel.segment_exists(("B", 0))  # back PQ original stitch preserved
    assert not panel.segment_exists(("B", 1))
    recheck = recheck_originals(panel, note)
    assert recheck["ok"]
    held = {(c["thread_id"], tuple(c["holes"])) for c in recheck["preserved_circles_held"]}
    assert ("B", ("P", "Q")) in held and ("R1", ("R", "S")) in held


def test_unthread_plan_skips_stitches_already_removed_by_a_cut_delivery():
    # four-segment blue thread starting on the front (PQ front, QR back,
    # RS front, ST back).  First a cut removes the tail stitch ST (knots on
    # the back, where S and T stock loops), then the unthread shop reaches the
    # back-face middle QR: the plan skips the already-removed tail and the
    # unthreading stops at the target.
    rows = [
        thread_row("T-B", PIECE, thread_id="B", color="blue",
                   holes=["P", "Q", "R", "S", "T"], sequences=[10, 11, 12, 13],
                   start_face="front"),
        observation_row("O1", PIECE, color="blue", hole_a="P", hole_b="Q",
                        observed_face="front"),
    ]
    library, _, _ = _library_with(rows, file_id="F-MIX")
    registry = registry_from_publish(library.publish(), piece_id=PIECE)
    panel = Panel.from_registry("p", registry, _loops())
    cut = WorkOrder("cut-back", CUT, frozenset({"front", "back"}),
                    ("scissors",), "PERMIT-1")
    unthread = WorkOrder("unthread-1", UNTHREAD, frozenset({"front", "back"}),
                         ("tweezers",), "PERMIT-2")

    tail_order = CounterOrder("O-TAIL", "back", "S", "T", "blue")
    deliver(panel, tail_order, cut)
    middle_order = CounterOrder("O-MID", "back", "Q", "R", "blue")
    quote = evaluate(panel, middle_order, unthread)
    assert quote.status == ACCEPTED
    # index 3 is already gone from the reverse path; index 2 remains and is
    # undone first, then the target index 1
    assert quote.plan["reverse_order"] == [
        {"thread_id": "B", "index": 2},
        {"thread_id": "B", "index": 1},
    ]
    note = deliver(panel, middle_order, unthread)
    assert set(note.unthreaded_keys) == {("B", 1), ("B", 2)}
    assert panel.segment_exists(("B", 0))
    assert not panel.segment_exists(("B", 1)) and not panel.segment_exists(("B", 2))
    assert not panel.segment_exists(("B", 3))
    assert recheck_originals(panel, note)["ok"]


def test_work_order_requires_trade_supplies_and_permit_together():
    with pytest.raises(ValueError):
        WorkOrder("bad", CUT, frozenset({"front"}), (), "PERMIT-1")
    with pytest.raises(ValueError):
        WorkOrder("bad", CUT, frozenset({"front"}), ("scissors",), "")
    with pytest.raises(ValueError):
        WorkOrder("bad", "WEAVING", frozenset({"front"}), ("x",), "PERMIT-1")


def test_repaired_panel_accepts_next_order_on_other_faces_and_holes():
    panel = _panel()
    first = CounterOrder("O-A", "front", "Q", "R", "blue")
    cut_front = WorkOrder("cut-front", CUT, frozenset({"front"}), ("scissors",), "PERMIT-1")
    deliver(panel, first, cut_front)

    # the delivered blue target is gone and cannot be reopened
    assert intake_order(panel, first).status == HELD
    # the single front loops of Q and R are now occupied by the two knots
    assert panel.free_loop("Q", "front") == 0 and panel.free_loop("R", "front") == 0

    # the repaired piece is still live for work on untouched holes and loops
    assert panel.free_loop("S", "front") == 1 and panel.free_loop("T", "back") == 1
    next_order = CounterOrder("O-D", "front", "Q", "R", "red")
    decision = intake_order(panel, next_order)
    assert decision.status == ACCEPTED
    # the same shop cannot take it: the shared holes' last loops are gone
    assert evaluate(panel, next_order, cut_front).status == HELD
