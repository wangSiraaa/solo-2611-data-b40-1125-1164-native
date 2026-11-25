#!/usr/bin/env bash
# 对板/放映端到端走查：录音原件->读板响(带来源)->求解->投影->制度对比->
# 放映单/按帧查询/音轨->存盘重开->旧放映单复查。
set -euo pipefail
cd "$(dirname "$0")/.."
D=/tmp/audiomix_syncdemo
PY="python3 -m audiomix.cli"
python3 -m demo.sync_demo

summ() { python3 -c 'import sys,json;d=json.load(sys.stdin);print(json.dumps(d,ensure_ascii=False,sort_keys=True))'; }

echo "== 1. 从录音原件读板响与来源（块/clip/素材） =="
$PY bang-detect --archive-root "$D/archive" --recording rec1 --templates "$D/templates.json" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  板响:",[(b["pattern"],b["cell"],"block",b["block_index"]) for b in d["bangs"]])'

echo "== 2. 对板求解：每卷相容解释 =="
$PY bang-solve --project "$D/project.json" --archive-root "$D/archive" --recording rec1 \
  --templates "$D/templates.json" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  counts:",d["counts"])'

echo "== 3. 放映投影：各台词可交付性 =="
$PY sync-project --project "$D/project.json" --archive-root "$D/archive" --recording rec1 \
  --templates "$D/templates.json" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  deliverable:",d["deliverable_lines"],"undeliverable:",d["undeliverable_lines"])'

echo "== 4. 现场才出现的一声板响：B 又在全局格11（二卷局部格1） =="
echo "   -- 开放解释（未锁）--"
$PY regime-assess --project "$D/project.json" --archive-root "$D/archive" --recording rec1 \
  --templates "$D/templates.json" --regime live_sync --new-bang "B:11" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("   deliverable:",d["deliverable_lines"],"undeliverable:",d["undeliverable_lines"])'

echo "   -- 印死边条：二卷锁在 offset1（正确）--"
$PY regime-assess --project "$D/project.json" --archive-root "$D/archive" --recording rec1 \
  --templates "$D/templates.json" --regime edge_strip --lock "二卷:0:1" --new-bang "B:11" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("   deliverable:",d["deliverable_lines"],"undeliverable:",d["undeliverable_lines"])'

echo "   -- 印死边条：二卷锁在 offset0（错误）-> 须重印 --"
$PY regime-remedy --project "$D/project.json" --archive-root "$D/archive" --recording rec1 \
  --templates "$D/templates.json" --regime edge_strip --lock "二卷:0:0" --new-bang "B:11" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);r=d["remedy"];print("   undeliverable:",d["assessment"]["undeliverable_lines"],"reprint:",r["reprint_reels"],"fill:",r["filler_frames"])'

echo "   -- 现场追板同步机：未验证卷界须真拿一帧空白校正 --"
$PY regime-remedy --project "$D/project.json" --archive-root "$D/archive" --recording rec1 \
  --templates "$D/templates.json" --regime live_sync --new-bang "B:11" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);r=d["remedy"];print("   需校正卷:",d["assessment"]["needs_calibration_reels"],"校正消耗帧:",r["filler_frames"],"cost:",r["calibration_cost"])'

echo "== 5. 开单：导出放映单 + 逐帧对齐音轨 =="
$PY sheet-export --project "$D/project.json" --archive-root "$D/archive" --recording rec1 \
  --templates "$D/templates.json" --out "$D/sheet.json" --track-out "$D/sheet_track.wav"
$PY sheet-query --project "$D/project.json" --archive-root "$D/archive" --recording rec1 \
  --templates "$D/templates.json" --frame 2 \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("   帧2:",{k:d[k] for k in ("reel_id","local_frame","sample_range","line_ids","flags")})'
$PY sheet-query --project "$D/project.json" --archive-root "$D/archive" --recording rec1 \
  --templates "$D/templates.json" --line "台词2" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("   台词2 落在帧:",d["global_frame"],"格:",d["cell"])'

echo "== 6. 旧放映单复查：重开工程重算放映单必须逐条一致 =="
python3 - <<'PY'
import json
from audiomix.film import FilmProject
from audiomix.archive import ArchiveStore
from audiomix.clapper import TemplateBook
from audiomix.recording import Recording
from audiomix.sync import solve
from audiomix.venue import ScreeningSheet
D = "/tmp/audiomix_syncdemo"
proj = FilmProject.load(D + "/project.json")
store = ArchiveStore(D + "/archive")
book = TemplateBook([__import__("audiomix.clapper", fromlist=["template"]).template(n, v)
                     for n, v in json.load(open(D + "/templates.json"))["patterns"].items()])
dets = Recording(store, "rec1").detect(book)
chosen = {r: sorted(s, key=lambda x: (x.gear_index, x.offset))[0]
          for r, s in solve(proj, dets).items()}
man = ScreeningSheet(proj, chosen).to_manifest()
old = json.load(open(D + "/sheet.json"))
# 忽略不稳定的创建信息：只比几何/帧条目/台词帧
for key in ("reel_boundaries", "frame_entries", "line_frames", "reels"):
    assert man[key] == old[key], f"放映单复查不一致: {key}"
print("   [OK] 旧放映单复查：几何/逐帧/台词帧逐条一致")
store.close()
PY

echo "对板/放映走查通过"
