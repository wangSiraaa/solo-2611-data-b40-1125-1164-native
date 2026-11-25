# 多采样率剪辑时间轴与真实混音档案（audiomix）

纯 Python 3.11 实现（NumPy/SciPy/标准 `wave`），无视频、无音频设备、无网页。
两个独立服务：

1. **可独立编辑的音频时间轴服务**（`audiomix/timeline.py`）：插入/移动/修改/删除/
   区间查询/发布，clip 身份独立于素材，有理数时间，不渲染即可点查询。
2. **实际混音数据仓**（`audiomix/archive.py`）：只消费不可变发布产品与实际素材字节，
   固定 16kHz 输出，按固定样本块存档，内容寻址复用，支持范围读取与逐块追溯；
   不读取编辑器私有操作日志，也接受外部合法时间轴产品。
3. **虚拟电话线与回送台**（`audiomix/lines.py` + `audiomix/console.py`）：两位电话
   嘉宾的独立干声/来线观测 PCM、逐层轨接线发布记录，以及按拍排程的排他回路、
   有限缓存、拍界换路与逐耳时间轴交付。不靠总母带作差还原个人语音。

## 目录

```
audiomix/
  rationals.py   精确有理数（Fraction）表示与边界整数运算
  assets.py      PCM WAV 生成/读取 + 素材登记仓（asset_id=正文 SHA-256，绑定摘要/样本数）
  timeline.py    时间轴编辑器（整笔校验、点查询、不可变发布产品）
  engine.py      混音引擎：逐样本参考解释器 + 等价的 NumPy 分块向量化渲染
  archive.py     混音数据仓：内容寻址块存储 + SQLite 依赖索引 + 增量渲染 + 读取/追溯
  lines.py       虚拟电话线：独立干声/来线 PCM、层轨与折返接线发布记录、母带作差反例
  console.py     回送台：路权簿、排他回路、有限缓存、拍界换路、回执与逐耳时间轴
  cli.py         命令行入口
tests/           手算案例、数据仓案例、边界语义、电话回送台案例（pytest）
demo/            30 素材/100 clip/120s 规模案例、CLI 走查、电话回送台四场景
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

## 虚拟电话线与回送台（`lines.py` / `console.py`）

**为什么不能靠总母带作差**：母带是两条来线相加后再饱和的结果。甲乙同拍各说
20000 时真实和为 40000，母带只剩削顶值 32767，`32767-20000=12767` 与乙的真实
干声 20000 完全不符；两线同值时差值也无法归属样本是谁说的。因此每位嘉宾的
干声 PCM 与每条来线的观测 PCM 都**独立生成、独立登记**为素材（各有 asset_id
与摘要），控制台只消费这些独立来线与不可变接线记录。

- **接线发布记录 `WiringManifest`（`audiomix.phone-wiring/1`，内容寻址）**：
  来线每一拍的全部层轨——`dry` 干声层（本线本拍）与 `echo` 折返层（来源嘉宾、
  源拍、回路 `route_id`、整数拍延迟）。`validate()` 逐层核对素材摘要/取值，并验证
  “观测 PCM == 该线全部层轨之和”；同值不同身份（乙干声 7 与甲折返的 7）是两条
  层轨、两个素材，永不按值合并。`loopback_status()` 给出每个干声样本是否已折返、
  经哪条回路、到达哪一拍。1 拍 = 1/16000 秒，延迟恒为整数拍，全程整数运算。
- **路权簿 `CircuitLedger`（`audiomix.circuit-ledger/1`）**：采用“公用回路按段
  换耳”经营法；整场专属预约就是覆盖 `[0,n_beats)` 的预约，落在同一本簿里。同一
  回路同拍不得重复预约（重叠/越界整笔拒绝），换路只在拍界，`earliest_free_slot()`
  保证尾样未送完的区间不被抢占——旧段尾音先按原预约送完。路权簿可落盘重开。
- **回送台 `ReturnConsole.plan()`**：干声片段只排给对方耳机（本句不回自己耳，
  折返层不进入任何重放），每段对每耳恰好一次；按当班窗口 `max_delay_beats` 在
  空闲/将释放的回路中确定性选路。交付不了的窗口出 `UndeliverableReceipt`，
  reason 只有两类：`missing_line`（缺哪个来线）与 `no_route_rights`（缺哪条路权，
  含“窗口内缺路权”，细节给出最早可换路拍与窗口上界）。
- **逐耳可渲染时间轴**：每条投递落成一个 clip（引用源嘉宾独立干声素材的
  `[source_lo,source_hi)`，放在输出拍 `[play_lo,play_hi)`），走既有时间轴发布 →
  数据仓渲染 → 重开 → 区间读取链路。更正素材产生新 asset_id/新产品/新 render，
  旧 render 原样保留（已播轨不重写，内容寻址保证新 render 不会误用旧块）。

```bash
# 四个场景端到端（每个场景 <= 8 拍），逐耳 render/计划/路权簿/接线记录落盘
python3 -m demo.phone_demo /tmp/audiomix_phone
# 用既有 CLI 独立区间读取、追溯、完整性复核两只耳机
bash demo/phone_walkthrough.sh
# 单元测试（14 项手算断言）
python3 -m pytest tests/test_phone.py -q
```

场景对应：S1 削顶/同值（两回路各听对方 20000 → 乙改 10000 只甲耳变 → 单回路
禁延迟出回执 → 允许拖一拍即交付）；S2 拍0 甲7、拍1 乙线=干声3+折返7=10 的两层轨
核对、撞值版保留乙的 7；S3 尾样未播完 + 第二回路整场订走时的换线签认与无等候
回执；S4 缺来线回执。

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
