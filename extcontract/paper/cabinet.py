"""发行柜 / 补量柜 — the local, stateful distribution cabinet.

The cabinet is the 发行员's (distributor's) single local entry point.  It can
*repeatedly* receive new business — contracts and station files — and each
new order may name a different block, permitted household or candidate line.

Layering, deliberately strict:

  * Station delivery files go through the **original**
    :class:`~extcontract.IngestionLibrary` unchanged (revision-aware, atomic,
    idempotent).  Settlement only ever reads ``publish()`` — the valid rows
    and their editions — so a rejected file changes nothing and a replayed
    delivery receipt is never credited twice.
  * :mod:`extcontract.paper.settle` supplies the worst-case math: the
    guaranteed lower bound over all compatible histories and the per-line
    added lower bound.
  * This module owns the business lifecycle: contracts, 版位 (slots),
    截稿 (deadline), 认款 (crediting), and 补投单 (makeup orders).

Two operation modes exist but an issue runs **only one of them**
(``targeted`` per-household delivery, or ``whole_line`` unsplittable lines);
a cabinet opened for one mode refuses orders of the other.

Makeup order rules
------------------
* *targeted*: an order may only name a household that the current evidence
  proves never received (a possible-misser is not enough); it occupies one
  slot and later produces a named delivery receipt.
* *whole_line*: a line is bought whole; per-household prior receipt need not
  be published, but signing is allowed only when its added lower bound still
  closes the residual gap under the worst possible overlap.

Basis retention and withdrawal
------------------------------
Every order freezes the basis it was signed against (the lower bound, the
per-line added bound and a witness arrangement).  Sealing locks in the
real slot usage; a sealed (or delivered) order keeps its historical basis
forever and cannot be withdrawn.  An order still unsealed may be withdrawn
while evidence changes — after new evidence arrives, a re-settlement reports
whether its basis still holds.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..contract import Contract
from ..errors import ContractError
from ..ingest import IngestionLibrary
from .settle import (
    Observation,
    lower_bound_with_extra,
    parse_observations,
    settle,
)

TARGETED = "targeted"
WHOLE_LINE = "whole_line"
MODES = frozenset({TARGETED, WHOLE_LINE})

# Order lifecycle states.
SIGNED = "signed"        # basis accepted, slots merely reserved
SEALED = "sealed"        # 封版: real slots locked, basis frozen
DELIVERED = "delivered"  # 送出: named receipt on file (targeted) / line run


@dataclass(frozen=True)
class Route:
    """An unsplittable candidate line over block households."""

    route_id: str
    block: str
    households: frozenset

    def __post_init__(self) -> None:
        if not self.route_id or not isinstance(self.route_id, str):
            raise ContractError("route_id must be a non-empty string")
        if not self.households:
            raise ContractError(f"route {self.route_id!r} has no households")

    def to_json(self) -> dict:
        return {
            "route_id": self.route_id,
            "block": self.block,
            "households": sorted(self.households),
            "slots": len(self.households),
        }


@dataclass
class AdContract:
    """A guaranteed-quantity advertising contract (保量合同)."""

    contract_no: str
    block: str
    guarantee: int
    permitted: frozenset
    makeup_edition: str
    deadline: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.contract_no, str) or not self.contract_no:
            raise ContractError("contract_no must be a non-empty string")
        if type(self.guarantee) is not int or self.guarantee < 0:
            raise ContractError("guarantee must be a non-negative integer")
        if not isinstance(self.permitted, frozenset):
            object.__setattr__(self, "permitted", frozenset(self.permitted))
        if not all(isinstance(h, str) and h for h in self.permitted):
            raise ContractError("permitted households must be non-empty strings")

    def to_json(self) -> dict:
        return {
            "contract_no": self.contract_no,
            "block": self.block,
            "guarantee": self.guarantee,
            "permitted": sorted(self.permitted),
            "makeup_edition": self.makeup_edition,
            "deadline": self.deadline,
        }


@dataclass
class MakeupOrder:
    """One 补投单 signed for a contract."""

    order_id: str
    contract_no: str
    mode: str
    block: str
    slots: int
    state: str
    basis: dict
    target_household: str | None = None
    route_id: str | None = None
    route_households: frozenset | None = None
    receipt: dict | None = None
    sealed_edition: str | None = None

    def to_json(self) -> dict:
        return {
            "order_id": self.order_id,
            "contract_no": self.contract_no,
            "mode": self.mode,
            "block": self.block,
            "slots": self.slots,
            "state": self.state,
            "target_household": self.target_household,
            "route_id": self.route_id,
            "route_households": sorted(self.route_households)
            if self.route_households is not None
            else None,
            "sealed_edition": self.sealed_edition,
            "basis": _deep_copy(self.basis),
            "receipt": _deep_copy(self.receipt),
        }


@dataclass
class CabinetResult:
    """All cabinet operations return a result object, never raise on rejection."""

    status: str  # "ACCEPTED" | "REJECTED"
    message: str = ""
    data: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    ingest_result: dict | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "ACCEPTED"

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "data": _deep_copy(self.data),
            "errors": list(self.errors),
            "ingest": self.ingest_result,
        }


def _reject(message: str, errors: list[str] | None = None) -> CabinetResult:
    return CabinetResult(status="REJECTED", message=message, errors=list(errors or [message]))


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_copy(v) for v in value]
    if isinstance(value, frozenset):
        return sorted(value)
    return value


class PaperCabinet:
    """Stateful local cabinet for one paper issue and one operation mode."""

    def __init__(
        self,
        reader: Contract,
        writer: Contract,
        *,
        mode: str,
        edition_slots: int,
        universes: dict[str, list[str]] | None = None,
        routes: list[Route] | None = None,
        clock: Callable[[], str | None] | None = None,
    ):
        if mode not in MODES:
            raise ContractError(f"mode must be one of {sorted(MODES)}, got {mode!r}")
        if type(edition_slots) is not int or edition_slots <= 0:
            raise ContractError("edition_slots must be a positive integer")
        self.reader = reader
        self.writer = writer
        self.mode = mode
        self.edition_slots = edition_slots
        self.library = IngestionLibrary(reader)
        self._lock = threading.RLock()
        # block -> roster (rosters may grow when a contract names a block)
        self.universes: dict[str, frozenset] = {
            block: frozenset(households) for block, households in (universes or {}).items()
        }
        self.routes: dict[str, Route] = {route.route_id: route for route in (routes or [])}
        self.contracts: dict[str, AdContract] = {}
        self.orders: dict[str, MakeupOrder] = {}
        self._order_seq = 0
        self._clock = clock or (lambda: None)
        # Sealed real-slot usage per makeup edition.
        self._sealed_slots: dict[str, int] = {}
        # Last station-file ingest receipts, keyed by file_id.
        self.last_receipts: dict[str, dict] = {}
        # Accepted station documents, retained verbatim so a reload can replay
        # them through the real ingest path (preserving revision/replay rules).
        self.station_documents: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Local intake: contracts and station files
    # ------------------------------------------------------------------

    def register_contract(self, document: dict) -> CabinetResult:
        """Register (or re-register with a new guarantee) one contract."""
        with self._lock:
            try:
                required = {"contract_no", "block", "guarantee", "makeup_edition"}
                missing = required - set(document)
                if missing:
                    return _reject("contract document incomplete",
                                   [f"missing keys {sorted(missing)}"])
                contract = AdContract(
                    contract_no=document["contract_no"],
                    block=document["block"],
                    guarantee=int(document["guarantee"]),
                    permitted=frozenset(document.get("permitted", [])),
                    makeup_edition=document["makeup_edition"],
                    deadline=document.get("deadline"),
                )
            except (ContractError, TypeError, ValueError) as exc:
                return _reject("invalid contract document", [str(exc)])

            existing = self.contracts.get(contract.contract_no)
            if existing is not None and existing.block != contract.block:
                return _reject(
                    "contract block is immutable",
                    [f"{contract.contract_no!r} already on block {existing.block!r}"],
                )
            roster = self.universes.setdefault(
                contract.block,
                frozenset(contract.permitted),
            )
            unknown_permitted = contract.permitted - roster
            if unknown_permitted:
                # The roster may legitimately grow to include permitted names.
                self.universes[contract.block] = roster | contract.permitted
            self.contracts[contract.contract_no] = contract
            return CabinetResult(
                status="ACCEPTED",
                message="contract registered",
                data={"contract": contract.to_json()},
            )

    def receive_station_file(self, document: dict) -> CabinetResult:
        """Feed one station delivery file through the original ingest path."""
        with self._lock:
            result = self.library.ingest(self.writer, document)
            payload = result.to_json()
            if result.accepted:
                self.last_receipts[result.file_id] = result.receipt
                self.station_documents[result.file_id] = _deep_copy(document)
            return CabinetResult(
                status=result.status,
                message="station file accepted" if result.accepted else "station file rejected",
                ingest_result=payload,
                data={"receipt": result.receipt} if result.accepted else {},
                errors=result.errors,
            )

    def last_receipt(self, file_id: str) -> dict | None:
        with self._lock:
            receipt = self.last_receipts.get(file_id)
            return _deep_copy(receipt)

    # ------------------------------------------------------------------
    # Settlement over the published valid rows
    # ------------------------------------------------------------------

    def _observations(self) -> list[Observation]:
        published = self.library.publish()
        return parse_observations(published["rows"])

    def _block_lines(self, block: str) -> dict[str, list[str]]:
        return {
            route.route_id: sorted(route.households)
            for route in self.routes.values()
            if route.block == block
        }

    def settle(self) -> CabinetResult:
        """Re-settle every known block from the current publish snapshot."""
        with self._lock:
            try:
                observations = self._observations()
                verdicts = settle(self.universes, observations, self._all_lines())
            except ContractError as exc:
                return _reject("settlement failed", [str(exc)])
            inconsistent = {b: v.conflicts for b, v in verdicts.items() if not v.consistent}
            if inconsistent:
                return CabinetResult(
                    status="REJECTED",
                    message="evidence inconsistent; no crediting possible",
                    data={"blocks": {b: v.to_json() for b, v in verdicts.items()}},
                    errors=[f"{b}: {msgs}" for b, msgs in inconsistent.items()],
                )
            return CabinetResult(
                status="ACCEPTED",
                message="settled",
                data={"blocks": {b: v.to_json() for b, v in verdicts.items()}},
            )

    def _all_lines(self) -> dict[str, dict[str, list[str]]]:
        lines: dict[str, dict[str, list[str]]] = {}
        for route in self.routes.values():
            lines.setdefault(route.block, {})[route.route_id] = sorted(route.households)
        return lines

    def _verdict(self, block: str):
        observations = self._observations()
        verdicts = settle(
            self.universes,
            observations,
            {block: self._block_lines(block)},
        )
        verdict = verdicts[block]
        if not verdict.consistent:
            raise ContractError(f"block {block!r} evidence inconsistent: {verdict.conflicts}")
        return verdict, observations

    # ------------------------------------------------------------------
    # Accounting: credit what is already guaranteed, expose the gap
    # ------------------------------------------------------------------

    def accounting(self, contract_no: str) -> CabinetResult:
        """认款: book the guaranteed reach and report the residual gap."""
        with self._lock:
            contract = self.contracts.get(contract_no)
            if contract is None:
                return _reject("unknown contract", [contract_no])
            try:
                verdict, observations = self._verdict(contract.block)
            except ContractError as exc:
                return _reject("cannot account", [str(exc)])

            extra_sets = self._delivered_extra_sets(contract)
            projected = lower_bound_with_extra(
                self.universes[contract.block],
                [o for o in observations if o.block == contract.block],
                extra_sets,
            )
            credited = min(contract.guarantee, projected)
            gap = max(0, contract.guarantee - projected)
            return CabinetResult(
                status="ACCEPTED",
                message="accounting computed",
                data={
                    "contract_no": contract.contract_no,
                    "block": contract.block,
                    "guarantee": contract.guarantee,
                    "delivery_lower_bound": verdict.lower_bound,
                    "projected_lower_bound": projected,
                    "credited": credited,
                    "residual_gap": gap,
                    "delivered_makeup_orders": [
                        order.order_id for order in self._contract_orders(contract_no)
                        if order.state == DELIVERED
                    ],
                    "basis": {
                        "witness": verdict.to_json()["witness"],
                        "history_count": verdict.history_count,
                    },
                },
            )

    def _contract_orders(self, contract_no: str) -> list[MakeupOrder]:
        return [o for o in self.orders.values() if o.contract_no == contract_no]

    def _delivered_extra_sets(self, contract: AdContract) -> list[frozenset]:
        """Coverage sets already locked by *delivered* makeup orders."""
        extra: list[frozenset] = []
        for order in self._contract_orders(contract.contract_no):
            if order.state != DELIVERED:
                continue
            if order.mode == TARGETED:
                extra.append(frozenset({order.target_household}))
            else:
                extra.append(frozenset(order.route_households))
        return extra

    # ------------------------------------------------------------------
    # Slot accounting (reserved vs. real)
    # ------------------------------------------------------------------

    def slot_state(self, edition: str) -> dict:
        with self._lock:
            sealed = self._sealed_slots.get(edition, 0)
            reserved = sum(
                order.slots
                for order in self.orders.values()
                if order.sealed_edition == edition and order.state == SIGNED
            )
            used = sealed + reserved
            return {
                "edition": edition,
                "capacity": self.edition_slots,
                "sealed_real_slots": sealed,
                "reserved_by_signed": reserved,
                "available": self.edition_slots - used,
            }

    def _deadline_open(self, contract: AdContract) -> str | None:
        if contract.deadline is None:
            return None
        now = self._clock()
        if now is not None and now > contract.deadline:
            return f"past deadline {contract.deadline!r} (now {now!r})"
        return None

    # ------------------------------------------------------------------
    # Signing makeup orders
    # ------------------------------------------------------------------

    def sign_targeted(self, contract_no: str, household: str) -> CabinetResult:
        with self._lock:
            if self.mode != TARGETED:
                return _reject("cabinet runs whole_line mode; targeted orders refused")
            contract = self.contracts.get(contract_no)
            if contract is None:
                return _reject("unknown contract", [contract_no])
            closed = self._deadline_open(contract)
            if closed:
                return _reject("cannot sign", [closed])
            if household not in contract.permitted:
                return _reject(
                    "household not permitted by advertiser",
                    [f"{household!r} not in {sorted(contract.permitted)}"],
                )
            try:
                verdict, _ = self._verdict(contract.block)
            except ContractError as exc:
                return _reject("cannot sign", [str(exc)])
            # Targeted signing needs the station's named non-receipt evidence
            # in every edition.  A merely *possible* misser is not enough:
            # before that receipt arrives the cabinet must refuse to sign.
            if household not in verdict.proven_missed:
                return _reject(
                    "awaiting named non-receipt receipt before targeted signing",
                    [
                        f"no named non-receipt proves {household!r} missed every "
                        f"edition; proven missed: {verdict.proven_missed}, "
                        f"possible missers: {verdict.possible_missers}"
                    ],
                )
            return self._create_order(
                contract,
                mode=TARGETED,
                slots=1,
                target_household=household,
                basis={
                    "kind": "targeted_named_miss",
                    "proven_missed": list(verdict.proven_missed),
                    "witness": verdict.to_json()["witness"],
                    "lower_bound": verdict.lower_bound,
                },
            )

    def sign_whole_line(self, contract_no: str, route_id: str) -> CabinetResult:
        with self._lock:
            if self.mode != WHOLE_LINE:
                return _reject("cabinet runs targeted mode; whole-line orders refused")
            contract = self.contracts.get(contract_no)
            if contract is None:
                return _reject("unknown contract", [contract_no])
            closed = self._deadline_open(contract)
            if closed:
                return _reject("cannot sign", [closed])
            route = self.routes.get(route_id)
            if route is None or route.block != contract.block:
                return _reject(
                    "unknown route for block",
                    [f"route {route_id!r} not available on {contract.block!r}"],
                )
            if not route.households <= contract.permitted:
                return _reject(
                    "route leaves advertiser permission",
                    [
                        f"{sorted(route.households - contract.permitted)} not permitted"
                    ],
                )
            try:
                verdict, observations = self._verdict(contract.block)
            except ContractError as exc:
                return _reject("cannot sign", [str(exc)])
            evaluation = verdict.line_evaluations.get(route_id)
            accounting = self.accounting(contract_no)
            gap = accounting.data["residual_gap"]
            if gap == 0:
                return _reject("contract already at guarantee; no residual gap")
            if evaluation.added_lower_bound < gap:
                return _reject(
                    "line cannot guarantee the gap under worst overlap",
                    [
                        f"added lower bound {evaluation.added_lower_bound} "
                        f"< residual gap {gap}"
                    ],
                )
            # Recompute the projected bound with this line to freeze the basis.
            block_obs = [o for o in observations if o.block == contract.block]
            prior_extra = self._delivered_extra_sets(contract)
            projected = lower_bound_with_extra(
                self.universes[contract.block],
                block_obs,
                prior_extra + [frozenset(route.households)],
            )
            return self._create_order(
                contract,
                mode=WHOLE_LINE,
                slots=len(route.households),
                route=route,
                basis={
                    "kind": "whole_line_added_lower_bound",
                    "route_id": route_id,
                    "added_lower_bound": evaluation.added_lower_bound,
                    "residual_gap_at_signing": gap,
                    "projected_lower_bound": projected,
                    "line_witness": evaluation.to_json()["witness"],
                    "block_witness": verdict.to_json()["witness"],
                },
            )

    def _create_order(
        self,
        contract: AdContract,
        *,
        mode: str,
        slots: int,
        basis: dict,
        target_household: str | None = None,
        route: Route | None = None,
    ) -> CabinetResult:
        edition = contract.makeup_edition
        slot_info = self.slot_state(edition)
        if slot_info["available"] < slots:
            return _reject(
                "not enough available edition slots",
                [
                    f"need {slots}, available {slot_info['available']} "
                    f"(capacity {self.edition_slots})"
                ],
            )
        self._order_seq += 1
        order_id = f"MO-{self._order_seq:04d}"
        order = MakeupOrder(
            order_id=order_id,
            contract_no=contract.contract_no,
            mode=mode,
            block=contract.block,
            slots=slots,
            state=SIGNED,
            basis=basis,
            target_household=target_household,
            route_id=route.route_id if route else None,
            route_households=frozenset(route.households) if route else None,
            sealed_edition=edition,
        )
        self.orders[order_id] = order
        return CabinetResult(
            status="ACCEPTED",
            message="makeup order signed; slots reserved",
            data={"order": order.to_json(), "slot_state": self.slot_state(edition)},
        )

    # ------------------------------------------------------------------
    # Sealing, delivery, withdrawal
    # ------------------------------------------------------------------

    def seal_edition(self, edition: str) -> CabinetResult:
        """封版: convert every signed reservation on the edition into real slots."""
        with self._lock:
            touched = [
                order for order in self.orders.values()
                if order.sealed_edition == edition and order.state == SIGNED
            ]
            if not touched:
                return _reject("no signed orders to seal", [edition])
            total = sum(order.slots for order in touched)
            already = self._sealed_slots.get(edition, 0)
            if already + total > self.edition_slots:
                return _reject(
                    "sealing would exceed edition capacity",
                    [f"{already}+{total} > {self.edition_slots}"],
                )
            for order in touched:
                order.state = SEALED
                # Freeze the signing basis permanently at seal time.
                order.basis = _deep_copy(order.basis)
            self._sealed_slots[edition] = already + total
            return CabinetResult(
                status="ACCEPTED",
                message="edition sealed; real slots locked",
                data={
                    "edition": edition,
                    "sealed_orders": [o.order_id for o in touched],
                    "slot_state": self.slot_state(edition),
                },
            )

    def deliver(self, order_id: str, receipt: dict | None = None) -> CabinetResult:
        """Deliver a sealed order and attach the named/line receipt."""
        with self._lock:
            order = self.orders.get(order_id)
            if order is None:
                return _reject("unknown order", [order_id])
            if order.state == DELIVERED:
                # Idempotent: replaying a delivery never double-credits.
                return CabinetResult(
                    status="ACCEPTED",
                    message="delivery replayed; not credited twice",
                    data={"order": order.to_json()},
                )
            if order.state != SEALED:
                return _reject("order must be sealed before delivery", [order.state])
            order.state = DELIVERED
            order.receipt = receipt or self._standard_receipt(order)
            return CabinetResult(
                status="ACCEPTED",
                message="delivered with receipt",
                data={"order": order.to_json(), "receipt": order.receipt},
            )

    def _standard_receipt(self, order: MakeupOrder) -> dict:
        if order.mode == TARGETED:
            return {
                "kind": "named_delivery_receipt",
                "order_id": order.order_id,
                "household": order.target_household,
                "block": order.block,
            }
        return {
            "kind": "whole_line_delivery_receipt",
            "order_id": order.order_id,
            "route_id": order.route_id,
            "households": sorted(order.route_households),
            "block": order.block,
        }

    def withdraw(self, order_id: str) -> CabinetResult:
        """Withdraw an order still unsigned-in-print; sealed orders are kept."""
        with self._lock:
            order = self.orders.get(order_id)
            if order is None:
                return _reject("unknown order", [order_id])
            if order.state != SIGNED:
                return _reject(
                    "only unsealed signed orders can be withdrawn",
                    [f"order is {order.state}; basis retained"],
                )
            removed = self.orders.pop(order_id)
            return CabinetResult(
                status="ACCEPTED",
                message="order withdrawn; reserved slots released",
                data={
                    "withdrawn": removed.to_json(),
                    "slot_state": self.slot_state(removed.sealed_edition),
                },
            )

    def basis_still_holds(self, order_id: str) -> CabinetResult:
        """Re-check an unsealed order's basis against the current evidence."""
        with self._lock:
            order = self.orders.get(order_id)
            if order is None:
                return _reject("unknown order", [order_id])
            contract = self.contracts[order.contract_no]
            try:
                verdict, _ = self._verdict(contract.block)
            except ContractError as exc:
                return _reject("basis check failed", [str(exc)])
            if order.state in (SEALED, DELIVERED):
                return CabinetResult(
                    status="ACCEPTED",
                    message="historical basis retained by rule",
                    data={"order_id": order_id, "holds": True, "frozen": True},
                )
            if order.mode == TARGETED:
                holds = order.target_household in verdict.proven_missed
            else:
                evaluation = verdict.line_evaluations.get(order.route_id)
                holds = (
                    evaluation is not None
                    and evaluation.added_lower_bound
                    >= order.basis["residual_gap_at_signing"]
                )
            return CabinetResult(
                status="ACCEPTED",
                message="basis holds" if holds else "basis invalidated; withdraw",
                data={"order_id": order_id, "holds": holds, "frozen": False},
            )

    # ------------------------------------------------------------------
    # Read-back by contract number
    # ------------------------------------------------------------------

    def readback(self, contract_no: str) -> CabinetResult:
        """按合同号读回版次、补投服务及其依据."""
        with self._lock:
            contract = self.contracts.get(contract_no)
            if contract is None:
                return _reject("unknown contract", [contract_no])
            accounting = self.accounting(contract_no)
            orders = [
                self.orders[o_id].to_json()
                for o_id in sorted(self.orders)
                if self.orders[o_id].contract_no == contract_no
            ]
            return CabinetResult(
                status="ACCEPTED",
                message="contract read-back",
                data={
                    "contract": contract.to_json(),
                    "makeup_edition_used": contract.makeup_edition,
                    "slot_state": self.slot_state(contract.makeup_edition),
                    "accounting": accounting.data if accounting.accepted else None,
                    "orders": orders,
                },
            )

    # ------------------------------------------------------------------
    # Persistence (local JSON state, same shape as the base CLI state)
    # ------------------------------------------------------------------

    def to_state(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "edition_slots": self.edition_slots,
                "universes": {b: sorted(h) for b, h in self.universes.items()},
                "routes": [route.to_json() for route in self.routes.values()],
                "contracts": [c.to_json() for c in self.contracts.values()],
                "orders": [o.to_json() for o in self.orders.values()],
                "order_seq": self._order_seq,
                "sealed_slots": dict(self._sealed_slots),
                "last_receipts": {k: _deep_copy(v) for k, v in self.last_receipts.items()},
                "station_documents": {
                    k: _deep_copy(v) for k, v in self.station_documents.items()
                },
            }

    @classmethod
    def from_state(
        cls,
        state: dict,
        reader: Contract,
        writer: Contract,
        *,
        clock: Callable[[], str | None] | None = None,
    ) -> "PaperCabinet":
        cabinet = cls(
            reader,
            writer,
            mode=state["mode"],
            edition_slots=state["edition_slots"],
            universes=state.get("universes", {}),
            routes=[
                Route(item["route_id"], item["block"], frozenset(item["households"]))
                for item in state.get("routes", [])
            ],
            clock=clock,
        )
        cabinet.contracts = {
            doc["contract_no"]: AdContract(
                contract_no=doc["contract_no"],
                block=doc["block"],
                guarantee=doc["guarantee"],
                permitted=frozenset(doc["permitted"]),
                makeup_edition=doc["makeup_edition"],
                deadline=doc.get("deadline"),
            )
            for doc in state.get("contracts", [])
        }
        cabinet._order_seq = state.get("order_seq", 0)
        cabinet._sealed_slots = dict(state.get("sealed_slots", {}))
        cabinet.last_receipts = dict(state.get("last_receipts", {}))
        # Replay every accepted station file through the original ingestion
        # library so revision resolution and replay idempotency stay exact.
        for doc in state.get("station_documents", {}).values():
            replayed = cabinet.library.ingest(writer, doc)
            if not replayed.accepted:
                raise ContractError(
                    f"cannot restore cabinet state; station file rejected on reload: "
                    f"{replayed.errors}"
                )
            cabinet.station_documents[replayed.file_id] = _deep_copy(doc)
        for doc in state.get("orders", []):
            cabinet.orders[doc["order_id"]] = MakeupOrder(
                order_id=doc["order_id"],
                contract_no=doc["contract_no"],
                mode=doc["mode"],
                block=doc["block"],
                slots=doc["slots"],
                state=doc["state"],
                basis=doc["basis"],
                target_household=doc.get("target_household"),
                route_id=doc.get("route_id"),
                route_households=frozenset(doc["route_households"])
                if doc.get("route_households") is not None
                else None,
                receipt=doc.get("receipt"),
                sealed_edition=doc.get("sealed_edition"),
            )
        return cabinet
