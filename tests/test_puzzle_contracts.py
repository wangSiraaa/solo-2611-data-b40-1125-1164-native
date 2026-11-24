"""Contracts and ingest-path tests for the loose-piece photo ledger.

The photo interpretation records ride the project's unchanged contract /
ingestion machinery; these tests pin the field contract, the SAFE adaptation,
revision handling on re-photographs and the rule that a corrected piece keeps
its original piece number.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from extcontract import (
    IngestionLibrary,
    check_compatibility,
    load_contract,
)
from extcontract.puzzle import (
    READER_VERSION,
    UNREAD,
    back_code_correction,
    photo_file,
    photo_reader_contract,
    photo_row,
    photo_writer_contract,
)

MATERIALS = Path("materials/puzzle")


@pytest.fixture()
def writer():
    return photo_writer_contract()


@pytest.fixture()
def reader():
    return photo_reader_contract()


@pytest.fixture()
def library(reader):
    return IngestionLibrary(reader)


def _edges():
    return {"n": "F", "e": "T", "s": "F", "w": "F"}


def test_json_contracts_match_builder_contracts():
    disk_writer = load_contract(MATERIALS / "writer-contract.json", role="writer")
    disk_reader = load_contract(MATERIALS / "reader-contract.json", role="reader")
    assert disk_writer.canonical_json() == photo_writer_contract().canonical_json()
    assert disk_reader.canonical_json() == photo_reader_contract().canonical_json()


def test_photo_contract_pair_is_safe_and_reader_only_fields_default(writer, reader):
    report = check_compatibility(writer, reader)
    assert report.safe, [c.reasons for c in report.columns if not c.safe]
    plan = report.adaptation_plan()
    fills = {step["field_id"]: step for step in plan["steps"]}
    assert fills["photo_id"]["source"] == "default"
    assert fills["note"]["source"] == "default"
    # Every writer field pairs by field_id with a reader column.
    writer_ids = {f.field_id for f in writer.fields}
    reader_ids = {f.field_id for f in reader.fields}
    assert writer_ids <= reader_ids
    assert "back_code" in writer_ids  # the workstation does report back codes


def test_ingest_photo_rows_and_publish_provenance(library, writer):
    row = photo_row(
        "P1",
        face_zone="ZA",
        edges=_edges(),
        orientations=[0, 180],
        photo_edition="X/Y",
    )
    result = library.ingest(writer, photo_file("photo-1", [row]))
    assert result.accepted, result.errors

    published = library.publish()
    assert published["reader_version"] == READER_VERSION
    record = published["rows"][0]
    assert record["row_id"] == "P1"
    assert record["values"]["orientations"] == "0|180"
    assert record["values"]["back_code"] == UNREAD
    # Reader-only defaults and the writer provenance are both retained.
    assert record["values"]["photo_id"] == "unknown-photo"
    assert record["sources"]["back_code"]["conversion"] == "text_identity"
    assert record["sources"]["note"]["conversion"] == "fill_default"


def test_rephotograph_keeps_piece_id_and_highest_revision_wins(library, writer):
    first = photo_row(
        "P1", face_zone="ZA", edges=_edges(), orientations=[0],
        photo_edition="X/Y",
    )
    assert library.ingest(writer, photo_file("photo-1", [first])).accepted

    corrected = back_code_correction(
        "P1",
        revision=2,
        back_code="X",
        previous=first,
    )
    result = library.ingest(writer, photo_file("photo-2", [corrected]))
    assert result.accepted, result.errors

    state = library.get_row("P1")
    assert state.row_id == "P1"                 # original piece number retained
    assert state.revision == 2
    assert state.encoded["back_code"] == "X"
    # The photograph facts survive the back-code correction unchanged.
    assert state.encoded["face_zone"] == "ZA"


def test_same_revision_conflict_rejects_whole_file(library, writer):
    first = photo_row(
        "P1", face_zone="ZA", edges=_edges(), orientations=[0],
        photo_edition="X/Y", back_code="X",
    )
    library.ingest(writer, photo_file("photo-1", [first]))
    conflicting = {
        "row_id": "P1",
        "revision": 1,
        "values": {**first["values"], "back_code": "Y"},
    }
    result = library.ingest(writer, photo_file("photo-bad", [conflicting]))
    assert not result.accepted
    assert library.get_row("P1").encoded["back_code"] == "X"


def test_back_code_contradicting_settled_photo_is_rejected_at_parse(library, writer):
    # A photograph that already pins edition X cannot be paired with a Y code.
    row = photo_row(
        "P1", face_zone="ZA", edges=_edges(), orientations=[0],
        photo_edition="X", back_code="Y",
    )
    accepted = library.ingest(writer, photo_file("photo-bad", [row]))
    assert accepted.accepted  # ingest itself only checks TEXT domains
    # The contradiction is a ledger-level invariant, raised on reconstruction.
    from extcontract.puzzle import pieces_from_publish

    with pytest.raises(Exception, match="contradicts photograph"):
        pieces_from_publish(library.publish(), editions=("X", "Y"))


def test_file_replay_is_idempotent(library, writer):
    row = photo_row("P1", face_zone="ZA", edges=_edges(), orientations=[0],
                    photo_edition="X/Y")
    doc = photo_file("photo-1", [row])
    first = library.ingest(writer, doc)
    again = library.ingest(writer, doc)
    assert first.accepted and again.accepted
    assert again.receipt["replay"] is True
    assert library.row_count() == 1
