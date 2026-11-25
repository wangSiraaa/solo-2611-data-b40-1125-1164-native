"""气象传真声带：短音脉冲编码、实际发布/渲染/重读与相位约束解图。

物理约定（与同步脉冲、行长、独立校准条相容）：
- 每个录段（segment）是一段独立录音，全段共用**一种**行相位——行首可能整体
  缺失 0..row_len-1 个脉冲，但段内每一行缺得一样多；不允许逐格各挑一次对齐。
- 段首是一条与行长等长的**独立校准参考行**（符号已知、按相位 0 发射）。
  参考槽被读坏（静音/无法辨别）时不提供约束；完好槽把数据行相对参考行的
  相位候选收窄到与之相容的子集，坏到没有完好槽时只能枚举全部相位。
- 随后每行恰好 ``row_len`` 个符号槽；脉冲按 ``slot`` 计时，解码以能量峰值落槽。
- 小字典三类：高值 ``H``、低值 ``L``、漏读 ``MISSING``（脉冲损坏/无法辨别）。
  漏读不是第四种格值，重读后按“仍可能是 H 或 L”参与后续枚举。

所有发布都走真实 PCM WAV（16bit 单声道），每个样本保留卷号与样本出处，
时间轴裁切/变速/重录一行由 :mod:`audiomix.faxedit` 基于时间轴产品完成。
"""
from __future__ import annotations

import json
import os
import wave
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

import numpy as np

from .assets import asset_id_of, write_wav, read_wav

# ----------------------------- 小字典 ----------------------------- #
HIGH = "H"
LOW = "L"
MISSING = "MISSING"
VALUES = (HIGH, LOW)
DICT = (HIGH, LOW, MISSING)

# 符号 -> 基频（Hz）。短音脉冲，频率区分高/低；漏读不发载波（静音槽）。
SYMBOL_FREQ = {HIGH: 1200.0, LOW: 600.0}
SAMPLE_RATE = 16000
SLOT_SECONDS = 0.02                      # 每个符号槽 20ms（320 样本）
SLOT_SAMPLES = int(SAMPLE_RATE * SLOT_SECONDS)
SYNC_GAP_SECONDS = 0.01                  # 行首同步间隔（静音），把各行脉冲分开
SYNC_GAP_SAMPLES = int(SAMPLE_RATE * SYNC_GAP_SECONDS)
# 判读门限：槽内主频能量占比低于该值 -> 无法辨别 -> MISSING
ENERGY_GATE = 0.55
MAX_MISSING_ENUM = 25                   # 漏读枚举上限（两展内每幅不超过 5x5=25 格）


class FaxError(ValueError):
    pass


# ----------------------------- 脉冲合成 ----------------------------- #
def _pulse(symbol: str) -> np.ndarray:
    """单个符号槽的真实波形：H/L 为带短淡入淡出的正弦脉冲，MISSING 为静音。"""
    n = SLOT_SAMPLES
    if symbol == MISSING:
        return np.zeros(n, dtype=np.int16)
    freq = SYMBOL_FREQ[symbol]
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
    env = np.minimum(1.0, np.minimum(t / 0.002, (n / SAMPLE_RATE - t) / 0.002))
    env = np.clip(env, 0.0, 1.0)
    x = 0.6 * 32767.0 * env * np.sin(2 * np.pi * freq * t)
    return np.rint(x).astype(np.int16)


def _silence(samples: int) -> np.ndarray:
    return np.zeros(samples, dtype=np.int16)


# ----------------------------- 录段/卷数据结构 ----------------------------- #
@dataclass
class Segment:
    """一次录音段：等长校准参考行 + 若干数据行；段内共用一个行相位 phase。

    ``rows`` 为发射端的逻辑符号行（不含相位、不含校准条），每行 row_len 个。
    ``calibration`` 是与行长等长、内容已知的独立参考行（按相位 0 发射）。
    ``phase`` 是该段已知的行首缺失脉冲数（发射事实）；重读端只能从校准条推断，
    推断不唯一时得到候选集合，绝不假定等于发射事实。
    """
    seg_id: str
    rows: list[list[str]]
    phase: int = 0
    calibration: list[str] | None = None
    corrupted: set[tuple[int, int]] = field(default_factory=set)   # 被读坏的 (行,列)
    calibration_corrupted: set[int] = field(default_factory=set)   # 被读坏的参考槽

    def __post_init__(self) -> None:
        if not self.rows:
            raise FaxError("录段至少要有一行")
        width = len(self.rows[0])
        for r in self.rows:
            if len(r) != width:
                raise FaxError("段内行长不一致")
            for s in r:
                if s not in (HIGH, LOW):
                    raise FaxError(f"发射符号只能是 H/L，收到 {s!r}")
        if not (0 <= self.phase < width):
            raise FaxError("行相位必须在 [0, row_len) 内")
        if self.calibration is None:
            # 默认参考行（宽 <=5；更大宽度按 H/L 交替延展）。这些序列满足：
            # 缺行首后只要还剩 >=2 个完好槽，就能把相位唯一锁定；只剩 1 个完好
            # 槽时二进制字典下存在固有两位歧义，按“仍可能读法”保留两个候选，
            # 校准槽再被读坏则候选集进一步扩大，解图端绝不硬猜。
            defaults = {
                1: [HIGH],
                2: [HIGH, LOW],
                3: [HIGH, HIGH, LOW],
                4: [HIGH, HIGH, LOW, LOW],
                5: [HIGH, HIGH, LOW, HIGH, LOW],
            }
            if width in defaults:
                self.calibration = list(defaults[width])
            else:
                self.calibration = [HIGH if k % 2 == 0 else LOW for k in range(width)]
        if len(self.calibration) != width:
            raise FaxError("校准参考行必须与行长等长")
        for s in self.calibration:
            if s not in (HIGH, LOW):
                raise FaxError("校准条只放 H/L 标准脉冲")

    @property
    def row_len(self) -> int:
        return len(self.rows[0])

    @property
    def n_rows(self) -> int:
        return len(self.rows)


