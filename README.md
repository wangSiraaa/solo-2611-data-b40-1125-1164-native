# SOLO 数据工程本地实现

仓库含两个纯 Python 3.11 包，均为可复跑的源码与合成输入：

1. **audiomix/**：多采样率剪辑时间轴与真实混音档案（逐样本 PCM、内容寻址
   块存储、恰半取偶舍入与削顶）。
2. **dictatype/**：听写机听打台与校样室。把 audiomix 发布/渲染的逐样本声音
   接到抄录工具，配合手录纸面压痕格与行尾位置，离线（最多八击两联）重建
   相容逐击记录、逐联逐格可确定性与覆盖先后，并按揭纸时刻封存、编号勘误、
   具名联签收；听打与校样都可从刊行号重新打开，历史收件回执支持只读重播。

验证：

```bash
# audiomix（17 个测试 + 规模案例）
bash audiomix/run_all_checks.sh
# dictatype（12 个题面逐句验收 + CLI 端到端走查）
PYTHON=python3 bash dictatype/run_all_checks.sh
```
