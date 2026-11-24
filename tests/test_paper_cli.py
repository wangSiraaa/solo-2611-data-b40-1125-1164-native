"""CLI integration tests for the local paper cabinet entry point."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DOC = {
    "contract_no": "C1",
    "block": "B1",
    "guarantee": 4,
    "permitted": ["甲", "乙", "丙", "丁"],
    "makeup_edition": "mk-1",
    "deadline": "2026-12-01",
}
ROUTES_DOC = {
    "routes": [
        {"route_id": "L", "block": "B1", "households": ["甲", "乙", "丙", "丁"]},
        {"route_id": "M", "block": "B1", "households": ["甲", "乙"]},
    ]
}
UNIVERSES_DOC = {"blocks": {"B1": ["甲", "乙", "丙", "丁"]}}


def run_cli(state: Path, *parts: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "extcontract", "paper", *parts],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode in (0, 1), proc.stderr
    return json.loads(proc.stdout)


def test_paper_cli_full_flow(tmp_path: Path):
    state = tmp_path / "cab.json"
    routes_path = tmp_path / "routes.json"
    universes_path = tmp_path / "universes.json"
    contract_path = tmp_path / "c1.json"
    routes_path.write_text(json.dumps(ROUTES_DOC, ensure_ascii=False), encoding="utf-8")
    universes_path.write_text(json.dumps(UNIVERSES_DOC, ensure_ascii=False), encoding="utf-8")
    contract_path.write_text(json.dumps(CONTRACT_DOC, ensure_ascii=False), encoding="utf-8")

    init = run_cli(state, "init", str(state), "--mode", "whole_line", "--slots", "5",
                   "--routes-doc", str(routes_path), "--universes-doc", str(universes_path))
    assert init["status"] == "ACCEPTED"

    assert run_cli(state, "contract", str(state), str(contract_path))["status"] == "ACCEPTED"
    for name in ("D-BASE", "D-FIX1"):
        station = run_cli(state, "station", str(state),
                          str(ROOT / "materials/paper" / f"{name}.json"))
        assert station["status"] == "ACCEPTED"

    settle = run_cli(state, "settle", str(state))
    assert settle["data"]["blocks"]["B1"]["lower_bound"] == 3

    account = run_cli(state, "account", str(state), "C1")
    assert account["data"]["residual_gap"] == 1

    cheap = run_cli(state, "sign", str(state), "C1", "--route", "M")
    assert cheap["status"] == "REJECTED"
    line = run_cli(state, "sign", str(state), "C1", "--route", "L")
    assert line["status"] == "ACCEPTED"
    assert line["data"]["slot_state"]["available"] == 1

    seal = run_cli(state, "seal", str(state), "mk-1")
    assert seal["data"]["slot_state"]["sealed_real_slots"] == 4

    # Replaying the station file then reading the contract back still works.
    replay = run_cli(state, "station", str(state),
                     str(ROOT / "materials/paper/D-BASE.json"))
    assert replay["ingest"]["receipt"]["replay"] is True
    readback = run_cli(state, "readback", str(state), "C1")
    assert readback["data"]["makeup_edition_used"] == "mk-1"
    assert len(readback["data"]["orders"]) == 1


def test_paper_cli_targeted_mode_rejects_line_flag(tmp_path: Path):
    state = tmp_path / "cab.json"
    universes_path = tmp_path / "universes.json"
    contract_path = tmp_path / "c1.json"
    universes_path.write_text(json.dumps(UNIVERSES_DOC, ensure_ascii=False), encoding="utf-8")
    contract_path.write_text(json.dumps(CONTRACT_DOC, ensure_ascii=False), encoding="utf-8")
    run_cli(state, "init", str(state), "--mode", "targeted", "--slots", "5",
            "--universes-doc", str(universes_path))
    run_cli(state, "contract", str(state), str(contract_path))
    rejected = run_cli(state, "sign", str(state), "C1", "--route", "L")
    assert rejected["status"] == "REJECTED"
