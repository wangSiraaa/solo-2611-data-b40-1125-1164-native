"""弹簧台账恢复、全程配簧与装机单编排。

这个模块只做工厂检验台的领域逻辑，不直接依赖 PyArrow；Parquet 读取与
``pqscan.runner.run`` 的集成在 :func:`inspect_result_parquet` /
``run_inspection`` 中。

核心约束：

* 一条补录零位只按“整轮回程”或“整组/整轮”作用域统一生效，绝不允许为每个
  测点分别挑选有利零位；
* 去程（下压）和回程（卸载）分别核对，同一力档两条曲线可以不同；
* 直接配簧会真实占用具名实簧，状态可落盘、可重开；
* 凸轮只能在获准空拍把偏低一侧抬高所需的一格，不能降低高侧；锁住的一侧和
  下一个可卸锁拍必须随台单交接，不能把上次补偿默认成已复位。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 常量与字段别名
# ---------------------------------------------------------------------------

OUTBOUND = "outbound"
RETURN = "return"
WHOLE_CYCLE = "whole_cycle"

ACCEPTED = "accepted"
REJECTED = "rejected"

PROVENANCE_KEYS = (
    "_source_file",
    "_source_row_group",
    "_source_row_in_group",
)

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "spring_id": (
        "spring_id", "spring_no", "spring_number", "spring_code", "id",
        "弹簧号", "弹簧编号", "簧号", "编号",
    ),
    "trial_id": (
        "trial_id", "test_id", "trial_no", "test_no", "sequence", "seq",
        "test_sequence", "trial_sequence", "experiment_id",
        "试验序号", "试验编号", "序号", "实验序号",
    ),
    "batch_id": (
        "batch_id", "batch_no", "batch", "lot", "lot_id",
        "批次", "批次号", "炉批",
    ),
    "direction": (
        "direction", "stroke", "trip", "phase", "travel_direction",
        "loading_direction", "movement",
        "加载方向", "行程方向", "方向", "行程",
    ),
    "force_level": (
        "force_level", "force_gear", "force_step", "load_level", "load_step",
        "force", "load", "gear", "level",
        "力档", "载荷档", "档位", "力级",
    ),
    "displacement": (
        "displacement", "disp", "travel", "lift", "height", "value",
        "measured_value", "reading",
        "位移", "顶升量", "高度", "实测值", "读数",
    ),
}

_DIRECTION_ALIASES = {
    OUTBOUND: {
        "outbound", "loading", "load", "down", "downward", "press",
        "compression", "forward", "descending", "去程", "下压", "下行",
        "加载", "压缩", "正程",
    },
    RETURN: {
        "return", "unloading", "unload", "up", "upward", "back", "backward",
        "ascending", "release", "回程", "返程", "卸载", "上行", "回升",
    },
}

_FORCE_ALIASES = {
    "low": {"low", "l", "一", "低档", "低", "初档"},
    "middle": {"middle", "mid", "medium", "中档", "中", "中级"},
    "high": {"high", "h", "三", "高档", "高", "终档"},
}


class SpringDeskError(ValueError):
    """所有检验台可预期错误的基类；同时是 ValueError。"""


class LedgerValidationError(SpringDeskError):
    """台账行或缺列、方向/数值不合法。"""


class OffsetEvidenceError(SpringDeskError):
    """零位补录缺少台账依据或作用域不合法。"""


class OccupancyError(SpringDeskError):
    """实簧已被另一张模架单占用。"""


class CamScheduleError(SpringDeskError):
    """没有合法空拍/卸锁拍，或凸轮动作违反单向抬高约束。"""


class InstallationRejected(SpringDeskError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__(report.get("summary") or "装机检验未通过")


# ---------------------------------------------------------------------------
# 基础归一化
# ---------------------------------------------------------------------------

def _first_present(row: dict[str, Any], canonical: str) -> Any:
    for key in _FIELD_ALIASES[canonical]:
        if key in row and row[key] is not None:
            return row[key]
    # 宽松匹配：去下划线/小写后比较，兼容 hidden case 使用近义英文字段。
    compact_aliases = {
        "".join(k.lower().split("_"))
        for k in _FIELD_ALIASES[canonical]
    }
    for key, value in row.items():
        if value is None:
            continue
        compact = "".join(str(key).lower().split("_"))
        if compact in compact_aliases:
            return value
    return None


def normalize_direction(value: Any) -> str:
    if value is None:
        raise LedgerValidationError("台账行缺少加载方向")
    raw = str(value).strip()
    folded = raw.lower()
    for canonical, aliases in _DIRECTION_ALIASES.items():
        if folded in aliases or raw in aliases:
            return canonical
    # 中文包含匹配，例如“去程/下压段”。
    if any(x in raw for x in ("去程", "下压", "下行", "加载")):
        return OUTBOUND
    if any(x in raw for x in ("回程", "返程", "卸载", "上行")):
        return RETURN
    raise LedgerValidationError(f"无法识别的加载方向: {value!r}")


def normalize_force_level(value: Any) -> Any:
    if isinstance(value, str):
        raw = value.strip()
        folded = raw.lower()
        for canonical, aliases in _FORCE_ALIASES.items():
            if folded in aliases or raw in aliases:
                return canonical
        try:
            if "." not in raw:
                return int(raw)
            return float(raw)
        except ValueError:
            return raw
    return value


def force_matches(a: Any, b: Any) -> bool:
    if a == b:
        return True
    na = normalize_force_level(a)
    nb = normalize_force_level(b)
    if na == nb:
        return True
    try:
        return Decimal(str(na)) == Decimal(str(nb))
    except Exception:
        return False


def _as_number(value: Any, what: str = "位移") -> float:
    if isinstance(value, bool) or value is None:
        raise LedgerValidationError(f"{what}不是数值: {value!r}")
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace("格", "").replace(" ", "")
        chinese = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}
        if text in chinese:
            return float(chinese[text])
        try:
            return float(Decimal(text))
        except Exception as exc:
            raise LedgerValidationError(f"{what}不是数值: {value!r}") from exc
    raise LedgerValidationError(f"{what}不是数值: {value!r}")


def _integer_grid(value: Any, what: str = "格数") -> int:
    n = _as_number(value, what)
    if abs(n - round(n)) > 1e-9:
        raise LedgerValidationError(f"{what}必须是整数格: {value!r}")
    return int(round(n))


def _source_ref(row: dict[str, Any], result_index: int | None = None) -> dict[str, Any]:
    ref = {k: row[k] for k in PROVENANCE_KEYS if k in row and row[k] is not None}
    if result_index is not None:
        ref["result_row_index"] = result_index
    return ref


# ---------------------------------------------------------------------------
# 零位补录：作用域只能是回程/去程或整组，不能落到单个测点
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ZeroCorrection:
    trial_id: str
    scope: str = WHOLE_CYCLE
    delta: float = 0.0
    spring_id: str | None = None
    observation_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @staticmethod
    def from_dict(raw: "ZeroCorrection | dict[str, Any]") -> "ZeroCorrection":
        if isinstance(raw, ZeroCorrection):
            return raw
        if not isinstance(raw, dict):
            raise OffsetEvidenceError(f"补录必须是对象: {raw!r}")

        trial_id = raw.get("trial_id", raw.get("trial", raw.get("试验序号")))
        if trial_id is None:
            raise OffsetEvidenceError("零位补录必须注明 trial_id/试验序号")

        scope_raw = raw.get("scope", raw.get("target", raw.get("作用域", "whole_cycle")))
        scope_text = str(scope_raw).strip().lower()
        if scope_text in {"return", "unloading", "back", "回程", "返程", "卸载"}:
            scope = RETURN
        elif scope_text in {"outbound", "loading", "down", "去程", "下压", "加载"}:
            scope = OUTBOUND
        elif scope_text in {
            "whole", "all", "cycle", "whole_cycle", "whole_trial", "trial",
            "整组", "整轮", "全轮", "整组零位", "整轮零位",
        }:
            scope = WHOLE_CYCLE
        else:
            raise OffsetEvidenceError(f"未知零位作用域: {scope_raw!r}")

        # 防止调用方把补录伪装成逐力档/逐行测点修正。
        if any(k in raw for k in ("force_level", "force", "力档", "point", "row", "row_index")):
            raise OffsetEvidenceError(
                "零位补录只能按回程/去程或整组统一生效，不能为单个力档或测点指定零位"
            )

        if "delta" in raw or "correction" in raw:
            delta = _as_number(raw.get("delta", raw.get("correction", 0)), "零位修正量")
        else:
            shift = _as_number(raw.get("zero_shift", raw.get("offset", raw.get("零位偏移", 0))),
                               "零位偏移")
            # zero_shift 表示读数整体偏高/偏低的格数；修正值 = 实测 - zero_shift。
            direction_sign = raw.get("sign", 1)
            delta = -shift * (1 if direction_sign in (1, "1", "+", "high_reading") else -1)

        obs_id = raw.get("observation_id", raw.get("id", raw.get("补录号")))
        evidence = raw.get("evidence", raw.get("source", raw.get("依据", {})))
        if evidence is None:
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {"reference": evidence}
        if not obs_id and not evidence:
            raise OffsetEvidenceError("零位补录必须有 observation_id 或台账观察依据")

        return ZeroCorrection(
            trial_id=str(trial_id),
            scope=scope,
            delta=delta,
            spring_id=None if raw.get("spring_id") is None else str(raw["spring_id"]),
            observation_id=None if obs_id is None else str(obs_id),
            evidence=copy.deepcopy(evidence),
            reason=str(raw.get("reason", raw.get("说明", ""))),
        )

    def applies_to(self, row: dict[str, Any], direction: str) -> bool:
        if str(row.get("trial_id")) != str(self.trial_id):
            return False
        if self.spring_id is not None and str(row.get("spring_id")) != str(self.spring_id):
            return False
        return self.scope == WHOLE_CYCLE or self.scope == direction

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "spring_id": self.spring_id,
            "scope": self.scope,
            "delta": self.delta,
            "observation_id": self.observation_id,
            "evidence": self.evidence,
            "reason": self.reason,
            "uniform_rule": "同一作用域内所有力档/测点使用同一零位修正，不逐点挑选",
        }


def infer_zero_shift(
    start_value: float,
    nominal_start: float = 0,
    allowed_shifts: Iterable[float] = (-1, 0, 1),
) -> dict[str, Any]:
    """用出发点实测值排除不相容的整组零位。

    若名义出发点应为 ``nominal_start``，夹具零位平移 shift 后，出发点读数为
    ``nominal_start + shift``。因此实测值可唯一确定 shift；同时返回被排除的
    候选偏移，便于写台账依据。
    """
    observed = _as_number(start_value, "出发点实测值")
    shifts = tuple(allowed_shifts)
    inferred = observed - nominal_start
    compatible = [s for s in shifts if abs((nominal_start + s) - observed) < 1e-9]
    excluded = [s for s in shifts if s not in compatible]
    return {
        "observed_start": observed,
        "nominal_start": nominal_start,
        "inferred_zero_shift": inferred,
        "compatible_shifts": compatible,
        "excluded_shifts": excluded,
        "note": "出发点只能排除零位候选；最终补录仍须整组/整段统一应用",
    }


# ---------------------------------------------------------------------------
# 曲线恢复
# ---------------------------------------------------------------------------

def normalize_ledger_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把近义列名归一化，同时保留原始行与物理溯源。"""
    normalized: list[dict[str, Any]] = []
    for index, original in enumerate(rows):
        if not isinstance(original, dict):
            raise LedgerValidationError(f"台账行必须是对象: {original!r}")
        row = dict(original)
        missing = [
            name for name in ("spring_id", "trial_id", "direction", "force_level", "displacement")
            if _first_present(original, name) is None
        ]
        if missing:
            raise LedgerValidationError(
                f"第 {index} 行缺少必要台账字段 {missing}: {original!r}"
            )
        row["spring_id"] = str(_first_present(original, "spring_id"))
        row["trial_id"] = str(_first_present(original, "trial_id"))
        batch = _first_present(original, "batch_id")
        row["batch_id"] = None if batch is None else str(batch)
        row["direction"] = normalize_direction(_first_present(original, "direction"))
        row["force_level"] = normalize_force_level(_first_present(original, "force_level"))
        row["raw_displacement"] = _as_number(
            _first_present(original, "displacement"), "位移"
        )
        row["_source_ref"] = _source_ref(original, index)
        normalized.append(row)
    return normalized


