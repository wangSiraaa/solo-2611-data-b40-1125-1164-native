# 大件行李转盘复跑报告

复跑入口：`python3.11 tools/baggage_demo.py`

目录产品 SHA-256：`53e0f39ba0f38781a84f967983fd26a91bae6cc0d2e86906cc86130c3155dbd0`
原仓：`/workspace/artifacts/baggage.db`；收帧计数：`{'ok': 28, 'rejected': 0, 'total': 28, 'observations': 75}`

每帧保留 frame_event_id、bus、实 frame_id、原始 payload_hex 与绑定产品 SHA-256
实帧号分布：frame_id=1 18 帧；frame_id=2 1 帧；frame_id=3 9 帧

## CANONICAL-DEFAULT — 例1：左0右2，留到第2拍可领齐

策略：`simple-open-port (canonical timing check; rack policy used in two-ring case)`；最迟/实际齐套拍：`deadline=2, complete=2`；结论：**领齐/交付成功**。
- 拓扑：A圈4格；开口：P0@A0号格/开放拍=[0, 1, 2, 3, 4, 5]
- 实物组单：TWIN(左/右板旅客)袋号[1, 2]/离厅第2拍

### 逐拍说明

**第 0 拍**

- 推进前环上袋位：`{'A': [1, None, 2, None]}`
- 开口袋：`[1]`；已领/已认袋：`[1]`
- 仓库齿步：`[1]`；推进后：`{'A': [None, None, None, 2]}`

**第 1 拍**

- 推进前环上袋位：`{'A': [None, None, None, 2]}`
- 开口袋：`[]`；已领/已认袋：`[1]`
- 仓库齿步：`[1]`；推进后：`{'A': [2, None, None, None]}`

**第 2 拍**

- 推进前环上袋位：`{'A': [2, None, None, None]}`
- 开口袋：`[2]`；已领/已认袋：`[1, 2]`
- 仓库齿步：`[1]`；推进后：`{'A': [None, None, None, None]}`

**第 3 拍**

- 推进前环上袋位：`{'A': [None, None, None, None]}`
- 开口袋：`[]`；已领/已认袋：`[1, 2]`
- 仓库齿步：`[1]`；推进后：`{'A': [None, None, None, None]}`

事件：
- `recognized_at_open_port` (beat=0, bag=1)
- `recognized_at_open_port` (beat=2, bag=2)
- `group_complete` (beat=2, group=TWIN)

核对点：
- 第 0 拍为开场可领拍
- 前进格数取自仓库中的实际 teeth 原始值，不按拍数想当然

## RIGHT-AT-ONE — 例2：右袋改在1号格，第3拍到口

策略：`simple-open-port (canonical timing check; rack policy used in two-ring case)`；最迟/实际齐套拍：`deadline=3, complete=3`；结论：**领齐/交付成功**。
- 拓扑：A圈4格；开口：P0@A0号格/开放拍=[0, 1, 2, 3, 4, 5]
- 实物组单：TWIN(左/右板旅客)袋号[1, 2]/离厅第3拍

### 逐拍说明

**第 0 拍**

- 推进前环上袋位：`{'A': [1, 2, None, None]}`
- 开口袋：`[1]`；已领/已认袋：`[1]`
- 仓库齿步：`[1]`；推进后：`{'A': [None, None, 2, None]}`

**第 1 拍**

- 推进前环上袋位：`{'A': [None, None, 2, None]}`
- 开口袋：`[]`；已领/已认袋：`[1]`
- 仓库齿步：`[1]`；推进后：`{'A': [None, None, None, 2]}`

**第 2 拍**

- 推进前环上袋位：`{'A': [None, None, None, 2]}`
- 开口袋：`[]`；已领/已认袋：`[1]`
- 仓库齿步：`[1]`；推进后：`{'A': [2, None, None, None]}`

**第 3 拍**

- 推进前环上袋位：`{'A': [2, None, None, None]}`
- 开口袋：`[2]`；已领/已认袋：`[1, 2]`
- 仓库齿步：`[1]`；推进后：`{'A': [None, None, None, None]}`

