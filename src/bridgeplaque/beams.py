"""梁片组装：读 pqscan 真正命中的结果 Parquet，把梁号、读数和来源行串起来。

案卷中每行是一个取样片段（同一根梁可能断成两截、中间漏读一环）：
    segment   桥段
    beam_id   梁号
    fragment  片段号
    start_pos 本片首环在梁内的环位（自最内一环计 1，漏读环也占位置）
    widths    本片逐环宽窄读数（窄/宽）
    bark_edge 本片末环是否为带皮末轮（1/0）

拼接规则：
- 同一梁号的全部片段属于同一根树（同一根实物梁），断口两边必须接回：
  按 start_pos 排序后，相邻片段之间恰好空一环视为一处漏读，以 "?" 占位接回；
  空超过一环或环位重叠，说明案卷自相矛盾，拒绝签发。
- 同树的两块碎片只算一份佐证：每根梁最终只产出一条 BeamRecord，
  候选年、带皮末轮都按整梁（而非按片段）进入后续求解。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pyarrow.parquet as pq

from . import PlaqueError
from .band import GAP, ReferenceBand

REQUIRED_COLUMNS = ("segment", "beam_id", "fragment", "start_pos", "widths", "bark_edge")


@dataclass(frozen=True)
class SourceRow:
    """一行案卷在 Parquet 中的物理来源（pqscan 溯源列）。"""

    file: str
    row_group: int
    row_in_group: int

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "row_group": self.row_group,
                "row_in_group": self.row_in_group}


@dataclass
class Fragment:
    beam_id: str
    fragment: str
    start_pos: int
    widths: str
    bark_edge: bool
    source: SourceRow


@dataclass
class BeamRecord:
    """一根梁拼接后的完整证据（同树多片段只此一份）。"""

    beam_id: str
    pos0: int                       # 拼接串首环对应的梁内环位
    pattern: str                    # 拼接后的逐环宽窄，漏读处为 "?"
    bark_edge: bool                 # 最外片段末环是否带皮末轮
    fragments: list[Fragment] = field(default_factory=list)
    source_rows: list[SourceRow] = field(default_factory=list)

    @property
    def last_pos(self) -> int:
        """拼接串末环的梁内环位。"""
        return self.pos0 + len(self.pattern) - 1

    def citable_pattern(self, covered: set[int]) -> str:
        """去掉被榫盖住、不得引用的环位后的可引用读数串（自环位 1 起截尾）。

        被盖住的只能是末尾连续一段（榫头盖住梁端外圈）；否则案卷与工单矛盾。
        """
        if not covered:
            return self.pattern
        first_covered = min(covered)
        if covered != set(range(first_covered, self.last_pos + 1)):
            raise PlaqueError(
                f"梁 {self.beam_id} 被盖环位 {sorted(covered)} 不是末尾连续段，"
                f"与拼接结果（{self.pos0}..{self.last_pos}）矛盾"
            )
        keep = first_covered - self.pos0
        if keep <= 0:
            raise PlaqueError(f"梁 {self.beam_id} 的全部读数都被榫盖住，无可引用环")
        return self.pattern[:keep]


def load_hit_fragments(results_parquet: str) -> list[Fragment]:
    """读 pqscan 结果 Parquet（只含真正命中行），逐行还原取样片段。"""
    table = pq.read_table(results_parquet)
    missing = [c for c in REQUIRED_COLUMNS if c not in table.schema.names]
    if missing:
        raise PlaqueError(f"结果 Parquet 缺少案卷列: {missing}")
    fragments: list[Fragment] = []
    for row in table.to_pylist():
        fragments.append(
            Fragment(
                beam_id=row["beam_id"],
                fragment=row["fragment"],
                start_pos=row["start_pos"],
                widths=row["widths"],
                bark_edge=bool(row["bark_edge"]),
                source=SourceRow(
                    file=row["_source_file"],
                    row_group=row["_source_row_group"],
                    row_in_group=row["_source_row_in_group"],
                ),
            )
        )
    return fragments


def stitch_beam(fragments: list[Fragment]) -> BeamRecord:
    """把同一根梁的片段按环位接回同一根树；断口空一环以 "?" 占位。"""
    beam_ids = {f.beam_id for f in fragments}
    if len(beam_ids) != 1:
        raise PlaqueError(f"一次只能拼接同一根梁，收到: {sorted(beam_ids)}")
    beam_id = fragments[0].beam_id

    by_pos: dict[int, str] = {}
    bark_edge = False
    last_end = -1
    for frag in sorted(fragments, key=lambda f: f.start_pos):
        if frag.start_pos <= last_end:
            raise PlaqueError(
                f"梁 {beam_id} 片段 {frag.fragment} 环位 {frag.start_pos} 与前片重叠，无法接回"
            )
        for i, ch in enumerate(frag.widths):
            by_pos[frag.start_pos + i] = ch
        if frag.start_pos + len(frag.widths) - 1 > last_end:
            last_end = frag.start_pos + len(frag.widths) - 1
            bark_edge = frag.bark_edge  # 最外片段的末环标记

    pos0 = min(by_pos)
    pattern_chars: list[str] = []
    for pos in range(pos0, last_end + 1):
        ch = by_pos.get(pos)
        if ch is None:
            # 断口：两边读数之间缺环。只允许一处漏读一环，否则无法确认同一根树。
            if pos + 1 not in by_pos:
                raise PlaqueError(
                    f"梁 {beam_id} 在环位 {pos} 处断口超过一环，断口两边无法接回同一根树"
                )
            ch = GAP
        pattern_chars.append(ch)

    return BeamRecord(
        beam_id=beam_id,
        pos0=pos0,
        pattern="".join(pattern_chars),
        bark_edge=bark_edge,
        fragments=sorted(fragments, key=lambda f: f.start_pos),
        source_rows=[f.source for f in sorted(fragments, key=lambda f: f.start_pos)],
    )


def build_beam_records(fragments: list[Fragment]) -> dict[str, BeamRecord]:
    """按梁号归并片段；同树两块碎片在此并成一份佐证。"""
    by_beam: dict[str, list[Fragment]] = {}
    for frag in fragments:
        by_beam.setdefault(frag.beam_id, []).append(frag)
    return {beam_id: stitch_beam(frags) for beam_id, frags in by_beam.items()}


def candidate_last_ring_years(record: BeamRecord, band: ReferenceBand,
                              covered: set[int] | None = None) -> list[int]:
    """梁片（可选地去掉被榫盖住的环位后）与参照带交叉对齐出的末环候选年。"""
    pattern = record.citable_pattern(covered or set())
    return band.align(pattern)
