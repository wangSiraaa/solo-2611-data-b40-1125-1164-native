"""ASCII registration sheets for comic translation-journal manuscripts.

Registration grammar (one directive per ASCII line; blank lines and lines
starting with ``#`` are ignored).  A manuscript holds at most two pages and
eight bubbles; bubble text may contain PAIR-mode characters because the
sheet itself is stored as a SOLO body.

::

    PAGE <id>
    PANEL <id> x0 y0 x1 y1
    ACTOR <name> @ <x> <y> [PANEL <id>]      # a speaker anchor point
    BUBBLE <id> PANEL <id> x0 y0 x1 y1 [OFFPAGE]
    SAY <id> <text...>                        # the bubble's actual line
    TAIL <id> -> <x> <y>                      # where its tail ends on paper
    REPLY <id> < > <id>                       # the right bubble answers the left one

Relation semantics: ``A>B`` means B answers A, so A must be read first;
``A<B`` means A answers B, so B must be read first.  The natural reading is
left-to-right within each row and rows top-to-bottom; reply edges and
off-page narration constrain that order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

__all__ = [
    "MAX_PAGES",
    "MAX_BUBBLES",
    "Point",
    "Actor",
    "Panel",
    "Say",
    "Bubble",
    "ManuscriptPage",
    "Manuscript",
    "ManuscriptError",
    "ManuscriptIssue",
    "parse_manuscript",
    "validate_manuscript",
    "natural_key",
    "reading_order",
    "inversions",
]

MAX_PAGES = 2
MAX_BUBBLES = 8

_BUBBLE_ID = re.compile(r"[A-Z][A-Z0-9]*")
_RELATION = re.compile(r"^REPLY\s+([A-Z][A-Z0-9]*)\s*([<>])\s*([A-Z][A-Z0-9]*)\s*$")
_INT4 = r"(-?\d+)"
_PANEL = re.compile(rf"^PANEL\s+(\S+)\s+{_INT4}\s+{_INT4}\s+{_INT4}\s+{_INT4}\s*$")
_ACTOR = re.compile(rf"^ACTOR\s+(\S+)\s+@\s+{_INT4}\s+{_INT4}(?:\s+PANEL\s+(\S+))?\s*$")
_BUBBLE = re.compile(
    rf"^BUBBLE\s+([A-Z][A-Z0-9]*)\s+PANEL\s+(\S+)\s+{_INT4}\s+{_INT4}\s+{_INT4}\s+{_INT4}(?:\s+(OFFPAGE))?\s*$"
)


class Point(NamedTuple):
    x: int
    y: int


@dataclass(frozen=True)
class Actor:
    name: str
    at: Point
    panel_id: str | None
    # Character span of the ACTOR line in the decoded manuscript text.
    line_span: tuple[int, int]


@dataclass(frozen=True)
class Panel:
    panel_id: str
    box: tuple[int, int, int, int]
    line_span: tuple[int, int]

    @property
    def center(self) -> Point:
        x0, y0, x1, y1 = self.box
        return Point((x0 + x1) // 2, (y0 + y1) // 2)


@dataclass(frozen=True)
class Say:
    bubble_id: str
    text: str
    line_span: tuple[int, int]


@dataclass(frozen=True)
class Bubble:
    bubble_id: str
    panel_id: str
    box: tuple[int, int, int, int]
    offpage: bool
    tail: Point | None
    tail_span: tuple[int, int] | None
    line_span: tuple[int, int]

    @property
    def center(self) -> Point:
        x0, y0, x1, y1 = self.box
        return Point((x0 + x1) // 2, (y0 + y1) // 2)


@dataclass(frozen=True)
class Relation:
    first: str
    second: str
    # ">" means second answers first (first read first); "<" means first answers second.
    symbol: str
    line_span: tuple[int, int]

    @property
    def edge(self) -> tuple[str, str]:
        """Return ``(read_first, read_second)`` regardless of symbol side.

        ``X>Y`` means Y answers X, so X speaks first: edge X -> Y.
        ``X<Y`` means X answers Y, so Y speaks first: edge Y -> X.
        """
        if self.symbol == ">":
            return (self.first, self.second)
        return (self.second, self.first)


@dataclass
class ManuscriptPage:
    page_id: str
    line_span: tuple[int, int]
    # Physical line numbers (1-based over the whole sheet) this PAGE block
    # occupies, including every directive that belongs to it.
    line_nos: set[int] = field(default_factory=set)
    panels: dict[str, Panel] = field(default_factory=dict)
    actors: dict[str, Actor] = field(default_factory=dict)
    bubbles: dict[str, Bubble] = field(default_factory=dict)
    says: dict[str, Say] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)

    def bubble(self, bubble_id: str) -> Bubble:
        return self.bubbles[bubble_id]


@dataclass
class Manuscript:
    pages: dict[str, ManuscriptPage]
    # Full decoded text the manuscript was parsed from.
    text: str
    # Char offset where each physical line starts (line 0 starts at 0).
    line_starts: tuple[int, ...]

    @property
    def all_bubbles(self) -> dict[str, Bubble]:
        out: dict[str, Bubble] = {}
        for page in self.pages.values():
            out.update(page.bubbles)
        return out

    def page_of(self, bubble_id: str) -> ManuscriptPage:
        for page in self.pages.values():
            if bubble_id in page.bubbles:
                return page
        raise KeyError(bubble_id)

    def byte_span(self, char_span: tuple[int, int], doc) -> tuple[int, int]:
        """Map a decoded-character span back to original-body byte offsets."""
        start_char, end_char = char_span
        n = len(doc.text)
        if end_char > n:
            end_char = n
        if start_char >= n:
            start = doc.char_boundary_byte(n)
            return start, start
        b0 = doc.byte_range(start_char)[0]
        if end_char <= start_char:
            return b0, b0
        b1 = doc.byte_range(min(end_char - 1, n - 1))[1]
        return b0, b1


class ManuscriptError(ValueError):
    """The registration sheet cannot be parsed at all."""

    def __init__(self, reason: str, line_no: int, line_span: tuple[int, int]) -> None:
        self.reason = reason
        self.line_no = line_no
        self.line_span = line_span
        super().__init__(f"line {line_no}: {reason}")


@dataclass(frozen=True)
class ManuscriptIssue:
    """A semantic problem found on a parseable sheet, located to a line."""

    code: str
    reason: str
    line_no: int
    line_span: tuple[int, int]
    bubble_id: str | None = None

    def summary(self) -> str:
        head = f"[{self.code}] line {self.line_no}"
        if self.bubble_id:
            head += f" bubble {self.bubble_id}"
        return f"{head}: {self.reason}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _line_spans(text: str) -> tuple[tuple[str, int, int, int], ...]:
    """Yield ``(stripped_line, line_no, start_char, end_char_exclusive)``."""
    out: list[tuple[str, int, int, int]] = []
    offset = 0
    line_no = 0
    for raw in text.split("\n"):
        end = offset + len(raw)
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            out.append((stripped, line_no, offset, end))
        offset = end + 1  # the newline itself
        line_no += 1
    return tuple(out)


def parse_manuscript(text: str) -> Manuscript:
    """Parse a registration sheet; raise :class:`ManuscriptError` on syntax errors."""
    spans = _line_spans(text)
    line_starts: list[int] = []
    pos = 0
    for raw in text.split("\n"):
        line_starts.append(pos)
        pos += len(raw) + 1

    pages: dict[str, ManuscriptPage] = {}
    current: ManuscriptPage | None = None
    total_bubbles = 0
    seen_panels: set[tuple[str, str]] = set()

    def fail(reason: str, line_no: int, span: tuple[int, int]) -> None:
        raise ManuscriptError(reason, line_no + 1, span)

    for stripped, line_no, cstart, cend in spans:
        span = (cstart, cend)
        if stripped.startswith("PAGE "):
            page_id = stripped[5:].strip()
            if not page_id:
                fail("PAGE needs an id", line_no, span)
            if page_id in pages:
                fail(f"duplicate PAGE {page_id}", line_no, span)
            if len(pages) >= MAX_PAGES:
                fail(f"a manuscript holds at most {MAX_PAGES} pages", line_no, span)
            current = ManuscriptPage(page_id=page_id, line_span=span, line_nos={line_no + 1})
            pages[page_id] = current
            continue

        if current is None:
            fail("directive before any PAGE", line_no, span)
        current.line_nos.add(line_no + 1)

        if m := _PANEL.match(stripped):
            panel_id, x0, y0, x1, y1 = m.groups()
            if not (int(x0) < int(x1) and int(y0) < int(y1)):
                fail(f"panel {panel_id} box must be x0<x1 and y0<y1", line_no, span)
            key = (current.page_id, panel_id)
            if key in seen_panels:
                fail(f"duplicate PANEL {panel_id}", line_no, span)
            seen_panels.add(key)
            current.panels[panel_id] = Panel(panel_id, (int(x0), int(y0), int(x1), int(y1)), span)

        elif m := _ACTOR.match(stripped):
            name, x, y, panel_id = m.groups()
            if name in current.actors:
                fail(f"duplicate ACTOR {name}", line_no, span)
            if panel_id is not None and panel_id not in current.panels:
                fail(f"ACTOR {name} references unknown PANEL {panel_id}", line_no, span)
            current.actors[name] = Actor(name, Point(int(x), int(y)), panel_id, span)

        elif m := _BUBBLE.match(stripped):
            bubble_id, panel_id, x0, y0, x1, y1, offpage = m.groups()
            if bubble_id in current.bubbles:
                fail(f"duplicate BUBBLE {bubble_id} on page {current.page_id}", line_no, span)
            if total_bubbles >= MAX_BUBBLES:
                fail(f"a manuscript holds at most {MAX_BUBBLES} bubbles", line_no, span)
            if panel_id not in current.panels:
                fail(f"BUBBLE {bubble_id} references unknown PANEL {panel_id}", line_no, span)
            if not (int(x0) < int(x1) and int(y0) < int(y1)):
                fail(f"BUBBLE {bubble_id} box must be x0<x1 and y0<y1", line_no, span)
            total_bubbles += 1
            current.bubbles[bubble_id] = Bubble(
                bubble_id=bubble_id,
                panel_id=panel_id,
                box=(int(x0), int(y0), int(x1), int(y1)),
                offpage=bool(offpage),
                tail=None,
                tail_span=None,
                line_span=span,
            )

        elif stripped.startswith("SAY "):
            rest = stripped[4:]
            parts = rest.split(None, 1)
            if len(parts) != 2 or not _BUBBLE_ID.fullmatch(parts[0]):
                fail("SAY needs a bubble id and text, e.g. SAY A hello", line_no, span)
            bubble_id, content = parts[0], parts[1]
            if bubble_id not in current.bubbles:
                fail(f"SAY references unknown BUBBLE {bubble_id}", line_no, span)
            if bubble_id in current.says:
                fail(f"duplicate SAY for bubble {bubble_id}", line_no, span)
            # Locate the text portion precisely (after the id and separating space).
            text_start = cstart + stripped.index(content)
            current.says[bubble_id] = Say(bubble_id, content, (text_start, text_start + len(content)))

        elif stripped.startswith("TAIL "):
            m = re.match(rf"^TAIL\s+([A-Z][A-Z0-9]*)\s+->\s+{_INT4}\s+{_INT4}\s*$", stripped)
            if not m:
                fail("TAIL needs 'TAIL <id> -> <x> <y>'", line_no, span)
            bubble_id, x, y = m.groups()
            if bubble_id not in current.bubbles:
                fail(f"TAIL references unknown BUBBLE {bubble_id}", line_no, span)
            old = current.bubbles[bubble_id]
            if old.tail is not None:
                fail(f"duplicate TAIL for bubble {bubble_id}", line_no, span)
            current.bubbles[bubble_id] = Bubble(
                bubble_id=old.bubble_id,
                panel_id=old.panel_id,
                box=old.box,
                offpage=old.offpage,
                tail=Point(int(x), int(y)),
                tail_span=span,
                line_span=old.line_span,
            )

        elif m := _RELATION.match(stripped):
            a, symbol, b = m.groups()
            for bubble_id in (a, b):
                if bubble_id not in current.bubbles:
                    fail(f"REPLY references unknown BUBBLE {bubble_id}", line_no, span)
            if a == b:
                # A self reply is syntactically parseable; semantic validation
                # reports it as a self-loop so the original page can be traced.
                pass
            current.relations.append(Relation(a, b, symbol, span))

        else:
            fail(f"unrecognised directive {stripped.split()[0]!r}", line_no, span)

    if not pages:
        raise ManuscriptError("registration sheet has no PAGE", 1, (0, min(len(text), 1)))
    return Manuscript(pages=pages, text=text, line_starts=tuple(line_starts))


# ---------------------------------------------------------------------------
# Semantic validation
# ---------------------------------------------------------------------------


def _issue(code: str, reason: str, line_no: int, span: tuple[int, int], bubble_id: str | None = None):
    return ManuscriptIssue(code, reason, line_no + 1, span, bubble_id)


def _line_no(manuscript: Manuscript, span: tuple[int, int]) -> int:
    # Binary-ish search; sheets are tiny so a scan is fine.
    for i, start in enumerate(manuscript.line_starts):
        if start > span[0]:
            return i - 1
    return len(manuscript.line_starts) - 1


def validate_manuscript(manuscript: Manuscript) -> list[ManuscriptIssue]:
    """Return every semantic issue (an empty list means the sheet is sound)."""
    issues: list[ManuscriptIssue] = []

    for page in manuscript.pages.values():
        # Every on-page bubble needs exactly one tail, ending at its speaker.
        for bubble_id, bubble in page.bubbles.items():
            if bubble.offpage:
                continue
            if bubble.tail is None:
                issues.append(
                    _issue(
                        "MISSING_TAIL",
                        f"bubble {bubble_id} has no TAIL endpoint",
                        _line_no(manuscript, bubble.line_span),
                        bubble.line_span,
                        bubble_id,
                    )
                )
                continue
            anchor = _speaker_at(page, bubble.tail)
            if anchor is None:
                issues.append(
                    _issue(
                        "TAIL_MISWIRED",
                        f"tail of {bubble_id} ends at {bubble.tail} where no speaker stands",
                        _line_no(manuscript, bubble.tail_span or bubble.line_span),
                        bubble.tail_span or bubble.line_span,
                        bubble_id,
                    )
                )

        # A tail ending at a speaker means that speaker owns the bubble.
        owners = _tail_owners(page)
        for bubble_id, owner_name in owners.items():
            say = page.says.get(bubble_id)
            if say is None:
                issues.append(
                    _issue(
                        "MISSING_SAY",
                        f"bubble {bubble_id} has no SAY line",
                        _line_no(manuscript, page.bubbles[bubble_id].line_span),
                        page.bubbles[bubble_id].line_span,
                        bubble_id,
                    )
                )

        # Relations: both ends known, no self loops, no edge leaving an
        # off-page narrator, and a reply graph without cycles.
        adjacency: dict[str, set[str]] = {b: set() for b in page.bubbles}
        for rel in page.relations:
            line_no = _line_no(manuscript, rel.line_span)
            if rel.first == rel.second:
                issues.append(
                    _issue(
                        "SELF_LOOP",
                        f"REPLY {rel.first}{rel.symbol}{rel.second} makes {rel.first} answer itself",
                        line_no,
                        rel.line_span,
                        rel.first,
                    )
                )
                continue
            earlier, later = rel.edge
            if page.bubbles[earlier].offpage:
                issues.append(
                    _issue(
                        "NARRATOR_FIRST",
                        f"off-page narrator {earlier} cannot be read before another bubble",
                        line_no,
                        rel.line_span,
                        earlier,
                    )
                )
            adjacency.setdefault(earlier, set()).add(later)
            adjacency.setdefault(later, set())

        cycle = _find_cycle(adjacency)
        if cycle:
            rel = _relation_for_edge(page, cycle[-1], cycle[0]) or page.relations[0]
            issues.append(
                _issue(
                    "REPLY_CYCLE",
                    "reply relations form a cycle: " + " -> ".join([*cycle, cycle[0]]),
                    _line_no(manuscript, rel.line_span),
                    rel.line_span,
                    cycle[0],
                )
            )

        # Off-page narration must stay last: no on-page bubble may be forced
        # after it by the reply graph.
        order = reading_order(page) if not cycle else None
        if order is not None:
            for bubble_id, bubble in page.bubbles.items():
                if bubble.offpage and order and order[-1] != bubble_id:
                    rel = _relation_touching(page, bubble_id)
                    span = rel.line_span if rel else bubble.line_span
                    issues.append(
                        _issue(
                            "NARRATOR_NOT_LAST",
                            f"off-page narrator {bubble_id} is not the final bubble in reading order",
                            _line_no(manuscript, span),
                            span,
                            bubble_id,
                        )
                    )

    return issues


def _speaker_at(page: ManuscriptPage, point: Point) -> Actor | None:
    for actor in page.actors.values():
        if actor.at == point:
            return actor
    return None


def _tail_owners(page: ManuscriptPage) -> dict[str, str]:
    """Map each bubble to the speaker its tail lands on."""
    owners: dict[str, str] = {}
    for bubble_id, bubble in page.bubbles.items():
        if bubble.tail is None:
            continue
        actor = _speaker_at(page, bubble.tail)
        if actor is not None:
            owners[bubble_id] = actor.name
    return owners


def _relation_for_edge(page: ManuscriptPage, earlier: str, later: str) -> Relation | None:
    for rel in page.relations:
        a, b = rel.edge
        if (a, b) == (earlier, later):
            return rel
    return None


def _relation_touching(page: ManuscriptPage, bubble_id: str) -> Relation | None:
    for rel in page.relations:
        if bubble_id in (rel.first, rel.second):
            return rel
    return None


def _find_cycle(adjacency: dict[str, set[str]]) -> list[str] | None:
    color: dict[str, int] = {node: 0 for node in adjacency}  # 0 white, 1 grey, 2 black
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(adjacency.get(node, ())):
            if color.get(nxt, 0) == 1:
                return stack[stack.index(nxt):]
            if color.get(nxt, 0) == 0:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = 2
        return None

    for node in sorted(adjacency):
        if color[node] == 0:
            found = visit(node)
            if found:
                return found
    return None


# ---------------------------------------------------------------------------
# Reading order
# ---------------------------------------------------------------------------


def natural_key(page: ManuscriptPage, bubble_id: str) -> tuple[int, int, str]:
    """Row-major natural position: top-to-bottom rows, left-to-right in a row.

    Bubbles whose vertical centres sit on the same row (half the smallest
    panel height is the row tolerance) share a row; x then breaks ties.
    """
    bubble = page.bubbles[bubble_id]
    row_tolerance = max(
        1,
        min((p.box[3] - p.box[1]) for p in page.panels.values()) // 2,
    ) if page.panels else 1
    row = bubble.center.y // row_tolerance
    return (row, bubble.center.x, bubble_id)


def _reply_edges(page: ManuscriptPage) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for rel in page.relations:
        if rel.first != rel.second:
            edges.add(rel.edge)
    return edges


def reading_order(page: ManuscriptPage) -> list[str]:
    """Topological reading order.

    Reply edges force the answerer after the speaker; off-page narrators are
    held back to the end.  Ties always resolve by the natural row-major
    position, so independent short lines read in parallel left-to-right.
    Raises :class:`ValueError` when the reply graph contains a cycle.
    """
    edges = _reply_edges(page)
    nodes = set(page.bubbles)
    indegree = {b: 0 for b in nodes}
    adjacency: dict[str, list[str]] = {b: [] for b in nodes}
    for earlier, later in edges:
        if earlier in nodes and later in nodes:
            adjacency[earlier].append(later)
            indegree[later] += 1

    def available(remaining: set[str], allow_offpage: bool) -> list[str]:
        ready = [b for b in remaining if indegree[b] == 0]
        if not allow_offpage:
            on_page = [b for b in ready if not page.bubbles[b].offpage]
            if on_page:
                ready = on_page
        return sorted(ready, key=lambda b: natural_key(page, b))

    remaining = set(nodes)
    order: list[str] = []
    while remaining:
        ready = available(remaining, allow_offpage=False)
        if not ready:  # only off-page narrators remain unblocked
            ready = available(remaining, allow_offpage=True)
        if not ready:
            raise ValueError("reply graph contains a cycle; no reading order exists")
        chosen = ready[0]
        order.append(chosen)
        remaining.remove(chosen)
        for nxt in adjacency[chosen]:
            indegree[nxt] -= 1
    return order


def inversions(page: ManuscriptPage, order: list[str]) -> list[tuple[str, str]]:
    """Reply edges whose natural paper order contradicts the reading order."""
    rank = {bubble_id: i for i, bubble_id in enumerate(order)}
    natural = sorted(page.bubbles, key=lambda b: natural_key(page, b))
    natural_rank = {bubble_id: i for i, bubble_id in enumerate(natural)}
    out: list[tuple[str, str]] = []
    for earlier, later in _reply_edges(page):
        if natural_rank[later] < natural_rank[earlier]:
            out.append((earlier, later))
    return out
