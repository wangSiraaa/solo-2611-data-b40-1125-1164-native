# Parquet footer 统计驱动的保守 plan/scan

用 Python 3.11 + PyArrow 19 实现的小型 Parquet 查询工具：计划阶段只读 footer 统计、
保守剪枝行组；执行阶段先读过滤列做 SQL 三值逻辑精确过滤，再只为命中行读投影列。
不解析 SQL、不做损坏恢复、不使用 page index、不含远程 Range 服务与性能压测。

## 目录

```
fixtures/                 输入：files.json、queries.json、stats.parquet、no-stats.parquet
tools/make_fixtures.py    从同一份数据生成“有统计/无统计”两个 Parquet（行组与行序一致）
src/pqscan/
  expr.py       谓词树解析、schema 校验、PyArrow 表达式翻译、纯 Python 3VL 逐行求值
  stats.py      只读 footer 的行组统计提取与可靠性判定
  planner.py    三值逻辑保守行组推理（can_be_true / can_be_false、trace）
  scanner.py    两阶段列读取、精确行选择、读取请求在执行入口即时登记
  reference.py  独立核对：无剪枝全扫描 + 纯 Python 3VL（与引擎不同代码路径）
  runner.py     串联计划/扫描/核对，落盘 plans、results、run_record、verification
  cli.py        命令行入口
tests/          pytest 套件（12 个用例）
docs/plan_format.md  计划与运行记录的 JSON 格式契约
output/         运行产物（git 忽略）
```

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python tools/make_fixtures.py           # 重新生成两个夹具（可选，仓库已带）
PYTHONPATH=src .venv/bin/python -m pqscan.cli run \
    --files fixtures/files.json --queries fixtures/queries.json --out output
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

输出：`output/plans/*.json`（行组选择说明）、`output/results/*.parquet`（查询结果）、
`output/run_record.json`（实际列读取请求）、`output/verification.json`（独立核对结论）。

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

# 旧木桥展牌签发（bridgeplaque）

在 pqscan 之上实现馆员可反复调用的展牌签发：馆员交来带桥段、梁号的署年
申请，程序用 `pqscan.runner.run` 按桥段查地方案卷，读 results 中真正命中
的 Parquet，把梁号、读数和来源行串成梁片证据，与随题自造的逐年宽窄参照带
交叉对齐，再同工单的安装先后相核，按所选工艺签出标年或暂缓，并登记台账
供凭展牌单号查回。

```
fixtures_bridge/         案卷 parquet（南/北桥段两个行组）、files/queries、
                         逐年宽窄参照带 band.json、工单 workorder.json
tools/make_bridge_fixtures.py  生成上述夹具（种子搜索 + 真实对齐代码验证）
src/bridgeplaque/
  band.py        参照带加载与对齐（漏读环 "?" 通配，输出仍成立的候选年）
  beams.py       读命中 Parquet：断口两边接回同一根树（同树两片只算一份佐证）、
                 被榫盖住的外圈从可引用读数中剔除
  workorder.py   工单：安装事件、现砍现装/库存旧材、榫序、封榫、查看权/取样许可
  solve.py       枚举 (梁->事件, 事件->年份)，输出全部仍成立的解
  plaque.py      签发编排 + 台账（只增不改，凭单号查回）
  demo.py        演示矩阵：单项证据/许可改动如何翻转同一次署年
  cli.py         issue / lookup / demo
docs/plaque_format.md    展牌单与台账格式
tests/test_bridgeplaque.py（16 用例）
```

## 运行

```bash
PYTHONPATH=src .venv/bin/python tools/make_bridge_fixtures.py   # 重新生成夹具（可选，仓库已带）
PYTHONPATH=src .venv/bin/python -m bridgeplaque.cli issue \
    --fixtures fixtures_bridge --out out --segment 南桥段 --beam 甲 --craft noninvasive
PYTHONPATH=src .venv/bin/python -m bridgeplaque.cli issue \
    --fixtures fixtures_bridge --out out --segment 南桥段 --beam 乙   # 换一根梁再办一次
PYTHONPATH=src .venv/bin/python -m bridgeplaque.cli lookup \
    --ledger out/plaque_ledger.json --plaque-id PB-0001
PYTHONPATH=src .venv/bin/python -m bridgeplaque.cli demo \
    --fixtures fixtures_bridge --out out_demo
```

## 证据与签发规则

- 同一片段断口两边的读数接回同一根树：相邻片段恰好空一环视为一处漏读，
  以 `?` 占位；空超过一环或环位重叠则案卷矛盾、拒绝签发。同树两块碎片
  只算一份佐证。
- 带皮末轮在且可引用：砍伐年 = 末轮候选年之一（精确）；缺最外圈（或外圈
  被榫盖住且无查看权/未取芯）：只能给砍伐年下界。
- 现砍现装：安装年 = 砍伐年；库存旧材：安装年 ≥ 砍伐年——旧材末轮更早
  也不能把安装提前（乙末轮 39 年与甲同次装入，桥不因此提前三年）。
- 榫孔谁切穿谁定就位先后（卯先榫后）；封榫记录（J1 初建后封存）证明该梁
  未在其后拆装，用来从候选年中排除另一次重装（甲的候选 42/54，54 即重装）。
- 不动实物工艺不得引用被榫盖住的外圈，除非有查看权；或改微芯取样工艺，
  在许可部位取一枚跨外圈的微芯定稿——该处榫痕后续观察机会永久损失，
  全桥只许可开一处（台账登记后不得再开）。
- 目标梁的 (事件, 年份) 唯一才签标年，否则暂缓并写明待补条件。

## 演示矩阵（demo）

基准（四梁、初建 42 年 -> 重装 54 年，恰十二年）签发“初建 42 年”；随后
每次只改一项：甲-2 带皮末轮改缺（单片证据）、J1 改重装后封存（封榫年代）、
收回 J1 查看权、微芯工艺下无/有取样许可、微芯再开第二处——前三项与无许可
都翻转成暂缓，放开取样许可签回 42 年，再开第二处被“只可开一处”拒绝。
