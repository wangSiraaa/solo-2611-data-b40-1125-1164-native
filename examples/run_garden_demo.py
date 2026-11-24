#!/usr/bin/env python3
"""Run the canonical spring garden hand-over story.

Usage:
    PYTHONPATH=. python3 examples/run_garden_demo.py [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extcontract.garden import run_story  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=ROOT / "examples/garden/report.json")
    args = parser.parse_args()
    report = run_story()
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(report["answer"])
    print(f"report: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
