#!/usr/bin/env bash
# 虚拟电话线/回送台端到端走查：
#   1) 跑全部班次（发布接线记录 -> 逐耳时间轴发布 -> 数据仓渲染 -> 重开 -> 区间读取）
#   2) 用既有 CLI 对两只耳机的 render 独立做区间读取、块追溯与完整性校验
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
ROOT=/tmp/audiomix_phone_cli
ARCH="$ROOT/products/archive"
REG="$ROOT/registry"

echo "== 1. 四个场景的回送台班次（削顶/同值、折返延迟、迟到换线、缺来线） =="
$PY -m demo.phone_demo "$ROOT" >/dev/null
echo "  OK：全部断言通过，逐耳 render 每条 8 拍"

echo "== 2. CLI 独立复核 S1 两回路班次的两只耳机（拍1 各听对方 20000） =="
$PY -m audiomix.cli read --archive-root "$ARCH" \
    --render-id S1-two-routes-earA --start 0 --end 8 \
  | $PY -c 'import sys,json; d=json.load(sys.stdin); assert d["samples"][1]==20000; print("  甲耳:", d["samples"])'
$PY -m audiomix.cli read --archive-root "$ARCH" \
    --render-id S1-two-routes-earB --start 0 --end 8 \
  | $PY -c 'import sys,json; d=json.load(sys.stdin); assert d["samples"][1]==20000; print("  乙耳:", d["samples"])'

echo "== 3. CLI 复核 S2：甲耳只听乙本次3，乙耳的7只出现一次 =="
$PY -m audiomix.cli read --archive-root "$ARCH" --render-id S2-live-earA \
    --start 0 --end 8 | $PY -c 'import sys,json; d=json.load(sys.stdin); assert d["samples"]==[0,3,0,0,0,0,0,0]; print("  甲耳:", d["samples"])'
$PY -m audiomix.cli read --archive-root "$ARCH" --render-id S2-live-earB \
    --start 0 --end 8 | $PY -c 'import sys,json; d=json.load(sys.stdin); assert d["samples"]==[7,0,0,0,0,0,0,0]; print("  乙耳:", d["samples"])'

echo "== 4. 块追溯与全仓完整性（含历史 render 未被重写） =="
$PY -m audiomix.cli trace --archive-root "$ARCH" --render-id S3-late-ok-earA \
    --start 0 --end 8 | $PY -c 'import sys,json; d=json.load(sys.stdin); print("  追溯块数:", len(d), "来源 clip:", sorted({c for t in d for c in t["clip_ids"]}))'
$PY -m audiomix.cli verify-integrity --archive-root "$ARCH" \
  | $PY -c 'import sys,json; d=json.load(sys.stdin); print("  完整性:", d)'

echo "== 5. 旧 render 重开仍可读（乙干声改版后，旧甲耳拍1 仍=20000） =="
$PY -m audiomix.cli read --archive-root "$ARCH" --render-id S1-two-routes-earA \
    --start 0 --end 8 | $PY -c 'import sys,json; assert json.load(sys.stdin)["samples"][1]==20000; print("  历史发布不可重写：OK")'
echo "电话回送台走查全部通过"
