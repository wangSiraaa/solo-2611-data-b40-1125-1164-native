#!/usr/bin/env bash
# 一键验证：手算案例 + 数据仓案例 + 边界语义 + 规模案例（30 素材/100 clip/120s）
#          + 虚拟电话线/回送台案例（pytest，14 项，全部场景 <= 8 拍）
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
$PY -m tests.test_hand_calc /tmp/audiomix_verify_hand
$PY -m tests.test_archive
$PY -m tests.test_edge
$PY -m demo.scale_demo /tmp/audiomix_verify_scale
$PY -m pytest tests/test_phone.py -q
$PY -m demo.phone_demo /tmp/audiomix_verify_phone
echo
echo "================ 全量验证通过 ================"
