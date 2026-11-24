"""Tests for the 发行柜/补量柜: contracts, ingestion, crediting, makeup orders.

The walk-through follows the mandated story exactly:

  * four mailboxes, two rounds of two, intersection one -> three guaranteed;
  * correcting the intersection to two drops the guarantee to two;
  * restoring it and raising the contract to four leaves a one-household gap;
  * the targeted cabinet waits for 丁's named non-receipt, while the
    whole-line cabinet may sign L on its added lower bound of one;
  * the cheap 甲乙 line cannot guarantee the gap;
  * the last remaining print slot goes to another contract;
  * replaying a delivery receipt never double-credits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from extcontract import PaperCabinet, Route, load_contract

MATERIALS = Path("materials/paper")
UNIVERSE = ["甲", "乙", "丙", "丁"]
ROUTES = [
    Route("L", "B1", frozenset(UNIVERSE)),
    Route("M", "B1", frozenset(["甲", "乙"])),
    Route("S", "B2", frozenset(["戊"])),
]


@pytest.fixture()
def contracts():
    writer = load_contract(MATERIALS / "delivery-writer-contract.json", role="writer")
    reader = load_contract(MATERIALS / "delivery-reader-contract.json", role="reader")
    return writer, reader


def station_doc(name: str) -> dict:
    return json.loads((MATERIALS / name).read_text(encoding="utf-8"))


def make_cabinet(contracts, mode, *, slots=5, universes=None, routes=None):
    writer, reader = contracts
    return PaperCabinet(
        reader,
        writer,
        mode=mode,
        edition_slots=slots,
        universes=universes or {"B1": UNIVERSE, "B2": ["戊"]},
        routes=routes if routes is not None else ROUTES,
    )


def c1(guarantee: int, *, permitted=None) -> dict:
    return {
        "contract_no": "C1",
        "block": "B1",
        "guarantee": guarantee,
        "permitted": UNIVERSE if permitted is None else permitted,
        "makeup_edition": "mk-1",
        "deadline": "2026-12-01",
    }


# ---------------------------------------------------------------------------
# Station files flow through the original ingestion library
# ---------------------------------------------------------------------------

def test_station_file_goes_through_original_ingest_and_publish(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    result = cabinet.receive_station_file(station_doc("D-BASE.json"))
    assert result.accepted
    assert result.ingest_result["status"] == "ACCEPTED"
    published = cabinet.library.publish()
    assert published["format"] == "reader-records/v1"
    assert {row["row_id"] for row in published["rows"]} == {
        "wd-size",
        "we-size",
        "wd-we-intersect",
    }
    # INT8 writer values were widened to DECIMAL(3,0) reader strings.
    sample = next(row for row in published["rows"] if row["row_id"] == "wd-size")
    assert sample["values"]["qty"] == "2"
    assert sample["sources"]["qty"]["conversion"] == "int_to_decimal_exact"


def test_rejected_station_file_changes_nothing(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    bad = {
        "file_id": "D-BAD",
        "writer_version": "dw1",
        "reader_version": "dr1",
        "rows": [
            {"row_id": "x", "values": {
                "block": "B1", "edition": "workday", "round": 1,
                "measure": "set_size", "household": None, "qty": 999}},
        ],
    }  # qty 999 outside INT8 writer domain
    result = cabinet.receive_station_file(bad)
    assert not result.accepted
    assert cabinet.library.row_count() == 0
    assert cabinet.last_receipt("D-BAD") is None


def test_replaying_station_file_returns_replay_and_does_not_recredit(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    first = cabinet.receive_station_file(station_doc("D-BASE.json"))
    again = cabinet.receive_station_file(station_doc("D-BASE.json"))
    assert again.ingest_result["receipt"]["replay"] is True
    assert again.ingest_result["receipt"]["sequence"] == first.ingest_result["receipt"]["sequence"]
    assert cabinet.library.row_count() == 3


# ---------------------------------------------------------------------------
# Guaranteed crediting across the four scenarios
# ---------------------------------------------------------------------------

def test_guarantee_three_is_credited_then_intersection_fix_drops_to_two(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.register_contract(c1(3))
    accounting = cabinet.accounting("C1")
    assert accounting.data["credited"] == 3
    assert accounting.data["residual_gap"] == 0

    # Station corrects the intersection from one to two.
    cabinet.receive_station_file(station_doc("D-FIX2.json"))
    accounting = cabinet.accounting("C1")
    assert accounting.data["delivery_lower_bound"] == 2
    assert accounting.data["credited"] == 2
    assert accounting.data["residual_gap"] == 1

    # Restore the original record (higher revision back to one).
    cabinet.receive_station_file(station_doc("D-FIX1.json"))
    assert cabinet.accounting("C1").data["delivery_lower_bound"] == 3


def test_contract_raised_to_four_leaves_a_one_household_gap(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.receive_station_file(station_doc("D-FIX1.json"))
    cabinet.register_contract(c1(4))
    accounting = cabinet.accounting("C1")
    assert accounting.data["credited"] == 3
    assert accounting.data["residual_gap"] == 1


def test_every_household_can_be_the_misser_before_named_receipt(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    verdict = cabinet.settle().data["blocks"]["B1"]
    assert set(verdict["possible_missers"]) == set(UNIVERSE)


# ---------------------------------------------------------------------------
# Whole-line cabinet
# ---------------------------------------------------------------------------

def test_whole_line_signs_L_on_added_lower_bound_one(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.receive_station_file(station_doc("D-FIX1.json"))
    cabinet.register_contract(c1(4))
    result = cabinet.sign_whole_line("C1", "L")
    assert result.accepted
    order = result.data["order"]
    assert order["slots"] == 4
    assert order["basis"]["added_lower_bound"] == 1
    assert result.data["slot_state"]["available"] == 1


def test_cheap_line_M_cannot_guarantee_the_gap(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.receive_station_file(station_doc("D-FIX1.json"))
    cabinet.register_contract(c1(4))
    result = cabinet.sign_whole_line("C1", "M")
    assert not result.accepted
    assert "added lower bound 0" in result.errors[0]


def test_line_outside_permission_is_refused(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.register_contract(c1(4, permitted=["甲", "乙", "丙"]))  # 丁 not permitted
    result = cabinet.sign_whole_line("C1", "L")
    assert not result.accepted
    assert any("丁" in e for e in result.errors)


def test_sealed_line_occupies_real_slots_and_delivery_closes_gap(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.receive_station_file(station_doc("D-FIX1.json"))
    cabinet.register_contract(c1(4))
    order_id = cabinet.sign_whole_line("C1", "L").data["order"]["order_id"]
    sealed = cabinet.seal_edition("mk-1")
    assert sealed.data["slot_state"]["sealed_real_slots"] == 4
    assert sealed.data["slot_state"]["available"] == 1
    delivery = cabinet.deliver(order_id)
    assert delivery.data["receipt"]["kind"] == "whole_line_delivery_receipt"
    accounting = cabinet.accounting("C1")
    assert accounting.data["projected_lower_bound"] == 4
    assert accounting.data["credited"] == 4
    assert accounting.data["residual_gap"] == 0


def test_delivery_is_idempotent_and_never_double_credits(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.register_contract(c1(4))
    order_id = cabinet.sign_whole_line("C1", "L").data["order"]["order_id"]
    cabinet.seal_edition("mk-1")
    first = cabinet.deliver(order_id)
    second = cabinet.deliver(order_id)
    assert first.accepted and second.accepted
    assert "replayed" in second.message
    assert cabinet.accounting("C1").data["credited"] == 4


def test_second_contract_takes_the_remaining_slot_then_edition_is_full(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.register_contract(c1(4))
    cabinet.sign_whole_line("C1", "L")
    cabinet.seal_edition("mk-1")
    cabinet.register_contract({
        "contract_no": "C2", "block": "B2", "guarantee": 1,
        "permitted": ["戊"], "makeup_edition": "mk-1",
    })
    signed = cabinet.sign_whole_line("C2", "S")
    assert signed.accepted
    assert signed.data["slot_state"]["available"] == 0
    cabinet.register_contract({
        "contract_no": "C3", "block": "B2", "guarantee": 1,
        "permitted": ["戊"], "makeup_edition": "mk-1",
    })
    overflow = cabinet.sign_whole_line("C3", "S")
    assert not overflow.accepted
    assert "available" in overflow.errors[0]


def test_sealed_order_keeps_its_basis_when_evidence_changes(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.register_contract(c1(4))
    order_id = cabinet.sign_whole_line("C1", "L").data["order"]["order_id"]
    cabinet.seal_edition("mk-1")
    cabinet.deliver(order_id)
    # Evidence later changes (intersection corrected to two): the historical
    # basis of the sealed order is retained by rule.
    cabinet.receive_station_file(station_doc("D-FIX2.json"))
    check = cabinet.basis_still_holds(order_id)
    assert check.data["frozen"] is True
    assert check.data["holds"] is True
    assert cabinet.withdraw(order_id).accepted is False


# ---------------------------------------------------------------------------
# Targeted cabinet
# ---------------------------------------------------------------------------

def test_targeted_cabinet_waits_for_named_non_receipt(contracts):
    cabinet = make_cabinet(contracts, "targeted")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.register_contract(c1(4))
    early = cabinet.sign_targeted("C1", "丁")
    assert not early.accepted
    assert "named non-receipt" in early.errors[0]
    cabinet.receive_station_file(station_doc("D-DING-MISS.json"))
    ready = cabinet.sign_targeted("C1", "丁")
    assert ready.accepted
    assert ready.data["order"]["target_household"] == "丁"


def test_targeted_refuses_household_without_evidence_and_wrong_mode(contracts):
    cabinet = make_cabinet(contracts, "targeted")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.receive_station_file(station_doc("D-DING-MISS.json"))
    cabinet.register_contract(c1(4))
    assert not cabinet.sign_targeted("C1", "甲").accepted
    # A targeted cabinet never accepts whole-line orders, and vice versa.
    assert not cabinet.sign_whole_line("C1", "L").accepted
    line_cabinet = make_cabinet(contracts, "whole_line")
    line_cabinet.register_contract(c1(4))
    assert not line_cabinet.sign_targeted("C1", "丁").accepted


def test_unsealed_targeted_order_can_be_withdrawn_after_evidence_changes(contracts):
    cabinet = make_cabinet(contracts, "targeted")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.receive_station_file(station_doc("D-DING-MISS.json"))
    cabinet.register_contract(c1(4))
    order_id = cabinet.sign_targeted("C1", "丁").data["order"]["order_id"]
    # Retract 丁's non-receipt at a higher revision (now claimed delivered).
    cabinet.receive_station_file({
        "file_id": "D-DING-RETRACT",
        "writer_version": "dw1",
        "reader_version": "dr1",
        "rows": [
            {"row_id": "ding-miss-wd", "revision": 2, "values": {
                "block": "B1", "edition": "workday", "round": 2,
                "measure": "named_delivered", "household": "丁", "qty": None}},
            {"row_id": "ding-miss-we", "revision": 2, "values": {
                "block": "B1", "edition": "weekend", "round": 2,
                "measure": "named_delivered", "household": "丁", "qty": None}},
        ],
    })
    check = cabinet.basis_still_holds(order_id)
    assert check.data["holds"] is False
    withdrawn = cabinet.withdraw(order_id)
    assert withdrawn.accepted
    assert withdrawn.data["slot_state"]["available"] == 5


def test_targeted_delivery_produces_named_receipt_and_credit(contracts):
    cabinet = make_cabinet(contracts, "targeted")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.receive_station_file(station_doc("D-DING-MISS.json"))
    cabinet.register_contract(c1(4))
    order_id = cabinet.sign_targeted("C1", "丁").data["order"]["order_id"]
    cabinet.seal_edition("mk-1")
    delivery = cabinet.deliver(order_id)
    assert delivery.data["receipt"]["kind"] == "named_delivery_receipt"
    assert delivery.data["receipt"]["household"] == "丁"
    assert cabinet.accounting("C1").data["credited"] == 4


# ---------------------------------------------------------------------------
# Deadline gate, read-back, persistence
# ---------------------------------------------------------------------------

def test_deadline_blocks_new_signing_after_cutoff(contracts):
    clock = iter(["2026-12-02"])
    writer, reader = contracts
    cabinet = PaperCabinet(
        reader, writer, mode="whole_line", edition_slots=5,
        universes={"B1": UNIVERSE}, routes=ROUTES, clock=lambda: next(clock),
    )
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.register_contract(c1(4))
    result = cabinet.sign_whole_line("C1", "L")
    assert not result.accepted
    assert "deadline" in result.errors[0]


def test_readback_by_contract_number_reports_edition_services_and_basis(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.register_contract(c1(4))
    order_id = cabinet.sign_whole_line("C1", "L").data["order"]["order_id"]
    readback = cabinet.readback("C1")
    assert readback.data["makeup_edition_used"] == "mk-1"
    assert readback.data["contract"]["guarantee"] == 4
    assert readback.data["orders"][0]["order_id"] == order_id
    assert "basis" in readback.data["orders"][0]
    # Read-back after replaying the previous delivery receipt still matches.
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    again = cabinet.readback("C1")
    assert again.data["orders"][0]["order_id"] == order_id


def test_state_persistence_round_trip_keeps_orders_slots_and_history(contracts):
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.receive_station_file(station_doc("D-FIX1.json"))
    cabinet.register_contract(c1(4))
    order_id = cabinet.sign_whole_line("C1", "L").data["order"]["order_id"]
    cabinet.seal_edition("mk-1")
    cabinet.deliver(order_id)

    writer, reader = contracts
    restored = PaperCabinet.from_state(cabinet.to_state(), reader, writer)
    # Station history is replayed through the real ingest path.
    assert restored.library.row_count() == 3
    assert restored.settle().data["blocks"]["B1"]["lower_bound"] == 3
    assert restored.slot_state("mk-1")["sealed_real_slots"] == 4
    restored_order = restored.orders[order_id]
    assert restored_order.state == "delivered"
    assert restored_order.basis["added_lower_bound"] == 1
    assert restored.accounting("C1").data["credited"] == 4


def test_next_order_may_change_block_permit_and_lines(contracts):
    """The local entry repeatedly accepts new, independently-scoped orders."""
    cabinet = make_cabinet(contracts, "whole_line")
    cabinet.receive_station_file(station_doc("D-BASE.json"))
    cabinet.register_contract(c1(3, permitted=["甲", "乙"]))
    cabinet.register_contract({
        "contract_no": "C2", "block": "B2", "guarantee": 1,
        "permitted": ["戊"], "makeup_edition": "mk-2",
    })
    assert cabinet.readback("C1").data["contract"]["block"] == "B1"
    assert cabinet.readback("C2").data["contract"]["block"] == "B2"
    # Different block, different permit, different line on the same cabinet.
    signed = cabinet.sign_whole_line("C2", "S")
    assert signed.accepted
    assert signed.data["order"]["block"] == "B2"
