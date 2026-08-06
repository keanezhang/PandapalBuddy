"""pandapal.events — 归一化事件系统。

提供跨渠道统一的 NormalizedEvent 数据类与 EventType 枚举（18 种）。
Broadcast → Transport 边界一律使用 NormalizedEvent，业务模块用各自的强类型 dataclass。

设计原则（与 5.2.A 一致）：
1. 业务层用各自强类型 dataclass（InboundMessage、AgentResult、ApprovalRequest…）
2. 跨渠道边界（Broadcast → Transport）一律用 NormalizedEvent
3. NormalizedEvent 是 frozen=True 的不可变对象，可哈希、可放心跨线程
"""

from .normalized import EventType, NormalizedEvent

__all__ = ["EventType", "NormalizedEvent"]
