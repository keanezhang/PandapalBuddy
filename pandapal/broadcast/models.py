"""Broadcast 层异常与历史兼容类。

★ 5.2 改造后：
- OutboundMessage 已被 NormalizedEvent 替代
- 事件侧 DispatchPolicy 已删除（2026-06）：分发策略移到渠道侧
  （ChannelDispatchPolicy，见 channel_registry.py）

本文件保留旧的异常类名以维持向后兼容（旧 Consumer 仍可 `from pandapal.broadcast.models import BroadcastConfigError`）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# ── 旧 OutboundMessage 兼容：转发到 NormalizedEvent ──────────
# 不再建议使用，5.2 之后所有代码应使用 NormalizedEvent
from pandapal.events.normalized import NormalizedEvent  # noqa: F401


@runtime_checkable
class BroadcastGatewayProtocol(Protocol):
    """保留旧 Protocol 名以向后兼容（5.2 重写为 send(NormalizedEvent)）。"""

    async def send(self, event: NormalizedEvent) -> None:
        ...


class BroadcastConfigError(Exception):
    """广播层配置错误。"""

    def __init__(self, message_type: str = "", reason: str = "") -> None:
        self.message_type = message_type
        self.reason = reason
        msg = reason or message_type
        super().__init__(f"Broadcast config error: {msg}")


class BroadcastTargetError(Exception):
    """广播目标错误（target_only 策略但 target_channel_ids 为空）。"""

    def __init__(self, message_type: str = "") -> None:
        self.message_type = message_type
        super().__init__(
            f"Broadcast target error [{message_type}]: "
            "target_only policy requires non-empty target_channel_ids"
        )
