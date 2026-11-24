"""生成展演排轮回放夹具：扁平 Parquet 观测表 + 行程表/签条/额度/节目 JSON。

两台模型车（C-A、C-B）、三套轴对（W1/W2/W3）、三个周末节目。
段 S1 即题面范例：W1 上场前还可走 5 格（limit=21、此前已耗 16），鼓面 8->1、
行程表只许 2..4 格，实际走 3 格；S1 后剩 2 格，再走 2 格恰好到限可收车、
走 3 格即超期。段 S4/S5 各跨一次零（实际 10 格）。

换轮签条 T 模糊：C-B 在 t=10 的段 S5 可能挂 W1 或 W2，两个候选互斥——
挂 W1 会与 C-A 同期占用 W1（时间窗重叠）被物理排斥，挂 W2 是唯一可行分支。
W3 是没有任何履历的纯库房备轮，专供第三个节目封存/换入。

直接运行：PYTHONPATH=src python tools/make_wheel_fixtures.py
"""
from __future__ import annotations

import json
import os

import pyarrow as pa
import pyarrow.parquet as pq

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "fixtures", "wheels")

# car, ts, drum, axle(清楚签条), tag(模糊签条), seg_id
# 模糊点 C-B@t=10：签条 T 在 W1/W2 之间二选一（互斥）。
OBSERVATIONS = [
    ("C-A", 0, 2, "W1", None, "S0"),
    ("C-A", 4, 9, "W1", None, "S2"),
    ("C-A", 8, 8, "W1", None, "S1"),
    ("C-A", 12, 1, "W1", None, None),
    ("C-B", 0, 3, "W2", None, "S3"),
    ("C-B", 6, 7, "W2", None, "S4"),
    ("C-B", 10, 7, None, "T", "S5"),
    ("C-B", 14, 9, None, "T", None),
]

# seg_id, car, lo, hi
ITINERARY = [
    ("S0", "C-A", 5, 9),     # 2->9：余数 7，区间内唯一解 7
    ("S2", "C-A", 8, 12),    # 9->8：余数 9，唯一解 9（19 超出区间，跨零 0 次）
    ("S1", "C-A", 2, 4),     # 8->1：余数 3，唯一解 3（题面范例，跨零 0 次）
    ("S3", "C-B", 1, 5),     # 3->7：余数 4，唯一解 4
    ("S4", "C-B", 0, 10),    # 7->7：余数 0，可行解 [0, 10]，跨零 0 或 1 次（二义保留）
    ("S5", "C-B", 8, 12),    # 7->9：余数 2，唯一解 12（跨零 1 次）
]

TAG_CANDIDATES = {"T": ["W1", "W2"]}

# 馆内自订翻修期限（总格子数）。
# W1：S1 前已耗 16（S0=7+S2=9），limit=21 -> 上场前恰好剩 5 格。
LIMITS = {"W1": 21, "W2": 30, "W3": 10}

# 开馆前车上现挂；W3 是无履历的纯库房备轮。
INITIAL_MOUNT = {"C-A": "W1", "C-B": "W2"}
SPARE_AXLES = ["W3"]

# program_id, car, stage, has_lift, ticks, departs_before_close
# P1 走 2 格：W1 历史已耗 19，19+2=21 恰好到限，可收车；走 3 格即超期。
# P2 让 C-B 挂 W2 在侧线散场前出发：W2 随车锁死，后续节目叫不回这套轮，
# 但车回到车库还能再上场；P3 在带举升台的 ST-举升台 把 C-B 换成库房备轮 W3。
# 撤掉该展台举升台后，无举升台只能沿用现挂 W2（已锁死/额度另计），共用路线即拒。
PROGRAMS = [
    ("P1", "C-A", "ST-主线", False, 2, False),
    ("P2", "C-B", "ST-侧线", False, 4, True),
    ("P3", "C-B", "ST-举升台", True, 2, False),
]

SCHEMA = pa.schema([
    pa.field("car", pa.string()),
    pa.field("ts", pa.int64()),
    pa.field("drum", pa.int32()),
    pa.field("axle", pa.string()),
    pa.field("swap_tag", pa.string()),
    pa.field("seg_id", pa.string()),
])


def main() -> None:
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    table = pa.table({
        "car": [r[0] for r in OBSERVATIONS],
        "ts": [r[1] for r in OBSERVATIONS],
        "drum": [r[2] for r in OBSERVATIONS],
        "axle": [r[3] for r in OBSERVATIONS],
        "swap_tag": [r[4] for r in OBSERVATIONS],
        "seg_id": [r[5] for r in OBSERVATIONS],
    }, schema=SCHEMA)
    pq.write_table(table, os.path.join(FIXTURE_DIR, "observations.parquet"),
                   write_statistics=True)

    with open(os.path.join(FIXTURE_DIR, "itinerary.json"), "w", encoding="utf-8") as fh:
        json.dump([
            {"seg_id": s, "car": c, "lo": lo, "hi": hi} for s, c, lo, hi in ITINERARY
        ], fh, ensure_ascii=False, indent=2)
    with open(os.path.join(FIXTURE_DIR, "tags.json"), "w", encoding="utf-8") as fh:
        json.dump(TAG_CANDIDATES, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(FIXTURE_DIR, "depot.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "overhaul_limits": LIMITS,
            "initial_mount": INITIAL_MOUNT,
            "spare_axles": SPARE_AXLES,
        }, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(FIXTURE_DIR, "programs.json"), "w", encoding="utf-8") as fh:
        json.dump([
            {"program_id": pid, "car": car, "stage": st, "has_lift": lift,
             "ticks": ticks, "departs_before_close": departs}
            for pid, car, st, lift, ticks, departs in PROGRAMS
        ], fh, ensure_ascii=False, indent=2)
    print(f"wrote wheel fixtures under {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
