#!/usr/bin/env bash
# 一键验证：手算案例 + 数据仓案例 + 边界语义 + 规模案例（30 素材/100 clip/120s）
#          + 气象传真制版（真实声带发布/编辑/重读/开馆/两工坊回放）
set -euo pipefail
cd "$(dirname "$0")"
python3 -m tests.test_hand_calc /tmp/audiomix_verify_hand
python3 -m tests.test_archive
python3 -m tests.test_edge
python3 -m demo.scale_demo /tmp/audiomix_verify_scale
python3 -m tests.test_fax
python3 -m demo.fax_demo /tmp/audiomix_verify_fax
echo
echo "================ 全量验证通过 ================"
