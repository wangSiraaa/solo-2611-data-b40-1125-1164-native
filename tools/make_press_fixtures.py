"""生成接印子系统的扁平 Parquet 夹具与任务 JSON。

领域设定（两个盒、两个夜班以内）：
- 字面甲 = 特征 {竖, 横, 缺口J}；字面乙 = 特征 {竖, 横, 缺口Y}（缺口形状不同）。
- B1 盒：甲、乙各一粒（P1、P2）——小单场景；
- B2 盒：甲两粒、乙两粒（P3..P6）——台面/压样翻转场景。
- 压样都是两晚开始前（taken_at < 100）留下的人工标记；另放两行两晚期间的
  迟到压样（taken_at=140），必须被原查询挡在证据之外。
- clear 版压样足以逐粒辨认；vague 版把 P4 的关键缺口标记改成“看不清”（NULL），
  P4 只剩不完整观察，单粒无法辨认，且整盒数量也无法把某粒唯一逼成甲。

直接运行：python tools/make_press_fixtures.py
"""
from __future__ import annotations

import json
import os

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRESS_DIR = os.path.join(ROOT, "fixtures", "press")

NIGHTS = [100, 200]
FACES = {
    "甲": ["竖", "横", "缺口J"],
    "乙": ["竖", "横", "缺口Y"],
}

PROOF_SCHEMA = pa.schema(
    [
        pa.field("proof_id", pa.string()),
        pa.field("piece_id", pa.string()),
        pa.field("box_id", pa.string()),
        pa.field("taken_at", pa.int64()),
        pa.field("feature", pa.string()),
        pa.field("present", pa.bool_()),          # NULL = 看不清的不完整观察
        pa.field("face_hint", pa.string()),
    ]
)

BOX_COUNT_SCHEMA = pa.schema(
    [
        pa.field("box_id", pa.string()),
        pa.field("face", pa.string()),
        pa.field("count", pa.int64()),
    ]
)

ORDER_SCHEMA = pa.schema(
    [
        pa.field("order_id", pa.string()),
        pa.field("night", pa.int64()),
        pa.field("machine", pa.string()),
        pa.field("slot", pa.string()),       # 一个字位一行：全标量扁平 Parquet
        pa.field("face", pa.string()),
        pa.field("source_box", pa.string()),
        pa.field("allowed_routes", pa.string()),  # 逗号分隔，如 "recompose,seal"
    ]
)

# 看清的压样：每行一个人工标记。present=None 即“看不清”。
# B2 的 P4 只留下甲乙共有的竖/横（不完整观察），单粒无法辨认；
# 但 P3 确定甲、P5/P6 确定乙后，整盒铸字数量（甲2乙2）把 P4 唯一逼成甲。
CLEAR_MARKS = [
    # P1：甲（B1）
    ("pr-1", "P1", "B1", 10, "竖", True, None),
    ("pr-1", "P1", "B1", 10, "横", True, None),
    ("pr-1", "P1", "B1", 10, "缺口J", True, "甲"),
    ("pr-1", "P1", "B1", 10, "缺口Y", False, None),
    # P2：乙（B1），竖只留了一次“看不清”——不完整观察不影响其余看清结论
    ("pr-2", "P2", "B1", 20, "竖", None, None),
    ("pr-2", "P2", "B1", 20, "横", True, None),
    ("pr-2", "P2", "B1", 20, "缺口Y", True, "乙"),
    ("pr-2", "P2", "B1", 20, "缺口J", False, None),
    # P3：甲（B2），缺口特征看得清
    ("pr-3", "P3", "B2", 30, "竖", True, None),
    ("pr-3", "P3", "B2", 30, "横", True, None),
    ("pr-3", "P3", "B2", 30, "缺口J", True, None),
    ("pr-3", "P3", "B2", 30, "缺口Y", False, "甲"),
    # P4：只有不完整观察——竖/横看得清，两个缺口都没压过，单粒候选 {甲,乙}
    ("pr-4", "P4", "B2", 40, "竖", True, None),
    ("pr-4", "P4", "B2", 40, "横", True, None),
    # P5：乙（B2）
    ("pr-5", "P5", "B2", 50, "竖", True, None),
    ("pr-5", "P5", "B2", 50, "横", True, None),
    ("pr-5", "P5", "B2", 50, "缺口Y", True, "乙"),
    ("pr-5", "P5", "B2", 50, "缺口J", False, None),
    # P6：乙（B2），只看得清乙的独有缺口；甲乙共有笔画未压
    ("pr-6", "P6", "B2", 60, "缺口Y", True, None),
    ("pr-6", "P6", "B2", 60, "缺口J", False, None),
    # 迟到两行：两晚期间才压的样，必须被时间谓词排除
    ("pr-late", "P3", "B2", 140, "缺口J", False, None),
    ("pr-late2", "P4", "B2", 150, "缺口J", False, None),
]

