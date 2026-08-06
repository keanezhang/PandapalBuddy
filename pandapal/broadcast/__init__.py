"""pandapal.broadcast — 跨渠道统一广播层。

公开 API：
- MessageBroadcast: 归一化事件广播器
- ChannelRegistry: 渠道注册表（纯元信息）
- Transport: 渠道传输层抽象
- ChannelDispatchPolicy / EventCategory: 渠道分发策略 / 事件类别
"""

from pandapal.broadcast.broadcaster import (
    BroadcastConfigError,
    BroadcastGatewayProtocol,
    MessageBroadcast,
)
from pandapal.broadcast.channel_registry import (
    ChannelCapability,
    ChannelDispatchPolicy,
    ChannelInfo,
    ChannelPolicyPredicate,
    ChannelRegistry,
    ChannelRegistryError,
    ChannelType,
)
from pandapal.broadcast.policy import (
    EVENT_CATEGORY,
    EventCategory,
)
from pandapal.broadcast.transport import Transport

__all__ = [
    # 广播器
    "MessageBroadcast",
    "BroadcastConfigError",
    "BroadcastGatewayProtocol",
    # 渠道
    "ChannelRegistry",
    "ChannelInfo",
    "ChannelType",
    "ChannelCapability",
    "ChannelDispatchPolicy",
    "ChannelPolicyPredicate",
    "ChannelRegistryError",
    # 事件类别
    "EventCategory",
    "EVENT_CATEGORY",
    # 传输层
    "Transport",
]
