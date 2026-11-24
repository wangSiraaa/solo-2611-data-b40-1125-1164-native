"""生成弹簧检验演示台账。

运行：
    .venv/bin/python tools/make_spring_fixture.py

输出 fixtures/spring_ledger.parquet。数据包含两组试验：
- T-AB：甲 2/2、乙 2/4，用来说明只看下压会误判；
- T-PRESS：两台模架的固定左簧 L1/L2、C 与 D。
"""
from __future__ import annotations

import os

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures")


def _cycle_rows(trial_id: str, spec: dict[str, tuple[int, int]]):
    rows = []
    for spring_id, (outbound, returning) in spec.items():
        rows.append({
            "spring_id": spring_id,
            "trial_id": trial_id,
            "direction": "outbound",
            "force_level": "middle",
            "displacement": outbound,
        })
        rows.append({
            "spring_id": spring_id,
            "trial_id": trial_id,
            "direction": "return",
            "force_level": "middle",
            "displacement": returning,
        })
    return rows


def main() -> None:
    rows = []
    rows += _cycle_rows("T-AB", {"JIA": (2, 2), "YI": (2, 4)})
    rows += _cycle_rows("T-PRESS", {
        "L1": (2, 2),
        "L2": (2, 2),
        "C": (2, 2),
        "D": (2, 3),
    })
    schema = pa.schema([
        pa.field("spring_id", pa.string()),
        pa.field("trial_id", pa.string()),
        pa.field("direction", pa.string()),
        pa.field("force_level", pa.string()),
        pa.field("displacement", pa.int64()),
    ])
    os.makedirs(FIXTURES, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, os.path.join(FIXTURES, "spring_ledger.parquet"),
                   write_statistics=True)
    with open(os.path.join(FIXTURES, "spring_files.json"), "w", encoding="utf-8") as fh:
        fh.write('["spring_ledger.parquet"]\n')
    print(f"wrote {table.num_rows} rows to fixtures/spring_ledger.parquet")


if __name__ == "__main__":
    main()
