"""核心数据结构：键、击（落锤工单）、联纸、压痕格。

打字机语义（题面）：
- 每个键给一段短声样和一个车行动作，可
  * 印字符并右移一格（CARRIAGE_RIGHT），
  * 退一格（CARRIAGE_LEFT，退格键，不印字），
  * 只抬色带（RIBBON_LIFT，落锤但色带抬起：不留下色带墨印，
    物理压痕仍打穿当前整叠纸）。
- 多个落锤重叠时按仓库实际 PCM 求和、舍入和削顶，一声不一定意味着一次键入。
- 每一击都落在某个允许的拍点 beat（从 0 起的整数）；同一拍点允许多击，
  其声样在该拍点重叠混音。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

MAX_STRIKES = 8          # 离线最多八击
MAX_COPIES = 2           # 离线最多两联
MAX_BEAT = MAX_STRIKES   # 拍点编号上界（每拍至少可放一击）


class Action(str, Enum):
    """车行动作。枚举值即 JSON 中的规范写法。"""

    PRINT_RIGHT = "print_right"   # 印字符并右移一格
    BACKSPACE = "backspace"       # 退一格（不印字）
    RIBBON_LIFT = "ribbon_lift"   # 只抬色带（落锤不印字，压痕仍在）


# 便于文档与测试引用的动作别名
CARRIAGE_RIGHT = Action.PRINT_RIGHT
CARRIAGE_LEFT = Action.BACKSPACE
RIBBON_LIFT = Action.RIBBON_LIFT


@dataclass(frozen=True)
class Key:
    """键盘上的一个键：绑定一段声样与一个车行动作。

    glyph 为该键可印字符；退格键与抬色带键的 glyph 为 None。
    sample 为该键短声样的 int16 样本序列（逐样本声音，进入 audiomix 混音）。
    """

    key_id: str
    action: Action
    sample: tuple[int, ...]
    glyph: str | None = None

    def __post_init__(self) -> None:
        if not self.key_id:
            raise ValueError("key_id 不能为空")
        if not isinstance(self.action, Action):
            raise ValueError(f"非法车行动作: {self.action!r}")
        if len(self.sample) == 0:
            raise ValueError(f"键 {self.key_id} 声样不能为空")
        for v in self.sample:
            if not (-32768 <= int(v) <= 32767):
                raise ValueError(f"键 {self.key_id} 声样超出 int16 范围")
        if self.action is Action.PRINT_RIGHT and (self.glyph is None or self.glyph == ""):
            raise ValueError(f"印字键 {self.key_id} 必须给出字符 glyph")
        if self.action is not Action.PRINT_RIGHT and self.glyph is not None:
            raise ValueError(f"非印字键 {self.key_id} 不能携带字符")

    def to_json(self) -> dict[str, Any]:
        return {"key_id": self.key_id, "action": self.action.value,
                "glyph": self.glyph, "sample": list(self.sample)}

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "Key":
        return Key(key_id=str(obj["key_id"]),
                   action=Action(obj["action"]),
                   sample=tuple(int(v) for v in obj["sample"]),
                   glyph=obj.get("glyph"))


@dataclass(frozen=True)
class Strike:
    """一次落锤工单（逐击记录中的一击）：在拍点 beat 敲下 key_id。"""

    seq: int          # 击序号，从 1 开始（同一拍点内按 seq 定先后）
    beat: int         # 允许的拍点（非负整数）
    key_id: str

    def __post_init__(self) -> None:
        if self.seq < 1:
            raise ValueError("击序号 seq 必须从 1 开始")
        if self.beat < 0 or self.beat > MAX_BEAT:
            raise ValueError(f"拍点必须落在 [0,{MAX_BEAT}]")

    def to_json(self) -> dict[str, Any]:
        return {"seq": self.seq, "beat": self.beat, "key_id": self.key_id}

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "Strike":
        return Strike(seq=int(obj["seq"]), beat=int(obj["beat"]),
                      key_id=str(obj["key_id"]))


@dataclass(frozen=True)
class CopySheet:
    """一联纸（具名收件联）。

    peel_after_strike：该联在第几击之后揭走；None 表示整叠留到终校。
    揭走判定：联在第 s 击时仍垫在下面  <=>  peel_after_strike is None
    或 peel_after_strike >= s。揭走后的落锤既不印字也不留压痕于该联。
    """

    copy_id: str
    recipient: str
    peel_after_strike: int | None = None

    def __post_init__(self) -> None:
        if not self.copy_id:
            raise ValueError("copy_id 不能为空")
        if self.peel_after_strike is not None and self.peel_after_strike < 0:
            raise ValueError("揭纸时刻不能为负")

    def present_at(self, strike_seq: int) -> bool:
        """第 strike_seq 击落锤时该联是否仍垫在整叠里。"""
        return self.peel_after_strike is None or self.peel_after_strike >= strike_seq

    def to_json(self) -> dict[str, Any]:
        return {"copy_id": self.copy_id, "recipient": self.recipient,
                "peel_after_strike": self.peel_after_strike}

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "CopySheet":
        return CopySheet(copy_id=str(obj["copy_id"]),
                         recipient=str(obj["recipient"]),
                         peel_after_strike=(None if obj.get("peel_after_strike") is None
                                            else int(obj["peel_after_strike"])))


@dataclass(frozen=True)
class IndentationMark:
    """手录纸面压痕格中的一格读数。

    state:
      "present"  —— 该格确有物理压痕（无论墨印，抬色带击也算）；
      "absent"   —— 该格确无压痕；
      "unknown"  —— 纸面未录/留疑。
    arrived_late: 迟来的压痕（联纸揭走封存之后才补录的证据）。
    """

    cell: int
    state: str
    arrived_late: bool = False

    def __post_init__(self) -> None:
        if self.state not in ("present", "absent", "unknown"):
            raise ValueError(f"非法压痕读数: {self.state}")
        if self.cell < 0:
            raise ValueError("格号不能为负")

    def to_json(self) -> dict[str, Any]:
        return {"cell": self.cell, "state": self.state,
                "arrived_late": self.arrived_late}

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "IndentationMark":
        return IndentationMark(cell=int(obj["cell"]), state=str(obj["state"]),
                               arrived_late=bool(obj.get("arrived_late", False)))
