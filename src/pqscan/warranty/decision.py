"""接单决策：为一张含若干实物锅的整单出「接单 / 条件接单 / 拒签」操作单。

三种可选的整单办法（method，每张单选定一种）：

- ``record``  ：保留原锅，凭每只锅的生产记录直接担保。只有当每只锅在**一切一致朝向**
                下都避开疑似致裂组合时才接单，否则拒签。不消耗库存、不拆见证锅。
- ``replace`` ：原记录安全的锅照常保留；其余锅用库内已验证合格的替换锅顶替，
                原锅留置。一件库存只能给一单，交出即消耗；库存不足即拒签。不拆见证锅。
- ``cut``     ：原记录安全的锅照常保留；其余锅若其每一个疑似可能组都有**同底釉同温区**
                的见证锅可拆，则出「待拆检」条件单（阴性放行、阳性隔离该组），
                否则拒签。不消耗库存。
- ``auto``    ：逐只锅按 record → replace → cut 的次序取第一个可行办法（默认）。

核心判据只用扫描证据与外部清单，绝不看任何拆检结果：

- 一只锅的可能组合来自 reconstruct.PotRecords.possible_groups；
  为 None（底釉缺失 / 挂架读数矛盾）时永远不能凭生产记录放行。
- 「疑似可能组」= 可能组合里命中 ProblemConfig 的那些；一只锅只要可能落入疑似组，
  就不能断言安全（除非落入它的每一种可能都被替换或见证锅覆盖）。
- 「同组」充分性：见证锅的 (底釉, 温区) 必须与待澄清锅的对应疑似可能组**完全相等**。
- 见证锅阴性只清除它自己那一组；阳性只隔离它那一组。不得把某只锅无裂外推到别的组，
  也不得用一只锅的阴性结果去放行另一只锅。

cut 办法产生的 ``accept_pending_cut`` 是**条件**单：操作单同时写明阴性/阳性两条分支，
不预挑分支。只有拆检结果真正到手后，才用 resolve_after_cut() 落实终态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Group, InventoryItem, ProblemConfig, WitnessPot
from .reconstruct import PotRecords

METHODS = ("auto", "record", "replace", "cut")

SAFE = "safe_record"
REPLACED = "replaced"
PENDING = "witness_pending"
UNRESOLVED = "unresolved"

ACCEPT = "accept"
ACCEPT_PENDING_CUT = "accept_pending_cut"
REFUSE = "refuse"


def _labels(groups: set[Group]) -> list[str]:
    return sorted(g.label() for g in groups)


@dataclass
class PotDisposition:
    pot_id: str
    method_used: str                       # record | replace | cut | none
    status: str                            # safe_record|replaced|witness_pending|unresolved
    shipped_as: str | None                 # "original" | item_id | "pending_cut" | None
    possible_groups: list[str] | None
    suspect_possible: list[str]
    hanger_id: str
    evidence_unknown_reason: str | None
    source_records: list[dict[str, Any]]
    required_groups: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pot_id": self.pot_id,
            "method_used": self.method_used,
            "hanger_id": self.hanger_id,
            "status": self.status,
            "shipped_as": self.shipped_as,
            "possible_groups": self.possible_groups,
            "suspect_possible": self.suspect_possible,
            "required_groups": self.required_groups,
            "evidence_unknown_reason": self.evidence_unknown_reason,
            "source_records": self.source_records,
            "notes": self.notes,
        }


@dataclass
class OperationSheet:
    order_id: str
    method: str
    verdict: str
    dispositions: list[PotDisposition]
    replacements: dict[str, str]
    replacement_sources: dict[str, Any]
    cuts: list[dict[str, Any]]
    inventory_consumed: list[str]
    witnesses_scrapped: list[str]
    branches: dict[str, Any]
    leftover_inventory: list[str]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "method": self.method,
            "verdict": self.verdict,
            "summary": self.summary,
            "dispositions": [d.to_dict() for d in self.dispositions],
            "replacements": self.replacements,
            "replacement_sources": self.replacement_sources,
            "cuts": self.cuts,
            "inventory_consumed": self.inventory_consumed,
            "witnesses_scrapped": self.witnesses_scrapped,
            "leftover_inventory": self.leftover_inventory,
            "branches": self.branches,
        }


@dataclass
class _Ctx:
    available_items: list[InventoryItem]
    witness_by_group: dict[Group, WitnessPot]
    replacements: dict[str, str] = field(default_factory=dict)
    replacement_sources: dict[str, Any] = field(default_factory=dict)
    inventory_consumed: list[str] = field(default_factory=list)
    cut_groups: dict[Group, WitnessPot] = field(default_factory=dict)


def _classify(pot: PotRecords, problem: ProblemConfig) -> tuple[str, set[Group]]:
    if pot.possible_groups is None:
        return "unknown_evidence", set()
    suspect = {g for g in pot.possible_groups if problem.is_suspect(g.glaze, g.zone)}
    if not suspect:
        return SAFE, set()
    return "needs_action", suspect


def _record(pot: PotRecords, stage: str, suspect: set[Group], possible_labels: list[str] | None,
            src: list[dict[str, Any]]) -> PotDisposition | None:
    if stage == SAFE:
        return PotDisposition(
            pot_id=pot.pot_id, method_used="record", status=SAFE, shipped_as="original",
            possible_groups=possible_labels, suspect_possible=[],
            hanger_id=pot.hanger_id, evidence_unknown_reason=None, source_records=src,
            notes="在一切一致朝向下都避开疑似组合，原锅直接凭生产记录担保",
        )
    return None


def _replace(pot: PotRecords, ctx: _Ctx, stage: str, suspect: set[Group],
             possible_labels: list[str] | None, src: list[dict[str, Any]]) -> PotDisposition | None:
    if not ctx.available_items:
        return None
    item = ctx.available_items.pop(0)
    ctx.replacements[pot.pot_id] = item.item_id
    ctx.replacement_sources[item.item_id] = item.source
    ctx.inventory_consumed.append(item.item_id)
    if stage == "unknown_evidence":
        reason = pot.unknown_reason
        note = f"证据不足（{reason}），以库内合格锅 {item.item_id} 替换，原锅留置"
        suspect_labels: list[str] = []
    else:
        reason = None
        note = f"疑似组合 {_labels(suspect)} 无法当场排除，用库内合格锅 {item.item_id} 替换，原锅留置"
        suspect_labels = _labels(suspect)
    return PotDisposition(
        pot_id=pot.pot_id, method_used="replace", status=REPLACED, shipped_as=item.item_id,
        possible_groups=possible_labels, suspect_possible=suspect_labels,
        hanger_id=pot.hanger_id, evidence_unknown_reason=reason, source_records=src,
        notes=note,
    )


def _cut(pot: PotRecords, ctx: _Ctx, stage: str, suspect: set[Group],
         possible_labels: list[str] | None, src: list[dict[str, Any]]) -> PotDisposition | None:
    if stage != "needs_action":
        return None  # 证据不足时组未知，无法选见证锅
    if not all(g in ctx.witness_by_group for g in suspect):
        return None
    for g in suspect:
        ctx.cut_groups.setdefault(g, ctx.witness_by_group[g])
    return PotDisposition(
        pot_id=pot.pot_id, method_used="cut", status=PENDING, shipped_as="pending_cut",
        possible_groups=possible_labels, suspect_possible=_labels(suspect),
        hanger_id=pot.hanger_id, evidence_unknown_reason=None, source_records=src,
        required_groups=_labels(suspect),
        notes=f"疑似组合 {_labels(suspect)} 可由同组见证锅拆检澄清；阴性放行、阳性隔离该组",
    )


def _unresolved(pot: PotRecords, stage: str, suspect: set[Group], possible_labels: list[str] | None,
                src: list[dict[str, Any]], method: str) -> PotDisposition:
    if stage == "unknown_evidence":
        note = f"证据不足（{pot.unknown_reason}）且所选办法 {method} 不能放行"
        suspect_labels: list[str] = []
        reason = pot.unknown_reason
    else:
        note = f"疑似组合 {_labels(suspect)} 在所选办法 {method} 下既不能记录担保、也未被替换/同组见证锅覆盖"
        suspect_labels = _labels(suspect)
        reason = None
    return PotDisposition(
        pot_id=pot.pot_id, method_used="none", status=UNRESOLVED, shipped_as=None,
        possible_groups=possible_labels, suspect_possible=suspect_labels,
        hanger_id=pot.hanger_id, evidence_unknown_reason=reason, source_records=src,
        required_groups=suspect_labels, notes=note,
    )


def decide(
    order_id: str,
    pots: list[PotRecords],
    problem: ProblemConfig,
    inventory: list[InventoryItem],
    witnesses: list[WitnessPot],
    method: str = "auto",
) -> OperationSheet:
    """为一张订单出操作单。method ∈ {"auto","record","replace","cut"}，整单选定一种。"""
    if method not in METHODS:
        raise ValueError(f"未知办法 {method!r}，可选 {METHODS}")

    witness_by_group: dict[Group, WitnessPot] = {}
    for wp in witnesses:
        g = wp.group
        if g is not None and g not in witness_by_group:
            witness_by_group[g] = wp
    ctx = _Ctx(available_items=list(inventory), witness_by_group=witness_by_group)

    dispositions: list[PotDisposition] = []
    unresolved: list[str] = []

    for pot in pots:
        stage, suspect = _classify(pot, problem)
        possible_labels = None if pot.possible_groups is None else _labels(pot.possible_groups)
        src = pot.source_records()

        # 无论哪种办法，原记录安全都直接保留原锅。
        disp = _record(pot, stage, suspect, possible_labels, src)
        if disp is None:
            order = {
                "record": (),
                "replace": (_replace,),
                "cut": (_cut,),
                "auto": (_replace, _cut),
            }[method]
            for fn in order:
                disp = fn(pot, ctx, stage, suspect, possible_labels, src)
                if disp is not None:
                    break
        if disp is None:
            disp = _unresolved(pot, stage, suspect, possible_labels, src, method)
        if disp.status == UNRESOLVED:
            unresolved.append(pot.pot_id)
        dispositions.append(disp)

    cuts = [
        {
            "group": g.to_dict(),
            "witness_pot_id": wp.pot_id,
            "witness_source": wp.source,
            "if_negative": f"清除组 {g.label()}，其上按该组放行的锅可发货",
            "if_positive": f"隔离组 {g.label()}，该组内锅不得发货（如改办法仍无库存则拒签）",
        }
        for g, wp in ctx.cut_groups.items()
    ]

    branches: dict[str, Any] = {}
    pending = [d for d in dispositions if d.status == PENDING]
    for g, wp in ctx.cut_groups.items():
        affected = [d.pot_id for d in pending if g.label() in d.required_groups]
        branches[g.label()] = {
            "witness_pot_id": wp.pot_id,
            "negative": {"group_cleared": g.label(), "pots_released": affected},
            "positive": {"group_quarantined": g.label(), "pots_blocked": affected,
                         "then": "该组内锅不得发货，需改用替换或拒签"},
        }

    if unresolved:
        verdict = REFUSE
    elif pending:
        verdict = ACCEPT_PENDING_CUT
    else:
        verdict = ACCEPT

    n_safe = sum(d.status == SAFE for d in dispositions)
    n_rep = sum(d.status == REPLACED for d in dispositions)
    if verdict == ACCEPT:
        summary = (f"接单（办法 {method}）：{len(pots)} 只锅全部可放行——"
                   f"原记录担保 {n_safe} 只、库内替换 {n_rep} 只，无条件。")
    elif verdict == ACCEPT_PENDING_CUT:
        summary = (f"条件接单（办法 {method}）：{len(pending)} 只锅待 {len(cuts)} 只见证锅拆检；"
                   f"阴性组放行、阳性组隔离并改用替换或拒签，不预挑分支。")
    else:
        summary = (f"拒签（办法 {method}）：{len(unresolved)} 只锅无法在该办法下放行：{unresolved}。")

    return OperationSheet(
        order_id=order_id, method=method, verdict=verdict, dispositions=dispositions,
        replacements=ctx.replacements, replacement_sources=ctx.replacement_sources,
        cuts=cuts, inventory_consumed=ctx.inventory_consumed,
        witnesses_scrapped=[c["witness_pot_id"] for c in cuts],
        branches=branches, leftover_inventory=[it.item_id for it in ctx.available_items],
        summary=summary,
    )


@dataclass
class StockLedger:
    """跨订单台账：交出即占用、拆检即报废，后续订单不得再用。"""

    consumed_items: dict[str, str] = field(default_factory=dict)      # item_id -> order_id
    scrapped_witnesses: dict[str, str] = field(default_factory=dict)  # witness_pot_id -> order_id

    def commit(self, sheet: OperationSheet) -> None:
        for item_id in sheet.inventory_consumed:
            if item_id in self.consumed_items:
                raise ValueError(
                    f"库存项 {item_id} 已被订单 {self.consumed_items[item_id]} 占用，不能重复交出")
            self.consumed_items[item_id] = sheet.order_id
        for wid in sheet.witnesses_scrapped:
            if wid in self.scrapped_witnesses:
                raise ValueError(
                    f"见证锅 {wid} 已被订单 {self.scrapped_witnesses[wid]} 占用，不能重复使用")
            self.scrapped_witnesses[wid] = sheet.order_id

    def available_inventory(self, inventory: list[InventoryItem]) -> list[InventoryItem]:
        return [it for it in inventory if it.item_id not in self.consumed_items]

    def available_witnesses(self, witnesses: list[WitnessPot]) -> list[WitnessPot]:
        return [wp for wp in witnesses if wp.pot_id not in self.scrapped_witnesses]

    def reuse_verdict(self, *, item_id: str | None = None, witness_pot_id: str | None = None) -> dict[str, Any]:
        """回单查询：某件替换锅 / 见证锅还能否再用（跨单台账判定）。"""
        if item_id is not None:
            used = self.consumed_items.get(item_id)
            return {
                "kind": "inventory_item", "id": item_id,
                "reusable": used is None, "allocated_by_order": used,
                "reason": "尚未交出，可给别单" if used is None else f"已交给订单 {used}，不得再许给别单",
            }
        if witness_pot_id is not None:
            used = self.scrapped_witnesses.get(witness_pot_id)
            return {
                "kind": "witness_pot", "id": witness_pot_id,
                "reusable": used is None, "allocated_by_order": used,
                "reason": "尚未拆检，可保留" if used is None else f"已由订单 {used} 预留拆检并报废，不得再用",
            }
        raise ValueError("需要 item_id 或 witness_pot_id")


def resolve_after_cut(
    sheet: OperationSheet,
    cut_results: dict[str, str],
    inventory: list[InventoryItem],
) -> dict[str, Any]:
    """**拆检结果到手之后**才调用：把 accept_pending_cut 条件单落实为终态。

    cut_results: {组标签: "negative" | "positive"}。
    这是唯一读拆检结果的地方；decide() 绝不调用它以预挑分支。

    - 阴性：只清除该组，其上待放行的锅发货；
    - 阳性：只隔离该组，该组内锅不得发货——若还有可替换库存则改用替换，否则拒签。
      某锅需要多个组时，任一阳性即该锅被隔离；阴性不外推到别的组。
    """
    if sheet.verdict != ACCEPT_PENDING_CUT:
        return {"verdict": sheet.verdict, "note": "该操作单无待拆分支，无需拆检结果"}

    # 只可用「本单尚未承诺出去」的库存：本单已替换用掉的 R1/R2 不能再拿来补阳性分支。
    committed = set(sheet.inventory_consumed)
    remaining_inventory = [it for it in inventory if it.item_id not in committed]
    released: list[str] = []
    quarantined: list[str] = []
    replaced_after_positive: dict[str, str] = {}
    refused_after_positive: list[str] = []

    for d in sheet.dispositions:
        if d.status != PENDING:
            continue
        statuses = [cut_results.get(g) for g in d.required_groups]
        if any(s is None for s in statuses):
            return {"verdict": "incomplete",
                    "note": f"锅 {d.pot_id} 所需组 {d.required_groups} 结果不齐：{cut_results}"}
        if all(s == "negative" for s in statuses):
            released.append(d.pot_id)
        else:
            quarantined.append(d.pot_id)
            if remaining_inventory:
                replaced_after_positive[d.pot_id] = remaining_inventory.pop(0).item_id
            else:
                refused_after_positive.append(d.pot_id)

    final = ACCEPT if not refused_after_positive else REFUSE
    return {
        "verdict": final,
        "released": released,
        "quarantined": quarantined,
        "replaced_after_positive": replaced_after_positive,
        "refused_after_positive": refused_after_positive,
        "note": "仅由已发生的拆检结果决定；阴性未外推到别的组，也未用某锅无裂放行他锅",
    }
