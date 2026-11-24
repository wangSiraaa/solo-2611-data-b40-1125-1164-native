"""社区小报投递结算 (community-paper delivery settlement).

The station's observations never identify every delivered mailbox.  Each
round/edition is only described by *partial* evidence:

  * a named household that was certainly delivered (``named_delivered``);
  * the size of the delivered set, without names (``set_size``);
  * the size of the intersection of two editions (``intersection``);
  * a named household that certainly did not receive (``named_missed``).

Settlement is therefore a *constraint* problem.  A *history* is one choice of
delivered set per edition of a block that satisfies every observation.  The
guaranteed quantity that can be 认款 (credited) is the worst case over all
compatible histories:

    lower_bound(block) = min over compatible histories of |union of sets|

Two supporting artifacts are always retained:

  * ``witness`` — one concrete arrangement (a delivered set per edition) that
    actually attains the lower bound, so the bound is independently
    re-checkable;
  * per-candidate-line ``added_lower_bound`` — the minimum number of
    *additional* households the line contributes in every compatible history,
    i.e. ``min |line \\ union|``; sealed lines are judged against this worst
    case, never against a hoped-for actual overlap.

The envelopes are deliberately tiny (at most eight households, four lines), so
the engine enumerates every candidate delivered subset per edition
(2**8 = 256 at most) and backtracks over editions.  Nothing is sampled and no
assumption of uniform randomness is made: a credit is only booked when *every*
remaining compatible history supports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable

from ..errors import ContractError

# The four legal observation kinds.  Their stable field numbers live in the
# delivery writer/reader contracts (materials/paper/*-contract.json).
NAMED_DELIVERED = "named_delivered"
SET_SIZE = "set_size"
INTERSECTION = "intersection"
NAMED_MISSED = "named_missed"

MEASURES = frozenset({NAMED_DELIVERED, SET_SIZE, INTERSECTION, NAMED_MISSED})


@dataclass(frozen=True)
class Observation:
    """One station observation, parsed from a published reader row."""

    block: str
    measure: str
    round_no: int
    edition: str | None = None
    edition_b: str | None = None
    household: str | None = None
    qty: int | None = None


# ---------------------------------------------------------------------------
# Parsing the published ingest rows (the *valid* rows are the only input)
# ---------------------------------------------------------------------------

def _parse_one(index: int, row: dict, errors: list[str]) -> Observation | None:
    values = row.get("values", row)
    try:
        block = values["block"]
        measure = values["measure"]
        round_raw = values["round"]
    except (TypeError, KeyError) as exc:
        errors.append(f"row #{index}: missing field {exc}")
        return None
    edition = values.get("edition")
    household = values.get("household")
    qty_raw = values.get("qty")

    if not isinstance(block, str) or not block:
        errors.append(f"row #{index}: block must be a non-empty string")
        return None
    if measure not in MEASURES:
        errors.append(
            f"row #{index}: unknown measure {measure!r}; expected one of "
            f"{sorted(MEASURES)}"
        )
        return None
    try:
        round_no = int(round_raw)
    except (TypeError, ValueError):
        errors.append(f"row #{index}: round must be an integer, got {round_raw!r}")
        return None
    if edition is not None and not isinstance(edition, str):
        errors.append(f"row #{index}: edition must be a string or null")
        return None
    if household is not None and not isinstance(household, str):
        errors.append(f"row #{index}: household must be a string or null")
        return None
    qty: int | None = None
    if qty_raw is not None:
        try:
            qty = int(qty_raw)
        except (TypeError, ValueError):
            errors.append(f"row #{index}: qty must be an integer or null")
            return None

    if measure == INTERSECTION:
        # An intersection is between two editions of one round; the two
        # edition names are carried as a single 'A|B' edition field.
        if not edition or "|" not in edition:
            errors.append(
                f"row #{index}: intersection requires edition 'A|B' naming "
                f"the two editions"
            )
            return None
        name_a, name_b = edition.split("|", 1)
        if not name_a or not name_b:
            errors.append(f"row #{index}: intersection has an empty edition name")
            return None
        if household is not None:
            errors.append(f"row #{index}: intersection must not name a household")
            return None
        if qty is None:
            errors.append(f"row #{index}: intersection requires a qty")
            return None
        return Observation(block, measure, round_no, name_a, name_b, None, qty)

    if edition is None:
        errors.append(f"row #{index}: {measure} requires an edition")
        return None
    if measure == SET_SIZE:
        if qty is None:
            errors.append(f"row #{index}: set_size requires a qty")
            return None
        if household is not None:
            errors.append(f"row #{index}: set_size must not name a household")
            return None
    else:  # NAMED_DELIVERED / NAMED_MISSED
        if not household:
            errors.append(f"row #{index}: {measure} requires a household")
            return None
        if qty is not None:
            errors.append(f"row #{index}: {measure} must not carry a qty")
            return None
    return Observation(
        block=block,
        measure=measure,
        round_no=round_no,
        edition=edition,
        household=household,
        qty=qty,
    )


def parse_observations(published_rows: Iterable[dict]) -> list[Observation]:
    """Parse the ``rows`` of an ``IngestionLibrary.publish()`` document.

    Only published (hence accepted, revision-resolved) rows are ever seen here;
    the cabinet is the only caller and it feeds the publish snapshot directly.
    Structural problems raise :class:`ContractError` with every issue listed.
    """
    observations: list[Observation] = []
    errors: list[str] = []
    for index, row in enumerate(published_rows):
        parsed = _parse_one(index, row, errors)
        if parsed is not None:
            observations.append(parsed)
    if errors:
        raise ContractError("; ".join(errors))
    return observations


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class LineEvaluation:
    """Worst-case evidence for one candidate line against a block."""

    line_id: str
    households: frozenset
    added_lower_bound: int
    # One arrangement attaining the minimum; lets a reviewer recompute the
    # bound and see exactly which line households were already covered.
    witness: dict
    possible_new_households: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "line_id": self.line_id,
            "households": sorted(self.households),
            "added_lower_bound": self.added_lower_bound,
            "witness": _witness_json(self.witness),
            "possible_new_households": sorted(self.possible_new_households),
        }


@dataclass
class BlockSettlement:
    """Settlement verdict for one disjoint block."""

    block: str
    universe: frozenset
    editions: tuple
    consistent: bool
    lower_bound: int
    witness: dict
    possible_missers: list
    proven_missed: list
    line_evaluations: dict
    history_count: int
    conflicts: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "block": self.block,
            "universe": sorted(self.universe),
            "editions": list(self.editions),
            "consistent": self.consistent,
            "lower_bound": self.lower_bound,
            "witness": _witness_json(self.witness),
            "possible_missers": sorted(self.possible_missers),
            "proven_missed": sorted(self.proven_missed),
            "history_count": self.history_count,
            "conflicts": list(self.conflicts),
            "line_evaluations": {
                key: value.to_json() for key, value in sorted(self.line_evaluations.items())
            },
        }


def _witness_json(witness: dict) -> dict:
    return {edition: sorted(households) for edition, households in sorted(witness.items())}


# ---------------------------------------------------------------------------
# Constraint model
# ---------------------------------------------------------------------------

@dataclass
class _BlockProblem:
    block: str
    universe: frozenset
    editions: tuple
    # edition -> forced-in / forced-out households
    forced_in: dict
    forced_out: dict
    # edition -> exact delivered-set size (at most one size claim per edition)
    sizes: dict
    # (edition_a, edition_b) -> exact intersection size
    intersections: dict
    observations: list


def _build_problem(
    block: str,
    universe: Iterable[str],
    observations: list[Observation],
) -> tuple[_BlockProblem, list[str]]:
    universe = frozenset(universe)
    forced_in: dict[str, set] = {}
    forced_out: dict[str, set] = {}
    sizes: dict[str, int] = {}
    intersections: dict[tuple[str, str], int] = {}
    edition_names: set[str] = set()
    conflicts: list[str] = []

    def require_household(obs: Observation, name: str | None) -> bool:
        if name is not None and name not in universe:
            conflicts.append(
                f"{obs.measure} names household {name!r} outside block {block!r} "
                f"roster {sorted(universe)}"
            )
            return False
        return True

    for obs in observations:
        if obs.measure == INTERSECTION:
            a, b = obs.edition, obs.edition_b
            edition_names.update((a, b))
            key = tuple(sorted((a, b)))
            if key in intersections and intersections[key] != obs.qty:
                conflicts.append(
                    f"intersection {a}/{b} claimed as both {intersections[key]} "
                    f"and {obs.qty}"
                )
            intersections[key] = obs.qty
            if obs.qty < 0:
                conflicts.append(f"intersection {a}/{b} has negative size {obs.qty}")
            continue

        edition = obs.edition
        edition_names.add(edition)
        if obs.measure == NAMED_DELIVERED:
            if require_household(obs, obs.household):
                forced_in.setdefault(edition, set()).add(obs.household)
        elif obs.measure == NAMED_MISSED:
            if require_household(obs, obs.household):
                forced_out.setdefault(edition, set()).add(obs.household)
        elif obs.measure == SET_SIZE:
            if obs.qty < 0 or obs.qty > len(universe):
                conflicts.append(
                    f"set_size {obs.qty} for {edition} is outside 0..{len(universe)}"
                )
            if edition in sizes and sizes[edition] != obs.qty:
                conflicts.append(
                    f"set size of {edition} claimed as both {sizes[edition]} "
                    f"and {obs.qty}"
                )
            sizes[edition] = obs.qty

    for edition in edition_names:
        both = forced_in.get(edition, set()) & forced_out.get(edition, set())
        if both:
            conflicts.append(
                f"household(s) {sorted(both)} claimed both delivered and missed "
                f"in edition {edition}"
            )
        if edition in sizes:
            size = sizes[edition]
            if len(forced_in.get(edition, set())) > size:
                conflicts.append(
                    f"edition {edition}: {len(forced_in[edition])} forced deliveries "
                    f"exceed declared size {size}"
                )
            if len(universe) - len(forced_out.get(edition, set())) < size:
                conflicts.append(
                    f"edition {edition}: declared size {size} cannot fit after "
                    f"{len(forced_out.get(edition, set()))} forced misses"
                )

    return _BlockProblem(
        block=block,
        universe=universe,
        editions=tuple(sorted(edition_names)),
        forced_in={k: frozenset(v) for k, v in forced_in.items()},
        forced_out={k: frozenset(v) for k, v in forced_out.items()},
        sizes=sizes,
        intersections=intersections,
        observations=list(observations),
    ), conflicts


def _candidate_sets(problem: _BlockProblem, edition: str) -> list[frozenset]:
    """All delivered subsets of one edition compatible with its local claims."""
    members = sorted(problem.universe)
    inside = problem.forced_in.get(edition, frozenset())
    outside = problem.forced_out.get(edition, frozenset())
    free = [h for h in members if h not in inside and h not in outside]
    if edition in problem.sizes:
        target = problem.sizes[edition]
        need = target - len(inside)
        if need < 0 or need > len(free):
            return []
        return [
            inside | frozenset(picked)
            for picked in combinations(free, need)
        ]
    # No size claim: any subset of the free households may be added.
    return [
        inside | frozenset(picked)
        for count in range(len(free) + 1)
        for picked in combinations(free, count)
    ]


def _intersections_hold(assignment: dict, problem: _BlockProblem) -> bool:
    for (a, b), expected in problem.intersections.items():
        if a not in assignment or b not in assignment:
            continue
        if len(assignment[a] & assignment[b]) != expected:
            return False
    return True


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

def _enumerate(
    problem: _BlockProblem,
    lines: dict[str, frozenset] | None = None,
) -> dict:
    """Backtrack over every compatible history, collecting worst-case stats.

    Bound: at most 256 candidate sets per edition and eight households, so the
    full cross-product remains local and cheap; intersections are checked as
    soon as both participating editions are assigned.
    """
    lines = lines or {}
    candidates = {
        edition: _candidate_sets(problem, edition) for edition in problem.editions
    }
    stats = {
        "history_count": 0,
        "min_union": None,
        "union_witness": None,
        # household -> True if it is outside the union in at least one history
        "can_miss": {h: False for h in problem.universe},
        "line_min_added": {line_id: None for line_id in lines},
        "line_witness": {line_id: None for line_id in lines},
        "line_possible_new": {line_id: set() for line_id in lines},
    }

    def visit(index: int, assignment: dict, union: frozenset) -> None:
        if index == len(problem.editions):
            stats["history_count"] += 1
            size = len(union)
            if stats["min_union"] is None or size < stats["min_union"]:
                stats["min_union"] = size
                stats["union_witness"] = dict(assignment)
            for household in problem.universe - union:
                stats["can_miss"][household] = True
            for line_id, line_households in lines.items():
                added = len(line_households - union)
                prior = stats["line_min_added"][line_id]
                if prior is None or added < prior:
                    stats["line_min_added"][line_id] = added
                    stats["line_witness"][line_id] = dict(assignment)
                stats["line_possible_new"][line_id] |= line_households - union
            return
        edition = problem.editions[index]
        for chosen in candidates[edition]:
            assignment[edition] = chosen
            if _intersections_hold(assignment, problem):
                visit(index + 1, assignment, union | chosen)
        assignment.pop(edition, None)

    visit(0, {}, frozenset())
    return stats


def settle_block(
    block: str,
    universe: Iterable[str],
    observations: Iterable[Observation],
    lines: dict[str, Iterable[str]] | None = None,
) -> BlockSettlement:
    """Settle one block: lower bound, a re-checkable witness, line evidence."""
    obs = [o for o in observations if o.block == block]
    line_sets = {
        line_id: frozenset(line_households)
        for line_id, line_households in (lines or {}).items()
    }
    for line_id, line_households in line_sets.items():
        unknown = line_households - frozenset(universe)
        if unknown:
            raise ContractError(
                f"line {line_id!r} leaves block {block!r}: {sorted(unknown)}"
            )
    problem, conflicts = _build_problem(block, universe, obs)
    if conflicts:
        return BlockSettlement(
            block=block,
            universe=frozenset(universe),
            editions=problem.editions,
            consistent=False,
            lower_bound=0,
            witness={},
            possible_missers=[],
            proven_missed=[],
            line_evaluations={},
            history_count=0,
            conflicts=conflicts,
        )

    stats = _enumerate(problem, line_sets)
    if stats["history_count"] == 0:
        return BlockSettlement(
            block=block,
            universe=frozenset(universe),
            editions=problem.editions,
            consistent=False,
            lower_bound=0,
            witness={},
            possible_missers=[],
            proven_missed=[],
            line_evaluations={},
            history_count=0,
            conflicts=["intersection constraints admit no compatible history"],
        )

    evaluations = {}
    for line_id, line_households in sorted(line_sets.items()):
        evaluations[line_id] = LineEvaluation(
            line_id=line_id,
            households=line_households,
            added_lower_bound=stats["line_min_added"][line_id],
            witness=stats["line_witness"][line_id],
            possible_new_households=sorted(stats["line_possible_new"][line_id]),
        )

    # A household is *proven missed* only when the station has published a
    # named non-receipt for it in every edition of the block: that is the
    # evidence the targeted cabinet requires before signing the household.
    # A miss in merely some editions still leaves other editions unproven.
    proven_missed = sorted(
        household
        for household in problem.universe
        if problem.editions
        and all(
            household in problem.forced_out.get(edition, frozenset())
            for edition in problem.editions
        )
    )
    return BlockSettlement(
        block=block,
        universe=frozenset(universe),
        editions=problem.editions,
        consistent=True,
        lower_bound=stats["min_union"],
        witness=stats["union_witness"],
        possible_missers=sorted(h for h, can in stats["can_miss"].items() if can),
        proven_missed=proven_missed,
        line_evaluations=evaluations,
        history_count=stats["history_count"],
    )


def settle(
    universes: dict[str, Iterable[str]],
    observations: Iterable[Observation],
    lines: dict[str, dict[str, Iterable[str]]] | None = None,
) -> dict[str, BlockSettlement]:
    """Settle every disjoint block.  Blocks never share households."""
    observations = list(observations)
    seen_blocks = {o.block for o in observations}
    blocks = set(universes) | seen_blocks
    unknown_blocks = seen_blocks - set(universes)
    if unknown_blocks:
        raise ContractError(
            f"observations reference block(s) with no roster: {sorted(unknown_blocks)}"
        )
    result = {}
    for block in sorted(blocks):
        block_lines = (lines or {}).get(block, {})
        result[block] = settle_block(block, universes[block], observations, block_lines)
    return result


# ---------------------------------------------------------------------------
# Re-checking and projected lower bounds (used when lines are signed)
# ---------------------------------------------------------------------------

def verify_arrangement(
    universe: Iterable[str],
    observations: Iterable[Observation],
    arrangement: dict[str, Iterable[str]],
) -> dict:
    """Independently re-check a witness arrangement against the observations.

    Returns every failed constraint; an empty ``violations`` list proves the
    arrangement is a genuine compatible history.
    """
    universe = frozenset(universe)
    assignment = {edition: frozenset(households) for edition, households in arrangement.items()}
    violations: list[str] = []
    for obs in observations:
        if obs.measure == NAMED_DELIVERED and obs.household not in assignment.get(obs.edition, frozenset()):
            violations.append(
                f"{obs.edition}: household {obs.household!r} must be delivered"
            )
        elif obs.measure == NAMED_MISSED and obs.household in assignment.get(obs.edition, frozenset()):
            violations.append(
                f"{obs.edition}: household {obs.household!r} must be missed"
            )
        elif obs.measure == SET_SIZE and len(assignment.get(obs.edition, frozenset())) != obs.qty:
            violations.append(
                f"{obs.edition}: size {len(assignment.get(obs.edition, frozenset()))} "
                f"!= claimed {obs.qty}"
            )
        elif obs.measure == INTERSECTION:
            actual = len(
                assignment.get(obs.edition, frozenset())
                & assignment.get(obs.edition_b, frozenset())
            )
            if actual != obs.qty:
                violations.append(
                    f"{obs.edition}/{obs.edition_b}: intersection {actual} != {obs.qty}"
                )
    for edition, chosen in assignment.items():
        if not chosen <= universe:
            violations.append(f"{edition}: households {sorted(chosen - universe)} leave roster")
    union = frozenset().union(*assignment.values()) if assignment else frozenset()
    return {
        "valid": not violations,
        "violations": violations,
        "union_size": len(union),
        "union": sorted(union),
    }


def lower_bound_with_extra(
    universe: Iterable[str],
    observations: Iterable[Observation],
    extra_sets: Iterable[Iterable[str]] | None = None,
) -> int:
    """Worst-case union size once fixed extra delivered sets are also booked.

    Used by the 补量柜 to project coverage after signing targeted households
    (singleton sets) or whole lines: each compatible history's union is
    extended by every extra set, and the minimum is taken.
    """
    extra = [frozenset(s) for s in (extra_sets or [])]
    universe = frozenset(universe)
    for extra_set in extra:
        if not extra_set <= universe:
            raise ContractError(
                f"extra set {sorted(extra_set)} leaves roster {sorted(universe)}"
            )
    obs = list(observations)
    problem, conflicts = _build_problem("projection", universe, obs)
    if conflicts:
        raise ContractError(f"cannot project over inconsistent evidence: {conflicts}")
    candidates = {
        edition: _candidate_sets(problem, edition) for edition in problem.editions
    }
    best: int | None = None

    def visit(index: int, assignment: dict, union: frozenset) -> None:
        nonlocal best
        if index == len(problem.editions):
            projected = union
            for extra_set in extra:
                projected = projected | extra_set
            if best is None or len(projected) < best:
                best = len(projected)
            return
        edition = problem.editions[index]
        for chosen in candidates[edition]:
            assignment[edition] = chosen
            if _intersections_hold(assignment, problem):
                visit(index + 1, assignment, union | chosen)
        assignment.pop(edition, None)

    visit(0, {}, frozenset())
    if best is None:
        raise ContractError("intersection constraints admit no compatible history")
    return best
