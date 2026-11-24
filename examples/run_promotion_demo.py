"""End-to-end promotion scenarios against the real revision-aware feed stores.

Run with::

    PYTHONPATH=. python3 examples/run_promotion_demo.py

It walks the five histories from the brief and prints, for each one, the
per-application answers, the seal/lock outcome, and the retained credential
versions with their revocation reasons.  Every station report is ingested
through the same ``IngestionLibrary`` machinery as the 745 workflow: ruling
corrections are higher-revision segment rows, and only published effective
rows feed the eligibility rule.
"""

from __future__ import annotations

import json

from extcontract.promotion.scenarios import (
    run_early_lock_late_withdrawal,
    run_early_lock_tradeoff,
    run_late_seal_baseline,
    run_late_seal_post_expiry_replay,
    run_late_seal_preseal_replay,
    run_two_person_early_lock,
)


def heading(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def show_board(result: dict) -> None:
    board = result["board"]
    print("-- effective segments (published rows) --")
    for segment in result["segments"]:
        mark = "valid" if segment["valid"] else "void "
        print(
            f"  [{segment['station']}] {segment['match']} #{segment['segment_index']} "
            f"{segment['jumper_a']}+{segment['jumper_b']} jumps={segment['jumps']} "
            f"{mark} rev={segment['revision']} ({segment['segment_id']})"
        )
    print("-- eligibility verdicts --")
    for label, qualification in result["qualifications"].items():
        stations = ", ".join(
            f"{station}:{best['jumps']} via {best['match']}"
            for station, best in qualification["stations"].items()
        )
        verdict = "QUALIFIED" if qualification["qualified"] else "not eligible"
        print(f"  {label}: {verdict} ({stations})")
    print("-- application answers --")
    for entry in result["answers"]:
        action = entry["action"]
        if "receipt" in entry:
            receipt = entry["receipt"]
            if action["kind"] == "report":
                print(f"  report {receipt['file_id']}: {receipt['status']}")
                continue
            seat = "FORMAL SEAT" if receipt["formal_seat"] else "no seat"
            reason = f" [{receipt['reason']}]" if receipt["reason"] else ""
            print(
                f"  day {action['at']} {action['kind']} "
                f"{receipt['label']} -> {receipt['status']} ({seat}){reason}"
            )
        elif "result" in entry:
            print(f"  day {action.get('at')} {action['kind']}: "
                  f"{json.dumps(entry['result'], ensure_ascii=False, sort_keys=True)}")
    print("-- credential history (versions retained) --")
    for credential in board.credential_history():
        line = (
            f"  {credential['credential_id']} v{credential['version']} "
            f"{credential['label']} seat#{credential['seat_index']} {credential['status']}"
        )
        if credential["status"] == "REVOKED":
            line += f" at day {credential['revoked_at']} reason={credential['revoke_reason']}"
        print(line)
    print(f"-- seats used/remaining: {board.seats_used()}/{board.seats_remaining()} --")


def main() -> None:
    print("OPERATED MECHANISM: LATE_SEAL (EARLY_LOCK shown as the alternative policy)")

    heading("1. Two athletes, one seat (EARLY_LOCK): promote -> rule -> restore -> withdraw")
    show_board(run_two_person_early_lock())

    heading("2. Four athletes, two seats (LATE_SEAL): day-3 seal, maximum team count")
    show_board(run_late_seal_baseline())

    heading("3a. Replay from the same pre-seal record: correction arrives BEFORE the seal")
    show_board(run_late_seal_preseal_replay())

    heading("3b. Replay from the same pre-seal record: correction arrives AFTER expiry")
    show_board(run_late_seal_post_expiry_replay())

    heading("4. EARLY_LOCK tradeoff: an on-the-spot lock versus a later partner")
    show_board(run_early_lock_tradeoff())

    heading("5. EARLY_LOCK late-withdrawal trap: the released seat has no valid partner")
    show_board(run_early_lock_late_withdrawal())

    print()
    print("Done. Every issued/revoked credential and every receipt stays queryable;")
    print("released seats are only re-offered to intentions valid at that moment.")


if __name__ == "__main__":
    main()
