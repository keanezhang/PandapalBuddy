"""Gateway 通信层数据模型。

本模块定义 Gateway 通信层的核心数据结构：
- ConnectionState: WebSocket 连接状态枚举（从 types.py 导入并重新导出）
- AgentConnectionStatus: 连接状态快照（对外暴露，可观测）
- PendingAckEntry: At-Least-Once 投递的待确认消息记录
- GatewayConfig: Gateway 配置参数（心跳/超时/重试等）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .types import ConnectionState  # ConnectionState 在 types.py 中定义，此处重新导出



@dataclass
class AgentConnectionStatus:
    """连接状态快照（对外暴露，可观测）。

    用途：
    - 供上层模块（如 UI、监控、日志）查询当前 Gateway 连接状态
    - 通过 Gateway.get_connection_state() 获取只读快照
    - 所有字段均为只读展示，不用于内部状态机驱动

    字段设计背景：
    - last_ping_sent / last_pong_received: 用于诊断心跳健康度
    - last_activity: 收到任何帧的时间戳（message/ack/pong），比 pong 更可靠
    - reconnect_attempts: 当前连续重连次数（成功连上后清零）
    - pending_offline_msgs: OutboundQueue 中排队的消息数（断线时累积）
    - outbound_queue_size: 同 pending_offline_msgs（保留字段，未来可能区分优先级）
    - pending_ack_count: PendingAck 表中等待 ACK 的消息数（连线时累积）
    """

    conn_state: ConnectionState = ConnectionState.DISCONNECTED
    user_id: str = ""                      # 从 JWT Token 解析的用户 ID
    relay_url: str = ""                    # 当前连接的 Relay 服务器 URL
    last_ping_sent: datetime | None = None      # 最后一次发 ping 的时间（用于诊断）
    last_pong_received: datetime | None = None  # 最后一次收 pong 的时间（用于诊断）
    last_activity: datetime | None = None        # 收到任何帧的时间戳（更可靠的活性指标）
    reconnect_attempts: int = 0                 # 当前连续重连次数
    pending_offline_msgs: int = 0               # OutboundQueue 中排队的消息数
    outbound_queue_size: int = 0                # 出站队列大小（同 pending_offline_msgs）
    pending_ack_count: int = 0                  # PendingAck 表中等待 ACK 的消息数


@dataclass
class PendingAckEntry:
    """PendingAck 表中的单条记录。

    At-Least-Once 投递机制：
    - 消息发送后记入 PendingAck 表，等待服务端回 ACK
    - 后台任务定期检查超时（默认 5s），超时则重发
    - 最多重试 max_ack_retries 次（默认 3 次），失败后丢弃并日志告警
    - 收到 ACK 后从表中删除，完成投递

    字段说明：
    - sent_at: 最后一次发送时间（用于计算超时）
    - attempts: 已尝试次数（初始 0，重试 +1）
    - frame: 原始消息帧（含 msg_id，用于重发）
    """

    sent_at: datetime       # 最后一次发送时间（UTC）
    attempts: int           # 已尝试次数（0 = 首次发送）
    frame: dict[str, Any]   # 原始消息帧（含 msg_id，用于重发）


@dataclass
class GatewayConfig:
    """Gateway 配置参数（E3 零配置默认）。

    设计原则：
    - 所有参数都有合理默认值，用户无需配置即可运行
    - 参数名称清晰，见名知意
    - 时间单位统一为秒（float），便于配置

    心跳策略（应用层 Ping/Pong）：
    - 客户端（Gateway）每隔 ping_interval_s 发送 {"type": "ping", "ts": <毫秒>}
    - 服务端（Relay）收到 ping 后立即回 {"type": "pong"}
    - 客户端超过 ping_timeout_s 没收到任何帧（message/ack/pong），判定连接死亡并触发重连
    - 任何收到的帧都会刷新 last_activity 时间戳（比单独检测 pong 更可靠）

    为什么不用 RFC 6455 Control Frame？
    - Nginx 默认不转发跨跳的 WebSocket Control Frame（Ping/Pong）
    - 所以用应用层 TEXT 帧模拟心跳，确保经过 Nginx 时也能正常工作

    参数说明：
    - ping_interval_s: 心跳发送间隔（默认 20s，平衡实时性与流量）
    - ping_timeout_s: 心跳超时（默认 45s，> 2*ping_interval_s 防止误判）
    - ack_timeout_s: ACK 超时检查间隔（默认 15s，后台任务轮询周期）
    - max_ack_retries: 最大 ACK 重试次数（默认 3 次，失败后丢弃并告警）
    - outbound_queue_max_size: 出站队列最大长度（默认 500，防止内存泄漏）
    - reconnect_max_delay_s: 重连最大延迟（默认 60s，指数退避上限）
    """

    ping_interval_s: float = 20.0          # 心跳发送间隔（秒）
    ping_timeout_s: float = 45.0            # 心跳超时（秒，> 2*ping_interval_s）
    ack_timeout_s: float = 15.0             # ACK 超时检查间隔（秒）
    max_ack_retries: int = 3                # 最大 ACK 重试次数
    outbound_queue_max_size: int = 500       # 出站队列最大长度（防止内存泄漏）
    reconnect_max_delay_s: float = 60.0      # 重连最大延迟（秒，指数退避上限）
