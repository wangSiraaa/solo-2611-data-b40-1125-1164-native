# 挂车换接下的双向过磅与净装载事实仓

一台牵引车第一次带空挂车进厂、第二次却换了另一挂车出厂时，若按车牌配对两次
毛重，会把两套不同空车质量混在一起，制造不存在的净货重。本仓库把问题拆成
两个互相独立的模块：

* **车辆组合目录** (`weighbridge/catalog.py`)：只管车辆稳定身份、空车质量整数
  区间 `[lo, hi]`（kg）以及按严格递增序号发生的组合动作。它不知道磅单。
* **过磅事实仓** (`weighbridge/warehouse.py`)：消费目录在票面序号处的 *公开组合
  产品*，把毛重换算成货重区间、组织运输、计算净装卸量的精确区间、处理重评。

全部输入为本地合成整数记录，不连接地磅或车牌识别设备。Python 3.11，仅用标准
库（SQLite + `fractions.Fraction`）。

## 快速开始

```bash
python3 -m unittest discover -s tests -v     # 70 个测试（过磅 46 + 返券 24）
python3 -m demo.run_demo                     # 合成案例
python3 -m demo.run_demo --json              # 另写 demo/out/*.json
python3 -m rebate.cli scenario --db demo/out/center.db   # 返券题面全判定
```

```python
from weighbridge import Catalog, Warehouse, LOAD, IN, OUT

c = Catalog()
c.define_mass("tractor", "T1", 7000, 7200)   # 整数 kg，lo <= hi
c.define_mass("trailer", "R1", 1500, 1600)
c.define_mass("trailer", "R2", 2000, 2200)

c.apply_attach("T1", "R1")        # seq 1
c.apply_detach("T1", "R1")        # seq 2
c.apply_attach("T1", "R2")        # seq 3
print(c.product_at(1, "T1").stable_identity())   # ('T1', 'R1')，历史不被换挂覆盖

w = Warehouse(c)
w.add_ticket(dict(ticket_id="IN",  tractor="T1", catalog_seq=1, ts=100,
                  direction=IN,  gross=9000))
w.add_ticket(dict(ticket_id="OUT", tractor="T1", catalog_seq=3, ts=200,
                  direction=OUT, gross=12000))

ev = w.preview(LOAD, "IN", "OUT")  # DRAFT：只算，不占票
print(ev.net_lo, ev.net_hi)        # 2300 2600（整数空重区间上的精确极值）
w.confirm(ev.trip_id)              # CONFIRMED：此刻才占票
print(w.reconcile())               # [] 表示核对通过
```

## 目录语义

* **稳定身份**：实体 ID 为 `tractor:<id>` / `trailer:<id>`，永不复用。
* **动作序号**：`attach` / `detach` / `set_mass` 的序号严格递增；失败不消耗
  序号。**批动作** `apply_batch` 在单事务内执行，任一步失败整批回滚（序号、
  绑定、产品、批次记录全部不存在）。
* **组合约束**：一个挂车同时至多一台牵引车；一台牵引车同时至多一个挂车。
  由两枚 `WHERE detach_seq IS NULL` 的部分唯一索引强制。
* **组合产品**：每次 `attach`/`detach` 产生一个 `EMPTY` 产品；`set_mass` 产生
  `RELABEL` 产品。产品带 `relation_src`（`initial`/`attach`/`detach`/
  `mass_correction`）、`attach_seq`、`predecessor`。
* **历史不被覆盖**：产品的质量是落盘时的快照。查询任意序号：

  ```python
  c.product_at(seq, tractor)   # 该序号处已生效的 live 产品
  c.history("trailer", "R1")   # 该实体参与过的全部产品（含更正版）
  c.snapshot_at(seq, tractor)  # 可跨系统交换的 JSON 快照
  c.corrected_product(p)       # 同一挂接 + attach_seq 的最新更正版
  ```

  换挂之后，旧序号处的旧产品原样保留；`corrected_product` 只沿同一
  `(identity, attach_seq)` 的 `predecessor` 链走，绝不从 "当前挂接" 猜历史。

## 仓库语义

**单票货重**：`tare` 为组合产品各实体空车质量之和，
`goods = gross - tare`，可行区间
`[max(0, gross - tare_hi), gross - tare_lo]`；`gross < tare_lo`（上界为负）时该票
不合物理约束。`explain_ticket(tid)` 给出毛重、`tare`、`goods`、每个实体区间与
宽度——即 "这张票为什么是这个货重、哪些实体贡献了不确定性"。

