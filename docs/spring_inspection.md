# 弹簧全程曲线与装机检验

这是接在 `pqscan.runner.run` 后的新增检验入口。Parquet 扫描、计划和溯源契约不变；
只有在输入 JSON 中提供 `spring_inspection`（或同目录 `spring_inspection.json`）时，
runner 才会在 `results/<result>.parquet` 真实写出后执行装机检验。

## 1. 数据来源

检验入口不回读 `fixtures` 源文件，而是读：

```text
<out_dir>/results/<result>.parquet
```

因此使用的是本次 run 的真实产物，并保留 pqscan 的物理来源列：

```text
_source_file / _source_row_group / _source_row_in_group
```

台账列支持常见中英文别名，规范字段为：

| 规范字段 | 含义 |
| --- | --- |
| `spring_id` | 弹簧号 |
| `trial_id` | 试验序号/整轮编号 |
| `batch_id` | 可选批次 |
| `direction` | `outbound`/去程/下压/加载，或 `return`/回程/返程/卸载 |
| `force_level` | 力档；支持 `middle`、`mid`、`中档` 等 |
| `displacement` | 位移/顶升量读数 |

报告中的每条 `basis` 和 `checks[].sources` 都带回这些物理来源行；另写
`<out_dir>/spring_curves.parquet` 保存恢复后的测点曲线与来源。

## 2. 零位补录规则

补录必须有 `observation_id` 或 `evidence`，且作用域只能是：

- `"return"` / `"回程"`：统一修正该轮整段回程；
- `"outbound"` / `"去程"`：统一修正整段去程；
- `"whole_cycle"` / `"whole_trial"` / `"整组"`：统一修正整轮。

支持两种写法：

```json
{"scope": "return", "zero_shift": 1}
```

表示读数整体偏高 1 格，修正值 = 实测值 − 1；或直接给有符号 `delta`。

不允许在补录中带 `force_level`、`point`、`row` 等单测点指定字段，否则抛
`OffsetEvidenceError`。`infer_zero_shift(start_value, nominal_start=0)` 可用出发点
实测值排除不相容的整组偏移候选。

## 3. 装机单判定

模架要求支持：

- `force_sequence` 或 `steps[].force_level`：两端共同的力档序列；
- `minimum_lift` / `minimum_lifts` / `steps[].minimum_lift`：各步最低顶升量；
- `tolerance` / `allowed_height_diff` / `限差`：左右允许高度差；
- `left_spring_id` / `right_spring_id`：固定侧或指定在机簧对；
- `mode`: `"direct"` 或 `"cam"`；
- `empty_beats`、`unlock_beats`、`transition_beat`、`reset_beat`。

每个力档都分别核对去程和回程；同一力档去/回程值不同是允许的。只有全部方向、
全部力档满足最低顶升量和高差限差，才接受装机单。

### 直接配簧

`InstallationDesk` / `springdesk.plan_direct_order` 从真实余库中选两根不同实簧。
接受后，`allocations[].spring_id` 会被状态文件标记为 `occupied/held_by=<order_id>`。
新建 `InstallationDesk.reopen(state_path)` 即“重开台账”，不会恢复已占用实簧。

### 凸轮柜制

`plan_cam_order` 只从 `empty_beats` 中选动作拍，默认在去程后、回程前的过渡空拍，
把偏低一侧抬高 1 格并保持到 `next_unlock_beat`。它不会降低高侧，也不使用非空拍。

接受的台单的 `cam` 字段至少包含：

```json
{
  "actual_locked_side": "left",
  "locked_at_beat": 1,
  "next_unlock_beat": 3
}
```

即使已安排第 3 拍卸锁，在实际调用 `unlock_machine(machine_id, beat=3)` 前，
台账仍保留锁定状态。若新工单要求 `equal_start_required=true` 却没有
`reset_beat` / `reset_performed=true`，该单以
`inherited_cam_lock_not_reset_before_equal_start` 拒绝，不能把上次左抬默认为复位。

## 4. 题面两组手算结论

### 甲/乙，中档，限差 1

| 弹簧 | 去程 | 回程 |
| --- | ---: | ---: |
| 甲 | 2 | 2 |
| 乙 | 2 | 4 |

去程高差 0，回程高差 2 > 1，所以原台账下不能直接配对。

- 乙有台账依据地补录“回程零位整体偏高 1 格”后，回程变为 3，高差 1，可改判接受；
- 若补录为整组零位偏高 1 格，乙变为去程 1、回程 3，两段高差均为 1，也可改判；
- 恢复原台账后仍拒绝；
- 撤掉唯一可借实簧或撤掉指定补偿空拍后，相应直接单/凸轮单重新拒绝。

### 两台模架，限差 0

固定左簧 L1/L2 均为 `(2,2)`；C 为 `(2,2)`，D 为 `(2,3)`。

**直接柜台办法：**

1. DIE-1 固定 L1，余库 C/D 中 C 全程合格，装 `L1 + C`，C 被 DIE-1 占用；
2. 重开台账后，DIE-2 固定 L2，真实余库只有 D；去程 2=2，但回程 2 对 3，
   高差 1 > 0，拒绝 DIE-2。

**凸轮柜办法（工单指定在机簧对）：**

1. DIE-1 为 `L1 + D`：去程 2=2；回程左 2、右 3。第 1 空拍抬左 1 格并锁住，
   回程变为 3=3；下一可卸锁拍为第 3 拍；
2. DIE-2 为 `L2 + C`：去/回程均为 2=2，不需要凸轮；
3. 若下一轮要求同高起步，却没有起步前卸锁/复位空拍，上次的左锁不能视为复位，
   新单拒绝。

可复跑：

```bash
.venv/bin/python tools/make_spring_fixture.py
.venv/bin/python tools/spring_walkthrough.py
```

产物在 `output/spring-ab-*`、`output/spring-two-dies-direct`、
`output/spring-two-dies-cam` 的 `spring_inspection.json`。
