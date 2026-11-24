"""社区小报 (community paper) distribution and guaranteed-quantity settlement.

Three deliberately separated pieces, mirroring the base toolkit's split
between planning, adaptation and stateful ingestion:

* :mod:`extcontract.paper.settle` — pure constraint engine over the station's
  partial delivery evidence (lower bound, re-checkable witness, per-line
  added lower bound);
* :mod:`extcontract.paper.cabinet` — the 发行柜: a local, stateful counter the
  发行员 (distributor) can feed new contracts and station files into, which
  runs the original :class:`~extcontract.IngestionLibrary` underneath and
  books/补投s against the published valid rows;
* :mod:`extcontract.paper.cli` — local entry point with on-disk state.
"""

from .settle import (
    INTERSECTION,
    NAMED_DELIVERED,
    NAMED_MISSED,
    SET_SIZE,
    BlockSettlement,
    LineEvaluation,
    Observation,
    lower_bound_with_extra,
    parse_observations,
    settle,
    settle_block,
    verify_arrangement,
)
from .cabinet import (
    AdContract,
    CabinetResult,
    MakeupOrder,
    PaperCabinet,
    Route,
)

__all__ = [
    "INTERSECTION",
    "NAMED_DELIVERED",
    "NAMED_MISSED",
    "SET_SIZE",
    "BlockSettlement",
    "LineEvaluation",
    "Observation",
    "lower_bound_with_extra",
    "parse_observations",
    "settle",
    "settle_block",
    "verify_arrangement",
    "AdContract",
    "CabinetResult",
    "MakeupOrder",
    "PaperCabinet",
    "Route",
]
