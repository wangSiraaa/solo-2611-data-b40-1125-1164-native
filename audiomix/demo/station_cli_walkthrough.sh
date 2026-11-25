#!/usr/bin/env bash
# 车站报站词库 CLI 端到端走查：发版 → 排播（签播/拒播依据）→ 回听 → 台账/预算
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=/tmp/audiomix_station_cli
rm -rf "$ROOT"
CLI="python3 -m audiomix.announce.cli"

echo "### 1. 初始化同一份词库并首发"
$CLI station-init --root "$ROOT"

echo; echo "### 2. 看资产目录（两家店同看这本）"
$CLI station-catalog --root "$ROOT"

echo; echo "### 3. 首班（班次1→甲站）整词签播"
$CLI station-evaluate --root "$ROOT" --contract rental --trip 1 \
    --plan whole --asset M_JIA
$CLI station-evaluate --root "$ROOT" --contract buyout --trip 1 \
    --plan whole --asset M_JIA

echo; echo "### 4. 次班改乙站：T_13 拼成丙站（邻站误听，拒播，退出码2）"
set +e
$CLI station-evaluate --root "$ROOT" --contract rental --trip 2 \
    --plan splice --splice H_41 T_13
echo "拒播退出码=$?"
set -e

echo; echo "### 5. 次班改乙站：T_03 拼成乙站（可签）"
$CLI station-evaluate --root "$ROOT" --contract rental --trip 2 \
    --plan splice --splice H_41 T_03

echo; echo "### 6. 买断店不受理拼接；整词乙缺录档，约新档期补录后再播"
$CLI station-evaluate --root "$ROOT" --contract buyout --trip 2 \
    --plan whole --asset M_JIA

echo; echo "### 7. 回听首班原音 [4,1,2]、查拒播依据、台账与预算"
$CLI station-replay --root "$ROOT" --contract rental --trip 1
$CLI station-rejections --root "$ROOT"
$CLI station-ledger --root "$ROOT"
$CLI station-budget --root "$ROOT"