# vague 版：P3、P5 的缺口标记也改成“看不清”并抹掉人工 face_hint。
# 此时 B2 中只有 P6 能确定为乙；P3/P4/P5 的单粒候选都是 {甲,乙}，
# 剩余配额“甲2乙1”落在三粒上有三种分配，没有一粒能被唯一逼出，
# 整盒数量也无法替双甲字位排除出可交付的甲——必须拒接。
VAGUE_MARKS = [
    (m[0], m[1], m[2], m[3], m[4],
     None if (m[1] in ("P3", "P5") and m[4].startswith("缺口")) else m[5],
     None if m[1] in ("P3", "P5") else m[6])
    for m in CLEAR_MARKS
]

BOX_COUNTS = [
    ("B1", "甲", 1),
    ("B1", "乙", 1),
    ("B2", "甲", 2),
    ("B2", "乙", 2),
]


def _proof_table(marks: list[tuple]) -> pa.Table:
    cols = zip(*marks)
    arrays = [pa.array(col, type=field.type) for col, field in zip(cols, PROOF_SCHEMA)]
    return pa.table({f.name: a for f, a in zip(PROOF_SCHEMA, arrays)}, schema=PROOF_SCHEMA)


def _write_proofs(name: str, marks: list[tuple]) -> None:
    # 三个行组：前 20 行（taken_at 均 < 100）拆成两组，最后两行迟到样
    # （taken_at=140/150）单独成组——该组 footer min 已 >= 100，
    # 时间谓词可证明跳过，与 pqscan 主线的保守剪枝行为一致。
    table = _proof_table(marks)
    groups = [table.slice(0, 10), table.slice(10, 10), table.slice(20)]
    path = os.path.join(PRESS_DIR, name)
    writer = pq.ParquetWriter(path, PROOF_SCHEMA, write_statistics=True)
    try:
        for group in groups:
            writer.write_table(group)
    finally:
        writer.close()


def _write_box_counts() -> None:
    table = pa.table(
        {
            "box_id": [r[0] for r in BOX_COUNTS],
            "face": [r[1] for r in BOX_COUNTS],
            "count": [r[2] for r in BOX_COUNTS],
        },
        schema=BOX_COUNT_SCHEMA,
    )
    pq.write_table(table, os.path.join(PRESS_DIR, "box_counts.parquet"))


def _write_orders(name: str, orders: list[tuple]) -> None:
    """orders: 每行 (订单, 晚, 机器, 字位, 字面, 来源盒, 路径串)，一个字位一行。"""
    table = pa.table(
        {
            "order_id": [r[0] for r in orders],
            "night": [r[1] for r in orders],
            "machine": [r[2] for r in orders],
            "slot": [r[3] for r in orders],
            "face": [r[4] for r in orders],
            "source_box": [r[5] for r in orders],
            "allowed_routes": [r[6] for r in orders],
        },
        schema=ORDER_SCHEMA,
    )
    pq.write_table(table, os.path.join(PRESS_DIR, name))


def _job_json(
    name: str, proof_file: str, order_file: str, *,
    table_slots: dict[int, int], old_forms: list[dict], sealed_forms: list[dict],
    extra_predicate: dict | None = None,
) -> dict:
    return {
        "name": name,
        "nights": NIGHTS,
        "proof_files": [proof_file],
        "box_count_file": "box_counts.parquet",
        "order_file": order_file,
        "faces": FACES,
        "open_boxes": {"0": ["B1", "B2"], "1": ["B1", "B2"]},
        "table_slots": {str(k): v for k, v in table_slots.items()},
        "old_forms": old_forms,
        "sealed_forms": sealed_forms,
        "proof_predicate": extra_predicate,
    }


