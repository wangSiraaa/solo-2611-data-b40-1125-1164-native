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

Optics handover ops::

    add_telescope      telescope_id, out_iface, travel_lo, travel_hi
    get_telescope      telescope_id
    add_batch          batch_id, scale_lo, scale_hi, cards
    calibrate_batch    batch_id, cards
    get_batch          batch_id
    register_piece     piece_id, kind, model, sale_id, unit_id, ...piece fields
    get_piece          piece_id
    piece_path_interval piece_id
    update_piece_evidence piece_id, measured_lo, measured_hi
    assess_combo       telescope_id, eyepiece_id, adapter_ids
    lock_group         group_id, telescope_id, eyepiece_id, adapter_ids
    verify_group       group_id, observe_wheel, technician
    unseal_group       group_id
    get_group          group_id
    add_session        session_id, seq, starts_at, ends_at
    get_session        session_id
    place_order        order_id, sale_id, group_id, session_id, slot_lo, slot_hi
    deliver_order      order_id
    fulfil_session     session_id
    cancel_order       order_id, reason?
    switch_telescope   order_id, new_telescope_id
    get_order          order_id
    get_delivery       order_id
    return_pieces      order_id
    return_piece       piece_id
    list_orders
    list_bookings

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
    if op == "add_telescope":
        return service.add_telescope(
            _require(step, "telescope_id", op), _require(step, "out_iface", op),
            _require(step, "travel_lo", op), _require(step, "travel_hi", op))
    if op == "get_telescope":
        return service.get_telescope(_require(step, "telescope_id", op))
    if op == "add_batch":
        return service.add_batch(
            _require(step, "batch_id", op), _require(step, "scale_lo", op),
            _require(step, "scale_hi", op), step.get("cards", []))
    if op == "calibrate_batch":
        return service.calibrate_batch(_require(step, "batch_id", op), step.get("cards", []))
    if op == "get_batch":
        return service.get_batch(_require(step, "batch_id", op))
    if op == "register_piece":
        fields = {k: v for k, v in step.items() if k not in ("op", "name", "expect_error", "piece_id", "kind", "model", "sale_id", "unit_id")}
        return service.register_piece(
            _require(step, "piece_id", op), _require(step, "kind", op), _require(step, "model", op),
            _require(step, "sale_id", op), _require(step, "unit_id", op), **fields)
    if op == "get_piece":
        return service.get_piece(_require(step, "piece_id", op))
    if op == "piece_path_interval":
        return list(service.piece_path_interval(_require(step, "piece_id", op)))
    if op == "update_piece_evidence":
        return service.update_piece_evidence(
            _require(step, "piece_id", op), _require(step, "measured_lo", op),
            _require(step, "measured_hi", op))
    if op == "assess_combo":
        return service.assess_combo(
            _require(step, "telescope_id", op), _require(step, "eyepiece_id", op),
            step.get("adapter_ids", []))
    if op == "lock_group":
        return service.lock_group(
            _require(step, "group_id", op), _require(step, "telescope_id", op),
            _require(step, "eyepiece_id", op), step.get("adapter_ids", []))
    if op == "verify_group":
        return service.verify_group(
            _require(step, "group_id", op), observe_wheel=_require(step, "observe_wheel", op),
            technician=_require(step, "technician", op))
    if op == "unseal_group":
        return service.unseal_group(_require(step, "group_id", op))
    if op == "get_group":
        return service.get_group(_require(step, "group_id", op))
    if op == "add_session":
        return service.add_session(
            _require(step, "session_id", op), _require(step, "seq", op),
            _require(step, "starts_at", op), _require(step, "ends_at", op))
    if op == "get_session":
        return service.get_session(_require(step, "session_id", op))
    if op == "place_order":
        return service.place_order(
            _require(step, "order_id", op), _require(step, "sale_id", op),
            _require(step, "group_id", op), _require(step, "session_id", op),
            _require(step, "slot_lo", op), _require(step, "slot_hi", op))
    if op == "deliver_order":
        return service.deliver_order(_require(step, "order_id", op))
    if op == "fulfil_session":
        return service.fulfil_session(_require(step, "session_id", op))
    if op == "cancel_order":
        return service.cancel_order(_require(step, "order_id", op), step.get("reason", ""))
    if op == "switch_telescope":
        return service.switch_telescope(
            _require(step, "order_id", op), _require(step, "new_telescope_id", op))
    if op == "get_order":
        return service.get_order(_require(step, "order_id", op))
    if op == "get_delivery":
        return service.get_delivery(_require(step, "order_id", op))
    if op == "return_pieces":
        return service.return_pieces(_require(step, "order_id", op))
    if op == "return_piece":
        return service.return_piece(_require(step, "piece_id", op))
    if op == "list_orders":
        return service.list_orders()
    if op == "list_bookings":
        return service.list_bookings()
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