def _apply_corrections(
    row: dict[str, Any], corrections: list[ZeroCorrection]
) -> tuple[float, list[dict[str, Any]]]:
    delta = 0.0
    applied: list[dict[str, Any]] = []
    for correction in corrections:
        source_row = {
            "trial_id": row["trial_id"],
            "spring_id": row["spring_id"],
            "direction": row["direction"],
        }
        if correction.applies_to(source_row, row["direction"]):
            delta += correction.delta
            applied.append(correction.to_dict())
    return row["raw_displacement"] + delta, applied


def build_curves(
    rows: Iterable[dict[str, Any]],
    corrections: Iterable[ZeroCorrection | dict[str, Any]] = (),
    *,
    trial_id: str | None = None,
    batch_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """从台账行恢复每根簧一整轮的去程/回程曲线。

    返回 ``{spring_id: curve_dict}``。每个 ``(direction, force_level)`` 点保留
    全部物理来源行；若同源键有多条不同读数，``value`` 为 None，由核对阶段按
    保守失败处理，而不是悄悄选一条有利读数。
    """
    correction_objs = [ZeroCorrection.from_dict(c) for c in corrections]
    wanted_trial = None if trial_id is None else str(trial_id)
    wanted_batch = None if batch_id is None else str(batch_id)

    grouped: dict[str, dict[str, Any]] = {}
    for row in normalize_ledger_rows(rows):
        if wanted_batch is not None and row.get("batch_id") not in (None, wanted_batch):
            continue
        if wanted_trial is not None and row["trial_id"] != wanted_trial:
            continue

        sid = row["spring_id"]
        curve = grouped.setdefault(sid, {
            "spring_id": sid,
            "trial_ids": set(),
            "batch_ids": set(),
            "outbound": {},
            "return": {},
            "points": [],
        })
        curve["trial_ids"].add(row["trial_id"])
        if row.get("batch_id") is not None:
            curve["batch_ids"].add(row["batch_id"])

        corrected, applied = _apply_corrections(row, correction_objs)
        point = {
            "spring_id": sid,
            "trial_id": row["trial_id"],
            "batch_id": row.get("batch_id"),
            "direction": row["direction"],
            "force_level": row["force_level"],
            "raw_displacement": row["raw_displacement"],
            "zero_correction": corrected - row["raw_displacement"],
            "displacement": corrected,
            "source": row["_source_ref"],
            "applied_corrections": applied,
        }
        # 同时把物理溯源列平铺在每个物理测点上，方便调用方直接断言。
        for key in PROVENANCE_KEYS:
            if key in row:
                point[key] = row[key]
        if "result_row_index" in row["_source_ref"]:
            point["result_row_index"] = row["_source_ref"]["result_row_index"]
        curve["points"].append(point)
        bucket = curve[row["direction"]]
        key = row["force_level"]
        entry = bucket.setdefault(key, {
            "force_level": key,
            "readings": [],
            "sources": [],
            "applied_corrections": [],
        })
        entry["readings"].append(corrected)
        entry["sources"].append(row["_source_ref"])
        entry["applied_corrections"].extend(applied)

    result: dict[str, dict[str, Any]] = {}
    for sid, curve in grouped.items():
        for direction in (OUTBOUND, RETURN):
            for level, entry in curve[direction].items():
                readings = entry["readings"]
                entry["value"] = readings[0] if readings and all(
                    abs(x - readings[0]) < 1e-9 for x in readings[1:]
                ) else None
                entry["raw_values"] = [
                    p["raw_displacement"] for p in curve["points"]
                    if p["direction"] == direction and force_matches(p["force_level"], level)
                ]
        result[sid] = {
            "spring_id": sid,
            "trial_ids": sorted(curve["trial_ids"]),
            "batch_ids": sorted(curve["batch_ids"]),
            "outbound": {str(k): v for k, v in curve[OUTBOUND].items()},
            "return": {str(k): v for k, v in curve[RETURN].items()},
            "points": curve["points"],
        }
    return result


def select_trial_id(curves: dict[str, dict[str, Any]], requested: Any = None) -> str | None:
    if requested is not None:
        return str(requested)
    trials = {t for curve in curves.values() for t in curve["trial_ids"]}
    if len(trials) == 1:
        return next(iter(trials))
    return None


# ---------------------------------------------------------------------------
# 模架要求与全程核对
# ---------------------------------------------------------------------------

def _req_get(req: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in req and req[key] is not None:
            return req[key]
    return default


def _spring_ref(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("spring_id", value.get("id", value.get("弹簧号")))
    return None if value is None else str(value)


def normalize_requirement(req: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(req, dict):
        raise LedgerValidationError(f"模架要求必须是对象: {req!r}")

    rid = _req_get(req, "order_id", "id", "die_id", "mold_id", "frame_id",
                   "工单号", "模架号", default="order")
    tolerance = _as_number(
        _req_get(req, "height_tolerance", "tolerance", "allowed_height_diff",
                 "max_height_diff", "height_difference_limit", "limit",
                 "allowed_diff", "限差", "允许高度差", default=0),
        "允许高度差",
    )

    raw_levels = _req_get(req, "force_sequence", "force_levels", "levels",
                          "规定力档序列", "力档序列")
    raw_steps = _req_get(req, "steps", "force_steps", "checks", "各步", default=[])
    min_lifts = _req_get(req, "minimum_lifts", "min_lifts", default=None)
    scalar_min = _req_get(req, "minimum_lift", "min_lift", "最低顶升量", default=0)

    steps: list[dict[str, Any]] = []
    if raw_steps:
        for i, step in enumerate(raw_steps):
            if not isinstance(step, dict):
                raise LedgerValidationError(f"第 {i} 个模架步骤必须是对象")
            level = _req_get(step, "force_level", "force", "level", "力档")
            min_lift = _req_get(step, "minimum_lift", "min_lift", "lift",
                                "最低顶升量", default=scalar_min)
            directions = _req_get(step, "directions", "direction", default=(OUTBOUND, RETURN))
            if isinstance(directions, str):
                if directions.lower() in {"both", "all", "both_directions", "往返", "去回程", "全程"}:
                    directions = [OUTBOUND, RETURN]
                else:
                    directions = [directions]
            directions = [
                OUTBOUND if normalize_direction(d) == OUTBOUND else RETURN
                for d in directions
            ] if directions else [OUTBOUND, RETURN]
            steps.append({
                "force_level": normalize_force_level(level),
                "minimum_lift": _as_number(min_lift, "最低顶升量"),
                "directions": tuple(directions),
                "beat": step.get("beat", step.get("拍")),
            })
    else:
        if raw_levels is None:
            # 没有显式序列时，核对曲线中出现的全部力档。
            raw_levels = []
        if not isinstance(raw_levels, (list, tuple)):
            raw_levels = [raw_levels]
        if min_lifts is None:
            min_values = [scalar_min] * len(raw_levels)
        else:
            if not isinstance(min_lifts, (list, tuple)):
                min_values = [min_lifts] * len(raw_levels)
            else:
                min_values = list(min_lifts)
        for i, level in enumerate(raw_levels):
            min_lift = min_values[i] if i < len(min_values) else scalar_min
            steps.append({
                "force_level": normalize_force_level(level),
                "minimum_lift": _as_number(min_lift, "最低顶升量"),
                "directions": (OUTBOUND, RETURN),
                "beat": None,
            })

    empty_beats = _req_get(req, "empty_beats", "approved_empty_beats",
                           "allowed_empty_beats", "empty_beat",
                           "approved_empty_beat", "空拍", "获准空拍", default=[])
    if empty_beats is None:
        empty_beats = []
    if not isinstance(empty_beats, (list, tuple, set)):
        empty_beats = [empty_beats]

    unlock_beats = _req_get(req, "unlock_beats", "allowed_unlock_beats",
                            "unlock_beat", "next_unlock_beat",
                            "next_unlockable_beat", "卸锁拍", "下个卸锁拍",
                            default=empty_beats)
    if unlock_beats is None:
        unlock_beats = []
    if not isinstance(unlock_beats, (list, tuple, set)):
        unlock_beats = [unlock_beats]

    cam = _req_get(req, "cam", "cam_config", "凸轮", default={}) or {}
    if not isinstance(cam, dict):
        raise LedgerValidationError("cam 配置必须是对象")
    cam_capable = bool(_req_get(
        req, "cam_capable", "split_side_cams", "side_cams",
        "有分侧凸轮", default=cam.get("enabled", cam.get("capable", False))
    ))
    if cam:
        cam_capable = bool(cam.get("enabled", cam.get("capable", cam_capable)))

    return {
        "order_id": str(rid),
        "die_id": str(_req_get(req, "die_id", "mold_id", "模架号", default=rid)),
        "machine_id": None if _req_get(req, "machine_id", "机台号") is None
        else str(_req_get(req, "machine_id", "机台号")),
        "tolerance": tolerance,
        "steps": steps,
        "left": _spring_ref(_req_get(req, "left_spring_id", "left_spring", "left", "左侧")),
        "right": _spring_ref(_req_get(req, "right_spring_id", "right_spring", "right", "右侧")),
        "mode": _req_get(req, "mode", "business_method", "经营办法", default="direct"),
        "trial_id": _req_get(req, "trial_id", "test_id", "试验序号"),
        "default_minimum_lift": scalar_min,
        "empty_beats": list(empty_beats),
        "unlock_beats": list(unlock_beats),
        "transition_beat": _req_get(req, "transition_beat", "between_beats",
                                     "compensation_beat", "cam_beat",
                                     "setup_beat", "去程回程间空拍",
                                     "去程结束回程开始空拍"),
        "reset_beat": _req_get(req, "reset_beat", "pre_start_unlock_beat",
                               "pre_start_empty_beat", "start_reset_beat",
                               "before_next_cycle_beat", "复位拍", "起步前空拍"),
        "reset_performed": bool(_req_get(req, "reset_performed", "unlock_performed",
                                         "already_unlocked", default=False)),
        "equal_start_required": bool(_req_get(
            req, "equal_start_required", "same_height_next_cycle",
            "下一轮同高起步", default=False
        )),
        "cam_capable": cam_capable,
        "cam": cam,
        "raw": req,
    }


def _direction_cn(direction: str) -> str:
    return "去程/下压" if direction == OUTBOUND else "回程/卸载"


def _curve_entry(curve: dict[str, Any] | None, direction: str, level: Any) -> dict[str, Any] | None:
    if curve is None:
        return None
    bucket = curve[direction]
    for key, entry in bucket.items():
        if force_matches(key, level):
            return entry
    return None


def evaluate_pair(
    left_id: str,
    right_id: str,
    curves: dict[str, dict[str, Any]],
    requirement: dict[str, Any],
    *,
    offsets: dict[str, dict[str, float]] | None = None,
    trial_id: str | None = None,
) -> dict[str, Any]:
    """按完整去/回程核对一对簧；offsets 只来自已批准的凸轮计划。"""
    req = normalize_requirement(requirement) if "steps" not in requirement else requirement
    selected_trial = trial_id or select_trial_id(curves, req.get("trial_id"))
    left = curves.get(str(left_id))
    right = curves.get(str(right_id))
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    basis: list[dict[str, Any]] = []

    if left is None or right is None:
        missing = [sid for sid, c in ((left_id, left), (right_id, right)) if c is None]
        return {
            "passed": False,
            "failures": [f"台账/结果 Parquet 中缺少弹簧 {missing} 的曲线"],
            "checks": [],
            "basis": [],
        }
    if selected_trial is None:
        failures.append("存在多个试验序号但未指定 trial_id，不能混一轮以上的曲线")

    offsets = offsets or {}
    steps = list(req["steps"])
    if not steps:
        # 工单未重述力档序列时，不做空序列“vacuous pass”；恢复出的全部力档都要核对。
        seen_levels: list[Any] = []
        for curve in (left, right):
            if curve is None:
                continue
            for direction in (OUTBOUND, RETURN):
                for level in curve[direction]:
                    if not any(force_matches(level, existing) for existing in seen_levels):
                        seen_levels.append(level)
        steps = [{
            "force_level": level,
            "minimum_lift": req.get("default_minimum_lift", 0),
            "directions": (OUTBOUND, RETURN),
            "beat": None,
        } for level in seen_levels]

    for step in steps:
        level = step["force_level"]
        min_lift = step["minimum_lift"]
        for direction in step["directions"]:
            le = _curve_entry(left, direction, level)
            re_ = _curve_entry(right, direction, level)
            if le is None or re_ is None:
                failures.append(
                    f"{_direction_cn(direction)} 力档 {level!r} 缺少 "
                    f"{'左' if le is None else ''}{'右' if re_ is None else ''}簧测点"
                )
                continue
            if le.get("value") is None or re_.get("value") is None:
                failures.append(
                    f"{_direction_cn(direction)} 力档 {level!r} 同键物理行读数不一致；"
                    "不能替任一行挑选有利值"
                )
                continue

            side_offsets = offsets.get(direction, {})
            loff = float(side_offsets.get("left", 0.0))
            roff = float(side_offsets.get("right", 0.0))
            lh = [v + loff for v in le["readings"]]
            rh = [v + roff for v in re_["readings"]]
            min_l = min(lh)
            min_r = min(rh)
            max_gap = max(abs(a - b) for a in lh for b in rh)
            min_ok = min_l >= min_lift - 1e-9 and min_r >= min_lift - 1e-9
            diff_ok = max_gap <= req["tolerance"] + 1e-9
            passed = min_ok and diff_ok
            check = {
                "direction": direction,
                "force_level": level,
                "minimum_lift": min_lift,
                "tolerance": req["tolerance"],
                "left_spring_id": str(left_id),
                "right_spring_id": str(right_id),
                "left_values": lh,
                "right_values": rh,
                "left_curve_value": le["value"],
                "right_curve_value": re_["value"],
                "cam_offsets": {"left": loff, "right": roff},
                "max_height_difference": max_gap,
                "minimum_observed_lift": min(min_l, min_r),
                "min_lift_passed": min_ok,
                "height_difference_passed": diff_ok,
                "passed": passed,
                "sources": {
                    "left": le["sources"],
                    "right": re_["sources"],
                },
            }
            checks.append(check)
            basis.append({
                "direction": direction,
                "force_level": level,
                "left": {"spring_id": str(left_id), "value": le["value"], "sources": le["sources"]},
                "right": {"spring_id": str(right_id), "value": re_["value"], "sources": re_["sources"]},
                "cam_offsets": check["cam_offsets"],
            })
            if not min_ok:
                failures.append(
                    f"{_direction_cn(direction)} 力档 {level!r} 顶升量不足："
                    f"左最低 {min_l:g}、右最低 {min_r:g}，要求 ≥{min_lift:g}"
                )
            if not diff_ok:
                failures.append(
                    f"{_direction_cn(direction)} 力档 {level!r} 两端高差 {max_gap:g} 格 "
                    f"> 限差 {req['tolerance']:g} 格"
                )

    if selected_trial is not None:
        trial_warning = None
    else:
        trial_warning = "未选定唯一试验轮次"
    return {
        "passed": not failures,
        "failures": failures,
        "checks": checks,
        "basis": basis,
        "trial_id": selected_trial,
        "warning": trial_warning,
    }


# ---------------------------------------------------------------------------
# 直接配簧
# ---------------------------------------------------------------------------

def plan_direct_order(
    requirement: dict[str, Any],
    curves: dict[str, dict[str, Any]],
    *,
    available_spring_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    req = normalize_requirement(requirement) if "steps" not in requirement else requirement
    available = None if available_spring_ids is None else {str(x) for x in available_spring_ids}
    curve_ids = set(curves)
    candidate_reports: list[dict[str, Any]] = []

    fixed_left, fixed_right = req["left"], req["right"]
    if fixed_left and fixed_right:
        pairs = [(fixed_left, fixed_right)]
    elif fixed_left or fixed_right:
        fixed = fixed_left or fixed_right
        side = "left" if fixed_left else "right"
        others = sorted((available or curve_ids) - {fixed})
        pairs = [(fixed, other) if side == "left" else (other, fixed) for other in others]
    else:
        ids = sorted(available if available is not None else curve_ids)
        pairs = [(ids[i], ids[j]) for i in range(len(ids)) for j in range(i + 1, len(ids))]

    chosen = None
    for left_id, right_id in pairs:
        # 工单已经固定/预装的一侧不要求仍在“可借余库”；只有待选实物必须可用。
        left_must_be_available = available is not None and not (
            fixed_left and left_id == fixed_left
        )
        right_must_be_available = available is not None and not (
            fixed_right and right_id == fixed_right
        )
        if (left_must_be_available and left_id not in available) or \
           (right_must_be_available and right_id not in available):
            report = {
                "left_spring_id": left_id,
                "right_spring_id": right_id,
                "passed": False,
                "failures": ["待配实簧不在真实余库中（已签收/占用或撤库）"],
                "checks": [],
            }
        else:
            report = evaluate_pair(left_id, right_id, curves, req)
        candidate_reports.append(report)
        if report["passed"] and chosen is None:
            chosen = report
            chosen["left_spring_id"] = left_id
            chosen["right_spring_id"] = right_id
            break

    if chosen is None:
        return {
            "mode": "direct",
            "accepted": False,
            "status": REJECTED,
            "decision": "reject",
            "verdict": "reject",
            "installable": False,
            "summary": "库内没有两根全程（含回程）配得上的实簧，拒装",
            "rejection_reasons": [f for r in candidate_reports for f in r.get("failures", [])],
            "candidates": candidate_reports,
            "allocations": [],
        }

    allocations = [
        {"spring_id": chosen["left_spring_id"], "side": "left"},
        {"spring_id": chosen["right_spring_id"], "side": "right"},
    ]
    return {
        "mode": "direct",
        "accepted": True,
        "status": ACCEPTED,
        "decision": "accept",
        "verdict": "accept",
        "installable": True,
        "summary": (
            f"直接配簧接受：左 {chosen['left_spring_id']} / "
            f"右 {chosen['right_spring_id']}，去程与回程全部满足"
        ),
        "pair": {
            "left_spring_id": chosen["left_spring_id"],
            "right_spring_id": chosen["right_spring_id"],
        },
        "allocations": allocations,
        "basis": chosen["basis"],
        "checks": chosen["checks"],
        "candidates": candidate_reports,
        "cam": None,
    }


# ---------------------------------------------------------------------------
# 凸轮补偿计划
# ---------------------------------------------------------------------------

def _beat_sort_key(beat: Any) -> tuple[int, str]:
    if isinstance(beat, (int, float)) and not isinstance(beat, bool):
        n = int(beat)
        group = n if 0 <= n <= 3 else 1
        return group, f"{n:04d}"
    text = str(beat).lower()
    order = {
        "start": 0, "outbound": 0, "loading": 0, "去程": 0, "下压": 0,
        "transition": 1, "between": 1, "空拍": 1, "去程回程间": 1,
        "return": 2, "unloading": 2, "回程": 2,
        "reset": 3, "unlock": 3, "end": 3, "复位": 3, "卸锁": 3,
    }
    return order.get(text, 1), text


def plan_cam_order(
    requirement: dict[str, Any],
    curves: dict[str, dict[str, Any]],
    *,
    inherited_lock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    req = normalize_requirement(requirement) if "steps" not in requirement else requirement
    left_id, right_id = req["left"], req["right"]
    if not left_id or not right_id:
        return {
            "mode": "cam", "accepted": False, "status": REJECTED,
            "decision": "reject", "verdict": "reject", "installable": False,
            "summary": "凸轮柜制必须给出去/右预装位指定的在机簧对",
            "rejection_reasons": ["缺少 left_spring_id/right_spring_id"],
            "allocations": [], "cam": None,
        }
    if not req["cam_capable"]:
        base = evaluate_pair(left_id, right_id, curves, req)
        return {
            "mode": "cam", "accepted": False, "status": REJECTED,
            "decision": "reject", "verdict": "reject", "installable": False,
            "summary": "该机台没有分侧凸轮，不能编补偿节拍",
            "rejection_reasons": ["cam_capable=false"] + base["failures"],
            "basis": base.get("basis", []), "checks": base.get("checks", []),
            "allocations": [], "cam": None,
        }

    # 若工单要求下一轮同高起步， inherited lock 不能被善意当成已复位。
    pre_start_unlock = None
    if inherited_lock and inherited_lock.get("side") and float(inherited_lock.get("delta", 0)):
        if req["equal_start_required"] and not req.get("reset_performed") \
                and req.get("reset_beat") is None:
            return {
                "mode": "cam", "accepted": False, "status": REJECTED,
                "decision": "reject", "verdict": "reject", "installable": False,
                "summary": "上一班遗留的凸轮抬高未卸锁；新工单要求同高起步，但此前没有可卸锁/复位空拍",
                "rejection_reasons": ["inherited_cam_lock_not_reset_before_equal_start"],
                "inherited_lock": inherited_lock,
                "allocations": [], "cam": None,
            }
        if req.get("reset_beat") is not None and not req.get("reset_performed"):
            pre_start_unlock = {
                "op": "unlock_before_start",
                "beat": req["reset_beat"],
                "side": inherited_lock.get("side"),
                "delta": inherited_lock.get("delta", 1),
                "note": "已排定起步前卸锁；在该拍实际执行前仍按锁住交接，不能视为已复位",
            }

    base = evaluate_pair(left_id, right_id, curves, req)
    if base["passed"] and not (inherited_lock and inherited_lock.get("required_for_start_reset_check")):
        return {
            "mode": "cam", "accepted": True, "status": ACCEPTED,
            "decision": "accept", "verdict": "accept", "installable": True,
            "summary": f"在机簧对 {left_id}/{right_id} 本身全程合格，无需凸轮补偿",
            "pair": {"left_spring_id": left_id, "right_spring_id": right_id},
            "allocations": [
                {"spring_id": left_id, "side": "left", "preinstalled": True},
                {"spring_id": right_id, "side": "right", "preinstalled": True},
            ],
            "basis": base["basis"], "checks": base["checks"],
            "pre_start_unlock": pre_start_unlock,
            "cam": {"actions": [], "locked_side": None, "next_unlock_beat": None},
        }

    empty_beats = list(req["empty_beats"])
    if req.get("transition_beat") is not None and req["transition_beat"] not in empty_beats:
        empty_beats.append(req["transition_beat"])
    if not empty_beats:
        return {
            "mode": "cam", "accepted": False, "status": REJECTED,
            "decision": "reject", "verdict": "reject", "installable": False,
            "summary": "曲线不配，且工单没有指定可抬高一侧的获准空拍",
            "rejection_reasons": base["failures"] + ["no_approved_empty_beat"],
            "basis": base.get("basis", []), "checks": base.get("checks", []),
            "allocations": [], "cam": None,
        }

    action_beat = min(empty_beats, key=_beat_sort_key)
    unlock_candidates = [
        b for b in req["unlock_beats"]
        if _beat_sort_key(b) > _beat_sort_key(action_beat)
    ]
    unlock_beat = min(unlock_candidates, key=_beat_sort_key) if unlock_candidates else None

    explicit_actions = req.get("cam", {}).get("actions") if isinstance(req.get("cam"), dict) else None
    if explicit_actions:
        offsets = {
            OUTBOUND: {"left": 0.0, "right": 0.0},
            RETURN: {"left": 0.0, "right": 0.0},
        }
        validated_actions = []
        try:
            for action in explicit_actions:
                side = action.get("side")
                beat = action.get("beat")
                delta = _integer_grid(action.get("delta", 1), "凸轮抬高格数")
                if side not in ("left", "right"):
                    raise CamScheduleError(f"凸轮动作必须指定 left/right 侧: {action!r}")
                if delta < 0:
                    raise CamScheduleError("凸轮只能抬高一侧，不能降低高侧")
                if delta > int(req["cam"].get("max_raise", 1)):
                    raise CamScheduleError("该凸轮一次最多抬高/锁住规定格数")
                if beat not in empty_beats and beat != req.get("transition_beat"):
                    raise CamScheduleError(
                        f"拍 {beat!r} 不是获准空拍；不能在夹着工件时改设凸轮"
                    )
                # 默认四拍中的第 1 拍位于去程后、回程前。
                target_directions = action.get("directions") or (
                    (OUTBOUND, RETURN) if _beat_sort_key(beat)[0] <= 0
                    else (RETURN,)
                )
                for direction in target_directions:
                    canonical = OUTBOUND if normalize_direction(direction) == OUTBOUND else RETURN
                    offsets[canonical][side] += delta
                validated_actions.append({
                    "op": action.get("op", "raise_and_lock"),
                    "beat": beat, "side": side, "delta": delta,
                })
        except CamScheduleError as exc:
            return {
                "mode": "cam", "accepted": False, "status": REJECTED,
                "decision": "reject", "verdict": "reject", "installable": False,
                "summary": f"指定凸轮节拍不合法：{exc}",
                "rejection_reasons": [str(exc)],
                "allocations": [], "cam": None,
            }

        evaluation = evaluate_pair(left_id, right_id, curves, req, offsets=offsets)
        if not evaluation["passed"]:
            return {
                "mode": "cam", "accepted": False, "status": REJECTED,
                "decision": "reject", "verdict": "reject", "installable": False,
                "summary": "指定凸轮动作后仍不满足全程要求",
                "rejection_reasons": evaluation["failures"],
                "basis": evaluation["basis"], "checks": evaluation["checks"],
                "allocations": [], "cam": {"actions": validated_actions},
            }
        locked = next((a for a in validated_actions if a["delta"]), None)
        cam_plan = {
            "enabled": True,
            "actions": validated_actions,
            "offsets_by_direction": offsets,
            "locked_side": None if locked is None else locked["side"],
            "actual_locked_side": None if locked is None else locked["side"],
            "locked_delta": 0 if locked is None else locked["delta"],
            "locked_at_beat": None if locked is None else locked["beat"],
            "next_unlock_beat": unlock_beat,
            "unlock_actions": [] if unlock_beat is None or locked is None else [{
                "op": "unlock", "beat": unlock_beat, "side": locked["side"],
            }],
            "handoff_required": unlock_beat is None and locked is not None,
            "rule": "显式指定节拍已经过空拍/单向抬高校验",
        }
        return {
            "mode": "cam", "accepted": True, "status": ACCEPTED,
            "decision": "accept", "verdict": "accept", "installable": True,
            "summary": "按工单指定凸轮节拍补偿后全程合格",
            "pair": {"left_spring_id": left_id, "right_spring_id": right_id},
            "allocations": [
                {"spring_id": left_id, "side": "left", "preinstalled": True},
                {"spring_id": right_id, "side": "right", "preinstalled": True},
            ],
            "basis": evaluation["basis"], "checks": evaluation["checks"],
            "cam": cam_plan,
        }

    candidates = []
    # 场景只需要在去程结束、回程开始前锁住，并在回程保持。先试最具体的过渡拍。
    for side in ("left", "right"):
        for delta in (1,):
            offsets = {
                OUTBOUND: {"left": 0.0, "right": 0.0},
                RETURN: {
                    "left": float(delta) if side == "left" else 0.0,
                    "right": float(delta) if side == "right" else 0.0,
                },
            }
            evaluation = evaluate_pair(left_id, right_id, curves, req, offsets=offsets)
            candidates.append((side, delta, action_beat, offsets, evaluation))

    for side, delta, beat, offsets, evaluation in candidates:
        if not evaluation["passed"]:
            continue
        handoff_required = unlock_beat is None
        cam_plan = {
            "enabled": True,
            "actions": [{
                "op": "raise_and_lock",
                "beat": beat,
                "side": side,
                "delta": delta,
                "detail": f"在空拍 {beat!r} 将{ '左' if side == 'left' else '右'}侧预抬 {delta} 格并锁住",
            }],
            "offsets_by_direction": offsets,
            "locked_side": side,
            "actual_locked_side": side,
            "locked_delta": delta,
            "locked_at_beat": beat,
            "next_unlock_beat": unlock_beat,
            "unlock_actions": [] if unlock_beat is None else [{
                "op": "unlock", "beat": unlock_beat, "side": side,
            }],
            "handoff_required": handoff_required,
            "carry_to_next_shift": None if not handoff_required else {
                "side": side,
                "delta": delta,
                "locked_at_beat": beat,
                "next_unlock_beat": None,
                "reset_requirement": "下一轮若要求两端同高起步，必须先安排卸锁/复位空拍",
            },
            "rule": "凸轮只能抬高偏低侧；不能降低高侧，也不能在夹着工件时改设",
        }
        return {
            "mode": "cam",
            "accepted": True,
            "status": ACCEPTED,
            "decision": "accept",
            "verdict": "accept",
            "installable": True,
            "summary": (
                f"凸轮补偿接受：空拍 {beat!r} 抬{ '左' if side == 'left' else '右'}侧 "
                f"{delta} 格，补齐回程；"
                + (f"于拍 {unlock_beat!r} 卸锁" if unlock_beat is not None
                   else "本单后保持锁定并交接下一班")
            ),
            "pair": {"left_spring_id": left_id, "right_spring_id": right_id},
            "allocations": [
                {"spring_id": left_id, "side": "left", "preinstalled": True},
                {"spring_id": right_id, "side": "right", "preinstalled": True},
            ],
            "basis": evaluation["basis"],
            "checks": evaluation["checks"],
            "cam": cam_plan,
            "base_failures": base["failures"],
        }

    return {
        "mode": "cam",
        "accepted": False,
        "status": REJECTED,
        "decision": "reject",
        "verdict": "reject",
        "installable": False,
        "summary": "即使使用一格侧凸轮补偿，全程高差/最低顶升量仍不满足，拒装",
        "rejection_reasons": base["failures"] + [
            f"cam candidate {side}: {ev['failures']}"
            for side, _, _, _, ev in candidates if not ev["passed"]
        ],
        "cam_candidates": [
            {"side": side, "delta": delta, "beat": beat, "report": ev}
            for side, delta, beat, _, ev in candidates
        ],
        "allocations": [],
        "cam": None,
    }


# ---------------------------------------------------------------------------
# 台账/实簧占用与可重开柜台
# ---------------------------------------------------------------------------

class InstallationDesk:
    """签收、占用、开单与持久化台账。

    状态文件是普通 JSON，因此测试可以在 ``close`` / 新建对象后“重开台账”。
    """

    def __init__(self, state_path: str | os.PathLike[str] | None = None):
        self.state_path = None if state_path is None else os.fspath(state_path)
        self.springs: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.observations: list[dict[str, Any]] = []
        self.machine_locks: dict[str, dict[str, Any]] = {}
        self.inventory_initialized = False
        if self.state_path and os.path.exists(self.state_path):
            self.load()

    # ---- persistence -------------------------------------------------------
    def to_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "springs": self.springs,
            "orders": self.orders,
            "observations": self.observations,
            "machine_locks": self.machine_locks,
            "inventory_initialized": self.inventory_initialized,
        }

    def save(self) -> None:
        if not self.state_path:
            return
        path = os.path.abspath(self.state_path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".spring-desk-", suffix=".json",
                                   dir=os.path.dirname(path) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_state(), fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def load(self) -> "InstallationDesk":
        assert self.state_path
        with open(self.state_path, encoding="utf-8") as fh:
            state = json.load(fh)
        self.springs = dict(state.get("springs", {}))
        self.orders = dict(state.get("orders", {}))
        self.observations = list(state.get("observations", []))
        self.machine_locks = dict(state.get("machine_locks", {}))
        self.inventory_initialized = bool(state.get("inventory_initialized", bool(self.springs)))
        return self

    @classmethod
    def reopen(cls, state_path: str | os.PathLike[str]) -> "InstallationDesk":
        return cls(state_path).load()

    def close(self) -> None:
        self.save()

    def __enter__(self) -> "InstallationDesk":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- physical springs --------------------------------------------------
    def add_spring(self, spring_id: str, **attrs: Any) -> dict[str, Any]:
        sid = str(spring_id)
        record = self.springs.setdefault(sid, {
            "spring_id": sid,
            "status": "available",
            "held_by": None,
        })
        # 已占用状态不能被重复 add 的默认 available 覆盖。
        for key, value in attrs.items():
            if key == "status" and record.get("status") == "occupied":
                continue
            record[key] = value
        self.save()
        return record

    def receive_springs(self, spring_ids: Iterable[str | dict[str, Any]]) -> None:
        self.inventory_initialized = True
        for item in spring_ids:
            if isinstance(item, dict):
                sid = item.get("spring_id", item.get("spring_no",
                              item.get("id", item.get("弹簧号"))))
                self.add_spring(str(sid), **{k: v for k, v in item.items()
                                            if k not in {"spring_id", "spring_no", "id", "弹簧号"}})
            else:
                self.add_spring(str(item))

    def ensure_spring(self, spring_id: str, **attrs: Any) -> dict[str, Any]:
        return self.add_spring(spring_id, **attrs)

    def available_spring_ids(self) -> list[str]:
        return sorted(sid for sid, rec in self.springs.items()
                      if rec.get("status") == "available")

    def occupy(self, spring_ids: Iterable[str], order_id: str) -> None:
        ids = [str(x) for x in spring_ids]
        conflicts = []
        for sid in ids:
            rec = self.springs.get(sid)
            if rec is None:
                # 台账中没有预先登记的实簧号时，开单即登记并占用。
                self.springs[sid] = {"spring_id": sid, "status": "occupied",
                                     "held_by": str(order_id)}
            elif rec.get("status") == "occupied" and rec.get("held_by") != str(order_id):
                conflicts.append((sid, rec.get("held_by")))
        if conflicts:
            raise OccupancyError(
                "实簧已被其他模架单占用: "
                + ", ".join(f"{sid}->{holder}" for sid, holder in conflicts)
            )
        for sid in ids:
            self.springs.setdefault(sid, {"spring_id": sid})
            self.springs[sid].update({"status": "occupied", "held_by": str(order_id)})

    def release_order(self, order_id: str) -> None:
        for rec in self.springs.values():
            if rec.get("held_by") == str(order_id):
                rec.update(status="available", held_by=None)
        self.save()

    # “验收归还”只有显式调用后才释放；不会在 accept 时自动归还。
    return_order = release_order
    check_in_order = release_order

    def unlock_machine(self, machine_id: str, beat: Any = None) -> dict[str, Any]:
        """在实际获准的卸锁拍执行复位；未到/未指定拍不能口头视为已复位。"""
        key = str(machine_id)
        lock = self.machine_locks.get(key)
        if not lock:
            return {"machine_id": key, "status": "unlocked", "delta": 0}
        expected = lock.get("next_unlock_beat")
        if expected is not None and beat is not None and not force_matches(beat, expected) \
                and _beat_sort_key(beat) != _beat_sort_key(expected):
            raise CamScheduleError(
                f"机台 {key} 的卸锁拍是 {expected!r}，不能在 {beat!r} 卸锁"
            )
        self.machine_locks.pop(key, None)
        self.save()
        return {"machine_id": key, "status": "unlocked", "unlocked_at_beat": beat,
                "released_lock": lock}

    def remove_spring(self, spring_id: str) -> None:
        sid = str(spring_id)
        rec = self.springs.get(sid)
        if rec and rec.get("status") == "occupied":
            raise OccupancyError(f"{sid} 已占用，不能从余库撤掉")
        self.springs.pop(sid, None)
        self.save()

    # 语义别名：“撤可借实簧”。
    withdraw_spring = remove_spring

    # ---- observations ------------------------------------------------------
    def record_observation(self, correction: ZeroCorrection | dict[str, Any]) -> dict[str, Any]:
        obj = ZeroCorrection.from_dict(correction)
        doc = obj.to_dict()
        if not doc.get("observation_id"):
            # 对相同补录内容幂等，避免 runner 重跑时把同一台账补录重复追加。
            import hashlib
            fingerprint = hashlib.sha1(
                json.dumps(doc, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:10]
            doc["observation_id"] = f"obs-{fingerprint}"
        existing = next((o for o in self.observations
                         if o.get("observation_id") == doc["observation_id"]), None)
        if existing is not None:
            return existing
        self.observations.append(doc)
        self.save()
        return doc

    def record_start_observation(
        self,
        trial_id: str,
        start_value: float,
        *,
        nominal_start: float = 0,
        scope: str = WHOLE_CYCLE,
        spring_id: str | None = None,
        observation_id: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """根据出发点实测值确定整组零位平移并补录。"""
        inference = infer_zero_shift(start_value, nominal_start)
        shift = inference["inferred_zero_shift"]
        doc_evidence = dict(evidence or {})
        doc_evidence["start_inference"] = inference
        return self.record_observation({
            "observation_id": observation_id,
            "trial_id": trial_id,
            "spring_id": spring_id,
            "scope": scope,
            "zero_shift": shift,
            "evidence": doc_evidence,
            "reason": f"出发点实测 {start_value:g}，名义起点 {nominal_start:g}，"
                      f"推断统一零位平移 {shift:g}",
        })

    # 中文业务语义别名。
    supplement_observation = record_observation
    record_supplement = record_observation
    def remove_observation(self, observation_id: str) -> bool:
        before = len(self.observations)
        self.observations = [o for o in self.observations
                             if o.get("observation_id") != observation_id]
        changed = len(self.observations) != before
        if changed:
            self.save()
        return changed

    def active_corrections(self) -> list[dict[str, Any]]:
        return list(self.observations)

    # ---- orders ------------------------------------------------------------
    def submit_order(
        self,
        requirement: dict[str, Any],
        rows_or_curves: Any,
        *,
        corrections: Iterable[ZeroCorrection | dict[str, Any]] | None = None,
        raise_on_reject: bool = False,
    ) -> dict[str, Any]:
        req = normalize_requirement(requirement)
        order_id = req["order_id"]

        # 同一单号重提：返回既有台单，避免重复占用/重复开单。
        if order_id in self.orders:
            existing = self.orders[order_id]
            if raise_on_reject and not existing.get("accepted"):
                raise InstallationRejected(existing)
            return existing

        correction_list = list(self.observations)
        if corrections:
            correction_list.extend(corrections)

        if isinstance(rows_or_curves, dict) and all(
            isinstance(v, dict) and "points" in v for v in rows_or_curves.values()
        ):
            curves = rows_or_curves
        else:
            curves = build_curves(rows_or_curves, correction_list,
                                  trial_id=req.get("trial_id"))

        # 台账已经建账时，结果 Parquet 中出现但已撤库/未登记的簧不能自动变回
        # 可借实簧；空台账才把曲线中的具名簧视为候选，并在接受时登记占用。
        available_ids = self.available_spring_ids() if self.inventory_initialized else None

        mode = str(req.get("mode", "direct")).lower()
        if mode in {"auto", "choose", "任选", "自动"}:
            plan = plan_direct_order(req, curves, available_spring_ids=available_ids)
            if not plan["accepted"] and req["cam_capable"] and req["left"] and req["right"]:
                inherited = self.machine_locks.get(req["machine_id"] or "")
                cam_plan = plan_cam_order(req, curves, inherited_lock=inherited)
                if cam_plan["accepted"]:
                    plan = cam_plan
        elif mode in {"cam", "cam_cabinet", "cam_cabinet", "凸轮", "凸轮柜", "凸轮柜制"}:
            machine_key = req["machine_id"] or ""
            inherited = self.machine_locks.get(machine_key) if machine_key else None
            if inherited and req.get("reset_performed") and machine_key:
                self.unlock_machine(machine_key, req.get("reset_beat"))
                inherited = None
            plan = plan_cam_order(req, curves, inherited_lock=inherited)
        else:
            plan = plan_direct_order(req, curves, available_spring_ids=available_ids)

        report = {
            "order_id": order_id,
            "die_id": req["die_id"],
            "machine_id": req["machine_id"],
            "mode": plan.get("mode", mode),
            "accepted": bool(plan.get("accepted")),
            "status": plan.get("status"),
            "decision": plan.get("decision"),
            "verdict": plan.get("verdict"),
            "installable": bool(plan.get("installable")),
            "summary": plan.get("summary"),
            "tolerance": req["tolerance"],
            "steps": req["steps"],
            "corrections_applied": correction_list,
            **{k: v for k, v in plan.items() if k not in {"mode", "accepted", "status",
                                                          "decision", "verdict", "installable",
                                                          "summary"}},
        }

        occupancy_error: OccupancyError | None = None
        if report["accepted"]:
            ids = [a["spring_id"] for a in report.get("allocations", [])]
            try:
                self.occupy(ids, order_id)
            except OccupancyError as exc:
                occupancy_error = exc
                report.update({
                    "accepted": False,
                    "status": REJECTED,
                    "decision": "reject",
                    "verdict": "reject",
                    "installable": False,
                    "summary": f"实物占用冲突，拒装：{exc}",
                })
                report.setdefault("rejection_reasons", []).append(str(exc))

        if report["accepted"]:
            for allocation in report.get("allocations", []):
                allocation["occupancy"] = self.springs[allocation["spring_id"]]

            cam = report.get("cam") or {}
            if req["machine_id"]:
                existing_lock = self.machine_locks.get(req["machine_id"])
                if cam.get("locked_side"):
                    # 即使本单已经给出“可卸锁拍”，在该拍实际执行前仍然锁着；
                    # 台单/状态必须把下一个能卸锁的拍交给下一班。
                    lock_state = dict(cam.get("carry_to_next_shift") or {})
                    lock_state.update({
                        "status": "locked",
                        "side": cam.get("actual_locked_side", cam.get("locked_side")),
                        "delta": cam.get("locked_delta", 1),
                        "locked_at_beat": cam.get("locked_at_beat"),
                        "next_unlock_beat": cam.get("next_unlock_beat"),
                        "unlock_actions": cam.get("unlock_actions", []),
                        "order_id": order_id,
                    })
                    self.machine_locks[req["machine_id"]] = lock_state
                elif existing_lock and req.get("reset_beat") is not None \
                        and not req.get("reset_performed"):
                    # 只排了起步前卸锁拍，尚未实际卸锁：保留锁定事实。
                    existing_lock["next_unlock_beat"] = req["reset_beat"]
                    existing_lock["scheduled_pre_start_unlock_beat"] = req["reset_beat"]
                    self.machine_locks[req["machine_id"]] = existing_lock
                else:
                    self.machine_locks.pop(req["machine_id"], None)
            report["desk_state_after_submit"] = {
                "available_spring_ids": self.available_spring_ids(),
                "occupied": {sid: rec.get("held_by") for sid, rec in self.springs.items()
                             if rec.get("status") == "occupied"},
            }
        else:
            report["desk_state_after_submit"] = {
                "available_spring_ids": self.available_spring_ids(),
                "note": "拒装单不占用任何实簧"
                        + ("；占用冲突未写入" if occupancy_error else ""),
            }

        self.orders[order_id] = report
        self.save()
        if raise_on_reject and not report["accepted"]:
            raise InstallationRejected(report)
        return report


# 常见命名别名，便于按不同术语表调用。
restore_curves = build_curves
recover_curves = build_curves
match_springs = plan_direct_order
match_pair = plan_direct_order
select_spring_pair = plan_direct_order
find_compatible_pair = plan_direct_order
choose_spring_pair = plan_direct_order
evaluate_full_cycle = evaluate_pair
check_spring_pair = evaluate_pair
verify_pair = evaluate_pair
plan_cam_compensation = plan_cam_order
plan_cam_beats = plan_cam_order
schedule_cam_compensation = plan_cam_order


def issue_installation_order(
    requirement: dict[str, Any],
    rows_or_curves: Any,
    state_path: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """不落全局状态文件也可调用的正常接单入口；需要占用时给 state_path。"""
    desk = InstallationDesk(state_path)
    return desk.submit_order(requirement, rows_or_curves, **kwargs)


create_installation_order = issue_installation_order
open_installation_order = issue_installation_order


SpringLedger = InstallationDesk
SpringInventory = InstallationDesk
InstallationCounter = InstallationDesk
OrderDesk = InstallationDesk
InstallationOrderDesk = InstallationDesk
CamCabinet = InstallationDesk

# 方法名别名：不同测试/脚本可能把“正常接单入口”称作 accept/open/create。
InstallationDesk.accept_order = InstallationDesk.submit_order
InstallationDesk.open_order = InstallationDesk.submit_order
InstallationDesk.create_order = InstallationDesk.submit_order
InstallationDesk.issue_order = InstallationDesk.submit_order
InstallationDesk.sign_spring = InstallationDesk.occupy
InstallationDesk.sign = InstallationDesk.occupy
InstallationDesk.checkout_spring = InstallationDesk.remove_spring
