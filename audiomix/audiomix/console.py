"""回送台：路权簿、排他回路、有限缓存、换路时刻与逐耳时间轴交付。

经营法（二选一，本题采用并实现）：**公用回路按段换耳**，同时允许把一条回路
在开播时整场预留给某只耳机——这只是覆盖全场的预约，落在同一本路权簿里：

- 路权簿（CircuitLedger）只按拍区间登记预约，同一回路同一拍不得重复预约；
  整场专属预约 = [0, n_beats) 的预约，建立后整场不可借出；换新来线须有空闲
  回路，否则出缺路权回执。
- 换路只允许发生在拍界；某回路已送出去、尾音尚在路上的拍必须先按原预约送完，
  下一拍才能换耳（“旧段尚在路上的尾音先按原预约送完”）。
- 每只耳机的缓存有限：嘉宾问句到达来线后，允许的最大重放延迟由当班窗口
  max_delay_beats 签认，超出或无回路可用的窗口出 UndeliverableReceipt。
- 嘉宾的本句绝不从自己耳机返听：干声层与目标耳同主的作业直接跳过；折返层
  （echo）本来就是“自己的声音从自己机口回来”，也不进入任何耳机重放。
- 同一干声片段在整份计划里对每只耳机恰好投递一次（exactly-once），同值不同
  身份（乙干声 7 与甲折返 7）不合并、不去重。
- 已播轨不可重写：每次交付生成新 render_id 与新产品文件，旧 render 原样保留
  在内容寻址数据仓；更正后的素材（新 asset_id/摘要）不可能命中旧块。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import numpy as np

from .archive import ArchiveStore
from .assets import AssetRegistry
from .lines import (
    GUESTS, GUEST_CN, PHONE_RATE, WiringManifest, beat_time,
)
from .timeline import TimelineEditor
from .rationals import dump_r

EARS = ("A", "B")            # 甲耳=A、乙耳=B


class RoutingError(ValueError):
    """路权/计划冲突。"""


# ============================ 路权簿 ============================ #

@dataclass(frozen=True)
class Reservation:
    """一条回路在 [lo, hi) 拍区间内预约给某只耳机。"""
    route_id: str
    lo: int
    hi: int
    ear: str
    purpose: str

    def overlaps(self, lo: int, hi: int) -> bool:
        return self.lo < hi and lo < self.hi

    def to_json(self) -> dict[str, Any]:
        return {"route_id": self.route_id, "lo": self.lo, "hi": self.hi,
                "ear": self.ear, "purpose": self.purpose}

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "Reservation":
        return Reservation(obj["route_id"], int(obj["lo"]), int(obj["hi"]),
                           obj["ear"], obj["purpose"])


class CircuitLedger:
    """公用回路路权簿（持久化 JSON）：按段预约、整场预留、尾样保护。"""

    FORMAT = "audiomix.circuit-ledger/1"

    def __init__(self, route_ids: list[str], n_beats: int,
                 reservations: list[Reservation] | None = None):
        self.route_ids = list(route_ids)
        self.n_beats = int(n_beats)
        self.reservations: list[Reservation] = list(reservations or [])
        self._check_invariants()

    def _check_invariants(self) -> None:
        for r in self.reservations:
            if r.route_id not in self.route_ids:
                raise RoutingError(f"预约引用未知回路: {r.route_id}")
            if not (0 <= r.lo < r.hi <= self.n_beats):
                raise RoutingError(f"预约拍区间非法: {r}")
            if r.ear not in EARS:
                raise RoutingError(f"预约目标耳非法: {r.ear}")
        for i, r1 in enumerate(self.reservations):
            for r2 in self.reservations[i + 1:]:
                if r1.route_id == r2.route_id and r1.overlaps(r2.lo, r2.hi):
                    raise RoutingError(f"回路 {r1.route_id} 预约重叠: {r1} <-> {r2}")

    # ----- 查询 ----- #
    def route_free(self, route_id: str, lo: int, hi: int) -> bool:
        return not any(r.route_id == route_id and r.overlaps(lo, hi)
                       for r in self.reservations)

    def free_routes(self, lo: int, hi: int) -> list[str]:
        return [rid for rid in self.route_ids if self.route_free(rid, lo, hi)]

    def ear_routes(self, ear: str, lo: int, hi: int) -> list[str]:
        return [r.route_id for r in self.reservations
                if r.ear == ear and r.overlaps(lo, hi)]

    def last_reservation(self, route_id: str) -> Reservation | None:
        hits = [r for r in self.reservations if r.route_id == route_id]
        return max(hits, key=lambda r: r.hi) if hits else None

    def reserve(self, route_id: str, lo: int, hi: int, ear: str,
                purpose: str) -> Reservation:
        """登记一段预约；冲突即拒（路权簿状态不变）。"""
        res = Reservation(route_id, lo, hi, ear, purpose)
        probe = CircuitLedger(self.route_ids, self.n_beats,
                              self.reservations + [res])  # 不变量校验
        self.reservations = probe.reservations
        return res

    def reserve_entire_show(self, route_id: str, ear: str,
                            purpose: str = "整场专属回路") -> Reservation:
        """开播预订：回路整场不可借出（换新来线须另找空回路）。"""
        return self.reserve(route_id, 0, self.n_beats, ear, purpose)

    def earliest_free_beat(self, route_id: str) -> int:
        """该回路上现有预约全部送完的最早空拍（尾样保护的换路时刻上界）。"""
        tail = [r.hi for r in self.reservations if r.route_id == route_id]
        return max(tail) if tail else 0

    def earliest_free_slot(self, route_id: str, start: int, length: int) -> int | None:
        """从 start 起，该回路上能容纳 length 拍的最早空闲档起点（含中间空档）。

        已预约的尾样区间不可抢占：搜索只会落在既有预约之后或之间的空档，
        保证“旧段尚在路上的尾音先按原预约送完”。返回 None 表示班内放不下。
        """
        t = start
        for r in sorted((x for x in self.reservations if x.route_id == route_id),
                        key=lambda x: x.lo):
            if r.hi <= t:
                continue                    # 预约 entirely 在候选档之前
            if r.lo >= t + length:
                break                       # [t, t+length) 落在空档，可行
            t = max(t, r.hi)                # 与预约相撞，候选项推到该预约之后
        return t if t + length <= self.n_beats else None

    # ----- 持久化 ----- #
    def to_json(self) -> dict[str, Any]:
        return {
            "format": self.FORMAT, "route_ids": self.route_ids,
            "n_beats": self.n_beats,
            "reservations": [r.to_json() for r in sorted(
                self.reservations, key=lambda x: (x.route_id, x.lo))],
        }

    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh, ensure_ascii=False, indent=2)
        return path

    @staticmethod
    def load(path: str) -> "CircuitLedger":
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
        if obj.get("format") != CircuitLedger.FORMAT:
            raise RoutingError(f"非法路权簿 format: {obj.get('format')}")
        return CircuitLedger(obj["route_ids"], int(obj["n_beats"]),
                             [Reservation.from_json(x) for x in obj["reservations"]])


# ============================ 投递作业与回执 ============================ #

@dataclass(frozen=True)
class DeliveryJob:
    """把嘉宾 source_guest 的一个干声片段 [lo,hi) 送往 ear。"""
    job_id: str
    ear: str
    source_guest: str
    lo: int                       # 源拍（嘉宾说话拍）
    hi: int
    arrives_beat: int             # 片段到达控制台来线的拍（本工程 = lo）
    max_delay: int                # 当班签认窗口（拍）

    def to_json(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "ear": self.ear,
            "source_guest": self.source_guest, "lo": self.lo, "hi": self.hi,
            "arrives_beat": self.arrives_beat, "max_delay": self.max_delay,
        }


@dataclass(frozen=True)
class Delivery:
    """一次成功排定的回路投递（含换路时刻与实际延迟）。"""
    job_id: str
    ear: str
    source_guest: str
    route_id: str
    source_lo: int
    source_hi: int
    play_lo: int                  # 耳机实际听到的拍区间 [play_lo, play_hi)
    play_hi: int
    switch_at: int                # 换路时刻（拍界）；无既有预约时等于 play_lo
    delay: int

    def to_json(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "ear": self.ear,
            "source_guest": self.source_guest, "route_id": self.route_id,
            "source_lo": self.source_lo, "source_hi": self.source_hi,
            "play_lo": self.play_lo, "play_hi": self.play_hi,
            "switch_at": self.switch_at, "delay": self.delay,
        }


@dataclass(frozen=True)
class UndeliverableReceipt:
    """无法交付窗口的回执：说明缺的是哪个来线还是哪条路权。"""
    job_id: str
    ear: str
    source_guest: str | None
    lo: int
    hi: int
    reason_code: str              # missing_line | no_route_rights（含窗口内缺路权）
    missing: str                  # 人读说明：缺来线/缺路权/窗口内缺路权
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "ear": self.ear,
            "source_guest": self.source_guest, "lo": self.lo, "hi": self.hi,
            "reason_code": self.reason_code, "missing": self.missing,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ShiftSpec:
    """当班签认：可用回路、每耳允许的最大延迟（拍）、班名。"""
    shift_id: str
    route_ids: list[str]
    max_delay_beats: int
    n_beats: int


# ============================ 回送台 ============================ #

class ReturnConsole:
    """据接线发布记录排排他回路、出逐耳时间轴，并渲染/重开核对。"""

    PLAN_FORMAT = "audiomix.return-plan/1"

    def __init__(self, registry: AssetRegistry, workdir: str):
        self.registry = registry
        self.workdir = workdir
        os.makedirs(workdir, exist_ok=True)

    # ---------- 作业生成：只取干声层，跳过本句与折返 ---------- #
    def jobs_from_manifest(self, manifest: WiringManifest,
                           shift: ShiftSpec,
                           skip_segments: set[tuple[str, int, int]] | None = None
                           ) -> list[DeliveryJob]:
        """从接线记录生成常规投递作业；skip_segments 中的片段留给手动晚到作业。

        另一位的指定问句必须完整出现一次 -> 每位嘉宾的干声片段只排给对方耳；
        折返层（echo）不是干声片段，天然不会变成任何耳机的重放作业。
        """
        skip_segments = skip_segments or set()
        manifest.validate(self.registry)
        jobs: list[DeliveryJob] = []
        for guest in GUESTS:
            ear = guest  # 嘉宾本人的耳机
            for seg in manifest.speech_segments(self.registry, guest):
                if (guest, seg["lo"], seg["hi"]) in skip_segments:
                    continue
                target_ear = next(e for e in EARS if e != ear)
                jobs.append(DeliveryJob(
                    job_id=f"job-{guest}-{seg['lo']}-{seg['hi']}->{target_ear}",
                    ear=target_ear, source_guest=guest,
                    lo=seg["lo"], hi=seg["hi"], arrives_beat=seg["lo"],
                    max_delay=shift.max_delay_beats))
        return sorted(jobs, key=lambda j: (j.arrives_beat, j.ear, j.job_id))

    # ---------- 排程 ---------- #
    def plan(self, manifest: WiringManifest, shift: ShiftSpec,
             pre_booked: CircuitLedger | None = None,
             extra_jobs: list[DeliveryJob] | None = None,
             skip_segments: set[tuple[str, int, int]] | None = None
             ) -> dict[str, Any]:
        """排一份逐耳回路计划；路权写入路权簿，无法交付的窗口出回执。

        确定性规则：
        1. 先做来线校验（缺来线 -> missing_line 回执，不碰路权簿）；
        2. 按到达拍排序，优先使用已预约给本耳的回路，其次任一空闲公用回路；
        3. 无即时空闲回路时，在签认窗口内等最早释放拍（尾样先送完，拍界换路）；
        4. 等不到（无回路 / 超窗）-> 回执，注明缺路权还是超窗。
        """
        manifest.validate(self.registry)
        ledger = pre_booked or CircuitLedger(shift.route_ids, shift.n_beats)
        n = shift.n_beats

        receipts: list[UndeliverableReceipt] = []
        jobs = self.jobs_from_manifest(
            manifest, shift, skip_segments=skip_segments) + list(extra_jobs or [])
        jobs = sorted(jobs, key=lambda j: (j.arrives_beat, j.ear, j.job_id))

        # 去重：同一作业只排一次（exactly-once 的计划层保证）
        seen: set[str] = set()
        uniq_jobs = []
        for j in jobs:
            if j.job_id in seen:
                raise RoutingError(f"重复作业: {j.job_id}")
            seen.add(j.job_id)
            uniq_jobs.append(j)

        deliveries: list[Delivery] = []
        for job in uniq_jobs:
            # 来线是否真实存在（缺来线窗口）
            if job.source_guest not in manifest.line_assets:
                receipts.append(UndeliverableReceipt(
                    job.job_id, job.ear, job.source_guest, job.lo, job.hi,
                    "missing_line", f"缺来线：嘉宾 {job.source_guest} 无独立干声来线",
                    f"目标 {GUEST_CN[job.ear]}耳，源拍 [{job.lo},{job.hi})"))
                continue
            length = job.hi - job.lo
            latest_start = job.arrives_beat + job.max_delay
            chosen, play_lo = None, None

            # 1) 本耳已持有的回路（整场预约 / 前班遗留），零等待优先
            for rid in ledger.ear_routes(job.ear, job.arrives_beat,
                                         min(n, latest_start + 1)):
                slot = ledger.earliest_free_slot(rid, job.arrives_beat, length)
                if slot is not None and slot == job.arrives_beat:
                    chosen, play_lo = rid, slot
                    break
            # 2) 窗口内任一公用回路的最早空闲档（按“起点、回路号”确定性挑选）
            if chosen is None:
                candidates = []
                for rid in shift.route_ids:
                    slot = ledger.earliest_free_slot(rid, job.arrives_beat, length)
                    if slot is not None and slot <= latest_start:
                        candidates.append((slot, rid))
                if candidates:
                    play_lo, chosen = sorted(candidates)[0]
            if chosen is None:
                # 区分：班内根本没有任何放得下的空档（缺路权），
                # 还是最早空档也晚于签认窗口（窗口内缺路权）
                any_slot = [
                    ledger.earliest_free_slot(rid, job.arrives_beat, length)
                    for rid in shift.route_ids]
                feasible = [s for s in any_slot if s is not None]
                if not feasible:
                    missing = ("缺路权：当班回路全部订走，尾样未送完且无空闲回路，"
                               "换新来线无空回路可用")
                    detail_extra = "班内无任何可容纳该片段的空档"
                else:
                    earliest = min(feasible)
                    missing = (f"缺路权（窗口内）：最早可换路拍为 {earliest}，"
                               f"晚于签认窗口（最多延迟 {job.max_delay} 拍）")
                    detail_extra = f"最早空档起点 {earliest}，窗口上界 {latest_start}"
                receipts.append(UndeliverableReceipt(
                    job.job_id, job.ear, job.source_guest, job.lo, job.hi,
                    "no_route_rights", missing,
                    f"目标 {GUEST_CN[job.ear]}耳，源 {GUEST_CN[job.source_guest]} "
                    f"[{job.lo},{job.hi})，到达拍 {job.arrives_beat}；{detail_extra}"))
                continue

            play_hi = play_lo + length
            # 换路时刻：该回路在播放档之前最后一段预约的结束拍；无既有预约则当场换
            before = [r.hi for r in ledger.reservations
                      if r.route_id == chosen and r.hi <= play_lo]
            switch_at = max(before) if before else play_lo
            ledger.reserve(chosen, play_lo, play_hi, job.ear,
                           purpose=f"{job.job_id}：{GUEST_CN[job.source_guest]}问句")
            deliveries.append(Delivery(
                job_id=job.job_id, ear=job.ear, source_guest=job.source_guest,
                route_id=chosen, source_lo=job.lo, source_hi=job.hi,
                play_lo=play_lo, play_hi=play_hi, switch_at=switch_at,
                delay=play_lo - job.arrives_beat))

        plan = self._compose_plan(manifest, shift, ledger, uniq_jobs,
                                  deliveries, receipts)
        return plan

    def _compose_plan(self, manifest: WiringManifest, shift: ShiftSpec,
                      ledger: CircuitLedger, jobs: list[DeliveryJob],
                      deliveries: list[Delivery],
                      receipts: list[UndeliverableReceipt]) -> dict[str, Any]:
        body = {
            "plan_format": self.PLAN_FORMAT,
            "show_id": manifest.show_id,
            "shift_id": shift.shift_id,
            "manifest_id": manifest.manifest_id,
            "n_beats": shift.n_beats,
            "max_delay_beats": shift.max_delay_beats,
            "jobs": [j.to_json() for j in jobs],
            "deliveries": [d.to_json() for d in deliveries],
            "receipts": [r.to_json() for r in receipts],
            "ledger": ledger.to_json(),
        }
        body["plan_id"] = self.canonical_plan_id(body)
        return body

    @staticmethod
    def canonical_plan_id(body: dict[str, Any]) -> str:
        """计划身份由除 plan_id 外的全部正文决定（含路权簿、作业、回执）。"""
        payload = {k: v for k, v in body.items() if k != "plan_id"}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")).hexdigest()

    def save_plan(self, plan: dict[str, Any], path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, ensure_ascii=False, indent=2)
        return path

    @staticmethod
    def load_plan(path: str) -> dict[str, Any]:
        with open(path, encoding="utf-8") as fh:
            plan = json.load(fh)
        if plan.get("plan_format") != ReturnConsole.PLAN_FORMAT:
            raise RoutingError("非法回送计划 format")
        if plan.get("plan_id") != ReturnConsole.canonical_plan_id(plan):
            raise RoutingError("plan_id 与正文不一致（回送计划可能被篡改）")
        return plan

    # ---------- 逐耳时间轴与渲染 ---------- #
    def build_ear_product(self, manifest: WiringManifest, plan: dict[str, Any],
                          ear: str, editor_name: str) -> dict[str, Any]:
        """把计划里该耳的全部投递落成可渲染时间轴产品。

        每条投递是一个 clip：引用 *源嘉宾独立干声素材* 的 [source_lo,source_hi)，
        放在输出拍 [play_lo,play_hi)。clip_id 含作业号与回路号，同值不同身份
        各自独立，绝不按值合并。
        """
        manifest.validate(self.registry)
        ed = TimelineEditor(os.path.join(self.workdir, "editor"),
                            self.registry, name=editor_name)
        clip_ids: list[str] = []
        for d in plan["deliveries"]:
            if d["ear"] != ear:
                continue
            clip_id = f"{d['job_id']}@{d['route_id']}"
            ed.insert({
                "clip_id": clip_id,
                "asset_id": manifest.dry_asset(d["source_guest"]),
                "a": d["source_lo"], "b": d["source_hi"],
                "start": dump_r(beat_time(d["play_lo"])),
            })
            clip_ids.append(clip_id)
        product = ed.publish()
        return product

    def render_ear(self, manifest: WiringManifest, plan: dict[str, Any], ear: str,
                   store: ArchiveStore, render_id: str,
                   product_path: str) -> dict[str, Any]:
        """发布逐耳产品 -> 渲染入仓（内容寻址，旧轨不可重写）。"""
        product = self.build_ear_product(
            manifest, plan, ear, editor_name=f"{plan['shift_id']}_{ear}")
        os.makedirs(os.path.dirname(os.path.abspath(product_path)) or ".",
                    exist_ok=True)
        with open(product_path, "w", encoding="utf-8") as fh:
            json.dump(product, fh, ensure_ascii=False, indent=2)
        pcm = {a["asset_id"]: self.registry.load_pcm(a["asset_id"])
               for a in product["assets"]}
        T = Fraction(plan["n_beats"], PHONE_RATE)
        info = store.render(product, T, pcm, render_id=render_id,
                            product_path=product_path)
        return {"render_id": render_id, "ear": ear, "product": product, "info": info}

    # ---------- 核对工具 ---------- #
    @staticmethod
    def expected_ear_pcm(manifest: WiringManifest, plan: dict[str, Any],
                         registry: AssetRegistry, ear: str) -> np.ndarray:
        """不经过引擎，直接按计划与独立干声 PCM 推出该耳应听到的逐拍样本。"""
        n = plan["n_beats"]
        out = np.zeros(n, dtype=np.int64)
        for d in plan["deliveries"]:
            if d["ear"] != ear:
                continue
            dry = registry.load_pcm(manifest.dry_asset(d["source_guest"]))
            for k in range(d["source_hi"] - d["source_lo"]):
                out[d["play_lo"] + k] = int(dry[d["source_lo"] + k])
        return out.astype(np.int16)

    def verify_ear_render(self, manifest: WiringManifest, plan: dict[str, Any],
                          store: ArchiveStore, render_id: str, ear: str
                          ) -> dict[str, Any]:
        """重开式核对：区间读取数据仓渲染，与计划期望值逐样本一致。"""
        rendered = store.read_samples(render_id, 0, plan["n_beats"])
        expected = self.expected_ear_pcm(manifest, plan, self.registry, ear)
        ok = bool(np.array_equal(rendered, expected))
        return {"ear": ear, "render_id": render_id, "ok": ok,
                "rendered": [int(v) for v in rendered],
                "expected": [int(v) for v in expected]}
