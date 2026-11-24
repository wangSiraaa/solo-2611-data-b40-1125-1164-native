"""Comic translation-journal proofing on top of the SOLO revision library.

A manuscript is an ASCII registration sheet (stored as a real SOLO body
inside a :class:`~solo.revision.RevisionLibrary`) describing panels, speech
bubbles, speakers, tail endpoints and reply relations.  Editors patch the
manuscript through the ordinary revision library; the layout worker hands a
manuscript version plus a printing permit to :class:`~solo.comics.proof.ProofDesk`
and receives either a paper-proof sheet that really can be read in the
intended order, or a rejection explaining whether the page is blocked at
proofing or at press.
"""

from .manuscript import (
    Actor,
    Bubble,
    Manuscript,
    ManuscriptError,
    ManuscriptIssue,
    Panel,
    Point,
    Say,
    natural_key,
    parse_manuscript,
    reading_order,
    validate_manuscript,
)
from .proof import (
    Guide,
    Move,
    ProofDesk,
    ProofSheet,
    Rejection,
    STAGE_PRESS,
    STAGE_PROOFING,
)
from .rights import MarginSlot, OrderBook, PrintPermit
from .sample import (
    build_canonical_permit,
    canonical_manuscript_text,
    second_page_manuscript_text,
    self_loop_manuscript_text,
    swap_relation,
    version_without_b_print,
)

__all__ = [
    "Actor",
    "Bubble",
    "Manuscript",
    "ManuscriptError",
    "ManuscriptIssue",
    "Panel",
    "Point",
    "Say",
    "Guide",
    "Move",
    "ProofDesk",
    "ProofSheet",
    "Rejection",
    "STAGE_PRESS",
    "STAGE_PROOFING",
    "MarginSlot",
    "OrderBook",
    "PrintPermit",
    "natural_key",
    "parse_manuscript",
    "reading_order",
    "validate_manuscript",
    "build_canonical_permit",
    "canonical_manuscript_text",
    "second_page_manuscript_text",
    "self_loop_manuscript_text",
    "swap_relation",
    "version_without_b_print",
]
