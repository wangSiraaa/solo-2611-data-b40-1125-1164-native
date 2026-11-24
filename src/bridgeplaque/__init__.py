"""bridgeplaque：旧木桥展牌签发。

在 pqscan（footer 统计保守剪枝 + 两阶段扫描）之上实现馆员可反复调用的
署年流程：按桥段查地方案卷 -> 读真正命中的结果 Parquet -> 拼接梁片年轮、
与逐年宽窄参照带交叉对齐 -> 结合工单安装先后与许可签发标年或暂缓，
并登记台账供凭单号查回。
"""
from __future__ import annotations


class PlaqueError(Exception):
    """署年流程的输入/证据错误（案卷缺梁、断口无法接回、许可缺失等）。"""
