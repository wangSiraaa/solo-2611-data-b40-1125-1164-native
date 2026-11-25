"""虚拟电话线：独立干声 PCM、来线观测 PCM、层轨与折返发布记录。

设计要点（对应导播台需求）：
- 每位嘉宾的干声（dry）与每条来线实际到达控制台的观测 PCM（observed）都是
  *独立生成、独立登记* 的素材，各有自己的 asset_id/摘要；控制台从不靠总母带
  作差反推某个人说了什么——削顶（如 20000+20000=32767）或两线同值时，作差
  必然丢掉另一人的话或归属错人。
- 来线观测样本可由多层轨叠加：干声层 + 折返层（echo，某条回路把耳机信号
  延迟整数拍后送回本机来线）。每层都带完整来源（嘉宾、源拍、素材、回路、
  延迟），写进不可变的接线发布记录（WiringManifest）。同值不等于同身份：
  乙干声 7 与甲折返回来的 7 是两条层轨、两个素材身份，永不按值合并。
- 拍（beat）与输出样本一一对应：1 拍 = 1/16000 秒，全部延迟为整数拍，
  因此一切核对都是精确整数运算，不引入浮点。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import numpy as np

from .assets import AssetRegistry, sequence

PHONE_RATE = 16000
GUESTS = ("A", "B")          # 甲=A，乙=B
GUEST_CN = {"A": "甲", "B": "乙"}


def beat_time(beat: int) -> Fraction:
    """拍号 -> 精确输出时刻（秒）。"""
    return Fraction(int(beat), PHONE_RATE)


# ============================ 层轨与接线发布记录 ============================ #

@dataclass(frozen=True)
class Layer:
    """来线某一拍上的一层轨。

    kind:
      - "dry"  : 本线嘉宾当拍干声，source_guest 即本线嘉宾，source_beat == beat
      - "echo" : 折返层；source_guest 是原声嘉宾，route_id/route_delay 标明
                 由哪条回路、延迟几拍折返而来
    """
    line: str                      # 来线（嘉宾号）
    beat: int                      # 到达来线的拍号
    kind: str                      # "dry" | "echo"
    source_guest: str
    source_beat: int
    asset_id: str                  # 该层取值所用的独立 PCM 素材
    route_id: str | None = None
    route_delay: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "line": self.line, "beat": self.beat, "kind": self.kind,
            "source_guest": self.source_guest, "source_beat": self.source_beat,
            "asset_id": self.asset_id, "route_id": self.route_id,
            "route_delay": self.route_delay,
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "Layer":
        return Layer(
            line=obj["line"], beat=int(obj["beat"]), kind=obj["kind"],
            source_guest=obj["source_guest"], source_beat=int(obj["source_beat"]),
            asset_id=obj["asset_id"], route_id=obj.get("route_id"),
            route_delay=(None if obj.get("route_delay") is None
                         else int(obj["route_delay"])),
        )


@dataclass(frozen=True)
class Route:
    """一条已知接线：source_guest 的声音送往 target_ear，耳机信号再以
    loopback_delay 拍（整数）折返回 target 嘉宾本机的来线。"""
    route_id: str
    source_guest: str
    target_ear: str
    loopback_delay: int

    def to_json(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id, "source_guest": self.source_guest,
            "target_ear": self.target_ear,
            "loopback_delay": int(self.loopback_delay),
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "Route":
        return Route(obj["route_id"], obj["source_guest"], obj["target_ear"],
                     int(obj["loopback_delay"]))


class WiringError(ValueError):
    """接线发布记录与实际 PCM 不一致。"""


class WiringManifest:
    """不可变接线发布记录：来线/素材/回路/逐层轨，内容寻址 manifest_id。"""

    FORMAT = "audiomix.phone-wiring/1"

    def __init__(self, show_id: str, n_beats: int,
                 line_assets: dict[str, dict[str, str]],
                 routes: list[Route], layers: list[Layer]):
        self.show_id = show_id
        self.n_beats = int(n_beats)
        self.line_assets = dict(line_assets)   # guest -> {"dry": aid, "observed": aid}
        self.routes = list(routes)
        self.layers = list(layers)
        self.manifest_id = self._canonical_id()

    # ----- 序列化 ----- #
    def to_json(self) -> dict[str, Any]:
        return {
            "format": self.FORMAT,
            "manifest_id": self.manifest_id,
            "show_id": self.show_id,
            "rate": PHONE_RATE,
            "n_beats": self.n_beats,
            "line_assets": self.line_assets,
            "routes": [r.to_json() for r in sorted(self.routes, key=lambda r: r.route_id)],
            "layers": [l.to_json() for l in sorted(
                self.layers, key=lambda x: (x.line, x.beat, x.kind,
                                            x.source_guest, x.source_beat))],
        }

    def _canonical_id(self) -> str:
        # 与 to_json 的排序规则保持一致，保证重开后重算身份不变
        body = {
            "show_id": self.show_id, "n_beats": self.n_beats,
            "line_assets": self.line_assets,
            "routes": [r.to_json() for r in sorted(self.routes,
                                                   key=lambda r: r.route_id)],
            "layers": [l.to_json() for l in sorted(
                self.layers, key=lambda x: (x.line, x.beat, x.kind,
                                            x.source_guest, x.source_beat))],
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh, ensure_ascii=False, indent=2)
        return path

    @staticmethod
    def load(path: str) -> "WiringManifest":
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        if obj.get("format") != WiringManifest.FORMAT:
            raise WiringError(f"非法接线记录 format: {obj.get('format')}")
        m = WiringManifest(
            show_id=obj["show_id"], n_beats=int(obj["n_beats"]),
            line_assets=obj["line_assets"],
            routes=[Route.from_json(r) for r in obj["routes"]],
            layers=[Layer.from_json(x) for x in obj["layers"]],
        )
        if m.manifest_id != obj.get("manifest_id"):
            raise WiringError("manifest_id 与正文不一致（接线记录可能被篡改）")
        return m

    # ----- 核对 ----- #
    def dry_asset(self, guest: str) -> str:
        return self.line_assets[guest]["dry"]

    def observed_asset(self, guest: str) -> str:
        return self.line_assets[guest]["observed"]

    def layers_at(self, line: str, beat: int) -> list[Layer]:
        return [l for l in self.layers if l.line == line and l.beat == beat]

    def validate(self, registry: AssetRegistry) -> None:
        """沿发布记录逐层核对：素材摘要、取值、以及“观测 PCM == 各层之和”。

        任何一层对不上即抛 WiringError——这是“两层轨、路由来源和延迟都可沿
        当时的发布记录核对”的程序化落地。
        """
        # 1) 素材必须真实存在且可读取（摘要由 registry 自行校验）
        pcm: dict[str, np.ndarray] = {}
        for guest, pair in self.line_assets.items():
            if guest not in GUESTS:
                raise WiringError(f"未知嘉宾来线: {guest}")
            for role in ("dry", "observed"):
                aid = pair[role]
                if not registry.contains(aid):
                    raise WiringError(f"缺少来线素材（缺来线）: {guest}/{role} {aid}")
                pcm[aid] = registry.load_pcm(aid)
                if len(pcm[aid]) != self.n_beats:
                    raise WiringError(
                        f"素材 {aid} 样本数 {len(pcm[aid])} != 拍数 {self.n_beats}")

        route_ids = {r.route_id for r in self.routes}
        sums = {g: [0] * self.n_beats for g in self.line_assets}
        dry_seen: set[tuple[str, int]] = set()
        for layer in self.layers:
            if layer.line not in self.line_assets:
                raise WiringError(f"层轨引用未知来线: {layer.line}")
            if not (0 <= layer.beat < self.n_beats and
                    0 <= layer.source_beat < self.n_beats):
                raise WiringError(f"层轨拍号越界: {layer}")
            if layer.asset_id not in pcm:
                raise WiringError(f"层轨引用未登记素材: {layer.asset_id}")
            value = int(pcm[layer.asset_id][layer.source_beat])
            if layer.kind == "dry":
                if layer.source_guest != layer.line or layer.source_beat != layer.beat:
                    raise WiringError(f"干声层来源必须是本线本拍: {layer}")
                key = (layer.line, layer.beat)
                if key in dry_seen:
                    raise WiringError(f"干声层重复: {key}")
                dry_seen.add(key)
                # 干声层取值必须等于该嘉宾独立干声 PCM 的同拍样本
                if value != int(pcm[self.dry_asset(layer.line)][layer.beat]):
                    raise WiringError(f"干声层取值与独立干声 PCM 不符: {layer}")
            elif layer.kind == "echo":
                if layer.route_id not in route_ids:
                    raise WiringError(f"折返层引用未知回路: {layer.route_id}")
                route = next(r for r in self.routes if r.route_id == layer.route_id)
                if route.target_ear != layer.line:
                    raise WiringError(
                        f"折返层回到 {layer.line}，但回路目标耳是 {route.target_ear}")
                if int(layer.route_delay) != route.loopback_delay:
                    raise WiringError(f"折返层延迟与回路接线不符: {layer}")
                if layer.beat - layer.source_beat != route.loopback_delay:
                    raise WiringError(
                        f"折返拍差 {layer.beat - layer.source_beat} "
                        f"!= 回路延迟 {route.loopback_delay}: {layer}")
            else:
                raise WiringError(f"未知层轨类型: {layer.kind}")
            sums[layer.line][layer.beat] += value

        # 2) 每条来线、每一拍：观测 PCM 必须恰好等于全部层轨之和
        for guest in self.line_assets:
            obs = pcm[self.observed_asset(guest)]
            for beat in range(self.n_beats):
                if int(obs[beat]) != sums[guest][beat]:
                    raise WiringError(
                        f"来线 {guest} 第 {beat} 拍观测值 {int(obs[beat])} "
                        f"!= 层轨之和 {sums[guest][beat]}，发布记录与实际 PCM 不符")
            # 干声层必须每拍齐全（含 0 值拍），保证“按样本还原”无空缺
            for beat in range(self.n_beats):
                if (guest, beat) not in dry_seen:
                    raise WiringError(f"来线 {guest} 第 {beat} 拍缺少干声层记录")

    # ----- 还原 ----- #
    def reconstruct(self, registry: AssetRegistry) -> dict[tuple[str, int], list[dict[str, Any]]]:
        """按样本还原每条来线每一拍的全部层轨（含取值与完整来源）。"""
        self.validate(registry)
        pcm = {g: registry.load_pcm(pair["dry"]) for g, pair in self.line_assets.items()}
        out: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for layer in self.layers:
            value = int(pcm[layer.source_guest][layer.source_beat])
            out.setdefault((layer.line, layer.beat), []).append(
                layer.to_json() | {"value": value})
        return out

    def speech_segments(self, registry: AssetRegistry,
                        guest: str) -> list[dict[str, Any]]:
        """还原某位嘉宾确切说了哪些片段：干声非零的极大连续区间（左闭右开）。"""
        self.validate(registry)
        dry = registry.load_pcm(self.dry_asset(guest))
        segments: list[dict[str, Any]] = []
        lo: int | None = None
        for beat in range(self.n_beats):
            nonzero = int(dry[beat]) != 0
            if nonzero and lo is None:
                lo = beat
            if not nonzero and lo is not None:
                segments.append({"guest": guest, "lo": lo, "hi": beat,
                                 "beats": list(range(lo, beat)),
                                 "values": [int(x) for x in dry[lo:beat]]})
                lo = None
        if lo is not None:
            segments.append({"guest": guest, "lo": lo, "hi": self.n_beats,
                             "beats": list(range(lo, self.n_beats)),
                             "values": [int(x) for x in dry[lo:]]})
        return segments

    def loopback_status(self) -> list[dict[str, Any]]:
        """每个干声样本是否已从某条回线折返、经哪条回路、到达哪一拍。"""
        echo_index = {(l.source_guest, l.source_beat): l
                      for l in self.layers if l.kind == "echo"}
        status = []
        for layer in sorted((l for l in self.layers if l.kind == "dry"),
                            key=lambda x: (x.source_guest, x.source_beat)):
            key = (layer.source_guest, layer.source_beat)
            echo = echo_index.get(key)
            status.append({
                "guest": layer.source_guest, "source_beat": layer.source_beat,
                "returned": echo is not None,
                "via_route": None if echo is None else echo.route_id,
                "arrived_line": None if echo is None else echo.line,
                "arrived_beat": None if echo is None else echo.beat,
                "delay": None if echo is None else echo.route_delay,
            })
        return status


# ============================ 来线 PCM 独立生成 ============================ #

@dataclass
class EchoSpec:
    """声明一个折返层：target_line 在 beat 收到 source_guest 在 source_beat 的干声。"""
    target_line: str
    beat: int
    source_guest: str
    source_beat: int
    route_id: str

    @property
    def delay(self) -> int:
        return self.beat - self.source_beat


def build_line_pcms(dry: dict[str, np.ndarray], echoes: list[EchoSpec]
                    ) -> dict[str, np.ndarray]:
    """由独立干声与折返声明生成每条来线的观测 PCM（独立数组，另行登记）。"""
    n_beats = {len(x) for x in dry.values()}
    if len(n_beats) != 1:
        raise ValueError("各来线干声长度（拍数）必须一致")
    n = n_beats.pop()
    observed = {g: np.array(x, dtype=np.int64).copy() for g, x in dry.items()}
    for spec in echoes:
        if spec.target_line not in dry:
            raise ValueError(f"折返目标来线未知: {spec.target_line}")
        if not (0 <= spec.beat < n and 0 <= spec.source_beat < n):
            raise ValueError("折返拍号越界")
        if spec.delay < 0:
            raise ValueError("折返延迟必须为非负整数拍")
        observed[spec.target_line][spec.beat] += int(
            dry[spec.source_guest][spec.source_beat])
    if any(np.any(x > 32767) or np.any(x < -32768) for x in observed.values()):
        raise ValueError("来线观测叠加超出 int16 范围")
    return {g: x.astype(np.int16) for g, x in observed.items()}


def register_show_assets(registry: AssetRegistry, dry: dict[str, np.ndarray],
                         observed: dict[str, np.ndarray],
                         note_prefix: str = "") -> dict[str, dict[str, str]]:
    """把干声/观测 PCM 分别作为独立素材登记，返回 guest -> {dry, observed} id。"""
    out: dict[str, dict[str, str]] = {}
    for guest in GUESTS:
        dry_rec = registry.register(
            sequence(int(v) for v in dry[guest]), PHONE_RATE,
            note=f"{note_prefix}{GUEST_CN[guest]}干声")
        obs_rec = registry.register(
            sequence(int(v) for v in observed[guest]), PHONE_RATE,
            note=f"{note_prefix}{GUEST_CN[guest]}来线观测")
        out[guest] = {"dry": dry_rec.asset_id, "observed": obs_rec.asset_id}
    return out


def build_manifest(show_id: str, line_assets: dict[str, dict[str, str]],
                   routes: list[Route], echoes: list[EchoSpec],
                   registry: AssetRegistry) -> WiringManifest:
    """据登记结果与折返声明构造完整层轨清单（每拍每线一条干声层）。"""
    n_beats = len(registry.load_pcm(line_assets[GUESTS[0]]["dry"]))
    layers: list[Layer] = []
    for guest in GUESTS:
        dry_aid = line_assets[guest]["dry"]
        for beat in range(n_beats):
            layers.append(Layer(
                line=guest, beat=beat, kind="dry",
                source_guest=guest, source_beat=beat, asset_id=dry_aid))
    for spec in echoes:
        route = next(r for r in routes if r.route_id == spec.route_id)
        layers.append(Layer(
            line=spec.target_line, beat=spec.beat, kind="echo",
            source_guest=spec.source_guest, source_beat=spec.source_beat,
            asset_id=line_assets[spec.source_guest]["dry"],
            route_id=spec.route_id, route_delay=route.loopback_delay))
    return WiringManifest(show_id, n_beats, line_assets, routes, layers)


# ============================ 母带作差（反例工具） ============================ #

def naive_master_subtraction(master: np.ndarray, known: np.ndarray) -> np.ndarray:
    """反例：只靠总母带与一条已知来线作差反推另一人。

    削顶后母带信息已永久丢失（32767 而非真实和 40000），作差结果
    32767-20000=12767 与乙真实干声 20000 完全不符；两线同值时也无法凭
    差值归属样本属于谁。本工程不采用此路径，仅留作对照。
    """
    if len(master) != len(known):
        raise ValueError("母带与来线长度不一致")
    return (np.asarray(master, dtype=np.int64)
            - np.asarray(known, dtype=np.int64)).astype(np.int16)


def master_mix(dry_a: np.ndarray, dry_b: np.ndarray) -> np.ndarray:
    """总母带：两条干声直接相加并饱和（与 engine 的单次饱和语义一致）。"""
    total = np.asarray(dry_a, dtype=np.int64) + np.asarray(dry_b, dtype=np.int64)
    return np.clip(total, -32768, 32767).astype(np.int16)
