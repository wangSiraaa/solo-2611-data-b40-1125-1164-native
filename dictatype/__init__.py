"""听写机（dictatype）：把 audiomix 发布/渲染的逐样本声音接到抄录工具，
再配合手录纸面压痕格做逐击重建与复写联（碳纸联）校样。

子模块：
- model       键、击（落锤工单）、联纸、压痕格等核心数据结构
- acoustic    击序列 -> audiomix 时间轴发布产品 -> 实际 PCM（同仓库求和/舍入/削顶）
- desk        听打台：相容逐击记录求解、逐格可确定性与覆盖先后
- proofroom   校样室：揭纸封存、联同步复写、具名联签收、编号勘误条
- store       交接仓库：刊行号开档/重开、只读历史收件回执重播
- cli         命令行入口

离线约束：单次会话最多八击、两联。
"""
from __future__ import annotations

import os as _os
import sys as _sys

# audiomix 是仓库内的同级包（../audiomix），未安装为 site-package，
# 这里把它的目录加入 sys.path，使 dictatype 在仓库任意工作目录下都可导入。
_AUDIOMIX_ROOT = _os.path.abspath(
    _os.path.join(_os.path.dirname(__file__), _os.pardir, "audiomix"))
if _AUDIOMIX_ROOT not in _sys.path:
    _sys.path.insert(0, _AUDIOMIX_ROOT)

from .model import (
    Action, Key, Strike, CARRIAGE_RIGHT, CARRIAGE_LEFT, RIBBON_LIFT,
    MAX_STRIKES, MAX_COPIES,
)
from .desk import TranscriptionDesk, SessionSpec, Evidence, Transcript, CellVerdict
from .proofroom import ProofRoom, Receipt, ErrataSlip, HANDOFF_SEAL_AT_SEPARATION
from .store import IssueStore, IssueError, ReopenError
from .acoustic import strikes_to_product, render_strikes

__all__ = [
    "Action", "Key", "Strike", "CARRIAGE_RIGHT", "CARRIAGE_LEFT", "RIBBON_LIFT",
    "MAX_STRIKES", "MAX_COPIES",
    "TranscriptionDesk", "SessionSpec", "Evidence", "Transcript", "CellVerdict",
    "ProofRoom", "Receipt", "ErrataSlip", "HANDOFF_SEAL_AT_SEPARATION",
    "IssueStore", "IssueError", "ReopenError",
    "strikes_to_product", "render_strikes",
]