**运输**：`preview(kind, ticket_in, ticket_out)` 显式选定一 IN 一 OUT 票，绝不按
时间就近猜配。IN 时间不得晚于 OUT；两票必须同车；一票不能同时占两个角色。
结果状态：

| 状态 | 含义 |
| --- | --- |
| `DRAFT` | 预览，未占票；失败时为不可确认的草稿 |
| `CONFIRMED` | 已确认；两票写进 `confirmed_ticket`，此后不可被别的运输占用 |
| `REVIEW` | 对已确认运输的重开/重评 |
| `SUPERSEDED` | 未确认的旧草稿/复核被同族新结果取代 |

**净装卸量**：

* 组合完全相同（同一牵引车 + 同一挂车）：空车质量是同一未知量，在毛重差中
  **精确抵消**，`net == |gross_out - gross_in|`，与区间宽度无关。
* 换挂：共享实体（如牵引车）系数为 0，精确抵消；不同实体以目录区间参与。
  在 "两张票货重非负 + 运输方向" 的联合可行域上，对净装卸量做**精确整数分支
  定界**（实数 LP 松弛用有理数顶点枚举），输出最小/最大净装卸量与货流区间。
  可行域为空则拒绝，区分 `DIRECTION_IMPOSSIBLE` 与 `TICKET_INFEASIBLE`。
* 绝不使用区间中点或实数顶点作为结算值。

`contributions` 逐个实体给出 `role`（`cancelled`/`fixed`/`uncertain`）、线性系数与
区间宽度。

**重评与重开**：

```python
c.apply_set_mass("trailer", "R1", 1500, 1600)  # 目录迟到更正
re = w.reopen(trip_id)        # 新增 REVIEW 运输，带 predecessor_trip
w.evaluation_history(re.trip_id)  # 旧（CONFIRMED）与新（REVIEW）都在，链式可追溯
```

* 已确认运输及其评估是历史事实，不会被覆盖或标为 `superseded`。
* 被确认运输占用的票不会被重新记成另一运输（重评沿用同一票与角色）。
* 重评默认对原评估中的 *历史产品* 取 `corrected_product`，而不是套用当前挂接。

**外部组合快照**：`Warehouse(None)` 可完全不依赖目录运行；用
`register_snapshot(...)` 导入与目录相同 JSON 格式的快照，按票面序号取产品。
若某张票的序号没有对应快照，直接报错——绝不回退到当前挂接去猜。

**核对**：`w.reconcile()`（`w.verify()` 同义，含目录自检）返回不一致列表，覆盖：
确认运输的两票方向/同车/时间序与可行性、确认占票唯一、每条评估在存下的快照
上可重算、同身份精确抵消、重评前序存在。

## 容量

目录 100 实体（`define_mass` 时强制）；仓库 1000 张票、200 次运输。同一车同一秒
可有多张不同 `ticket_id` 的票。

## 社区废纸返券柜台（`rebate/`）

在过磅事实仓之上叠了一层 *返券柜台*：社区按住户一整期的废纸净重返券，但一车
只过一次磅，住户重量要把箱号、封箱转交、允许袋重与自愿出示的局部称量接起来
推断。同样坚持只追加、不覆盖、按回执快照可重算。

* **收集网络 + 约束求解** (`rebate/network.py`)：纯数据、不落盘。袋有毛重与
  允许袋重（皮重区间），可选离散纸重档位；箱在车次上首次归集；`add_transfer`
  做封箱转车（两侧纸重相等、源箱在原车净重只计一次）；车次净重是可回查版本。
  在袋纸重整数域上回溯枚举 *全部可行世界*——无法区分的两种情况都留下，某户在
  所有世界里取同值才算 *证实*（`proven`），否则只能挂起。
* **柜台事实仓** (`rebate/counter.py`)：SQLite 落盘核算期（减量线、领券期限）、
  预算（统一池或拆到各期/各站）、领券权状态机、带证据快照的回执与 `reconcile`。
* **过磅适配器** (`rebate/warehouse_adapter.py`)：只导入 Warehouse 的
  `CONFIRMED` 车次；`reopen` 产生的 REVIEW 评估链展开成同一车次的多净重版本，
  草稿不导入。

### 两种经营办法

