# 接印格式说明（`pqscan accept`）

入口：

```bash
PYTHONPATH=src .venv/bin/python -m pqscan.cli accept \
    --job fixtures/press/job_small_busy.json --out output/press/small_busy
```

一个任务 JSON 描述两晚的静态安排；压样、盒数量、订单全部是**全标量扁平 Parquet**
（嵌套列在加载期直接报错）。整个流程可任意重跑，枚举顺序固定，结论确定。

# 输入

## 任务 JSON

| 字段 | 含义 |
| --- | --- |
| `name` | 任务名（人读） |
| `nights` | `[第一晚起点, 第二晚起点]` 两个递增整数时间戳；压样只采信 `taken_at < nights[0]` |
| `proof_files` | 压样 Parquet 列表（相对任务 JSON 解析） |
| `box_count_file` | 盒铸字数量 Parquet |
| `order_file` | 领物票 Parquet（一个字位一行） |
| `faces` | 字面字典：字面 -> 该字面必须具备的特征（笔画/缺口）集合 |
| `open_boxes` | `{"0": [...], "1": [...]}` 每班有权拆开的字盒 |
| `table_slots` | `{"0": n, "1": n}` 每班排版台时段数 |
| `old_forms` | 旧版清单 `[{form_id, pieces:[字粒号...]}]`，拆字免开盒 |
| `sealed_forms` | 预封版 `[{form_id, pieces, machine, print_night}]`，封住的字两晚借不出 |
| `proof_predicate` | 可选，与 queries.json 同格式的附加谓词树，AND 到时间谓词上 |

## 压样 Parquet（一行一个人工标记）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `proof_id` | string | 压样编号 |
| `piece_id` | string | 字粒号 |
| `box_id` | string | 所属字盒 |
| `taken_at` | int64 | 压样时间戳 |
| `feature` | string | 笔画/缺口特征码（须出现在某字面定义中） |
| `present` | bool / **NULL** | 看清的有/无；NULL = 看不清的不完整观察（UNKNOWN） |
| `face_hint` | string / NULL | 领料员人工字面猜测；与看清事实冲突时该粒判矛盾 |

## 盒数量 Parquet

`box_id string / face string / count int64`。同一盒同一字面多行按合计处理。

## 订单 Parquet（一个字位一行，订单级字段逐行重复）

| 列 | 说明 |
| --- | --- |
| `order_id` | 领物票编号；同编号多行构成一张票的固定字位 |
| `night` | 0 第一晚 / 1 第二晚 |
| `machine` | 机器号；一台机器一晚只印一块版 |
| `slot` / `face` | 字位编号与该位要求的字面 |
| `source_box` | 限定配字盒；NULL 不限 |
| `allowed_routes` | 逗号分隔：`recompose`（现场拆版）、`seal`（提前封版） |

同一订单各行的 night/machine/source_box/allowed_routes 必须一致，字位不得重复。

# 推理语义

## 单粒观察（三值逻辑）

- 同一字粒的标记按特征聚合：看清 True / 看清 False / 看不清 三类分开计数；
- 同一特征既有 True 又有 False = 矛盾，候选字面集合为空，该粒不可用；
- `face_hint` 指向的字面必须与全部看清特征相容，否则判 hint 矛盾；
- 只有 UNKNOWN 的特征不排除任何字面（不完整观察不得当成否定证据）。

## 整盒数量分配（CSP）

- 每盒先核对：字粒数必须等于铸字数量合计，否则盒台账 `feasible=false`，
  该盒身份不可作为排除依据，`identity.json` 与 `decision.json` 同时告警；
- 回溯枚举满足“单粒候选 ∩ 每字面配额”的全部可行分配（上限 1000 份，
  触顶在 reason 中声明）；
- 某粒在所有可行分配中字面相同 -> `forced_face`，可有凭据交付；
- 否则只给 `candidate_faces`，调度器不得把它当作任何字面承诺。

## 排班约束

1. 同一夜班一个字粒最多出现在一张票上；
2. `seal`：字粒两晚占用、不耗台面、不开盒；只能封盒字（旧版上的字不可封）；
3. `recompose`：耗当班一个台面时段；旧版拆字免开盒，盒取字要求盒在
   `open_boxes[night]` 内；字粒次晚释放；
4. 机器一晚一块版（预封版印刷同样占机器）；
5. 只有 `forced_face` 命中要求字面的字粒可配给字位。

# 输出凭据

## `proofs/proofs_before_nights.plan.json`

复用 pqscan 的行组剪枝计划（trace、can_be_true/can_be_false、保守保留理由），
谓词固定为 `{"lt": ["taken_at", nights[0]]}`（带附加谓词时外包一层 AND）。

## `proofs/proofs_before_nights.parquet`

采信压样行，投影列后附 `_source_file/_source_row_group/_source_row_in_group`
三列溯源；迟到/被附加谓词排除的行不在其中。

## `proofs/verification.json`

无剪枝全扫描 + 纯 Python 3VL 的独立核对结论；不通过则主流程报错退出，
禁止产出决策。

## `identity.json`

- `pieces[*]`：每粒字的特征级结论（verdict/矛盾行号/UNKNOWN 行号）、
  候选字面、可交付字面、全部标记的来源行；
- `boxes[*]`：铸字配额、是否可兑现、一份可行分配 `witness_assignment`、
  唯一逼出的 `forced_faces` 与人读 reason。

## `decision.json`

- `summary`：接/拒笔数与不可兑现盒；
- `evidence`：采信/排除行数、核对结论与产物路径；
- `sealed_forms`：预封版及其两晚占用声明；
- `orders[*]`：`accepted`、`route`、逐字位的字粒号/来源盒/旧版、
  拒接 `reason_code` 与人读理由、订单来源行；
- `night_ledger[*]`：每班台面时段用量、机器占用、字粒占用、可开盒、
  已拆旧版——领班据此核对“拿哪盒、何时动哪块版”。

### 拒接原因码

| code | 含义 |
| --- | --- |
| `no_eligible_piece` | 没有字粒能有凭据地满足字位（身份不足或观察矛盾） |
| `piece_busy` | 能识别的字粒该晚（封版则两晚）均已被占 |
| `machine_busy` | 该晚机器已被别的版占用 |
| `box_not_open_that_night` | 候选盒当班无权拆开，旧版上又无现成字 |
| `no_composing_table_slot` | 排版台时段排满，无法现场配版 |
| `route_not_allowed` | 订单限定的兑现路径均不可用 |
| `box_ledger_infeasible` | 整盒铸字数量记录无法兑现 |
