"""读音模板、允许误差与发音人音高档位归并。

录音师用少量已标注的读音模板解释词头和尾字：每个模板绑定一个发音人
音高档位，给一小段参考 PCM 及允许误差（逐样本绝对差）。
同一发音人编号的片段共享一次录制的音高档位，档位必须能让该发音人
全部片段同时成立；一段录音若同时落入两个字的容差，两个读法都保留。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .pcm import PCM


@dataclass(frozen=True)
class CharacterTemplate:
    """一个字（词头或尾字）在某音高档位下的读音模板。"""

    character: str
    speaker: str
    gear: str
    reference: PCM
    tolerance: int

    def __post_init__(self) -> None:
        if self.tolerance < 0:
            raise ValueError("允许误差不能为负")
        if len(self.reference) == 0:
            raise ValueError("模板至少含一个采样")

    def admits(self, pcm: PCM) -> bool:
        """片段是否落入本模板的逐样本容差（长度必须一致，边界明确）。"""
        if len(pcm) != len(self.reference):
            return False
        return all(abs(a - b) <= self.tolerance for a, b in zip(pcm, self.reference))


@dataclass(frozen=True)
class StationTemplate:
    """站名的严格模板：零误差整句 PCM，决定“还能听出哪些候选站”。"""

    station: str
    reference: PCM

    def admits(self, pcm: PCM) -> bool:
        return pcm == self.reference


def segment_matches(pcm: PCM, templates: list[CharacterTemplate]) -> list[CharacterTemplate]:
    """找出片段在给定模板下仍能同时成立的全部读法（容差匹配，不去重字）。"""
    return [t for t in templates if t.admits(pcm)]


def strict_match(pcm: PCM, stations: list[StationTemplate]) -> list[StationTemplate]:
    """严格听辨：只有整句逐样本相等才算能听出该站。"""
    return [s for s in stations if s.admits(pcm)]


@dataclass(frozen=True)
class ReadingEvidence:
    """一段录音在某个音高档位下保留的读法证据。"""

    asset_id: str
    speaker: str
    gear: str
    readings: tuple[str, ...]
    matched_templates: tuple[str, ...]


@dataclass
class SpeakerAnalysis:
    """发音人归并结果：可成立档位、每段录音在该档位下保留的读法。"""

    speaker: str
    candidate_gears: tuple[str, ...]
    evidence_by_gear: dict[str, tuple[ReadingEvidence, ...]] = field(default_factory=dict)

    def evidence_for(self, gear: str) -> tuple[ReadingEvidence, ...]:
        return self.evidence_by_gear.get(gear, ())


def analyze_speaker(
    speaker: str,
    segments: list[tuple[str, PCM]],
    templates: list[CharacterTemplate],
) -> SpeakerAnalysis:
    """归并同一发音人的全部片段，求能同时成立的音高档位及各段读法。

    segments 为 (asset_id, pcm)；模板按 (speaker, gear) 分组。
    一个档位成立的条件：该档位对每一段都至少有一个容许的读法模板；
    只要存在任一段在该档位无任何模板容差，此档位被整段排除。
    """
    speaker_templates = [t for t in templates if t.speaker == speaker]
    gears = tuple(sorted({t.gear for t in speaker_templates}))
    analysis = SpeakerAnalysis(speaker=speaker, candidate_gears=())
    viable: list[str] = []
    for gear in gears:
        gear_templates = [t for t in speaker_templates if t.gear == gear]
        evidences: list[ReadingEvidence] = []
        gear_ok = True
        for asset_id, pcm in segments:
            hits = segment_matches(pcm, gear_templates)
            if not hits:
                gear_ok = False  # 另一段能排除这一档
                break
            evidences.append(
                ReadingEvidence(
                    asset_id=asset_id,
                    speaker=speaker,
                    gear=gear,
                    readings=tuple(dict.fromkeys(t.character for t in hits)),
                    matched_templates=tuple(f"{t.character}@{t.gear}" for t in hits),
                )
            )
        if gear_ok:
            viable.append(gear)
            analysis.evidence_by_gear[gear] = tuple(evidences)
    analysis.candidate_gears = tuple(viable)
    return analysis
