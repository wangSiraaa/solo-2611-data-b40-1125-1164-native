# 展演排轮输出格式（output/wheels/）

`python -m pqscan.cli wheels --fixtures fixtures/wheels --out output/wheels`
读取扁平 Parquet 观测表与行程/签条/库房/节目 JSON，产出四份交接文件。

## 领域口径

- 计程鼓跟车身走，只有 0..9 十格，满十翻零。相邻观测的实际走行 d 满足
  `after = (before + d) mod 10`，即 `d ≡ (after-before) (mod 10)`；
  行程表给区间 `[lo, hi]`，可行解是区间内余数相符的全部整数，
  跨零次数固定为 `d // 10`。可行解为空即履历矛盾，直接报错。
- 车轮整对换车、计程鼓跟车身；段内走行记在"段起点装在车上的那套轴对"上。
- 模糊换轮签条的每个候选组合展开成一条互斥分支；同一轴对同一时间窗挂两台车的
  分支标记 `feasible=false` 并写排斥原因，但分支与段轨迹仍保留。
- 翻修期限是馆内自订的总额度；`历史最大已耗 + 本场使用 <= limit` 才放行，
  等号合法（恰好到限可收车），超一格即超期。

## wheel_branches.json

```json
{
  "rule": "……",
  "branches": [
    {
      "branch": "T->W2",
      "tag_choice": {"T": "W2"},
      "feasible": true,
      "exclusion_reasons": [],
      "segments": [
        {"seg_id": "S1", "car": "C-A", "start_ts": 8, "end_ts": 12,
         "drum_before": 8, "drum_after": 1, "allowed_range": [2, 4],
         "mounted_axle": "W1", "feasible_ticks": [3],
         "rollover_options": [0], "ambiguous_rollover": false,
         "consumed_min": 3, "consumed_max": 3}
      ],
      "axle_segments": {"W1": ["S0", "S2", "S1"]}
    }
  ]
}
```

`feasible_ticks` 有多个值时（如 7->7、区间 [0,10] 给出 0/10），
`ambiguous_rollover=true`，表示同一段跨零次数尚不能唯一确定，需保留给值班员。

## wheel_quota.json

每个轴对一条：`overhaul_limit`、`consumed_min/max`（跨可行分支与段可行解的
已耗区间）、`headroom_min/max`（剩余额度区间）、`overdue`、参与汇总的分支名。
无履历的纯库房备轮 `consumed=0`、`branches=["spare_no_history"]`。
值班员可借 `wheel_branches.json` 的 `axle_segments` 从轴对指回具体车身观测段。

## handover.json

两条路线各一份节目单，assignment 字段：

| 字段 | 含义 |
| --- | --- |
| `stage` / `axle` | 到哪个展台、用哪套轮 |
| `swapped` / `lift_used` | 是否换轮；共用路线下是否占用举升台 |
| `ticks` / `headroom_after` | 本段格数与走完后的保守剩余 |
| `status` | `accepted` 或 `rejected` |
| `reject_reasons` | 被拒时写明：无举升台、现挂轮锁死、额度不足或无轮可借 |

- `sealed`：开馆前每个节目封存一套独立轴对，期间不借出，故三个节目必须三套不同轮；
- `shared`：可在节目间复读鼓面后换轮，但只有 `has_lift=true` 的展台能换；
  `departs_before_close=true` 的列车出发后，其轴对随车锁死、叫不回来。

## replay.json

基线与四类单点突变（每类只改一处）各自重跑履历/额度/两条路线：

1. `mutate_reading`：单改一次鼓面读数，使某段余数类与行程表区间不相交 -> 履历矛盾；
2. `mutate_tag`：单改一次模糊签条候选，删掉唯一可行分支 -> 无可用履历；
3. `mutate_spare`：单改一次库房备轮（移除一套）-> 封存路线凑不齐独立轴对；
4. `mutate_lift`：单改一次举升台安排 -> 共用路线无法换轮（封存路线不受影响）。

`history_error` 非空表示履历阶段即失败；否则看 `plans[].feasible`，
翻成 `false` 的节目单会在最后一个 assignment 给出 `reject_reasons`。
