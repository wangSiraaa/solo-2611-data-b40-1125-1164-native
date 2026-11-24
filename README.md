# SOLO 流式字符编码服务与原件保真修订库

本题面定义的 SOLO 编码（仅实现题面格式，不接入任何外部编码标准）的 Python 3.11
实现，以及在其之上构建的、按字符区间打补丁但保持未修改原始字节的修订库。

## 交付物

| 路径 | 内容 |
| --- | --- |
| `solo/codec.py` | 流式解码器 `Decoder`、检查点 `Checkpoint`、整件解码 `decode_document` / `decode_all` / `iter_decode`、独立编码入口 `encode_text` / `encode_document`，错误类型与路径记录 |
| `solo/revision.py` | 修订库 `RevisionLibrary`、`replace` / `preserve` 补丁、版本与批次记录、边界模式调整、字节保真校验 |
| `solo/fishdesk.py` | 观赏鱼寄养值班簿：ASCII 鱼号/尾斑/缸号/时刻解析、PAIR 夹注、可能历史枚举、点数/鱼号/观察窗过滤、隔位报告、回单/委托/遮光盒与前台销账 |
| `solo/fish_demo.py` | 题面三转折 + 隔位紧张/观察窗漏看演示，写出 `artifacts/fish_demo_report.json` |
| `solo/sample.py` | 确定性合成原件生成器（ESC/PAIR 故意跨 128 KiB 块，含模式切换、空替换、连续补丁与非法尾部所需素材） |
| `solo/demo.py` | 演示：真实原件、两批修订、未修改字节核对、错误报告、较大语料，写出 `artifacts/` |
| `solo/__main__.py` | CLI：`decode/check/encode/revise/demo` |
| `tests/test_solo.py` | 独立参考状态机、差分测试、跨块、检查点、修订、规模测试 |
| `tests/test_fishdesk.py` | 观赏鱼 canonical 三转折、勘误跨度复演、同尾斑多历史、点数/鱼号排歧、隔位压力、观察窗缺口、回单独占与上限 |
| `artifacts/` | 运行 `python3 -m solo.demo` 生成 SOLO 原件/版本；运行 `python3 -m solo.fish_demo` 另生成 `fish_demo_report.json` 与鱼簿 v0/未看清勘误 v1 的 `.solo` 正文 |

## SOLO 编码（题面定义）

- 正文从 `SINGLE` 模式开始。
- `SINGLE`：`00..7F` → 同码点 ASCII；`FF` 为 ESC；`80..FE` 非法。
- ESC 后单字节指令：`01` → 切到 `PAIR`；`00` → 切回 `SINGLE`；`FF` → 字面
  `U+00FF` 且不改模式；其它 → 错误。模式切换本身不产字符。
- `PAIR`：除 `FF`（仍是 ESC）外，两个字节组成无符号大端整数 `n`，映射
  `U+4E00 + n`，限定 `0 ≤ n ≤ 0x0FFF`。第一字节正常时，第二字节即使是 `FF`
  也属于该数值（`00 FF` = n=0x00FF），不会重新开转义。
- 正文结束遇到半个 PAIR 或半条 ESC 均为错误。
- 错误报告原字节偏移与到达该状态的路径（`DecodeError.offset` / `.path`），绝不
  用 `?` 替换后继续。解码失败后解码器进入失败态，不会假成功。

## 流式语义

`Decoder` 按任意字节块增量运行：`feed()` / `feed_into()`，偏移始终是原件正文
偏移。跨块半条 ESC、半个 PAIR 会被保存为待续状态，下一块首字节续接，因此不会
像旧的按块重新初始化解码器那样丢掉块首字符。`finish()` 校验正文末尾并返回结束
模式。

### 检查点

`Decoder.checkpoint()` 返回 `Checkpoint`，包含位置、模式、待续状态与**前缀
SHA-256**；`Checkpoint.resume(body)` / `Decoder.from_checkpoint()` 只在给定正文
前缀摘要一致时才从检查点继续（`verify=False` 可跳过，供已自行校验前缀的调用方
使用）。`Version.checkpoint_at(byte_offset)` 可直接从某个版本继续解码。

