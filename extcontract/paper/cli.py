"""Local command-line entry point for the 发行柜 (distribution cabinet).

    python -m extcontract paper init      STATE --mode ... --slots ...
    python -m extcontract paper contract  STATE CONTRACT.json
    python -m extcontract paper station   STATE FILE.json [--receipt FILE_ID]
    python -m extcontract paper settle    STATE
    python -m extcontract paper account   STATE CONTRACT_NO
    python -m extcontract paper sign      STATE CONTRACT_NO (--household H | --route R)
    python -m extcontract paper seal      STATE EDITION
    python -m extcontract paper deliver   STATE ORDER_ID
    python -m extcontract paper withdraw  STATE ORDER_ID
    python -m extcontract paper readback  STATE CONTRACT_NO

State is one JSON document per cabinet; station files are persisted and
re-played through the original ingest path on reload, so reload preserves the
revision rules and replay idempotency instead of bypassing them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..contract import load_contract
from .cabinet import PaperCabinet, Route

# Defaults resolve against the repository root, so the CLI works from any CWD.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WRITER = _REPO_ROOT / "materials/paper/delivery-writer-contract.json"
DEFAULT_READER = _REPO_ROOT / "materials/paper/delivery-reader-contract.json"


def _load_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _contracts(args):
    writer = load_contract(args.writer, role="writer")
    reader = load_contract(args.reader, role="reader")
    return writer, reader


def _load_cabinet(state_path: Path, writer, reader) -> PaperCabinet:
    state = _load_json(str(state_path))
    return PaperCabinet.from_state(state, reader, writer)


def _save(cabinet: PaperCabinet, state_path: Path) -> None:
    _dump(state_path, cabinet.to_state())


def _print(result) -> int:
    print(json.dumps(result.to_json(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.accepted else 1


def cmd_init(args) -> int:
    writer, reader = _contracts(args)
    routes = [
        Route(item["route_id"], item["block"], frozenset(item["households"]))
        for item in args.routes_doc.get("routes", [])
    ] if args.routes_doc else []
    universes = args.universes_doc.get("blocks", {}) if args.universes_doc else {}
    cabinet = PaperCabinet(
        reader,
        writer,
        mode=args.mode,
        edition_slots=args.slots,
        universes=universes,
        routes=routes,
    )
    _save(cabinet, Path(args.state))
    print(json.dumps({"status": "ACCEPTED", "state": args.state,
                      "mode": args.mode, "slots": args.slots}, ensure_ascii=False, indent=2))
    return 0


def cmd_contract(args) -> int:
    writer, reader = _contracts(args)
    cabinet = _load_cabinet(Path(args.state), writer, reader)
    result = cabinet.register_contract(_load_json(args.document))
    if result.accepted:
        _save(cabinet, Path(args.state))
    return _print(result)


def cmd_station(args) -> int:
    writer, reader = _contracts(args)
    cabinet = _load_cabinet(Path(args.state), writer, reader)
    result = cabinet.receive_station_file(_load_json(args.document))
    if result.accepted:
        _save(cabinet, Path(args.state))
        if args.receipt:
            receipt = cabinet.last_receipt(args.receipt)
            _dump(Path(args.receipt_out or "/dev/stdout"), receipt or {})
    return _print(result)


def cmd_settle(args) -> int:
    writer, reader = _contracts(args)
    cabinet = _load_cabinet(Path(args.state), writer, reader)
    return _print(cabinet.settle())


def cmd_account(args) -> int:
    writer, reader = _contracts(args)
    cabinet = _load_cabinet(Path(args.state), writer, reader)
    return _print(cabinet.accounting(args.contract_no))


def cmd_sign(args) -> int:
    writer, reader = _contracts(args)
    cabinet = _load_cabinet(Path(args.state), writer, reader)
    if cabinet.mode == "targeted":
        if not args.household:
            return _print(_reject_cli("targeted mode requires --household"))
        result = cabinet.sign_targeted(args.contract_no, args.household)
    else:
        if not args.route:
            return _print(_reject_cli("whole_line mode requires --route"))
        result = cabinet.sign_whole_line(args.contract_no, args.route)
    if result.accepted:
        _save(cabinet, Path(args.state))
    return _print(result)


def cmd_seal(args) -> int:
    writer, reader = _contracts(args)
    cabinet = _load_cabinet(Path(args.state), writer, reader)
    result = cabinet.seal_edition(args.edition)
    if result.accepted:
        _save(cabinet, Path(args.state))
    return _print(result)


def cmd_deliver(args) -> int:
    writer, reader = _contracts(args)
    cabinet = _load_cabinet(Path(args.state), writer, reader)
    result = cabinet.deliver(args.order_id)
    if result.accepted:
        _save(cabinet, Path(args.state))
    return _print(result)


def cmd_withdraw(args) -> int:
    writer, reader = _contracts(args)
    cabinet = _load_cabinet(Path(args.state), writer, reader)
    result = cabinet.withdraw(args.order_id)
    if result.accepted:
        _save(cabinet, Path(args.state))
    return _print(result)


def cmd_readback(args) -> int:
    writer, reader = _contracts(args)
    cabinet = _load_cabinet(Path(args.state), writer, reader)
    result = cabinet.readback(args.contract_no)
    if args.receipt:
        receipt = cabinet.last_receipt(args.receipt)
        result.data["previous_receipt"] = receipt
    return _print(result)


def _reject_cli(message: str):
    from .cabinet import CabinetResult
    return CabinetResult(status="REJECTED", message=message, errors=[message])


def build_paper_parser(sub: argparse._SubParsersAction) -> None:
    paper = sub.add_parser("paper", help="community-paper distribution cabinet")
    psub = paper.add_subparsers(dest="paper_command", required=True)

    def common(p):
        p.add_argument("state")
        p.add_argument("--writer", default=str(DEFAULT_WRITER))
        p.add_argument("--reader", default=str(DEFAULT_READER))

    p_init = psub.add_parser("init", help="create a local cabinet")
    p_init.add_argument("state")
    p_init.add_argument("--mode", choices=sorted(["targeted", "whole_line"]), required=True)
    p_init.add_argument("--slots", type=int, required=True)
    p_init.add_argument("--routes-doc", type=_load_json, default=None)
    p_init.add_argument("--universes-doc", type=_load_json, default=None)
    p_init.add_argument("--writer", default=str(DEFAULT_WRITER))
    p_init.add_argument("--reader", default=str(DEFAULT_READER))
    p_init.set_defaults(func=cmd_init)

    p_contract = psub.add_parser("contract", help="register a guaranteed contract")
    common(p_contract)
    p_contract.add_argument("document")
    p_contract.set_defaults(func=cmd_contract)

    p_station = psub.add_parser("station", help="receive a station delivery file")
    common(p_station)
    p_station.add_argument("document")
    p_station.add_argument("--receipt", default=None)
    p_station.add_argument("--receipt-out", default=None)
    p_station.set_defaults(func=cmd_station)

    p_settle = psub.add_parser("settle", help="settle blocks from published rows")
    common(p_settle)
    p_settle.set_defaults(func=cmd_settle)

    p_account = psub.add_parser("account", help="credit guaranteed quantity")
    common(p_account)
    p_account.add_argument("contract_no")
    p_account.set_defaults(func=cmd_account)

    p_sign = psub.add_parser("sign", help="sign a makeup order")
    common(p_sign)
    p_sign.add_argument("contract_no")
    p_sign.add_argument("--household", default=None)
    p_sign.add_argument("--route", default=None)
    p_sign.set_defaults(func=cmd_sign)

    p_seal = psub.add_parser("seal", help="seal an edition (lock real slots)")
    common(p_seal)
    p_seal.add_argument("edition")
    p_seal.set_defaults(func=cmd_seal)

    p_deliver = psub.add_parser("deliver", help="deliver a sealed order")
    common(p_deliver)
    p_deliver.add_argument("order_id")
    p_deliver.set_defaults(func=cmd_deliver)

    p_withdraw = psub.add_parser("withdraw", help="withdraw an unsealed order")
    common(p_withdraw)
    p_withdraw.add_argument("order_id")
    p_withdraw.set_defaults(func=cmd_withdraw)

    p_read = psub.add_parser("readback", help="read a contract back by number")
    common(p_read)
    p_read.add_argument("contract_no")
    p_read.add_argument("--receipt", default=None)
    p_read.set_defaults(func=cmd_readback)
