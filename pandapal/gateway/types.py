from enum import Enum


class ConnectionState(str, Enum):
    """WebSocket 连接状态机（四态）。

    状态转换图：
                          ┌─────────────┐
                          │             │
                          ▼             │
        ┌──────────┐   ┌──────────┐   ┌────────────┐   ┌──────────┐
        │DISCONNECTED│──▶│CONNECTING │──▶│ CONNECTED  │──▶│RECONNECTING│
        └──────────┘   └──────────┘   └────────────┘   └──────────┘
             ▲               │               │               │
             │               │               │               │
             │               └───────────────┘               │
             │                   (断线时)                     │
             │                                               │
             └───────────────────────────────────────────────┘
                        (重连失败 / 达到最大重试次数)

    状态说明：
    - DISCONNECTED: 初始状态，或未连接状态
    - CONNECTING: 正在建立 WebSocket 连接（WSS 握手中）
    - CONNECTED: 连接已建立，可以收发消息
    - RECONNECTING: 检测到断线，正在重连中（指数退避等待）

    设计要点：
    - 使用 str Enum 便于 JSON 序列化（对外暴露时直接转字符串）
    - 重连失败后会回到 DISCONNECTED，避免无限重连
    - RECONNECTING 状态会触发指数退避逻辑（1s → 2s → 4s → ... → 60s）
    """

    DISCONNECTED = "disconnected"   # 初始状态 / 连接已关闭
    CONNECTING = "connecting"       # WSS 握手进行中
    CONNECTED = "connected"         # 连接已建立，正常通信中
    RECONNECTING = "reconnecting"   # 断线重连中（指数退避等待）
