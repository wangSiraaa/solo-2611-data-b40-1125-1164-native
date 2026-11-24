"""Double-sided embroidery screen repair domain.

This sub-package plugs the embroidery-screen metaphor into the existing
``extcontract`` toolkit:

  * :mod:`extcontract.embroidery.contract` - the read/write contracts for the
    needle-road registry and helpers that build ingestion documents;
  * :mod:`extcontract.embroidery.records` - typed registry rows parsed from the
    *published, effective* rows of an :class:`~extcontract.IngestionLibrary`;
  * :mod:`extcontract.embroidery.reconstruction` - recovery of every threading
    world that simultaneously satisfies the needle roads and the side
    observations, including the injective observation-claim rule;
  * :mod:`extcontract.embroidery.orders` - counter orders, single-mode workshop
    work orders, feasibility quotes, delivery notes, and original-stitch
    rechecks.

Business rejections (target not found, ambiguity, protected stitches, missing
loops, blocker stitches) are always returned as structured result objects;
exceptions are reserved for malformed registry documents.
"""

from .contract import (
    EMBROIDERY_READER_VERSION,
    EMBROIDERY_WRITER_VERSION,
    REGISTRY_FIELDS,
    embroidery_contract,
    noknot_row,
    observation_row,
    protection_row,
    registry_document,
    thread_row,
)
from .orders import (
    ACCEPTED,
    BLOCKED,
    CUT,
    DELIVERED,
    HELD,
    UNTHREAD,
    CounterOrder,
    DeliveryNote,
    IntakeDecision,
    Panel,
    Quote,
    WorkOrder,
    deliver,
    evaluate,
    intake_order,
    recheck_originals,
)
from .reconstruction import (
    AMBIGUOUS,
    CERTAIN,
    NOT_FOUND,
    UNSATISFIABLE,
    Reconstruction,
    TargetResolution,
    World,
    reconstruct,
    resolve_target,
    segment_key,
)
from .records import (
    NoKnotZone,
    Observation,
    Protection,
    Registry,
    RegistryError,
    Segment,
    ThreadRecord,
    registry_from_publish,
)

__all__ = [
    # contract / ingestion documents
    "EMBROIDERY_READER_VERSION",
    "EMBROIDERY_WRITER_VERSION",
    "REGISTRY_FIELDS",
    "embroidery_contract",
    "noknot_row",
    "observation_row",
    "protection_row",
    "registry_document",
    "thread_row",
    # registry rows
    "NoKnotZone",
    "Observation",
    "Protection",
    "Registry",
    "RegistryError",
    "Segment",
    "ThreadRecord",
    "registry_from_publish",
    # reconstruction
    "AMBIGUOUS",
    "CERTAIN",
    "NOT_FOUND",
    "UNSATISFIABLE",
    "Reconstruction",
    "TargetResolution",
    "World",
    "reconstruct",
    "resolve_target",
    "segment_key",
    # orders / workshops / panel
    "ACCEPTED",
    "BLOCKED",
    "CUT",
    "DELIVERED",
    "HELD",
    "UNTHREAD",
    "CounterOrder",
    "DeliveryNote",
    "IntakeDecision",
    "Panel",
    "Quote",
    "WorkOrder",
    "deliver",
    "evaluate",
    "intake_order",
    "recheck_originals",
]
