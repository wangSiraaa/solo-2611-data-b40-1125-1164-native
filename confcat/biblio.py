"""地方译本目录的领域模型与纯函数规则（无 IO，可独立单测）。

编目对象
========
* :class:`PenNameDeclaration` —— 一条“署名—译者”笔名声明，带出处（来源任务、
  原 CSV 行号、列血缘），可按原文语种或一段卷期限定生效范围；
* :class:`HoldingCopy` —— 交柜的一册书：卷号、印本版次、扉页上**实际看得见**
  的区分字段、以及是否为可印新版。

索引规则（与 IO 无关的纯函数）
==============================
* :func:`candidate_identities` —— 编目端：一册书里可成立的“署名—译者”关系，
  每条候选都连到声明出处；
* :func:`evaluate_holding` —— 索引端：只拿该版**可印或可询问**的字段消歧。
  同一个问题在书上找不到答案（字段不在 ``visible_fields``）就不能问读者，
  该字段记为“待查缺项”，不能据此排除候选。

两种约束的判定：
* 卷号区间：卷号是收册必录、读者可逐册核对的字段，恒为可询问字段；
* 原文语种：只有扉页印了语种栏（``visible_fields`` 含 ``original_language``）
  才可问；旧节印本删掉语种栏时，语种约束一律不参与筛选，只列为缺项。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 可用于消歧的字段名
FIELD_ORIGINAL_LANGUAGE = "original_language"
FIELD_VOLUME = "volume"
# 卷号对每一册都可见（书脊/题名页必录），故恒为可询问字段
ALWAYS_VISIBLE_FIELDS: frozenset[str] = frozenset({FIELD_VOLUME})


class BiblioError(Exception):
    """译本目录领域错误基类。"""

    def __init__(self, message: str, reason: str = ""):
        self.reason = reason
        super().__init__(message)


# --------------------------------------------------------------------------- #
# 出处：声明的每一列都连得到归一任务的行与列血缘
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DeclarationSource:
    """一条声明的出处证据。

    ``task_id`` 指向已有接入任务（归一化关系表）；``row_no`` 是该任务内的
    0 基行号；``columns`` 记录每个目标列来自哪个原 CSV 列（列血缘）。
    """

    task_id: str
    row_no: int
    columns: dict[str, str]  # 目标列 -> 原 CSV 列
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "row_no": self.row_no,
            "columns": dict(self.columns),
            "note": self.note,
        }


# --------------------------------------------------------------------------- #
# 笔名声明
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PenNameDeclaration:
    """一条可生效的“署名—译者”笔名声明（某一不可变版本）。"""

    decl_id: str
    version: int
    pen_name: str
    translator_id: str
    translator_name: str
    source: DeclarationSource
    # 生效范围；None 表示不限定
    original_language: str | None = None
    volume_from: int | None = None
    volume_to: int | None = None
    withdrawn: bool = False

    @property
    def decl_key(self) -> str:
        """同一逻辑声明的稳定键（修订沿用、撤回按此键）。"""
        return self.decl_id

    def covers_volume(self, volume: int) -> bool:
        if self.volume_from is not None and volume < self.volume_from:
            return False
        if self.volume_to is not None and volume > self.volume_to:
            return False
        return True

    def language_scope(self) -> str | None:
        return self.original_language

    def as_dict(self) -> dict[str, Any]:
        return {
            "decl_id": self.decl_id,
            "version": self.version,
            "pen_name": self.pen_name,
            "translator_id": self.translator_id,
            "translator_name": self.translator_name,
            "original_language": self.original_language,
            "volume_from": self.volume_from,
            "volume_to": self.volume_to,
            "withdrawn": self.withdrawn,
            "source": self.source.as_dict(),
        }


# --------------------------------------------------------------------------- #
# 交柜的一册书
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HoldingCopy:
    """一册交柜的书。

    ``visible_fields`` 是该版扉页/题名页上**实际印着、可逐问核对**的字段集合；
    旧节印本删掉语种栏时，它不在该集合里，馆员便不能问读者语种。
    """

    copy_id: str
    volume: int
    edition: str
    pen_name: str
    visible_fields: frozenset[str]
    print_number: str | None = None  # 已印旧册自带印号；待印新版为 None
    original_language: str | None = None  # 编目著录的原文语种（可能不对读者可见）
    title: str = ""
    printable_new_edition: bool = False  # 是否为可走“重印配号”的待印新版

    def is_visible(self, field_name: str) -> bool:
        return field_name in ALWAYS_VISIBLE_FIELDS or field_name in self.visible_fields


# --------------------------------------------------------------------------- #
# 候选与消歧结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Candidate:
    """一册书上一条可成立的“署名—译者”关系，连同成立依据。"""

    translator_id: str
    translator_name: str
    declaration: PenNameDeclaration
    # 该候选通过了哪些**可询问**约束（字段名 -> 比对值）
    checked: dict[str, str]
    # 该候选携带、但此版无法核对的约束（字段名 -> 声明要求值）
    unchecked: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "translator_id": self.translator_id,
            "translator_name": self.translator_name,
            "checked": dict(self.checked),
            "unchecked": dict(self.unchecked),
            "declaration": self.declaration.as_dict(),
        }


@dataclass(frozen=True)
class HoldingEvaluation:
    """索引端对一册书的完整判断。"""

    copy_id: str
    pen_name: str
    candidates: tuple[Candidate, ...]
    # 此版可逐问核对的区分字段
    answerable_fields: tuple[str, ...]
    # 消歧所需、但书上没有答案的字段（写明还缺哪项）
    missing_fields: tuple[str, ...]
    unique: bool
    # 唯一时的译者；不唯一为 None
    resolved_translator_id: str | None
    resolved_translator_name: str | None
    status: str  # "resolvable" | "ambiguous" | "no-candidate"

    def as_dict(self) -> dict[str, Any]:
        return {
            "copy_id": self.copy_id,
            "pen_name": self.pen_name,
            "unique": self.unique,
            "status": self.status,
            "answerable_fields": list(self.answerable_fields),
            "missing_fields": list(self.missing_fields),
            "resolved_translator_id": self.resolved_translator_id,
            "resolved_translator_name": self.resolved_translator_name,
            "candidates": [c.as_dict() for c in self.candidates],
        }


# --------------------------------------------------------------------------- #
# 纯函数规则
# --------------------------------------------------------------------------- #
def candidate_identities(
    copy: HoldingCopy, declarations: list[PenNameDeclaration]
) -> tuple[Candidate, ...]:
    """编目端：返回该册可成立的全部“署名—译者”候选（含不可见约束的声明）。

    只按**恒成立/可核对**的条件筛选：署名一致、未撤回、卷号落在生效区间。
    语种限定若此版不可见，则不排除该声明（读者无法回答，不能据此淘汰），
    而把它记录到候选的 ``unchecked`` 里。
    """
    out: list[Candidate] = []
    for decl in declarations:
        if decl.withdrawn:
            continue
        if decl.pen_name != copy.pen_name:
            continue
        if not decl.covers_volume(copy.volume):
            continue

        checked: dict[str, str] = {}
        unchecked: dict[str, str] = {}

        # 卷号区间：卷号恒可见，已由 covers_volume 核对
        if decl.volume_from is not None or decl.volume_to is not None:
            checked[FIELD_VOLUME] = str(copy.volume)

        # 语种限定
        if decl.original_language is not None:
            if copy.is_visible(FIELD_ORIGINAL_LANGUAGE):
                # 扉页印有语种栏：可核对；不一致则该候选不成立
                if copy.original_language != decl.original_language:
                    continue
                checked[FIELD_ORIGINAL_LANGUAGE] = decl.original_language
            else:
                # 语种栏被删：不能问读者，不能排除，只能记为待核对
                unchecked[FIELD_ORIGINAL_LANGUAGE] = decl.original_language

        out.append(
            Candidate(
                translator_id=decl.translator_id,
                translator_name=decl.translator_name,
                declaration=decl,
                checked=checked,
                unchecked=unchecked,
            )
        )
    return tuple(out)


def evaluate_holding(
    copy: HoldingCopy, declarations: list[PenNameDeclaration]
) -> HoldingEvaluation:
    """索引端：只凭此版可印/可询问的字段，判断能否唯一找到译者。

    * 可询问字段 = 恒可见卷号 + ``visible_fields``；
    * 同一问题书上无答案（不可见）就不能问，相关字段进 ``missing_fields``；
    * 候选按译者去重（同一译者多条声明只算一个身份，但出处全部保留在候选里）。
    """
    candidates = candidate_identities(copy, declarations)

    # 按译者归并身份；保留该译者全部成立声明的出处
    by_translator: dict[str, list[Candidate]] = {}
    for cand in candidates:
        by_translator.setdefault(cand.translator_id, []).append(cand)

    merged: list[Candidate] = []
    for translator_id, cands in by_translator.items():
        merged_check: dict[str, str] = {}
        merged_uncheck: dict[str, str] = {}
        for c in cands:
            merged_check.update(c.checked)
            merged_uncheck.update(c.unchecked)
        merged.append(
            Candidate(
                translator_id=translator_id,
                translator_name=cands[0].translator_name,
                declaration=cands[0].declaration,
                checked=merged_check,
                unchecked=merged_uncheck,
            )
        )

    answerable = tuple(
        sorted(set(ALWAYS_VISIBLE_FIELDS) | set(copy.visible_fields))
    )
    language_answerable = copy.is_visible(FIELD_ORIGINAL_LANGUAGE)

    # 缺项判定（只写“书上没有答案、却又是消歧所需”的字段）：
    # 1) 任一候选带了此版无法核对的约束 -> 该字段是缺项；
    # 2) 候选不唯一且语种栏不可问 -> 语种本身就是还缺的那项，
    #    无论声明是否显式带语种限定（节印本删栏，谁也不能问读者）。
    missing: set[str] = set()
    for cand in merged:
        for f in cand.unchecked:
            missing.add(f)
    if len(merged) > 1 and not language_answerable:
        missing.add(FIELD_ORIGINAL_LANGUAGE)

    unique = len(merged) == 1
    if unique:
        only = merged[0]
        resolved_id = only.translator_id
        resolved_name = only.translator_name
        status = "resolvable"
    elif merged:
        resolved_id = None
        resolved_name = None
        status = "ambiguous"
    else:
        resolved_id = None
        resolved_name = None
        status = "no-candidate"

    return HoldingEvaluation(
        copy_id=copy.copy_id,
        pen_name=copy.pen_name,
        candidates=tuple(merged),
        answerable_fields=answerable,
        missing_fields=tuple(sorted(missing)),
        unique=unique,
        resolved_translator_id=resolved_id,
        resolved_translator_name=resolved_name,
        status=status,
    )
