# 受限 YAML 配置目录服务与配置消费分析仓

Python 3.11 标准库实现（无第三方依赖），严格按题面合同控制类型、锚点/别名与
merge 语义，并把“有效配置 + 每键来源证据 + 关系表血缘”持久化到真实 SQLite。

## 目录结构

```
confcat/
  errors.py         # 合同级错误类型（解析期/消费期分离，整次任务可回滚）
  model.py          # 原始共享图 RawNode 与物化有效树 MatNode、来源证据模型
  yaml_parser.py    # 受限 YAML 解析器（词法 + 递归下降，游标不变量统一）
  materialize.py    # 别名解引用/循环检测 + merge 合同物化 + 来源证据生成
  codec.py          # 物化树 JSON 编解码（保留 int/str 双键区分）与取值辅助
  catalog.py        # 配置产品目录：版本化发布、路径查询、哈希校验（SQLite）
  csv_pipeline.py   # CSV 读取与按配置归一化（列映射/缺失值/分区/列血缘）
  ingest.py         # 消费仓：类型化取参、整次拒绝、任务结果持久化（SQLite）
  workshop_model.py # 布景工坊：卷/可裁段/墙/接缝模型、接缝判花、回溯核花
  workshop.py       # 工坊接单服务：读归一任务实际行、封签/整卷两工艺、占用与回单
  cli.py            # 人工检查命令行
samples/            # 多文档同名锚点、merge 序列冲突、显式键前后等 YAML 样例
data/               # 合成 CSV 与运行期 SQLite
demo/run_demo.py    # 端到端演示（含断言）
demo/run_workshop_demo.py  # 布景工坊裁纸交接端到端演示（含断言）
tests/              # unittest：真值表、逐行关系表、隔离、重开、规模、深度、工坊裁装
```

至少两个实质模块的分工：`yaml_parser`/`materialize`/`codec` 负责 YAML 目录与
来源，`csv_pipeline`/`ingest` 负责 CSV 数据转换与消费；`workshop_model`/`workshop`
在归一任务之上负责布景工坊的裁纸交接。

## 合同实现要点

### 类型与标量
- 仅双/单引号字符串、十进制整数（`0` 或负号可选的非零首位，正则 `^(0|-?[1-9][0-9]*)$`）、
  `true`/`false`/`null`；其余未加引号标量（`yes`、`001`、日期、`1.5`、`-0`）一律字符串。
- 映射键仅允许 `str`/`int`；`1` 与 `"1"` 是两个键，编解码显式带 `kt` 类型标签持久化。
- 显式拒绝：自定义 tag（`!`）、flow 集合（`[`/`{`）、块标量（`|`/`>`）、
  Tab 缩进、重复显式键、bool/null 键。

### 锚点与别名
- 锚点作用域为**当前文档**：多文档同名锚点互不影响；前向/未知别名、重名锚点拒绝。
- 自引用可通过解析期（pending 锚点），循环在物化期经 active 集合确定性抛
  `CircularReferenceError`，别名链逐跳记录（别名 -> 锚点位置）。
- 原始图保留共享子结构（别名指向同一锚点节点）；物化时每个插入点生成**独立子树**，
  从根上保证消费方修改不可能经别名影响其他任务。

### merge 键 `<<`
- 裸 `<<` 走内部哨兵识别；引号 `"<<"` 是普通字符串键，不触发 merge。
- merge 值为映射或映射序列，否则 `MergeTypeError`。
- 序列从左到右：第一个提供的键胜出，后者只补缺；当前映射**显式键恒覆盖** merge，
  与 `<<` 文本位置无关（前/后均有测试）。
- 只复制“键 -> 值引用”，**不递归合并**同名子映射（整体占位）。
- 每个有效键的 `Provenance` 记录：值/键位置、是否显式、别名链、每个 merge 候选的
  `source_index/won/overridden_by` 覆盖证据。

### 目录与消费仓
- 目录可发布多个命名产品，每次发布产生不可变新版本；任务绑定产品+版本快照，
  其他产品或新版本发布不影响在途/已完成任务。
- 任务按固定路径类型化取得 `source_uri`(str)、`column_mapping`(str->str)、
  `missing_value`(可选)、`partition_by`(可选)。
- 路径缺失、错误类型、原 CSV 列缺失、列映射冲突、分区键缺失 -> **整次任务拒绝**，
  单事务回滚，任务与结果均不落库。
