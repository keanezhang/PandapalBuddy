"""pandapal/dispatch — 入站归一化分发层。

入站是出站 broadcast 的镜像：
- InboundEnvelope  统一内部表示（类比 NormalizedEvent）
- InboundChannelAdapter  渠道方言翻译（类比 Transport）
- InboundDispatcher  唯一分发核心（类比 MessageBroadcast）
- InboundPipeline  gate 唯一持有的入口（adapter + dispatcher 绑定胶水）
"""

from pandapal.dispatch.adapter import InboundChannelAdapter
from pandapal.dispatch.dispatcher import DirectHandler, InboundDispatcher
from pandapal.dispatch.pipeline import InboundPipeline
from pandapal.dispatch.types import ChannelContext, InboundEnvelope

__all__ = [
    "ChannelContext",
    "DirectHandler",
    "InboundChannelAdapter",
    "InboundDispatcher",
    "InboundEnvelope",
    "InboundPipeline",
]
