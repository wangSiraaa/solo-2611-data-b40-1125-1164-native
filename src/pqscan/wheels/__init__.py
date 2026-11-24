"""模型铁路展演排轮（wheelset weekend staging）。

在 pqscan 的扁平 Parquet 扫描能力之上，新增一层"展演用轮入口"：
- drum.py     ：计程鼓模 10 翻零算术（鼓面读数、跨零次数、可行走格数范围）；
- history.py  ：按实物轴对接回履历，处理换轮签条模糊产生的互斥分支；
- quota.py    ：每个轴对的已耗/剩余额度（馆内自订翻修期限，恰好到限即收车）；
- schedule.py ：封存/共用两条路线的节目单回溯搜索与拒绝理由；
- runner.py   ：读扁平 Parquet + 行程表，落盘值班员交接清单与回放。
"""
