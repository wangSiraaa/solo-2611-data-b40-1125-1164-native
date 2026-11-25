# 多采样率剪辑时间轴、真实混音档案与车站报站词库（audiomix）

纯 Python 3.11 实现，无视频、无音频设备、无网页。包含三个服务：

1. **可独立编辑的音频时间轴服务**（`audiomix/timeline.py`）：插入/移动/修改/删除/
   区间查询/发布，clip 身份独立于素材，有理数时间，不渲染即可点查询。（依赖 NumPy/SciPy）
2. **实际混音数据仓**（`audiomix/archive.py`）：只消费不可变发布产品与实际素材字节，
   固定 16kHz 输出，按固定样本块存档，内容寻址复用，支持范围读取与逐块追溯。（依赖 NumPy/SciPy）
3. **车站报站词库与出声台**（`audiomix/announce/`）：纯标准库子包，小整数 PCM、
   读音模板与容差、音高档位归并、母录/分出资产与不可变发版、接头拼接渲染、
   严格听辨裁决、买断/租用合同、排播与按原音回听。**无 NumPy 也可独立运行。**

## 目录

```
audiomix/
  rationals.py   精确有理数（Fraction）表示与边界整数运算
  assets.py      PCM WAV 生成/读取 + 素材登记仓（asset_id=正文 SHA-256，绑定摘要/样本数）
  timeline.py    时间轴编辑器（整笔校验、点查询、不可变发布产品）
  engine.py      混音引擎：逐样本参考解释器 + 等价的 NumPy 分块向量化渲染
  archive.py     混音数据仓：内容寻址块存储 + SQLite 依赖索引 + 增量渲染 + 读取/追溯
  cli.py         命令行入口
tests/           手算案例、数据仓案例、边界语义测试
demo/            30 素材/100 clip/120s 规模案例与 CLI 端到端走查
run_all_checks.sh 一键全量验证
```

## 核心语义（均为精确有理数，杜绝浮点边界误差）

- 素材：单声道有符号 16bit PCM，采样率仅 8000/16000/48000；
  `asset_id = SHA-256(PCM 正文)`，同字节内容永远同一身份，绑定摘要与样本数。
- clip：`clip_id` 与素材独立；同一素材可被任意多 clip 反复引用；多 clip 允许重叠。
  时长 `(b-a)/(源采样率×速度)`，参数全部为正/非负有理数。
- 编辑整笔拒绝：未知素材、源区间 `[a,b)` 越界、淡化之和超过 clip 时长等，
  单次/批量操作要么全部生效要么状态不变。
- 输出样本：固定 16kHz，总时长 T 显式给定，输出恰为所有满足
  `0 ≤ n/16000 < T` 的样本（数量 `ceil(16000T)`），区间一律左闭右开，
  不因浮点四舍五入多一个样本。
- 源映射：实数样本位置 `p = a + (t-start)×源采样率×速度`；相邻样本线性插值；
  `p` 到达最后一个样本之后（仍在 clip 时长内）保持末样本值，绝不读区间外。
- 淡化：增益乘 0→1 线性淡入、1→0 线性淡出；淡化时长为 0 时因子恒 1；
  边界用严格小于判定（`d < fade_in`、`remain < fade_out`）。
- **舍入只做一次**：所有有效 clip 的浮点贡献全部相加后，才做“恰半取偶”
  （banker's rounding）一次，再饱和到 `[-32768, 32767]`。单片段先裁剪/先舍入
  再相加是不同语义，本工程不采用。
- 块：每块 1600 样本（0.1s）。块存档记录峰值、实际削顶样本数、所用 clip/asset。

## 复用依据（为什么更正后不会误用旧块）

块键 `block_key = SHA-256(规范JSON{输出采样率, 块序号, 该块重叠到的全部 clip
完整身份与全部转换参数, 这些 clip 引用素材的摘要})`。

- 刻意**不含 product_id、不含 T**：时间轴更正产生新产品时，依赖逐项相同的
  未受影响块仍可复用；块内容与总时长无关。
- 任何重叠 clip 的参数变化或素材正文（摘要）变化都会令键改变 → 新产品不可能
  命中旧块。规模案例中移动一个 clip，1200 块里仅 2 块重渲染、1198 块复用。

## 命令速览（有理数接受 `1/3`、`0.25`、`7` 等写法）