事件：
- `recognized_at_open_port` (beat=0, bag=1)
- `recognized_at_open_port` (beat=3, bag=2)
- `group_complete` (beat=3, group=TWIN)

核对点：
- 第 0 拍为开场可领拍
- 前进格数取自仓库中的实际 teeth 原始值，不按拍数想当然

## EARLY-DEPARTURE — 例3：恢复0/2但第1拍离厅，交不齐

策略：`simple-open-port (canonical timing check; rack policy used in two-ring case)`；最迟/实际齐套拍：`deadline=1, complete=2`；结论：**未领齐（按离厅时刻判失败）**。
- 拓扑：A圈4格；开口：P0@A0号格/开放拍=[0, 1, 2, 3, 4, 5]
- 实物组单：TWIN(左/右板旅客)袋号[1, 2]/离厅第1拍

### 逐拍说明

**第 0 拍**

- 推进前环上袋位：`{'A': [1, None, 2, None]}`
- 开口袋：`[1]`；已领/已认袋：`[1]`
- 仓库齿步：`[1]`；推进后：`{'A': [None, None, None, 2]}`

**第 1 拍**

- 推进前环上袋位：`{'A': [None, None, None, 2]}`
- 开口袋：`[]`；已领/已认袋：`[1]`
- 仓库齿步：`[1]`；推进后：`{'A': [2, None, None, None]}`

**第 2 拍**

- 推进前环上袋位：`{'A': [2, None, None, None]}`
- 开口袋：`[2]`；已领/已认袋：`[1, 2]`
- 仓库齿步：`[1]`；推进后：`{'A': [None, None, None, None]}`

**第 3 拍**

- 推进前环上袋位：`{'A': [None, None, None, None]}`
- 开口袋：`[]`；已领/已认袋：`[1, 2]`
- 仓库齿步：`[1]`；推进后：`{'A': [None, None, None, None]}`

事件：
- `recognized_at_open_port` (beat=0, bag=1)
- `recognized_at_open_port` (beat=2, bag=2)
- `group_complete` (beat=2, group=TWIN)
- `旅客已于第 1 拍离厅，整组未交齐`

核对点：
- 第 0 拍为开场可领拍
- 前进格数取自仓库中的实际 teeth 原始值，不按拍数想当然

## TWO-RING-RACK — 两圈、两开口、六袋：互斥摆法交接 + 编号整组架交付

策略：`numbered-group-rack`；最迟/实际齐套拍：`deadline=3, complete=3`；结论：**领齐/交付成功**。
- 拓扑：A圈4格、B圈4格；开口：PA@A0号格/开放拍=[0, 1, 2, 3, 4, 5]；PB@B0号格/开放拍=[0, 1, 2, 3, 4, 5]
- 实物组单：T1(林岚)袋号[1, 2]/离厅第2拍；T2(陈默)袋号[3, 4]/离厅第3拍；T3(周禾)袋号[5, 6]/离厅第1拍

### 读标遮两标签的互斥交接

互斥整圈摆法只能分别推演；不得把不同摆法中的同号袋合并计数，也不得把两个候选身份当成同时存在的两袋。

- `AMB-1-L1`（来源 `TWO-RING-RACK-R23`）：
  - A 圈：`[1, None, 2, 3]`
  - B 圈：`[5, 4, None, 6]`
- `AMB-1-L2`（来源 `TWO-RING-RACK-R23`）：
  - A 圈：`[1, None, 3, 2]`
  - B 圈：`[5, 4, None, 6]`
- 各袋可经过的领取口：1号→['PA']；2号→['PA']；3号→['PA']；4号→['PA']；5号→['PB']；6号→['PB']

### 逐拍说明

**第 0 拍**

