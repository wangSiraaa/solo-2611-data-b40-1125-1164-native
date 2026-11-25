# 多采样率剪辑时间轴与真实混音档案（audiomix）

纯 Python 3.11 实现（NumPy/SciPy/标准 `wave`），无视频、无音频设备、无网页。
四部分：

1. **可独立编辑的音频时间轴服务**（`audiomix/timeline.py`）：插入/移动/修改/删除/
   区间查询/发布，clip 身份独立于素材，有理数时间，不渲染即可点查询。
2. **实际混音数据仓**（`audiomix/archive.py`）：只消费不可变发布产品与实际素材字节，
   固定 16kHz 输出，按固定样本块存档，内容寻址复用，支持范围读取与逐块追溯；
   不读取编辑器私有操作日志，也接受外部合法时间轴产品。
3. **对板室与放映排片**（`clapper.py`/`film.py`/`sync.py`/`venue.py`）：把数据仓里
   可重开的 16kHz 渲染当作录音原件，按短 PCM 模板读板响（带块/clip 来源），
   在片卷次序、两档走片速度与卷内单调约束下发布所有仍相容的声画对应，并据
   已锁片头、可换速卷界、可补无对白帧判定每条具名台词能否在**全部剩余解释**
   下落在允许帧内；导出可按帧查询的放映单与逐帧对齐音轨。
4. **两套对板制度**（`regimes.py`）：印死声画边条 vs 现场追板同步机，含一次
   现场才出现的板响下“锁定边界如何影响补救”的对比。

## 目录

```
audiomix/
  rationals.py   精确有理数（Fraction）表示与边界整数运算
  assets.py      PCM WAV 生成/读取 + 素材登记仓（asset_id=正文 SHA-256，绑定摘要/样本数）
  timeline.py    时间轴编辑器（整笔校验、点查询、不可变发布产品）
  engine.py      混音引擎：逐样本参考解释器 + 等价的 NumPy 分块向量化渲染
  archive.py     混音数据仓：内容寻址块存储 + SQLite 依赖索引 + 增量渲染 + 读取/追溯
  clapper.py     板响模板与录音格（十样本为一格）整格精确检测
  film.py        胶片卷/帧号/场记板标/具名台词/锁帧工程（可整份存盘重开）
  sync.py        对板求解（全部相容解释）+ 放映投影（可交付性）
  recording.py   把 archive render 当录音原件，板响带块/clip 来源
  venue.py       放映单（按帧查询）+ 逐帧音轨 + 回放（卷界换速/补帧）
  regimes.py     印死声画边条 vs 现场追板同步机 + 补救方案
  cli.py         命令行入口
tests/           手算案例、数据仓案例、边界语义、对板/放映测试
demo/            规模案例、CLI 端到端走查、对板/放映走查
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

## 对板与放映（录音原件、板响、排片、制度）

**模型**：十个录音样本为一格（格宽 1/1600 秒）。录音原件就是数据仓里某条
**可重开的 16kHz 渲染**（`render_id`），重开逐样本一致。板响来自事先给出的
短 PCM 模板，只在**整格对齐**处精确匹配（见 `clapper.py`），不涉及视频编解码。

**片卷与映射**：各卷按片卷次序首尾相接成全局帧与全局格。卷内局部帧 `f` 对到
局部录音格 `offset + f·gear`；`gear` 即走片速度（每胶片帧推进几格，题面
“每胶片帧推进一格”= 1），只能在卷界改档；`offset` 是待校起点（候选如 {0,1}）。
卷内单调。视觉场记给出“第某帧有某花纹的板标”，板标必须对到同花纹的真实板响。

**容差**：录音带拼接处最多漏一声、胶片同花纹最多跳一格，合并成“同花纹相邻板响
之间可缺 2 个未见板响”的预算——但**不放宽板标本身**。题面试例：第 1 帧板标只
可能对到唯一板响格 2 ⇒ 起点唯一为 1，格 3 的台词落第 2 帧。

**发布**：`sync-project` 给出每卷全部相容解释，并对每条具名台词给出
`possible_frames`（剩余解释下的可能帧）、`guaranteed_frames`（交集）与
`deliverable`（可能帧全落在允许帧内，且非已锁片头/院方静音帧）。两个反例均
复现：格 1、2 各一声同板 ⇒ 台词可为第 2 或第 3 帧，不能确保；原样带把该帧改为
院方静音帧 ⇒ 同样交不了。

```bash
# 读板响（带块/clip/素材来源）-> 对板求解 -> 放映投影
python3 -m audiomix.cli bang-detect  --archive-root DIR/archive --recording r1 --templates tpl.json
python3 -m audiomix.cli bang-solve   --project proj.json --archive-root DIR/archive --recording r1 --templates tpl.json
python3 -m audiomix.cli sync-project --project proj.json --archive-root DIR/archive --recording r1 --templates tpl.json

