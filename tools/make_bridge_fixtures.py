"""生成 bridgeplaque 夹具（fixtures_bridge/），确定性、可重复。

生成物：
- archive/beam_readings.parquet  地方案卷：每行一个取样片段（两个行组：
  RG0 南桥段 5 行，RG1 北桥段 2 行；按桥段查询时北桥段行组可被 footer
  统计证明跳过）；
- files.json / queries.json      pqscan 输入（queries.json 供人工查看，
                                 签发时按申请桥段临时生成查询）；
- band.json                      随题自造的逐年宽窄参照带（1..60 年）；
- workorder.json                 工单：事件、用材、榫孔/封榫、查看权与取样许可。

参照带用种子搜索构造，并用 bridgeplaque.band.ReferenceBand 的真实对齐
代码验证以下设计（不满足就换下一个种子，因此输出是确定的）：
- 甲（现砍现装，带皮末轮，中间漏读一环）候选末轮年恰为 [42, 54]，
  54 即另一次重装，留给封榫记录排除；
- 乙（库存旧材）唯一对齐末轮 39 年；丙唯一对齐 54 年（锁定重装）；
- 丁唯一对齐 36 年。四梁、初建42 -> 重装54 恰十二年。

运行：PYTHONPATH=src .venv/bin/python tools/make_bridge_fixtures.py
"""
from __future__ import annotations

import json
import os
import random

import pyarrow as pa
import pyarrow.parquet as pq

from bridgeplaque.band import ReferenceBand

OUT = os.path.join(os.path.dirname(__file__), "..", "fixtures_bridge")

SCHEMA = pa.schema(
    [
        pa.field("segment", pa.string()),
        pa.field("beam_id", pa.string()),
        pa.field("fragment", pa.string()),
        pa.field("start_pos", pa.int64()),
        pa.field("widths", pa.string()),
        pa.field("bark_edge", pa.int64()),
    ]
)

# 参照带落点设计（公历年 -> 字符下标 = 年 - 1）
A_SPANS = (33, 45)          # 甲的 9 环模式在参照带上的两处落点（末环 42 / 54）
YI_SPAN = (30, 39)          # 乙：31..39 年，末环 39
BING_SPAN = (44, 54)        # 丙：45..54 年，末环 54
DING_SPAN = (28, 36)        # 丁：29..36 年，末环 36


def _search_band() -> tuple[ReferenceBand, str, int]:
    """种子搜索：返回满足全部对齐约束的参照带、甲的 9 环模式、种子。"""
    for seed in range(100_000):
        rng = random.Random(seed)
        widths = [rng.choice("窄宽") for _ in range(60)]
        a = [rng.choice("窄宽") for _ in range(9)]
        for start in A_SPANS:
            widths[start : start + 9] = a
        band = ReferenceBand(start_year=1, widths="".join(widths))

        jia = "".join(a[:5]) + "?" + "".join(a[6:])     # 漏读一环
        yi = band.widths[YI_SPAN[0] : YI_SPAN[1]]
        bing = band.widths[BING_SPAN[0] : BING_SPAN[1]]
        ding = band.widths[DING_SPAN[0] : DING_SPAN[1]]
        if band.align(jia) != [42, 54]:
            continue
        if band.align(yi) != [39] or band.align(bing) != [54] or band.align(ding) != [36]:
            continue
        return band, "".join(a), seed
    raise SystemExit("种子搜索失败：未找到满足约束的参照带")


def _rows(band: ReferenceBand, a: str) -> list[dict]:
    w = band.widths
    return [
        # 南桥段（RG0）：甲乙丙丁四梁，甲断为两片、中间漏读一环
        {"segment": "南桥段", "beam_id": "甲", "fragment": "甲-1",
         "start_pos": 1, "widths": a[:5], "bark_edge": 0},
        {"segment": "南桥段", "beam_id": "甲", "fragment": "甲-2",
         "start_pos": 7, "widths": a[6:], "bark_edge": 1},
        {"segment": "南桥段", "beam_id": "乙", "fragment": "乙-1",
         "start_pos": 1, "widths": w[YI_SPAN[0]:YI_SPAN[1]], "bark_edge": 1},
        {"segment": "南桥段", "beam_id": "丙", "fragment": "丙-1",
         "start_pos": 1, "widths": w[BING_SPAN[0]:BING_SPAN[1]], "bark_edge": 1},
        {"segment": "南桥段", "beam_id": "丁", "fragment": "丁-1",
         "start_pos": 1, "widths": w[DING_SPAN[0]:DING_SPAN[1]], "bark_edge": 1},
        # 北桥段（RG1）：另一桥段的案卷，用于演示按桥段查询时的行组跳过
        {"segment": "北桥段", "beam_id": "戊", "fragment": "戊-1",
         "start_pos": 1, "widths": w[9:15], "bark_edge": 1},
        {"segment": "北桥段", "beam_id": "己", "fragment": "己-1",
         "start_pos": 1, "widths": w[19:24], "bark_edge": 0},
    ]


def _write_parquet(path: str, rows: list[dict]) -> None:
    writer = pq.ParquetWriter(path, SCHEMA, write_statistics=True)
    writer.write_table(pa.Table.from_pylist(rows[:5], schema=SCHEMA))   # RG0 南桥段
    writer.write_table(pa.Table.from_pylist(rows[5:], schema=SCHEMA))   # RG1 北桥段
    writer.close()


def main() -> None:
    band, a, seed = _search_band()
    rows = _rows(band, a)

    os.makedirs(os.path.join(OUT, "archive"), exist_ok=True)
    _write_parquet(os.path.join(OUT, "archive", "beam_readings.parquet"), rows)

    _dump("files.json", ["archive/beam_readings.parquet"])
    _dump("queries.json", [
        {"id": "seg_nan", "where": {"eq": ["segment", "南桥段"]},
         "columns": ["segment", "beam_id", "fragment", "start_pos", "widths", "bark_edge"]},
        {"id": "seg_bei", "where": {"eq": ["segment", "北桥段"]},
         "columns": ["segment", "beam_id", "fragment", "start_pos", "widths", "bark_edge"]},
    ])
    _dump("band.json", {"start_year": band.start_year, "widths": band.widths})
    _dump("workorder.json", {
        "events": ["初建", "重装"],
        "beams": {
            "甲": {"material": "现砍现装", "events": ["初建", "重装"]},
            "乙": {"material": "库存旧材", "events": ["初建"]},
            "丙": {"material": "现砍现装", "events": ["重装"]},
            "丁": {"material": "库存旧材", "events": ["初建"]},
        },
        "joints": [
            {"id": "J1", "mortise_beam": "丁", "tenon_beam": "甲",
             "covers": {"甲": [8, 9]}, "sealed_after": "初建"},
        ],
        "permits": {"view": {"J1": True}, "sample": {"J1": False}},
    })

    print(f"参照带种子 seed={seed}；甲模式 {a[:5]}?{a[6:]} -> 候选 [42, 54]")
    print(f"fixtures_bridge/ 已生成（{len(rows)} 行案卷，2 个行组）")


def _dump(name: str, obj) -> None:
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
