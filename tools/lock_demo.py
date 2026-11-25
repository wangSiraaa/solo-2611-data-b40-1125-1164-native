"""可重跑入口：缩尺船闸六场景 + 真实帧入观测仓 + 逐拍水账与闭闸理由。

用法
====

.. code-block:: bash

    python3.11 tools/lock_demo.py                 # 终端打印全部场景
    python3.11 tools/lock_demo.py --out artifacts/lock  # 另存 JSON 水账与 SQLite 仓

输出
----

* 终端：每个场景的可行性、错误码、闭闸理由，以及主交付场景（两船、四格侧池
  全回收）的逐拍水账。
* ``--out`` 目录：
  - ``lock_catalog.json`` 船闸帧目录产品（内容寻址、可长期复查）；
  - ``lock_frames.db`` 观测仓，语义校验通过的真实帧全部入库，拒绝帧单列；
  - ``lock_scenarios.json`` 六场景结构化结果（含逐拍账）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitcatalog.lock import (  # noqa: E402
    CHAMBER,
    SIDE,
    VALVE_DOWN,
    VALVE_SIDE,
    VALVE_UP,
)
from bitcatalog.lock_frames import (  # noqa: E402
    BUS,
    FRAME_GATE_CMD,
    FRAME_LEVEL_GAUGE,
    FRAME_SHIP_PHASE,
    FRAME_VALVE_CMD,
    build_lock_product,
    encode_gate_frame,
    encode_gauge_frame,
    encode_ship_frame,
    encode_valve_frame,
    ingest_lock_frames,
    validate_lock_frame,
)
from bitcatalog.lock_scenarios import (  # noqa: E402
    _standard_topology,
    run_all,
    scenario_two_ships_recycle,
)
from bitcatalog.warehouse import ObservationWarehouse  # noqa: E402

# 阀门/闸门编号（与帧目录的 id 字段约定一致）
VALVE_IDS = {VALVE_UP: 1, VALVE_SIDE: 2, VALVE_DOWN: 3, "valve_makeup": 4, "valve_drain": 5}
GATE_IDS = {"gate_up": 1, "gate_down": 2}


def header(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def print_schedule(schedule_json: dict, *, title: str) -> None:
    print(f"\n-- {title} --")
    print(f"{'拍':>3} {'类型':<8} {'水账(阀:源->目)':<34} {'闸室':>4} {'侧池':>4}  说明")
    for action in schedule_json["actions"]:
        transfers = ",".join(
            f"{t['valve']}:{t['from']}->{t['to']}" for t in action["valve_transfers"]
        ) or "-"
        print(
            f"{action['beat']:>3} {action['kind']:<8} {transfers:<34} "
            f"{action['levels'].get(CHAMBER, '-'):>4} {action['levels'].get(SIDE, '-'):>4}  "
            f"{action['note']}"
        )
    print(f"过闸拍：{schedule_json['pass_beats']}  阀累计：{schedule_json['valve_usage']}")


def build_demo_frames(product, topology):
    """把两船主场景的真实动作编成一序列真实帧（含故意非法帧用于验证拒绝）。"""

    events: list[dict] = []
    ns = 1_000_000_000  # 1 秒/拍，纳秒
    # 拍 1：上游阀开，正向 1 格
    events.append(
        dict(
            frame_event_id="lock-0001",
            bus=BUS,
            frame_id=FRAME_VALVE_CMD,
            occurred_at=ns,
            payload_hex=encode_valve_frame(
                product, valve_id=VALVE_IDS[VALVE_UP], direction=1, pulse_amount=1
            ).hex(),
        )
    )
    # 拍 2：关阀帧（selector=0，无脉冲字段）
    events.append(
        dict(
            frame_event_id="lock-0002",
            bus=BUS,
            frame_id=FRAME_VALVE_CMD,
            occurred_at=2 * ns,
            payload_hex=encode_valve_frame(
                product, valve_id=VALVE_IDS[VALVE_UP], direction=None
            ).hex(),
        )
    )
    # 拍 3：平水开门帧（4 == 4，合法）
    events.append(
        dict(
            frame_event_id="lock-0003",
            bus=BUS,
            frame_id=FRAME_GATE_CMD,
            occurred_at=3 * ns,
            payload_hex=encode_gate_frame(
                product, gate_id=GATE_IDS["gate_up"], open=True, left_level=4, right_level=4
            ).hex(),
        )
    )
    # 拍 4：故意不平的开门帧（3 != 4），语义层必须拒绝
    events.append(
        dict(
            frame_event_id="lock-0004-bad",
            bus=BUS,
            frame_id=FRAME_GATE_CMD,
            occurred_at=4 * ns,
            payload_hex=encode_gate_frame(
                product, gate_id=GATE_IDS["gate_down"], open=True, left_level=3, right_level=4
            ).hex(),
        )
    )
    # 拍 5：水位尺遥测（闸室 4 格，合法）
    events.append(
        dict(
            frame_event_id="lock-0005",
            bus=BUS,
            frame_id=FRAME_LEVEL_GAUGE,
            occurred_at=5 * ns,
            payload_hex=encode_gauge_frame(product, pool=CHAMBER, level=4).hex(),
        )
    )
    # 拍 6：水位尺越界（闸室容量 4，报 7），语义层必须拒绝
    events.append(
        dict(
            frame_event_id="lock-0006-bad",
            bus=BUS,
            frame_id=FRAME_LEVEL_GAUGE,
            occurred_at=6 * ns,
            payload_hex=encode_gauge_frame(product, pool=CHAMBER, level=7).hex(),
        )
    )
    # 拍 7：船阶段帧
    events.append(
        dict(
            frame_event_id="lock-0007",
            bus=BUS,
            frame_id=FRAME_SHIP_PHASE,
            occurred_at=7 * ns,
            payload_hex=encode_ship_frame(
                product, ship_id=1, phase="in_chamber", beat=7, pass_beat=10
            ).hex(),
        )
    )
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="结果输出目录（默认只打印）")
    args = parser.parse_args(argv)

    header("1. 六场景可行性总览")
    results = run_all()
    for result in results:
        flag = "可执行" if result.feasible else "闭闸"
        print(f"[{flag}] {result.name:<22} code={result.reason_code or '-'}")
        if result.reason:
            print(f"       理由：{result.reason}")

    header("2. 主交付：两船反向、四格侧池全回收（七格以内逐拍水账）")
    main_result = scenario_two_ships_recycle()
    print_schedule(main_result.detail["schedule"], title="甲下行 → 乙上行，侧池容量 4")
    print(f"\n守恒：{main_result.detail['finite_total_by_beat']}")
    print(f"回收规则：{main_result.detail['recycle_rule']}")

    header("3. 真实帧目录与门位控制校验（接入 bitcatalog 观测仓）")
    product = build_lock_product()
    topology = _standard_topology(side_capacity=4)
    print(f"船闸目录产品 sha256 = {product.fingerprint}")
    print(f"消息数 = {len(product.messages)}（gate_cmd/valve_cmd/level_gauge/ship_phase）")
    events = build_demo_frames(product, topology)

    semantic_rejections: list[dict] = []
    accepted_events = []
    for event in events:
        payload = bytes.fromhex(event["payload_hex"])
        verdict = validate_lock_frame(product, payload, frame_id=event["frame_id"], topology=topology)
        if verdict.accepted:
            accepted_events.append(event)
            print(f"  接受 {event['frame_event_id']:<14} frame={event['frame_id']}")
        else:
            semantic_rejections.append({"frame_event_id": event["frame_event_id"], **verdict.as_json()})
            print(
                f"  拒绝 {event['frame_event_id']:<14} frame={event['frame_id']} "
                f"code={verdict.code}：{verdict.message}"
            )

    out_dir = Path(args.out) if args.out else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        product.save(out_dir / "lock_catalog.json")
        db_path = out_dir / "lock_frames.db"
        if db_path.exists():
            db_path.unlink()
        with ObservationWarehouse(db_path) as warehouse:
            ingestion = ingest_lock_frames(warehouse, product, events, topology=topology)
            counts = warehouse.counts(product)
        payload = {
            "scenarios": [result.as_json() for result in results],
            "frame_ingestion": {
                "accepted": ingestion.accepted,
                "rejected_semantic": list(ingestion.rejected_semantic),
                "warehouse_counts": counts,
            },
        }
        (out_dir / "lock_scenarios.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\n已写出：{out_dir / 'lock_catalog.json'}")
        print(f"        {out_dir / 'lock_frames.db'}（观测仓计数 {counts}）")
        print(f"        {out_dir / 'lock_scenarios.json'}")
    else:
        # 即使不落盘也用内存仓跑一遍入库桥接，证明端到端可用。
        with ObservationWarehouse(":memory:") as warehouse:
            ingestion = ingest_lock_frames(warehouse, product, events, topology=topology)
            print(
                f"\n内存观测仓端到端：语义接受 {ingestion.accepted} 帧、"
                f"语义拒绝 {len(ingestion.rejected_semantic)} 帧"
            )

    header("4. 题面三问的直接回答")
    print("(1) 单船四格水：逐拍一格，第 4 拍闸室到 4 格与上游平，第 5 拍准开上门。")
    print("(2) 末脉冲若进了侧池：本池只到 3 格，差 1 格不能平水；两套相容账必须")
    print("    逐套核算，禁止把闸室/侧池上限相加，按差格账闭闸。")
    print("(3) 两船反向：甲第 6-9 拍把 4 格水收入侧池，回收水第 12 拍起才能被乙")
    print("    调用——未到账的水不得提前支配；乙第 16 拍出闸，全程有限容器水量守恒。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
