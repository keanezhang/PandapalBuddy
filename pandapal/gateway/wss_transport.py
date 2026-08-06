"""WSSGateway — 把 NormalizedEvent 通过 WSS 发送到远端 Relay（5.2 重写版）。

★ 5.2 关键改造：
  - WSSGateway 是一个 Transport（实现 Transport 协议）
  - 内部复用原 Gateway 的 WSS plumbing（连接、心跳、重连、ACK、出站队列）
  - 上层 Broadcast 调 `await wss.send(event)`，WSSGateway 内部把 NormalizedEvent
    序列化为 JSON dict → 走原 Gateway 的 send_message_frame → 走 WSS → Relay

设计约束：
- BL1: 唯一职责 = 「NormalizedEvent → JSON dict → WSS 帧」+ 复用 WSS 连接管理
- HC3: 失败时内部消化，永不向上抛
- O3: send() 调用永不抛
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from pandapal.broadcast.transport import Transport
from pandapal.events.normalized import NormalizedEvent
from pandapal.gateway.gateway import Gateway

logger = logging.getLogger(__name__)


class WSSGateway(Transport):
    """WSS 渠道的 Transport 适配器（5.2 新版）。

    内部持有一个 Gateway 实例，复用其 WSS 连接/重连/ACK/OutboundQueue 全部能力；
    唯一新增的职责是把 NormalizedEvent 序列化为 JSON dict 并送进 WSS。

    使用方式：
        gateway = Gateway(relay_url=..., jwt_token=..., config=...)
        wss_transport = WSSGateway(gateway=gateway, default_channel_id="wecom")
        # 注册到 ChannelRegistry：
        registry.register(ChannelInfo(
            id="wecom", type=ChannelType.REMOTE,
            capabilities=frozenset({ChannelCapability.TEXT}),
            transport=wss_transport,
        ))
        # 上层 Broadcast 直接 await wss_transport.send(event)
    """

    def __init__(
        self,
        gateway: Gateway,
        default_channel_id: str = "wecom",
    ) -> None:
        if gateway is None:
            raise ValueError("gateway cannot be None")
        self._gateway = gateway
        self._default_channel_id = default_channel_id
        self._started = False

    # ──────────────────────────────────────────────
    # Transport Protocol
    # ──────────────────────────────────────────────

    @property
    def is_started(self) -> bool:
        """★ Transport 契约：start() 后为 True，stop() 后为 False。

        用于 PandaPalApp 启动自检 + 测试断言 + 幂等控制。
        """
        return self._started

    async def start(self) -> None:
        """建立 WSS 连接（懒连接，内部幂等）。"""
        if self._started:
            return
        try:
            await self._gateway.establish_relay_connection()
            self._started = True
            logger.info("WSSGateway started (channel_id=%s)", self._default_channel_id)
        except Exception as e:
            logger.warning("WSSGateway start failed (will run in offline mode): %s", e)
            # 不抛异常 —— 离线模式下 broadcast 仍可工作（仅本地渠道）

    async def stop(self) -> None:
        """关闭 WSS 连接。"""
        if not self._started:
            return
        try:
            await self._gateway.close_relay_connection(reason="shutdown")
        except Exception as e:
            logger.warning("WSSGateway stop error: %s", e)
        self._started = False
        logger.info("WSSGateway stopped")

    async def send(self, event: NormalizedEvent) -> None:
        """★ 主入口：把 NormalizedEvent 序列化为 WSS 帧并发送。

        帧结构：
            {
                "type": "message",
                "msg_id": <event.msg_id>,
                "event_type": <EventType.value>,   # ★ 5.2 新增：直接传 EventType
                "reply_id": <event.reply_id>,
                "run_id":   <event.run_id>,
                "origin_channel_id": <event.origin_channel_id>,
                "payload":  <event.payload>,
            }
        """
        # 1. 构造 WSS 帧（JSON dict）
        frame = self._to_wss_frame(event)
        # 2. 走原 Gateway 的 send_message_frame（At-Least-Once + 断线暂存）
        try:
            await self._gateway.send_message_frame(frame)
        except Exception as e:
            # O3: 永不向上抛异常
            logger.warning(
                "WSSGateway.send failed (event=%s): %s",
                event.event_type.value, e,
            )

    # ──────────────────────────────────────────────
    # WSS frame 序列化
    # ──────────────────────────────────────────────

    def _to_wss_frame(self, event: NormalizedEvent) -> dict[str, Any]:
        """NormalizedEvent → WSS JSON dict。

        ★ 5.2 关键变化（vs 旧协议）：
          - 不再用 envelope（type=message + payload 嵌套）
          - 字段直接平铺：event_type, reply_id, run_id, payload
          - msg_id 沿用 event.msg_id（去重/ACK 唯一键）
        """
        frame: dict[str, Any] = {
            "type": "message",                        # WSS 帧类型（必填）
            "msg_id": event.msg_id,                   # WSS ACK 唯一键
            "event_type": event.event_type.value,     # ★ 5.2 新增：NormalizedEvent 类型
            "payload": dict(event.payload),           # 业务负载
        }
        # 可选字段：仅在非 None 时写入（减少帧体积）
        if event.reply_id is not None:
            frame["reply_id"] = event.reply_id
        if event.run_id is not None:
            frame["run_id"] = event.run_id
        if event.origin_channel_id is not None:
            frame["origin_channel_id"] = event.origin_channel_id
        return frame

    # ──────────────────────────────────────────────
    # 适配器便捷方法
    # ──────────────────────────────────────────────

    def get_connection_state(self):
        """透传 Gateway 的连接状态（可观测性）。"""
        return self._gateway.get_connection_state()

    def update_jwt_token(self, new_token: str) -> None:
        """热更新 JWT（透传）。"""
        self._gateway.update_jwt_token(new_token)

    def register_inbound_handler(
        self, handler,
    ) -> None:
        """注册入站消息回调（透传到 Gateway）。"""
        self._gateway.register_inbound_handler(handler)

    @property
    def gateway(self) -> Gateway:
        """只读暴露内部 Gateway（供装配层注册 token 刷新回调，避免私访 _gateway）。"""
        return self._gateway

    def register_on_token_refreshed_callback(
        self, callback: Callable[[str], Awaitable[None]]
    ) -> None:
        """注册 token 刷新成功回调（透传到 Gateway，参数为新 token）。"""
        self._gateway.register_on_token_refreshed_callback(callback)

    def register_on_auth_expired_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> None:
        """注册认证彻底失效回调（透传到 Gateway）。"""
        self._gateway.register_on_auth_expired_callback(callback)
