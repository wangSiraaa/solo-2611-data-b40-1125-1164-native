"""bitcatalog -- bit-field message catalog and bus observation warehouse.

Two source modules carry the substance:

* :mod:`bitcatalog.catalog` -- definition model, validation (including
  simultaneous-activity / physical-overlap proofs), bit masking, frame
  compilation, payload encoding and compiled-product serialisation.
* :mod:`bitcatalog.warehouse` -- SQLite observation warehouse that binds
  frames to an immutable catalog product and aggregates exact rationals.

:mod:`bitcatalog.reference` is an independent pure-Python bit-list model used
only by tests and cross-checks; it shares no bit arithmetic with the catalog.
"""

from .catalog import (
    CatalogError,
    CatalogProduct,
    CompileError,
    DecodeError,
    EncodedFrame,
    FrameCodec,
    MessageDef,
    SignalDef,
    SelectorDef,
    build_product,
    load_product,
)
from .warehouse import (
    FrameError,
    FrameEvent,
    FrameIdentity,
    IngestStats,
    ObservationWarehouse,
    SignalAggregate,
    StoredFrame,
    StoredProduct,
)
from .lock import (
    LedgerBook,
    LedgerEntry,
    LockError,
    LockState,
    LockTopology,
    PulseObs,
    ValveSpec,
    GateSpec,
    reconcile_pulses,
    side_pool_topology,
    straight_through_topology,
)
from .lock_schedule import (
    DOWNBOUND,
    UPBOUND,
    BeatAction,
    LockSchedule,
    LockScheduler,
    SchedulingError,
    Ship,
)

__all__ = [
    "CatalogError",
    "CatalogProduct",
    "CompileError",
    "DecodeError",
    "EncodedFrame",
    "FrameCodec",
    "MessageDef",
    "SignalDef",
    "SelectorDef",
    "build_product",
    "load_product",
    "FrameError",
    "FrameEvent",
    "FrameIdentity",
    "IngestStats",
    "ObservationWarehouse",
    "SignalAggregate",
    "StoredFrame",
    "StoredProduct",
    # 缩尺船闸
    "LedgerBook",
    "LedgerEntry",
    "LockError",
    "LockState",
    "LockTopology",
    "PulseObs",
    "ValveSpec",
    "GateSpec",
    "reconcile_pulses",
    "side_pool_topology",
    "straight_through_topology",
    "DOWNBOUND",
    "UPBOUND",
    "BeatAction",
    "LockSchedule",
    "LockScheduler",
    "SchedulingError",
    "Ship",
]

__version__ = "0.1.0"
