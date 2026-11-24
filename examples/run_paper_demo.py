"""End-to-end demonstration of the community-paper guaranteed-quantity flow.

Run with:

    PYTHONPATH=. python3 examples/run_paper_demo.py

It exercises the whole mandated narrative against the real
:class:`~extcontract.IngestionLibrary` (the station files are ordinary
ingest documents) and the two cabinet operation modes:

  1. four mailboxes, two rounds of two households, intersection one ->
     the guaranteed reach is three and every household could be the misser;
  2. the station corrects the intersection to two -> the guarantee drops to
     two; restoring the record puts the lower bound back to three;
  3. the contract is raised to four households -> a one-household gap;
  4. the targeted cabinet must wait for 丁's named non-receipt, while the
     whole-line cabinet may sign line L on its added lower bound of one;
  5. the cheap 甲乙 line has added lower bound zero and cannot fill the gap;
  6. sealing L locks four real slots; the last slot goes to another contract;
  7. replaying a delivery receipt never double-credits.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extcontract import PaperCabinet, Route, load_contract  # noqa: E402

MATERIAL = ROOT / "materials/paper"
UNIVERSE = ["甲", "乙", "丙", "丁"]
ROUTES = [
    Route("L", "B1", frozenset(UNIVERSE)),
    Route("M", "B1", frozenset(["甲", "乙"])),
    Route("S", "B2", frozenset(["戊"])),
]


def banner(n: int, title: str) -> None:
    print(f"\n=== {n}. {title} ===")


def station(name: str) -> dict:
    return json.loads((MATERIAL / name).read_text(encoding="utf-8"))


def contract(guarantee: int, *, permitted=None) -> dict:
    return {
        "contract_no": "C1",
        "block": "B1",
        "guarantee": guarantee,
        "permitted": UNIVERSE if permitted is None else permitted,
        "makeup_edition": "mk-1",
        "deadline": "2026-12-01",
    }


def main() -> int:
    writer = load_contract(MATERIAL / "delivery-writer-contract.json", role="writer")
    reader = load_contract(MATERIAL / "delivery-reader-contract.json", role="reader")

    # 1. Baseline evidence: two rounds of two, intersection one --------------
    banner(1, "两轮各两户、交集一 -> 保底三户，四人皆可能漏投")
    line_cabinet = PaperCabinet(
        reader, writer, mode="whole_line", edition_slots=5,
        universes={"B1": UNIVERSE, "B2": ["戊"]}, routes=ROUTES,
    )
    assert line_cabinet.receive_station_file(station("D-BASE.json")).accepted
    verdict = line_cabinet.settle().data["blocks"]["B1"]
    print("lower bound:", verdict["lower_bound"],
          "| possible missers:", verdict["possible_missers"],
          "| histories:", verdict["history_count"])
    print("witness arrangement:", verdict["witness"])
    assert verdict["lower_bound"] == 3
    assert set(verdict["possible_missers"]) == set(UNIVERSE)

    line_cabinet.register_contract(contract(3))
    accounting = line_cabinet.accounting("C1").data
    print("认款:", accounting["credited"], "| 缺口:", accounting["residual_gap"])
    assert accounting["credited"] == 3 and accounting["residual_gap"] == 0

    # 2. Intersection corrected to two, then restored ------------------------
    banner(2, "交集更正为二 -> 保底降为二；恢复记录 -> 回到三")
    line_cabinet.receive_station_file(station("D-FIX2.json"))
    lowered = line_cabinet.accounting("C1").data
    print("交集=2 时认款:", lowered["credited"], "| 下界:", lowered["delivery_lower_bound"])
    assert lowered["credited"] == 2 and lowered["delivery_lower_bound"] == 2
    line_cabinet.receive_station_file(station("D-FIX1.json"))
    assert line_cabinet.accounting("C1").data["delivery_lower_bound"] == 3
    print("恢复后下界:", line_cabinet.accounting("C1").data["delivery_lower_bound"])

    # 3. Guarantee raised to four -> one household still missing --------------
    banner(3, "合同改保四户 -> 尚差一户")
    line_cabinet.register_contract(contract(4))
    gap = line_cabinet.accounting("C1").data
    print("认款:", gap["credited"], "| 缺口:", gap["residual_gap"])
    assert gap["credited"] == 3 and gap["residual_gap"] == 1

    # 4. Targeted cabinet waits; whole-line cabinet signs L ------------------
    banner(4, "定向柜等丁未收回执；整线柜按新增下界一签 L")
    targeted = PaperCabinet(
        reader, writer, mode="targeted", edition_slots=5,
        universes={"B1": UNIVERSE}, routes=ROUTES,
    )
    targeted.receive_station_file(station("D-BASE.json"))
    targeted.register_contract(contract(4))
    early = targeted.sign_targeted("C1", "丁")
    print("丁未回执前定向签丁:", early.status, "-", early.errors[0])
    assert not early.accepted
    targeted.receive_station_file(station("D-DING-MISS.json"))
    assert targeted.sign_targeted("C1", "丁").accepted
    print("收到具名未收回执后定向签丁: ACCEPTED")

    cheap = line_cabinet.sign_whole_line("C1", "M")
    print("便宜线路 M (甲乙):", cheap.status, "-", cheap.errors[0])
    assert not cheap.accepted
    signed_l = line_cabinet.sign_whole_line("C1", "L")
    assert signed_l.accepted
    print("整线 L 新增下界:", signed_l.data["order"]["basis"]["added_lower_bound"],
          "| 剩余版位:", signed_l.data["slot_state"]["available"])
    assert signed_l.data["order"]["basis"]["added_lower_bound"] == 1

    # 5. Sealing locks real slots; the last slot serves another contract -----
    banner(5, "封版占实版位，末位给另一合同；重放回执不重复认款")
    sealed = line_cabinet.seal_edition("mk-1")
    print("封版:", sealed.data["slot_state"])
    order_id = signed_l.data["order"]["order_id"]
    first = line_cabinet.deliver(order_id)
    second = line_cabinet.deliver(order_id)
    print("首次送达:", first.status, "| 重放:", second.message)
    assert "replayed" in second.message
    assert line_cabinet.accounting("C1").data["credited"] == 4

    line_cabinet.register_contract({
        "contract_no": "C2", "block": "B2", "guarantee": 1,
        "permitted": ["戊"], "makeup_edition": "mk-1",
    })
    last_slot = line_cabinet.sign_whole_line("C2", "S")
    print("C2 占用最后版位:", last_slot.data["slot_state"])
    assert last_slot.data["slot_state"]["available"] == 0

    # 6. Read-back by contract number, after replaying a station receipt -----
    banner(6, "按合同号读回版次、补投服务与依据")
    replay = line_cabinet.receive_station_file(station("D-BASE.json"))
    assert replay.ingest_result["receipt"]["replay"] is True
    readback = line_cabinet.readback("C1").data
    print("采用版次:", readback["makeup_edition_used"],
          "| 补投单:", [o["order_id"] for o in readback["orders"]],
          "| 认款:", readback["accounting"]["credited"])
    assert readback["makeup_edition_used"] == "mk-1"
    assert readback["accounting"]["credited"] == 4

    print("\nAll paper demo assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
