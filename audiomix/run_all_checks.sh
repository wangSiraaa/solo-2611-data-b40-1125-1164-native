#!/usr/bin/env bash
# 一键验证：车站报站词库（纯标准库）+ 既有时间轴/混音档案（需 NumPy/SciPy）
set -euo pipefail
cd "$(dirname "$0")"

echo "== 车站报站词库与出声台：单元测试 =="
python3 -m unittest tests.test_station_announce -v

echo "== 车站报站词库与出声台：题面全场景叙事 =="
python3 -m demo.station_scenario /tmp/audiomix_verify_station >/tmp/audiomix_station.log
tail -n 12 /tmp/audiomix_station.log

# 既有混音子系统依赖 NumPy/SciPy；环境缺失时跳过而非失败
if python3 -c "import numpy, scipy" 2>/dev/null; then
  echo "== 时间轴/混音档案：手算案例 + 数据仓 + 边界语义 + 规模案例 =="
  python3 -m tests.test_hand_calc /tmp/audiomix_verify_hand
  python3 -m tests.test_archive
  python3 -m tests.test_edge
  python3 -m demo.scale_demo /tmp/audiomix_verify_scale
else
  echo "== 跳过时间轴/混音档案测试（环境未安装 NumPy/SciPy）=="
fi

echo
echo "================ 全量验证完成 ================"
