#!/usr/bin/env bash
# 听写机一键验证：题面逐句验收（12 案例）+ CLI 端到端走查
# 依赖：numpy（audiomix 同级包，自动加入 sys.path）
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
echo "== 1/3 题面逐句验收（离线八击两联，含混音三方等价、退格覆盖、迟来压痕、只读重播） =="
"$PYTHON" -m tests.test_dictatype /tmp/dictatype_verify

echo "== 2/3 边界与持久化语义（整笔拒绝、压痕/行尾剪枝、JSON 往返、只读保护） =="
"$PYTHON" -m tests.test_dictatype_edge /tmp/dictatype_edge

echo "== 3/3 CLI 端到端走查（刊行号开档/重开、封存/勘误/签收、只读回执重播） =="
PYTHON="$PYTHON" bash demo/cli_walkthrough.sh >/tmp/dictatype_walkthrough.log 2>&1
tail -3 /tmp/dictatype_walkthrough.log

echo
echo "================ 听写机全量验证通过 ================"
