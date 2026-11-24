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
  cli.py            # 人工检查命令行
  tactile/          # 触读地图展：制牌（domain/plates/fabricator）与布牌（placement/exhibition/cli）
samples/            # 多文档同名锚点、merge 序列冲突、显式键前后等 YAML 样例
  tactile/          # 点阵刻样/底座朝向两份归档产品配置 + 六展位布展输入
data/               # 合成 CSV 与运行期 SQLite
  tactile/          # 两份归一任务的实际任务行（CSV）与触读目录库
demo/run_demo.py    # 端到端演示（含断言）
demo/run_tactile_demo.py  # 触读制牌/布牌端到端演示（含断言）
tests/              # unittest：真值表、逐行关系表、隔离、重开、规模、深度、触读领域
```

至少两个实质模块的分工：`yaml_parser`/`materialize`/`codec` 负责 YAML 目录与
来源，`csv_pipeline`/`ingest` 负责 CSV 数据转换与消费。

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

## 触读地图展：制牌与布牌（`confcat.tactile`）

把“翻一块触读路牌会不会把出口摸成楼梯”建成两条归一任务 + 两个角色工序，
全部复用上面的目录版本与 CSV 归一管线（无第三方依赖）。

### 两份归一任务（各自归档产品版本）
* **点阵刻样**（产品 `tactile-dot`）：任务行给出每块牌的格数、凸点格、被遮挡
  凸点格、可摸边缘；列映射 `目标列 -> 原 CSV 列` 与配置来源逐列留血缘。
* **底座朝向**（产品 `tactile-base`）：任务行给出底座型号、`locked`（正向锁死，
  记录锁住哪一边）或 `swivel`（允许转半周）。
* 两块牌形状相同**不算**混淆；只有同一触读签名对应**两个不同实际去向**才引错。

### 领域模型（`tactile/domain.py`）
* 一维格条 0..n-1（每边至多四格）；半周翻转 `i -> n-1-i`，可摸边缘左右互换。
* 可触摸三件事：未被遮挡的凸点、可摸边缘、方向标记。遮挡格必须是已刻凸点格，
  它摸不到但仍占格（不能再刻标记）。
* **方案 A（已实现）**：`AnchorEndPolicy` 在牌的**固定一端真实空格**上刻锚点，
  约定“摸到锚点的一侧即该端”，搬到别处仍可辨；该端被占用时**不回退另一端**
  （回退会让锚点失去方向意义），该牌只能靠正向锁死底座。
* 对照方案 B（底座与展位建立可触及定位边、牌本身无标记）由 `NoMarkerPolicy`
  表达：无锚点时拆装后必须重新查验访客能否摸到那条定位边，混淆由布牌员复验。

### 两个角色
* **制牌员**（`fabricator.py`）：按工单把同一块牌的凸点遮挡、可摸边、允许翻转
  折成 `FabricatedSign.readings`（每个允许朝向一份访客触读图样），来源同时挂接
  点阵任务与底座任务的实际行、列映射和归档版本。
* **布牌员**（`placement.py`）：按入口走近侧（`L`/`R`，对侧走近做镜像）、相邻
  入口与底座锁边求触读签名交集，输出会引错的**两次具体触读**（谁、何朝向、
  摸到什么、各自去哪）；`plan_exhibition` 在展方输入（格数、改刻许可、底座
  型号与件数）内规划，库存不足或改刻许可关闭且撞形 -> **无法开放站点**，并给
  最小改刻建议；规划不擅自把普通座换成锁死座。

### 三格玩具结论（演示逐步断言）
1. 出口左点、楼梯右点，两座都锁正：签名不同，可分；
2. 只把出口制版行改成右端点：正向下两牌同形，撞形混淆；
3. 恢复左点、换成允许转半周的普通座且无方向标记：出口转半周即摸成楼梯
   （两个方向各一次混淆）；方案 A 锚点能在锚点端空闲时救回，锚点端被占用的
   牌仍只能靠锁死座；
4. 遮住端点但保留可摸边：凸点信息全失，仅靠边无法区分去向，供下一位布展员复验。

## 设计取舍：物化独立树 vs 保留共享图按需解引用

本实现**两者都保留**：解析期保留共享锚点图（表达共享事实、支撑同一性检查与别名链），
发布时物化为带完整来源证据的独立树并落库。代价是大范围别名复用时物化有额外内存/时间
（已用 100 配置 × 万节点、200 路别名复用测试覆盖）；收益是消费隔离由数据结构天然保证、
来源证据随树自包含、重开无需重放解析。

## 运行

```bash
# 端到端演示（发布/检查/任务/拒绝/修订有效性/隔离/重开，含内置断言）
python3 demo/run_demo.py

# 触读地图展端到端演示（两份归一/制牌/布牌/无法开放站点/版本隔离，含断言）
python3 demo/run_tactile_demo.py

# 全量测试（秒级）
python3 -m unittest discover -s tests -v

# 题面满规模基准（100 配置 / 万节点 / 10 万 CSV 行）
CONFCAT_FULL=1 python3 -m unittest tests.test_scale -v

# 人工检查 CLI
python -m confcat.cli publish data/catalog.db orders-product samples/orders_config.yaml
python -m confcat.cli products data/catalog.db
python -m confcat.cli query data/catalog.db orders-product doc-0 ingest/tier
python -m confcat.cli verify data/catalog.db orders-product

# 触读展 CLI：发布两份归档产品，再做触读唯一性审计与布展规划
python -m confcat.tactile.cli publish data/tactile/catalog.db \
    tactile-dot samples/tactile/dot_product_v1.yaml \
    tactile-base samples/tactile/base_product_v1.yaml
python -m confcat.tactile.cli audit data/tactile/catalog.db \
    samples/tactile/exhibition.yaml \
    data/tactile/dot_tasks.csv data/tactile/base_tasks.csv
python -m confcat.tactile.cli audit data/tactile/catalog.db \
    samples/tactile/exhibition.yaml \
    data/tactile/dot_tasks.csv data/tactile/base_tasks.csv --policy none
```

无第三方依赖，仅用 Python 3.11 标准库；SQLite 数据库文件为真实持久化产物。
