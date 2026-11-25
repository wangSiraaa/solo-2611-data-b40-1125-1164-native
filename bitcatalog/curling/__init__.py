"""桌上冰壶清场交接子包：量尺目录、整盘量测、计分台与裁判室。

分层一览：

* :mod:`bitcatalog.curling.ruler`    俯拍量尺小目录（三活动分支）、原仓入帧与指纹/帧号读回
* :mod:`bitcatalog.curling.geometry` 每次曝光一套校正、双钉唯一/单钉双假设、整盘壶心还原
* :mod:`bitcatalog.curling.scoring`  犯规撤除与圈半径另管；严格近距、并列记零、无对方壶全计
* :mod:`bitcatalog.curling.referee`  封桌/还桌两种交接办法，受理计分、放桌、补拍与申诉
* :mod:`bitcatalog.curling.scenarios` 题面全部示例的精确数据与可复跑时间线
"""

from .geometry import (
    Calibration,
    CalibrationError,
    ExposureObs,
    ExposureSolution,
    Plate,
    Point,
    calibrate_from_pins,
    calibrate_hypotheses,
    exact_sqrt,
    reconstruct_plate,
    solve_exposure,
)
from .referee import (
    CheckpointVerdict,
    End,
    EvidenceFrame,
    MAX_REPLAY_STONES,
    RELEASE,
    SEAL,
    RefereeError,
    RefereeOffice,
    SupplementReceipt,
)
from .ruler import (
    BUS,
    SELECTOR_CENTER_NAIL,
    SELECTOR_PIN,
    SELECTOR_STONE,
    RulerRecord,
    decode_frame,
    encode_record,
    readback_frame,
    ruler_product,
)
from .scoring import (
    BLUE,
    RED,
    ScoreCase,
    ScoreDesk,
    ScoreRange,
    naive_per_stone_minimum,
    score_plate,
    score_range,
)

__all__ = [
    # geometry
    "Point", "Plate", "Calibration", "CalibrationError",
    "ExposureObs", "ExposureSolution", "calibrate_from_pins",
    "calibrate_hypotheses", "exact_sqrt", "reconstruct_plate", "solve_exposure",
    # ruler
    "BUS", "SELECTOR_PIN", "SELECTOR_STONE", "SELECTOR_CENTER_NAIL",
    "RulerRecord", "ruler_product", "encode_record", "decode_frame", "readback_frame",
    # scoring
    "RED", "BLUE", "ScoreDesk", "ScoreCase", "ScoreRange",
    "score_plate", "score_range", "naive_per_stone_minimum",
    # referee
    "SEAL", "RELEASE", "MAX_REPLAY_STONES", "RefereeOffice", "RefereeError",
    "End", "EvidenceFrame", "SupplementReceipt", "CheckpointVerdict",
]
