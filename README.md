# Parquet footer 统计驱动的保守 plan/scan

用 Python 3.11 + PyArrow 19 实现的小型 Parquet 查询工具：计划阶段只读 footer 统计、
保守剪枝行组；执行阶段先读过滤列做 SQL 三值逻辑精确过滤，再只为命中行读投影列。
不解析 SQL、不做损坏恢复、不使用 page index、不含远程 Range 服务与性能压测。

## 目录

```
fixtures/                 输入：files.json、queries.json、stats.parquet、no-stats.parquet
fixtures/press/           接印子系统输入：压样/盒数量/订单扁平 Parquet + 任务 JSON
tools/make_fixtures.py    从同一份数据生成“有统计/无统计”两个 Parquet（行组与行序一致）
tools/make_press_fixtures.py 生成接印夹具（7 个任务，含两组只改单一变量的翻转对照）
src/pqscan/
  expr.py       谓词树解析、schema 校验、PyArrow 表达式翻译、纯 Python 3VL 逐行求值
  stats.py      只读 footer 的行组统计提取与可靠性判定
  planner.py    三值逻辑保守行组推理（can_be_true / can_be_false、trace）
  scanner.py    两阶段列读取、精确行选择、读取请求在执行入口即时登记
  reference.py  独立核对：无剪枝全扫描 + 纯 Python 3VL（与引擎不同代码路径）
  runner.py     串联计划/扫描/核对，落盘 plans、results、run_record、verification
  press/        接印子系统：压样证据(evidence) -> 字粒身份(identity) -> 两晚排班(scheduler)
  cli.py        命令行入口（run / verify / accept）
tests/          pytest 套件（12 个原用例 + 17 个接印用例）
docs/plan_format.md  计划与运行记录的 JSON 格式契约
docs/press_format.md 接印任务、身份台账与决策凭据的格式契约
output/         运行产物（git 忽略）
```

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python tools/make_fixtures.py           # 重新生成两个夹具（可选，仓库已带）
.venv/bin/python tools/make_press_fixtures.py     # 重新生成接印夹具（可选，仓库已带）
PYTHONPATH=src .venv/bin/python -m pqscan.cli run \
    --files fixtures/files.json --queries fixtures/queries.json --out output
# 接印：一个任务 JSON 即可从命令行重跑两晚领物票的接/拒决策
PYTHONPATH=src .venv/bin/python -m pqscan.cli accept \
    --job fixtures/press/job_small_busy.json --out output/press/small_busy
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

接印输出：`proofs/`（复用 pqscan 的剪枝计划、带溯源的压样结果 Parquet、独立核对）、
`identity.json`（每粒字的 3VL 观察聚合与整盒数量分配）、`decision.json`
（每笔订单接/拒、拿哪盒、何时动哪块版、两晚台账）。格式契约见 `docs/press_format.md`。

## 支持范围

- 平面 schema；整数（有/无符号 int32/int64 等）与字符串列；
- 比较 `eq/ne/lt/le/gt/ge`、`is_null`、`and/or/not`、列投影；
- 谓词以 JSON 前缀树表达（见 `fixtures/queries.json`），不解析 SQL；
- 类型不匹配、列不存在、嵌套 schema 等在计划期报错；不处理文件损坏。

## 关键设计约束（与需求逐条对应）

1. **计划只读 footer。** `stats.py` 只访问 `FileMetaData/RowGroupMetaData/Statistics`，
   不调用任何数据读取接口。
2. **三值逻辑双结论。** 每个谓词子树求 `can_be_true`/`can_be_false`，
   NULL 产生的 UNKNOWN 既不算 TRUE 也不算 FALSE。`NOT` 同时翻转两个结论，
   不会出现“把 UNKNOWN 当 FALSE 再取反”的错误剪枝（见 `planner.py`）。
3. **统计缺失/不可靠即保守保留。** 无 Statistics、`has_min_max=False`（全 NULL 组的
   典型形态）、`null_count` 缺失、物理类型不受支持，叶子结论退化为
   `(true, true)` 并在 trace 的 `unknown` 中登记原因。PyArrow 19 的 Statistics
   不暴露 min/max 截断标记，因此对存在且类型受支持的 min/max 视为精确；
   若环境提供截断信息，应在 `stats.py` 将其降级为不可靠。
4. **AND/OR 不跨列关联极值。** 只组合独立子树的两个结论，不假设不同列的
   min/max 来自同一行。只有证明 `can_be_true=false` 才 `skip`。
5. **两阶段扫描。** 保留组先读过滤列；`Table.filter` 的 Kleene 语义丢弃
   UNKNOWN/FALSE、只留 TRUE，再用临时行号列取回组内物理行号。无命中组到此结束，
   不读 payload 等投影专用列；有命中组才读剩余投影列。过滤列同时在投影中时复用
   第一阶段数组，不会发起第二次读取（空列集合不产生请求）。
6. **读取计数来自执行入口。** `InstrumentedParquetFile.read_row_group` 在真实
   调用发生时登记 `(文件, 行组, phase, 列集合)`，计划器不参与填写；记录的是工具
   发起的列读取请求，不是操作系统字节数，也不用 footer 的压缩大小冒充 I/O。
7. **结果与溯源。** 输出按 files.json 文件顺序、行组物理索引、组内行号排列；
   附 `_source_file/_source_row_group/_source_row_in_group` 三列；重复数据保留；
   空结果写出同 schema 的零行 Parquet。
8. **独立核对。** `reference.py` 不看任何统计、每个行组全列全量读取，用纯 Python
   3VL 逐行求值，逐行比对引擎结果的内容、顺序与溯源。

