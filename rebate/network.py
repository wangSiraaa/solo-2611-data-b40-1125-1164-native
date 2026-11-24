"""社区废纸收集网络与住户重量约束求解 (collection network solver).

本模块是纯数据 + 纯求解，不落盘、不认识柜台或预算。柜台事实仓
(``counter.py``) 把它的 SQLite 记录重建成 :class:`CollectionNetwork`，
再按 *某个经办方当时有权看见的证据* 取视图求解。

事实模型
--------
社区按住户 *一整个核算期* 的废纸净重返券。收集车可能跨楼串收，途中还有
封箱转车。一次过磅只产生 *车净重*（题面：一车只过一次磅），住户层面的
重量只能靠把下列可回查事实接起来推断：

* **袋** ``Bag``：住户交出的一袋纸，有毛重 ``gross`` 与 *允许袋重*
  （皮重整数闭区间 ``[tare_lo, tare_hi]``）。纸重
  ``paper = gross - tare``，故袋纸重落在
  ``[max(0, gross-tare_hi), gross-tare_lo]``。
* **箱** ``Box``：袋装进箱；箱在某条车次上首次称重。被封箱转交的源箱
  仍归 *原车次* 只计一次（``include_in_trip`` 由转交登记自动置否）。
* **车次净重版本** ``TripVersion``：同一车次的可回查净重区间
  ``[lo, hi]``；精确净重写成 ``lo == hi``。迟到的称量纠正 *新增版本*，
  旧版本永不覆盖（与过磅事实仓的 REVIEW 链同构）。
* **封箱转交** :meth:`CollectionNetwork.add_transfer`：把源箱整体封进
  新车次上的目标箱，约束为两侧纸重严格相等，且源箱在原车净重中只出现
  一次——同一封箱转车绝不归两次。
* **自愿出示的局部称量** ``PartialWeigh``：实名凭条，约束某节点
  （袋/箱/车次/住户合计）的纸重为精确值或区间；带可见域，决定哪个
  经办方有权把它纳入求解。

求解
----
在 *袋纸重* 的整数笛卡尔域上回溯枚举（容量：最多 3 住户、小整数），
对箱/车次/住户合计做线性聚合，逐节点核对局部称量、车次净重版本与
住户减量线。返回 *全部互不相同的可行世界*——无法区分的两种情况都留在
结果里；某住户在所有可行世界里取同一值时，其重量才被 *证实*
（``proven``），否则只能挂起。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

# 证据可见域
ORIGIN_CENTER = "CENTER"          # 中心统一受理，全体经办方可见
ORIGIN_STATION = "STATION"        # 分站实名凭条，仅该站与中心可见

# 节点种类
NODE_BAG = "bag"
NODE_BOX = "box"
NODE_TRIP = "trip"
NODE_HOUSEHOLD = "household"


@dataclass(frozen=True)
class Bag:
    """住户交出的一袋：毛重 + 允许袋重（皮重区间）+ 归属箱与住户。

    ``allowed_paper`` 给定时，袋纸重只能取这些 *离散* 值（实名称量常见为
    离散档位，如题面的每袋 4 或 6）；否则由毛重与允许袋重推出连续整数
    闭区间 ``[max(0, gross-tare_hi), gross-tare_lo]``。
    """

    bag_id: str
    household_id: str
    box_id: str
    gross: int
    tare_lo: int
    tare_hi: int
    allowed_paper: Optional[tuple[int, ...]] = None

    def paper_domain(self) -> tuple[int, int]:
        """纸重可行整数闭区间（离散档位时取最小/最大包络）。"""
        if self.allowed_paper:
            return min(self.allowed_paper), max(self.allowed_paper)
        return max(0, self.gross - self.tare_hi), self.gross - self.tare_lo

    def paper_values(self) -> tuple[int, ...]:
        """袋纸重可取的全部离散值。"""
        if self.allowed_paper:
            return tuple(sorted(set(self.allowed_paper)))
        lo, hi = self.paper_domain()
        return tuple(range(lo, hi + 1))

    def to_dict(self) -> dict:
        return {
            "bag_id": self.bag_id,
            "household_id": self.household_id,
            "box_id": self.box_id,
            "gross": self.gross,
            "tare": [self.tare_lo, self.tare_hi],
            "paper_domain": list(self.paper_domain()),
            "allowed_paper": list(self.allowed_paper) if self.allowed_paper else None,
        }


@dataclass(frozen=True)
class Box:
    """收集箱：在某条车次上首次归集；``include_in_trip`` 决定是否计入该车净重。"""

    box_id: str
    trip_id: str
    include_in_trip: bool = True

    def to_dict(self) -> dict:
        return {
            "box_id": self.box_id,
            "trip_id": self.trip_id,
            "include_in_trip": self.include_in_trip,
        }


@dataclass(frozen=True)
class TripVersion:
    """车次净重的一个可回查版本（区间；精确值 lo==hi）。"""

    trip_id: str
    lo: int
    hi: int
    version: int = 1
    predecessor_version: Optional[int] = None
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "trip_id": self.trip_id,
            "version": self.version,
            "net": [self.lo, self.hi],
            "predecessor_version": self.predecessor_version,
            "source": self.source,
        }


@dataclass(frozen=True)
class PartialWeigh:
    """自愿出示的局部称量（实名称量凭条）。

    ``node_kind``/``node_id`` 指向被称对象（袋/箱/车次/住户合计）；
    ``[lo, hi]`` 为出示纸重区间（精确值 lo==hi）。``origin`` 与
    ``station_id`` 决定可见域：中心凭条全局可见；分站凭条仅该站与中心
    可见，其他分站看不到。
    """

    weigh_id: str
    node_kind: str
    node_id: str
    lo: int
    hi: int
    origin: str = ORIGIN_CENTER
    station_id: Optional[str] = None
    household_id: Optional[str] = None

    def visible_to(self, station_id: Optional[str]) -> bool:
        # station_id=None 表示中心视角：获准看凑成同车总数的全部局部称量。
        if station_id is None:
            return True
        if self.origin == ORIGIN_CENTER:
            return True
        return self.station_id == station_id

    def to_dict(self) -> dict:
        return {
            "weigh_id": self.weigh_id,
            "node_kind": self.node_kind,
            "node_id": self.node_id,
            "weight": [self.lo, self.hi],
            "origin": self.origin,
            "station_id": self.station_id,
            "household_id": self.household_id,
        }


@dataclass(frozen=True)
class FeasibleWorld:
    """一个能同时解释全部车次与可见证据的整数重量世界。"""

    bag_paper: Mapping[str, int]
    box_paper: Mapping[str, int]
    trip_net: Mapping[str, int]
    household_paper: Mapping[str, int]

    def to_dict(self) -> dict:
        return {
            "bag_paper": dict(self.bag_paper),
            "box_paper": dict(self.box_paper),
            "trip_net": dict(self.trip_net),
            "household_paper": dict(self.household_paper),
        }


@dataclass(frozen=True)
class SolveResult:
    feasible: bool
    worlds: tuple[FeasibleWorld, ...]
    proven: Mapping[str, Optional[tuple[int, int]]]
    household_domain: Mapping[str, tuple[int, int]]
    reason: str

    def to_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "worlds": [w.to_dict() for w in self.worlds],
            "proven": {h: None if v is None else list(v) for h, v in self.proven.items()},
            "household_domain": {h: list(v) for h, v in self.household_domain.items()},
            "reason": self.reason,
        }


REASON_FEASIBLE = "FEASIBLE"
REASON_INFEASIBLE = "NETWORK_INFEASIBLE"


def _within(value: int, lo: int, hi: int) -> bool:
    return lo <= value <= hi


def solve(
    bags: Mapping[str, Bag],
    boxes: Mapping[str, Box],
    trips: Mapping[str, TripVersion],
    partials: tuple[PartialWeigh, ...],
    transfers: Mapping[str, str],
    households: tuple[str, ...],
) -> SolveResult:
    """在给定证据集合上枚举全部可行世界并判定各户重量是否被证实。

    纯函数：调用方负责先按经办方权限过滤 ``partials``（见
    :func:`view_for_station`）。箱/车次/住户合计都是袋纸重的线性式，
    封箱转交是两侧相等约束。
    """
    bag_ids = tuple(bags)
    domains = {bid: bags[bid].paper_domain() for bid in bag_ids}
    discrete_values = {bid: bags[bid].paper_values() for bid in bag_ids}

    # 线性聚合索引
    bags_of_household: dict[str, list[str]] = {h: [] for h in households}
    bags_of_box: dict[str, list[str]] = {}
    for bid, b in bags.items():
        bags_of_household.setdefault(b.household_id, []).append(bid)
        bags_of_box.setdefault(b.box_id, []).append(bid)
    boxes_of_trip: dict[str, list[str]] = {}
    for bxid, bx in boxes.items():
        if bx.include_in_trip:
            boxes_of_trip.setdefault(bx.trip_id, []).append(bxid)

    def node_value(
        node_kind: str,
        node_id: str,
        assignment: Mapping[str, int],
        box_paper: Mapping[str, int],
        trip_net: Mapping[str, int],
        household_paper: Mapping[str, int],
    ) -> Optional[int]:
        if node_kind == NODE_BAG:
            return assignment.get(node_id)
        if node_kind == NODE_BOX:
            return box_paper.get(node_id)
        if node_kind == NODE_TRIP:
            return trip_net.get(node_id)
        if node_kind == NODE_HOUSEHOLD:
            return household_paper.get(node_id)
        return None

    def check_partials(
        assignment: Mapping[str, int],
        box_paper: Mapping[str, int],
        trip_net: Mapping[str, int],
        household_paper: Mapping[str, int],
    ) -> bool:
        for pw in partials:
            v = node_value(
                pw.node_kind, pw.node_id, assignment, box_paper, trip_net, household_paper
            )
            if v is None or not _within(v, pw.lo, pw.hi):
                return False
        return True

    worlds: list[FeasibleWorld] = []
    seen_household_tuples: set[tuple[tuple[str, int], ...]] = set()

    def aggregate(assignment: Mapping[str, int]) -> tuple[dict, dict, dict]:
        box_paper: dict[str, int] = {}
        for bxid in boxes:
            total = sum(assignment[bid] for bid in bags_of_box.get(bxid, ()))
            # 封箱转交：目标箱承接源箱全部纸重（两侧相等）。
            if bxid in transfers.values():
                src = next(s for s, d in transfers.items() if d == bxid)
                total += box_paper[src]
            box_paper[bxid] = total
        trip_net: dict[str, int] = {}
        for tid in trips:
            trip_net[tid] = sum(box_paper[bx] for bx in boxes_of_trip.get(tid, ()))
        household_paper: dict[str, int] = {}
        for h in households:
            household_paper[h] = sum(
                assignment[bid] for bid in bags_of_household.get(h, ())
            )
        return box_paper, trip_net, household_paper

    def backtrack(i: int, assignment: dict[str, int]) -> None:
        if i == len(bag_ids):
            box_paper, trip_net, household_paper = aggregate(assignment)
            # 车次净重版本约束（只约束 include_in_trip 的箱之和）。
            for tid, ver in trips.items():
                if not _within(trip_net[tid], ver.lo, ver.hi):
                    return
            if not check_partials(assignment, box_paper, trip_net, household_paper):
                return
            key = tuple(sorted(household_paper.items()))
            if key in seen_household_tuples:
                return
            seen_household_tuples.add(key)
            worlds.append(
                FeasibleWorld(
                    bag_paper=dict(assignment),
                    box_paper=dict(box_paper),
                    trip_net=dict(trip_net),
                    household_paper=dict(household_paper),
                )
            )
            return
        bid = bag_ids[i]
        lo, hi = domains[bid]
        for value in discrete_values[bid]:
            if not _within(value, lo, hi):
                continue
            assignment[bid] = value
            backtrack(i + 1, assignment)
        assignment.pop(bid, None)

    backtrack(0, {})

    household_domain: dict[str, tuple[int, int]] = {}
    proven: dict[str, Optional[tuple[int, int]]] = {}
    for h in households:
        vals = {w.household_paper.get(h, 0) for w in worlds}
        if not vals:
            proven[h] = None
            household_domain[h] = (0, 0)
        else:
            lo, hi = min(vals), max(vals)
            household_domain[h] = (lo, hi)
            proven[h] = (lo, lo) if lo == hi else None

    if not worlds:
        return SolveResult(False, (), proven, household_domain, REASON_INFEASIBLE)
    return SolveResult(True, tuple(worlds), proven, household_domain, REASON_FEASIBLE)


def view_for_station(
    bags: Mapping[str, Bag],
    boxes: Mapping[str, Box],
    trips: Mapping[str, TripVersion],
    partials: tuple[PartialWeigh, ...],
    transfers: Mapping[str, str],
    households: tuple[str, ...],
    station_id: Optional[str],
) -> SolveResult:
    """按某分站 *有权看见* 的局部称量取视图后求解。

    车次净重、箱号、转交、允许袋重都是公开事实；分站私重凭条只有该站
    （与中心）可见。``station_id=None`` 表示中心视角（可见全部）。
    """
    visible = tuple(pw for pw in partials if pw.visible_to(station_id))
    return solve(bags, boxes, trips, visible, transfers, households)


class CollectionNetwork:
    """可变收集网络构造器；校验引用一致后交给求解器。"""

    def __init__(self) -> None:
        self.bags: dict[str, Bag] = {}
        self.boxes: dict[str, Box] = {}
        self.trips: dict[str, TripVersion] = {}
        self.partials: list[PartialWeigh] = []
        self.transfers: dict[str, str] = {}
        self.households: list[str] = []

    # ------------------------------------------------------------ builders
    def add_household(self, household_id: str) -> None:
        if household_id not in self.households:
            self.households.append(household_id)

    def add_bag(
        self,
        bag_id: str,
        household_id: str,
        box_id: str,
        gross: int,
        tare_lo: int = 0,
        tare_hi: int = 0,
        allowed_paper: Optional[tuple[int, ...]] = None,
    ) -> Bag:
        if bag_id in self.bags:
            raise ValueError(f"bag {bag_id} already exists")
        if not (0 <= tare_lo <= tare_hi):
            raise ValueError(f"invalid tare [{tare_lo},{tare_hi}]")
        if gross < 0 or gross < tare_lo:
            raise ValueError(f"bag {bag_id} gross {gross} below tare {tare_lo}")
        if allowed_paper is not None:
            vals = tuple(sorted({int(v) for v in allowed_paper}))
            if not vals or any(v < 0 for v in vals):
                raise ValueError("allowed_paper must contain non-negative integers")
        else:
            vals = None
        bag = Bag(bag_id, household_id, box_id, int(gross), int(tare_lo), int(tare_hi), vals)
        self.bags[bag_id] = bag
        self.add_household(household_id)
        return bag

    def add_box(self, box_id: str, trip_id: str, include_in_trip: bool = True) -> Box:
        if box_id in self.boxes:
            raise ValueError(f"box {box_id} already exists")
        box = Box(box_id, trip_id, bool(include_in_trip))
        self.boxes[box_id] = box
        return box

    def add_trip_version(
        self,
        trip_id: str,
        lo: int,
        hi: int,
        *,
        version: int = 1,
        predecessor_version: Optional[int] = None,
        source: str = "",
    ) -> TripVersion:
        if lo < 0 or hi < lo:
            raise ValueError(f"invalid net interval [{lo},{hi}]")
        ver = TripVersion(trip_id, int(lo), int(hi), int(version), predecessor_version, source)
        self.trips[trip_id] = ver  # 同车次新版本取代旧活动版本，旧版本由柜台留档
        return ver

    def add_transfer(self, source_box_id: str, dest_box_id: str) -> None:
        """封箱转车：源箱整体封进目标箱；源箱在原车净重中只计一次。"""
        if source_box_id not in self.boxes:
            raise ValueError(f"unknown source box {source_box_id}")
        if dest_box_id not in self.boxes:
            raise ValueError(f"unknown dest box {dest_box_id}")
        if source_box_id in self.transfers:
            raise ValueError(f"box {source_box_id} already transferred once")
        src, dst = self.boxes[source_box_id], self.boxes[dest_box_id]
        if src.trip_id == dst.trip_id:
            raise ValueError("a sealed transfer must move across different trips")
        # 同一封箱转车只归一次：源箱移出后不再计入原车净重。
        self.boxes[source_box_id] = Box(src.box_id, src.trip_id, False)
        self.transfers[source_box_id] = dest_box_id

    def add_partial(self, partial: PartialWeigh) -> None:
        if any(pw.weigh_id == partial.weigh_id for pw in self.partials):
            raise ValueError(f"partial weigh {partial.weigh_id} already exists")
        if partial.lo < 0 or partial.hi < partial.lo:
            raise ValueError(f"invalid partial interval [{partial.lo},{partial.hi}]")
        self.partials.append(partial)
        if partial.household_id:
            self.add_household(partial.household_id)

    # ------------------------------------------------------------ solving
    def _validate(self) -> None:
        for b in self.bags.values():
            if b.box_id not in self.boxes:
                raise ValueError(f"bag {b.bag_id} references unknown box {b.box_id}")
        for bx in self.boxes.values():
            if bx.trip_id not in self.trips:
                raise ValueError(f"box {bx.box_id} references unknown trip {bx.trip_id}")
        for src, dst in self.transfers.items():
            if src not in self.boxes or dst not in self.boxes:
                raise ValueError("transfer references unknown box")

    def solve(self, station_id: Optional[str] = None) -> SolveResult:
        self._validate()
        return view_for_station(
            self.bags,
            self.boxes,
            self.trips,
            tuple(self.partials),
            self.transfers,
            tuple(self.households),
            station_id,
        )

    def visible_partials(self, station_id: Optional[str]) -> list[PartialWeigh]:
        return [pw for pw in self.partials if pw.visible_to(station_id)]
