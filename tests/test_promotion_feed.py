"""Tests for station feeds: read/write contracts, revisions and atomic rejects."""

from __future__ import annotations

import pytest

from extcontract import IngestionLibrary
from extcontract.promotion import (
    StationDirectory,
    segment_reader_contract,
    segment_writer_contract,
)
from extcontract.promotion.scenarios import segment_row


@pytest.fixture()
def feed():
    return StationDirectory(["east", "west"]).feed("east")


def test_segment_contracts_are_safe_pair():
    from extcontract import check_compatibility

    report = check_compatibility(segment_writer_contract(), segment_reader_contract())
    assert report.safe


def test_feed_uses_an_ingestion_library_under_a_reader_contract(feed):
    assert isinstance(feed._library, IngestionLibrary)
    assert feed.reader_version == "segment-reader-v1"


def test_foreign_station_rejects_whole_report(feed):
    receipt = feed.ingest_report(
        [segment_row("S1", station="west", match="M", index=1, a="甲", b="乙", jumps=5)],
        file_id="foreign",
    )
    assert not receipt.accepted
    assert feed.publish_segments() == []


def test_valid_flag_must_be_0_or_1(feed):
    receipt = feed.ingest_report(
        [
            {
                "row_id": "S1",
                "revision": 1,
                "values": {
                    "station": "east",
                    "match": "M",
                    "segment_index": 1,
                    "jumper_a": "甲",
                    "jumper_b": "乙",
                    "jumps": 5,
                    "valid": 2,
                },
            }
        ],
        file_id="bad-flag",
    )
    assert not receipt.accepted
    assert "valid must be 0" in receipt.errors[0]


def test_same_revision_conflict_rejects_and_higher_revision_supersedes(feed):
    first = segment_row("S1", station="east", match="M", index=1, a="甲", b="乙", jumps=5)
    assert feed.ingest_report([first], file_id="r1").accepted

    conflict = segment_row("S1", station="east", match="M", index=1, a="甲", b="丙", jumps=5)
    rejected = feed.ingest_report([conflict], file_id="r2")
    assert not rejected.accepted
    # The conflicting report left no trace: still 甲+乙 at revision 1.
    [segment] = feed.publish_segments()
    assert segment.jumper_b == "乙" and segment.revision == 1

    correction = segment_row(
        "S1", station="east", match="M", index=1, a="甲", b="丙", jumps=5, revision=2
    )
    assert feed.ingest_report([correction], file_id="r3").accepted
    [segment] = feed.publish_segments()
    assert segment.jumper_b == "丙" and segment.revision == 2


def test_replay_same_file_returns_stored_receipt(feed):
    row = segment_row("S1", station="east", match="M", index=1, a="甲", b="乙", jumps=5)
    feed.ingest_report([row], file_id="dup")
    again = feed.ingest_report([row], file_id="dup")
    assert again.accepted
    assert feed.receipt_for("dup") is not None


def test_void_segments_are_published_but_marked(feed):
    feed.ingest_report(
        [
            segment_row("S1", station="east", match="M", index=1, a="甲", b="乙", jumps=5),
            segment_row("S2", station="east", match="M", index=2, a="甲", b="乙", jumps=4, valid=False),
        ],
        file_id="void",
    )
    by_id = {s.segment_id: s for s in feed.publish_segments()}
    assert by_id["S1"].valid is True
    assert by_id["S2"].valid is False


def test_directory_enforces_station_cap():
    with pytest.raises(Exception):
        StationDirectory(["s1", "s2", "s3", "s4", "s5"])
