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
python3 -m unittest discover -s tests -v     # 67 个测试
python3 -m demo.run_demo                     # 合成过磅案例
python3 -m demo.run_street_demo              # 腾街单桌面回放
python3 -m demo.run_street_demo --json       # 另写 demo/out/street_report.json
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

## 腾街单（街道清雪交接）

`weighbridge/street.py` 在过磅仓之上增加可复核的 `StreetOffice`。它通过
`Warehouse.trip()` / `evaluation()` 只引用 `CONFIRMED` 车次的
`[net_lo, net_hi]`，再把确认净重与分段装车、转堆、落堆、清扫前后堆量界连接起来。

```python
from weighbridge import StreetOffice

office = StreetOffice(w)          # 也可用 w.street_office()
office.add_segments("甲", "乙", "丙")
office.set_initial_snow("甲", 5)
office.set_initial_snow("乙", 5)
office.reference_confirmed_trip("TRIP0001")
office.add_pickup("P-甲", "TRIP0001", "甲", lo=0, hi=6)
office.record_loading_witness("W-乙", "TRIP0001", "乙", 1)

recon = office.reconstruct()
assert recon.remaining_range == {"甲": [0, 0], "乙": [4, 4], "丙": [0, 0]}
```

语义要点：

* **物料守恒**：转堆签条同时做源段减、目标段加；没有装车动作时，搬移后的雪仍
  计入街上剩余量，不会变成外运。
* **一车净重只分一次**：同一车次全部分段装车量之和必须落在确认净重区间内；两段
  各认整车六公斤会得到 `feasible=False`。
* **按最坏世界受理**：区间证据会枚举小整数可行世界；只有每个世界交场段都严格
  低于各自余雪线，腾街单才是 `ACCEPTED`。零雪要求可写 `snow_line=1`。
* **连续摊位**：申请段必须在 `add_segments()` 声明的道路顺序上连续；查询返回可交
  路段、摊位、引用车次/净重、许出堆位和失败原因。
* **禁倒改道**：`sign_no_dump_order()` 必须同时在替代段为邻队签下可用堆位凭证；
  堆位已先租给别人时留下 `REJECTED` 禁倒单并抛 `SlotUnavailable`。有效禁倒单会
  拒绝之后向保护段落堆/转堆。
* **岔口观测**：`record_observation(..., timing=BEFORE_FORK)` 才能让末车按实际落点
  选可达装车点；`AFTER_FORK` 的旧预计表不能充当观测。单行路线一旦选错边不能返回，
  整单被拒时会给出在该固定动作下仍可保守交出的连续缩段。

`weighbridge/scenarios.py` 与 `demo/run_street_demo.py` 固化了题面两个三段街、
不超过两趟车的例子：

1. 甲乙各 5kg、一车净重 6kg：无见证时甲可能剩 `0..4kg`；乙见证 1kg 后才唯一得到
   甲 0、乙 4。随后分别重放“替换乙装车见证为 5kg”“撤掉甲段禁倒权”“撤掉甲侧可达
   装车点”三个单点反事实。
2. 甲乙同时无雪：邻队 1kg 暂存可能落甲或乙，现场在岔口前看见后派末车跟进，两种
   实况都能交甲乙；丙位已租时禁倒单不能签但观测方案仍可执行；观测迟到且旧预计表
   错指乙侧时，甲留下 1kg，原甲乙单不能交，固定乙侧动作可保守改交乙丙两段。

## 容量

目录 100 实体（`define_mass` 时强制）；仓库 1000 张票、200 次运输。同一车同一秒
可有多张不同 `ticket_id` 的票。

## 文件

```
weighbridge/catalog.py        独立车辆组合目录
weighbridge/warehouse.py      过磅事实仓 + 精确整数求解
weighbridge/street.py         腾街单、物料重建、堆位/禁倒与岔口回放
weighbridge/scenarios.py      三段街/两趟车的题面桌面案例
weighbridge/__init__.py       公共 API
tests/test_weighbridge.py     过磅仓测试（含小整数区间暴力核对极值）
tests/test_street.py          腾街单场景与反事实测试
demo/run_demo.py              可复跑合成过磅案例
demo/run_street_demo.py       可复跑腾街单桌面回放
demo/out/                     演示输出（--json 时生成）
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
