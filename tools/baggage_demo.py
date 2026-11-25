#!/usr/bin/env python3.11
"""Rerun the toy-terminal baggage carousel handoff and delivery scenarios.

Examples
--------
    python3.11 tools/baggage_demo.py
    python3.11 tools/baggage_demo.py --out /tmp/baggage-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.baggage import run_demo  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "artifacts"), help="report/db output directory")
    parser.add_argument("--quiet", action="store_true", help="write files without printing the Markdown report")
    args = parser.parse_args(argv)

    report = run_demo(args.out)
    if not args.quiet:
        print((Path(args.out) / "baggage_report.md").read_text(encoding="utf-8"))
    print(f"\nJSON report: {Path(args.out) / 'baggage_report.json'}")
    print(f"Markdown report: {Path(args.out) / 'baggage_report.md'}")
    print(f"Warehouse: {report['warehouse_db']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
