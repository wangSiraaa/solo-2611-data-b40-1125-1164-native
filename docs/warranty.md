# 保修接单：用 pqscan 扫描结果按炉、按锅号调取证据并出操作单

本目录在 pqscan 之上加了一层「搪瓷锅保修接单」：把**刚扫出的 Parquet 结果**（pqscan
的 `results/*.parquet`，带 `_source_file/_source_row_group/_source_row_in_group` 溯源）
当作证据，按炉号/锅号重建每只锅可能经历的 **底釉批号 × 温区**，再据此为一张包含若干
实物锅的整单出 **接单 / 条件接单 / 拒签** 操作单。

## 1. 证据从哪来

一次扫描的每个命中行 = 一只锅在一个挂位上的记录：

| 列 | 含义 |
| --- | --- |
| `furnace_id` | 炉号 |
| `hanger_id` | 挂架号 |
| `pot_id` | 锅号（实物锅） |
| `row` | 挂位排号，0/1 |
| `slot` | 挂位号 |
| `glaze_batch` | 底釉批号（可缺失） |
| `hot_row` | 该挂架「高温侧」排号读数（由炉温见证片判定，可缺失） |
| `witness_plate_id` | 读出该读数的见证片编号 |

加 pqscan 追加的三列溯源。`pqscan.warranty.evidence_io.scan_furnace` 会对某个炉号
跑一次 pqscan（footer 计划 + 两阶段扫描），正是「刚扫出的结果」。

## 2. 重建规则（关键约束）

- **一架一个朝向。** 挂架翻身入炉时两排挂位一道对调，所以「哪一排是高温侧」是**整架
  一个变量** `hot_row ∈ {0,1}`；同一挂架上的每只锅必须共用同一朝向，**不能各选一次**。
- **读数缺失即保留。** 某架 `hot_row` 为 NULL 时保留两种朝向，即其上每只锅都保留两种
  可能的温区；读数存在则把该架朝向钉死。
- **读数矛盾即不可证。** 同一架同时出现 0 和 1 的读数，视为证据自相矛盾：该架无一致朝向，
  其上锅一律「无法证明」，绝不猜一个朝向。
- **底釉缺失即不可枚举。** `glaze_batch` 为 NULL 时该锅的可能组合无法枚举，永不判可放行。
- **同锅多行取并集。** 一只锅若有多条命中行，把各行给出的可能组合取并集。
- 锅、架、来源行都可查回：回单里每只锅带 `source_records`（含文件/行组/组内行），
  并由 `provenance_audit` 实际打开原始 Parquet 核对该行确实属于这只锅。

## 3. 三种可选的整单办法（每张单选定一种）

| 办法 | 做什么 | 消耗 | 何时拒签 |
| --- | --- | --- | --- |
| `record` | 原锅留置，凭生产记录直接担保 | 无 | 只要有 1 只锅在某个一致朝向下可能落入疑似组合 |
| `replace` | 原记录安全的保留；其余用库内合格锅替换 | 库存，交出即消耗 | 库存不足 / 底釉缺失无从匹配 |
| `cut` | 原记录安全的保留；其余用**同底釉同温区**见证锅拆检澄清 | 见证锅，拆后报废 | 任一疑似组没有同组见证锅 |
| `auto`（默认） | 逐只锅按 record → replace → cut 取第一个可行 |

- **疑似致裂组合**由 `ProblemConfig` 给出（某些底釉 × 温区同时出现才有暗裂）。
  一只锅「安全」= 在**一切一致朝向**下都避开疑似组合；只可能落入就**不能**担保。
- **同组的充分性**：见证锅的 (底釉, 温区) 必须与待澄清锅对应的疑似可能组**完全相等**。
  同底釉不同温区、同温区不同底釉都不算同组。

### 题目工作例（甲乙）

一架只在左侧进高温区，甲乙同用有问题底釉；读数确定甲在右时：

- 甲在右（非高温侧）→ `safe_record`，可保；
- 乙在左（高温侧）→ 有风险，不能凭记录放行；
- 关键否定：**甲的无裂结果不会被拿来放行乙**（每只锅独立判定，见证锅只清自己那一组）。
  单测 `test_known_orientation_releases_only_outside_pot` 即此例。
- 读数缺失时，甲、乙**都不能**凭记录放行（`test_unknown_orientation_cannot_release_either_pot`）。

## 4. 拆检分支：不预挑

`decide()` 的签名里**不接受任何拆检结果**（单测用结构断言守着）。当只剩拆检办法可行时，
操作单给出 `accept_pending_cut`，并同时写明阴性/阳性两条分支。**只有拆检结果真正到手**，
才调用 `resolve_after_cut()`：

- **阴性**：只清除该组，其上待放行的锅可发货；
- **阳性**：只隔离该组，该组内锅不得发货——若还有**本单尚未承诺**的可替换库存则改用替换，
  否则拒签。阳性不外推到别的组，阴性不越界。
