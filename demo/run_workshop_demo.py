"""布景工坊端到端演示：归一任务 -> 接单（封签/整卷）-> 双销位接班 -> 占用与复看。

运行：``python demo/run_workshop_demo.py``
产物：``data/demo_workshop_shop.db`` 等 SQLite 库与控制台报告。

覆盖题面三档事实：
1. 三拍一轮、两卷各三格：首拍 0/1 能交；第二卷标记改 2 卡在 A->B 缝；乙墙加高无段可裁；
2. 两面转台双销位：封签不能同时担保，整卷两现场各自兑现且实卷余量在不同端；
3. 他班已签本卷首格后，按真实占用重新受理，已许边头不会再裁给本班。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from confcat.catalog import Catalog
from confcat.ingest import TaskRepository
from confcat.workshop import WorkshopRepository
from confcat.workshop_model import MODE_SEALED, MODE_WHOLE_ROLL

# 演示库与中间 CSV 落在临时目录，不污染仓库内的 data/
_TMP = tempfile.TemporaryDirectory()
WORKDIR = Path(_TMP.name)
CATALOG_DB = WORKDIR / "demo_workshop_catalog.db"
TASK_DB = WORKDIR / "demo_workshop_tasks.db"
SHOP_DB = WORKDIR / "demo_workshop_shop.db"
BASIC_SHOP_DB = WORKDIR / "demo_workshop_basic_shop.db"
for f in (CATALOG_DB, TASK_DB, SHOP_DB, BASIC_SHOP_DB):
    f.unlink(missing_ok=True)
# 反例/再受理各自独立的工坊库
for name in ("mark2_shop.db", "tall_shop.db", "reserve_shop.db"):
    (WORKDIR / name).unlink(missing_ok=True)

ROLL_CONFIG = """
ingest:
  source_uri: file:///data/rolls.csv
  column_mapping:
    roll_id: rid
    pattern_period: cycle
    direction: side
    marker_phase: mark
    start_coord: c0
    end_coord: c1
"""

WALL_CONFIG = """
ingest:
  source_uri: file:///data/walls.csv
  column_mapping:
    scenario: scene
    wall_id: wid
    height: h
    first_beat: beat0
    pre_mounted: fixed
    from_wall: src
    to_wall: dst
    offset_beats: shift
