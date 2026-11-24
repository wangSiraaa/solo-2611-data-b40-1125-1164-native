# 展牌单与台账格式（plaque_ledger.json）

台账是只增不改的 JSON 文件，馆员每办一份署年申请就追加一条记录；
凭 `plaque_id`（展牌单号，PB-0001 起递增）可查回**当时**的具体梁片、
采用的安装证据与可公开结论——日后再办（哪怕证据或许可已变）不改写旧记录。

## 顶层结构

```json
{ "records": [ <展牌单>, ... ] }
```

## 展牌单

| 字段 | 含义 |
| --- | --- |
| `plaque_id` | 展牌单号，台账内递增唯一 |
| `issued_at` | 签发时间（ISO8601，秒级） |
| `application` | 署年申请：`segment` 桥段、`beam_id` 梁号、`craft` 工艺（`noninvasive` 不动实物 / `microcore` 微芯取样） |
| `status` | `issued` 签出标年 / `deferred` 暂缓 |
| `dating` | 签发时非空：`{"event": "初建", "year": 42}` |
| `possibilities` | 目标梁在全部仍成立的解中的 (事件, 年份) 投影；唯一才签发 |
| `beam_evidence` | 每根命中梁的证据链（见下） |
| `installation_evidence` | 人读安装证据句（用材性质、拼接、候选年/下界） |
| `excluded_candidates` | 从候选中排除的另一次安装及依据（如封榫记录） |
| `public_conclusion` | 当时可公开的结论全文 |
| `problems` | 暂缓原因 / 待补条件（查看权、取样许可、封榫记录等） |
| `results_parquet` | 本次 pqscan 真正命中的结果 Parquet 路径 |
| `core` | 微芯记录：`joint` 取芯节点、`cost` 代价说明；未取芯为 `null` |

## beam_evidence 条目

```json
{
  "beam_id": "甲",
  "fragments": ["甲-1", "甲-2"],
  "same_tree_single_evidence": true,
  "joined_pattern": "宽窄窄窄窄?宽窄窄",
  "citable_pattern": "宽窄窄窄窄?宽窄窄",
  "covered_blocked_positions": [],
  "last_ring_candidates": [42, 54],
  "bark_edge": true,
  "bark_citable": true,
  "felling_constraint": {"kind": "exact", "years": [42, 54]},
  "candidate_events": ["初建"],
  "source_rows": [{"file": ".../beam_readings.parquet", "row_group": 0, "row_in_group": 0}, ...]
}
```

- 同一梁号的片段属同一根树，断口两边接回后**只计一份佐证**
  （`same_tree_single_evidence`）；漏读一环以 `?` 占位。
- `covered_blocked_positions`：被榫盖住且当时无权引用的环位
  （不动实物工艺下无查看权，或未取芯）；被盖外圈不进 `citable_pattern`。
- `felling_constraint`：`exact`（带皮末轮可引用，砍伐年 ∈ 候选集合）或
  `lower_bound`（缺最外圈/外圈不可引用，只给下界）。
- `source_rows`：pqscan 溯源列，指回案卷 Parquet 的物理行。

## 签发判定

1. 目标梁的 (事件, 年份) 投影唯一 -> `issued`；
2. 投影不唯一 -> `deferred`，`possibilities` 列出并存候选；
3. 无仍成立的解（年轮证据与工单约束矛盾）-> `deferred` 并注明矛盾。

## 微芯的“只可开一处”

台账中任一记录含非空 `core` 后，后续微芯申请一律暂缓并注明已用单号；
取样许可部位超过一处，或许可部位不在申请梁上，同样不能定稿。