- 拆检成阴性/阳性都会使该见证锅**报废**，台账禁止后续单再用。

## 5. 回单与跨单台账

`StockLedger` 记录：库存锅「已交给哪张单」、见证锅「被哪张单预留拆检」。`reuse_verdict()`
回答售后两个问题：**已交出去的替换锅、已拆的见证锅，还能否再用**。一经交出/预留即 `False`。

回单 `receipt` 含：

- `source_records_by_pot`：每只锅用到的来源记录（文件/行组/组内行）；
- `provenance_audit`：逐条核对来源行确实存在且是该锅；
- `hanger_orientation_evidence`：用到的挂架朝向可能性（说明「整架一个朝向」的依据）；
- `inventory_consumed` / `witnesses_scrapped` 及其 `reusable_after_this_order`；
- `reuse_checks`：本次交出/报废项的实时复用结论。

## 6. 命令行

```bash
PYTHONPATH=src python -m pqscan.warranty.cli demo --out output/warranty
# 一键：建夹具 → 两炉两单 → 拆检分支（阴/阳）→ 三种杠杆 → 落盘 JSON

PYTHONPATH=src python -m pqscan.warranty.cli evidence \
    --config fixtures/warranty/warranty_config.json \
    --furnace furnace-alpha --order ORD-A --pot A10 --pot B20
# 按炉号/锅号调取底釉与温区证据及其来源行

PYTHONPATH=src python -m pqscan.warranty.cli decide \
    --config fixtures/warranty/warranty_config.json \
    --order ORD-A --furnace furnace-alpha \
    --pot A10 --pot A20 --pot B10 --pot B20 --method record
# 为一张订单出操作单（method ∈ auto/record/replace/cut）

PYTHONPATH=src python -m pqscan.warranty.cli run \
    --config fixtures/warranty/warranty_config.json --out output/warranty/orders_receipts.json
# 按配置办理多张订单，写全部回单 + 台账 + 库存/见证锅复用总表

PYTHONPATH=src python -m pqscan.warranty.cli levers \
    --config fixtures/warranty/warranty_config.json \
    --order ORD-A --furnace furnace-alpha \
    --pot A10 --pot A20 --pot B10 --pot B20
# 演示三种单点改动
```

`fixtures/warranty/` 内路径为相对文件名，提交到仓库后可在任意目录复现（无需固定绝对路径）。

## 7. 夹具场景（两架、四只锅）

炉 `furnace-alpha`：

| 挂架 | 读数 | 锅 | 排 | 底釉 | 可能组合 |
| --- | --- | --- | --- | --- | --- |
| H1 | `hot_row=0`（确定） | A10 | 0 | GX | GX/high |
| H1 | — | A20 | 1 | GX | GX/low |
| H2 | NULL（缺失） | B10 | 0 | GY | GY/high 或 GY/low |
| H2 | — | B20 | 1 | GY | GY/high 或 GY/low |

炉 `furnace-beta`（另一份「炉号及锅号不同的小单」）：H9 读数缺失，C10/C20 底釉 GZ。

疑似组合：`GX/high`、`GY/high`、`GZ/high`。库存：R1、R2。见证锅：W-GY-HIGH、W-GZ-HIGH。

`demo` 的基线：ORD-A 为 `accept_pending_cut`（A10 替换 R1、A20 原锅、B10 替换 R2、
B20 待 W-GY-HIGH 拆检），ORD-B 为 `accept_pending_cut`（C10/C20 待 W-GZ-HIGH）。

## 8. 三种杠杆（各改一笔，改变同一张订单）

对 ORD-A 分别只改一处输入并重跑：

| 杠杆 | 改动 | 结果变化 |
| --- | --- | --- |
| 补一条架向读数 | H2 补 `hot_row=1` | B10 落到 `GY/low` 变安全、B20 替换 R2 → 整单 `accept`，不再需要拆检 |
| 改一件替换库存 | 追加合格库存 R3 | B20 直接替换 R3 → 整单 `accept`，见证锅保留 |
| 换一只见证锅 | 撤掉 W-GY-HIGH、换入 W-GX-HIGH | B20 的 `GY/high` 无同组见证锅 → 整单 `refuse` |

三者都不读任何拆检结果，符合「不能依据拆开后才知道的结果预先挑分支」。

## 9. 测试

`tests/test_warranty.py`（21 个用例）覆盖：整架朝向共享与缺读数保留两种可能、读数矛盾
不可证、底釉缺失不可放行、甲乙工作例（不得用甲放行乙）、三种办法的接单/拒签、同组必须
完全相等、拆检阴性清组 / 阳性隔离且不挪用已承诺库存、`decide()` 签名不含拆检结果、
跨单台账阻止复用、第二张不同炉号锅号小单的办理与溯源、三种杠杆各自改变同一订单。
