"""Canonical 甲/乙 split-bed story and runnable hand-over scenario."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .ledger import CONTRACT_SPECS, GardenLedger
from .planner import GardenPlanner
from .world import GardenWorld

BEAN = "豆"
GRAIN = "禾"

A = "A"
B = "B"
BOX1 = "BOX-1"
BOX2 = "BOX-2"
SOURCE_A = "SOIL-A"
SOURCE_B = "SOIL-B"
LANE = "LANE"


def _doc(kind: str, file_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    version = CONTRACT_SPECS[kind]["version"]
    return {
        "file_id": file_id,
        "writer_version": version,
        "reader_version": version,
        "rows": rows,
    }


def _row(row_id: str, values: dict[str, Any], revision: int = 1) -> dict[str, Any]:
    return {"row_id": row_id, "revision": revision, "values": values}


def planting_rows() -> list[dict[str, Any]]:
    rows = []
    for source, label in ((SOURCE_A, "A"), (SOURCE_B, "B")):
        for rank in (-1, -2):
            season = "last" if rank == -1 else "before_last"
            rows.append(
                _row(
                    f"P-{label}-{season}",
                    {
                        "source_id": source,
                        "season_rank": rank,
                        "crop_family": GRAIN,
                        "crop_name": "水稻" if rank == -1 else "谷子",
                    },
                )
            )
    return rows


def correction_document(family: str = BEAN, revision: int = 2) -> dict[str, Any]:
    return _doc(
        "planting",
        "garden-correct-b-before-last",
        [
            _row(
                "P-B-before_last",
                {
                    "source_id": SOURCE_B,
                    "season_rank": -2,
                    "crop_family": family,
                    "crop_name": "大豆",
                },
                revision=revision,
            )
        ],
    )


def restore_document(revision: int = 3) -> dict[str, Any]:
    doc = correction_document(GRAIN, revision=revision)
    doc["file_id"] = "garden-restore-b-grain"
    return doc


def map_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def map_row(rid: str, kind: str, rev: int = 1, **values: Any) -> dict[str, Any]:
        values = {"record_kind": kind, **values}
        return _row(rid, values, rev)

    positions = {
        "LA": (0, 1),
        "LB": (1, 1),
        "P2": (2, 1),
        "PB": (2, 0),
        "EX": (3, 0),
        "P1": (3, 1),
    }
    for pid, (x, y) in positions.items():
        rows.append(map_row(f"POS-{pid}", "position", lot_id=pid, x=x, y=y))

    lots = [
        ("LOT-A", A, "cell", "甲格", None, 0, 0, None),
        ("LOT-B", B, "cell", "乙格", None, 1, 0, None),
        ("LOT-BOX1", BOX1, "box", "一号箱", "P1", 3, 1, "yes"),
        ("LOT-BOX2", BOX2, "box", "二号箱", "P2", 2, 1, "yes"),
    ]
    for rid, lot_id, kind, name, pos, x, y, sealed in lots:
        rows.append(
            map_row(
                rid,
                "lot",
                lot_id=lot_id,
                lot_kind=kind,
                display_name=name,
                position_id=pos,
                x=x,
                y=y,
                width_units=1,
                sealed_bottom=sealed,
            )
        )

    edges = [
        ("EDGE-A-LA", A, "LA"),
        ("EDGE-B-LB", B, "LB"),
        ("EDGE-LA-LB", "LA", "LB"),
        ("EDGE-BOX2-LB", "P2", "LB"),
        ("EDGE-BOX1-EX", "P1", "EX"),
        ("EDGE-EX-PB", "EX", "PB"),
        ("EDGE-LB-PB", "LB", "PB"),
    ]
    for rid, left, right in edges:
        rows.append(
            map_row(
                rid,
                "edge",
                lot_id=left,
                related_id=right,
                allowed="yes",
                width_units=2,
            )
        )

    rows.append(
        map_row(
            "AISLE-LANE",
            "aisle",
            lot_id=LANE,
            display_name="总过道",
            allowed="yes",
            width_units=2,
        )
    )
    # LB is the actual graph node carrying the LANE aisle permit.
    rows.append(
        map_row(
            "AISLE-NODE-LB",
            "aisle",
            lot_id="LB",
            display_name="总过道西段",
            allowed="yes",
            width_units=2,
        )
    )
    return rows


def narrow_aisle_document(*, capacity: int = 1, revision: int = 2) -> dict[str, Any]:
    return _doc(
        "map",
        "garden-narrow-aisle-to-one",
        [
            _row(
                "AISLE-NODE-LB",
                {
                    "record_kind": "aisle",
                    "lot_id": "LB",
                    "display_name": "收窄后的过道交位",
                    "allowed": "yes",
                    "width_units": 1,
                    "handoff_capacity": capacity,
                },
                revision=revision,
            )
        ],
    )


def widen_aisle_document(revision: int = 3) -> dict[str, Any]:
    return _doc(
        "map",
        "garden-widen-aisle",
        [
            _row(
                "AISLE-NODE-LB",
                {
                    "record_kind": "aisle",
                    "lot_id": "LB",
                    "display_name": "恢复宽度的过道",
                    "allowed": "yes",
                    "width_units": 2,
                    "handoff_capacity": None,
                },
                revision=revision,
            )
        ],
    )


def rename_place_document(revision: int = 2) -> dict[str, Any]:
    return _doc(
        "map",
        "garden-rename-a",
        [
            _row(
                "LOT-A",
                {
                    "record_kind": "lot",
                    "lot_id": A,
                    "lot_kind": "cell",
                    "display_name": "春畦东一号（改名后）",
                    "x": 0,
                    "y": 0,
                    "width_units": 1,
                },
                revision=revision,
            )
        ],
    )


def soil_rows() -> list[dict[str, Any]]:
    rows = [
        _row(
            "BATCH-A",
            {
                "record_kind": "batch",
                "source_id": SOURCE_A,
                "origin_lot": A,
                "source_amount": "1",
            },
        ),
        _row(
            "BATCH-B",
            {
                "record_kind": "batch",
                "source_id": SOURCE_B,
                "origin_lot": B,
                "source_amount": "1",
            },
        ),
    ]
    movements = [
        ("M-A-STAY", SOURCE_A, A, "1/2", "retained", A, None),
        ("M-A-BOX1", SOURCE_A, A, "1/2", "box", BOX1, "一号箱"),
        ("M-B-STAY", SOURCE_B, B, "1/2", "retained", B, None),
        ("M-B-BOX2", SOURCE_B, B, "1/2", "box", BOX2, "二号箱"),
    ]
    for mid, source, origin, amount, kind, dest, box_name in movements:
        rows.append(
            _row(
                mid,
                {
                    "record_kind": "movement",
                    "source_id": source,
                    "origin_lot": origin,
                    "amount": amount,
                    "destination_kind": kind,
                    "destination_id": dest,
                    "sealed_bottom": "yes",
                    "box_name": box_name,
                },
            )
        )
    return rows


def initial_documents() -> dict[str, dict[str, Any]]:
    return {
        "planting": _doc("planting", "garden-planting-initial", planting_rows()),
        "map": _doc("map", "garden-map-initial", map_rows()),
        "soil": _doc("soil", "garden-soil-initial", soil_rows()),
    }


def build_ledger(
    *,
    corrected_b: bool = False,
    restored: bool = False,
    narrow_aisle: bool = False,
    renamed: bool = False,
) -> GardenLedger:
    ledger = GardenLedger()
    results = ledger.ingest_files(initial_documents())
    for kind, result in results.items():
        if not result.accepted:
            raise RuntimeError(f"initial {kind} ingest rejected: {result.errors}")
    if corrected_b:
        result = ledger.ingest("planting", correction_document())
        if not result.accepted:
            raise RuntimeError(f"correction ingest rejected: {result.errors}")
    if restored:
        result = ledger.ingest("planting", restore_document())
        if not result.accepted:
            raise RuntimeError(f"restore ingest rejected: {result.errors}")
    if narrow_aisle:
        result = ledger.ingest("map", narrow_aisle_document())
        if not result.accepted:
            raise RuntimeError(f"narrow aisle ingest rejected: {result.errors}")
    if renamed:
        result = ledger.ingest("map", rename_place_document())
        if not result.accepted:
            raise RuntimeError(f"rename ingest rejected: {result.errors}")
    return ledger


def build_planner(**kwargs: Any) -> GardenPlanner:
    ledger = kwargs.pop("ledger", None) or build_ledger()
    planner = GardenPlanner(ledger.build_world(), move_capacity=2, **kwargs)
    planner.ledger = ledger  # type: ignore[attr-defined]
    return planner


def solve_garden(data: dict[str, Any] | GardenWorld | None = None, **kwargs: Any) -> dict[str, Any]:
    """Convenience solver: canonical story with no arg, otherwise one plan."""

    if data is None:
        return run_story()
    household = kwargs.pop("household_id", kwargs.pop("household", "H1"))
    plan_keys = {"crop_family", "places", "mode", "shift", "fulfillment_shift", "target_aisle"}
    plan_kwargs = {k: kwargs.pop(k) for k in list(kwargs) if k in plan_keys}
    planner = GardenPlanner(data, **kwargs)
    return planner.plan(household, **plan_kwargs)


def run_story() -> dict[str, Any]:
    """Execute the scripted checks and return a JSON-serialisable report."""

    # 1. Baseline: arranger chooses the two adjacent land cells A+B.
    baseline_ledger = build_ledger()
    baseline = GardenPlanner(baseline_ledger.build_world(), move_capacity=2)
    h1 = baseline.sign_application(
        "H1",
        BEAN,
        places=2,
        shift=1,
        order_id="HO-H1-LAND",
        revisions=baseline_ledger.revision_basis(),
    )
    baseline.hand_over("HO-H1-LAND", shift=2)

    # 2. Correct B's season-before-last to bean.  Both B-derived places fail.
    corrected_ledger = build_ledger(corrected_b=True)
    corrected_world = corrected_ledger.build_world()
    corrected = GardenPlanner(corrected_world, move_capacity=2)
    corrected_plan = corrected.plan("HX", BEAN, places=2, shift=1)
    corrected_eligibility = {
        lot: corrected_world.assess_lot(lot, BEAN).to_json()
        for lot in (A, B, BOX1, BOX2)
    }

    # 3. Restore B to grain, then narrow aisle to one hand-over place.
    narrow_ledger = build_ledger(restored=True, narrow_aisle=True)
    narrow_world = narrow_ledger.build_world()
    narrow = GardenPlanner(narrow_world, move_capacity=2)
    narrow_plan = narrow.plan("HN", BEAN, places=2, shift=1)

    # 4. Next shift: H1 boundary is already handed; move real BOX-1 to PB and
    #    let H2 apply for the adjacent real boxes.  Orders signed in shift 2
    #    are fulfilled by shift 3 with a fresh two-slot carry allowance.
    next_ledger = build_ledger()
    next_planner = GardenPlanner(next_ledger.build_world(), move_capacity=2)
    next_planner.sign_application(
        "H1",
        BEAN,
        places=2,
        shift=1,
        order_id="HO-H1-LAND",
        revisions=next_ledger.revision_basis(),
    )
    next_planner.hand_over("HO-H1-LAND", shift=2)
    move = next_planner.move_box(BOX1, "PB", shift=2)
    who_before_signing = next_planner.who_can_be_served(
        [
            {"household_id": "H2", "crop_family": BEAN, "places": 2, "shift": 2},
        ]
    )
    h2_same_shift_preview = next_planner.plan(
        "H2", BEAN, places=2, mode="box", shift=2, fulfillment_shift=2
    )
    h2 = next_planner.sign_application(
        "H2",
        BEAN,
        places=2,
        shift=2,
        order_id="HO-H2-BOXES",
        revisions=next_ledger.revision_basis(),
    )
    h2_handover = next_planner.hand_over("HO-H2-BOXES", shift=3)
    h3_query = next_planner.who_can_be_served(
        [
            {"household_id": "H3", "crop_family": BEAN, "places": 2, "shift": 2},
        ]
    )
    # Confirm the query result and demonstrate the three-household limit.
    h3 = next_planner.sign_application("H3", BEAN, places=2, shift=2, order_id="HO-H3")

    return {
        "title": "菜园分畦春季交接",
        "answer": (
            "旧茬不按新地名落账，而按稳定土批来源继承；乙格更正为豆后，乙格留田土与"
            "二号箱同时失格。首户按相邻甲乙格签地，次班可凭真实箱号接一号、二号箱，"
            "第三户没有相邻两处可签。"
        ),
        "baseline": {
            "plan": h1["plan"],
            "signed_order": h1["order"],
        },
        "corrected_B": {
            "publish_revisions": corrected_ledger.revision_basis(),
            "eligibility": corrected_eligibility,
            "two_place_plan": {
                "status": corrected_plan["status"],
                "accepted": corrected_plan["accepted"],
                "reasons": corrected_plan["reasons"],
            },
        },
        "restored_and_narrowed": {
            "two_place_plan": {
                "status": narrow_plan["status"],
                "accepted": narrow_plan["accepted"],
                "reasons": narrow_plan["reasons"],
            }
        },
        "next_shift": {
            "move_box1": move,
            "who_can_apply_before_signing": who_before_signing,
            "h2_same_shift_carry_preview": {
                "status": h2_same_shift_preview["status"],
                "accepted": h2_same_shift_preview["accepted"],
                "reasons": h2_same_shift_preview["reasons"],
                "moves_required": (h2_same_shift_preview["selected"] or {}).get("moves_required")
                if h2_same_shift_preview["selected"]
                else 2,
                "move_slots_available": h2_same_shift_preview["selected"]["move_slots_available"]
                if h2_same_shift_preview["selected"]
                else next_planner.move_slots_available(2),
            },
            "h2_sign": {
                "status": h2["status"],
                "order": h2.get("order"),
                "reasons": h2.get("reasons"),
            },
            "h2_shift3_handover": {"status": h2_handover["status"]},
            "h3_sign": {"status": h3["status"], "reasons": h3.get("reasons")},
            "h3_query": h3_query,
            "query_before_h3": h3_query,  # backward-compatible report key
            "shift2_report": next_planner.next_shift_report(2),
            "shift3_report": next_planner.next_shift_report(3),
            "boundary_occupancy": next_planner.occupancy_by_boundary(),
            "box_occupancy": next_planner.occupancy_by_box(),
        },
    }
