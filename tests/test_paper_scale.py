"""Scale envelope for the settlement engine: 8 households, 4 lines, local.

The engine enumerates candidate delivered subsets per edition (2**8 = 256 at
most) and backtracks over editions.  This test pins the documented envelope as
fast and deterministic on ordinary local hardware.
"""

from __future__ import annotations

import time

from extcontract import Observation, settle_block
from extcontract.paper.settle import INTERSECTION, SET_SIZE


def test_max_envelope_settles_locally_and_quickly():
    universe = [f"h{i}" for i in range(8)]
    lines = {
        "L1": universe[0:4],
        "L2": universe[2:6],
        "L3": universe[4:8],
        "L4": [universe[0], universe[7]],
    }
    observations = [
        Observation("B", SET_SIZE, 1, edition="e1", qty=5),
        Observation("B", SET_SIZE, 1, edition="e2", qty=5),
        Observation("B", SET_SIZE, 1, edition="e3", qty=3),
        Observation("B", INTERSECTION, 1, edition="e1", edition_b="e2", qty=3),
        Observation("B", INTERSECTION, 1, edition="e2", edition_b="e3", qty=2),
    ]
    start = time.perf_counter()
    verdict = settle_block("B", universe, observations, lines)
    elapsed = time.perf_counter() - start

    assert verdict.consistent
    assert verdict.history_count > 0
    assert set(verdict.line_evaluations) == set(lines)
    # Every bound is attainable and within the roster.
    for line_id, evaluation in verdict.line_evaluations.items():
        assert 0 <= evaluation.added_lower_bound <= len(lines[line_id])
        union = frozenset().union(
            *(frozenset(s) for s in evaluation.witness.values())
        )
        assert len(frozenset(lines[line_id]) - union) == evaluation.added_lower_bound
    # Local, no I/O: comfortably under a second for the whole envelope.
    assert elapsed < 1.0
