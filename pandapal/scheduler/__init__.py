"""pandapal.scheduler — Agent 调度层（5.2 重写版）。

公开 API：
- AgentScheduler: 调度器
- ReplyIdManager / ReplyId / ReplyScope: reply_id 协议
- convert_stream_event_to_normalized: StreamEvent → NormalizedEvent 转换
"""

from pandapal.scheduler.reply_manager import ReplyId, ReplyIdManager, ReplyScope
from pandapal.scheduler.scheduler import AgentScheduler
from pandapal.scheduler.stream_to_normalized import (
    convert_stream_event_to_normalized,
    STREAM_TO_NORMALIZED_MAPPING,
)

__all__ = [
    "AgentScheduler",
    "ReplyIdManager",
    "ReplyId",
    "ReplyScope",
    "convert_stream_event_to_normalized",
    "STREAM_TO_NORMALIZED_MAPPING",
]
