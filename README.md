# SOLO 流式字符编码服务与原件保真修订库

本题面定义的 SOLO 编码（仅实现题面格式，不接入任何外部编码标准）的 Python 3.11
实现，以及在其之上构建的、按字符区间打补丁但保持未修改原始字节的修订库。

## 交付物

| 路径 | 内容 |
| --- | --- |
| `solo/codec.py` | 流式解码器 `Decoder`、检查点 `Checkpoint`、整件解码 `decode_document` / `decode_all` / `iter_decode`、独立编码入口 `encode_text` / `encode_document`，错误类型与路径记录 |
| `solo/revision.py` | 修订库 `RevisionLibrary`、`replace` / `preserve` 补丁、版本与批次记录、边界模式调整、字节保真校验 |
| `solo/sample.py` | 确定性合成原件生成器（ESC/PAIR 故意跨 128 KiB 块，含模式切换、空替换、连续补丁与非法尾部所需素材） |
| `solo/comics/` | 漫画译刊校样层：ASCII 登记稿解析与校验、自然读序、版面工单一入口 `ProofDesk.proof`、页边指引与自然重排两种工艺、不可收回的页边条/封样授权 |
| `solo/demo.py` | 演示：真实原件、两批修订、未修改字节核对、错误报告、较大语料，写出 `artifacts/` |
| `solo/__main__.py` | CLI：`decode/check/encode/revise/demo/comics` |
| `tests/test_solo.py` | 独立参考状态机、差分测试、跨块、检查点、修订、规模测试 |
| `tests/test_comics.py` | 登记稿校验、读序、修订定位、两种工艺、跨页授权与校稿/付印分阶段退件测试 |
| `artifacts/` | `python3 -m solo.demo` 生成的真实原件、各版本字节与 `manifest.json`；`python3 -m solo comics` 生成的校样与 `comics/manifest.json` |

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

## 漫画译刊校样（`solo/comics/`）

登记稿是存在修订库里的真实 SOLO 正文（`SAY` 行可夹 PAIR 字符），每行一条 ASCII
指令，最多两页八泡：

- `PAGE/PANEL/ACTOR/BUBBLE/SAY/TAIL/REPLY` 登记画格、气泡、说话人、尾线端点与答复
  关系。`A>B` 表示 B 接 A 的话（A 先读），`A<B` 表示 A 接 B 的话（B 先读）。
- 校验（`validate_manuscript`）定位到具体行：尾线落点没有说话人（`TAIL_MISWIRED`）、
  缺尾线、引用悬空在解析期拒绝、自环（`SELF_LOOP`）、答复成环（`REPLY_CYCLE`）、
  场外旁白未排末位等，都能凭行号与字符/原件字节跨度回到原页查明。
- 自然读法（`reading_order`）是每行从左到右、逐行向下的拓扑序：答复边强制答者在
  后，场外旁白压到末位，无关系的独立短句按自然位并列。

版面工只有一个出样入口 `ProofDesk.proof(version_id, permit, order_book)`，传入稿件
版本和本页许可，返回**校样** `ProofSheet` 或**退件** `Rejection`：

- `stage="proofing"`：稿子本身不成立（自环、成环、尾线错等），先卡在校稿。
- `stage="press"`：稿子合法，但授权不允许这次付印（对白刊印权被撤、页边条已售、
  画格已签样封死、无重描许可）。

两种工艺任选其一，成张的纸都能逐行读回真实先后（`ProofSheet.readback()` 与纸面
`COLOPHON readback=` 一致）：

1. **页边引读（margin）**：原画与气泡原位不动，版心不变；仅在许可页边格内印编号，
   指引必须能实际指到对应气泡。唯一能同时引到倒置两泡的窄条（`reaches` 恰为该泡
   集合）可先售给同册另一页；`OrderBook` 里售出即不可收回，复查同一单时该页在付印
   被拒（`MARGIN_STRIP_SOLD`）。
2. **自然重排（relayout）**：仅在许可重描区域内移动气泡、把尾线改接到**原说话者**
   的锚点，把对白排回自然读序；已由画家签样的画格只能原位，再排即在付印被拒
   （`PANEL_SEALED`）。

小样三泡一行（左 A、中 B、右 C，C 为场外旁白）。编辑只把末行 `A>B` 的单字符 `>`
经 `RevisionLibrary.apply` 按字符位置改成 `<`：旧版读序 `A B C` 可直接交，新版须读成
`B A C`，C 始终排末。两版各出两张真实校样（两种工艺），编辑处可由
`Version.decode` 的实际文字和 `PatchRecord` 的原件字节跨度找回。

## 运行

```bash
python3 -m unittest discover -s tests -v      # 全部测试（含 test_comics）
python3 -m solo.demo                          # 演示并写 artifacts/
python3 -m solo comics                        # 漫画两版校样、退件与 artifacts/comics/
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
- 规模：10 MiB 原件、一批 100 个补丁（`ScaleTests`），未修改运行按原字节复制。
- 漫画：登记稿语法与两页八泡上限、尾线错接/悬空/自环/成环、行自然读序、按字符位置
  改答复关系并经解码与原件字节跨度找回、两种工艺的真实纸面读回、窄条售给同册另一页
  不可收回、签样画格封死，以及自环卡在校稿、撤对白权卡在付印的分阶段退件。