## 修订库

- 版本是不可变的真实原件字节 + 有序补丁批次；一个库先经 `add_original()` 载入
  原件（会完整解码校验，失败即拒绝）。
- `replace(start_char, end_char, text)`：按某固定原件版本的**字符**半开区间替换
  为 Unicode 文本。`preserve(start_byte, end_byte)`：要求该**字节**区间原样保留
  （可以落在字符内部；包含它的字符因此不可被替换；若边界落在纯切换字节上则拒绝）。
- 一批补丁先校验：同一基准版本、替换区间两两不相交（相邻允许）、preserve 区间
  互不重叠且不被替换触碰、替换文本可编码；任一失败则整批不发布。
- 组装：未替换字符按原字节复制（`kept_runs` 记录旧字符区间与新字节区间的对应），
  只对替换文本编码；当前输出模式与下一段原始字节解码模式不一致时，才在拼接边界
  插入合法切换（`BoundaryAdjustment`，`splice` / `boundary` / `final`）。整文件
  从不重新编码，因此不会掩盖原件差异。
- 新版本结束模式强制为 `SINGLE`，不会给后续保留字节留下悬空模式。
- 发布前独立校验：重新流式解码新正文，逐段核对未替换区间字节一致、preserve
  区间逐字节一致，并核对解码文本等于“旧 Unicode 做同批替换”的结果。全部通过
  才登记新版本；否则抛出 `RevisionError`，库不变化。

### 版本记录

`Version.records` / `BatchRecord` 记录每个替换的旧字符区间、旧字节区间、新字符
区间、新字节区间、新增字节与边界切换；`boundary_adjustments` 记录新字节偏移、
前后模式和切换字节；`kept_runs` 与 `dropped_switch_spans` 记录原样复制和未保留的
纯切换跨度。

## 观赏鱼寄养值班簿（`solo/fishdesk.py`）

在 SOLO 修订库之上实现的身份复原与前台销账层，硬上限为 **4 鱼、3 缸、6 次转移**。
核心入口：

```python
from solo.fishdesk import FishBoarding, RevisionLedger, FrontDesk, PickupCase

ledger = RevisionLedger.from_text(ascii_ledger_text)
replay = ledger.replay()                 # 保留全部不矛盾的身份历史
report = replay.identity_report()        # 确定位置、相混鱼群、每户状态
decision = FrontDesk(...).evaluate(case, replay)
```

### 值班簿语法

核心记录全部为 ASCII；`#` 注释可含本 SOLO 码区可编码的 PAIR 夹注字符：

```text
FISH F1 OWNER JIA SPOT WHITE TANK A AT 09:00
NET N1 TANK A OWNER JIA
MERGE A,B INTO C ID M1 AT 10:00
SCOOP 2 SPOT WHITE FROM A TO B ID E1 AT 10:10
SPLIT C INTO A:1,B:1 ID S1 AT 11:00
SHIFTNET N1 FROM C TO A ID N1-MOVE AT 11:10
COUNT C 2 AT 11:05
SEE F1,F2 IN A AT 11:06
WITNESS S1 DEST A SPOT WHITE BY LIN AT 11:01
WINDOW S1 IN A WHITE,F1 COMPLETE AT 11:02
WINDOWGAP S1 IN A AT 11:03
```

`RevisionLedger.replace_line_containing()` 先调用 `Version.decode()`，用解码出的
字符区间构造 `replace()` 补丁并走 `RevisionLibrary.apply()`；每行同时保留字符跨度和
原件正文的字节跨度。勘误后重新 `Version.decode()`、重新解析和复演，绝不直接改内存结论。

### 身份历史规则

- `SCOOP n ...` 会枚举从候选缸/隔网中选取 n 条鱼的所有组合；多条同尾斑鱼被捞出且只记
  去向时，所有成组路径和逐次 `MoveTrace` 都保留。
