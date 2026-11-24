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
  biblio.py         # 地方译本目录领域规则：笔名声明、候选身份、可见字段消歧（纯函数）
  index_service.py  # 译本索引出版服务：修订/撤回、收册、印前核签、两种签发、勘误、倒查
  cli.py            # 人工检查命令行
samples/            # 多文档同名锚点、merge 序列冲突、显式键前后、译本声明配置等 YAML 样例
data/               # 合成 CSV 与运行期 SQLite
demo/run_demo.py    # 端到端演示（含断言）
demo/run_biblio_demo.py  # 译本索引端到端演示：三人六册、三次修订、勘误与倒查
tests/              # unittest：真值表、逐行关系表、隔离、重开、规模、深度、译本目录
```

至少两个实质模块的分工：`yaml_parser`/`materialize`/`codec` 负责 YAML 目录与
来源，`csv_pipeline`/`ingest` 负责 CSV 数据转换与消费；`biblio`/`index_service`
在归一关系表之上构建地方译本目录与索引签发。

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

## 地方译本目录的索引出版（`biblio` / `index_service`）

题内人物与书均为虚构。编目端把一本书里可成立的“署名—译者”关系连到声明出处，
索引端只拿该版**实际可印或可询问**的区分信息判断能否唯一找到译者。

### 数据与规则

* 笔名声明（`biblio.PenNameDeclaration`）带出处：来源接入任务、原 CSV 行号、
  逐列血缘（`DeclarationSource`）。声明可按原文语种或一段卷期限定，也允许
  有人在两个时期用相同笔名（卷期区间区分）。
* 交柜书册（`biblio.HoldingCopy`）记录卷号、印本版次与扉页**实际看得见**的字段
  集合 `visible_fields`。卷号恒可见；原文语种只有印了语种栏才可问。
* `candidate_identities` 只按可核对条件筛候选；不可见的语种限定不排除声明，
  而记进候选的 `unchecked`。`evaluate_holding` 输出唯一/歧义、可问字段与
  “还缺哪项”——同一个问题书上没有答案就不能要求读者回答。

### 两种办索引的办法（任选其一，入口都在 `BiblioService`）

* **重印配号** `issue_reprint_voucher`：仅待印新版可办，须先由印前岗位
  `verify_prepress_proof` 核验具名底稿（具名者须为当前候选），再占用一个
  印号把译者绑到这一册；纸索引与书同发。事后改归属走 `issue_errata`，
  旧凭证置 `superseded` 但不删除、印号沿用，勘误可追到旧册。
* **馆员查问** `issue_inquiry`：按手中版次看得见的字逐问；能分开当场签认，
  问不出只登记缺项并保留受理凭证，不出凭证。不能签出的册仍可受理查询。

### 版本化与权限

* 编辑 `submit_revision(kind="amend"/"withdraw")` 每次落不可变版本，整库
  修订默认至多三次；撤回后候选身份按新关系表重算。凭证保存签发当时的声明
  版本快照，`recall_voucher` 次日凭原凭证号原样调回，撤回/再修订不回改旧纸。
* 岗位：`editor` 修订/撤回/勘误，`prepress` 印前核签，`librarian` 收册签发，
  `reader` 仅 `public_lookup`——读者视图不含出版社私藏的印前校样。
* `trace_print_number` 从印本印号倒查到书、凭证链、当时声明版本与勘误；
  旧藏无印号且删语种栏时，印厂无从改扉页，系统也不给它配重印号。

## 运行

```bash
# 端到端演示（发布/检查/任务/拒绝/修订有效性/隔离/重开，含内置断言）
python3 demo/run_demo.py

# 译本索引演示：三人六册、补录/撤回/勘误三次修订、凭证快照与印号倒查
python3 demo/run_biblio_demo.py

# 全量测试（秒级）
python3 -m unittest discover -s tests -v

# 题面满规模基准（100 配置 / 万节点 / 10 万 CSV 行）
CONFCAT_FULL=1 python3 -m unittest tests.test_scale -v

# 人工检查 CLI
python -m confcat.cli publish data/catalog.db orders-product samples/orders_config.yaml
python -m confcat.cli products data/catalog.db
python -m confcat.cli query data/catalog.db orders-product doc-0 ingest/tier
python -m confcat.cli verify data/catalog.db orders-product

# 译本索引 CLI（岗位用 --actor 指定：editor/prepress/librarian）
python -m confcat.cli biblio-load data/biblio_index.db data/catalog.db data/tasks.db <task> --actor editor
python -m confcat.cli biblio-receive data/biblio_index.db V05 5 待印新版 远舟 \
    --language 法文 --visible original_language --printable
python -m confcat.cli biblio-proof data/biblio_index.db PRF-05 V05 DR-05 TJIA 甲 --actor prepress
python -m confcat.cli biblio-reprint data/biblio_index.db V05
python -m confcat.cli biblio-inquiry data/biblio_index.db V06
python -m confcat.cli biblio-lookup-volume data/biblio_index.db 5
python -m confcat.cli biblio-public data/biblio_index.db 6
python -m confcat.cli biblio-recall data/biblio_index.db V-V05
python -m confcat.cli biblio-trace data/biblio_index.db YIN-001
```

无第三方依赖，仅用 Python 3.11 标准库；SQLite 数据库文件为真实持久化产物。
