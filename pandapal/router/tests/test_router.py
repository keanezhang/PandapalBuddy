"""MessageRouter 测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import pytest

from pandapal.router.models import (
    InboundMessage,
    RouterConfigError,
    RouterPermissionError,
    RouterStateError,
)
from pandapal.router.router import MessageRouter


# ──────────────────────────────────────────────
# Helper
# ──────────────────────────────────────────────


def _make_frame(
    msg_id: str = "m1",
    message_type: str = "user_instruction",
    user_id: str = "u1",
    content: str = "hello",
    source_channel_id: str = "ch1",
) -> dict:
    """构建一个模拟的 MessageFrame dict。"""
    payload = json.dumps({
        "msg_id": msg_id,
        "message_type": message_type,
        "user_id": user_id,
        "content": content,
    })
    return {
        "msg_id": msg_id,
        "payload": payload,
        "source_channel_id": source_channel_id,
    }


class MockGateway:
    """满足 GatewayProtocol 的最小 Mock 实现。

    attach_to_gateway 调用后，handler 属性持有已注册的入站回调。
    测试通过 await mock_gw.send(frame) 模拟 gateway 推送消息。
    """

    def __init__(self) -> None:
        self.handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    def register_inbound_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        self.handler = handler

    async def send(self, frame: dict[str, Any]) -> None:
        """模拟 gateway 向 router 推送消息帧。"""
        assert self.handler is not None, (
            "No handler registered — did you call router.attach_to_gateway()?"
        )
        await self.handler(frame)


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
    from pandapal.router.models import InboundMessage
    msg = InboundMessage(
        msg_id="dup-test-1",
        message_type="user_instruction",
        source_channel_id="__desktop_ipc__",  # v1.1: inject 必须从白名单短路径走
        user_id="u1",
    )
    await router.inject_inbound_message(msg)
    assert calls == ["h2"], f"Expected only handler2 to fire, got: {calls}"


# ──────────────────────────────────────────────
# Attach Tests
# ──────────────────────────────────────────────


def test_attach_empty_table_raises():
    """路由表为空时 attach 抛出 RouterConfigError（I1）。"""
    router = MessageRouter()
    with pytest.raises(RouterConfigError, match="No handlers registered"):
        router.attach_to_gateway(MockGateway())


def test_attach_registers_inbound_handler():
    """attach_to_gateway 必须向 gateway 注册入站回调。"""
    router = MessageRouter()

    async def handler(_):
        pass

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    assert mock_gw.handler is not None, (
        "attach_to_gateway must call gateway.register_inbound_handler"
    )


def test_double_attach_is_noop():
    """重复 attach 不报错（幂等），且 handler 仍正确注册。"""
    router = MessageRouter()

    async def handler(_):
        pass

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)
    router.attach_to_gateway(mock_gw)  # 第二次应无操作

    assert mock_gw.handler is not None


# ──────────────────────────────────────────────
# Dispatch Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_routes_to_handler():
    """消息成功路由到对应 handler。"""
    router = MessageRouter()
    received = []

    async def handler(msg: InboundMessage):
        received.append(msg)

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    frame = _make_frame()
    await mock_gw.send(frame)

    assert len(received) == 1
    assert received[0].msg_id == "m1"
    assert received[0].message_type == "user_instruction"
    assert received[0].user_id == "u1"


@pytest.mark.asyncio
async def test_dispatch_dedup():
    """同一 msg_id 只处理一次（I3 幂等去重）。"""
    router = MessageRouter()
    call_count = []

    async def handler(msg: InboundMessage):
        call_count.append(1)

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    frame = _make_frame(msg_id="dup1")
    await mock_gw.send(frame)
    await mock_gw.send(frame)
    await mock_gw.send(frame)

    assert len(call_count) == 1  # 只处理一次


@pytest.mark.asyncio
async def test_dispatch_unknown_type_discarded():
    """未知 message_type 静默丢弃（WARN 日志）。"""
    router = MessageRouter()

    async def handler(_):
        pass

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    frame = _make_frame(msg_id="m2", message_type="unknown_type")
    # 不应抛异常
    await mock_gw.send(frame)


@pytest.mark.asyncio
async def test_dispatch_invalid_json_discarded():
    """无效 JSON payload 静默丢弃。"""
    router = MessageRouter()

    async def handler(_):
        pass

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    frame = {
        "msg_id": "m3",
        "payload": "not valid json {{{",
        "source_channel_id": "ch1",
    }
    await mock_gw.send(frame)  # 不应抛异常


@pytest.mark.asyncio
async def test_dispatch_missing_required_field_discarded():
    """缺少必填字段的消息静默丢弃。"""
    router = MessageRouter()

    async def handler(_):
        pass

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    frame = {
        "msg_id": "m4",
        "payload": json.dumps({"msg_id": "m4"}),  # 缺 message_type 和 user_id
        "source_channel_id": "ch1",
    }
    await mock_gw.send(frame)  # 不应抛异常


@pytest.mark.asyncio
async def test_dispatch_handler_timeout():
    """handler 超时不阻塞路由器（I5）。"""
    router = MessageRouter(handler_timeout_seconds=0.1)

    async def slow_handler(msg):
        await asyncio.sleep(10)  # 模拟慢 handler

    router.register_route_handler("user_instruction", slow_handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    frame = _make_frame(msg_id="timeout1")
    # 不应阻塞，应在 0.1s 内超时
    await mock_gw.send(frame)


@pytest.mark.asyncio
async def test_dispatch_handler_exception_isolated():
    """handler 异常不影响路由器继续工作。"""
    router = MessageRouter()
    calls = []

    async def bad_handler(msg):
        raise RuntimeError("intentional error")

    async def good_handler(msg):
        calls.append(msg.msg_id)

    router.register_route_handler("user_instruction", bad_handler)
    router.register_route_handler("task_instruction", good_handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    # bad handler 异常
    await mock_gw.send(_make_frame(msg_id="bad1"))

    # good handler 仍能工作
    await mock_gw.send(_make_frame(msg_id="good1", message_type="task_instruction"))
    assert "good1" in calls


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


# ──────────────────────────────────────────────
# Dedup Window Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dedup_window_size():
    """去重窗口大小正确统计。"""
    router = MessageRouter()

    async def handler(_):
        pass

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    for i in range(5):
        await mock_gw.send(_make_frame(msg_id=f"m{i}"))

    assert router.get_dedup_window_size() == 5


@pytest.mark.asyncio
async def test_dedup_window_capacity_eviction():
    """去重窗口超容量时 FIFO 驱逐。"""
    router = MessageRouter(dedup_max_size=3)

    async def handler(_):
        pass

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    for i in range(5):
        await mock_gw.send(_make_frame(msg_id=f"m{i}"))

    # 最多保留 3 条
    assert router.get_dedup_window_size() <= 3


# ──────────────────────────────────────────────
# v1.1 新约束测试（依据 04 文档 v1.1）
# ──────────────────────────────────────────────


def test_register_after_attach_raises_state_error():
    """v1.1: attach_to_gateway 之后再调用 register_route_handler 必须抛 RouterStateError。

    背景：附录 D04 失败情况 9 — 运行时动态注册会与并发执行的 _dispatch 产生竞态。
    """
    router = MessageRouter()

    async def handler(_):
        pass

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    # attach 之后再注册，必须 raise
    async def another_handler(_):
        pass

    with pytest.raises(RouterStateError, match="must be called before attach_to_gateway"):
        router.register_route_handler("task_instruction", another_handler)


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


@pytest.mark.asyncio
async def test_payload_oversize_discarded():
    """v1.1: payload 超过 _max_payload_bytes 时静默丢弃（PayloadTooLargeError 被路由层捕获）。

    背景：失败情况 8 — 防御 OOM/内存放大攻击。
    """
    # 设一个很小的上限（200 字节）便于测试
    router = MessageRouter(max_payload_bytes=200)
    received = []

    async def handler(msg):
        received.append(msg)

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    # 构造一个超过上限的 payload（content 字段塞满）
    big_payload = json.dumps({
        "msg_id": "big1",
        "message_type": "user_instruction",
        "user_id": "u1",
        "content": "x" * 500,  # 总长度必然 > 200
    })
    assert len(big_payload) > 200, "测试前置：payload 必须超过上限"

    frame = {
        "msg_id": "big1",
        "payload": big_payload,
        "source_channel_id": "ch1",
    }
    # 不应抛异常（路由层捕获 + WARN 丢弃）
    await mock_gw.send(frame)

    # handler 不应被调用
    assert received == [], "超限 payload 不应触发 handler"


@pytest.mark.asyncio
async def test_required_field_whitespace_rejected():
    """v1.1: 必填字段为仅空白字符串时必须按解析失败处理。

    背景：Step 5b — 必填 = key 存在 + str 类型 + strip() != ''。
    旧实现 `if not user_id` 无法挡住 "   "（空格）。
    """
    router = MessageRouter()
    received = []

    async def handler(msg):
        received.append(msg)

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    # user_id 仅含空白字符
    frame = {
        "msg_id": "ws1",
        "payload": json.dumps({
            "msg_id": "ws1",
            "message_type": "user_instruction",
            "user_id": "   ",  # ❌ 仅空白
            "content": "hello",
        }),
        "source_channel_id": "ch1",
    }
    # 不应抛异常（被静默丢弃），handler 不应触发
    await mock_gw.send(frame)
    assert received == [], "仅空白的 user_id 应被路由层丢弃"


@pytest.mark.asyncio
async def test_required_field_non_string_rejected():
    """v1.1: 必填字段类型不是 str 时必须按解析失败处理（防御 user_id=123 这种）。"""
    router = MessageRouter()
    received = []

    async def handler(msg):
        received.append(msg)

    router.register_route_handler("user_instruction", handler)
    mock_gw = MockGateway()
    router.attach_to_gateway(mock_gw)

    # user_id 是数字而非字符串
    frame = {
        "msg_id": "num1",
        "payload": json.dumps({
            "msg_id": "num1",
            "message_type": "user_instruction",
            "user_id": 12345,  # ❌ int 而非 str
        }),
        "source_channel_id": "ch1",
    }
    await mock_gw.send(frame)
    assert received == [], "非 str 的 user_id 应被路由层丢弃"


def test_max_payload_bytes_default():
    """v1.1: max_payload_bytes 默认值为 1 MiB（1_048_576 字节）。"""
    router = MessageRouter()
    assert router._max_payload_bytes == 1_048_576


def test_handler_timeout_default():
    """v1.1（保留代码值）: handler_timeout_seconds 默认 300s（个人 Agent LLM 长推理需要）。"""
    router = MessageRouter()
    assert router._handler_timeout_seconds == 300.0