"""


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def write_csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    with Catalog(CATALOG_DB) as cat:
        cat.publish("rolls", ROLL_CONFIG)
        cat.publish("walls", WALL_CONFIG)
        d = WORKDIR

        # 1. 基础算例：两卷各三格 ------------------------------------------------ #
        hr("1) 基础算例：三拍一轮，两卷各三格（首拍 0/1）")
        basic_roll = write_csv(
            d / "basic_rolls.csv",
            "rid,cycle,side,mark,c0,c1\nR1,3,正,0,0,2\nR2,3,正,1,0,2\n",
        )
        basic_wall = write_csv(
            d / "basic_walls.csv",
            "scene,wid,h,beat0,fixed,src,dst,shift\n"
            "default,A,3,0,,,\n"
            "default,B,3,,,,\n"
            "default,A,3,0,,A,B,1\n"
            "default,B,3,,,B,A,2\n",
        )
        with TaskRepository(TASK_DB, cat) as tasks, WorkshopRepository(BASIC_SHOP_DB, tasks) as shop:
            tasks.create_task("basic-roll", "rolls", "doc-0", basic_roll)
            tasks.create_task("basic-wall", "walls", "doc-0", basic_wall)
            ok = shop.place_order("basic-ok", "basic-roll", "basic-wall", ["A", "B"], MODE_SEALED)
            assert ok.guaranteed
            for wall, seg in ok.scenarios[0].assignments.items():
                print(f"  墙 {wall} <- 卷 {seg.roll_id} 坐标[{seg.start},{seg.end}]"
                      f" 首拍 {seg.first_beat} 末拍 {seg.last_beat}")

        # 反例各自用独立工坊库，避免已担保单的占用串扰（换卷/改料本就另办新单）
        hr("2) 反例：第二卷来源标记改成 2 -> 卡在 A->B 接缝")
        mark2 = write_csv(
            d / "mark2_rolls.csv",
            "rid,cycle,side,mark,c0,c1\nR1,3,正,0,0,2\nR2,3,正,2,0,2\n",
        )
        with TaskRepository(TASK_DB, cat) as tasks, WorkshopRepository(
            d / "mark2_shop.db", tasks
        ) as shop:
            tasks.create_task("mark2-roll", "rolls", "doc-0", mark2)
            blocked = shop.place_order(
                "basic-mark2", "mark2-roll", "basic-wall", ["A", "B"], MODE_SEALED
            )
            assert not blocked.guaranteed
            b = blocked.scenarios[0].blockages[0]
            print(f"  卡点: {b.detail} | 接缝 {b.seam} | 卷 {b.roll_id} 段 {b.segment}")
            assert b.seam == ("A", "B") and b.roll_id == "R2"

        hr("3) 反例：乙墙加高到 4 格 -> 同一张可裁清单不够长")
        tall_wall = write_csv(
            d / "tall_walls.csv",
            "scene,wid,h,beat0,fixed,src,dst,shift\n"
            "default,A,3,0,,,\n"
            "default,B,4,,,,\n"
            "default,A,3,0,,A,B,1\n"
            "default,B,4,,,B,A,2\n",
        )
        with TaskRepository(TASK_DB, cat) as tasks, WorkshopRepository(
            d / "tall_shop.db", tasks
        ) as shop:
            tasks.create_task("tall-wall", "walls", "doc-0", tall_wall)
            tall = shop.place_order(
                "basic-tall", "basic-roll", "tall-wall", ["A", "B"], MODE_SEALED
            )
            assert not tall.guaranteed
            print("  卡点:", tall.scenarios[0].blockages[0].detail)
            assert tall.scenarios[0].blockages[0].kind == "no-segment"

        # 2. 两面转台双销位 ------------------------------------------------------ #
        hr("4) 两面转台：甲已上台首拍0；一卷四格相位 1,2,0,1；甲乙双销位")
        turn_roll = write_csv(
            d / "turn_rolls.csv", "rid,cycle,side,mark,c0,c1\nR,3,正,1,0,3\n"
        )
        turn_wall = write_csv(
            d / "turn_walls.csv",
            "scene,wid,h,beat0,fixed,src,dst,shift\n"
            "pin-A,A,3,0,是,,\n"
            "pin-A,B,3,,,,\n"
            "pin-A,A,3,0,是,A,B,1\n"
            "pin-A,B,3,,,B,A,2\n"
            "pin-B,A,3,0,是,,\n"
            "pin-B,B,3,,,,\n"
            "pin-B,A,3,0,是,A,B,2\n"
            "pin-B,B,3,,,B,A,1\n",
        )
        with TaskRepository(TASK_DB, cat) as tasks, WorkshopRepository(SHOP_DB, tasks) as shop:
            tasks.create_task("turn-roll", "rolls", "doc-0", turn_roll)
            tasks.create_task("turn-wall", "walls", "doc-0", turn_wall)

            sealed = shop.place_order(
                "turn-sealed", "turn-roll", "turn-wall", ["A", "B"], MODE_SEALED
            )
            print("  封签定向段整单担保:", sealed.guaranteed, "（两销位要求互换，同一段纸无法兼顾）")
            for p in sealed.scenarios:
                if not p.guaranteed:
                    print("   ", p.scenario, "卡点:", p.blockages[0].detail)
            assert not sealed.guaranteed
            assert shop.current_occupancy() == {}  # 不能承诺的单不占料

            whole = shop.place_order(
                "turn-whole", "turn-roll", "turn-wall", ["A", "B"], MODE_WHOLE_ROLL
            )
            assert whole.guaranteed
            for p in whole.scenarios:
                seg = p.assignments["B"]
                print(f"  整卷 {p.scenario}: 墙B 从卷 {seg.roll_id}[{seg.start},{seg.end}] 下刀")
            ra = shop.realize_scenario("turn-whole", "pin-A")
            rb = shop.realize_scenario("turn-whole", "pin-B")
            print("  甲销接班后实卷余量:", ra["leftover_intervals"], "（尾端坐标3还在）")
            print("  乙销接班后实卷余量:", rb["leftover_intervals"], "（边头坐标0还在）")

        # 3. 他班已签首格后的再受理 --------------------------------------------- #
        hr("5) 另一班已签本卷首格：按真实占用再受理，已许边头不裁给本班")
        reserve_wall = write_csv(
            d / "reserve_walls.csv",
            "scene,wid,h,beat0,fixed,src,dst,shift\n"
            "default,A,1,,,,\n"
            # 单格段首末拍相同，自接缝错 2 拍几何可行（offset 0 永不成立）
            "default,A,1,,,A,A,2\n",
        )
        with TaskRepository(TASK_DB, cat) as tasks, WorkshopRepository(
            d / "reserve_shop.db", tasks
        ) as shop:
            tasks.create_task("reserve-wall", "walls", "doc-0", reserve_wall)
            # 他班先接单，领走本卷首格（坐标 0）
            reserve = shop.place_order(
                "reserve-edge", "turn-roll", "reserve-wall", ["A"], MODE_WHOLE_ROLL
            )
            assert reserve.guaranteed
            print("  他班领纸坐标:", shop.current_occupancy())
            assert shop.current_occupancy() == {"R": [(0, 0)]}

            # 本班再受理同一张双销位出车单
            again = shop.place_order(
                "turn-again", "turn-roll", "turn-wall", ["A", "B"], MODE_WHOLE_ROLL
            )
            for p in again.scenarios:
                status = "兑现" if p.guaranteed else "不能承诺: " + p.blockages[0].detail
                print(f"  本班 {p.scenario}: {status}")
            plans = {p.scenario: p for p in again.scenarios}
            assert not plans["pin-A"].guaranteed  # 甲销需要已许出的边头
            assert plans["pin-B"].guaranteed
            assert (plans["pin-B"].assignments["B"].start,
                    plans["pin-B"].assignments["B"].end) == (1, 3)
            # 整单因甲销不能承诺 -> 不新占任何卷料，已许边头仍归他班
            assert not again.guaranteed
            assert shop.current_occupancy() == {"R": [(0, 0)]}

        hr("6) 重开工坊库按单号复看（含当时任务实际行与映射来源）")
        with Catalog(CATALOG_DB) as cat2, TaskRepository(TASK_DB, cat2) as tasks2, \
                WorkshopRepository(SHOP_DB, tasks2) as shop2:
            order = shop2.get_order("turn-whole")
            print("  单号 turn-whole 担保:", order["guaranteed"], "| 墙序:", order["wall_order"])
            snap = shop2.get_task_snapshot("turn-roll")
            print("  卷料快照行数:", len(snap["rows"]),
                  "| 列血缘来源路径:", snap["lineage"][0]["provenance"]["path"][:2])
            print("  工坊全部单号:", [o["order_id"] for o in shop2.list_orders()])

    hr("全部工坊演示断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
