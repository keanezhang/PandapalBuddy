"""Gateway 测试（无真实 WebSocket 连接）。

测试关注：
- 构造参数校验
- 状态管理
- OutboundQueue（断线暂存 + FIFO 驱逐）
- PendingAck 逻辑
- 回调注册
- Token 热更新
- Ping/Pong 心跳状态
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pandapal.gateway.gateway import Gateway
from pandapal.gateway.models import (
    AgentConnectionStatus,
    ConnectionState,
    GatewayConfig,
    PendingAckEntry,
)


# ──────────────────────────────────────────────
# Construction Tests
# ──────────────────────────────────────────────


def test_construct_valid():
    """有效参数可构造。"""
    gw = Gateway(
        relay_url="wss://relay.example.com/ws",
        jwt_token="valid.jwt.token",
    )
    assert gw._conn_state == ConnectionState.DISCONNECTED


def test_construct_empty_relay_url_raises():
    """空 relay_url 抛出 ValueError。"""
    with pytest.raises(ValueError, match="relay_url"):
        Gateway(relay_url="", jwt_token="token")


def test_construct_empty_jwt_raises():
    """空 jwt_token 抛出 ValueError。"""
    with pytest.raises(ValueError, match="jwt_token"):
        Gateway(relay_url="wss://relay.com/ws", jwt_token="")


# ──────────────────────────────────────────────
# State Tests
# ──────────────────────────────────────────────


def test_initial_state():
    """初始状态为 DISCONNECTED。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t")
    status = gw.get_connection_state()
    assert status.conn_state == ConnectionState.DISCONNECTED
    assert status.reconnect_attempts == 0
    assert status.outbound_queue_size == 0
    assert status.pending_ack_count == 0


def test_initial_connection_state():
    """初始状态为 DISCONNECTED。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t")
    assert gw.get_connection_state().conn_state == ConnectionState.DISCONNECTED


def test_initial_ping_pong_state():
    """初始 Ping/Pong 时间戳为 None。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t")
    status = gw.get_connection_state()
    assert status.last_ping_sent is None
    assert status.last_pong_received is None
    assert status.last_activity is None


# ──────────────────────────────────────────────
# OutboundQueue Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_when_disconnected_enqueues():
    """断线时发送消息暂存到 OutboundQueue。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t")
    # 未连接状态下发送
    await gw.send_message_frame({"msg_id": "m1", "payload": b"hello"})

    status = gw.get_connection_state()
    assert status.outbound_queue_size == 1


@pytest.mark.asyncio
async def test_outbound_queue_fifo_eviction():
    """OutboundQueue 超容量时 FIFO 驱逐最旧。"""
    config = GatewayConfig(outbound_queue_max_size=3)
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t", config=config)

    for i in range(5):
        await gw.send_message_frame({"msg_id": f"m{i}", "payload": b"data"})

    status = gw.get_connection_state()
    # 最多 3 条（配置的 max_size）
    assert status.outbound_queue_size == 3
    # 最旧的 m0, m1 被驱逐
    remaining_ids = [f.get("msg_id") for f in gw._outbound_queue]
    assert "m0" not in remaining_ids
    assert "m1" not in remaining_ids
    assert "m4" in remaining_ids


# ──────────────────────────────────────────────
# PendingAck Tests
# ──────────────────────────────────────────────


def test_on_ack_received_removes_entry():
    """收到 ACK 从 PendingAck 移除。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t")
    # 手动添加一条 pending
    gw._pending_ack["m1"] = PendingAckEntry(
        sent_at=datetime.now(timezone.utc),
        attempts=0,
        frame={"msg_id": "m1"},
    )
    assert len(gw._pending_ack) == 1

    gw._on_ack_received("m1")
    assert len(gw._pending_ack) == 0


def test_on_ack_received_nonexistent_is_noop():
    """收到不存在的 ACK 是无操作（幂等）。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t")
    gw._on_ack_received("nonexistent")  # 不应抛异常


# ──────────────────────────────────────────────
# Message Dispatch Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_message_received_updates_last_activity():
    """收到任何有效帧都会刷新 _last_activity。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t")
    assert gw._last_activity is None

    # 模拟收到 pong 帧
    import json
    await gw._on_message_received(json.dumps({"type": "pong"}))
    assert gw._last_activity is not None

    # 模拟收到 ack 帧
    old_activity = gw._last_activity
    import asyncio
    await asyncio.sleep(0.01)
    await gw._on_message_received(json.dumps({"type": "ack", "msg_id": "m1"}))
    assert gw._last_activity >= old_activity


@pytest.mark.asyncio
async def test_on_pong_received_updates_last_pong():
    """收到 pong 帧更新 _last_pong_received。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t")
    assert gw._last_pong_received is None

    import json
    await gw._on_message_received(json.dumps({"type": "pong"}))
    assert gw._last_pong_received is not None


@pytest.mark.asyncio
async def test_invalid_json_does_not_crash():
    """无效 JSON 不崩溃。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t")
    await gw._on_message_received("not valid json {{")
    # 不抛异常即为通过


# ──────────────────────────────────────────────
# Callback Tests
# ──────────────────────────────────────────────


def test_register_inbound_handler():
    """注册入站处理器。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t")

    async def handler(frame):
        pass

    gw.register_inbound_handler(handler)
    assert gw._inbound_handler is handler


def test_register_on_auth_failed_callback():
    """注册认证失败回调。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t")

    async def callback():
        pass

    gw.register_on_auth_failed_callback(callback)
    assert gw._on_auth_failed_callback is callback


# ──────────────────────────────────────────────
# Token Tests
# ──────────────────────────────────────────────


def test_update_jwt_token():
    """热更新 JWT Token。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="old_token")
    gw.update_jwt_token("new_token")
    assert gw._jwt_token == "new_token"


# ──────────────────────────────────────────────
# Config Tests
# ──────────────────────────────────────────────


def test_default_config_ping_pong():
    """默认配置使用 ping/pong 参数名。"""
    config = GatewayConfig()
    assert config.ping_interval_s == 20.0
    assert config.ping_timeout_s == 45.0
    assert config.ack_timeout_s == 15.0
    assert config.max_ack_retries == 3


def test_custom_config():
    """自定义配置。"""
    config = GatewayConfig(
        ping_interval_s=15.0,
        ping_timeout_s=30.0,
        ack_timeout_s=10.0,
        max_ack_retries=5,
    )
    assert config.ping_interval_s == 15.0
    assert config.ping_timeout_s == 30.0


# ──────────────────────────────────────────────
# Close Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_clears_state():
    """关闭连接清理所有状态。"""
    gw = Gateway(relay_url="wss://r.com/ws", jwt_token="t")
    # 模拟一些状态
    gw._pending_ack["m1"] = PendingAckEntry(
        sent_at=datetime.now(timezone.utc), attempts=0, frame={}
    )
    gw._outbound_queue.append({"msg_id": "m2"})

    await gw.close_relay_connection("test")

    assert gw._conn_state == ConnectionState.DISCONNECTED
    assert len(gw._pending_ack) == 0
    assert len(gw._outbound_queue) == 0
