"""车站报站词库：母录、分出资产、改版血缘与不可变发版。

关键语义：

- 素材是有明确采样边界的本地短录音（小整数 PCM）。
- 整词母录可分出词头/尾字：分出资产只记录母录上的切片边界，
  不产生新录音，因此不计入录音采样预算。
- 母录改版会同时替换母录与所有来源于它的分出资产；已发布版本与
  已播班次仍指向改版前的快照，旧播次永远按原音回听。
- 发版产品不可变：乘务台的整词/拼接渲染只能读取发版后的真实 PCM。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .pcm import PCM, as_pcm
from .templates import CharacterTemplate, StationTemplate, analyze_speaker


class LexiconError(ValueError):
    """词库操作不合法（越界切片、发版后篡改、预算超限等）。"""


@dataclass(frozen=True)
class DerivationSpec:
    """分出资产在母录上的切片边界：pcm = master[start:end]。"""

    master_id: str
    start: int
    end: int

    def slice_of(self, master_pcm: PCM) -> PCM:
        if self.start < 0 or self.end > len(master_pcm) or self.start >= self.end:
            raise LexiconError(
                f"分出切片越界：[{self.start}:{self.end}] 母录长度 {len(master_pcm)}"
            )
        return tuple(master_pcm[self.start:self.end])


@dataclass(frozen=True)
class AssetRevision:
    """母录的一次改版留痕（改版只允许发生在母录上）。"""

    seq: int
    pcm: PCM
    reason: str


@dataclass
class Asset:
    """词库中的一个可出声资产（整词母录或分出的词头/尾字）。"""

    id: str
    kind: str  # whole_word | head | tail
    speaker: str
    pcm: PCM
    label: str  # 整词为站名，分出片段为字
    master_id: str | None = None
    derivation: DerivationSpec | None = None
    current_seq: int = 0
    revisions: tuple[AssetRevision, ...] = ()
    frozen: bool = False

    def is_master(self) -> bool:
        return self.master_id is None

    def require_mutable(self) -> None:
        if self.frozen:
            raise LexiconError(f"资产 {self.id} 属于已发版快照，禁止篡改")


@dataclass(frozen=True)
class Release:
    """一次不可发版词库快照。"""

    version: str
    note: str
    assets: dict[str, Asset]
    character_templates: tuple[CharacterTemplate, ...]
    station_templates: tuple[StationTemplate, ...]

    def pcm_of(self, asset_id: str) -> PCM:
        try:
            return self.assets[asset_id].pcm
        except KeyError as exc:
            raise LexiconError(f"发版 {self.version} 不含资产 {asset_id}") from exc

    def station_template(self, station: str) -> StationTemplate:
        for t in self.station_templates:
            if t.station == station:
                return t
        raise LexiconError(f"发版 {self.version} 无 {station} 的严格模板")


class Lexicon:
    """所有人共用的同一份词库（工作态 + 历次发版快照）。"""

    def __init__(self, sample_budget: int = 100) -> None:
        self.assets: dict[str, Asset] = {}
        self.character_templates: list[CharacterTemplate] = []
        self.station_templates: list[StationTemplate] = []
        self.releases: dict[str, Release] = {}
        self._next_release_no = 1
        self.sample_budget = sample_budget
        self.recorded_samples = 0  # 仅统计真实新录音，分出切片不计

    # --------------------------- 录音与分出 --------------------------- #

    def _charge(self, extra: int) -> None:
        if self.recorded_samples + extra > self.sample_budget:
            raise LexiconError(
                f"录音采样预算超限：已用 {self.recorded_samples} + 新增 {extra} "
                f"> 预算 {self.sample_budget}"
            )

    def record_master(
        self,
        asset_id: str,
        kind: str,
        speaker: str,
        pcm: list[int] | PCM,
        label: str,
        reason: str = "首录",
    ) -> Asset:
        """登记一段真实新录音（整词母录或独立短单元母录）。"""
        if asset_id in self.assets:
            raise LexiconError(f"资产编号已存在：{asset_id}")
        if kind not in ("whole_word", "head", "tail"):
            raise LexiconError("资产种类必须是 whole_word/head/tail")
        pcm = as_pcm(pcm)
        self._charge(len(pcm))
        self.recorded_samples += len(pcm)
        asset = Asset(
            id=asset_id, kind=kind, speaker=speaker, pcm=pcm, label=label,
            revisions=(AssetRevision(seq=0, pcm=pcm, reason=reason),),
        )
        self.assets[asset_id] = asset
        return asset

    def derive(
        self,
        derived_id: str,
        master_id: str,
        kind: str,
        label: str,
        start: int,
        end: int,
    ) -> Asset:
        """从同一母录分出词头/尾字；只记边界，不新增录音、不占采样预算。"""
        if derived_id in self.assets:
            raise LexiconError(f"资产编号已存在：{derived_id}")
        if kind not in ("head", "tail"):
            raise LexiconError("分出资产只能是词头或尾字")
        master = self._require(master_id)
        spec = DerivationSpec(master_id=master_id, start=start, end=end)
        pcm = spec.slice_of(master.pcm)  # 越界在此直接拒绝
        asset = Asset(
            id=derived_id, kind=kind, speaker=master.speaker, pcm=pcm,
            label=label, master_id=master_id, derivation=spec,
            current_seq=master.current_seq,
        )
        self.assets[derived_id] = asset
        return asset

    # --------------------------- 改版与恢复 --------------------------- #

    def revise_master(self, master_id: str, new_pcm: list[int] | PCM, reason: str) -> None:
        """改母录末样本（或整体改版）：母录与全部分出资产同步换版。"""
        master = self._require(master_id)
        master.require_mutable()
        if not master.is_master():
            raise LexiconError("只能对母录改版，分出资产随母录联动")
        new_pcm = as_pcm(new_pcm)
        self._charge(len(new_pcm))
        self.recorded_samples += len(new_pcm)
        seq = master.current_seq + 1
        revised = replace(
            master,
            pcm=new_pcm,
            current_seq=seq,
            revisions=master.revisions + (AssetRevision(seq, new_pcm, reason),),
        )
        self.assets[master_id] = revised
        for asset in list(self.assets.values()):
            if asset.master_id == master_id:
                self.assets[asset.id] = replace(
                    asset,
                    pcm=asset.derivation.slice_of(new_pcm),
                    current_seq=seq,
                )

    def restore_master(self, master_id: str, reason: str) -> None:
        """恢复母录原版（同样按一次新录音计费并留痕）。"""
        master = self._require(master_id)
        original = master.revisions[0].pcm
        self.revise_master(master_id, original, reason)

    # --------------------------- 模板登记 ----------------------------- #

    def add_character_template(self, template: CharacterTemplate) -> None:
        self.character_templates.append(template)

    def add_station_template(self, template: StationTemplate) -> None:
        self.station_templates.append(template)

    # --------------------------- 发版 --------------------------------- #

    def release(self, note: str = "") -> Release:
        """冻结当前词库为不可变发版，乘务台只能从发版读取真实渲染。"""
        version = f"R{self._next_release_no}"
        self._next_release_no += 1
        snapshot = {aid: replace(a, frozen=True) for aid, a in self.assets.items()}
        rel = Release(
            version=version,
            note=note,
            assets=snapshot,
            character_templates=tuple(self.character_templates),
            station_templates=tuple(self.station_templates),
        )
        self.releases[version] = rel
        return rel

    def get_release(self, version: str) -> Release:
        try:
            return self.releases[version]
        except KeyError as exc:
            raise LexiconError(f"未知发版：{version}") from exc

    def latest_release(self) -> Release:
        if not self.releases:
            raise LexiconError("词库尚未发版")
        return self.releases[f"R{self._next_release_no - 1}"]

    # --------------------------- 查询证据 ----------------------------- #

    def _require(self, asset_id: str) -> Asset:
        try:
            return self.assets[asset_id]
        except KeyError as exc:
            raise LexiconError(f"未知资产：{asset_id}") from exc

    def speaker_segments(self, speaker: str) -> list[tuple[str, PCM]]:
        return [(a.id, a.pcm) for a in self.assets.values() if a.speaker == speaker]

    def speaker_analysis(self, speaker: str):
        return analyze_speaker(
            speaker, self.speaker_segments(speaker), self.character_templates
        )

    def catalog(self) -> list[dict]:
        """两家店看到的同一本资产目录。"""
        return [
            {
                "id": a.id,
                "kind": a.kind,
                "speaker": a.speaker,
                "label": a.label,
                "samples": len(a.pcm),
                "master_id": a.master_id,
                "seq": a.current_seq,
            }
            for a in sorted(self.assets.values(), key=lambda x: x.id)
        ]