# 两套制度：印死边条 vs 现场追板同步机（可追加一次现场才出现的板响）
python3 -m audiomix.cli regime-assess --project proj.json --archive-root DIR/archive --recording r1 \
    --templates tpl.json --regime edge_strip --lock "二卷:0:1" --new-bang "B:11"
python3 -m audiomix.cli regime-remedy --project proj.json --archive-root DIR/archive --recording r1 \
    --templates tpl.json --regime live_sync --new-bang "B:11"

# 开单：放映单（按帧可查）+ 逐帧对齐音轨；回放（卷界换速/补无对白帧）
python3 -m audiomix.cli sheet-export --project proj.json --archive-root DIR/archive --recording r1 \
    --templates tpl.json --out sheet.json --track-out track.wav
python3 -m audiomix.cli sheet-query  --project proj.json --archive-root DIR/archive --recording r1 \
    --templates tpl.json --frame 2
python3 -m audiomix.cli playback     --project proj.json --archive-root DIR/archive --recording r1 \
    --templates tpl.json --play 0:6 --fill 10
```

**两套制度**（`regimes.py`）：
- **印死声画边条**：卷的 `(gear, offset)` 一次性印死，不可卷内逐句挪；印错只能等
  卷界重印（`regime-remedy` 报 `reprint_reels`）。
- **现场追板同步机**：未验证卷界须真拿一帧标定可补的无对白帧做校正，随后才能
  重锁（`regime-remedy` 报 `filler_frames`/`calibration_cost`）。已放帧保留，
  换速只可在卷界进行（`venue.Playback`）。

对板走查：`bash demo/sync_walkthrough.sh`（含一次现场才出现的板响下两种制度的补救对比）。

## 验证策略（不使用任何外部混音结果）

- `tests/test_hand_calc.py`：短整数序列素材，10 个案例人工推导每个输出样本
  （跨采样率插值、末样本保持、2 倍/半倍速、淡化边界、重叠相消、削顶、
  恰半取偶、跨块边界、整笔拒绝、48k 抽点），并要求**手算值 = 逐样本参考
  解释器 = 分块向量化渲染 = 逐块拼接**四者一致。
- `tests/test_archive.py`：内容寻址复用、移动 clip 的增量渲染、素材摘要变化
  零误用、外部产品篡改拒绝、编辑器日志移走后旧 render 仍可读。
- `tests/test_edge.py`：非整数 T、左闭右开边界、非法修改回滚、持久化重开、
  “先求和后单次舍入”与“逐片段先舍入”的语义区分。
- `tests/test_sync.py`：逐条复现题面“试片”三情形（唯一板响⇒起点1/台词第2帧；
  两声同板⇒不确定；院方静音帧⇒仍交不了）；板响整格检测与来源追溯；
  可重开录音逐样本一致；放映单按帧查询与存盘重开逐条一致；卷界换速/已放帧
  保留/补帧规则；两套制度在一次新板响下的补救差异；缺声预算。
- `demo/scale_demo.py`：30 素材（每采样率 10 个）、100 clip、120 秒/192 万样本，
  含全部要求情形；窗口化逐样本交叉核对，输出 WAV、块峰值、削顶数与来源索引。

参考解释器（`engine.render_reference`）是独立的纯 Python 逐样本 Fraction 实现，
与向量化渲染路径代码完全分开，仅共享产品装载/校验，作为语义基准。