def main() -> None:
    os.makedirs(PRESS_DIR, exist_ok=True)
    _write_proofs("proofs_clear.parquet", CLEAR_MARKS)
    _write_proofs("proofs_vague.parquet", VAGUE_MARKS)
    _write_box_counts()

    # 小单：两台同时要甲（限定 B1，盒内仅甲乙各一）。
    _write_orders(
        "orders_small_busy.parquet",
        [
            ("O1", 0, "M1", "s1", "甲", "B1", "recompose,seal"),
            ("O2", 0, "M2", "s1", "甲", "B1", "recompose,seal"),
        ],
    )
    # 小单变体：第二台改要乙。
    _write_orders(
        "orders_small_switch.parquet",
        [
            ("O1", 0, "M1", "s1", "甲", "B1", "recompose,seal"),
            ("O2", 0, "M2", "s1", "乙", "B1", "recompose,seal"),
        ],
    )
    # 压样翻转：B2 双甲字位；clear 能辨认 P3/P4，vague 下 P4 无法有凭据交付。
    _write_orders(
        "orders_proof_flip.parquet",
        [
            ("Ob", 1, "M3", "s1", "甲", "B2", "recompose,seal"),
            ("Ob", 1, "M3", "s2", "甲", "B2", "recompose,seal"),
        ],
    )

    # 台面翻转：两张票都只能现场拆版（旧版 OF1 占着 P3/P4），第二晚。
    table_rows = [
        ("Ta", 1, "M1", "s1", "甲", None, "recompose"),
        ("Tb", 1, "M2", "s1", "甲", None, "recompose"),
    ]
    _write_orders("orders_table_flip.parquet", table_rows)
    old_forms = [{"form_id": "OF1", "pieces": ["P3", "P4"]}]

    # 封版演示：封 P1 两晚占用，第一晚另一台仍想要 P1 必须被拒。
    _write_orders(
        "orders_lock.parquet",
        [("Ol", 0, "M2", "s1", "甲", "B1", "recompose,seal")],
    )
    sealed_forms = [{"form_id": "SF1", "pieces": ["P1"], "machine": "M1", "print_night": 1}]

    jobs = {
        "job_small_busy.json": _job_json(
            "小单-两台同要甲-应拒一笔", "proofs_clear.parquet",
            "orders_small_busy.parquet", table_slots={0: 2, 1: 2},
            old_forms=[], sealed_forms=[],
        ),
        "job_small_switch.json": _job_json(
            "小单-第二台改要乙-应全接", "proofs_clear.parquet",
            "orders_small_switch.parquet", table_slots={0: 2, 1: 2},
            old_forms=[], sealed_forms=[],
        ),
        "job_proof_flip_clear.json": _job_json(
            "压样翻转-看得清-双甲可接", "proofs_clear.parquet",
            "orders_proof_flip.parquet", table_slots={0: 1, 1: 2},
            old_forms=[], sealed_forms=[],
        ),
        "job_proof_flip_vague.json": _job_json(
            "压样翻转-缺口看不清-只一粒可交付应拒", "proofs_vague.parquet",
            "orders_proof_flip.parquet", table_slots={0: 1, 1: 2},
            old_forms=[], sealed_forms=[],
        ),
        "job_table_flip_tight.json": _job_json(
            "台面翻转-仅一个时段-第二张应拒", "proofs_clear.parquet",
            "orders_table_flip.parquet", table_slots={0: 0, 1: 1},
            old_forms=old_forms, sealed_forms=[],
        ),
        "job_table_flip_full.json": _job_json(
            "台面翻转-两个时段-两张全接", "proofs_clear.parquet",
            "orders_table_flip.parquet", table_slots={0: 0, 1: 2},
            old_forms=old_forms, sealed_forms=[],
        ),
        "job_lock.json": _job_json(
            "封版-P1 两晚锁死-同字另票应拒", "proofs_clear.parquet",
            "orders_lock.parquet", table_slots={0: 1, 1: 1},
            old_forms=[], sealed_forms=sealed_forms,
        ),
    }
    for fname, doc in jobs.items():
        with open(os.path.join(PRESS_DIR, fname), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
    print(f"wrote {len(jobs)} press jobs and parquet fixtures into {PRESS_DIR}")


if __name__ == "__main__":
    main()
