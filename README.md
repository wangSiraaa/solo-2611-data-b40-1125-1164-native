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
  arena/            # 虚构楹联擂台：词册审读、赛制裁定、获选墙（SQLite）
    wordbook.py     #   联律词册装载；沿 TaskRepository 行/列溯源补录单与签录人
    scrolls.py      #   带卷号参赛原文、同音标记、选手封注装载
    review.py       #   审读台：完整覆盖断词/同音一致读法/联律裁定/全联标注
    contest.py      #   赛务柜台（题匾制/封匣制）、承诺名额、回放、截止回执
  cli.py            # 人工检查命令行（含 arena-* 赛务命令）
samples/            # 多文档同名锚点、merge 序列冲突、显式键前后、擂台词册/卷 YAML
data/               # 合成 CSV 与运行期 SQLite（data/arena/ 为擂台词册与参赛卷）
demo/run_demo.py    # 端到端演示（含断言）
demo/run_arena_demo.py  # 擂台端到端演示（白露两断法/两套赛制/回放/词册改版）
tests/              # unittest：真值表、逐行关系表、隔离、重开、规模、深度、擂台合同
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

## 设计取舍：物化独立树 vs 保留共享图按需解引用

本实现**两者都保留**：解析期保留共享锚点图（表达共享事实、支撑同一性检查与别名链），
发布时物化为带完整来源证据的独立树并落库。代价是大范围别名复用时物化有额外内存/时间
（已用 100 配置 × 万节点、200 路别名复用测试覆盖）；收益是消费隔离由数据结构天然保证、
来源证据随树自包含、重开无需重放解析。

## 楹联擂台（`confcat.arena`）

给 confcat 接的一届虚构楹联擂台。声类、词类、特许读音全部以**本届自定词册**为准，
词册与带卷号参赛原文是两份归一任务，经**同一个** `TaskRepository` 入口受理：编辑换
带新词条的词册或递正文不同的新卷，都是按 `column_mapping` 再跑一次归一任务。

### 数据血缘与改版重开
- 词册每行一个词条（词形/词类/词末平仄/读音/补录单单号/签录人），
  `Wordbook.trace_entry` 沿 TaskRepository 的实际归一行（`task_row`）与列血缘
  （`task_column` 的原 CSV 列、表头行号）查回该版词条的补录单及**真实签录人**。
- 每场比赛固定开场时的词册任务与收卷任务（=目录版本快照）；撤词条、改词册只影响
  新开场次，**已挂出的旧卷仍按原词册重开**，挂卷订单里的全联标注与裁定原样可查。

### 审读台合同（`arena/review.py`）
- DFS 枚举每联**所有能完整覆盖原文**的断词；同形多词条逐一套用。
- 上、下联对应词**字数与词类相合、词末声类相反**（平对仄）。
- 同一卷标作同音的一组字经并查集并入同读组件，组件可取读音为各词读音集合交集；
  每个读法都是覆盖全联的完整赋值，**不产生两种互斥读法的局部拼接**。
- 淘汰断法逐条留诊断；获选卷附可向作者解释的全联标注（逐词带补录单/签录人依据）。

### 两套赛制（任选一套，开场即公布）
- `plaque` 题匾制：赛前刻定一种上联断法整场公开沿用，后来收的卷须守原匾；
  匾词必须能拼成场上联原文且每段都是词册收录词条，否则拒绝开场。
- `seal` 封匣制：选手揭卷前封好上联断法与下联读音，挂卷时拆封；封注晚于截止
  时刻（封匣到期、事后补封无效）或对不上审读台读法均拒收。

### 获选墙与承诺
- 受理开场即公布**最多能承诺的不同作者人数**（墙格 `slots`），承诺槽位在挂卷时
  落实到卷号；一位作者最多挂一卷，名额满后后来的卷一律拒收。
- 对外墙只显示获选卷；申诉人可按本人卷号经 `appeal` 对照已用词册版本与截止回执，
  回执不含他人卷信息。
- `replay_match` 从同一场赛前记录另开回放（词册/原文/题匾照前，仅换收卷任务与
  受理次序），演示已刻匾或已到期封匣如何限制柜台。

### 小例（白露/清霜/晚霞，读音取词条唯一读音）
白露整词为名、词末仄；拆开为 白（形平）+ 露（名仄）。清霜只录整词名平，
晚霞只录 晚（形仄）+ 霞（名平）。故甲投清霜只有整词读法合式，乙投晚霞只有
拆词读法合式；撤白露整词条后甲卷无完整合法读法。

```bash
# 擂台端到端演示（含全部题面场景与断言）
python3 demo/run_arena_demo.py

# 人工柜台命令（库均为演示产物）
python -m confcat.cli arena-open  data/demo_arena.db data/demo_arena_tasks.db \
    data/demo_arena_catalog.db M1 seal wb-v1 sc-m1 2 \
    --deadline 2026-11-02T00:00:00 --plaque 白露
python -m confcat.cli arena-hang    data/demo_arena.db data/demo_arena_tasks.db \
    data/demo_arena_catalog.db M1 J01
python -m confcat.cli arena-review  data/demo_arena.db data/demo_arena_tasks.db \
    data/demo_arena_catalog.db M1 J02
python -m confcat.cli arena-wall    data/demo_arena.db data/demo_arena_tasks.db \
    data/demo_arena_catalog.db M1
python -m confcat.cli arena-promises data/demo_arena.db data/demo_arena_tasks.db \
    data/demo_arena_catalog.db M1
python -m confcat.cli arena-appeal  data/demo_arena.db data/demo_arena_tasks.db \
    data/demo_arena_catalog.db M1 J01
```

## 运行

```bash
# 端到端演示（发布/检查/任务/拒绝/修订有效性/隔离/重开，含内置断言）
python3 demo/run_demo.py

# 擂台端到端演示
python3 demo/run_arena_demo.py

# 全量测试（秒级）
python3 -m unittest discover -s tests -v

# 题面满规模基准（100 配置 / 万节点 / 10 万 CSV 行）
CONFCAT_FULL=1 python3 -m unittest tests.test_scale -v

# 人工检查 CLI
python -m confcat.cli publish data/catalog.db orders-product samples/orders_config.yaml
python -m confcat.cli products data/catalog.db
python -m confcat.cli query data/catalog.db orders-product doc-0 ingest/tier
python -m confcat.cli verify data/catalog.db orders-product
```

无第三方依赖，仅用 Python 3.11 标准库；SQLite 数据库文件为真实持久化产物。