| | 统一封账 `UNIFIED` | 分站断网 `STATION` |
| --- | --- | --- |
| 谁能看局部称量 | 中心可见凑成同车总数的全部凭条 | 分站只见本站/中心/住户本人凭条 |
| 预算 | 中心池统一调度，签券即锁 | 必须显式拆到各站，分站看不到别站池 |
| 证据不足 | 全局反推可证实即可签 | 留单，等住户本人凭条到达再办 |
| 封账后 | 只能开 *差额争议*，不在原券旁另兑 | 同左 |

**领券权（跨站令牌）**：`HELD -> HANDED_OVER -> RECEIVED -> SPENT`。跨站时原站
先 `hand_over`，收件未到两站都不能花；`receive_right` 后接收站可办，原站补送
同一 `msg_id` 幂等、不会恢复。签券成功才核销为 `SPENT` 并等额锁预算。

**回执与隐私**：每次申请都有回执号，封存经办方 *当时有权看见* 的证据、全部
可行世界、证实重量、签发/挂起原因、扣到哪期哪户哪站。按 `CENTER`/`STATION`/
`HOUSEHOLD` 角色查询时自动剔除别户私重与别站流水；公开车次总量任何角色可见。

```python
from rebate import Counter, PartialWeigh, NODE_BAG, ORIGIN_CENTER, MODE_STATION

c = Counter(mode="UNIFIED")
c.open_period("P1", reduction_line=4, claim_deadline=100, central_budget=2)
c.add_box("BX", "TRIP1")
c.add_bag("B甲", "甲", "BX", gross=6, allowed_paper=(4, 6))
c.add_bag("B乙", "乙", "BX", gross=6, allowed_paper=(4, 6))
c.add_trip_version("TRIP1", 10, 10)          # 一车一次磅：车净重精确 10
c.apply("P1", "甲")                           # 无个人称量 -> SUSPENDED/WEIGHT_UNPROVEN
c.add_partial(PartialWeigh("W甲", NODE_BAG, "B甲", 4, 4,
                           ORIGIN_CENTER, household_id="甲"))
d = c.apply("P1", "甲")                       # 甲出示 4 -> ISSUED，预算等额锁定
c.receipt(d.receipt_id)                      # 按回执号回查原因与已兑额度
c.reconcile()                                 # [] 表示在回执快照上可重算
```

### 本机入口（可办新申请，可模拟两站）

```bash
python3 -m rebate.cli init --db demo/out/station.db --mode STATION --budget 3
python3 -m rebate.cli scenario --db demo/out/center.db   # 演完题面全部判定
python3 -m rebate.cli apply --db demo/out/station.db \
        --period P1 --household 甲 --station S1 \
        --weigh-node B甲 --weigh-value 4 --weigh-origin STATION
python3 -m rebate.cli receipt --db demo/out/station.db --receipt 2 --role HOUSEHOLD
python3 -m rebate.cli reconcile --db demo/out/station.db
```

`scenario` 依次复演：仅总量两户均挂起 → 甲出示 4 可签 / 预算归零不签 →
仅乙向中心出示 6 时统一封账反推签给甲、分站则留单 → 跨站交出在途两站不能花、
接收后原站补送不恢复 → 最后额度签出后第二户挂起 → 封账后称量纠正只开差额争议
（含净重与凭条版本链，从原车次复演）。容量：最多 3 户、2 站。

## 文件

```
weighbridge/catalog.py      独立车辆组合目录
weighbridge/warehouse.py    过磅事实仓 + 精确整数求解
weighbridge/__init__.py     公共 API
rebate/network.py           废纸收集网络 + 住户重量整数约束求解
rebate/counter.py           返券柜台事实仓（核算期/预算/领券权/回执）
rebate/warehouse_adapter.py 导入确认车次与净重版本链
rebate/cli.py               本机办理入口与两站模拟
tests/test_weighbridge.py   46 个测试（含小整数区间暴力核对极值）
tests/test_rebate.py        24 个返券柜台测试
demo/run_demo.py            可复跑合成案例
demo/out/                   演示输出（--json 时生成）
```

## 覆盖的案例

* 同车同挂精确抵消（含极宽空车区间）
* 仅换挂：共享牵引车抵消、两挂车参与整数区间极值（与暴力枚举逐个核对）
* 目录迟到更正：已确认运输不变，新增带前序身份的重评
* 方向不可能、单票物理不可行、两票区间不相交
* 同票竞争：已确认运输占票后另一运输确认失败，且失败不改变状态/不占票
* 重开：多轮更正形成可追溯评估链
* 外部快照独立运行、快照冲突拒绝、缺失历史产品拒绝
* 批动作原子回滚、序号严格递增、单挂车/单牵引车约束、容量上限
