"""制播合同与代价台账。

两种业务看同一本资产目录、同一条临时改站，许可和代价按选中的合同兑现：

- 买断（BUYOUT）整词录音：锁住素材与档期，已签的整段不得换尾；
  补录必须约新的录音档（新母录、新档期）。
- 长期租用（RENTAL）短单元：尾字可跨班复用；每次选用的接头与听辨
  结论必须对应那次实际出声，台账逐次留痕；可被另一班独占排期。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ContractKind(str, Enum):
    BUYOUT = "buyout"
    RENTAL = "rental"


@dataclass(frozen=True)
class LedgerEntry:
    """一次制播动作的代价留痕（对应一次实际出声或一次买断）。"""

    trip_no: int
    contract: str
    action: str          # buyout_lock | rental_use | deferred_order
    asset_refs: tuple[str, ...]
    unit_price: int
    amount: int
    note: str = ""


@dataclass
class CostLedger:
    """单店代价台账；两家店各自一本，依据各自合同记账。"""

    name: str
    entries: list[LedgerEntry] = field(default_factory=list)

    def charge(self, trip_no: int, kind: ContractKind, action: str,
               asset_refs: tuple[str, ...], unit_price: int,
               quantity: int = 1, note: str = "") -> LedgerEntry:
        entry = LedgerEntry(
            trip_no=trip_no,
            contract=kind.value,
            action=action,
            asset_refs=tuple(asset_refs),
            unit_price=unit_price,
            amount=unit_price * quantity,
            note=note,
        )
        self.entries.append(entry)
        return entry

    def total(self) -> int:
        return sum(e.amount for e in self.entries)
