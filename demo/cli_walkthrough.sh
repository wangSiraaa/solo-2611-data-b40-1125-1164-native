#!/usr/bin/env bash
# 听写机端到端走查（离线最多八击两联）：
#   开档 -> 录证据 -> 听打求解 -> 校样封存/勘误/迟来压痕 -> 封档 -> 刊行号重开 -> 只读重播
# 复用仓库内 audiomix 的实际混音数据仓做声学渲染，不另造混音语义。
set -euo pipefail

ROOT=/tmp/dictatype_cli
rm -rf "$ROOT"
mkdir -p "$ROOT"
STORE="$ROOT/store"
SPEC="$ROOT/spec.json"
EVID="$ROOT/evidence.json"
PY="${PYTHON:-python3} -m dictatype.cli"

echo "== 1. 准备会话规格：模板 A=[1]、B=[2]，两联（乙联第二击前揭走） =="
cat > "$SPEC" <<'JSON'
{
  "session_id": "walk-ab",
  "keys": [
    {"key_id": "A", "action": "print_right", "glyph": "A", "sample": [100]},
    {"key_id": "B", "action": "print_right", "glyph": "B", "sample": [200]}
  ],
  "copies": [
    {"copy_id": "top", "recipient": "顶联收件人", "peel_after_strike": null},
    {"copy_id": "yi",  "recipient": "乙联收件人", "peel_after_strike": 1}
  ],
  "allowed_beats": [0, 1],
  "start_position": 0,
  "line_width": 16,
  "max_strikes": 8
}
JSON

echo "== 2. 先用 audiomix 路径渲染实录 PCM（两击不重叠：[100,0,0,0,200]） =="
cat > "$ROOT/strikes.json" <<'JSON'
{"strikes": [
  {"seq": 1, "beat": 0, "key_id": "A"},
  {"seq": 2, "beat": 1, "key_id": "B"}
]}
JSON
$PY render-demo --spec "$SPEC" --strikes "$ROOT/strikes.json" \
                --workdir "$ROOT/render" --render-id walk-truth \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["block_equals_reference"]; print("  实录 PCM =", d["samples"])'

echo "== 3. 录入三路证据（实录 PCM、手录压痕格、行尾位置） =="
cat > "$EVID" <<'JSON'
{
  "pcm": [100, 0, 0, 0, 200],
  "indentation": {
    "top": [{"cell": 0, "state": "present", "arrived_late": false},
            {"cell": 1, "state": "present", "arrived_late": false}],
    "yi":  [{"cell": 0, "state": "present", "arrived_late": false},
            {"cell": 1, "state": "absent",  "arrived_late": false}]
  },
  "line_end": 2
}
JSON

echo "== 4. 开档分配刊行号、挂证据、听打台求解 =="
ISSUE=$($PY issue-open --store "$STORE" --spec "$SPEC" \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["issue_no"])')
echo "  刊行号=$ISSUE"
$PY evidence-add --store "$STORE" --issue "$ISSUE" --evidence "$EVID" \
  | python3 -c 'import sys,json;print("  证据样本数:",json.load(sys.stdin)["n_samples"])'
$PY transcribe --store "$STORE" --issue "$ISSUE" --verbose \
  | python3 -c '
import sys,json
d=json.load(sys.stdin)
assert d["unique"], d
print("  相容解唯一:", [[(s["beat"],s["key_id"]) for s in sol] for sol in d["solutions"]])
for cid, vs in d["verdicts"].items():
    for v in vs:
        print("  %s 格%s: 字符=%s 覆盖击序=%s 可确定=%s" % (
            cid, v["cell"], v["glyph"], v["coverage_seqs"], v["determinate"]))
'

echo "== 5. 校样室封存乙联（第二击前揭走，只有格0=A）与顶联（AB） =="
$PY proof-seal --store "$STORE" --issue "$ISSUE" --copy yi \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  乙联封存:", [(c["cell"],c["glyph"]) for c in d["sealed_cells"]])'
$PY proof-seal --store "$STORE" --issue "$ISSUE" --copy top \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  顶联封存:", [(c["cell"],c["glyph"]) for c in d["sealed_cells"]])'

echo "== 6. 迟来压痕的差异化处理 =="
echo "  6a. 对已发乙联补一条格1迟来压痕：只能出编号勘误条，封存正文冻结"
$PY proof-late --store "$STORE" --issue "$ISSUE" --copy yi --cell 1 --state present --glyph B \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  处理:",d["late_handling"],"勘误号:",d["slip"]["slip_no"])'
echo "  6b. 新开一档演示未发联：先不封存顶联，迟来证据被同步复写吸收"
ISSUE2=$($PY issue-open --store "$STORE" --spec "$SPEC" \
         | python3 -c 'import sys,json;print(json.load(sys.stdin)["issue_no"])')
$PY evidence-add --store "$STORE" --issue "$ISSUE2" --evidence "$EVID"
$PY transcribe --store "$STORE" --issue "$ISSUE2" >/dev/null
$PY proof-late --store "$STORE" --issue "$ISSUE2" --copy top --cell 1 --state present --glyph B \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  处理:",d["late_handling"],"-",d["note"])'

echo "== 7. 具名联签收：乙联补齐后可签 =="
$PY proof-sign --store "$STORE" --issue "$ISSUE" --copy yi \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  乙联签收:",d["decision"]["signable"],"勘误条数:",len(d["decision"]["slips"]))'

echo "== 8. 封档后按刊行号重开（只读），写操作被拒 =="
$PY issue-close --store "$STORE" --issue "$ISSUE"
$PY issue-reopen --store "$STORE" --issue "$ISSUE" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["read_only"]; print("  重开只读:",d["state"])'
set +e
$PY proof-sign --store "$STORE" --issue "$ISSUE" --copy top >/dev/null
echo "  只读写操作退出码=$?（非零表示拒绝）"
set -e

echo "== 9. 重播一次只读历史收件回执（哈希校验，不重渲染） =="
$PY receipt-replay --store "$STORE" --issue "$ISSUE" --copy yi \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["hash_check"]=="ok"; print("  重播:",d["hash_check"],"封存格:",d["n_sealed_cells"],"勘误条:",d["n_errata_slips"])'

echo
echo "================ 听写机 CLI 端到端走查通过 ================"