@dataclass
class Reel:
    """一卷气象传真：卷号 + 若干录段（各段可有自己的相位）。"""
    reel_id: str
    segments: list[Segment]
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "reel_id": self.reel_id,
            "note": self.note,
            "segments": [
                {
                    "seg_id": s.seg_id,
                    "phase": s.phase,
                    "calibration": list(s.calibration),
                    "rows": [list(r) for r in s.rows],
                    "corrupted": [list(c) for c in sorted(s.corrupted)],
                    "calibration_corrupted": sorted(s.calibration_corrupted),
                }
                for s in self.segments
            ],
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "Reel":
        segs = []
        for so in obj["segments"]:
            segs.append(Segment(
                seg_id=so["seg_id"],
                rows=[list(r) for r in so["rows"]],
                phase=int(so.get("phase", 0)),
                calibration=so.get("calibration"),
                corrupted={tuple(c) for c in so.get("corrupted", [])},
                calibration_corrupted=set(so.get("calibration_corrupted", [])),
            ))
        return Reel(reel_id=obj["reel_id"], segments=segs, note=obj.get("note", ""))


# ----------------------------- 真实声带合成 ----------------------------- #
def _segment_waveform(seg: Segment) -> np.ndarray:
    """把一个录段合成为真实 PCM：校准参考行 + 数据行，全部按同一行相位缺行首。

    行相位物理含义：整段录音行首整体缺失 ``phase`` 个脉冲，校准行与每行数据缺得
    一样多——这正是“同一录段共用一种相位、不能逐格各挑一次对齐”。两种静音必须
    可区分：缺行首表现为行同步间隔变长（不占位），脉冲读坏表现为槽位上的静音槽。
    每行保留固定 ``row_len`` 个槽位计时，数据行缺失的行首槽记为漏读；校准行用
    变长同步间隔表达缺行首，其首个出现的脉冲落在槽 ``phase`` 上，据此可定位相位。
    """
    pieces: list[np.ndarray] = []
    row_len = seg.row_len

    # 校准行：同步间隔额外吞掉 phase 个槽（缺行首，无静音槽），剩余槽按原槽距计时
    pieces.append(_silence(SYNC_GAP_SAMPLES + seg.phase * SLOT_SAMPLES))
    for k in range(seg.phase, row_len):
        c = (k - seg.phase) % row_len
        if k in seg.calibration_corrupted:
            pieces.append(_pulse(MISSING))
        else:
            pieces.append(_pulse(seg.calibration[c]))
    # 数据行：固定 row_len 槽，行首 phase 槽为漏读静音槽，逻辑列 c 落在槽 (c+phase)
    for i, row in enumerate(seg.rows):
        pieces.append(_silence(SYNC_GAP_SAMPLES))
        for k in range(row_len):
            if k < seg.phase or (i, k) in seg.corrupted:
                pieces.append(_pulse(MISSING))
            else:
                c = (k - seg.phase) % row_len
                pieces.append(_pulse(row[c]))
    return np.concatenate(pieces).astype(np.int16)


def reel_pcm(reel: Reel) -> np.ndarray:
    """整卷渲染为一段 16kHz 单声道 int16 PCM（段间留一个同步间隔）。"""
    chunks = []
    for seg in reel.segments:
        chunks.append(_segment_waveform(seg))
        chunks.append(_silence(SYNC_GAP_SAMPLES))
    return np.concatenate(chunks).astype(np.int16)


