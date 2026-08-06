"""Gateway — 完整版 Gateway 通信层。

职责：
- WSS 连接生命周期（握手、鉴权、关闭）
- Ping/Pong 心跳维持与超时检测
- 断线检测与指数退避重连（1s → 60s）
- 出站消息 At-Least-Once（PendingAck + 重试）
- 断线暂存（OutboundQueue）+ 重连补发
- 入站消息回调分发
- 在线渠道查询

心跳设计（标准 Ping/Pong 模式）：
- Gateway（客户端）每 ping_interval_s 发送 {"type": "ping", "ts": <毫秒时间戳>}
- Relay（服务端）收到 ping 后立即回 {"type": "pong"}
- Gateway 收到 pong 时更新 last_pong_received
- 收到任何帧（message/ack/pong/offline_batch）都刷新 last_activity
- 超过 ping_timeout_s 未收到任何帧 → 判定连接死亡 → 触发重连
- 单向发起：只有客户端发 ping，服务端只回 pong，不互发

设计约束：
- I1: 无状态可重启
- I2: At-Least-Once 投递
- I3: 主动存活探测（Ping/Pong）
- I4: Fail-Safe（断线不阻塞启动，不向上抛异常）
- I5: 最小权限连接（JWT）
- E3: 零配置默认
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from pandapal.gateway.models import (
    AgentConnectionStatus,
    GatewayConfig,
    PendingAckEntry,
)
from pandapal.gateway.types import ConnectionState

logger = logging.getLogger(__name__)

# 防止 websockets 库在 DEBUG/INFO 级别打印含 Token 的完整握手 URL
logging.getLogger("websockets").setLevel(logging.WARNING)

# JWT 本地过期检查的时钟偏移余量（秒）：exp <= now + 余量 即视为临期，提前 proactive 刷新
_TOKEN_EXPIRY_LEEWAY_S = 30.0


class Gateway:
    """完整版 Gateway 通信层管理器。

    架构角色（Gateway = 内外消息关口）：
    ═══════════════════════════════════════════════════════════════════════════
    │                        Gateway 的核心职责                         │
    ═══════════════════════════════════════════════════════════════════════════
    │                                                                        │
    │   ┌─────────────────────────────────────────────────────────┐       │
    │   │                 入站消息处理（Inbound）                  │       │
    │   │  Relay ──► Gateway ──► InboundAdapter ──► Dispatcher ──► handler             │       │
    │   │             │                                           │       │
    │   │             ├─ message 帧：调用 _inbound_handler       │       │
    │   │             │   （实际是 InboundPipeline.handle        │       │
    │   │             │    → GatewayInboundAdapter.normalize     │       │
    │   │             │    → InboundDispatcher.dispatch）        │       │
    │   │             ├─ ack 帧：从 PendingAck 移除             │       │
    │   │             ├─ pong 帧：更新心跳时间戳                 │       │
    │   │             └─ offline_batch：逐条处理                 │       │
    │   └─────────────────────────────────────────────────────────┘       │
    │                                                                        │
    │   ┌─────────────────────────────────────────────────────────┐       │
    │   │                   出站消息（Outbound）                   │       │
    │   │  Agent ──► Broadcast ──► Gateway ──► Relay ──► 用户 │       │
    │   │                             │                                        │       │
    │   │                             └─ 通过 _ws_send() 发送 message 帧     │       │
    │   │                                At-Least-Once 保证（PendingAck）      │       │
    │   └─────────────────────────────────────────────────────────┘       │
    │                                                                        │
    │   特殊说明：                                                        │
    │   - IPC 通信（本地渠道）不走 Gateway，直接回调                     │
    │   - Gateway 是 远程渠道 与 Agent 通信的必经之路                   │
    ═══════════════════════════════════════════════════════════════════════════

    完整生命周期（由 Bootstrap 编排）：

    1️⃣ 初始化（Bootstrap Step 6 — run_local.py）
       ────────────────────────────────────────────────────────
        gateway = Gateway(
            relay_url="wss://relay.example.com/ws",
            jwt_token="eyJ...",
            config=GatewayConfig(),
        )
        → 仅创建对象，不建立连接（懒加载）

    2️⃣ 注册入站回调（Bootstrap Step 11 — _finalize_wiring）
       ────────────────────────────────────────────────────────
        gateway.register_inbound_handler(pipeline.handle)
        → InboundPipeline.handle → GatewayInboundAdapter.normalize → InboundDispatcher.dispatch
        → Router 类型最终回落 router.inject_inbound_message（经 Dispatcher）

        broadcast.attach_to_gateway(gateway)
        → 绑定 Gateway 到 Broadcast 层（用于出站消息分发）

    3️⃣ 启动连接（Bootstrap Step 11 — _finalize_wiring 之后）
       ────────────────────────────────────────────────────────
        await gateway.establish_relay_connection()
        → 触发 _connect()：WSS 握手 → JWT 鉴权 → 启动后台任务
        → 连接失败不抛异常，进入后台重连（指数退避）

    架构设计要点：
    - 🔌 依赖注入：Gateway 不依赖 Router/Broadcast，通过 attach_to_gateway 注入
    - ⏱️ 懒连接：构造时不连，等 establish_relay_connection() 才连
    - 🛡️ Fail-Safe：连接失败不阻塞启动，进入后台重连
    - 📡 可观测：通过 get_connection_state() 获取连接状态快照
    """

    def __init__(
        self,
        relay_url: str,
        jwt_token: str,
        config: GatewayConfig | None = None,
    ) -> None:
        if not relay_url:
            raise ValueError("relay_url cannot be empty")
        if not jwt_token:
            raise ValueError("jwt_token cannot be empty")

        # ══════════════════════════════════════════════════════════════════════════════
        # 核心配置（不可变）
        # ══════════════════════════════════════════════════════════════════════════════
        self._relay_url = relay_url          # Relay 的 WebSocket 地址（wss://...）
        self._jwt_token = jwt_token          # JWT 认证令牌（用于握手鉴权）
        self._config = config or GatewayConfig()  # 心跳/超时/重试配置

        # ══════════════════════════════════════════════════════════════════════════════
        # 连接状态管理
        # ══════════════════════════════════════════════════════════════════════════════
        self._conn_state = ConnectionState.DISCONNECTED  # 四态：DISCONNECTED/CONNECTING/CONNECTED/RECONNECTING
        self._user_id = ""                              # 从 JWT Token 解析的用户 ID
        self._ws: Any = None  # WebSocket 连接对象（websockets.WebSocketClientProtocol）

        # ══════════════════════════════════════════════════════════════════════════════
        # Ping/Pong 心跳状态
        # ══════════════════════════════════════════════════════════════════════════════
        # 心跳策略：应用层 TEXT 帧 {"type":"ping"}/{"type":"pong"}
        # （不用 RFC 6455 Control Frame，因为 Nginx 不转发跨跳 Control Frame）
        self._last_ping_sent: datetime | None = None      # 最后一次发 ping 的时间
        self._last_pong_received: datetime | None = None  # 最后一次收 pong 的时间
        self._last_activity: datetime | None = None  # 收到任何帧的时间戳（包括 message/ack/pong）
        self._ping_task: asyncio.Task | None = None        # 后台 ping 发送协程
        self._ack_check_task: asyncio.Task | None = None   # 后台 ACK 超时检查协程
        self._receive_task: asyncio.Task | None = None      # 后台消息接收协程

        # ══════════════════════════════════════════════════════════════════════════════
        # 重连状态
        # ══════════════════════════════════════════════════════════════════════════════
        self._reconnect_attempts = 0   # 当前重连次数（用于指数退避计算）
        self._reconnecting = False       # 是否正在重连中（防止并发重连）
        self._shutdown_requested = False  # 是否请求关闭（阻止重连）

        # ══════════════════════════════════════════════════════════════════════════════
        # 并发写保护
        # ══════════════════════════════════════════════════════════════════════════════
        # 多个协程可能同时调用 _ws_send（ping_loop + send_message_frame + ack retry）
        # 需要用 Lock 保护，防止帧交错
        self._ws_lock = asyncio.Lock()

        # ══════════════════════════════════════════════════════════════════════════════
        # PendingAck（At-Least-Once 投递保证）
        # ══════════════════════════════════════════════════════════════════════════════
        # 消息发送后记入此表，收到 ACK 后移除。
        # 超时未收到 ACK → 重试（最多 max_ack_retries 次）。
        # 超过重试次数 → 触发 on_send_failed 回调（通知 Broadcast 最终失败）。
        self._pending_ack: dict[str, PendingAckEntry] = {}

        # ══════════════════════════════════════════════════════════════════════════════
        # OutboundQueue（出站断线消息暂存）
        # ══════════════════════════════════════════════════════════════════════════════
        # 断线时，出站消息暂存到此队列（FIFO）。
        # 重连成功后，_flush_outbound_queue() 按序补发。
        # 队列有界（outbound_queue_max_size），溢出时驱逐最旧的消息。
        self._outbound_queue: deque[dict[str, Any]] = deque()

        # ══════════════════════════════════════════════════════════════════════════════
        # 回调函数（依赖注入）
        # ══════════════════════════════════════════════════════════════════════════════
        self._inbound_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        # ↑ 入站消息回调（由装配层注入，指向 InboundPipeline.handle）
        #
        # 什么是"入站"？
        #   ┌─────────┐  入站 (Inbound)   ┌───────┐
        #   │  Agent  │  ◄──────────────  │ Relay │  ◄── 用户/设备
        #   │(Gateway)│    Relay 转发      │       │
        #   └─────────┘  出站 (Outbound)  └───────┘
        #              ──────────────────►
        #
        # 注册位置（完整调用链，v2.0 起由装配层直接注入，不再经 Router）：
        #   1. app 装配层构造 InboundPipeline 并 gateway.register_inbound_handler(pipeline.handle)
        #   2. register_inbound_handler() 设置 self._inbound_handler = handler
        #   3. handler 实际是 InboundPipeline.handle → adapter.normalize → dispatcher.dispatch
        #   4. Adapter 归一化 → InboundDispatcher 分类分流
        #
        # 回调目的（解耦 Gateway 与入站处理链）：
        #   - Gateway 只负责 WebSocket 通信（收发消息、ACK、重连）
        #   - Gateway 不关心消息内容是什么、应该怎么处理
        #   - 收到消息后，调用 _inbound_handler(frame) 回调
        #   - GatewayInboundAdapter 归一化，InboundDispatcher 分流到对应 handler
        #
        # 触发时机：Relay 转发用户/设备的消息给 Agent 时
        #   示例：用户在企微发消息 → WeComBridge → Relay → Agent
        #         此时 _inbound_handler(frame) 被调用
        #
        # 签名：async def handler(frame: dict) -> None
        #   - frame: 见 _on_message_received() 中的帧结构说明
        #
        # 设计模式：依赖注入（Dependency Injection）+ 回调模式

        self._on_auth_failed_callback: Callable[[], Awaitable[None]] | None = None
        # ↑ 认证失败回调（401 时触发，Bootstrap 注册此方法用于刷新 Token）

        self._on_send_failed_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
        # ↑ 消息发送最终失败回调（超过 ACK 重试次数后触发）
        #   Broadcast 注册此方法，用于通知上层消息丢失）

        # ══════════════════════════════════════════════════════════════════════════════
        # Token 自动刷新状态（JWT 过期自动续期）
        # ══════════════════════════════════════════════════════════════════════════════
        self._refresh_lock = asyncio.Lock()  # refresh 互斥：一个在途，其余等待后复用结果
        self._refreshed_tokens: set[str] = set()  # 熔断：同一旧 token 只刷新一次
        self._on_token_refreshed_callback: Callable[[str], Awaitable[None]] | None = None
        # ↑ token 刷新成功回调（参数为新 token，装配层注册用于回写 auth_store.json）
        self._on_auth_expired_callback: Callable[[], Awaitable[None]] | None = None
        # ↑ 认证彻底失效回调（refresh 被 Relay 401 拒绝时触发，装配层注册用于跳登录页）

        # ══════════════════════════════════════════════════════════════════════════════
        # 离线消息进度（可观测性）
        # ══════════════════════════════════════════════════════════════════════════════
        self._pending_offline_msgs = 0  # 当前正在处理的离线消息数量

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    async def establish_relay_connection(self) -> None:
        """建立与 Relay 的 WebSocket 连接。

        I4: 连接失败不抛异常，进入后台重连。
        """
        self._shutdown_requested = False
        await self._connect()

    async def close_relay_connection(self, reason: str = "shutdown") -> None:
        """优雅关闭连接。"""
        self._shutdown_requested = True

        # 发送 close 帧（告知 Relay 正常关闭）
        if self._ws is not None:
            try:
                await self._ws_send({"type": "close", "reason": reason})
            except Exception:
                pass

        # 停止后台任务（内部会关闭 ws）
        await self._stop_background_tasks()

        # 清理状态
        self._pending_ack.clear()
        self._outbound_queue.clear()
        self._conn_state = ConnectionState.DISCONNECTED

        logger.info("Gateway connection closed (reason=%s)", reason)

    async def send_message_frame(self, frame: dict[str, Any]) -> None:
        """发送出站消息帧。

        I4: 断线时暂存到 OutboundQueue，不抛异常。
        I2: 连线时记入 PendingAck 等待 ACK。
        """
        msg_id = frame.get("msg_id")

        if self._conn_state == ConnectionState.CONNECTED and self._ws is not None:
            try:
                await self._ws_send(frame)
                # 记入 PendingAck
                if msg_id:
                    self._pending_ack[msg_id] = PendingAckEntry(
                        sent_at=datetime.now(timezone.utc),
                        attempts=0,
                        frame=frame,
                    )
            except Exception as e:
                logger.warning("Send failed, enqueuing: %s", e)
                self._enqueue_outbound(frame)
        else:
            logger.info("[Gateway] Not connected, enqueueing to outbound queue: msg_id=%s", msg_id)
            self._enqueue_outbound(frame)

    def get_connection_state(self) -> AgentConnectionStatus:
        """获取连接状态快照（只读，O1 可观测）。"""
        return AgentConnectionStatus(
            conn_state=self._conn_state,
            user_id=self._user_id,
            relay_url=self._relay_url,
            last_ping_sent=self._last_ping_sent,
            last_pong_received=self._last_pong_received,
            last_activity=self._last_activity,
            reconnect_attempts=self._reconnect_attempts,
            pending_offline_msgs=self._pending_offline_msgs,
            outbound_queue_size=len(self._outbound_queue),
            pending_ack_count=len(self._pending_ack),
        )

    def force_reconnect(self) -> None:
        """强制重连（运维命令）。不重置 reconnect_attempts。

        在任何非 DISCONNECTED 状态下都可触发重连，
        处理 ws 已丢失但状态仍为 CONNECTED 的边界情况。
        """
        if self._conn_state == ConnectionState.DISCONNECTED:
            return
        asyncio.create_task(self._safe_do_force_reconnect())

    def register_inbound_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """注册入站消息回调（装配层调用一次）。

        注册位置：
        - 调用方：app 装配层（app.py）
        - 注册内容：InboundPipeline.handle（→ GatewayInboundAdapter.normalize → Dispatcher）
        - 调用时机：Bootstrap 装配阶段

        回调目的（解耦 Gateway 与入站处理链）：
        ┌──────────────────────────────────────────────────────────┐
        │  Gateway 职责：WebSocket 通信（收发消息、ACK、重连）    │
        │  Pipeline.handle：原始 frame → Adapter.normalize → Dispatch  │
        │  Adapter 职责：原始 frame → InboundEnvelope 归一化       │
        │  Dispatcher 职责：按类型分流到 直通/Router handler       │
        └──────────────────────────────────────────────────────────┘

        工作流程：
        1. Gateway 收到 Relay 转发的消息（type="message"帧）
        2. Gateway 调用 _inbound_handler(frame) 回调
        3. 实际调用的是 InboundPipeline.handle(frame)
           → GatewayInboundAdapter.normalize(frame) → InboundDispatcher.dispatch(env)
        4. Adapter 归一化 → Dispatcher 分流 → 对应 handler

        设计模式：依赖注入（Dependency Injection）+ 回调模式
        """
        self._inbound_handler = handler
        logger.debug("Inbound handler registered")

    def update_jwt_token(self, new_token: str) -> None:
        """热更新 JWT Token（不触发重连）。"""
        self._jwt_token = new_token
        logger.debug("JWT token updated (hot-swap)")

    def register_on_auth_failed_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> None:
        """注册认证失败回调（Bootstrap 调用）。"""
        self._on_auth_failed_callback = callback

    def register_on_token_refreshed_callback(
        self, callback: Callable[[str], Awaitable[None]]
    ) -> None:
        """注册 token 刷新成功回调（装配层调用）。

        Args:
            callback: async def callback(new_token: str) → None
                刷新成功后触发，用于通知前端回写 auth_store.json。
        """
        self._on_token_refreshed_callback = callback

    def register_on_auth_expired_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> None:
        """注册认证彻底失效回调（装配层调用）。

        触发时机：refresh 被 Relay 明确 401 拒绝（超出宽限期或签名无效），
        前端收到通知后应登出并跳登录页。

        Args:
            callback: async def callback() → None
        """
        self._on_auth_expired_callback = callback

    def register_on_send_failed_callback(
        self, callback: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        """注册消息发送最终失败回调（广播层调用）。

        Args:
            callback: async def callback(msg_id: str, frame: dict) → None
        """
        self._on_send_failed_callback = callback

    # ──────────────────────────────────────────────
    # Internal: Connection Lifecycle
    # ──────────────────────────────────────────────

    async def _connect(self, _triggered_by_reconnect: bool = False) -> None:
        """尝试建立 WebSocket 连接。

        Args:
            _triggered_by_reconnect: 是否由 _reconnect_with_backoff 调用。
                为 True 时连接失败不再触发新的重连任务（由外层循环控制重试）。
        
        连接流程：
        0. Proactive 刷新：本地解码 exp，已过期/临期则先 _ensure_fresh_token()
           依据：JWT 只在握手时验证，已建立的连接不会因过期被踢——
           过期只可能发生在连接建立时，所以连接前检查即全覆盖
        1. 构造握手 URL（含 JWT Token 作为 Query Parameter）
        2. 调用 websockets.connect() 建立 WSS 连接
           - 禁用 ping_interval（避免 Nginx 不转发 Control Frame 导致假断线）
        3. 连接成功后：
           - 重置重连计数器
           - 解析 user_id（从 JWT Token）
           - 初始化活性时间戳
           - 启动 3 个后台任务：_ping_loop / _ack_check_task / _receive_loop
           - 补发 OutboundQueue 中的暂存消息
        4. 连接失败：
           - 401/403 → reactive 兜底：_ensure_fresh_token() 刷新后重连
             （同 token 已刷过一次仍被拒 → 熔断，降级为普通 backoff）
           - 其他错误 → 触发重连（仅非重连调用时）
        """
        self._conn_state = ConnectionState.CONNECTING

        # ══════════════════════════════════════════════════════════════════
        # Step 0: Proactive 刷新（连接前本地检查 exp）
        # ══════════════════════════════════════════════════════════════════
        if self._is_token_expired():
            logger.info("JWT expired or near expiry, refreshing proactively...")
            outcome = await self._ensure_fresh_token()
            if outcome == "auth_expired":
                # 已通知前端重新登录 → 停止重连，等待用户重新登录
                # （_shutdown_requested 会终止外层 backoff 循环；
                #   重新登录后 establish_relay_connection() 会重置该标志）
                self._conn_state = ConnectionState.DISCONNECTED
                self._shutdown_requested = True
                return
            # "ok" / "network_error" 都继续：ok 用新 token；network_error 拿旧 token 试连，
            # 撞 403 会走 reactive 分支，网络恢复后自然成功

        try:
            import websockets

            # ══════════════════════════════════════════════════════════════════
            # 构造握手 URL
            # ══════════════════════════════════════════════════════════════════
            # JWT Token 作为 Query Parameter 传递（Relay 握手时验证）
            # channel_id=__agent__：标识这是 Agent 的连接（不是用户设备）
            # channel_type=websocket：标识连接类型
            url = f"{self._relay_url}?token={self._jwt_token}&channel_id=__agent__&channel_type=websocket"

            # ══════════════════════════════════════════════════════════════════
            # WebSocket 连接参数说明
            # ══════════════════════════════════════════════════════════════════
            # 问题背景：
            #   - 本项目通过 Nginx 反向代理连接 Relay
            #   - Nginx 不转发 RFC 6455 Control Frame（Ping/Pong）
            #   - 客户端发 Control Ping → Nginx 不转发 → 服务端收不到
            #   - 服务端无法回 Control Pong → 客户端等超时 → 误判断线
            #
            # 解决方案：
            #   - ping_interval=None：禁用客户端主动发 Control Ping
            #   - ping_timeout=None：取消等待 Control Pong
            #   - 存活探测改用应用层 TEXT 帧 {"type":"ping"}/{"type":"pong"}
            #   - TEXT 帧可正常穿越 Nginx（它是普通 WebSocket 消息，不是 Control Frame）
            self._ws = await websockets.connect(
                url,
                ping_interval=None,   # 禁用客户端主动 Control Ping（避免 Nginx 截断导致假断线）
                ping_timeout=None,    # 对应取消等待，存活检测由应用层 _ping_loop 负责
                logger=logging.getLogger("websockets.client"),
            )

            # ── 连接成功，更新状态 ──
            self._conn_state = ConnectionState.CONNECTED
            self._reconnect_attempts = 0  # 重置重连计数器（重连成功）
            self._user_id = self._extract_user_id_from_token()

            # 日志脱敏：不打印含 Token 的完整 URL
            logger.info(
                "Gateway connected to relay: url=%s, user_id=%s",
                self._relay_url, self._user_id,
            )

            # ══════════════════════════════════════════════════════════════════
            # 初始化活性时间戳（连接建立即为"活跃"）
            # ══════════════════════════════════════════════════════════════════
            # _last_activity：收到任何帧时刷新（message/ack/pong/offline_batch）
            # _last_pong_received：收到 pong 时刷新（本项目中就是 _last_activity）
            now = datetime.now(timezone.utc)
            self._last_activity = now
            self._last_pong_received = now

            # ══════════════════════════════════════════════════════════════════
            # 启动后台任务（3 个守护协程）
            # ══════════════════════════════════════════════════════════════════
            # 1. _ping_loop：每隔 ping_interval_s 发应用层 ping，检测超时
            # 2. _ack_check_task：每隔 1s 检查 PendingAck 是否超时，超时则重试
            # 3. _receive_loop：持续接收 Relay 发来的消息帧
            self._ping_task = asyncio.create_task(self._ping_loop())
            self._ack_check_task = asyncio.create_task(self._check_pending_ack_timeouts())
            self._receive_task = asyncio.create_task(self._receive_loop())

            # ══════════════════════════════════════════════════════════════════
            # 重连后补发暂存消息
            # ══════════════════════════════════════════════════════════════════
            # 断线期间，出站消息被暂存到 OutboundQueue（FIFO 队列）。
            # 重连成功后，按序补发这些消息（保证 At-Least-Once）。
            await self._flush_outbound_queue()

        except Exception as e:
            self._conn_state = ConnectionState.DISCONNECTED

            # ══════════════════════════════════════════════════════════════════
            # 检查是否为 401/403 认证失败（异常类型优先，字符串兜底）
            # ══════════════════════════════════════════════════════════════════
            # 401/403 说明 JWT Token 已过期或无效，需要刷新 Token 后重连。
            # Relay server.py 在 accept() 前 close(4003)，Starlette 转成 HTTP 403，
            # 两者都按"token 无效"处理。
            status_code = self._extract_http_status(e)
            if status_code in (401, 403):
                logger.error("Gateway auth failed (HTTP %d): %s", status_code, e)
                if self._jwt_token in self._refreshed_tokens:
                    # 熔断：这个 token 刷新过一次还 401/403（如两端密钥不一致/服务端配置问题）
                    # → 不是过期问题，不再 refresh，走普通 backoff 重连（避免死循环）
                    logger.error(
                        "Auth still rejected after refresh; stop refreshing, normal backoff"
                    )
                    if not self._shutdown_requested and not _triggered_by_reconnect:
                        asyncio.create_task(self._safe_reconnect_with_backoff())
                    return
                # fire-and-forget：锁在 _ensure_fresh_token 内部
                asyncio.create_task(self._safe_refresh_and_reconnect())
                return

            logger.warning("Gateway connection failed: %s", e)
            # ══════════════════════════════════════════════════════════════════
            # 触发重连（条件：非 shutdown + 非重连调用）
            # ══════════════════════════════════════════════════════════════════
            # _triggered_by_reconnect=True 时，外层 _reconnect_with_backoff 的循环
            # 会控制重试，不需要再创建新的重连任务。
            if not self._shutdown_requested and not _triggered_by_reconnect:
                asyncio.create_task(self._safe_reconnect_with_backoff())

    async def _stop_background_tasks(self) -> None:
        """停止所有后台任务。

        先关闭 WebSocket（使 _receive_loop 的 async for 自然退出），
        再 cancel 所有 tasks 并给予短超时等待，避免卡在网络 I/O。
        """
        # 先关闭 ws，让 _receive_loop 跳出 async for
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                pass
            self._ws = None

        for task in [self._ping_task, self._ack_check_task, self._receive_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=2.0
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
        self._ping_task = None
        self._ack_check_task = None
        self._receive_task = None

    async def _do_force_reconnect(self) -> None:
        """执行强制重连。"""
        self._conn_state = ConnectionState.DISCONNECTED
        await self._reconnect_with_backoff()

    # ──────────────────────────────────────────────
    # Internal: Token Refresh（JWT 过期自动续期）
    # ──────────────────────────────────────────────

    @staticmethod
    def _extract_http_status(exc: Exception) -> int | None:
        """从 websockets 握手异常中提取 HTTP 状态码（异常类型优先，字符串兜底）。

        websockets 版本差异：
        - ≥12（当前锁定 15.0.1）：InvalidStatus，状态码在 e.response.status_code
        - 旧版：InvalidStatusCode，状态码在 e.status_code
        - 字符串兜底：兼容 Nginx 夹层等只给文本报错的场景
        """
        try:
            import websockets.exceptions as ws_exc
        except ImportError:
            ws_exc = None

        if ws_exc is not None:
            # websockets ≥12：InvalidStatus（getattr 防御：旧版无此类）
            invalid_status = getattr(ws_exc, "InvalidStatus", None)
            if invalid_status is not None and isinstance(exc, invalid_status):
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if isinstance(status, int):
                    return status
            # 旧版兼容：InvalidStatusCode
            invalid_status_code = getattr(ws_exc, "InvalidStatusCode", None)
            if invalid_status_code is not None and isinstance(exc, invalid_status_code):
                status = getattr(exc, "status_code", None)
                if isinstance(status, int):
                    return status

        error_str = str(exc)
        if "401" in error_str or "Unauthorized" in error_str:
            return 401
        if "403" in error_str or "Forbidden" in error_str:
            return 403
        return None

    def _is_token_expired(self) -> bool:
        """本地检查 JWT 是否已过期或临期（不验签，仅读 exp）。

        exp <= now + 30s（30s 时钟偏移余量）→ True。
        解析失败返回 False（不阻塞连接，交给服务端握手时判定）。
        """
        try:
            import jwt as pyjwt

            payload = pyjwt.decode(
                self._jwt_token, options={"verify_signature": False}
            )
            exp = payload.get("exp")
            if not isinstance(exp, (int, float)):
                return False  # 无 exp 字段 → 交给服务端判定
            now = datetime.now(timezone.utc).timestamp()
            return exp <= now + _TOKEN_EXPIRY_LEEWAY_S
        except Exception:
            return False  # 解析失败不阻塞连接，交给服务端判定

    def _derive_refresh_url(self) -> str:
        """从 relay_url 推导 /auth/refresh 的 HTTP 地址。

        wss://host/relay/ws → https://host/auth/refresh
        （前端登录走同 host 的 /auth/*，nginx 同机路由，推导可靠）
        """
        base = self._relay_url.removesuffix("/relay/ws")
        base = base.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
        return f"{base}/auth/refresh"

    async def _ensure_fresh_token(self) -> str:
        """确保持有新鲜 JWT（proactive / reactive 共用内核：锁互斥 + 熔断）。

        Returns:
            "ok"            — token 可用（本来就新鲜，或已成功刷新并热更新）
            "network_error" — Relay 不可达/超时/5xx/429，走正常 backoff，下次连接再试
            "auth_expired"  — Relay 明确 401（超宽限期/签名无效），已通知前端重新登录

        并发与熔断设计：
        - _refresh_lock 互斥：同一时刻只有一个 HTTP refresh 在途，
          其余协程等待后通过双重检查复用结果
        - _refreshed_tokens 熔断：同一旧 token 只向 Relay 刷一次
          （401 或成功时标记），防止 "refresh→403→refresh" 死循环
          和 auth_expired 通知风暴；网络错误不标记（允许重试）
        """
        async with self._refresh_lock:
            # ── 锁内双重检查 1：当前 token 未过期 ──
            # （本来就新鲜，或等待锁期间已被并发刷新者换掉）→ 直接复用结果
            if not self._is_token_expired():
                return "ok"
            # ── 锁内双重检查 2：同一 token 已被 Relay 明确拒绝过 ──
            # （401 时已标记 + 已通知过前端）→ 不重复请求、不重复通知
            if self._jwt_token in self._refreshed_tokens:
                return "auth_expired"

            old_token = self._jwt_token
            refresh_url = self._derive_refresh_url()
            logger.info("Refreshing JWT via %s ...", refresh_url)

            # ── 调用 Relay /auth/refresh ──
            import httpx

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        refresh_url,
                        headers={"Authorization": f"Bearer {old_token}"},
                    )
            except httpx.HTTPError as e:
                # 请求级异常（连接失败/超时/DNS 等）按瞬时错误处理，下次连接再试
                logger.warning("Token refresh request failed (network): %s", e)
                return "network_error"

            if resp.status_code == 401:
                # Relay 明确拒绝（超宽限期/签名无效）→ 熔断标记 + 通知前端重新登录
                self._refreshed_tokens.add(old_token)
                logger.warning("Token refresh rejected (401): auth expired, notifying frontend")
                await self._fire_auth_expired()
                return "auth_expired"

            if resp.status_code != 200:
                # 5xx / 429 等按瞬时错误处理（不标记熔断，允许下次重试）
                logger.warning(
                    "Token refresh failed (HTTP %d), treating as transient",
                    resp.status_code,
                )
                return "network_error"

            try:
                new_token = resp.json()["token"]
            except (ValueError, KeyError):
                logger.warning("Token refresh response malformed (bad JSON or missing 'token')")
                return "network_error"
            if not isinstance(new_token, str) or not new_token:
                # token 属 ID 类字段：值非法绝不放行（§九），按服务端异常当瞬时错误处理
                logger.warning("Token refresh response 'token' field invalid (not a non-empty str)")
                return "network_error"

            # ── 刷新成功：热更新 + 熔断标记 + 通知前端回写 auth_store.json ──
            self._refreshed_tokens.add(old_token)
            self._refreshed_tokens.discard(new_token)  # 新 token 若 403 允许再刷一次（密钥滚动场景）
            self.update_jwt_token(new_token)
            logger.info("Token refresh succeeded")
            await self._fire_token_refreshed(new_token)
            return "ok"

    async def _safe_refresh_and_reconnect(self) -> None:
        """Reactive 兜底：403/401 后刷新 token 并按结果分流（异常隔离）。

        分流策略：
        - "ok"            → _do_force_reconnect()（用新 token 立即重连）
        - "network_error" → _safe_reconnect_with_backoff()（正常退避，不撞墙）
        - "auth_expired"  → 什么都不做（已通知前端重新登录）
        """
        try:
            outcome = await self._ensure_fresh_token()
        except Exception as e:
            logger.error("Unexpected error in token refresh: %s", e)
            return

        if self._shutdown_requested:
            return

        if outcome == "ok":
            await self._do_force_reconnect()
        elif outcome == "network_error":
            await self._safe_reconnect_with_backoff()
        # "auth_expired" → 不重连，等待用户重新登录

    async def _fire_token_refreshed(self, new_token: str) -> None:
        """安全地执行 on_token_refreshed 回调（异常隔离，复刻 _safe_on_auth_failed_callback 模式）。"""
        if not self._on_token_refreshed_callback:
            return
        try:
            await self._on_token_refreshed_callback(new_token)
        except Exception as e:
            logger.warning("on_token_refreshed callback error: %s", e)

    async def _fire_auth_expired(self) -> None:
        """安全地执行 on_auth_expired 回调（异常隔离，复刻 _safe_on_auth_failed_callback 模式）。"""
        if not self._on_auth_expired_callback:
            return
        try:
            await self._on_auth_expired_callback()
        except Exception as e:
            logger.warning("on_auth_expired callback error: %s", e)

    # ──────────────────────────────────────────────
    # Internal: Receive & Dispatch
    # ──────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        """WebSocket 消息接收循环。

        核心职责：
        1. 持续接收 Relay 发来的消息帧（async for 迭代 WebSocket）
        2. 收到消息 → 调用 _on_message_received() 处理
        3. 检测到 shutdown → 优雅退出
        4. 循环退出（正常关闭或异常）→ 触发重连（只要非主动 shutdown）

        关键设计：
        - async for raw_msg in self._ws:
          WebSocket 连接关闭时，async for 会自然退出（不会抛异常）。
          - 正常关闭：Relay 发送 close frame → ws 关闭 → async for 退出
          - 异常断开：网络中断 → ws 关闭 → async for 退出
        - 无论何种退出，只要不是主动 shutdown，都必须触发重连
          （保证网络抖动后自动恢复）
        """
        try:
            # ════════════════════════════════════════════════════════════════
            # async for 迭代 WebSocket 连接
            # ════════════════════════════════════════════════════════════════
            # - 收到消息时，async for 产生 raw_msg（str 或 bytes）
            # - WebSocket 关闭时，async for 自然退出（不会抛异常）
            #   - 正常关闭：Relay 发 close frame → ws.close() → async for 退出
            #   - 异常断开：网络中断 → ws 关闭 → async for 退出
            async for raw_msg in self._ws:
                # 检查是否请求关闭（SIGINT/SIGTERM → _shutdown_event.set()）
                if self._shutdown_requested:
                    break  # 优雅退出，不重连

                # 处理收到的消息帧（按 type 分发）
                await self._on_message_received(raw_msg)

        except Exception as e:
            # 异常退出（不应该到这里，async for 应该自然退出）
            if not self._shutdown_requested:
                logger.warning("WebSocket receive error: %s", e)

        # ════════════════════════════════════════════════════════════════
        # 循环退出 → 触发重连（只要非主动 shutdown）
        # ════════════════════════════════════════════════════════════════
        # 触发条件：
        #   - Relay 重启（close frame）
        #   - 网络断开（WebSocket 超时）
        #   - Nginx 主动断开
        # 不触发条件：
        #   - 主动 shutdown（close_relay_connection() 设置了 _shutdown_requested=True）
        if not self._shutdown_requested:
            logger.warning(
                "WebSocket receive loop ended (server closed or error), triggering reconnect"
            )
            # 标记状态为断开（_reconnect_with_backoff 会检查状态）
            self._conn_state = ConnectionState.DISCONNECTED
            # 创建重连任务（指数退避）
            asyncio.create_task(self._safe_reconnect_with_backoff())

    async def _on_message_received(self, raw_msg: str | bytes) -> None:
        """处理接收到的消息帧。

        核心职责：
        1. 解析原始消息（bytes → str → JSON dict）
        2. 刷新活性时间戳（_last_activity）
        3. 按 type 分发到具体处理分支

        消息类型分发：
        - "pong":         Relay 对我们 ping 的回复 → 更新 _last_pong_received
        - "ack":          Relay 收到我们发的消息 → 从 PendingAck 移除
        - "message":       Relay 转发的入站消息 → 回 ACK → 触发 Router 回调
        - "offline_batch": Relay 推送的离线消息批次 → 逐条处理
        - 其他:           未知类型 → 警告并忽略

        关键设计：
        - 收到任何有效帧都刷新 _last_activity（连接活性证据）
        - 心跳超时检测基于 _last_activity，不只是 pong
          （因为 message/ack 也证明连接存活）
        """
        # ══════════════════════════════════════════════════════════════════
        # Step 1: 解析原始消息
        # ══════════════════════════════════════════════════════════════════
        try:
            # bytes → str（WebSocket 可能返回 bytes）
            if isinstance(raw_msg, bytes):
                raw_msg = raw_msg.decode("utf-8")
            # str → dict（JSON 解析）
            # frame 结构总览（Relay ↔ Agent 通信协议）：
            #
            # 1. type="pong" - Relay 对 ping 的回复
            #    {"type": "pong", "ts": 1716542400000}
            #
            # 2. type="ack" - Relay 确认收到 Agent 发的消息
            #    {"type": "ack", "msg_id": "msg_abc123"}
            #
            # 3. type="message" - Relay 转发的入站消息（最重要）
            #    {
            #      "type": "message",
            #      "msg_id": "msg_abc123",
            #      "source_channel_id": "wechat_xxx",
            #      "payload": {
            #        "message_type": "user_instruction",
            #        "user_id": "user_123",
            #        "session_id": "session_456",
            #        "content": <any>,
            #        "source_channel_id": "wechat_xxx"
            #      }
            #    }
            #
            # 4. type="offline_batch" - Relay 推送的离线消息批次
            #    {"type": "offline_batch", "messages": [{...}, ...]}
            frame = json.loads(raw_msg)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("Invalid frame received: %s", e)
            return  # 解析失败 → 丢弃（不刷新 _last_activity）

        # ══════════════════════════════════════════════════════════════════
        # Step 2: 刷新活性时间戳（连接存活的证据）
        # ══════════════════════════════════════════════════════════════════
        # 关键设计：收到任何有效帧都刷新（不只是 pong）
        #   - ping → pong：证明连接存活
        #   - message/ack：证明连接存活（有业务流量）
        #   - 心跳超时基于 _last_activity，不是 _last_pong_received
        #     （避免 Nginx 不转发 Control Frame 导致误判）
        self._last_activity = datetime.now(timezone.utc)

        # ══════════════════════════════════════════════════════════════════
        # Step 3: 按 type 分发处理
        # ══════════════════════════════════════════════════════════════════
        frame_type = frame.get("type", "message")  # 默认 type="message"

        if frame_type == "pong":
            # ══════════════════════════════════════════════════════════════════
            # Relay 对我们 ping 的回复
            # ══════════════════════════════════════════════════════════════════
            # 帧结构：
            #   {"type": "pong", "ts": 1716542400000}
            #   - ts: 毫秒时间戳（Relay 收到 ping 的时间）
            #
            # 更新 _last_pong_received（可选，主要用于可观测性）
            self._last_pong_received = datetime.now(timezone.utc)
            logger.debug("Pong received, activity refreshed")

        elif frame_type == "ack":
            # ══════════════════════════════════════════════════════════════════
            # Relay 确认收到我们发的消息
            # ══════════════════════════════════════════════════════════════════
            # 帧结构：
            #   {"type": "ack", "msg_id": "msg_abc123"}
            #   - msg_id: 我们发送的消息 ID（用于从 PendingAck 移除）
            #
            # 从 PendingAck 表移除（消息已送达）
            # 如果超时未收到 ACK → _check_pending_ack_timeouts() 会重试
            self._on_ack_received(frame.get("msg_id", ""))

        elif frame_type == "message":
            # ══════════════════════════════════════════════════════════════════
            # Relay 转发的入站消息（用户/设备 → Relay → Agent）
            # ══════════════════════════════════════════════════════════════════
            # 帧结构：
            #   {
            #     "type": "message",
            #     "msg_id": "msg_abc123",           # 消息唯一 ID（用于去重和 ACK）
            #     "source_channel_id": "wechat_xxx", # 来源渠道 ID（可选，可能在 payload 内）
            #     "payload": {                        # 消息内容（路由层解析）
            #       "message_type": "user_instruction", # 必须是 RouterMessageType.* 之一
            #       "user_id": "user_123",
            #       "session_id": "session_456",       # 可选，None 时下游创建新 session
            #       "content": <any>,                  # 消息内容（路由层不校验内部结构）
            #       "source_channel_id": "wechat_xxx"  # 可能在 payload 内
            #     }
            #   }
            #
            # 典型场景：
            #   - 用户在企微发消息 → WeComBridge → Relay → Agent
            #   - 小智设备发消息 → Relay → Agent
            #
            # 处理流程：
            #   1. 校验 msg_id（必须存在，用于去重和 ACK）
            #   2. 回 ACK（告诉 Relay "我已收到，不用重发"）
            #   3. 触发入站回调（→ InboundPipeline.handle → Adapter.normalize → Dispatcher.dispatch）
            # 校验 message 帧必须包含 msg_id
            msg_id = frame.get("msg_id")
            if not msg_id:
                logger.warning("Received message frame without msg_id, skipping: %s", frame)
                return

            # ══════════════════════════════════════════════════════════════════
            # 回 ACK（At-Least-Once 投递保证）
            # ══════════════════════════════════════════════════════════════════
            # Relay 收到 ACK 后，不会再次推送这条消息。
            # 如果 Relay 没收到 ACK → 会重发 → 去重窗口会过滤重复消息。
            await self._send_ack(msg_id)

            # ══════════════════════════════════════════════════════════════════
            # 触发入站回调（异步执行，不阻塞接收循环）
            # ══════════════════════════════════════════════════════════════════
            # _inbound_handler 由装配层注册，
            # 实际指向 InboundPipeline.handle（→ GatewayInboundAdapter.normalize）。
            # 使用 create_task 异步执行（不阻塞 _receive_loop 接收下一条消息）。
            if self._inbound_handler:
                asyncio.create_task(self._safe_inbound_dispatch(frame))

        elif frame_type == "offline_batch":
            # ══════════════════════════════════════════════════════════════════
            # Relay 推送的离线消息批次
            # ══════════════════════════════════════════════════════════════════
            # 帧结构：
            #   {
            #     "type": "offline_batch",
            #     "messages": [
            #       {"type": "message", "msg_id": "...", "payload": {...}},
            #       {"type": "message", "msg_id": "...", "payload": {...}},
            #       ...
            #     ]
            #   }
            #   - messages: message 帧数组（每个元素结构同 type="message"）
            #
            # 场景：Agent 断线期间，用户发了消息 → Relay 缓存
            #        Agent 重连后，Relay 推送离线消息批次。
            # 处理：逐条调用 _on_message_received()（模拟实时接收）
            await self._receive_offline_batch(frame.get("messages", []))

        else:
            # ══════════════════════════════════════════════════════════════════
            # 未知消息类型 → 警告并忽略
            # ══════════════════════════════════════════════════════════════════
            logger.warning(
                "Received unknown frame type '%s', ignoring: %s",
                frame_type, frame,
            )

    def _on_ack_received(self, msg_id: str) -> None:
        """收到 ACK，从 PendingAck 表移除。"""
        if msg_id in self._pending_ack:
            del self._pending_ack[msg_id]

    async def _safe_inbound_dispatch(self, frame: dict[str, Any]) -> None:
        """安全地执行入站回调（捕获异常，不中断接收循环）。"""
        try:
            await self._inbound_handler(frame)  # type: ignore
        except Exception as e:
            logger.error(
                "Inbound handler error (msg_id=%s): %s",
                frame.get("msg_id", "?"), e,
            )

    async def _safe_on_send_failed_callback(
        self, msg_id: str, frame: dict[str, Any]
    ) -> None:
        """安全地执行 on_send_failed 回调（捕获异常，不中断其他任务）。

        设计说明：
        - 使用 create_task 异步执行时，回调内部的异常不会在调用处抛出
        - 因此需要一个包装协程来捕获异常，避免任务失败导致警告
        """
        if not self._on_send_failed_callback:
            return
        try:
            await self._on_send_failed_callback(msg_id, frame)
        except Exception as e:
            logger.warning("on_send_failed callback error: %s", e)

    async def _safe_on_auth_failed_callback(self) -> None:
        """安全地执行 on_auth_failed 回调（捕获异常，不中断其他任务）。

        设计说明：
        - 使用 create_task 异步执行时，回调内部的异常不会在调用处抛出
        - 因此需要一个包装协程来捕获异常，避免任务失败导致警告
        """
        if not self._on_auth_failed_callback:
            return
        try:
            await self._on_auth_failed_callback()
        except Exception as e:
            logger.warning("on_auth_failed callback error: %s", e)

    async def _safe_reconnect_with_backoff(self) -> None:
        """安全地执行重连逻辑（捕获异常，不中断其他任务）。"""
        try:
            await self._reconnect_with_backoff()
        except Exception as e:
            logger.error("Unexpected error in reconnect_with_backoff: %s", e)
            self._reconnecting = False  # 确保释放锁

    async def _safe_do_force_reconnect(self) -> None:
        """安全地执行强制重连（捕获异常，不中断其他任务）。"""
        try:
            await self._do_force_reconnect()
        except Exception as e:
            logger.error("Unexpected error in _do_force_reconnect: %s", e)

    async def _send_ack(self, msg_id: str) -> None:
        """向 Relay 发送 ACK 帧。"""
        ack_frame = {"type": "ack", "msg_id": msg_id}
        try:
            await self._ws_send(ack_frame)
        except Exception:
            pass  # ACK 发送失败不致命

    # ──────────────────────────────────────────────
    # Internal: Ping/Pong Heartbeat
    # ──────────────────────────────────────────────

    async def _ping_loop(self) -> None:
        """后台 Ping 发送 + 超时检测循环。

        核心职责：
        1. 每隔 ping_interval_s 发送应用层 {"type":"ping", "ts":<毫秒>}
        2. 检查 _last_activity 是否超过 ping_timeout_s
           （不只看 pong，任何帧都算活性证据）
        3. 超时则判定连接死亡 → 主动关闭 ws → 触发重连

        心跳策略（应用层 TEXT 帧）：
        - 为什么不用 RFC 6455 Control Frame？
          Nginx 不转发跨跳 Control Frame → 客户端收不到 Pong → 误判死亡
        - 解决方案：
          禁用 websockets 的 ping_interval（不发 Control Ping）
          改用应用层 TEXT 帧 {"type":"ping"}/{"type":"pong"}
          TEXT 帧可正常穿越 Nginx（它是普通消息，不是 Control Frame）

        检测逻辑：
        - 收到任何帧（message/ack/pong/offline_batch）→ 刷新 _last_activity
        - 超过 ping_timeout_s 未收到任何帧 → 判定死亡 → 触发重连
        """
        # ════════════════════════════════════════════════════════════════
        # 主循环：每隔 ping_interval_s 执行一次
        # ════════════════════════════════════════════════════════════════
        while not self._shutdown_requested:
            # 等待 ping_interval_s（默认 20s）
            await asyncio.sleep(self._config.ping_interval_s)

            # 检查是否请求关闭（SIGINT/SIGTERM）
            if self._shutdown_requested:
                break

            # ════════════════════════════════════════════════════════════════
            # Step 1: 发送应用层 Ping
            # ════════════════════════════════════════════════════════════════
            # 帧格式：{"type": "ping", "ts": <毫秒时间戳>}
            #   - type: 标识这是 ping 帧
            #   - ts: 毫秒时间戳（用于 RTT 计算，当前未使用）
            #
            # 发送失败的可能原因：
            #   - WebSocket 已断开（网络中断）
            #   - WebSocket 正在关闭
            # 发送失败 → break → _receive_loop 会检测到 ws 关闭 → 触发重连
            try:
                await self._ws_send({
                    "type": "ping",
                    "ts": int(datetime.now(timezone.utc).timestamp() * 1000),
                })
                # 记录 ping 发送时间（用于可观测性）
                self._last_ping_sent = datetime.now(timezone.utc)
            except Exception:
                # 发送失败说明 ws 已断
                # 不需要在这里触发重连，_receive_loop 会检测到 ws 关闭并触发重连
                break

            # ════════════════════════════════════════════════════════════════
            # Step 2: 检查活性超时
            # ════════════════════════════════════════════════════════════════
            # 关键设计：基于 _last_activity，不只是 _last_pong_received
            #   - _last_activity 在收到任何帧时刷新：
            #     * message（入站消息）
            #     * ack（Relay 收到我们发的消息）
            #     * pong（Relay 对我们 ping 的回复）
            #     * offline_batch（离线消息批次）
            #   - 只要有任何帧流动，就证明连接存活
            #
            # 超时判定：
            #   - elapsed > ping_timeout_s（默认 45s）→ 连接死亡
            #   - 触发重连（主动关闭 ws → 状态设为 DISCONNECTED → 创建重连任务）
            if self._last_activity is not None:
                elapsed = (datetime.now(timezone.utc) - self._last_activity).total_seconds()
                if elapsed > self._config.ping_timeout_s:
                    logger.warning(
                        "Connection inactive: %.0fs since last activity (timeout=%.0fs), triggering reconnect",
                        elapsed, self._config.ping_timeout_s,
                    )
                    # ════════════════════════════════════════════════════════════════
                    # 主动断开并触发重连
                    # ════════════════════════════════════════════════════════════════
                    # 1. 主动关闭 ws（发送 close frame 给 Relay）
                    # 2. 更新状态为 DISCONNECTED
                    # 3. 创建重连任务（指数退避）
                    if self._ws:
                        try:
                            await self._ws.close()
                        except Exception:
                            pass
                    self._conn_state = ConnectionState.DISCONNECTED
                    asyncio.create_task(self._reconnect_with_backoff())
                    break  # 退出 ping_loop（重连成功后会启动新的 ping_loop）


    # ──────────────────────────────────────────────
    # Internal: ACK Timeout Check
    # ──────────────────────────────────────────────

    async def _check_pending_ack_timeouts(self) -> None:
        """后台 ACK 超时检查协程（每 1s 轮询）。

        消息流向与 ACK 机制：
        ┌─────────┐  outbound message   ┌───────┐
        │  Agent  │  ──────────────────►  │ Relay │
        │(Gateway)│  ◄──────────────────  │       │
        └─────────┘      ACK frame        └───────┘
        - Agent 发送出站消息给 Relay
        - Relay 收到消息后，回 ACK 给 Agent
        - 如果 Agent 超时未收到 ACK → 认为 Relay 未收到 → 重试

        核心职责：
        1. 每隔 1s 检查 PendingAck 表（存储"已发送、待确认"的消息）
        2. 超时未收到 ACK → 重新发送消息给 Relay（最多 max_ack_retries 次）
        3. 超过重试次数 → 从 PendingAck 移除 → 触发 on_send_failed 回调

        At-Least-Once 投递保证：
        - 消息发送后记入 PendingAck（等待 Relay 的 ACK）
        - 收到 ACK → 从 PendingAck 移除（_on_ack_received）
        - 超时未收到 ACK → 重试发送
        - 超过重试次数 → 通知上层"消息最终失败"

        重试策略：
        - ack_timeout_s（默认 15s）：每次发送后等待 ACK 的超时时间
        - max_ack_retries（默认 3 次）：最大重试次数
        - 重试间隔：1s（本协程的轮询间隔）
        """
        # ════════════════════════════════════════════════════════════════════════════
        # 主循环：每隔 1s 检查一次
        # ════════════════════════════════════════════════════════════════════════════
        while not self._shutdown_requested:
            await asyncio.sleep(1.0)

            if self._shutdown_requested:
                break

            now = datetime.now(timezone.utc)
            expired_ids = []

            # ════════════════════════════════════════════════════════════════════════════
            # 遍历 PendingAck 表，检查是否超时
            # ════════════════════════════════════════════════════════════════════════════
            # PendingAck 表结构：
            #   {msg_id: PendingAckEntry(sent_at, attempts, frame)}
            #
            # 超时判定：
            #   (now - entry.sent_at) > ack_timeout_s
            #
            # 处理方式：
            #   - 未超过最大重试：重试（attempts += 1，重新发送）
            #   - 超过最大重试：记录到 expired_ids（后续统一处理）
            for msg_id, entry in list(self._pending_ack.items()):
                elapsed = (now - entry.sent_at).total_seconds()
                if elapsed > self._config.ack_timeout_s:
                    if entry.attempts < self._config.max_ack_retries:
                        # ════════════════════════════════════════════════════════════════════════════
                        # 重试：重新发送消息
                        # ════════════════════════════════════════════════════════════════════════════
                        # 更新重试次数和发送时间
                        entry.attempts += 1
                        entry.sent_at = now
                        logger.warning(
                            "ACK retry: msg_id=%s, attempt=%d/%d",
                            msg_id, entry.attempts, self._config.max_ack_retries,
                        )
                        # 重新发送（可能失败，失败则不记入 PendingAck）
                        try:
                            await self._ws_send(entry.frame)
                            # 重新发送成功 → 继续等待 ACK（不移除，下次循环继续检查）
                        except Exception:
                            pass  # 发送失败 → 下次循环继续重试
                    else:
                        # ════════════════════════════════════════════════════════════════════════════
                        # 超过最大重试次数 → 标记为重送失败
                        # ════════════════════════════════════════════════════════════════════════════
                        expired_ids.append(msg_id)
                        logger.error(
                            "ACK failed after %d attempts: msg_id=%s",
                            self._config.max_ack_retries, msg_id,
                        )

            # ════════════════════════════════════════════════════════════════════════════
            # 处理超过重试次数的消息
            # ════════════════════════════════════════════════════════════════════════════
            # 从 PendingAck 移除 → 触发 on_send_failed 回调
            #   → Broadcast 收到回调后，可以通知用户"消息发送失败"
            for msg_id in expired_ids:
                # 竞态保护：检查 msg_id 是否还在 _pending_ack 中
                # （_receive_loop 可能已通过 _on_ack_received 删除）
                if msg_id not in self._pending_ack:
                    continue

                frame = self._pending_ack[msg_id].frame
                del self._pending_ack[msg_id]
                # 触发 on_send_failed 回调（通知广播层消息最终失败）
                if self._on_send_failed_callback:
                    asyncio.create_task(
                        self._safe_on_send_failed_callback(msg_id, frame)
                    )

    # ──────────────────────────────────────────────
    # Internal: Reconnect
    # ──────────────────────────────────────────────

    async def _reconnect_with_backoff(self) -> None:
        """指数退避重连。

        核心职责：
        1. 防止并发重连（_reconnecting 锁）
        2. 停止旧的后台任务（防止 task 泄漏和双重重连）
        3. 指数退避等待（1s → 2s → 4s → ... → reconnect_max_delay_s）
        4. 循环尝试连接，直到成功或 shutdown

        退避策略：
        - 第 1 次重连：等待 1s（2^0）
        - 第 2 次重连：等待 2s（2^1）
        - 第 3 次重连：等待 4s（2^2）
        - ...
        - 第 N 次重连：等待 min(2^(N-1), reconnect_max_delay_s)
        - 上限：reconnect_max_delay_s（默认 60s）

        并发保护：
        - _reconnecting 锁：同一时间只有一个重连协程在运行
        - _stop_background_tasks()：重连前先停止旧的后台任务，
          防止多个 _ping_loop / _receive_loop 并发踩踏状态
        """
        # ════════════════════════════════════════════════════════════════════
        # 并发保护：防止多个重连协程同时运行
        # ════════════════════════════════════════════════════════════════════
        if self._reconnecting or self._shutdown_requested:
            return  # 已有重连在进行中，或请求关闭

        # 设置重连锁（防止并发重连）
        self._reconnecting = True
        self._conn_state = ConnectionState.RECONNECTING

        try:

            # ════════════════════════════════════════════════════════════════════
            # 关键：先停止所有旧的后台任务
            # ════════════════════════════════════════════════════════════════════
            # 问题 1：task 泄漏
            #   - 旧连接的后台任务（_ping_loop / _receive_loop）还在运行
            #   - _connect() 创建新任务，但不 cancel 旧的
            #   - 多次重连后，会有多个 _ping_loop / _receive_loop 并发运行
            #   - 它们会同时操作 self._ws / self._pending_ack 等状态 → 数据竞争
            #
            # 问题 2：双重重连
            #   - 旧 _receive_loop 检测到 ws 关闭 → 触发 _reconnect_with_backoff
            #   - 旧 _ping_loop 检测到心跳超时 → 也触发 _reconnect_with_backoff
            #   - 两个重连任务同时运行：
            #     * 任务 A 成功重连 → 释放 _reconnecting 锁
            #     * 任务 B 立即启动 → 再次重连 → 刚建好的连接被顶掉
            #
            # 解决：重连前先 stop 旧任务 → 确保只有一个重连任务在操作状态
            await self._stop_background_tasks()

            # ════════════════════════════════════════════════════════════════════
            # 重连主循环：指数退避等待 → 尝试连接 → 成功则返回
            # ════════════════════════════════════════════════════════════════════
            while not self._shutdown_requested:
                # ─ 计算退避时间 ─
                # 公式：min(2^(attempts), reconnect_max_delay_s)
                #   - 第 1 次：min(2^0, 60) = 1s
                #   - 第 2 次：min(2^1, 60) = 2s
                #   - 第 3 次：min(2^2, 60) = 4s
                #   - 第 7 次：min(2^6, 60) = 64 → 60s（达到上限）
                #   - 第 N 次：60s（保持上限）
                wait_time = min(
                    2 ** self._reconnect_attempts,
                    self._config.reconnect_max_delay_s,
                )
                # 递增重连计数器（用于下次计算退避时间）
                self._reconnect_attempts += 1

                # ─ 日志：即将重连 ─
                logger.info(
                    "Reconnecting in %.0fs (attempt %d)...",
                    wait_time, self._reconnect_attempts,
                )

                # ─ 等待退避时间 ─
                await asyncio.sleep(wait_time)

                # ─ 检查是否请求关闭（等待期间可能收到 SIGINT/SIGTERM）──
                if self._shutdown_requested:
                    break  # 退出重连循环

                # ════════════════════════════════════════════════════════════════════
                # 尝试连接
                # ════════════════════════════════════════════════════════════════════
                # _triggered_by_reconnect=True：
                #   - 告诉 _connect() 这是重连触发
                #   - 连接失败时，_connect() 不会创建新的重连任务
                #   - 重连循环由本方法控制（外层 while 循环）
                attempts_before = self._reconnect_attempts
                await self._connect(_triggered_by_reconnect=True)

                # ─ 检查是否连接成功 ─
                if self._conn_state == ConnectionState.CONNECTED:
                    # 连接成功 → 释放重连锁 → 返回
                    self._reconnecting = False
                    logger.info(
                        "Reconnected after %d attempt(s)",
                        attempts_before,
                    )
                    return  # 退出重连循环（新的后台任务已在 _connect() 中启动）

            # ════════════════════════════════════════════════════════════════════
            # 重连循环退出（shutdown 或失败）
            # ════════════════════════════════════════════════════════════════════
            self._reconnecting = False  # 释放重连锁

        except Exception as e:
            # 捕获所有未处理的异常（防御性编程）
            logger.error("Unexpected error in _reconnect_with_backoff: %s", e)
            self._reconnecting = False  # 确保释放锁

    # ──────────────────────────────────────────────
    # Internal: Offline Batch
    # ──────────────────────────────────────────────

    async def _receive_offline_batch(self, messages: list[dict]) -> None:
        """处理离线消息批次。"""
        self._pending_offline_msgs = len(messages)
        logger.info("Offline batch received: %d messages", len(messages))

        for frame in messages:
            await self._on_message_received(json.dumps(frame))
            self._pending_offline_msgs -= 1

        self._pending_offline_msgs = 0

    # ──────────────────────────────────────────────
    # Internal: Outbound Queue
    # ──────────────────────────────────────────────

    def _enqueue_outbound(self, frame: dict[str, Any]) -> None:
        """断线时暂存到 OutboundQueue（FIFO 有界队列）。"""
        self._outbound_queue.append(frame)

        if len(self._outbound_queue) > self._config.outbound_queue_max_size:
            evicted = self._outbound_queue.popleft()
            logger.warning(
                "Outbound queue overflow: evicted msg_id=%s, size=%d",
                evicted.get("msg_id", "?"),
                len(self._outbound_queue),
            )

    async def _flush_outbound_queue(self) -> None:
        """重连后补发暂存消息。"""
        count = len(self._outbound_queue)
        if count == 0:
            return

        logger.info("Flushing outbound queue: %d messages", count)
        while self._outbound_queue:
            frame = self._outbound_queue.popleft()
            await self.send_message_frame(frame)

    # ──────────────────────────────────────────────
    # Internal: WebSocket Send
    # ──────────────────────────────────────────────

    async def _ws_send(self, data: dict[str, Any]) -> None:
        """通过 WebSocket 发送 JSON 帧（出站消息）。

        消息流向：
        ┌─────────┐  outbound message  ┌───────┐  ──→  用户/设备
        │  Agent  │  ────────────────► │ Relay │
        │(Gateway)│  ◄────────────────  │       │  ◄── （如企微、小智设备）
        └─────────┘       ACK frame     └───────┘
        - Agent 发送出站消息给 Relay
        - Relay 收到后转发给目标渠道（target_channel_ids）
        - Relay 回 ACK 给 Agent（表示已收到）

        出站帧结构（由 Broadcaster._build_frame /Broadcaster._build_frame_raw()构建）：
        {
          "type": "message",                        # 固定为 "message"
          "msg_id": "msg_abc123",                  # 消息唯一 ID (UUID v4)
          "event_type": "agent_reply",               # 事件类型（EventType.value 字符串）
          "payload": {                              # 消息内容（dict，不是 bytes）
            "user_id": "user_123",
            "content": "你好，世界",
            "content_type": "text",
            "status": "success",
            "timestamp": "2026-05-24T19:30:00+00:00",
            "reply_id": "r1",
          },
          "target_channel_ids": ["wechat_xxx"],    # 目标渠道 ID 列表
          "origin_channel_id": "desktop_ipc",       # 来源渠道 ID（可选，None 时不传）
        }

        使用 asyncio.Lock 保护，防止多协程并发写入导致帧交错。

        帧结构约定（2026-05-24 统一）：
        - frame["payload"] 在进入本方法前已经是 dict（由 Broadcaster._build_frame
          / _build_frame_raw 中的 _payload_to_dict 保证）。
        - 本方法只做 json.dumps → WebSocket send，不做 bytes→dict 隐式转换。
        - _bytes_fallback 仅作安全兜底：万一仍有 bytes 字段残留（如 PendingAck
          重发历史帧），将其转为 UTF-8 字符串而非嵌套 dict。
        """
        if self._ws is None:
            raise ConnectionError("WebSocket not connected")

        def _bytes_fallback(obj: Any) -> Any:
            """安全兜底：将 bytes 转为字符串，不做 json.loads（避免嵌套）。"""
            if isinstance(obj, bytes):
                return obj.decode("utf-8")
            raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

        async with self._ws_lock:
            await self._ws.send(json.dumps(data, default=_bytes_fallback))

    # ──────────────────────────────────────────────
    # Internal: JWT
    # ──────────────────────────────────────────────

    def _extract_user_id_from_token(self) -> str:
        """从 JWT token 解析 user_id（不验证签名，仅提取 claims）。"""
        try:
            import jwt as pyjwt
            # 不验证签名（Gateway 不持有 secret，只提取 claims）
            payload = pyjwt.decode(
                self._jwt_token, options={"verify_signature": False}
            )
            return payload.get("user_id", "")
        except Exception:
            # user_id 属 ID 类：JWT 解析失败回落空串（下游仅作连接 label），绝不静默（§九）。
            from pandapal.degradation import DegradationEvent, report_degradation
            report_degradation(
                DegradationEvent.JWT_USER_ID_PARSE_FAILED,
                category="id", source="gateway.extract_user_id",
                fallback="", exc_info=True,
            )
            return ""
