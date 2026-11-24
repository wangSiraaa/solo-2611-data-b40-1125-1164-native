"""Reasoning engine: candidate placements, exclusions, surviving interpretations.

The packing desk never guesses a piece's edition.  For every physical piece it
keeps the set of plate editions the current evidence still allows and, inside
each still-possible edition, the cells the piece can physically occupy.

The exclusion chain applied to every (piece, edition, cell) candidate is:

1. *face*       - the piece's printed zone must be the zone the cell shows
                  under that edition;
2. *rotation*   - the order's rotation policy must permit an orientation the
                  piece physically allows;
3. *borders*    - flat ("F") edges may only face the outside of the board and
                  every border-facing side of the cell must be flat;
4. *adjacency*  - wherever the cell touches an already recognised neighbour
                  piece, the two touching edge marks must be tab/blank
                  opposites.  A candidate whose mark cannot mate is dropped.

An *interpretation* is one concrete edition per still-ambiguous piece.  An
interpretation survives only when the board admits a complete bijection of
pieces onto cells (each cell filled once, each piece used once) satisfying all
four constraints.  A box may be promised only when **every** surviving
interpretation is completable, so no later back-code outcome can make the
sealed box unbuildable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Iterable

from .board import BoardSpec, Cell, edges_mate, opposite
from .pieces import Piece


@dataclass(frozen=True)
class Placement:
    """One way one piece can occupy one cell."""

    piece_id: str
    cell_id: str
    edition: str
    orientation: int

    def to_json(self) -> dict:
        return {
            "piece_id": self.piece_id,
            "cell_id": self.cell_id,
            "edition": self.edition,
            "orientation": self.orientation,
        }


@dataclass
class Interpretation:
    """One whole-plate explanation: a single board edition plus one complete
    tiling proving every cell can be filled under that edition."""

    edition: str
    placements: tuple[Placement, ...]        # a complete, consistent tiling
    ambiguous_pieces: tuple[str, ...]

    @property
    def key(self) -> tuple:
        return (self.edition,)

    @property
    def assignment(self) -> dict[str, str]:
        return {p.piece_id: self.edition for p in self.placements}

    def to_json(self) -> dict:
        return {
            "edition": self.edition,
            "assignment": dict(sorted(self.assignment.items())),
            "tiling": [p.to_json() for p in self.placement_order()],
            "ambiguous_pieces": list(self.ambiguous_pieces),
        }

    def placement_order(self) -> list[Placement]:
        return sorted(self.placements, key=lambda p: (p.cell_id, p.piece_id))


@dataclass
class ReasoningReport:
    board_id: str
    pieces: list[Piece]
    allow_rotation: bool
    interpretations: list[Interpretation]
    # piece_id -> edition -> list[Placement] across every surviving tiling
    placements_by_piece: dict[str, dict[str, list[Placement]]]
    exclusions: list[dict] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return bool(self.interpretations)

    def candidate_pieces_for_cell(self, cell_id: str) -> list[str]:
        ids = {
            p.piece_id
            for by_edition in self.placements_by_piece.values()
            for placements in by_edition.values()
            for p in placements
            if p.cell_id == cell_id
        }
        return sorted(ids)

    def to_json(self) -> dict:
        return {
            "board_id": self.board_id,
            "allow_rotation": self.allow_rotation,
            "feasible": self.feasible,
            "interpretation_count": len(self.interpretations),
            "interpretations": [i.to_json() for i in self.interpretations],
            "placements_by_piece": {
                pid: {
                    edition: [p.to_json() for p in sorted(pl, key=lambda x: x.cell_id)]
                    for edition, pl in sorted(by_edition.items())
                }
                for pid, by_edition in sorted(self.placements_by_piece.items())
            },
            "exclusions": list(self.exclusions),
        }


# ---------------------------------------------------------------------------
# Candidate generation with the documented exclusion chain
# ---------------------------------------------------------------------------

def _allowed_orientations(piece: Piece, allow_rotation: bool) -> tuple[int, ...]:
    if allow_rotation:
        return piece.orientations
    # No rotation permitted: the piece may only be used as photographed (0).
    return (0,) if 0 in piece.orientations else ()


def candidate_placements(
    board: BoardSpec,
    pieces: Iterable[Piece],
    *,
    allow_rotation: bool,
    known_neighbors: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> tuple[dict[str, dict[str, list[Placement]]], list[dict]]:
    """Return per-piece/edition candidate placements and an exclusion audit.

    ``known_neighbors`` maps (cell_id, direction) to (neighbour_piece_id, mark):
    adjacency evidence from already identified pieces of the same batch.
    """
    known_neighbors = known_neighbors or {}
    candidates: dict[str, dict[str, list[Placement]]] = {}
    exclusions: list[dict] = []

    by_id = {p.piece_id: p for p in pieces}
    for piece in pieces:
        candidates[piece.piece_id] = {}
        orientations = _allowed_orientations(piece, allow_rotation)
        if not orientations:
            exclusions.append(
                {
                    "piece_id": piece.piece_id,
                    "rule": "rotation",
                    "reason": "order forbids rotation but the piece has no 0-degree use",
                }
            )
        for edition in piece.editions:
            placements: list[Placement] = []
            for cell in board.cells:
                required_zone = cell.zone_for(edition)
                if required_zone is None or piece.face_zone != required_zone:
                    exclusions.append(
                        {
                            "piece_id": piece.piece_id,
                            "edition": edition,
                            "cell_id": cell.cell_id,
                            "rule": "face",
                            "reason": (
                                f"face zone {piece.face_zone!r} != cell zone "
                                f"{required_zone!r} under edition {edition}"
                            ),
                        }
                    )
                    continue
                for orientation in orientations:
                    rotated = piece.edges_at(orientation)
                    border_ok = True
                    for direction in board.border_directions(cell):
                        if rotated[direction] != "F":
                            exclusions.append(
                                {
                                    "piece_id": piece.piece_id,
                                    "edition": edition,
                                    "cell_id": cell.cell_id,
                                    "orientation": orientation,
                                    "rule": "borders",
                                    "reason": (
                                        f"{direction} side faces the board border but "
                                        f"carries {rotated[direction]!r}, not flat"
                                    ),
                                }
                            )
                            border_ok = False
                            break
                    if not border_ok:
                        continue
                    for direction in board.internal_directions(cell):
                        if rotated[direction] == "F":
                            exclusions.append(
                                {
                                    "piece_id": piece.piece_id,
                                    "edition": edition,
                                    "cell_id": cell.cell_id,
                                    "orientation": orientation,
                                    "rule": "borders",
                                    "reason": f"{direction} side is internal but flat",
                                }
                            )
                            border_ok = False
                            break
                    if not border_ok:
                        continue

                    adjacency_ok = True
                    for (cand_cell, direction), (neighbour_id, neighbour_mark) in known_neighbors.items():
                        if cand_cell != cell.cell_id:
                            continue
                        neighbour = by_id.get(neighbour_id)
                        if neighbour is None:
                            continue
                        # Only evidence for an edition both pieces can share.
                        if edition not in neighbour.editions:
                            continue
                        if not edges_mate(rotated[direction], neighbour_mark):
                            exclusions.append(
                                {
                                    "piece_id": piece.piece_id,
                                    "edition": edition,
                                    "cell_id": cell.cell_id,
                                    "orientation": orientation,
                                    "rule": "adjacency",
                                    "reason": (
                                        f"{direction} edge {rotated[direction]!r} cannot "
                                        f"mate piece {neighbour_id!r} mark {neighbour_mark!r}"
                                    ),
                                }
                            )
                            adjacency_ok = False
                            break
                    if not adjacency_ok:
                        continue
                    placements.append(
                        Placement(
                            piece_id=piece.piece_id,
                            cell_id=cell.cell_id,
                            edition=edition,
                            orientation=orientation,
                        )
                    )
            # Deduplicate (same cell may be reachable only once, but keep this safe).
            unique = {(p.cell_id, p.orientation): p for p in placements}
            candidates[piece.piece_id][edition] = [
                unique[k] for k in sorted(unique)
            ]
    return candidates, exclusions


# ---------------------------------------------------------------------------
# Interpretations and complete-tiling search
# ---------------------------------------------------------------------------

def _complete_tiling(
    board: BoardSpec,
    pieces: list[Piece],
    assignment: dict[str, str],
    candidates: dict[str, dict[str, list[Placement]]],
) -> tuple[Placement, ...] | None:
    """Backtracking bijection search: every cell filled once, edges must mate."""
    cells = sorted(board.cells, key=lambda c: c.cell_id)
    piece_by_id = {p.piece_id: p for p in pieces}

    def search(index: int, used_pieces: set[str], chosen: dict[str, Placement]):
        if index == len(cells):
            return tuple(sorted(chosen.values(), key=lambda p: p.cell_id))
        cell = cells[index]
        options: list[Placement] = []
        for piece in pieces:
            if piece.piece_id in used_pieces:
                continue
            edition = assignment[piece.piece_id]
            for placement in candidates[piece.piece_id].get(edition, []):
                if placement.cell_id != cell.cell_id:
                    continue
                if not _edges_consistent(board, cell, placement, chosen, piece_by_id):
                    continue
                options.append(placement)
        # Most constrained first is unnecessary at this scale; keep ordering stable.
        for placement in sorted(options, key=lambda p: (p.piece_id, p.orientation)):
            chosen[cell.cell_id] = placement
            used_pieces.add(placement.piece_id)
            result = search(index + 1, used_pieces, chosen)
            if result is not None:
                return result
            used_pieces.remove(placement.piece_id)
            del chosen[cell.cell_id]
        return None

    return search(0, set(), {})


def _edges_consistent(
    board: BoardSpec,
    cell: Cell,
    placement: Placement,
    chosen: dict[str, Placement],
    piece_by_id: dict[str, Piece],
) -> bool:
    piece = piece_by_id[placement.piece_id]
    rotated = piece.edges_at(placement.orientation)
    for direction in board.internal_directions(cell):
        neighbour_cell = board.neighbor(cell, direction)
        other = chosen.get(neighbour_cell.cell_id)
        if other is None:
            continue  # neighbour cell not filled yet; checked when it is placed
        other_piece = piece_by_id[other.piece_id]
        other_edges = other_piece.edges_at(other.orientation)
        if not edges_mate(rotated[direction], other_edges[opposite(direction)]):
            return False
    return True


def reason(
    board: BoardSpec,
    pieces: list[Piece],
    *,
    allow_rotation: bool,
    known_neighbors: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> ReasoningReport:
    """Run the full exclusion + surviving-interpretation analysis.

    A whole-plate interpretation picks *one* board edition and asks whether the
    pieces whose evidence still permits that edition tile every cell.  An
    interpretation survives iff such a complete tiling exists.
    """
    candidates, exclusions = candidate_placements(
        board, pieces, allow_rotation=allow_rotation, known_neighbors=known_neighbors
    )

    # Pieces that can occupy a cell in at least one edition are the usable
    # stock; pieces excluded everywhere never enter a tiling.
    participating = [
        piece
        for piece in pieces
        if any(candidates[piece.piece_id].get(edition) for edition in piece.editions)
    ]
    ambiguous = tuple(
        sorted(
            p.piece_id
            for p in participating
            if len(p.editions) > 1
        )
    )
    interpretations: list[Interpretation] = []

    for edition in board.editions:
        in_edition = [p for p in participating if edition in p.editions]
        assignment = {p.piece_id: edition for p in in_edition}
        tiling = _complete_tiling(board, in_edition, assignment, candidates)
        if tiling is None:
            continue
        interpretations.append(
            Interpretation(
                edition=edition,
                placements=tiling,
                ambiguous_pieces=ambiguous,
            )
        )

    interpretations.sort(key=lambda i: i.key)

    placements_by_piece: dict[str, dict[str, list[Placement]]] = {
        p.piece_id: {} for p in pieces
    }
    for interp in interpretations:
        for placement in interp.placements:
            by_edition = placements_by_piece[placement.piece_id]
            by_edition.setdefault(placement.edition, [])
            if not any(
                q.cell_id == placement.cell_id and q.orientation == placement.orientation
                for q in by_edition[placement.edition]
            ):
                by_edition[placement.edition].append(placement)
    for by_edition in placements_by_piece.values():
        for pl in by_edition.values():
            pl.sort(key=lambda p: (p.cell_id, p.orientation))

    return ReasoningReport(
        board_id=board.board_id,
        pieces=list(pieces),
        allow_rotation=allow_rotation,
        interpretations=interpretations,
        placements_by_piece=placements_by_piece,
        exclusions=exclusions,
    )


# ---------------------------------------------------------------------------
# No-peel spare-piece cover (the single implemented delivery strategy)
# ---------------------------------------------------------------------------

def covers_cell(
    candidates: dict[str, dict[str, list[Placement]]],
    piece_id: str,
    edition: str,
    cell_id: str,
) -> bool:
    return any(
        p.cell_id == cell_id
        for p in candidates.get(piece_id, {}).get(edition, [])
    )


def minimum_cover(
    board: BoardSpec,
    pieces: list[Piece],
    report: ReasoningReport,
    *,
    available_piece_ids: set[str],
    required_edition: str,
) -> tuple[frozenset[str], list[dict]] | None:
    """Smallest piece set that keeps the ordered edition buildable across
    every still-possible physical outcome.

    The sealing paper is never peeled.  Each ambiguous piece (back paper on,
    photograph leaving two editions open) can physically turn out to be either
    edition.  Enumerate the joint outcome vectors and, for every outcome that
    admits an ordered-edition tiling at all, take the smallest such tiling.
    The packed set is the union of those tilings: at assembly time the buyer
    trial-fits the actual pieces, drops any that turn out to be the other
    edition, and a complete tiling still remains inside the box.

    Outcomes under which no ordered-edition tiling exists are only fatal when
    they are not ruled out by other evidence: if *every* outcome is untileable
    the ordered edition cannot be promised.  Physically impossible outcomes
    (for example two distinct pieces both resolving to the single wrong
    edition when only one copy of that edition exists in the batch) simply
    contribute no tiling.

    Ties inside each outcome are broken by piece id, so the cover is
    deterministic; spare pieces are precisely the cover minus the smallest
    tiling under the buyer's edition.
    """
    candidates, _ = candidate_placements(
        board, pieces, allow_rotation=report.allow_rotation
    )
    cell_ids = [c.cell_id for c in sorted(board.cells, key=lambda c: c.cell_id)]

    relevant = [
        p for p in pieces
        if p.piece_id in available_piece_ids and required_edition in p.editions
    ]
    ambiguous = [p for p in relevant if len(p.editions) > 1]
    fixed = [p for p in relevant if len(p.editions) == 1]

    outcome_vectors = list(product(*(p.editions for p in ambiguous))) or [()]
    outcome_covers: list[frozenset[str]] = []
    for vector in outcome_vectors:
        outcome = {p.piece_id: required_edition for p in fixed}
        for piece, edition in zip(ambiguous, vector):
            outcome[piece.piece_id] = edition
        usable = [p for p in relevant if outcome[p.piece_id] == required_edition]
        assignment = {p.piece_id: required_edition for p in usable}
        tiling_sets = _all_tiling_covers(
            board, usable, assignment, candidates, set(available_piece_ids)
        )
        if tiling_sets:
            outcome_covers.append(
                min(tiling_sets, key=lambda s: (len(s), sorted(s)))
            )

    if not outcome_covers:
        return None
    packed = frozenset().union(*outcome_covers)
    instructions = _instructions(board, report, packed, candidates, cell_ids)
    return packed, instructions


def _all_tiling_covers(
    board: BoardSpec,
    pieces: list[Piece],
    assignment: dict[str, str],
    candidates: dict[str, dict[str, list[Placement]]],
    available: set[str],
) -> list[frozenset[str]]:
    """All piece sets (within stock) that fully tile under one board edition."""
    cells = sorted(board.cells, key=lambda c: c.cell_id)
    piece_by_id = {p.piece_id: p for p in pieces}
    results: list[frozenset[str]] = []

    def search(index: int, used: set[str], chosen: dict[str, Placement]):
        if index == len(cells):
            results.append(frozenset(used))
            return
        cell = cells[index]
        seen_pieces: set[str] = set()
        for piece in pieces:
            if piece.piece_id in used or piece.piece_id not in available:
                continue
            edition = assignment[piece.piece_id]
            for placement in candidates[piece.piece_id].get(edition, []):
                if placement.cell_id != cell.cell_id:
                    continue
                if piece.piece_id in seen_pieces:
                    continue
                if not _edges_consistent(board, cell, placement, chosen, piece_by_id):
                    continue
                seen_pieces.add(piece.piece_id)
                chosen[cell.cell_id] = placement
                used.add(piece.piece_id)
                search(index + 1, used, chosen)
                used.remove(piece.piece_id)
                del chosen[cell.cell_id]

    search(0, set(), {})
    unique = sorted(set(results), key=lambda s: sorted(s))
    return unique


def _instructions(
    board: BoardSpec,
    report: ReasoningReport,
    subset: frozenset[str],
    candidates: dict[str, dict[str, list[Placement]]],
    cell_ids: list[str],
) -> list[dict]:
    """Per-cell selection notes: pick by edge marks; spares are identified."""
    notes: list[dict] = []
    for cell_id in cell_ids:
        fitting = sorted(
            pid
            for pid in subset
            if any(
                p.cell_id == cell_id
                for by_edition in candidates[pid].values()
                for p in by_edition
            )
        )
        notes.append(
            {
                "cell_id": cell_id,
                "candidate_pieces": fitting,
                "instruction": (
                    "试拼该格：按相邻边纹选片，凸(T)配凹(B)，平边(F)朝外；"
                    "背纸未揭的片若拼合后印面与订版不符，换同格备用片"
                ),
            }
        )
    spares = sorted(
        pid
        for pid in subset
        if not any(pid in n["candidate_pieces"] for n in notes)
    )
    for pid in spares:
        notes.append({"cell_id": None, "candidate_pieces": [pid],
                      "instruction": "备用片：仅在试拼判定为另一版时启用"})
    return notes
