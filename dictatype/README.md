# 听写机听打台与校样室（dictatype）

档案馆给老式机械打字机做听写副本。本包把 `audiomix` 发布、渲染的**逐样本
声音**接到抄录工具，配合手录纸面压痕格与行尾位置，重建逐击记录，并按各联
纸揭下时刻决定具名收件联的签收与编号勘误。纯 Python 3.11 + NumPy，复用
仓库内 `audiomix` 的实际混音数据仓；离线约束**最多八击、两联**。

## 与题面的对应

| 题面要素 | 实现位置 |
| --- | --- |
| 每个键一段短声样 + 一个车行动作（印字右移/退格/只抬色带） | `model.Key` / `model.Action` |
| 多落锤重叠按仓库实际 PCM 求和、舍入、削顶；一声不一定一次键入 | `acoustic`（走 audiomix 发布产品 + `ArchiveStore`）、`acoustic.mix_strikes_pcm` |
| 允许拍点与键盘状态下找共同相容的逐击记录 | `desk.TranscriptionDesk.solve`（DFS + 三路证据） |
| 实录、压痕、行尾位置共同相容 | `desk.Evidence` + 声学/压痕/行尾三闸 |
| 各处可确定的覆盖先后 | `desk.CellVerdict`（字符/压痕可确定性、覆盖击序、同拍留疑） |
| 重击覆盖只作用于当拍仍垫着的联，不可只看终局顶联 | `desk.replay`、`proofroom.ProofRoom.rebuild_from_strikes` |
| 揭纸时刻封存可确定格，留疑格以后用编号勘误条补 | `proofroom`（`seal_at_separation` 制度） |
| 预约保留整叠待终校（多等一次取证但不能按原时刻交件） | `proofroom`（`hold_for_final` 备选制度，只判定不开通道） |
| 接手已揭走的纸按当时纸序重建 | `rebuild_from_strikes` |
| 迟来压痕对未发联/已发联的不同处理 | `ProofRoom.admit_late_indentation` |
| 听打与校样都可从刊行号重新打开 | `store.IssueStore`（DK-xxxx 开档/重开/封档） |
| 离线最多八击两联 | `model.MAX_STRIKES=8`、`MAX_COPIES=2`，`SessionSpec` 强校验 |
| 重播一次只读历史收件回执 | `IssueStore.replay_receipt`（body_hash 校验，不重渲染） |

## 目录

```
dictatype/
  model.py       键/击/联纸/压痕格数据结构与离线约束
  acoustic.py    击序列 -> audiomix 发布产品 -> 仓库 PCM；快速精确混音（同语义）
  desk.py        听打台：相容解 DFS、逐联逐格可确定性与覆盖先后
  proofroom.py   校样室：封存、编号勘误条、签收、迟来压痕、接手重建
  store.py       刊行号仓库：开档/重开/封档/只读回执重播
  cli.py         命令行接口（全部可按刊行号重开）
  run_all_checks.sh
tests/test_dictatype.py   题面逐句验收（12 个案例）
demo/cli_walkthrough.sh   端到端走查
```

## 核心语义

### 1. 声学：一声不一定是一次键入

- 每个键的声样登记为 audiomix 素材；每击是一个 clip，起点为拍点时刻
  （拍距 4 个 16kHz 样本）。同拍多击产生重叠 clip，由 audiomix
  **全部有效 clip 浮点贡献先求和 → 唯一一次恰半取偶 → 削顶 int16**。
- DFS 前缀剪枝用 `mix_strikes_pcm`（与仓库严格同语义的快速混音）；每个被
  接受的完整解都再走一次**仓库块渲染 == 逐样本 Fraction 解释器 == 实录**
  的三方确认（见测试例 5b），快速路径偏差不可能流入结论。
- 同拍开放区的样本还能被后续击改写，前缀只核对**已封闭样本号**，因此
  `[P]=101` 与 `[P,Q]=303` 这类“一声多键”不会被误剪（测试例 6）。

### 2. 听打台：三路证据共同相容

候选逐击记录必须同时满足：声学 PCM 全等、手录压痕格（present/absent/
unknown）相容、终局车座等于行尾。相容解合并后逐联逐格报告：

- 字符是否可确定（多解字符不同 → 留疑）；
- 覆盖该格的击序号序列（覆盖先后）；同拍多击先后不可观察时显式留疑。

### 3. 联纸与重击覆盖

- `peel_after_strike` 决定联在第几击后揭走；揭走后落锤既不印字也不留压痕，
  该联冻结在揭纸时刻。判断签收按当时纸序逐联重建，**不只看终局顶联**。
- 抬色带击（`ribbon_lift`）车座不动：有物理压痕、无墨印；后续印字会重击
  同一格（测试例 7）。退格键（`backspace`）不印字、车座退一格。

### 4. 交接制度（二选一，本仓固化第一种）

- **封存于分离时刻（seal_at_separation，默认）**：揭纸即封存当时已可确定
  的格；留疑格只能挂**有编号勘误条**（E-DK-xxxx-nnnn）补齐后才能签收。
  封存正文只读，任何后到证据只产生勘误条，绝不改写封存格。
- **保留整叠待终校（hold_for_final）**：可多等一次取证，但被扣着的具名联
  不能按原时刻交件（测试例 9）。本仓只提供判定，不开此交接通道。
- 迟来取证每联最多一次。**迟来压痕差异化处理**（测试例 8 / 走查第 6 步）：
  已发联 → 出编号勘误条、正文冻结；未发联 → 同步复写吸收、封存前生效。

### 5. 刊行号与只读重播

- `issue-open` 顺序分配 `DK-0001…`，规格/证据落盘后不可变；`issue-close`
  封档，此后重开为只读句柄，任何写操作抛 `ReopenError`（CLI 退出码 3）。
- `receipt-replay` 只读取历史回执，按“封存格 + 勘误条”重算 `body_hash`
  比对，不触碰声学仓、不重新求解；回执被篡改即报错（测试例 10）。

## 命令速览

```bash
PY=python3
$PY -m dictatype.cli issue-open   --store DIR/store --spec spec.json
$PY -m dictatype.cli evidence-add --store DIR/store --issue DK-0001 --evidence evidence.json
$PY -m dictatype.cli transcribe   --store DIR/store --issue DK-0001 --verbose
$PY -m dictatype.cli proof-seal   --store DIR/store --issue DK-0001 --copy yi
$PY -m dictatype.cli proof-fill   --store DIR/store --issue DK-0001 --copy yi --cell 1 --glyph B
$PY -m dictatype.cli proof-late   --store DIR/store --issue DK-0001 --copy yi --cell 1 --state present --glyph B
$PY -m dictatype.cli proof-sign   --store DIR/store --issue DK-0001 --copy yi
$PY -m dictatype.cli issue-close  --store DIR/store --issue DK-0001
$PY -m dictatype.cli issue-reopen --store DIR/store --issue DK-0001
$PY -m dictatype.cli receipt-replay --store DIR/store --issue DK-0001 --copy yi
$PY -m dictatype.cli render-demo  --spec spec.json --strikes strikes.json --workdir DIR/render
```

端到端：`PYTHON=python3 bash dictatype/run_all_checks.sh`
（或 `bash demo/cli_walkthrough.sh`）。
