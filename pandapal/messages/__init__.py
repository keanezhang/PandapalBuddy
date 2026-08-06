"""pandapal/messages — 消息类型契约层。

所有 message_type 字符串常量在此统一声明，禁止在业务模块中散落魔法字符串。
"""

from .types import HITLDecision, RouterMessageType

__all__ = [
    "RouterMessageType",
    "HITLDecision",
]
