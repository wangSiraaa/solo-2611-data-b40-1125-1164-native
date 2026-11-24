"""社区废纸返券柜台公共 API。

    from rebate import Counter, CollectionNetwork, PartialWeigh, MODE_STATION

三层互相独立、各自可测：

* :mod:`rebate.network`：纯收集网络 + 整数约束求解（不落盘）；
* :mod:`rebate.counter`：SQLite 事实仓，叠加核算期、预算、领券权、回执；
* :mod:`rebate.warehouse_adapter`：消费过磅事实仓的真实确认车次与净重版本。
"""

from .counter import (
    CENTER_STATION,
    Counter,
    CounterError,
    Decision,
    MAX_HOUSEHOLDS,
    MAX_STATIONS,
    MODE_STATION,
    MODE_UNIFIED,
    REASON_BELOW_LINE,
    REASON_BUDGET_EXHAUSTED,
    REASON_CLAIM_DEADLINE_PASSED,
    REASON_DUPLICATE_CLAIM,
    REASON_ISSUED_OK,
    REASON_NETWORK_INFEASIBLE,
    REASON_NO_RIGHT_AT_STATION,
    REASON_PERIOD_CLOSED,
    REASON_POST_CLOSURE_DELTA,
    REASON_RIGHT_IN_TRANSIT,
    REASON_WEIGHT_UNPROVEN,
    RESULT_DISPUTE,
    RESULT_ISSUED,
    RESULT_REJECTED,
    RESULT_SUSPENDED,
    RIGHT_HANDED_OVER,
    RIGHT_HELD,
    RIGHT_RECEIVED,
    RIGHT_SPENT,
)
from .network import (
    Bag,
    Box,
    CollectionNetwork,
    FeasibleWorld,
    NODE_BAG,
    NODE_BOX,
    NODE_HOUSEHOLD,
    NODE_TRIP,
    ORIGIN_CENTER,
    ORIGIN_STATION,
    PartialWeigh,
    SolveResult,
    TripVersion,
    solve,
    view_for_station,
)
from .warehouse_adapter import import_confirmed_trips, net_version_chain

__all__ = [
    # counter
    "Counter", "CounterError", "Decision", "MAX_HOUSEHOLDS", "MAX_STATIONS",
    "MODE_STATION", "MODE_UNIFIED", "CENTER_STATION",
    "RIGHT_HELD", "RIGHT_HANDED_OVER", "RIGHT_RECEIVED", "RIGHT_SPENT",
    "RESULT_ISSUED", "RESULT_SUSPENDED", "RESULT_REJECTED", "RESULT_DISPUTE",
    "REASON_ISSUED_OK", "REASON_WEIGHT_UNPROVEN", "REASON_BELOW_LINE",
    "REASON_BUDGET_EXHAUSTED", "REASON_CLAIM_DEADLINE_PASSED",
    "REASON_PERIOD_CLOSED", "REASON_NO_RIGHT_AT_STATION",
    "REASON_RIGHT_IN_TRANSIT", "REASON_NETWORK_INFEASIBLE",
    "REASON_DUPLICATE_CLAIM", "REASON_POST_CLOSURE_DELTA",
    # network
    "Bag", "Box", "TripVersion", "PartialWeigh", "FeasibleWorld",
    "SolveResult", "CollectionNetwork", "solve", "view_for_station",
    "NODE_BAG", "NODE_BOX", "NODE_TRIP", "NODE_HOUSEHOLD",
    "ORIGIN_CENTER", "ORIGIN_STATION",
    # warehouse adapter
    "import_confirmed_trips", "net_version_chain",
]