```bash
# 素材登记（序列/斜坡/正弦/静音，自动生成 PCM WAV）
python3 -m audiomix.cli asset-seq --root DIR/registry --rate 16000 --values 0,100,200,300
python3 -m audiomix.cli asset-list --root DIR/registry

# 编辑
python3 -m audiomix.cli tl-insert --root DIR/registry --clip-id c1 \
    --asset-id AID --a 0 --b 4 --start 1/4000 --speed 2 --gain 3/2 \
    --fade-in 1/16000 --fade-out 1/8000
python3 -m audiomix.cli tl-move   --root DIR/registry --clip-id c1 --new-start 1/8000
python3 -m audiomix.cli tl-modify --root DIR/registry --clip-id c1 --set gain=1 --set speed=1
python3 -m audiomix.cli tl-delete --root DIR/registry --clip-id c1
python3 -m audiomix.cli tl-query  --root DIR/registry --lo 0 --hi 1/8000
python3 -m audiomix.cli tl-at     --root DIR/registry --time 1/16000   # 不渲染点查询
python3 -m audiomix.cli tl-publish --root DIR/registry --out product.json

# 校验/渲染/读取/追溯
python3 -m audiomix.cli verify-product --product product.json --registry-root DIR/registry
python3 -m audiomix.cli render-compare --product product.json \
    --registry-root DIR/registry --T 120          # 分块渲染 vs 独立逐样本解释器
python3 -m audiomix.cli render --archive-root DIR/archive --product product.json \
    --registry-root DIR/registry --T 120 --render-id r1
python3 -m audiomix.cli read  --archive-root DIR/archive --render-id r1 --start 0 --end 1600
python3 -m audiomix.cli trace --archive-root DIR/archive --render-id r1 --start 0 --end 3200
python3 -m audiomix.cli export-wav --archive-root DIR/archive --render-id r1 --out out.wav
python3 -m audiomix.cli verify-integrity --archive-root DIR/archive
```

完整端到端示例：`bash demo/cli_walkthrough.sh`；全量验证：`bash run_all_checks.sh`。

---

# 车站报站词库与出声台（`audiomix/announce`，纯标准库）

乘务员可再次调用的**词库发版、排播、试听入口**。素材是有明确采样边界的本地短录音，
一律用小整数 PCM 元组表示；全部状态可落盘为单个 `state.json`，重开后续办、回听。

## 目录

```
audiomix/announce/
  pcm.py        小整数 PCM；接头重叠固定格数、重叠采样相加、禁止重剪
  templates.py  读音模板（逐样本容差）、严格站名模板、发音人音高档位归并
  lexicon.py    整词母录/分出词头尾字、改版血缘、采样预算、不可变发版 Release
  render.py     接头表 SpliceTable；整词/拼接只从发版 Release 读真实 PCM 渲染
  hearing.py    严格听辨裁决：核对全部候选站（本站+相邻站），决定能否按方向开播
  contracts.py  买断 BUYOUT / 租用 RENTAL 两种合同与代价台账
  schedule.py   班次、广播窗、短单元独占预约、录音档期、签播凭证与拒播依据
  desk.py       乘务出声台：发版读取→渲染→听辨→按合同签播/拒播/约补录/回听
  store.py      JSON 持久化（落盘重开，旧播次 PCM 快照不变）
  cli.py        命令行入口
demo/station_scenario.py       题面全场景叙事（含全部验收断言）
demo/station_cli_walkthrough.sh CLI 端到端走查
tests/test_station_announce.py  21 个纯标准库 unittest
```

## 关键语义（每条都有测试与叙事留痕）

- **模板与容差**：`CharacterTemplate(字, 发音人, 档位, 参考PCM, 允许误差)`，
  逐样本绝对差不超容差且采样边界一致才算命中；一段录音同时落入两个字的容差时，
  两个读法都保留（`segment_matches`）。
- **音高档位归并**：同一发音人编号的片段共享一次录制的档位；某档位必须让该发音人
  **全部**片段同时成立，任一段无容许模板即整档排除（`analyze_speaker`）。复例中
  P1=[5] 同时兼容两字/落两档，P2=[8] 排除高档，最终只剩低档且保留 P1 的两个读法。
- **母录与分出资产**：母录 `[4,1,2]` 可分出词头 `[4]`、尾字 `[1,2]`，只记切片边界、
  不占录音采样预算；母录改版（如末样本 2→3）会**同时**替换母录与全部分出资产，
  已发版 `Release` 与已签播次仍指向改版前快照。
- **真实渲染只读取发版**：整词或两片段拼接都从 `Release.pcm_of` 取 PCM，未发版资产
  拒播；拼接按接头表固定重叠一格、重叠采样相加，无接头即“禁止重剪”拒播。
- **严格听辨**：零误差整句模板（甲 `[4,1,2]`、乙 `[4,1,3]`、丙 `[4,2,3]`，均三样本）。
  渲染命中邻站即“误听”须改选整词/另找接头/留待补录；一个候选都不命中即“缺录档”；
  广播窗按样本数硬约束且禁止截字，窗不足直接拒签。