- 归一化表逐行写入 SQLite，每列保留目标列、原 CSV 列、表头行号与配置来源 Provenance。
- 重开目录/任务仓可回查全部结果与配置链；发布快照含源文本 SHA-256，`verify` 校验。

## 设计取舍：物化独立树 vs 保留共享图按需解引用

本实现**两者都保留**：解析期保留共享锚点图（表达共享事实、支撑同一性检查与别名链），
发布时物化为带完整来源证据的独立树并落库。代价是大范围别名复用时物化有额外内存/时间
（已用 100 配置 × 万节点、200 路别名复用测试覆盖）；收益是消费隔离由数据结构天然保证、
来源证据随树自包含、重开无需重放解析。

## 运行

```bash
# 端到端演示（发布/检查/任务/拒绝/修订有效性/隔离/重开，含内置断言）
python3 demo/run_demo.py

# 布景工坊裁纸交接演示（三算例/双销位/封签整卷/占用再受理，含内置断言）
python3 demo/run_workshop_demo.py

# 全量测试（秒级）
python3 -m unittest discover -s tests -v

# 题面满规模基准（100 配置 / 万节点 / 10 万 CSV 行）
CONFCAT_FULL=1 python3 -m unittest tests.test_scale -v

# 人工检查 CLI
python -m confcat.cli publish data/catalog.db orders-product samples/orders_config.yaml
python -m confcat.cli products data/catalog.db
python -m confcat.cli query data/catalog.db orders-product doc-0 ingest/tier
python -m confcat.cli verify data/catalog.db orders-product

# 布景工坊 CLI（先 run-task 做出卷料/墙面两项归一任务，再接单）
python -m confcat.cli cut-order data/shop.db data/tasks.db data/catalog.db \
    单号 卷料任务 墙面任务 A,B sealed        # 或 whole_roll
python -m confcat.cli cut-orders data/shop.db data/tasks.db data/catalog.db
python -m confcat.cli cut-review data/shop.db data/tasks.db data/catalog.db 单号
python -m confcat.cli cut-realize data/shop.db data/tasks.db data/catalog.db 单号 pin-A
```

无第三方依赖，仅用 Python 3.11 标准库；SQLite 数据库文件为真实持久化产物。

## 布景工坊裁纸交接

戏台两面墙在转台上相邻，同一花纹按拍数纵向循环。工坊在两项归一任务的**实际行**
之上接单：卷料任务记各卷正反方向、标记相位与可用连续区间；墙面任务记墙高、
可选要求首拍与接缝两边应错开的拍数（多现场 = 台轴销位变体）。

### 裁装数学

* 卷上坐标 `c` 的拍号 `(marker_phase + c) mod period`；**取模只用于判拍，
  卷号与原坐标在回单全过程保留**，已领原卷坐标随时能对回。
* 正向卷装台自上而下坐标递增；反向卷不能翻头，装台首格取窗口右端坐标相位。
* 接缝 X→Y 要求错 `d` 拍的判定（末墙回首墙的环闭合缝同样适用）：
  `(first(Y) − last(X) − 1) mod period == d`。
* 裁纸员从可用区间滑动窗口推导真正可裁段；装台员回溯指派，逐接缝增量核花，
  并保证同一段纸（同卷坐标相交）不被两面墙重复领走。规模上限 6 面墙、4 卷纸。

### 两种交货办法

* **封签定向段 `sealed`**：库内按面裁好、封签随车，每面一段、上台不能翻头或
  另开卷。所有现场必须共用同一份（墙 → 卷段）指派；任一现场合不上即整单不能担保。
* **整卷上台 `whole_roll`**：立好第一面才看到实销，再从余卷下刀；各现场独立选段。
  已剪掉的边头按卷坐标落为占用，后续订单只能按剩下的整段领取。

### 回单、卡点与复看

* 不能承诺的单不占任何卷料，卡点指到**具体接缝与卷段**（含 kind/detail/卷号/坐标）。
* 接单即把两项归一任务的实际行与逐列映射来源快照进工坊库；墙面任务后来修订
  相位/高度，旧裁单仍按当时行复看；同编号任务实际行变化则拒绝混用、提示另办新单。
* 只有能担保的单才写 `workshop_occupancy`；重开库可按单号复查实用段、卡住的缝
  与现场接班后的实卷余量（哪一端还在）。
