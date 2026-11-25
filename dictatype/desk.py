"""听打台：在允许的拍点与键盘状态下，找出与实录、压痕和行尾位置共同相容的
逐击记录，并给出各处（每联每格）可确定的覆盖先后。

求解方式：离线最多八击、两联，击序列空间可穷举。DFS 枚举所有满足键盘状态
约束的候选逐击记录，逐候选做三项证据核对：
  1. 声学：候选经 audiomix 实际混音仓渲染出的逐样本 PCM 必须与实录全等
     （重叠求和、单次恰半取偶舍入、削顶都在仓库侧完成）；
  2. 压痕：手录纸面压痕格（present/absent/unknown）与逐联回放相容；
  3. 行尾：终局车座位置必须与行尾读数相等。
所有相容解合并后，逐联逐格报告字符是否可确定，以及该格落锤覆盖的先后序
是否可确定（同拍多击先后不可观察时标记留疑）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from .acoustic import AcousticSession, BEAT_PERIOD_SAMPLES, mix_strikes_pcm
from .model import (
    Action, CopySheet, IndentationMark, Key, Strike,
    MAX_STRIKES, MAX_COPIES, MAX_BEAT,
)


class DeskError(ValueError):
    """听打台规格/证据非法。"""


# ----------------------------- 输入规格 ----------------------------- #

@dataclass(frozen=True)
class SessionSpec:
    """一次听打会话的固定规格。"""

    session_id: str
    keys: tuple[Key, ...]
    copies: tuple[CopySheet, ...]
    allowed_beats: tuple[int, ...]          # 允许落锤的拍点（升序、去重）
    start_position: int = 0                 # 车座起点（格号）
    line_width: int = 16                    # 行宽：车座必须始终落在 [0, line_width)
    max_strikes: int = MAX_STRIKES

    def __post_init__(self) -> None:
        if not self.session_id:
            raise DeskError("session_id 不能为空")
        if not self.keys:
            raise DeskError("至少需要一个键")
        ids = [k.key_id for k in self.keys]
        if len(set(ids)) != len(ids):
            raise DeskError("key_id 重复")
        if not (1 <= len(self.copies) <= MAX_COPIES):
            raise DeskError(f"离线联数必须在 1..{MAX_COPIES}")
        cids = [c.copy_id for c in self.copies]
        if len(set(cids)) != len(cids):
            raise DeskError("copy_id 重复")
        if not self.allowed_beats:
            raise DeskError("allowed_beats 不能为空")
        beats = sorted(set(self.allowed_beats))
        if any(b < 0 or b > MAX_BEAT for b in beats):
            raise DeskError(f"拍点必须落在 [0,{MAX_BEAT}]")
        object.__setattr__(self, "allowed_beats", tuple(beats))
        if not (0 <= self.start_position < self.line_width):
            raise DeskError("起点必须在 [0,line_width) 内")
        if not (1 <= self.max_strikes <= MAX_STRIKES):
            raise DeskError(f"击数上限必须在 1..{MAX_STRIKES}")
        for c in self.copies:
            if c.peel_after_strike is not None and c.peel_after_strike > self.max_strikes:
                raise DeskError("揭纸时刻超出击数上限")

    def key(self, key_id: str) -> Key:
        for k in self.keys:
            if k.key_id == key_id:
                return k
        raise DeskError(f"未知键: {key_id}")

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "keys": [k.to_json() for k in self.keys],
            "copies": [c.to_json() for c in self.copies],
            "allowed_beats": list(self.allowed_beats),
            "start_position": self.start_position,
            "line_width": self.line_width,
            "max_strikes": self.max_strikes,
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "SessionSpec":
        return SessionSpec(
            session_id=str(obj["session_id"]),
            keys=tuple(Key.from_json(k) for k in obj["keys"]),
            copies=tuple(CopySheet.from_json(c) for c in obj["copies"]),
            allowed_beats=tuple(int(b) for b in obj["allowed_beats"]),
            start_position=int(obj.get("start_position", 0)),
            line_width=int(obj.get("line_width", 16)),
            max_strikes=int(obj.get("max_strikes", MAX_STRIKES)),
        )


@dataclass(frozen=True)
class Evidence:
    """三路证据：实录 PCM、手录压痕格（逐联）、行尾位置。"""

    pcm: tuple[int, ...]
    indentation: dict[str, tuple[IndentationMark, ...]]
    line_end: int
    # 实录来源描述：内联样本（inline）或 audiomix 仓 render_id（archive_render）
    pcm_source: str = "inline"

    def __post_init__(self) -> None:
        if not self.pcm:
            raise DeskError("实录 PCM 不能为空")
        for v in self.pcm:
            if not (-32768 <= int(v) <= 32767):
                raise DeskError("实录 PCM 超出 int16 范围")
        if self.line_end is not None and self.line_end < 0:
            raise DeskError("行尾位置不能为负（None 表示该行尾证据不约束）")

    def to_json(self) -> dict[str, Any]:
        return {
            "pcm": list(self.pcm),
            "pcm_source": self.pcm_source,
            "indentation": {cid: [m.to_json() for m in marks]
                            for cid, marks in self.indentation.items()},
            "line_end": self.line_end,
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "Evidence":
        line_end = obj["line_end"]
        return Evidence(
            pcm=tuple(int(v) for v in obj["pcm"]),
            indentation={
                cid: tuple(IndentationMark.from_json(m) for m in marks)
                for cid, marks in obj.get("indentation", {}).items()},
            line_end=None if line_end is None else int(line_end),
            pcm_source=str(obj.get("pcm_source", "inline")),
        )


# ----------------------------- 逐联回放 ----------------------------- #

@dataclass
class CellState:
    """一联上一格的回放状态。"""

    glyph: str | None = None          # 当前顶联墨印字符（None=空格/从未印字）
    indent: bool = False              # 是否有物理压痕
    strike_seqs: list[int] = field(default_factory=list)   # 落过该格的击序号（覆盖先后）


@dataclass
class ReplayState:
    """一次逐击回放的完整状态。"""

    position: int
    cells: dict[str, dict[int, CellState]]   # copy_id -> cell -> state
    history: list[Strike]

    def copy(self) -> "ReplayState":
        cells = {
            cid: {cell: CellState(cs.glyph, cs.indent, list(cs.strike_seqs))
                  for cell, cs in row.items()}
            for cid, row in self.cells.items()}
        return ReplayState(self.position, cells, list(self.history))


def replay(spec: SessionSpec, strikes: Sequence[Strike]) -> ReplayState:
    """按仓库纸序逐击回放，返回每联每格的墨印、压痕与覆盖击序。

    重击覆盖只作用于当拍仍垫在下面的联；揭走的联冻结在揭纸时刻的状态。
    """
    pos = spec.start_position
    cells: dict[str, dict[int, CellState]] = {c.copy_id: {} for c in spec.copies}
    present = {c.copy_id: c for c in spec.copies}
    last_seq = 0
    for st in strikes:
        if st.seq != last_seq + 1:
            raise DeskError(f"击序号必须连续：期望 {last_seq + 1}，收到 {st.seq}")
        last_seq = st.seq
        key = spec.key(st.key_id)
        for cid, sheet in present.items():
            if not sheet.present_at(st.seq):
                continue
            row = cells[cid]
            cs = row.setdefault(pos, CellState())
            cs.indent = True                       # 落锤必有物理压痕（含抬色带）
            cs.strike_seqs.append(st.seq)
            if key.action is Action.PRINT_RIGHT:
                cs.glyph = key.glyph              # 后击覆盖先击（仅限仍垫着的联）
        if key.action is Action.PRINT_RIGHT:
            pos += 1
        elif key.action is Action.BACKSPACE:
            pos -= 1
        # RIBBON_LIFT：车座不动
        if not (0 <= pos < spec.line_width):
            raise DeskError(f"第 {st.seq} 击后车座 {pos} 越出 [0,{spec.line_width})")
    return ReplayState(position=pos, cells=cells, history=list(strikes))


def _indentation_compatible(spec: SessionSpec, state: ReplayState,
                            evidence: Evidence) -> bool:
    for cid, marks in evidence.indentation.items():
        if cid not in state.cells:
            raise DeskError(f"压痕证据引用未知联: {cid}")
        row = state.cells[cid]
        for mark in marks:
            if mark.state == "unknown":
                continue
            actual = row.get(mark.cell, CellState()).indent
            if mark.state == "present" and not actual:
                return False
            if mark.state == "absent" and actual:
                return False
    return True


# ----------------------------- 输出 ----------------------------- #

@dataclass
class CellVerdict:
    """某联某格的可确定性结论。"""

    copy_id: str
    cell: int
    glyphs: tuple[str | None, ...]               # 各相容解在该格的字符集合
    determinate: bool
    glyph: str | None                            # 可确定时的字符（含 None=确定空白）
    indent_determinate: bool
    indent_present: bool
    # 覆盖该格的击序号序列；各解不一致或同拍多击不可观察时留疑
    coverage_seqs: tuple[int, ...] | None
    coverage_determinate: bool
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "copy_id": self.copy_id, "cell": self.cell,
            "glyphs": list(self.glyphs), "determinate": self.determinate,
            "glyph": self.glyph,
            "indent_determinate": self.indent_determinate,
            "indent_present": self.indent_present,
            "coverage_seqs": self.coverage_seqs,
            "coverage_determinate": self.coverage_determinate,
            "note": self.note,
        }


@dataclass
class Transcript:
    """听打台结论。"""

    session_id: str
    solutions: tuple[tuple[Strike, ...], ...]
    verdicts: dict[str, list[CellVerdict]]
    line_end_determinate: bool
    line_end: int | None
    n_candidates_explored: int
    n_fast_mixes: int                       # 快速精确混音次数（缓存未命中）
    n_archive_confirms: int = 0             # 完整解的 audiomix 仓库确认次数

    @property
    def consistent(self) -> bool:
        return len(self.solutions) > 0

    @property
    def unique(self) -> bool:
        return len(self.solutions) == 1

    def solution_to_json(self, sol: Sequence[Strike]) -> list[dict[str, Any]]:
        return [st.to_json() for st in sol]

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "consistent": self.consistent,
            "unique": self.unique,
            "n_solutions": len(self.solutions),
            "solutions": [[st.to_json() for st in sol] for sol in self.solutions],
            "verdicts": {cid: [v.to_json() for v in vs]
                         for cid, vs in self.verdicts.items()},
            "line_end_determinate": self.line_end_determinate,
            "line_end": self.line_end,
            "n_candidates_explored": self.n_candidates_explored,
            "n_fast_mixes": self.n_fast_mixes,
            "n_archive_confirms": self.n_archive_confirms,
        }


# ----------------------------- 听打台求解器 ----------------------------- #

class TranscriptionDesk:
    """听打台：持有规格与声学工作台，穷举相容逐击记录。"""

    def __init__(self, spec: SessionSpec, workdir: str):
        self.spec = spec
        self.workdir = workdir
        self.acoustic = AcousticSession(workdir, spec.keys)
        self._explored = 0
        self._mixes = 0          # 快速精确混音次数（缓存未命中）
        self._archive_confirms = 0   # 完整解走 audiomix 仓库块渲染的确认次数
        # 同一击序列在不同 DFS 分支会被反复要求混音；其 PCM 只由击序列决定，
        # 按规范击序列哈希缓存，避免重复计算。
        self._pcm_cache: dict[str, np.ndarray] = {}

    def _prefix_key(self, strikes: Sequence[Strike]) -> str:
        canon = json.dumps([s.to_json() for s in strikes],
                           sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def _render_cached(self, strikes: Sequence[Strike],
                       use_reference: bool) -> np.ndarray:
        # DFS 前缀核对走快速精确混音（与仓库同求和/舍入/削顶语义），避免每个
        # 候选都付出 audiomix 块存储/SQLite 的固定开销。
        if not use_reference:
            key = self._prefix_key(strikes)
            if key not in self._pcm_cache:
                self._mixes += 1
                self._pcm_cache[key] = mix_strikes_pcm(self.spec.keys, strikes)
            return self._pcm_cache[key]
        # 完整解的独立复核仍走 audiomix 逐样本 Fraction 解释器（语义基准）。
        return self.acoustic.render(strikes, use_reference=True)["pcm"]

    def _block_render_confirm(self, strikes: Sequence[Strike]) -> np.ndarray:
        """完整解的仓库块渲染确认（内容寻址、真实 PCM 数据仓路径）。"""
        self._archive_confirms += 1
        return self.acoustic.render(strikes)["pcm"]

    def close(self) -> None:
        self.acoustic.close()

    def __enter__(self) -> "TranscriptionDesk":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 声学核对：前缀快速剪枝 + 完整解仓库/解释器三方确认 ---- #
    def _acoustic_prefix_ok(self, strikes: Sequence[Strike],
                            evidence_pcm: np.ndarray,
                            spec: "SessionSpec") -> tuple[bool, np.ndarray]:
        """渲染候选前缀，只核对“已封闭”的样本号，开放拍点上的样本不判。

        同拍多击的声样从同一样本号开始叠加，后击会改变当前拍点已渲染的
        样本，因此前缀里*最后一个拍点*及其之后的样本仍可被同拍后续击修改，
        属于开放区；只有严格早于最后拍点起点的样本才已封闭（后续击拍点
        不小于当前拍点，绝不会回写到更早的样本号）。
        长度相等（无开放样本）时才用参考解释器独立复核一次。
        """
        got = self._render_cached(strikes, use_reference=False)
        last_beat = max(st.beat for st in strikes)
        closed_end = last_beat * BEAT_PERIOD_SAMPLES   # 开放区起点
        # 封闭区必须落在双方都已有的样本号内：got 比 evidence 短时，只比到
        # got 的长度；比 evidence 长时，超出部分也要逐样本对得上（否则即便
        # 后续击不改写这些样本号，也永远无法相容）。
        n = min(closed_end, len(evidence_pcm))
        # 没有已封闭样本（所有击都挤在第一拍）时不剪枝；同拍后续击还能改写。
        if n > 0:
            cmp_n = min(n, len(got))
            if not np.array_equal(got[:cmp_n], evidence_pcm[:cmp_n]):
                return False, got
            # got 在封闭区内比实录更长：多出来的封闭样本已不可被改写
            if len(got) > len(evidence_pcm) and closed_end > len(evidence_pcm):
                return False, got
        return True, got

    def _acoustic_fullmatch(self, strikes: Sequence[Strike],
                            evidence_pcm: np.ndarray) -> bool:
        """完整候选的最终声学确认：快速混音 == 仓库块渲染 == 逐样本解释器 == 实录。

        前缀阶段只用快速混音剪枝；每个被接受为相容解的完整候选都在这里走
        一次真实 audiomix 数据仓块渲染与独立 Fraction 解释器，三方逐样本
        全等才采信，保证离线结论锚定在仓库实际 PCM 上。
        """
        fast = self._render_cached(strikes, use_reference=False)
        if len(fast) != len(evidence_pcm):
            return False
        block = self._block_render_confirm(strikes)
        ref = self._render_cached(strikes, use_reference=True)
        if not (np.array_equal(fast, block) and np.array_equal(block, ref)):
            raise RuntimeError("快速混音/仓库块渲染/逐样本解释器三者不一致")
        return bool(np.array_equal(block, evidence_pcm))

    def solve(self, evidence: Evidence, max_nodes: int = 200_000) -> Transcript:
        spec = self.spec
        self._explored = 0
        self._mixes = 0
        self._archive_confirms = 0
        self._pcm_cache = {}
        solutions: list[tuple[Strike, ...]] = []
        evidence_pcm = np.asarray(evidence.pcm, dtype=np.int16)
        target_len = len(evidence.pcm)

        # DFS：按击序号连续枚举；每拍允许放多击，也允许空拍。
        present = {c.copy_id: c for c in spec.copies}

        def apply_strike(cells, pos, st, key):
            """在增量状态上落一锤：仍垫着的联记压痕，印字键覆盖墨印。"""
            ncells = _snapshot(cells)
            for cid, sheet in present.items():
                if not sheet.present_at(st.seq):
                    continue
                cs = ncells[cid].setdefault(pos, CellState())
                cs.indent = True
                cs.strike_seqs.append(st.seq)
                if key.action is Action.PRINT_RIGHT:
                    cs.glyph = key.glyph
            return ncells

        def dfs(seq: int, beat_idx: int, prefix: list[Strike],
                pos: int, cells: dict[str, dict[int, CellState]]) -> None:
            # seq 表示“下一击的序号”；seq > max_strikes 时前缀已达最大长度，
            # 只做完整候选核对，不再扩展。
            self._explored += 1
            if self._explored > max_nodes:
                raise DeskError(f"候选节点超过 {max_nodes}，离线八击规模不应触发")

            # 长度上界剪枝：声样非空，追加击只会维持或加长渲染长度；
            # 前缀渲染长度一旦超过实录即不可能相容。下界在扩展循环里按
            # 每个候选击的实际拍点长度（trial_len）逐分支剪枝。
            cur_len = 0 if not prefix else _rendered_sample_count(spec, prefix)
            if cur_len > len(evidence.pcm):
                return

            # 终止 1：以当前前缀作为完整候选核对。
            # 扩展时只核对了已封闭样本区；到达目标长度后必须做一次完整
            # 声学确认（仓库渲染 == 逐样本解释器 == 实录），再验行尾与压痕。
            line_end_ok = evidence.line_end is None or pos == evidence.line_end
            if (prefix and cur_len == target_len and line_end_ok
                    and _indentation_compatible(
                        spec, ReplayState(pos, _snapshot(cells), prefix), evidence)
                    and self._acoustic_fullmatch(prefix, evidence_pcm)):
                solutions.append(tuple(prefix))

            if seq > spec.max_strikes:
                return

            # 终止 2：继续枚举下一击，拍点只能与上一击相同或更晚
            for bi in range(beat_idx, len(spec.allowed_beats)):
                beat = spec.allowed_beats[bi]
                for key in spec.keys:
                    npos = _next_position(pos, key.action)
                    if not (0 <= npos < spec.line_width):
                        continue
                    st = Strike(seq=seq, beat=beat, key_id=key.key_id)
                    # 追加后的渲染长度（旧前缀与新击声样末尾取较大者）
                    added_len = beat * BEAT_PERIOD_SAMPLES + len(key.sample)
                    trial_len = (added_len if not prefix
                                 else max(_rendered_sample_count(spec, prefix), added_len))
                    if trial_len > target_len:
                        continue   # 上界：已超实录
                    ncells = apply_strike(cells, pos, st, key)
                    prefix.append(st)
                    # 声学前缀剪枝：仅已封闭样本与实录不符才砍子树；
                    # 同拍开放区留给后续击叠加，不能提前判死。
                    audio_ok, _ = self._acoustic_prefix_ok(prefix, evidence_pcm, spec)
                    if audio_ok:
                        dfs(seq + 1, bi, prefix, npos, ncells)
                    prefix.pop()

        dfs(1, 0, [], spec.start_position,
            {c.copy_id: {} for c in spec.copies})
        return self._build_transcript(solutions, evidence, evidence_pcm)

    def _build_transcript(self, solutions: list[tuple[Strike, ...]],
                          evidence: Evidence,
                          evidence_pcm: np.ndarray) -> Transcript:
        spec = self.spec
        verdicts: dict[str, list[CellVerdict]] = {}
        if not solutions:
            return Transcript(spec.session_id, (), {}, False, None,
                              self._explored, self._mixes, self._archive_confirms)

        replayed = [replay(spec, sol) for sol in solutions]
        line_ends = {r.position for r in replayed}
        all_cells = sorted({(cid, cell) for r in replayed for cid, row in r.cells.items()
                            for cell in row})
        for cid in [c.copy_id for c in spec.copies]:
            rows: list[CellVerdict] = []
            for (ccid, cell) in all_cells:
                if ccid != cid:
                    continue
                glyphs = tuple(sorted(
                    {r.cells[cid].get(cell, CellState()).glyph for r in replayed},
                    key=lambda g: ("\x00" if g is None else g)))
                indents = {r.cells[cid].get(cell, CellState()).indent for r in replayed}
                cov_sets = [tuple(r.cells[cid].get(cell, CellState()).strike_seqs)
                            for r in replayed]
                # 覆盖先后可确定：所有相容解的覆盖击序集合相同，且每个解里
                # 这些击都落在不同拍点（同拍多击的拍点内先后不可观察）。
                cov_unique = len(set(cov_sets)) == 1
                beat_observable = all(
                    _beat_order_observable(sol, seqs)
                    for sol, seqs in zip(solutions, cov_sets))
                cov_det = cov_unique and beat_observable
                note = ""
                if cov_unique and not cov_det:
                    note = "同拍多击：拍点内先后不可由声音/压痕区分"
                rows.append(CellVerdict(
                    copy_id=cid, cell=cell, glyphs=glyphs,
                    determinate=len(glyphs) == 1,
                    glyph=glyphs[0] if len(glyphs) == 1 else None,
                    indent_determinate=len(indents) == 1,
                    indent_present=next(iter(indents)) if len(indents) == 1 else True,
                    coverage_seqs=cov_sets[0] if len(set(cov_sets)) == 1 else None,
                    coverage_determinate=cov_det, note=note))
            verdicts[cid] = rows
        return Transcript(
            session_id=spec.session_id,
            solutions=tuple(solutions),
            verdicts=verdicts,
            line_end_determinate=len(line_ends) == 1,
            line_end=next(iter(line_ends)) if len(line_ends) == 1 else None,
            n_candidates_explored=self._explored,
            n_fast_mixes=self._mixes,
            n_archive_confirms=self._archive_confirms)


# ----------------------------- 回放辅助 ----------------------------- #

def _next_position(pos: int, action: Action) -> int:
    if action is Action.PRINT_RIGHT:
        return pos + 1
    if action is Action.BACKSPACE:
        return pos - 1
    return pos


def _rendered_sample_count(spec: SessionSpec, strikes: Sequence[Strike]) -> int:
    """击序列经 audiomix 渲染后的样本数（拍点起点 + 最长末拍声样）。

    与 ``AcousticSession.total_duration`` 同公式，用于在昂贵的仓库渲染前
    先按长度剪枝。
    """
    last_beat = max(st.beat for st in strikes)
    longest = max(len(spec.key(st.key_id).sample) for st in strikes
                  if st.beat == last_beat)
    return last_beat * BEAT_PERIOD_SAMPLES + longest


def _snapshot(cells: dict[str, dict[int, CellState]]) -> dict[str, dict[int, CellState]]:
    return {cid: {cell: CellState(cs.glyph, cs.indent, list(cs.strike_seqs))
                  for cell, cs in row.items()} for cid, row in cells.items()}


def _beat_order_observable(strikes: Sequence[Strike],
                           seqs: Sequence[int]) -> bool:
    """覆盖击序中任意两击若落在同一拍点，则拍点内先后不可观察。"""
    beat_of = {st.seq: st.beat for st in strikes}
    beats = [beat_of[s] for s in seqs if s in beat_of]
    return len(set(beats)) == len(beats)