def publish_reel(reel: Reel, path: str) -> dict[str, Any]:
    """实际发布：写 WAV 声带 + 并排写卷元数据（.fax.json），返回发布记录。

    卷号与每个样本的出处由元数据保留；WAV 正文做 SHA-256 内容寻址身份。
    """
    pcm = reel_pcm(reel)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    write_wav(path, pcm, SAMPLE_RATE)
    digest = asset_id_of(pcm.tobytes())
    meta = {
        "format": "audiomix.fax/1",
        "reel": reel.to_json(),
        "wav": os.path.basename(path),
        "sample_rate": SAMPLE_RATE,
        "sample_count": len(pcm),
        "pcm_digest": digest,
    }
    meta_path = path + ".fax.json"
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    return {"path": path, "meta_path": meta_path, "pcm_digest": digest,
            "sample_count": len(pcm)}


def load_published_reel(path: str) -> tuple[Reel, np.ndarray, dict[str, Any]]:
    """读回发布物：校验 WAV 摘要后返回 (卷结构, 实际 PCM, 元数据)。"""
    meta = json.load(open(path + ".fax.json", encoding="utf-8"))
    pcm, rate = read_wav(path)
    if rate != SAMPLE_RATE:
        raise FaxError(f"传真声带采样率必须为 {SAMPLE_RATE}")
    if asset_id_of(pcm.tobytes()) != meta["pcm_digest"]:
        raise FaxError("WAV 正文与发布摘要不符（声带被替换）")
    return Reel.from_json(meta["reel"]), pcm, meta


# ----------------------------- 脉冲重读 ----------------------------- #
def _read_slot(pcm: np.ndarray, start: int) -> str:
    """重读单个符号槽：用两个字典频率的 Goertzel 能量判别 H/L/MISSING。"""
    x = pcm[start:start + SLOT_SAMPLES].astype(np.float64)
    if len(x) < SLOT_SAMPLES:
        x = np.concatenate([x, np.zeros(SLOT_SAMPLES - len(x))])
    total_energy = float(np.dot(x, x))
    if total_energy < 1e-6:
        return MISSING
    scores = {}
    n = SLOT_SAMPLES
    idx = np.arange(n)
    for sym in VALUES:
        k = int(round(SYMBOL_FREQ[sym] * n / SAMPLE_RATE))
        basis = np.exp(-2j * np.pi * k * idx / n)
        # 槽内该频能量占比：纯正弦槽趋近 1，静音/损坏槽趋近 0
        scores[sym] = 2.0 * float(np.abs(np.dot(x, basis)) ** 2) / n
    best = max(scores, key=scores.get)
    if scores[best] / total_energy < ENERGY_GATE:
        return MISSING
    return best


def _row_stride(row_len: int) -> int:
    return SYNC_GAP_SAMPLES + row_len * SLOT_SAMPLES


def _slot_starts(seg_meta: dict[str, Any]) -> list[list[int]]:
    """根据版面几何（校准行长、行长、槽宽）算出每个数据行各槽的样本起点。

    只依赖同步脉冲/行长等相容几何，不依赖发射端的 phase 事实。校准行占一个标准
    行步长（内部用变长同步间隔表达缺行首，总时长不变）。
    """
    row_len = len(seg_meta["rows"][0])
    offset = _row_stride(row_len)
    stride = _row_stride(row_len)
    starts = []
    for i in range(len(seg_meta["rows"])):
        row_start = offset + i * stride + SYNC_GAP_SAMPLES
        starts.append([row_start + j * SLOT_SAMPLES for j in range(row_len)])
    return starts


@dataclass
class SegmentRead:
    """一段声带的实际重读结果。"""
    seg_id: str
    row_len: int
    calibration_read: list[str]                  # 校准条实际读到的符号
    observed_rows: list[list[str]]               # 按收到的槽顺序读到的符号
    candidate_phases: list[int]                  # 校准条仍允许的全部候选相位
    phase_locked: bool

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def read_segment_waveform(pcm: np.ndarray, seg_meta: dict[str, Any]) -> SegmentRead:
    """只凭实际波形与版面几何重读一段（不偷看发射 phase）。

    校准行几何：行同步间隔之后是 ``row_len`` 个候选槽位；缺行首把前 ``phase`` 个
    脉冲整个吞掉（间隔变长、无静音槽），故首个有脉冲的槽位就是 ``phase``。读坏槽
    在槽位上仍是静音，与“被吞掉”的区别由后续脉冲的槽距恢复：把每个非漏读槽
    (k, 读数) 与已知参考序列循环对齐，全部相容的 p 构成候选相位；证据全无则
    保留全部相位作为“仍可能读法”。
    """
    row_len = len(seg_meta["rows"][0])
    cal_read = []
    for k in range(row_len):
        start = SYNC_GAP_SAMPLES + k * SLOT_SAMPLES
        cal_read.append(_read_slot(pcm, start))
    reference = seg_meta["calibration"]
    observed = []
    for starts in _slot_starts(seg_meta):
        observed.append([_read_slot(pcm, st) for st in starts])

    candidates = []
    for p in range(row_len):
        ok = True
        saw_evidence = False
        for k, sym in enumerate(cal_read):
            if sym == MISSING:
                continue
            saw_evidence = True
            if sym != reference[(k - p) % row_len]:
                ok = False
                break
        if ok and (saw_evidence or all(s == MISSING for s in cal_read)):
            candidates.append(p)
    return SegmentRead(
        seg_id=seg_meta["seg_id"], row_len=row_len, calibration_read=cal_read,
        observed_rows=observed, candidate_phases=candidates,
        phase_locked=len(candidates) == 1)


