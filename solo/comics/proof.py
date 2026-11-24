"""The layout worker's proofing desk: one entry, proof sheet or rejection.

::

    desk = ProofDesk(library)
    result = desk.proof(version_id, permit, order_book)

The desk decodes the manuscript version through the SOLO codec, parses and
validates the registration sheet, plans the licensed craft against the
committed order book, and returns either a :class:`ProofSheet` -- paper
that really can be read in the intended order, rendered as ASCII lines --
or a :class:`Rejection` whose ``stage`` says whether the page is blocked at
proofing (the sheet itself is unsound) or at press (the sheet is sound but
the printing permit/rights do not allow this impression).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..codec import DecodeError
from ..revision import RevisionLibrary
from .manuscript import (
    Manuscript,
    ManuscriptError,
    ManuscriptPage,
    inversions,
    natural_key,
    parse_manuscript,
    reading_order,
    validate_manuscript,
)
from .rights import (
    CRAFT_MARGIN,
    CRAFT_RELAYOUT,
    OrderBook,
    PrintPermit,
)

__all__ = [
    "STAGE_PROOFING",
    "STAGE_PRESS",
    "Guide",
    "Move",
    "ProofSheet",
    "Rejection",
    "ProofDesk",
]

STAGE_PROOFING = "proofing"
STAGE_PRESS = "press"


@dataclass(frozen=True)
class Guide:
    """A printed margin mark: ``label`` in ``slot_id`` points at ``bubble_id``."""

    slot_id: str
    side: str
    label: str
    bubble_id: str
    reaches: frozenset[str]


@dataclass(frozen=True)
class Move:
    """One bubble relocated by the relayout craft, tail reconnected."""

    bubble_id: str
    from_panel: str
    to_panel: str
    old_box: tuple[int, int, int, int]
    new_box: tuple[int, int, int, int]
    old_tail: tuple[int, int] | None
    new_tail: tuple[int, int]
    speaker: str


@dataclass(frozen=True)
class Rejection:
    stage: str
    code: str
    reason: str
    page_id: str | None = None
    bubble_id: str | None = None
    line_no: int | None = None
    char_span: tuple[int, int] | None = None
    byte_span: tuple[int, int] | None = None
    blocked_by_page: str | None = None

    def summary(self) -> str:
        head = f"{self.stage.upper()} REJECT [{self.code}]"
        loc = []
        if self.page_id:
            loc.append(f"page {self.page_id}")
        if self.bubble_id:
            loc.append(f"bubble {self.bubble_id}")
        if self.line_no is not None:
            loc.append(f"line {self.line_no}")
        if self.byte_span is not None:
            loc.append(f"body bytes {self.byte_span[0]}..{self.byte_span[1]}")
        suffix = f" ({'; '.join(loc)})" if loc else ""
        extra = f" -- committed to page {self.blocked_by_page}" if self.blocked_by_page else ""
        return f"{head}{suffix}: {self.reason}{extra}"


@dataclass
class ProofSheet:
    order_id: str
    version_id: int
    page_id: str
    craft: str
    reading_order: tuple[str, ...]
    paper_order: tuple[str, ...]
    guides: tuple[Guide, ...] = ()
    moves: tuple[Move, ...] = ()
    speakers: dict[str, str] = field(default_factory=dict)
    lines: tuple[str, ...] = ()
    body_sha256: str = ""

    def render(self) -> str:
        return "\n".join(self.lines)

    def readback(self) -> tuple[str, ...]:
        """The order a reader actually follows on the printed sheet."""
        return tuple(self.paper_order)


# ---------------------------------------------------------------------------
# Paper rendering (ASCII, deliberately printable)
# ---------------------------------------------------------------------------


def _render_sheet(
    *,
    order_id: str,
    version_id: int,
    page: ManuscriptPage,
    craft: str,
    order_seq: tuple[str, ...],
    guides: tuple[Guide, ...],
    moves: tuple[Move, ...],
    moved_boxes: dict[str, tuple[int, int, int, int]],
    moved_tails: dict[str, tuple[int, int]],
    speakers: dict[str, str],
    says: dict[str, str],
    sha: str,
) -> tuple[str, ...]:
    width = 78
    rule = "=" * width
    thin = "-" * width
    out: list[str] = [
        rule,
        f"PROOF SHEET  order={order_id} version=v{version_id} page={page.page_id} craft={craft}",
        f"intended reading order: {' '.join(order_seq)}",
        thin,
    ]

    if craft == CRAFT_MARGIN:
        out.append("MARGIN GUIDES (artwork and bubble positions unchanged):")
        if guides:
            for guide in guides:
                out.append(
                    f"  [{guide.side:6s} slot {guide.slot_id}] mark {guide.label} -> bubble {guide.bubble_id}"
                )
        else:
            out.append("  (none needed; natural paper order already matches)")
        out.append(thin)

    out.append("PAPER (read each row left to right, rows top to bottom):")
    panels_sorted = sorted(page.panels.values(), key=lambda p: (p.box[1], p.box[0]))

    def effective_box(bubble_id: str) -> tuple[int, int, int, int]:
        return moved_boxes.get(bubble_id, page.bubbles[bubble_id].box)

    # The visual row order uses each bubble's effective box, so a relocated
    # bubble really appears where the reader will meet it.
    def visual_key(bubble_id: str) -> tuple[int, int, str]:
        x0, y0, _x1, _y1 = effective_box(bubble_id)
        return (y0, x0, bubble_id)

    if craft == CRAFT_RELAYOUT and moved_boxes:
        # Visual impression after redraw: bubbles appear in their new boxes,
        # so a straight row-major scan of this list is the paper order.
        out.append("  VISUAL LAYOUT AFTER REDRAW (row-major order is the reading order):")
        for bubble_id in sorted(page.bubbles, key=visual_key):
            bubble = page.bubbles[bubble_id]
            box = effective_box(bubble_id)
            tail = moved_tails.get(bubble_id, tuple(bubble.tail) if bubble.tail else None)
            mark = "OFFPAGE" if bubble.offpage else f"speaker={speakers.get(bubble_id, '?')}"
            origin = f" home-panel={bubble.panel_id}" if bubble_id in moved_boxes else ""
            out.append(f"  [{bubble_id}] box={box} {mark}{origin}")
            out.append(f"      SAY: {says.get(bubble_id, '')}  TAIL -> {tail}")
        out.append(thin)

    for panel in panels_sorted:
        x0, y0, x1, y1 = panel.box
        out.append(f"  +-- PANEL {panel.panel_id} ({x0},{y0})-({x1},{y1}) " + "-" * 20)
        members = [b for b in page.bubbles.values() if b.panel_id == panel.panel_id]
        members.sort(key=lambda b: visual_key(b.bubble_id))
        for bubble in members:
            box = moved_boxes.get(bubble.bubble_id, bubble.box)
            tail = moved_tails.get(bubble.bubble_id, tuple(bubble.tail) if bubble.tail else None)
            mark = "OFFPAGE" if bubble.offpage else f"speaker={speakers.get(bubble.bubble_id, '?')}"
            moved = " RELOCATED" if bubble.bubble_id in moved_boxes else ""
            out.append(f"  | BUBBLE {bubble.bubble_id} box={box} {mark}{moved}")
            out.append(f"  |   SAY: {says.get(bubble.bubble_id, '')}")
            out.append(f"  |   TAIL -> {tail}")
        out.append("  +" + "-" * (width - 4))

    if craft == CRAFT_RELAYOUT and moves:
        out.append(thin)
        out.append("REDRAW MOVES (tails reconnected to the original speakers):")
        for move in moves:
            out.append(
                f"  bubble {move.bubble_id}: panel {move.from_panel} -> {move.to_panel} "
                f"box {move.old_box} -> {move.new_box}; tail -> {move.new_tail} ({move.speaker})"
            )

    out.append(thin)
    out.append(f"COLOPHON readback={' '.join(order_seq)} sha256={sha[:12]}")
    out.append(rule)
    return tuple(out)


# ---------------------------------------------------------------------------
# Desk
# ---------------------------------------------------------------------------


class ProofDesk:
    """Stateless planner over a :class:`RevisionLibrary` plus an order book."""

    def __init__(self, library: RevisionLibrary) -> None:
        self._library = library

    # -- entry point ------------------------------------------------------

    def proof(
        self,
        version_id: int,
        permit: PrintPermit,
        order_book: OrderBook,
    ) -> ProofSheet | Rejection:
        version = self._library.get(version_id)
        try:
            doc = version.decode()
        except DecodeError as exc:
            return Rejection(
                STAGE_PROOFING,
                "UNDECODABLE",
                str(exc),
                page_id=permit.page_id,
                byte_span=(exc.offset, exc.offset + 1),
            )

        try:
            manuscript = parse_manuscript(doc.text)
        except ManuscriptError as exc:
            return Rejection(
                STAGE_PROOFING,
                "UNPARSEABLE",
                exc.reason,
                page_id=permit.page_id,
                line_no=exc.line_no,
                char_span=exc.line_span,
                byte_span=manuscript_byte_span(doc, exc.line_span),
            )

        page = manuscript.pages.get(permit.page_id)
        if page is None:
            return Rejection(
                STAGE_PROOFING,
                "UNKNOWN_PAGE",
                f"permit names page {permit.page_id} but the manuscript has {sorted(manuscript.pages)}",
                page_id=permit.page_id,
            )

        issues = validate_manuscript(manuscript)
        page_issues = [i for i in issues if _issue_is_on_page(manuscript, i, permit.page_id)]
        if page_issues:
            issue = page_issues[0]
            return Rejection(
                STAGE_PROOFING,
                issue.code,
                issue.reason,
                page_id=permit.page_id,
                bubble_id=issue.bubble_id,
                line_no=issue.line_no,
                char_span=issue.line_span,
                byte_span=manuscript_byte_span(doc, issue.line_span),
            )

        try:
            order = reading_order(page)
        except ValueError:
            return Rejection(
                STAGE_PROOFING,
                "REPLY_CYCLE",
                "reply relations form a cycle; no reading order exists",
                page_id=permit.page_id,
            )

        if permit.craft == CRAFT_MARGIN:
            return self._proof_margin(version, doc, manuscript, page, order, permit, order_book)
        return self._proof_relayout(version, doc, manuscript, page, order, permit, order_book)

    def _dialogue_rejection(self, doc, manuscript, page, order, permit) -> Rejection | None:
        missing = [b for b in order if not permit.can_print(b)]
        if not missing:
            return None
        bubble = page.bubbles[missing[0]]
        return Rejection(
            STAGE_PRESS,
            "DIALOGUE_NOT_LICENSED",
            f"dialogue print right for bubble {missing[0]} has been withdrawn",
            page_id=permit.page_id,
            bubble_id=missing[0],
            line_no=_line_of(manuscript, bubble.line_span),
            char_span=bubble.line_span,
            byte_span=manuscript_byte_span(doc, bubble.line_span),
        )

    # -- margin craft -----------------------------------------------------

    def _proof_margin(
        self,
        version,
        doc,
        manuscript: Manuscript,
        page: ManuscriptPage,
        order: list[str],
        permit: PrintPermit,
        order_book: OrderBook,
    ) -> ProofSheet | Rejection:
        # Dialogue print rights are a press matter: the sheet itself is sound.
        rejection = self._dialogue_rejection(doc, manuscript, page, order, permit)
        if rejection:
            return rejection

        # An inversion is a connected group of bubbles whose reply order
        # contradicts the paper order.  The group is guided by the one narrow
        # strip whose reach is exactly that group; that strip is the resource
        # the volume may pre-sell to another page.
        inverted = {b for edge in inversions(page, order) for b in edge}
        guides: list[Guide] = []
        if inverted:
            rank = {bubble_id: i + 1 for i, bubble_id in enumerate(order)}
            groups = _inversion_groups(inversions(page, order), inverted)
            for group in groups:
                group_set = frozenset(group)
                # A strip qualifies when it reaches every bubble of the
                # group.  A strip whose reach is exactly the group is the
                # "narrow strip" and is preferred over any broader one.
                candidates = [
                    slot for slot in permit.margin_slots if group_set <= slot.reaches
                ]
                candidates.sort(
                    key=lambda s: (s.reaches != group_set, len(s.reaches), s.slot_id)
                )
                if not candidates:
                    first = sorted(group, key=lambda b: rank[b])[0]
                    return Rejection(
                        STAGE_PRESS,
                        "NO_MARGIN_SLOT",
                        f"no permitted margin strip reaches the whole group "
                        f"{sorted(group)}; the natural paper order would misread it",
                        page_id=permit.page_id,
                        bubble_id=first,
                    )
                chosen = next(
                    (slot for slot in candidates if order_book.strip_is_available(slot, permit.page_id)),
                    None,
                )
                if chosen is None:
                    first = sorted(group, key=lambda b: rank[b])[0]
                    buyer = order_book.strip_buyer(candidates[0].slot_id)
                    return Rejection(
                        STAGE_PRESS,
                        "MARGIN_STRIP_SOLD",
                        f"the only strip reaching group {sorted(group)} is already sold "
                        "to another page of this volume",
                        page_id=permit.page_id,
                        bubble_id=first,
                        blocked_by_page=buyer,
                    )
                for bubble_id in sorted(group, key=lambda b: rank[b]):
                    guides.append(
                        Guide(
                            slot_id=chosen.slot_id,
                            side=chosen.side,
                            label=str(rank[bubble_id]),
                            bubble_id=bubble_id,
                            reaches=chosen.reaches,
                        )
                    )

        # On paper the reader follows the guide labels first, then every
        # unguided bubble in natural row-major order.
        guided_ids = {g.bubble_id for g in guides}
        guided_seq = tuple(
            sorted(
                guided_ids,
                key=lambda b: int(next(g.label for g in guides if g.bubble_id == b)),
            )
        )
        natural_seq = tuple(
            b for b in sorted(page.bubbles, key=lambda b: natural_key(page, b)) if b not in guided_ids
        )
        paper_order = guided_seq + natural_seq

        speakers = _tail_speakers(page)
        says = {b: s.text for b, s in page.says.items()}
        lines = _render_sheet(
            order_id=order_book.order_id,
            version_id=version.version_id,
            page=page,
            craft=CRAFT_MARGIN,
            order_seq=tuple(order),
            guides=tuple(guides),
            moves=(),
            moved_boxes={},
            moved_tails={},
            speakers=speakers,
            says=says,
            sha=version.body_sha256,
        )
        sheet = ProofSheet(
            order_id=order_book.order_id,
            version_id=version.version_id,
            page_id=permit.page_id,
            craft=CRAFT_MARGIN,
            reading_order=tuple(order),
            paper_order=paper_order,
            guides=tuple(guides),
            speakers=speakers,
            lines=lines,
            body_sha256=version.body_sha256,
        )
        order_book.mark_proofed(permit.page_id)
        return sheet

    # -- relayout craft ---------------------------------------------------

    def _proof_relayout(
        self,
        version,
        doc,
        manuscript: Manuscript,
        page: ManuscriptPage,
        order: list[str],
        permit: PrintPermit,
        order_book: OrderBook,
    ) -> ProofSheet | Rejection:
        rejection = self._dialogue_rejection(doc, manuscript, page, order, permit)
        if rejection:
            return rejection

        natural = sorted(page.bubbles, key=lambda b: natural_key(page, b))

        # Required permutation: the bubble that must be read at rank k takes
        # the paper slot naturally occupied by natural[k].  Off-page
        # narrators stay last in both orders, so only on-page bubbles move.
        assignment: dict[str, str] = {}
        for rank, bubble_id in enumerate(order):
            target_id = natural[rank]
            if target_id != bubble_id and not page.bubbles[bubble_id].offpage:
                assignment[bubble_id] = target_id

        # Resolve the permutation into cycles; every bubble in a cycle needs
        # redraw clearance both where it leaves and where it lands.
        visited: set[str] = set()
        cycles: list[list[str]] = []
        for bubble_id in assignment:
            if bubble_id in visited:
                continue
            cycle: list[str] = []
            cur = bubble_id
            while cur not in visited:
                visited.add(cur)
                cycle.append(cur)
                cur = assignment[cur]
            cycles.append(cycle)

        def clearance_rejection(bubble_id: str) -> Rejection | None:
            bubble = page.bubbles[bubble_id]
            if order_book.is_sealed(permit.page_id, bubble.panel_id):
                return Rejection(
                    STAGE_PRESS,
                    "PANEL_SEALED",
                    f"panel {bubble.panel_id} holding bubble {bubble_id} is painter-signed; "
                    "its artwork is frozen in place and cannot be redrawn",
                    page_id=permit.page_id,
                    bubble_id=bubble_id,
                    blocked_by_page=permit.page_id,
                )
            if bubble.panel_id not in permit.redraw_panels:
                return Rejection(
                    STAGE_PRESS,
                    "REDRAW_NOT_LICENSED",
                    f"panel {bubble.panel_id} is outside the redraw region but bubble "
                    f"{bubble_id} must move to restore natural reading order",
                    page_id=permit.page_id,
                    bubble_id=bubble_id,
                )
            return None

        for cycle in cycles:
            for bubble_id in cycle:
                blocked = clearance_rejection(bubble_id)
                if blocked:
                    return blocked
                # The slot this bubble lands on belongs to another cycle
                # member, whose panel must itself be clear.
                target_id = assignment[bubble_id]
                target_panel = page.bubbles[target_id].panel_id
                if order_book.is_sealed(permit.page_id, target_panel):
                    return Rejection(
                        STAGE_PRESS,
                        "PANEL_SEALED",
                        f"the target slot for bubble {bubble_id} sits in painter-signed "
                        f"panel {target_panel}",
                        page_id=permit.page_id,
                        bubble_id=bubble_id,
                        blocked_by_page=permit.page_id,
                    )
                if target_panel not in permit.redraw_panels:
                    return Rejection(
                        STAGE_PRESS,
                        "REDRAW_NOT_LICENSED",
                        f"target panel {target_panel} is outside the redraw region",
                        page_id=permit.page_id,
                        bubble_id=bubble_id,
                    )

        speakers = _tail_speakers(page)
        moves: list[Move] = []
        moved_boxes: dict[str, tuple[int, int, int, int]] = {}
        moved_tails: dict[str, tuple[int, int]] = {}
        touched_panels: set[str] = set()

        for cycle in cycles:
            # Each bubble takes the box of the next bubble in its cycle;
            # its tail is reconnected to its own speaker's anchor.
            for i, bubble_id in enumerate(cycle):
                bubble = page.bubbles[bubble_id]
                target_id = cycle[(i + 1) % len(cycle)]
                target_bubble = page.bubbles[target_id]
                speaker_name = speakers.get(bubble_id)
                anchor = _speaker_anchor(page, speaker_name)
                if anchor is None:
                    return Rejection(
                        STAGE_PROOFING,
                        "TAIL_MISWIRED",
                        f"bubble {bubble_id} cannot be reconnected: speaker "
                        f"{speaker_name} has no anchor",
                        page_id=permit.page_id,
                        bubble_id=bubble_id,
                    )
                moves.append(
                    Move(
                        bubble_id=bubble_id,
                        from_panel=bubble.panel_id,
                        to_panel=target_bubble.panel_id,
                        old_box=bubble.box,
                        new_box=target_bubble.box,
                        old_tail=tuple(bubble.tail) if bubble.tail else None,
                        new_tail=tuple(anchor),
                        speaker=speaker_name,
                    )
                )
                moved_boxes[bubble_id] = target_bubble.box
                moved_tails[bubble_id] = tuple(anchor)
                touched_panels.add(bubble.panel_id)
                touched_panels.add(target_bubble.panel_id)

        says = {b: s.text for b, s in page.says.items()}
        lines = _render_sheet(
            order_id=order_book.order_id,
            version_id=version.version_id,
            page=page,
            craft=CRAFT_RELAYOUT,
            order_seq=tuple(order),
            guides=(),
            moves=tuple(moves),
            moved_boxes=moved_boxes,
            moved_tails=moved_tails,
            speakers=speakers,
            says=says,
            sha=version.body_sha256,
        )
        sheet = ProofSheet(
            order_id=order_book.order_id,
            version_id=version.version_id,
            page_id=permit.page_id,
            craft=CRAFT_RELAYOUT,
            reading_order=tuple(order),
            paper_order=tuple(order),
            moves=tuple(moves),
            speakers=speakers,
            lines=lines,
            body_sha256=version.body_sha256,
        )
        for panel_id in touched_panels:
            order_book.consume_redraw(permit.page_id, panel_id)
        order_book.mark_proofed(permit.page_id)
        return sheet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def manuscript_byte_span(doc, char_span: tuple[int, int]) -> tuple[int, int]:
    start, end = char_span
    n = len(doc.text)
    if n == 0:
        return (0, 0)
    start = max(0, min(start, n - 1))
    b0 = doc.byte_range(start)[0]
    if end <= start:
        return b0, b0
    end = min(end, n)
    b1 = doc.byte_range(end - 1)[1]
    return b0, b1


def _issue_is_on_page(manuscript: Manuscript, issue, page_id: str) -> bool:
    page = manuscript.pages[page_id]
    if issue.line_no in page.line_nos:
        return True
    return bool(issue.bubble_id and issue.bubble_id in page.bubbles)


def _line_of(manuscript: Manuscript, span: tuple[int, int]) -> int:
    for i, start in enumerate(manuscript.line_starts):
        if start > span[0]:
            return i + 1
    return len(manuscript.line_starts)


def _tail_speakers(page: ManuscriptPage) -> dict[str, str]:
    out: dict[str, str] = {}
    for bubble_id, bubble in page.bubbles.items():
        if bubble.tail is None:
            continue
        for actor in page.actors.values():
            if actor.at == bubble.tail:
                out[bubble_id] = actor.name
                break
    return out


def _speaker_anchor(page: ManuscriptPage, speaker_name: str | None):
    if speaker_name is None:
        return None
    actor = page.actors.get(speaker_name)
    return actor.at if actor else None


def _inversion_groups(edges, inverted: set[str]) -> list[list[str]]:
    """Connected components of the inversion reply graph (union-find)."""
    parent = {b: b for b in inverted}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for earlier, later in edges:
        if earlier in inverted and later in inverted:
            union(earlier, later)

    groups: dict[str, list[str]] = {}
    for bubble_id in sorted(inverted):
        groups.setdefault(find(bubble_id), []).append(bubble_id)
    return list(groups.values())
