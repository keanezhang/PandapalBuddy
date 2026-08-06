"""pandapal.gateway — Gateway 通信层（5.2 重写版）。"""

from pandapal.gateway.gateway import Gateway
from pandapal.gateway.models import (
    AgentConnectionStatus,
    GatewayConfig,
    PendingAckEntry,
)
from pandapal.gateway.types import ConnectionState
from pandapal.gateway.wss_transport import WSSGateway

__all__ = [
    "Gateway",                 # 原版 WSS plumbing（保留，供 WSSGateway 内部复用）
    "WSSGateway",              # ★ 5.2 新增：Transport 适配器（NormalizedEvent → WSS 帧）
    "GatewayConfig",
    "ConnectionState",
    "AgentConnectionStatus",
    "PendingAckEntry",
]