def read_reel_waveform(pcm: np.ndarray, reel_meta: dict[str, Any]) -> list[SegmentRead]:
    """重读整卷：按段几何逐段切分 PCM（段起始样本由前面各段长度累加）。"""
    out = []
    cursor = 0
    for seg_meta in reel_meta["segments"]:
        row_len = len(seg_meta["rows"][0])
        n_rows = len(seg_meta["rows"]) + 1          # 含 1 条校准参考行
        seg_len = n_rows * _row_stride(row_len)
        piece = pcm[cursor:cursor + seg_len]
        out.append(read_segment_waveform(piece, seg_meta))
        cursor += seg_len + SYNC_GAP_SAMPLES
    return out


# ----------------------------- 相位校正与格值枚举 ----------------------------- #
def _unshift(observed_row: list[str], phase: int) -> list[str]:
    """按候选相位把收到的行还原为逻辑列顺序。

    缺行首模型：逻辑列 c 落在槽 (c+phase)，行尾 phase 个逻辑列被同步间隔截断，
    行首 phase 个槽为漏读。故只恢复列 0..w-1-phase；被截断的列记为漏读，
    交由 :func:`possible_grids` 在 H/L 间枚举。
    """
    width = len(observed_row)
    out = [MISSING] * width
    for c in range(width - phase):
        out[c] = observed_row[c + phase]
    return out


def possible_grids(reads: Iterable[SegmentRead]) -> list[list[list[str]]]:
    """枚举在“全部仍可能读法”下的整幅格值（去重）。

    约束：
    - 每个录段从其候选相位里取**同一个**相位（段内共用，不逐格挑）；
    - MISSING 槽在 H/L 间任意，但总数受 :data:`MAX_MISSING_ENUM` 限制；
    - 非漏读槽的读数在任何读法下都固定。
    """
    reads = list(reads)
    width = reads[0].row_len
    if any(r.row_len != width for r in reads):
        raise FaxError("各段行长不一致，无法拼成一幅图")
    if any(not r.candidate_phases for r in reads):
        return []                       # 校准条排除了全部相位 -> 无任何相容读法

    # 先按“段 × 候选相位”生成校正后的观测阵（含 MISSING 占位）
    options: list[list[list[list[str]]]] = []
    total_missing = 0
    for rd in reads:
        per_phase = []
        for p in rd.candidate_phases:
            grid = [_unshift(row, p) for row in rd.observed_rows]
            per_phase.append(grid)
            total_missing += sum(row.count(MISSING) for row in grid)
        options.append(per_phase)
    if total_missing > MAX_MISSING_ENUM:
        raise FaxError(
            f"漏读 {total_missing} 格超过枚举上限 {MAX_MISSING_ENUM}，拒绝猜测读法")

    results: set[tuple[tuple[str, ...], ...]] = set()

    def fill(grid: list[list[str]]) -> None:
        miss = [(i, j) for i in range(len(grid)) for j in range(width)
                if grid[i][j] == MISSING]
        if not miss:
            results.add(tuple(tuple(r) for r in grid))
            return
        for bits in range(1 << len(miss)):
            g = [list(r) for r in grid]
            for k, (i, j) in enumerate(miss):
                g[i][j] = LOW if ((bits >> k) & 1) else HIGH
            results.add(tuple(tuple(r) for r in g))

    def choose(seg_idx: int, acc: list[list[list[str]]]) -> None:
        if seg_idx == len(options):
            # 多段按行序首尾相接拼成整幅
            grid = [row for g in acc for row in g]
            fill(grid)
            return
        for grid in options[seg_idx]:
            choose(seg_idx + 1, acc + [grid])

    choose(0, [])
    return [list(map(list, g)) for g in sorted(results)]


def cell_possible_values(grids: list[list[list[str]]]) -> list[list[set[str]]]:
    """每格在全部可能读法下可取到的值集合（逐格边际，供展板公示）。"""
    if not grids:
        return []
    h, w = len(grids[0]), len(grids[0][0])
    margin = [[set() for _ in range(w)] for _ in range(h)]
    for g in grids:
        for i in range(h):
            for j in range(w):
                margin[i][j].add(g[i][j])
    return margin