- 拍初环位：`{'A': [1, None, 2, 3], 'B': [5, 4, None, 6]}`
- 实际齿步：`[{'ring': 'A', 'teeth': 1, 'before': [None, None, 2, 3]}, {'ring': 'B', 'teeth': 1, 'before': [None, 4, None, 6]}]`
- 拍末环位：`{'A': [3, None, None, 2], 'B': [6, None, 4, None]}`
- 暂存架：`{'PA-01': 1, 'PA-02': None, 'PA-03': None, 'PB-01': 5, 'PB-02': None, 'PB-03': None}`
- 已许诺未占架位：`{'PA-03': 2}`
  - `promised` (beat=0, slot=PA-03, bag=2, group=T1, reason=None)
  - `contest_lost` (beat=0, slot=PA-03, bag=4, group=T2, reason=priority went to bag 2 / group T1 (depart beat 2))
  - `racked` (beat=0, slot=PA-01, bag=1, group=T1, reason=None)
  - `只形成互斥整圈摆法，不据此提前拣袋` (candidates=[2, 3])
  - `racked` (beat=0, slot=PB-01, bag=5, group=T3, reason=None)

**第 1 拍**

- 拍初环位：`{'A': [3, None, None, 2], 'B': [6, None, 4, None]}`
- 实际齿步：`[{'ring': 'A', 'teeth': 1, 'before': [None, None, 4, 2]}, {'ring': 'B', 'teeth': 1, 'before': [None, None, None, None]}]`
- 拍末环位：`{'A': [2, None, None, 4], 'B': [None, None, None, None]}`
- 暂存架：`{'PA-01': 1, 'PA-02': 3, 'PA-03': None, 'PB-01': None, 'PB-02': None, 'PB-03': None}`
- 已许诺未占架位：`{'PA-03': 2}`
  - `第1拍 PA 精确读到3号：保留 AMB-1-L1，排除互斥的 AMB-1-L2`
  - `racked` (beat=1, slot=PA-02, bag=3, group=T2, reason=None)
  - `racked` (beat=1, slot=PB-02, bag=6, group=T3, reason=None)
  - `slot_occupied_denied` (beat=1, slot=PA-02, bag=4, group=T2, reason=slot is already physically occupied by bag 3)
  - `赵误 的 T2 组单不含 6 号袋，拒绝误领` (claimant=赵误, target_bag=6)
  - `周禾 凭 T3 组单领齐 [5, 6] 号袋` (group=T3)
  - `switch_moved` (beat=1, bag=4, route=B->{A})

**第 2 拍**

- 拍初环位：`{'A': [2, None, None, 4], 'B': [None, None, None, None]}`
- 实际齿步：`[{'ring': 'A', 'teeth': 1, 'before': [None, None, None, 4]}, {'ring': 'B', 'teeth': 1, 'before': [None, None, None, None]}]`
- 拍末环位：`{'A': [4, None, None, None], 'B': [None, None, None, None]}`
- 暂存架：`{'PA-01': None, 'PA-02': 3, 'PA-03': None, 'PB-01': None, 'PB-02': None, 'PB-03': None}`
  - `racked` (beat=2, slot=PA-03, bag=2, group=T1, reason=None)
  - `林岚 凭 T1 组单领齐 [1, 2] 号袋` (group=T1)

**第 3 拍**

- 拍初环位：`{'A': [4, None, None, None], 'B': [None, None, None, None]}`
- 拍末环位：`{'A': [None, None, None, None], 'B': [None, None, None, None]}`
- 暂存架：`{'PA-01': None, 'PA-02': None, 'PA-03': None, 'PB-01': None, 'PB-02': None, 'PB-03': None}`
  - `racked` (beat=3, slot=PA-01, bag=4, group=T2, reason=None)
  - `陈默 凭 T2 组单领齐 [3, 4] 号袋` (group=T2)

拍后核对事件：
- `group was already handed over; same bags cannot count twice` (group=T1)

核对点：
- 读标 selector=2 时，仓库观测中没有 exact_bag_no/branch1_hint；脏分支位未参与袋号判断
- AMB-1-L1 与 AMB-1-L2 互斥；每份摆法都恰有6袋，未把候选合并成12袋
- 4号袋第0拍齿步后到 B2，第1拍凭道岔帧 B2→A2；不在道岔格的换环会被 switch_bag 拒绝
- PA-03 争用按离厅拍优先给 T1/2号袋；PA-02 被3号袋占住后拒绝再许给4号袋
- 每组袋号唯一；重复交 T1 组被 duplicate_delivery_blocked 拦截
- 赵误持 T2 组单索6号袋被 misclaim_blocked 拦截
