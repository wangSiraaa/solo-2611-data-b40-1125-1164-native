"""布牌员：按入口方向、相邻去向与底座锁边，判定每条入口的触读是否唯一。

判定模型
========
每个展位入口挂一块制好的牌（:class:`FabricatedSign`）与该入口的**实际去向**。
访客从某一侧走近（``approach``：``L``/``R``），其触觉参照系以“自己摸到的左端”
为 0 号格，因此需要把牌的触读图样再按入口侧做一次镜像，才是访客真正摸到的
签名。底座允许的每个朝向都会产生一个可达签名。

唯一性
------
对每条入口，把它可达的每个触读签名，与**相邻入口**（同展位或用 ``adjacency``
显式相连）可达的签名求交：

* 相交签名对应的去向集合只有一个 -> 指向唯一；
* 存在两个不同去向共用同一签名 -> 混淆，输出这两次具体触读（谁、在哪个朝向、
  摸到什么、各自实际去哪）。

两块牌形状完全相同但实际去向相同，不算混淆（题面：只有对应实际去向不同才算）。

规划（方案 A 方向标记）
=======================
:func:`plan_exhibition` 在展方输入（格数、改刻许可、可用底座清单）内尝试让站点
可开放：优先正向锁死底座，不足时普通座依赖方向标记；无法在许可内消除混淆或
底座不够时，该展位判为**无法开放站点**并给出原因与改刻建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .domain import (
    LEFT,
    RIGHT,
    UPRIGHT,
    BaseSpec,
    FabricatedSign,
    FeltReading,
    MarkerPolicy,
    NoMarkerPolicy,
    flip_index,
    signature_key,
)

ORIENTATION_NAME = {UPRIGHT: "正向", 1: "转半周"}
SIDE_NAME = {LEFT: "左侧", RIGHT: "右侧"}


# --------------------------------------------------------------------------- #
# 展位与入口
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Entrance:
    entrance_id: str
    sign: FabricatedSign
    destination: str  # 实际去向（出口/楼梯/…）
    approach: str = LEFT  # 访客从哪一侧走近
    note: str = ""


@dataclass
class Booth:
    booth_id: str
    entrances: list[Entrance]
    # 额外相邻关系：同展位入口默认两两相邻；这里可补跨展位相邻入口 id 对
    adjacency: tuple[tuple[str, str], ...] = ()


def approach_transform(index: int, n: int, approach: str) -> int:
    """牌面格号 -> 访客触觉参照系格号（取决于走近侧）。"""
    return index if approach == LEFT else flip_index(index, n)


def felt_signatures(ent: Entrance) -> dict[tuple, FeltReading]:
    """一条入口在其走近侧下，每个可达触读签名对应的图样。"""
    n = ent.sign.n
    out: dict[tuple, FeltReading] = {}
    for reading in ent.sign.readings:
        felt_dots = frozenset(approach_transform(i, n, ent.approach) for i in reading.dots)
        felt_edges = frozenset(
            (RIGHT if e == LEFT else LEFT) if ent.approach == RIGHT else e for e in reading.edges
        )
        felt_marker = (
            approach_transform(reading.marker, n, ent.approach)
            if reading.marker is not None
            else None
        )
        felt = FeltReading(reading.orientation, felt_dots, felt_edges, felt_marker)
        out.setdefault(signature_key(felt), felt)
    return out


# --------------------------------------------------------------------------- #
# 混淆：两次具体触读
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TouchEvent:
    """一次具体触读。"""

    entrance_id: str
    destination: str
    orientation: int
    felt: FeltReading
    n: int

    def render(self) -> str:
        return self.felt.render(self.n)

    def describe(self) -> str:
        return (
            f"入口 {self.entrance_id}（实际去向：{self.destination}）在{ORIENTATION_NAME[self.orientation]}"
            f"下摸到 {self.render()}"
        )


@dataclass(frozen=True)
class Confusion:
    """会把人引错的一对触读：同一签名、不同实际去向。"""

    key: tuple
    first: TouchEvent
    second: TouchEvent

    def describe(self) -> str:
        return (
            f"同一触读 {self.first.render()} 对应两个去向：\n"
            f"  ① {self.first.describe()}\n"
            f"  ② {self.second.describe()}"
        )


def _adjacency_pairs(booths: list[Booth]) -> set[frozenset[str]]:
    pairs: set[frozenset[str]] = set()
    for booth in booths:
        ids = [e.entrance_id for e in booth.entrances]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pairs.add(frozenset((ids[i], ids[j])))
        for a, b in booth.adjacency:
            pairs.add(frozenset((a, b)))
    return pairs


def evaluate(booths: list[Booth]) -> list[Confusion]:
    """全展触读唯一性检查，返回所有“不同去向共用签名”的两次触读。"""
    by_id: dict[str, Entrance] = {}
    for booth in booths:
        for ent in booth.entrances:
            if ent.entrance_id in by_id:
                raise ValueError(f"入口 id 重复: {ent.entrance_id}")
            by_id[ent.entrance_id] = ent

    sig_of: dict[str, dict[tuple, FeltReading]] = {eid: felt_signatures(ent) for eid, ent in by_id.items()}
    pairs = _adjacency_pairs(booths)
    confusions: list[Confusion] = []
    seen: set[tuple] = set()
    for pair in sorted(pairs, key=lambda p: sorted(p)):
        a_id, b_id = sorted(pair)
        a, b = by_id[a_id], by_id[b_id]
        if a.destination == b.destination:
            continue  # 同去向不混淆
        shared = sig_of[a_id].keys() & sig_of[b_id].keys()
        for key in sorted(shared, key=lambda k: repr(k)):
            dedup = (key, a_id, b_id)
            if dedup in seen:
                continue
            seen.add(dedup)
            confusions.append(
                Confusion(
                    key=key,
                    first=TouchEvent(a_id, a.destination, sig_of[a_id][key].orientation,
                                     sig_of[a_id][key], a.sign.n),
                    second=TouchEvent(b_id, b.destination, sig_of[b_id][key].orientation,
                                      sig_of[b_id][key], b.sign.n),
                )
            )
    return confusions


# --------------------------------------------------------------------------- #
# 布展规划：底座分配 + 方向标记 + 改刻许可 + 无法开放站点
# --------------------------------------------------------------------------- #
@dataclass
class BaseInventory:
    """展方可用底座清单：底座型号 -> (规格, 可用数量)。

    底座任务行定义型号（类型 + 锁边），库存数量是展方输入；同一型号可在多个
    展位安装，规划时按数量扣减。
    """

    bases: dict[str, BaseSpec]
    counts: dict[str, int]

    @classmethod
    def from_specs(cls, specs: list[BaseSpec], counts: dict[str, int] | None = None) -> "BaseInventory":
        base_map = {s.base_id: s for s in specs}
        eff = {bid: counts.get(bid, 1) if counts else 1 for bid in base_map}
        return cls(base_map, eff)


@dataclass
class ReengraveProposal:
    """让两块牌可分所需的最小改刻：把某块牌的某空格刻上/去掉凸点。"""

    plate_id: str
    add_cells: tuple[int, ...]
    remove_cells: tuple[int, ...]
    reason: str

    def describe(self) -> str:
        adds = f" 加刻格 {list(self.add_cells)}" if self.add_cells else ""
        rems = f" 去刻格 {list(self.remove_cells)}" if self.remove_cells else ""
        return f"牌 {self.plate_id}{adds}{rems}（{self.reason}）"


@dataclass
class EntrancePlan:
    entrance_id: str
    assigned_base: str | None
    openable: bool
    reasons: tuple[str, ...] = ()


@dataclass
class BoothPlan:
    booth_id: str
    openable: bool
    entrances: list[EntrancePlan]
    confusions: list[Confusion]
    proposals: list[ReengraveProposal]

    @property
    def blocked_reasons(self) -> list[str]:
        reasons: list[str] = []
        for ep in self.entrances:
            if not ep.openable:
                reasons.extend(ep.reasons)
        return reasons


@dataclass
class ExhibitionPlan:
    marker_policy: MarkerPolicy
    reengrave_allowed: bool
    booth_plans: list[BoothPlan]
    base_usage: dict[str, list[str]]  # base 型号 -> 安装到的入口 id 列表
    leftover_bases: tuple[str, ...]

    @property
    def open_booths(self) -> list[str]:
        return [b.booth_id for b in self.booth_plans if b.openable]

    @property
    def closed_booths(self) -> list[str]:
        return [b.booth_id for b in self.booth_plans if not b.openable]


def _rebuild_entrance(ent: Entrance, base: BaseSpec, policy: MarkerPolicy) -> Entrance:
    """用新底座与标记策略重新制同一块牌（刻样不变）。"""
    from .domain import read_sign

    sign = read_sign(ent.sign.plate, base, policy, source=ent.sign.source)
    return Entrance(ent.entrance_id, sign, ent.destination, ent.approach, ent.note)


def propose_reengraving(a: Entrance, b: Entrance) -> ReengraveProposal:
    """给出区分两块牌的最小改刻建议（优先在 a 的空格上加刻一点）。"""
    plate = a.sign.plate
    other_felt = b.sign.plate.felt_dots
    free = sorted(plate.free_cells)
    if free:
        cell = free[0]
        return ReengraveProposal(
            plate_id=plate.plate_id,
            add_cells=(cell,),
            remove_cells=(),
            reason=f"在空格 {cell} 加刻一点，使其与 {b.sign.plate.plate_id} 不再撞形",
        )
    # 无空格：建议把 a 独有的、对方没有的凸点去刻（退化为遮挡语义时另由遮挡处理）
    removable = sorted(plate.felt_dots - other_felt)
    if removable:
        cell = removable[0]
        return ReengraveProposal(
            plate_id=plate.plate_id,
            add_cells=(),
            remove_cells=(cell,),
            reason=f"去掉凸点 {cell}，使其与 {b.sign.plate.plate_id} 不再撞形",
        )
    return ReengraveProposal(plate.plate_id, (), (), reason="两块牌刻面完全相同且无空格，需整体重刻")


def plan_exhibition(
    booths: list[Booth],
    inventory: BaseInventory,
    policy: MarkerPolicy | None = None,
    reengrave_allowed: bool = False,
) -> ExhibitionPlan:
    """在展方输入内规划布展；不可消除混淆或底座不足的展位判为无法开放。

    规则
    ----
    1. 每个入口已经挂在某个**指定底座型号**上（``ent.sign.base``），规划先按
       库存数量逐一扣减；库存不足 -> 该入口无法开放。
    2. 不擅自把普通座换成锁死座（那会抹掉“允许翻转”这一输入事实）；普通座
       导致的翻转混淆保留并上报，由改刻许可/方向标记两条展方输入决定结局。
    3. 每个展位独立用同一库存池扣减；混淆给出最小改刻建议，改刻许可关闭时
       展位判为无法开放。
    """
    policy = policy or NoMarkerPolicy()
    remaining: dict[str, int] = dict(inventory.counts)
    base_usage: dict[str, list[str]] = {bid: [] for bid in inventory.bases}

    booth_plans: list[BoothPlan] = []
    for booth in booths:
        rebuilt: list[Entrance] = []
        entrance_plans: list[EntrancePlan] = []

        # 1) 按入口指定型号扣库存（库存不足即无法开放，但仍参与混淆复验）
        for ent in booth.entrances:
            wanted_id = ent.sign.base.base_id
            if wanted_id not in inventory.bases:
                entrance_plans.append(
                    EntrancePlan(ent.entrance_id, None, False, (f"底座型号 {wanted_id} 不在可用清单",))
                )
                rebuilt.append(ent)
                continue
            if remaining.get(wanted_id, 0) <= 0:
                entrance_plans.append(
                    EntrancePlan(ent.entrance_id, None, False, (f"底座 {wanted_id} 库存已耗尽",))
                )
                rebuilt.append(ent)
                continue
            remaining[wanted_id] -= 1
            base_usage[wanted_id].append(ent.entrance_id)
            rebuilt.append(_rebuild_entrance(ent, inventory.bases[wanted_id], policy))
            entrance_plans.append(EntrancePlan(ent.entrance_id, wanted_id, True))

        # 2) 在当前（尊重指定底座的）安装方案下复验触读唯一性
        confusions = evaluate([Booth(booth.booth_id, rebuilt, booth.adjacency)])
        proposals: list[ReengraveProposal] = []
        for conf in confusions:
            a = next(e for e in rebuilt if e.entrance_id == conf.first.entrance_id)
            b = next(e for e in rebuilt if e.entrance_id == conf.second.entrance_id)
            proposals.append(propose_reengraving(a, b))
            if not reengrave_allowed:
                reason = "改刻许可关闭：存在不同去向共用同一触读签名"
            else:
                reason = "已允许改刻：按建议重制后须由下一位布展员复验"
            for eid in (conf.first.entrance_id, conf.second.entrance_id):
                ep = next(x for x in entrance_plans if x.entrance_id == eid)
                ep.reasons = ep.reasons + (reason,)
                ep.openable = False

        openable = not confusions and all(ep.openable for ep in entrance_plans)
        booth_plans.append(
            BoothPlan(booth.booth_id, openable, entrance_plans, confusions, proposals)
        )

    return ExhibitionPlan(
        marker_policy=policy,
        reengrave_allowed=reengrave_allowed,
        booth_plans=booth_plans,
        base_usage=base_usage,
        leftover_bases=tuple(sorted(bid for bid, n in remaining.items() if n > 0)),
    )
