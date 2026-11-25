"""车站报站词库与出声台（announce 子域）。

纯标准库实现：小整数 PCM、读音模板与容差、音高档位归并、母录/分出资产、
不可发版词库、接头拼接渲染、严格听辨裁决、买断/租用合同、排播与原音回听。
与仓库既有的时间轴/混音档案服务相互独立。
"""
from .pcm import PCM, overlap_join, JointError
from .templates import (
    CharacterTemplate, StationTemplate, segment_matches, strict_match,
    ReadingEvidence, analyze_speaker,
)
from .lexicon import Lexicon, Release, Asset, DerivationSpec, LexiconError
from .render import (
    WholeWordPlan, SplicePlan, SpliceTable, Joint, Rendering,
    render_plan, RenderError,
)
from .hearing import adjudicate, HearingReport, Reason
from .contracts import ContractKind, CostLedger, LedgerEntry
from .schedule import (
    Trip, PlayOrder, Rejection, BookingTable, RecordingCalendar, Schedule,
    ScheduleError,
)
from .desk import BroadcastDesk, DeskDecision
from .store import WorkspaceStore

__all__ = [
    "PCM", "overlap_join", "JointError",
    "CharacterTemplate", "StationTemplate", "segment_matches", "strict_match",
    "ReadingEvidence", "analyze_speaker",
    "Lexicon", "Release", "Asset", "DerivationSpec", "LexiconError",
    "WholeWordPlan", "SplicePlan", "SpliceTable", "Joint", "Rendering",
    "render_plan", "RenderError",
    "adjudicate", "HearingReport", "Reason",
    "ContractKind", "CostLedger", "LedgerEntry",
    "Trip", "PlayOrder", "Rejection", "BookingTable", "RecordingCalendar",
    "Schedule", "ScheduleError",
    "BroadcastDesk", "DeskDecision",
    "WorkspaceStore",
]
