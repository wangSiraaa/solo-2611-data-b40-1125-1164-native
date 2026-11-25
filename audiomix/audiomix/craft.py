"""两家工坊的贴签工艺与两展交接回放。

工艺甲——压印（press）：馆方在有校准边的底版上提前把闭合区签压入版材，压入后
成为版材一部分；撤展必须连同旧版整体撤下才不会留下失实字样，印过的签材不能拆
去乙展（一枚认证签材料压印即随版封存）。

工艺乙——游签（floating）：装框时再贴实体游签；换位只能在具名闭馆窗口进行。
裁框时正有一次可取下/交接的闭馆窗，错过则要到乙展散场后才能再移动。

两家工坊都只发“观众站在已开放展板前真能看见”的签；旧目录按原展次仍可查回。
本模块对实际采用的工艺各回放两次：接单在窗口前、接单在窗口后。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Literal

CraftName = Literal["press", "floating"]


@dataclass
class LabelMaterial:
    """一枚认证签材料（两展合计仅一枚）。"""
    label_id: str
    consumed_by: str | None = None        # 被哪个展/哪块版消耗
    location: str = "vault"                # vault / plate:<id> / sealed:<id>

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BoardState:
    """一块展板的状态：底版、裁框、是否有可见闭合签、是否封存。"""
    board_id: str
    exhibition: str
    has_valid_closure: bool                # 按实读格值当前是否仍可签闭合
    sealed: bool = False
    label_id: str | None = None
    history: list[str] = None

    def __post_init__(self) -> None:
        if self.history is None:
            self.history = []

    def log(self, msg: str) -> None:
        self.history.append(msg)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReplayResult:
    """一次回放的结论。"""
    craft: CraftName
    order_timing: Literal["before_window", "after_window"]
    jia_visitor_sees_closure: bool
    yi_visitor_sees_closure: bool
    sealed_board: str | None
    label_final_location: str
    narrative: list[str]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------- 压印工坊 ----------------------------- #
def replay_press(timing: str, jia_valid_before: bool, jia_valid_after: bool,
                 yi_requires_label: bool = True) -> ReplayResult:
    """压印工艺回放。

    甲展旧版在第一场已用、签已压入版材；开幕后收到裁顶行通知，新框仍露出原中心
    但中心已成为边点（不再闭合），现场不许可遮字，替换版材次日才到。
    """
    label = LabelMaterial(label_id="AUTH-1")
    jia = BoardState("JIA", "甲展", has_valid_closure=jia_valid_before)
    yi = BoardState("YI", "乙展", has_valid_closure=True)
    story: list[str] = []

    # 甲展开幕：闭合签随底版压印，认证材料就此随版消耗
    jia.label_id = label.label_id
    label.consumed_by, label.location = "JIA", "plate:JIA"
    jia.log("甲展开幕：闭合低雨区签已压入底版，成为版材一部分")
    story.append("甲展开幕时观众可见闭合声明（压印在版）。")

    if timing == "before_window":
        # 通知在撤换窗口前到达：旧版连同失实压印签整体撤下封存，新版无签
        jia.has_valid_closure = jia_valid_after
        jia.sealed, jia.label_id = True, None
        label.location = "sealed:JIA"
        jia.log("窗口前接单：旧版连同压印签整体撤下封存，换上无签新版")
        story.append("裁顶行通知在闭馆窗前到达：旧版整体撤下封存，"
                     "印过的签材不能拆去乙展，甲新版无闭合字样。")
        jia_visitors = False
    else:
        # 窗口后才到：旧版无法在窗内撤换，带失实压印签撑到散场
        jia.has_valid_closure = jia_valid_after
        story.append("通知在闭馆窗后到达：已无撤换窗口，甲展现场不许可遮字，"
                     "观众在该场剩余时间仍看到版上压印的闭合字样（失实）。")
        jia.log("窗口后接单：旧版无法撤换，失实压印签保留至散场，散场后封存")
        jia.sealed, jia.label_id = True, None
        label.location = "sealed:JIA"
        jia_visitors = True   # 字样物理上仍在眼前（虽已失实）

    # 乙展：完整底图本可签，但唯一认证签材已随甲旧版封存，压印签不可拆运
    yi_open = not yi_requires_label
    story.append(
        "乙展约定须亮出中心签才开幕，但唯一认证签材已随甲旧版封存、"
        "压印签不能拆去乙展，故乙展" + ("如期开（无签合同）。" if yi_open else
                                       "不能按合同如期开幕。"))
    return ReplayResult(
        craft="press", order_timing=timing,
        jia_visitor_sees_closure=jia_visitors,
        yi_visitor_sees_closure=False,
        sealed_board="JIA", label_final_location=label.location, narrative=story)


# ----------------------------- 游签工坊 ----------------------------- #
def replay_floating(timing: str, jia_valid_after: bool,
                    window_open: bool = True) -> ReplayResult:
    """游签工艺回放：裁框时有一次具名闭馆窗可取下并交接游签。"""
    label = LabelMaterial(label_id="AUTH-1")
    jia = BoardState("JIA", "甲展", has_valid_closure=True)
    yi = BoardState("YI", "乙展", has_valid_closure=True)
    story: list[str] = []

    # 甲第一场：游签贴在甲版中心槽
    jia.label_id = label.label_id
    label.consumed_by, label.location = "JIA", "plate:JIA"
    jia.log("甲第一场：实体游签贴于中心具名槽，观众可见闭合声明")
    story.append("甲第一场观众可见闭合声明（实体游签）。")

    jia.has_valid_closure = jia_valid_after     # 裁顶行后中心变边点，不再闭合

    if timing == "before_window" and window_open:
        # 窗口前接单：闭馆窗内取下游签，甲换无签新框，游签交接给乙
        jia.label_id, label.location = None, "plate:YI"
        yi.label_id = label.label_id
        jia.log("窗口前接单：闭馆窗内取下游签，甲改无签框，游签交接乙展")
        story.append("通知在闭馆窗前到达：窗内取下游签，甲新框不露闭合字样，"
                     "游签交到乙展中心槽。")
        jia_sees, yi_sees = False, True
        sealed = None
    else:
        # 错过窗口：游签不能移动，直到乙展散场后；甲场剩余时间字样仍在
        story.append("通知在闭馆窗后到达（或窗口已关）：游签被锁在甲版上，"
                     "现场不许可遮字，甲展观众仍见闭合字样；乙展无签可用，"
                     "不能按‘亮出中心签’合同开幕，直到乙散场后才能再移动。")
        jia.log("窗口后接单：游签锁定甲版至乙散场，乙展无认证签")
        jia_sees, yi_sees = True, False
        sealed = "JIA"
    return ReplayResult(
        craft="floating", order_timing=timing,
        jia_visitor_sees_closure=jia_sees,
        yi_visitor_sees_closure=yi_sees,
        sealed_board=sealed, label_final_location=label.location, narrative=story)


def run_all_replays(jia_valid_before: bool = True, jia_valid_after: bool = False
                    ) -> dict[str, list[dict[str, Any]]]:
    """对两种工艺各回放窗口前/后两次，返回可入档的完整结论。"""
    out: dict[str, list[dict[str, Any]]] = {"press": [], "floating": []}
    for timing in ("before_window", "after_window"):
        out["press"].append(
            replay_press(timing, jia_valid_before, jia_valid_after).to_json())
        out["floating"].append(
            replay_floating(timing, jia_valid_after).to_json())
    return out
