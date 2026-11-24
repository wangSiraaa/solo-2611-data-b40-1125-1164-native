"""演示：同一署年申请在单项证据/许可改动下如何翻转。

基准案情（fixtures_bridge，最多四梁、跨度十二年）：
- 甲梁现砍现装，甲-1/甲-2 同树接回（中间漏读一环），带皮末轮；
  对齐候选末轮年 [42, 54] —— 54 正是另一次重装；
- 乙梁库存旧材，末轮 39 年，与甲同次装入（只压下界，不能把桥提前三年）；
- 丙梁现砍现装，锁定重装 = 54 年；丁梁库存旧材，末轮 36 年；
- J1：甲榫切穿丁卯（丁先就位），盖住甲的 8..9 环，初建后封存。

演示矩阵（每次只改一项，其余与基准一致）：
- 基准：不动实物 -> 签 初建42年；
- D1 只改单片证据（甲-2 带皮末轮改为缺）-> 只能给下界，暂缓；
- D2 只改封榫年代（J1 改为重装后封存）-> 甲可能属重装，42/54 并存，暂缓；
- D3 只改查看权（J1 被盖外圈不可引用）-> 末轮候选退化为下界，暂缓；
- D4 在 D3 背景下只改取样许可：无许可 -> 暂缓；有许可 -> 微芯定稿 42 年
  （并登记代价）；之后再申请微芯 -> “只可开一处”拒绝。
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Any

import pyarrow.parquet as pq
import pyarrow as pa

from .plaque import (Application, CRAFT_MICROCORE, CRAFT_NONINVASIVE,
                     issue_plaque)

SEGMENT = "南桥段"
TARGET = "甲"


def _copy_fixtures(fixtures_dir: str, dst: str) -> str:
    shutil.copytree(fixtures_dir, dst)
    return dst


def _edit_workorder(fixtures_dir: str, **changes: Any) -> None:
    path = os.path.join(fixtures_dir, "workorder.json")
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    for key, value in changes.items():
        if key == "sealed_after":
            doc["joints"][0]["sealed_after"] = value
        elif key == "view":
            doc["permits"]["view"]["J1"] = value
        elif key == "sample":
            doc["permits"]["sample"]["J1"] = value
        else:  # pragma: no cover - 防误用
            raise KeyError(key)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)


def _drop_bark_edge(fixtures_dir: str, beam_fragment: str) -> None:
    """单片证据改动：把指定片段的带皮末轮标记改为缺，行组布局保持不变。"""
    path = os.path.join(fixtures_dir, "archive", "beam_readings.parquet")
    pf = pq.ParquetFile(path)
    groups = [pf.read_row_group(i).to_pylist() for i in range(pf.num_row_groups)]
    for rows in groups:
        for row in rows:
            if row["fragment"] == beam_fragment:
                row["bark_edge"] = 0
    schema = pf.schema_arrow
    writer = pq.ParquetWriter(path, schema, write_statistics=True)
    for rows in groups:
        writer.write_table(pa.Table.from_pylist(rows, schema=schema))
    writer.close()


def _brief(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "plaque_id": rec["plaque_id"],
        "status": rec["status"],
        "dating": rec["dating"],
        "possibilities": rec["possibilities"],
        "problems": rec["problems"],
    }


def run_demo(fixtures_dir: str, out_dir: str) -> list[dict[str, Any]]:
    """跑完整演示矩阵，返回每一步的单号与结果（每步独立台账、独立 out 子目录）。"""
    steps: list[dict[str, Any]] = []

    def step(name: str, change: str, rec: dict[str, Any]) -> None:
        steps.append({"demo": name, "change": change, **_brief(rec)})

    # 基准：不动实物，签 初建42年。
    rec = issue_plaque(fixtures_dir, os.path.join(out_dir, "base"),
                       Application(SEGMENT, TARGET, CRAFT_NONINVASIVE))
    step("基准", "无", rec)

    # D1 只改单片证据：甲-2 带皮末轮 -> 缺。
    d1 = _copy_fixtures(fixtures_dir, os.path.join(out_dir, "d1_fixtures"))
    _drop_bark_edge(d1, "甲-2")
    rec = issue_plaque(d1, os.path.join(out_dir, "d1"),
                       Application(SEGMENT, TARGET, CRAFT_NONINVASIVE))
    step("D1 单片证据", "甲-2 带皮末轮标记改为缺", rec)

    # D2 只改封榫年代：J1 初建后 -> 重装后。
    d2 = _copy_fixtures(fixtures_dir, os.path.join(out_dir, "d2_fixtures"))
    _edit_workorder(d2, sealed_after="重装")
    rec = issue_plaque(d2, os.path.join(out_dir, "d2"),
                       Application(SEGMENT, TARGET, CRAFT_NONINVASIVE))
    step("D2 封榫年代", "J1 封存由初建后改为重装后", rec)

    # D3 只改查看权：J1 被盖外圈不得引用。
    d3 = _copy_fixtures(fixtures_dir, os.path.join(out_dir, "d3_fixtures"))
    _edit_workorder(d3, view=False)
    rec = issue_plaque(d3, os.path.join(out_dir, "d3"),
                       Application(SEGMENT, TARGET, CRAFT_NONINVASIVE))
    step("D3 查看权", "J1 查看权收回", rec)

    # D4 在 D3 背景下只改取样许可：先无许可（暂缓），再有许可（微芯定稿）。
    d4 = _copy_fixtures(fixtures_dir, os.path.join(out_dir, "d4_fixtures"))
    _edit_workorder(d4, view=False)
    rec = issue_plaque(d4, os.path.join(out_dir, "d4_no_permit"),
                       Application(SEGMENT, TARGET, CRAFT_MICROCORE))
    step("D4 取样许可（无）", "J1 查看权收回且无取样许可，微芯工艺", rec)

    _edit_workorder(d4, sample=True)
    d4_out = os.path.join(out_dir, "d4_permit")
    rec = issue_plaque(d4, d4_out, Application(SEGMENT, TARGET, CRAFT_MICROCORE))
    step("D4 取样许可（有）", "仅放开 J1 取样许可，微芯定稿", rec)

    # 同一台账再申请一次微芯：只可开一处，拒绝。
    rec = issue_plaque(d4, d4_out, Application(SEGMENT, "乙", CRAFT_MICROCORE))
    step("D4 再开第二处", "微芯已用，乙梁申请微芯", rec)

    return steps
