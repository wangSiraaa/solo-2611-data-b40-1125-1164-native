"""Station feeds: read/write contracts for the on-rope segment reports.

Each station is its own :class:`IngestionLibrary` under a fixed reader
contract, exactly like the rest of the toolkit:

  * a ruling body **writes** segment reports (``writer-contract`` shaped),
    one row per on-rope segment, with a positive integer ``revision`` on every
    row envelope;
  * the feed ingests each report **atomically** — an illegal report changes
    nothing and returns the rejection;
  * :meth:`StationFeed.publish_segments` reads the effective rows from the
    real target slice, so a later ruling revision (a correction that changes
    *who was on the rope* for a segment) supersedes the earlier one by the
    normal highest-revision-wins rule, and same-revision conflicts reject.

This is the single place that talks about rows and revisions.  Everything
above the feed only sees :class:`~extcontract.promotion.Segment` values.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..adapter import RECORDS_FORMAT
from ..contract import Contract, parse_contract
from ..errors import ContractError
from ..ingest import IngestResult, IngestionLibrary
from . import MAX_STATIONS, Segment

_WRITER_VERSION = "segment-writer-v1"
_READER_VERSION = "segment-reader-v1"

# Identity of a report row is the segment id; the revision envelope carries
# the ruling version (a correction re-uses the same segment_id at a higher
# revision).  Both contracts carry identical field_ids, all INT/TEXT, so the
# row-free compatibility check is trivially SAFE.
_FIELD_SPECS = (
    ("station", "station", "TEXT", False),
    ("match", "match id", "TEXT", False),
    ("segment_index", "segment index within the match", "INT", False),
    ("jumper_a", "first jumper on the rope", "TEXT", False),
    ("jumper_b", "second jumper on the rope", "TEXT", False),
    ("jumps", "counted jumps in this segment", "INT", False),
    ("valid", "1 if the segment counts, 0 if voided", "INT", False),
)


def _contract(version: str, *, role: str) -> Contract:
    fields = []
    for field_id, name, kind, nullable in _FIELD_SPECS:
        type_json = (
            {"kind": "INT", "bits": 32, "nullable": nullable}
            if kind == "INT"
            else {"kind": "TEXT", "nullable": nullable}
        )
        fields.append(
            {
                "field_id": field_id,
                "name": name,
                "type": type_json,
                "nullable": nullable,
            }
        )
    return parse_contract({"version": version, "fields": fields}, role=role)


def segment_writer_contract() -> Contract:
    return _contract(_WRITER_VERSION, role="writer")


def segment_reader_contract() -> Contract:
    return _contract(_READER_VERSION, role="reader")


@dataclass
class IngestReceipt:
    """The feed-level answer to one report file."""

    station: str
    file_id: str
    accepted: bool
    errors: list[str]
    segment_ids: list[str]

    def to_json(self) -> dict:
        return {
            "station": self.station,
            "file_id": self.file_id,
            "status": "ACCEPTED" if self.accepted else "REJECTED",
            "errors": list(self.errors),
            "segment_ids": list(self.segment_ids),
        }


class StationFeed:
    """One station's revision-aware segment store."""

    def __init__(self, station: str):
        if not isinstance(station, str) or not station:
            raise ContractError("station id must be a non-empty string")
        self.station = station
        self._writer = segment_writer_contract()
        self._reader = segment_reader_contract()
        self._library = IngestionLibrary(self._reader)
        self._file_count = 0

    @property
    def writer(self) -> Contract:
        return self._writer

    @property
    def reader(self) -> Contract:
        return self._reader

    @property
    def reader_version(self) -> str:
        return self._reader.version

    def ingest_report(self, rows: list[dict], *, file_id: str | None = None) -> IngestReceipt:
        """Ingest one ruling report atomically.

        ``rows`` are raw writer rows:
        ``{"row_id": <segment id>, "revision": <ruling version>, "values": {...}}``.
        Every row must name this feed's station; a foreign station rejects the
        whole report so one feed can never mutate another station's rows.
        """
        for row in rows:
            values = row.get("values") if isinstance(row, dict) else None
            if not isinstance(values, dict) or values.get("station") != self.station:
                return IngestReceipt(
                    station=self.station,
                    file_id=file_id or "",
                    accepted=False,
                    errors=[
                        f"station feed {self.station!r} only accepts rows whose "
                        f"station is {self.station!r}"
                    ],
                    segment_ids=[],
                )
            if values.get("valid") not in (0, 1):
                return IngestReceipt(
                    station=self.station,
                    file_id=file_id or "",
                    accepted=False,
                    errors=["valid must be 0 (void) or 1 (counting)"],
                    segment_ids=[],
                )
        self._file_count += 1
        fid = file_id or f"{self.station}-report-{self._file_count}"
        document = {
            "file_id": fid,
            "writer_version": self._writer.version,
            "reader_version": self._reader.version,
            "rows": rows,
        }
        result: IngestResult = self._library.ingest(self._writer, document)
        segment_ids = sorted(result.receipt["row_ids"]) if result.accepted else []
        return IngestReceipt(
            station=self.station,
            file_id=fid,
            accepted=result.accepted,
            errors=list(result.errors),
            segment_ids=segment_ids,
        )

    def receipt_for(self, file_id: str) -> dict | None:
        return self._library.receipt_for(file_id)

    def publish(self) -> dict:
        """The ``reader-records/v1`` slice: only currently effective rows."""
        return self._library.publish()

    def publish_segments(self) -> list[Segment]:
        """Effective segments from the real published slice, highest revision wins."""
        document = self.publish()
        if document["format"] != RECORDS_FORMAT:  # pragma: no cover - invariant
            raise ContractError("unexpected records format from ingestion library")
        segments: list[Segment] = []
        for row in document["rows"]:
            values = row["values"]
            segments.append(
                Segment(
                    segment_id=row["row_id"],
                    station=values["station"],
                    match=values["match"],
                    segment_index=int(values["segment_index"]),
                    jumper_a=values["jumper_a"],
                    jumper_b=values["jumper_b"],
                    jumps=int(values["jumps"]),
                    valid=bool(int(values["valid"])),
                    revision=int(row["revision"]),
                )
            )
        segments.sort(key=lambda s: (s.match, s.segment_index, s.segment_id))
        return segments


class StationDirectory:
    """The bounded set of station feeds (at most :data:`MAX_STATIONS`)."""

    def __init__(self, stations: list[str]):
        if not stations:
            raise ContractError("at least one station is required")
        if len(stations) > MAX_STATIONS:
            raise ContractError(f"at most {MAX_STATIONS} stations are supported")
        if len(set(stations)) != len(stations):
            raise ContractError("duplicate station ids")
        self._feeds: dict[str, StationFeed] = {s: StationFeed(s) for s in stations}

    @property
    def stations(self) -> list[str]:
        return sorted(self._feeds)

    def feed(self, station: str) -> StationFeed:
        try:
            return self._feeds[station]
        except KeyError:
            raise ContractError(f"unknown station {station!r}") from None

    def all_segments(self) -> list[Segment]:
        segments: list[Segment] = []
        for station in self.stations:
            segments.extend(self._feeds[station].publish_segments())
        return segments
