"""JSON plan runner for the quote service and ledger.

A plan is ``{"steps": [ ... ]}``.  Each step is an object with an ``op`` and
operation-specific fields.  Steps may be named; later steps can reference an
earlier result with a ``"$name"`` placeholder (``"$name"`` alone returns the
whole result, ``"$name.field"`` walks dictionaries, ``"$name.0"`` indexes
lists).  Every executed step is recorded in the returned plan result so the
delivered JSON is a complete, replayable transcript.

Supported ops::

    publish_snapshot   products, coupons, snapshot_id?
    get_snapshot       snapshot_id
    quote              basket, coupons, request_id, snapshot_id?
    get_quote          request_id
    register_sale      sale_id, quote_request_id | quote, snapshot_id?
    get_sale           sale_id
    return             request_id, sale_id, unit_ids, grade
    get_return         request_id
    adjust_inventory   event_id, sku, quantity, bucket
    inventory          skus?
    audit_sales
    audit_inventory
    set_shop_mode      mode
    register_physical  physical_id, side, width, length?, stretch_to?, directions?, origin?, sale_id?, unit_id?, fitter?
    recover_from_sale  physical_id, side, sale_id, unit_id, width, length?, stretch_to?, directions?, fitter?
    get_physical       physical_id
    stretch            physical_id, to_width, fitter?
    update_fitting     physical_id, stretch_to? | directions? | length?
    consign            physical_id, dancer_id, receipt_id
    revoke_consignment physical_id
    register_dancer    dancer_id, name?, left?, right?, run?
    get_dancer         dancer_id
    publish_fitting_slip  slip_id, physical_id
    retract_fitting_slip  slip_id
    get_fitting_slip   slip_id
    register_pad       pad_id, width_step?, holder?
    get_pad            pad_id
    record_pad_receipt transfer_id, pad_id, from_holder, to_holder, receipt_id
    acquire_borrowable_pad  transfer_id, pad_id, receipt_id
    pad_ledger         pad_id?
    assign_occupancy   physical_id, run_id
    release_occupancy  physical_id, run_id
    create_appointment appointment_id, needs, run?, stretch_budget?
    allocate           allocation_id, needs, run?, stretch_budget?, use_pads?, require_slip?
    get_allocation     allocation_id
    confirm_delivery   allocation_id, run_until?, tied_laces?
    return_delivery    binding_id, on_time?, receipt_id?
    handover_return    show_id, on_time?, receipt_id?
    list_bindings      active_only?
    confirm_return     confirmation_id, return_request_id, sale_id
    cabinet_view

Any step may set ``"expect_error": <code>`` (string or list) to assert that the
operation is rejected with that error code; the transcript then records a
passing step instead of aborting.  Without it, the first error aborts the plan
and is reported in the transcript.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

from .errors import BasketError


class PlanError(Exception):
    def __init__(self, message: str, *, step_index: int, step: Any, transcript: List[Dict[str, Any]]):
        super().__init__(message)
        self.step_index = step_index
        self.step = step
        self.transcript = transcript


def _walk(value: Any, path: Sequence[str]) -> Any:
    current = value
    for part in path:
        if isinstance(current, Mapping):
            if part not in current:
                raise KeyError(part)
            current = current[part]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            current = current[int(part)]
        else:
            raise KeyError(part)
    return current


def _resolve(value: Any, env: Mapping[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        body = value[1:]
        if body == "":
            raise PlanError("empty placeholder", step_index=-1, step=value, transcript=[])
        parts = body.split(".")
        name, rest = parts[0], parts[1:]
        if name not in env:
            raise PlanError(f"unknown step name {name!r}", step_index=-1, step=value, transcript=[])
        return _walk(env[name], rest) if rest else env[name]
    if isinstance(value, list):
        return [_resolve(item, env) for item in value]
    if isinstance(value, Mapping):
        return {key: _resolve(item, env) for key, item in value.items()}
    return value


def _require(step: Mapping[str, Any], key: str, op: str) -> Any:
    if key not in step:
        raise PlanError(f"{op} step missing {key!r}", step_index=-1, step=step, transcript=[])
    return step[key]


def _error_codes(expected: Any) -> set[str]:
    if isinstance(expected, str):
        return {expected}
    if isinstance(expected, Sequence):
        return {str(code) for code in expected}
    return {str(expected)}


def _execute(service, step: Mapping[str, Any], env: Mapping[str, Any]) -> Any:
    op = step.get("op")
    if op == "publish_snapshot":
        return service.publish_snapshot(
            step.get("products"),
            step.get("coupons"),
            snapshot_id=step.get("snapshot_id"),
        )
    if op == "get_snapshot":
        return service.get_snapshot(_require(step, "snapshot_id", op))
    if op == "quote":
        return service.quote(
            _require(step, "basket", op),
            _require(step, "coupons", op),
            _require(step, "request_id", op),
            step.get("snapshot_id"),
        )
    if op == "get_quote":
        return service.get_quote(_require(step, "request_id", op))
    if op == "register_sale":
        sale_id = _require(step, "sale_id", op)
        if "quote" in step:
            kwargs: Dict[str, Any] = {"quote": step["quote"]}
        elif "quote_request_id" in step:
            kwargs = {"request_id": step["quote_request_id"]}
        elif "quote_id" in step:
            kwargs = {"quote_id": step["quote_id"]}
        else:
            raise PlanError("register_sale needs quote or quote_request_id", step_index=-1, step=step, transcript=[])
        if "snapshot_id" in step:
            kwargs["snapshot_id"] = step["snapshot_id"]
        return service.register_sale(sale_id, **kwargs)
    if op == "get_sale":
        return service.get_sale(_require(step, "sale_id", op))
    if op == "return":
        return service.return_units(
            _require(step, "request_id", op),
            _require(step, "sale_id", op),
            _require(step, "unit_ids", op),
            _require(step, "grade", op),
        )
    if op == "get_return":
        return service.get_return(_require(step, "request_id", op))
    if op == "adjust_inventory":
        return service.adjust_inventory(
            _require(step, "event_id", op),
            _require(step, "sku", op),
            _require(step, "quantity", op),
            _require(step, "bucket", op),
        )
    if op == "inventory":
        return service.inventory(step.get("skus"))
    if op == "audit_sales":
        return service.audit_sales()
    if op == "audit_inventory":
        return service.audit_inventory()
    # troupe fitting / cabinet / paired delivery ---------------------------
    if op == "set_shop_mode":
        return service.set_shop_mode(_require(step, "mode", op))
    if op == "register_physical":
        return service.register_physical(
            _require(step, "physical_id", op),
            _require(step, "side", op),
            width=_require(step, "width", op),
            length=step.get("length"),
            stretch_to=step.get("stretch_to"),
            directions=step.get("directions"),
            origin=step.get("origin", "STOCK"),
            sale_id=step.get("sale_id"),
            unit_id=step.get("unit_id"),
            fitter=step.get("fitter"),
        )
    if op == "recover_from_sale":
        return service.recover_from_sale(
            _require(step, "physical_id", op),
            _require(step, "side", op),
            _require(step, "sale_id", op),
            _require(step, "unit_id", op),
            width=_require(step, "width", op),
            length=step.get("length"),
            stretch_to=step.get("stretch_to"),
            directions=step.get("directions"),
            fitter=step.get("fitter"),
        )
    if op == "get_physical":
        return service.get_physical(_require(step, "physical_id", op))
    if op == "stretch":
        return service.stretch(_require(step, "physical_id", op), _require(step, "to_width", op), fitter=step.get("fitter"))
    if op == "update_fitting":
        kwargs: Dict[str, Any] = {}
        for key in ("stretch_to", "directions", "length"):
            if key in step:
                kwargs[key] = step[key]
        return service.update_fitting(_require(step, "physical_id", op), **kwargs)
    if op == "consign":
        return service.consign(_require(step, "physical_id", op), _require(step, "dancer_id", op), _require(step, "receipt_id", op))
    if op == "revoke_consignment":
        return service.revoke_consignment(_require(step, "physical_id", op))
    if op == "register_dancer":
        return service.register_dancer(
            _require(step, "dancer_id", op),
            name=step.get("name"),
            left=step.get("left"),
            right=step.get("right"),
            run=step.get("run"),
        )
    if op == "get_dancer":
        return service.get_dancer(_require(step, "dancer_id", op))
    if op == "publish_fitting_slip":
        return service.publish_fitting_slip(_require(step, "slip_id", op), _require(step, "physical_id", op))
    if op == "retract_fitting_slip":
        return service.retract_fitting_slip(_require(step, "slip_id", op))
    if op == "get_fitting_slip":
        return service.get_fitting_slip(_require(step, "slip_id", op))
    if op == "register_pad":
        return service.register_pad(_require(step, "pad_id", op), width_step=step.get("width_step", 1), holder=step.get("holder", "shop"))
    if op == "get_pad":
        return service.get_pad(_require(step, "pad_id", op))
    if op == "record_pad_receipt":
        return service.record_pad_receipt(
            _require(step, "transfer_id", op),
            _require(step, "pad_id", op),
            _require(step, "from_holder", op),
            _require(step, "to_holder", op),
            _require(step, "receipt_id", op),
        )
    if op == "acquire_borrowable_pad":
        return service.acquire_borrowable_pad(
            _require(step, "transfer_id", op), _require(step, "pad_id", op), _require(step, "receipt_id", op)
        )
    if op == "pad_ledger":
        return service.pad_ledger(step.get("pad_id"))
    if op == "assign_occupancy":
        return service.assign_occupancy(_require(step, "physical_id", op), _require(step, "run_id", op))
    if op == "release_occupancy":
        return service.release_occupancy(_require(step, "physical_id", op), _require(step, "run_id", op))
    if op == "create_appointment":
        return service.create_appointment(
            _require(step, "appointment_id", op),
            _require(step, "needs", op),
            run=step.get("run"),
            stretch_budget=step.get("stretch_budget", 0),
        )
    if op == "allocate":
        return service.allocate(
            _require(step, "allocation_id", op),
            _require(step, "needs", op),
            run=step.get("run"),
            stretch_budget=step.get("stretch_budget", 0),
            use_pads=step.get("use_pads", True),
            require_slip=step.get("require_slip", True),
        )
    if op == "get_allocation":
        return service.get_allocation(_require(step, "allocation_id", op))
    if op == "confirm_delivery":
        return service.confirm_delivery(
            _require(step, "allocation_id", op),
            run_until=step.get("run_until", "run-end"),
            tied_laces=step.get("tied_laces", True),
        )
    if op == "return_delivery":
        return service.return_delivery(
            _require(step, "binding_id", op), on_time=step.get("on_time", True), receipt_id=step.get("receipt_id")
        )
    if op == "handover_return":
        return service.handover_return(
            _require(step, "show_id", op), on_time=step.get("on_time", True), receipt_id=step.get("receipt_id")
        )
    if op == "list_bindings":
        return service.list_bindings(active_only=step.get("active_only", False))
    if op == "confirm_return":
        return service.confirm_return(
            _require(step, "confirmation_id", op),
            _require(step, "return_request_id", op),
            _require(step, "sale_id", op),
        )
    if op == "cabinet_view":
        return service.cabinet_view()
    raise PlanError(f"unknown op {op!r}", step_index=-1, step=step, transcript=[])


def run_plan(service, plan: Any) -> Dict[str, Any]:
    if not isinstance(plan, Mapping) or not isinstance(plan.get("steps"), Sequence):
        raise PlanError("plan must be an object with a steps list", step_index=-1, step=plan, transcript=[])
    env: Dict[str, Any] = {}
    transcript: List[Dict[str, Any]] = []
    for index, raw_step in enumerate(plan["steps"]):
        if not isinstance(raw_step, Mapping):
            raise PlanError("each step must be an object", step_index=index, step=raw_step, transcript=transcript)
        name = raw_step.get("name")
        expect = raw_step.get("expect_error")
        try:
            step = _resolve(raw_step, env)
        except (PlanError, KeyError) as exc:
            raise PlanError(f"placeholder resolution failed: {exc}", step_index=index, step=raw_step, transcript=transcript)
        record: Dict[str, Any] = {"index": index, "op": raw_step.get("op")}
        if name is not None:
            record["name"] = name
        try:
            result = _execute(service, step, env)
            if expect is not None:
                raise PlanError(
                    f"expected error {expect!r} but step succeeded",
                    step_index=index,
                    step=raw_step,
                    transcript=transcript,
                )
            record.update({"ok": True, "result": result})
            if name is not None:
                env[name] = result
        except BasketError as exc:
            record.update({"ok": False, "error": exc.to_dict()["error"]})
            if expect is None:
                transcript.append(record)
                raise PlanError(
                    f"step failed with {exc.code}: {exc.message}",
                    step_index=index,
                    step=raw_step,
                    transcript=transcript,
                )
            if exc.code not in _error_codes(expect):
                transcript.append(record)
                raise PlanError(
                    f"expected {expect!r} but got {exc.code}",
                    step_index=index,
                    step=raw_step,
                    transcript=transcript,
                )
            record["expected_error_observed"] = exc.code
            if name is not None:
                env[name] = exc.to_dict()["error"]
        transcript.append(record)
    return {"ok": True, "steps": transcript}