- `COUNT`、`SEE`、`WITNESS`、`WINDOW` 是过滤器：只排除矛盾世界，不“挑一个顺眼编号”。
- `WINDOW ... COMPLETE` 做完整观察匹配；非 COMPLETE 保留漏看情况下的注入匹配；
  `WINDOWGAP` 明确表示缺少一段观察，不产生尾斑结论。
- `IdentityReport` 给出每条鱼的唯一/可能位置、稳定或不确定的提货 lot、跨缸相混鱼群，
  以及每户是 `located`、`located_but_shared` 还是 `identity_mixed`。
- `NetRack` 是可写的有限隔位架：预约私网和整隔转缸都会检查目标缸容量；`slot_report()`
  则从值班簿复演每个缸的最终占用与峰值隔位。

### 前台销账与遮光盒

`FrontDesk` 依次核对：回单存在且已交回、未重复销账、鱼主/受托人委托有效、当日有可提
隔间/lot、身份复演一致且该 lot 的内容恰好等于本笔回单指向的鱼。成功后生成
`OpaqueTransportBox` 并标记回单已用；该对象没有前台开盒看尾斑的操作，调用
`open_at_front_desk()` 会直接拒绝。`ReceiptBook.issue()` 从存储上保证两张回单不能指向
同一条鱼。

### 题面验收弧线

1. 甲鱼 `F1=WHITE`、乙鱼 `F2=PLAIN`，并缸 C 后分去 A/B；`WITNESS ... DEST A SPOT WHITE`
   留下唯一历史 `F1→A`、`F2→B`。受托人持 A 委托和甲回单时，可封盒、交付并销账。
2. 经 `RevisionLibrary.apply` 把具名见证行改成
   `# ERRATUM_UNSEEN which fish entered A`（即“未看清哪条入 A”的可编码 ASCII 勘误；
   题面 SOLO 的 PAIR 码区为 U+4E00..U+5DFF，不能编码常用字“进”），复演得到两个互换
   世界，前台以 `IDENTITY_MIXED` 暂缓。
3. 用反向勘误恢复见证行，或读取原版本 v0，身份重新唯一；但撤销受托人提 A 的委托后，
   前台以 `MANDATE_REVOKED` 暂缓。

新寄养另演示两种养法压力：C 缸只有两个私网隔位时，`N1/N2` 整隔转缸可接甲、乙；
丙、丁两条 `WHITE` 只能在 `OPEN:C` 相混，单独回单不可提。增加 `N3/N4` 后四笔订单都可接。
观察窗演示中，完整看到 A 口的 `WHITE` 可定位甲；该段成为 `WINDOWGAP` 后保留三个世界并
暂缓。

```bash
python3 -m solo.fish_demo
python3 -m solo fish-demo
python3 -m unittest tests.test_fishdesk -v
```

## 运行

```bash
python3 -m unittest discover -s tests -v      # 全部测试
python3 -m solo.demo                          # 演示并写 artifacts/
python3 -m solo check artifacts/original.bin
python3 -m solo decode --checkpoint 10 artifacts/version_1.bin
python3 -m solo revise artifacts/original.bin patches.json > new.bin
```

`patches.json` 格式：

```json
{"patches": [
  {"replace": [0, 3, "新文本"]},
  {"preserve": [100, 200]}
]}
```

## 测试覆盖

- 1/2/7/4096 字节分块对独立朴素状态机逐字符、逐字节区间核对（含非法输入与
  错误偏移）。
- 生成器把 ESC/PAIR 精确放在 128 KiB 边界上；跨块、模式切换无字符、替换为空、
  连续/相邻补丁、非法尾部均有专项测试。
- 修订：未修改区间字节等同、每版本可重开解码、失败批次不发布、preserve 逐字节
  保留、新文本等于同批替换、无悬空模式。
- 观赏鱼：canonical 交付/未看清暂缓/撤委托暂缓、勘误字符与字节跨度、同尾斑组合历史、
  点数与看清鱼号排歧、四鱼三缸六转移上限、隔位峰值、观察窗缺口、回单唯一和遮光盒拒开。
- 规模：10 MiB 原件、一批 100 个补丁（`ScaleTests`），未修改运行按原字节复制。
