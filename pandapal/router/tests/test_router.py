"""MessageRouter 测试。"""

from __future__ import annotations

import pytest

from pandapal.router.models import (
    InboundMessage,
    RouterPermissionError,
)
from pandapal.router.router import MessageRouter


# ──────────────────────────────────────────────
# Registration Tests
# ──────────────────────────────────────────────


def test_register_handler():
    """注册 handler 成功。"""
    router = MessageRouter()

    async def handler(_):
        pass

    router.register_route_handler("user_instruction", handler)
    assert "user_instruction" in router.get_registered_message_types()


def test_register_invalid_type_raises():
    """无效 message_type 格式抛出 ValueError。"""
    router = MessageRouter()

    async def handler(_):
        pass

    with pytest.raises(ValueError, match="Invalid message_type"):
        router.register_route_handler("INVALID-TYPE!", handler)


@pytest.mark.asyncio
async def test_register_duplicate_overwrites():
    """重复注册同一 type 覆盖旧 handler，后注册的 handler 生效。"""
    router = MessageRouter()
    calls = []

    async def handler1(_msg):
        calls.append("h1")

    async def handler2(_msg):
        calls.append("h2")

    router.register_route_handler("user_instruction", handler1)
    router.register_route_handler("user_instruction", handler2)
    assert len(router.get_registered_message_types()) == 1

    # 发送一条消息，确认是 handler2（后注册者）被调用，handler1 已被覆盖
    msg = InboundMessage(
        msg_id="dup-test-1",
        message_type="user_instruction",
        source_channel_id="__desktop_ipc__",  # v1.1: inject 必须从白名单短路径走
        user_id="u1",
    )
    await router.inject_inbound_message(msg)
    assert calls == ["h2"], f"Expected only handler2 to fire, got: {calls}"


# ──────────────────────────────────────────────
# Inject (CLI 短路径) Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inject_inbound_message():
    """CLI 本地注入消息直接路由到 handler。"""
    router = MessageRouter()
    received = []

    async def handler(msg: InboundMessage):
        received.append(msg)

    router.register_route_handler("task_instruction", handler)

    msg = InboundMessage(
        msg_id="cli-1",
        message_type="task_instruction",
        source_channel_id="__desktop_ipc__",
        user_id="local_user",
        content={"command": "status"},
    )
    await router.inject_inbound_message(msg)

    assert len(received) == 1
    assert received[0].content == {"command": "status"}


@pytest.mark.asyncio
async def test_inject_dedup():
    """注入的消息也受去重保护。"""
    router = MessageRouter()
    calls = []

    async def handler(_):
        calls.append(1)

    router.register_route_handler("task_instruction", handler)

    msg = InboundMessage(
        msg_id="same-id",
        message_type="task_instruction",
        source_channel_id="__desktop_ipc__",
        user_id="u1",
    )
    await router.inject_inbound_message(msg)
    await router.inject_inbound_message(msg)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_inject_rejects_non_whitelist_channel():
    """v1.1: inject_inbound_message 必须强校验 source_channel_id 在 channel_ids 白名单内。

    白名单（与 broadcast.channel_ids 一致）：
      - 本地虚拟：__desktop_ipc__ / __hitl_bridge__ / __scheduler__
      - 远程：wecom 精确 / xiaozhi:* 前缀

    背景：附录 D04-3 — 防止 inject 被误用为权限旁路（伪造 user_id 绕过 Gateway 鉴权）。
    """
    router = MessageRouter()

    async def handler(_):
        pass

    router.register_route_handler("user_instruction", handler)

    # 伪造的 channel_id（不在白名单）应被拒绝
    fake_msg = InboundMessage(
        msg_id="fake-1",
        message_type="user_instruction",
        source_channel_id="fake_channel_xxx",  # ❌ 不在白名单
        user_id="attacker_user",
    )
    with pytest.raises(RouterPermissionError, match="not in channel_ids whitelist"):
        await router.inject_inbound_message(fake_msg)


@pytest.mark.asyncio
async def test_inject_accepts_local_whitelist_channels():
    """v1.1: inject 接受三个本地虚拟渠道（Desktop IPC / HITL Bridge / Scheduler）。"""
    router = MessageRouter()
    received = []

    async def handler(msg):
        received.append(msg.source_channel_id)

    router.register_route_handler("user_instruction", handler)

    for ch_id in ("__desktop_ipc__", "__hitl_bridge__", "__scheduler__"):
        msg = InboundMessage(
            msg_id=f"local-{ch_id}",
            message_type="user_instruction",
            source_channel_id=ch_id,
            user_id="u1",
        )
        await router.inject_inbound_message(msg)

    assert sorted(received) == sorted(["__desktop_ipc__", "__hitl_bridge__", "__scheduler__"])


@pytest.mark.asyncio
async def test_inject_accepts_remote_whitelist_channels():
    """v1.1: inject 接受远程白名单匹配的 channel_id（wecom 精确、xiaozhi:* 前缀）。

    背景：HITL Bridge / Scheduler 注入审批消息时会使用真实外部 channel_id。
    """
    router = MessageRouter()
    received = []

    async def handler(msg):
        received.append(msg.source_channel_id)

    router.register_route_handler("approval_response", handler)

    test_cases = [
        "wecom",                # 精确匹配
        "xiaozhi:device_001",   # 前缀匹配
        "xiaozhi:abc",
    ]
    for i, ch_id in enumerate(test_cases):
        msg = InboundMessage(
            msg_id=f"remote-{i}",
            message_type="approval_response",
            source_channel_id=ch_id,
            user_id="u1",
        )
        await router.inject_inbound_message(msg)

    assert sorted(received) == sorted(test_cases)