## 夹具与现成查询的预期现象

两个 Parquet 由同一份 20 行数据写成，各 5 个行组、每组 4 行；RG2 的 `k` 全 NULL。

| 查询 | stats.parquet 计划 | no-stats.parquet 计划 |
| --- | --- | --- |
| `range`（k∈[14,30)） | 跳过 RG0/RG1/RG4；RG2 全 NULL 保守保留但过滤后无命中；仅 RG3 命中 | 5 组全保留，结果完全一致 |
| `nulls`（k IS NULL） | null_count=0 的 RG1/RG3/RG4 证明跳过；RG0/RG2 命中 | 全保留，结果一致 |
| `negated`（NOT kind='A'） | 全部保留（含 NULL 行，UNKNOWN 不取反为 TRUE） | 全保留，结果一致 |
| `or_missing_stats`（kind='A' OR amount>160） | 全部保留（OR 跨列独立推理） | 全保留，结果一致 |
| `no_hits`（k>1000） | RG0/RG1/RG3/RG4 证明跳过；RG2 统计缺失保守保留、过滤后无命中 | 全保留、全部无命中 |

可在 `output/run_record.json` 验证：`no_hits` 与 `range` 中所有
`no_hits_after_filter` 行组只有 `phase=filter` 的请求，从不读取投影专用列
（`row_id/payload/kind/amount`）；`planned_skip` 行组零请求。

## 测试覆盖

`tests/test_pqscan.py`（12 用例）覆盖：范围剪枝、全 NULL 组、IS NULL 的
null_count 推理、NOT 的 UNKNOWN 处理与可证明 FALSE、OR 跨列不关联极值、
无统计全保留、3VL 真值表、无命中不读投影列、过滤列复用、端到端与无剪枝
全扫描一致、有/无统计结果相同而计划保守度不同、空结果 schema 与输出顺序。

# 接印子系统（`pqscan accept`）

把同一套保守查询机器用到印刷工坊的领物票接排上：压样是扁平 Parquet 人工标记，
排班决策必须能向领班出示来源行与推理凭据。

## 领域到数据的映射

| 工坊概念 | 数据形态 |
| --- | --- |
| 压样（字粒号、盒号、压样时间、笔画/缺口、人工字面猜测） | 全标量扁平 Parquet，一行一个标记；`present` 为 NULL 表示“看不清” |
| 盒内各字铸字数量 | 扁平 Parquet（box_id/face/count） |
| 领物票（固定字位） | 扁平 Parquet，一个字位一行，订单级字段逐行重复 |
| 字面字典、每班可开盒、台面时段、旧版、封版 | 任务 JSON |

## 关键设计约束（与需求逐条对应）

1. **复用原查询筛压样、保留来源行。** `press/evidence.py` 把“两晚开始前”翻译成
   `taken_at < 第一晚起点` 谓词（可再 AND 任务自带的 `proof_predicate`），走与
   `run` 完全相同的 planner/scanner/reference 代码路径；结果 Parquet 带
   `_source_file/_source_row_group/_source_row_in_group`，并经无剪枝全扫描核对。
   两晚期间新压的样（夹具中 taken_at=140/150）在行组级被证明跳过。
2. **看不清是不完整观察（3VL）。** `present=NULL` 聚合为 UNKNOWN，既不肯定也不
   否定一个字面；同一特征看清结论互相冲突、或人工 hint 与看清事实冲突时，该粒
   判矛盾、不可用——同一字粒先后留下的笔画必须能解释为同一字面。
3. **整盒数量可排除分配。** 单粒候选不唯一时，用“每盒每字面用量等于铸字数量”
   做回溯 CSP；所有可行分配中字面相同才算“唯一逼出”（可交付），否则只给候选集。
   夹具 clear 版的 P4 单粒候选是 {甲,乙}，被盒数量逼成甲；vague 版三粒共享候选、
   三种分配并存，没有甲能交付。
4. **同版占字不跨机、封版两晚锁死。** 同一夜班一个字粒最多被一张票占用；
   `seal` 路径把字粒放进两晚的占用集，不耗排版台、开印不开盒；`recompose`
   只占当班、耗一个台面时段，旧版拆字免开盒，盒取字必须来自当班可开盒。
5. **确定性可重跑。** 订单按输入顺序 DFS，路径 recompose 优先于 seal，字位与字粒
   按字典序枚举；拒接带原因码（`piece_busy`/`machine_busy`/`box_not_open_that_night`/
   `no_composing_table_slot`/`no_eligible_piece` 等），全部结论附来源行。

## 夹具演示的翻转对照（两个盒、两个夜班以内）

| 任务对 | 唯一改动 | 决策翻转 |
| --- | --- | --- |
| `job_small_busy` → `job_small_switch` | 第二台的字位字面 甲→乙（压样可区分两粒） | 拒一笔 → 全接 |
| `job_proof_flip_clear` → `..._vague` | **只改压样**（缺口标记改为看不清） | 双甲字位接单 → `no_eligible_piece` 拒接 |
| `job_table_flip_tight` → `..._full` | **只改台面占用**（第二晚时段 1→2） | 第二张 `no_composing_table_slot` → 全接 |
| `job_lock` | 封版安排（P1 两晚锁死） | 同字另票 `piece_busy` 拒接 |

`tests/test_press.py`（17 用例）覆盖：迟到压样过滤与溯源、UNKNOWN 聚合、
特征/hint 矛盾、盒数量逼出与多分配不可逼、不可兑现盒台账、小单主线、
压样/台面两次翻转、封版两晚锁死、机器占用、闭盒下两条路径差异、嵌套 Parquet
拒绝与 CLI 可重跑。