- **合同**：买断店锁素材与档期、已签整段不得换尾、补录必须约新录音档，并受理
  “档期允许时补录后再播”的订单；租用店尾字可同店跨班复用，每次接头与听辨结论
  对应那次实际出声，短单元被另一班独占时本店不能借。
- **回听**：`PlayOrder` 保存签播时的 PCM 快照与听辨证据；车已发出的旧播次不受后续
  词库改版影响，`replay` 永远听到原音。
- **采样预算**：只有真实新录音计费，分出切片不计；题面各次录音合计远低于百样本，
  超限在录入时直接拒绝。

## 题面验收点对照（`python3 -m demo.station_scenario`）

| 题面要求 | 场景中的证据 |
| --- | --- |
| 零误差且三样本窗可播甲 | 首版 `M_JIA=[4,1,2]` 两店首班签播 |
| 改末样本为3，整词与分出尾字都无法播甲 | 改版后母录 `[4,1,3]`、尾字联动 `[1,3]`，R1 快照不变 |
| 恢复原版但窗缩到两样本且禁截字，不能签 | `WINDOW_TOO_SMALL` 拒签 |
| 同一发音人片段落两档、另一段排除一档 | 音高复例，最终仅“低档”成立且保留双读法 |
| 次班情形A：T_03 可借 | 拼接 `[4,1]+[0,3]=[4,1,3]` 签播乙；T_13 拼成丙站误听拒播 |
| 次班情形B：T_03 被他班独占 | 误听+独占双依据，租用店无播次，留待补录 |
| 买断店补录后再播 | 档期3 录入 `M_YI=[4,1,3]` 后班次3 播出；再办触发“已买断锁定” |
| 接回首班听当时的甲 | 两店 `replay(1)` 均为 `[4,1,2]` |
| 各次录音合计不超过百样本 | 预算检查输出 `recorded_samples <= 100` |

## 命令速览（工作区可反复调用，状态自动落盘）

```bash
python3 -m audiomix.announce.cli station-init --root DIR/station      # 初始化+首发
python3 -m audiomix.announce.cli station-catalog --root DIR/station   # 同一份资产目录
python3 -m audiomix.announce.cli station-release --root DIR/station --note 改版
# 整词签播/拒播（拒播退出码 2，便于脚本编排；买断缺录档会带 deferred_slot）
python3 -m audiomix.announce.cli station-evaluate --root DIR/station \
    --contract rental --trip 2 --plan splice --splice H_41 T_03
python3 -m audiomix.announce.cli station-evaluate --root DIR/station \
    --contract buyout --trip 2 --plan whole --asset M_JIA
python3 -m audiomix.announce.cli station-replay     --root DIR/station --contract rental --trip 1
python3 -m audiomix.announce.cli station-rejections --root DIR/station
python3 -m audiomix.announce.cli station-ledger     --root DIR/station
python3 -m audiomix.announce.cli station-budget     --root DIR/station
```

CLI 走查：`bash demo/station_cli_walkthrough.sh`；全量验证：`bash run_all_checks.sh`。

## 验证策略

- `tests/test_station_announce.py`：21 个 unittest 覆盖接头数学与禁重剪、容差/双读法/
  档位排除、母录改版联动与发版不可变、预算、四类听辨结论、发版读取、两种合同、
  次班 A/B 两情形、跨班复用、落盘重开回听。
- `demo/station_scenario.py`：把题面叙事按顺序办理一遍，关键结论均带 `assert`，
  并打印实际播音 PCM、拒播依据、两店台账与采样预算。


## 验证策略（不使用任何外部混音结果）

- `tests/test_hand_calc.py`：短整数序列素材，10 个案例人工推导每个输出样本
  （跨采样率插值、末样本保持、2 倍/半倍速、淡化边界、重叠相消、削顶、
  恰半取偶、跨块边界、整笔拒绝、48k 抽点），并要求**手算值 = 逐样本参考
  解释器 = 分块向量化渲染 = 逐块拼接**四者一致。
- `tests/test_archive.py`：内容寻址复用、移动 clip 的增量渲染、素材摘要变化
  零误用、外部产品篡改拒绝、编辑器日志移走后旧 render 仍可读。
- `tests/test_edge.py`：非整数 T、左闭右开边界、非法修改回滚、持久化重开、
  “先求和后单次舍入”与“逐片段先舍入”的语义区分。
- `demo/scale_demo.py`：30 素材（每采样率 10 个）、100 clip、120 秒/192 万样本，
  含全部要求情形；窗口化逐样本交叉核对，输出 WAV、块峰值、削顶数与来源索引。

参考解释器（`engine.render_reference`）是独立的纯 Python 逐样本 Fraction 实现，
与向量化渲染路径代码完全分开，仅共享产品装载/校验，作为语义基准。
