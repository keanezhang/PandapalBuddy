"""★ 根本解回归测试：Relay 端 transport 生命周期驱动（2026-06-10）。

背景（与 pandapal/broadcast/tests/test_lifecycle.py 对称）：
  之前 run_relay.py 在 init_wecom_bridge 构造完 WeComRestTransport 就走人，
  导致 transport.start() 永远不被调用，access_token 永远不校验，
  第一次发企微消息就失败。
  根本解：把 transport.start()/stop() 调度集中到 run_relay.py 启动/关闭流程。

本文件验证：
  1. transport_protocol 强制要求 is_started
  2. WeComRestTransport 实现 is_started
  3. WeComRestTransport.start() 幂等
  4. WeComRestTransport.stop() 幂等
  5. start() 失败不阻塞（HC3 Fail-Safe）
  6. init_wecom_bridge 返回 transport 引用
  7. run_relay.py 源码包含 wecom_transport.start() / .stop() 调用
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pandapal_relay.transport_protocol import Transport
from pandapal_relay.wecom_transport import WeComRestTransport


# ══════════════════════════════════════════════════════════════════════════════
# Transport Protocol 契约测试
# ══════════════════════════════════════════════════════════════════════════════


def test_transport_protocol_requires_is_started():
    """Transport Protocol 必须声明 is_started（★ 根本解契约，与 pandapal 端同步）。"""
    assert hasattr(Transport, "is_started"), (
        "Transport Protocol missing is_started — "
        "根本解要求生命周期状态可查询"
    )


# ══════════════════════════════════════════════════════════════════════════════
# WeComRestTransport 生命周期测试
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_wecom_transport_is_started_initially_false():
    """★ 构造完成 ≠ 启动：构造后 is_started 必须是 False。"""
    sender = MagicMock()
    transport = WeComRestTransport(sender=sender)
    assert transport.is_started is False, (
        "WeComRestTransport 构造后 is_started 必须为 False，"
        "避免 'log 说 initialized 但实际未校验 access_token' 的歧义"
    )


@pytest.mark.asyncio
async def test_wecom_transport_start_verifies_access_token():
    """start() 必须校验 access_token（★ 这是 start() 的核心职责）。"""
    sender = MagicMock()
    sender.verify_access_token = AsyncMock(return_value=True)
    transport = WeComRestTransport(sender=sender)

    await transport.start()

    sender.verify_access_token.assert_awaited_once()
    assert transport.is_started is True


@pytest.mark.asyncio
async def test_wecom_transport_start_is_idempotent():
    """start() 重复调用不会重复 verify。"""
    sender = MagicMock()
    sender.verify_access_token = AsyncMock(return_value=True)
    transport = WeComRestTransport(sender=sender)

    await transport.start()
    await transport.start()
    await transport.start()

    assert sender.verify_access_token.await_count == 1, (
        "重复 start() 不应重复调 sender.verify_access_token()"
    )


@pytest.mark.asyncio
async def test_wecom_transport_stop_is_idempotent():
    """stop() 关闭已启动 transport；幂等。"""
    sender = MagicMock()
    sender.verify_access_token = AsyncMock(return_value=True)
    transport = WeComRestTransport(sender=sender)

    await transport.start()
    assert transport.is_started is True

    await transport.stop()
    assert transport.is_started is False

    # 幂等：再次 stop 不应报错
    await transport.stop()
    await transport.stop()
    assert transport.is_started is False


@pytest.mark.asyncio
async def test_wecom_transport_start_isolates_failure():
    """start() 内部 verify 失败不应抛异常（HC3 Fail-Safe）。"""
    sender = MagicMock()
    sender.verify_access_token = AsyncMock(side_effect=RuntimeError("network error"))
    transport = WeComRestTransport(sender=sender)

    # 不应抛异常
    await transport.start()
    # _started 仍置 True（已尝试启动，标记为「lifecycle 已驱动」）
    assert transport.is_started is True, (
        "即使 verify 失败，is_started 也应置 True——表示 'lifecycle 已驱动'，"
        "这样 run_relay 启动自检不会误报。"
    )


@pytest.mark.asyncio
async def test_wecom_transport_satisfies_transport_protocol():
    """WeComRestTransport 是 Transport Protocol 的合法实现。"""
    sender = MagicMock()
    transport = WeComRestTransport(sender=sender)
    assert isinstance(transport, Transport), (
        "WeComRestTransport 必须满足 Transport Protocol——is_started 属性不能少"
    )


# ══════════════════════════════════════════════════════════════════════════════
# init_wecom_bridge 返回 transport 引用
# ══════════════════════════════════════════════════════════════════════════════


def test_init_wecom_bridge_returns_transport():
    """init_wecom_bridge 必须返回 transport 引用（供 run_relay 调度 start/stop）。

    ★ 关键修复：wecom_bridge.py 用了 `from __future__ import annotations`，
      所有类型注解是字符串。用 typing.get_type_hints() 解析字符串。
    """
    import typing

    from pandapal_relay import wecom_bridge

    sig = inspect.signature(wecom_bridge.init_wecom_bridge)
    assert sig.return_annotation is not inspect.Signature.empty, (
        "init_wecom_bridge 必须声明返回类型"
    )

    # 解析字符串注解（因为 from __future__ import annotations）
    hints = typing.get_type_hints(wecom_bridge.init_wecom_bridge)
    return_type = hints.get("return")
    assert return_type is WeComRestTransport, (
        f"init_wecom_bridge 返回类型应为 WeComRestTransport，"
        f"实际为 {return_type}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# ★ 核心回归测试：run_relay.py 源码必须调度 transport.start()/stop()
# ══════════════════════════════════════════════════════════════════════════════


_RUN_RELAY_PATH = Path(__file__).resolve().parent.parent / "run_relay.py"


def _read_run_relay_source() -> str:
    return _RUN_RELAY_PATH.read_text(encoding="utf-8")


def test_run_relay_invokes_wecom_transport_start():
    """run_relay.py 源码必须包含 wecom_transport.start() 调用。

    ★ 这是根本解的防回归网：防止以后有人把 start() 调用删了，
      导致 access_token 永远不校验，企微消息第一次发就失败。
    """
    src = _read_run_relay_source()
    assert "wecom_transport.start" in src, (
        "run_relay.py 未调用 wecom_transport.start() — "
        "根本解要求 run_relay.py 调度 transport 生命周期，"
        "否则 access_token 永远不校验，企微消息第一次发就失败。"
    )


def test_run_relay_invokes_wecom_transport_stop():
    """run_relay.py 源码必须包含 wecom_transport.stop() 调用（与 start 对称）。"""
    src = _read_run_relay_source()
    assert "wecom_transport.stop" in src, (
        "run_relay.py 未调用 wecom_transport.stop() — "
        "stop 必须与 start 对称，否则会泄露 transport 状态。"
    )


# ══════════════════════════════════════════════════════════════════════════════
# ★ 根本解 2026-06-10 第四波：R1 (wecom_bridge 引用不存在属性) 回归测试
#   + R2 (server.py 公共通道) 契约测试
# ══════════════════════════════════════════════════════════════════════════════


_SERVER_PATH = Path(__file__).resolve().parent.parent / "server.py"
_WECOM_BRIDGE_PATH = Path(__file__).resolve().parent.parent / "wecom_bridge.py"


def _read_server_source() -> str:
    return _SERVER_PATH.read_text(encoding="utf-8")


def _read_wecom_bridge_source() -> str:
    return _WECOM_BRIDGE_PATH.read_text(encoding="utf-8")


def test_server_exposes_is_agent_connected():
    """server.py 必须暴露 is_agent_connected() public accessor。

    ★ 防止以后有人删了公共 accessor，导致 wecom_bridge 又被逼得去访问私有 _agent_ws。
    """
    import re
    src = _read_server_source()
    assert re.search(r"^def\s+is_agent_connected\s*\(", src, re.MULTILINE), (
        "server.py 缺少 is_agent_connected() public accessor — "
        "wecom_bridge 会被迫去访问私有 _agent_ws，破坏封装。"
    )


def test_server_exposes_send_to_agent():
    """server.py 必须暴露 send_to_agent() public 函数（★ 公共通道）。"""
    import re
    src = _read_server_source()
    assert re.search(r"^async\s+def\s+send_to_agent\s*\(", src, re.MULTILINE), (
        "server.py 缺少 send_to_agent() public 函数 — "
        "wecom_bridge 会被迫去访问私有 _agent_ws，破坏封装。"
    )


def test_wecom_bridge_does_not_access_relay_ws():
    """★ 关键回归防护：wecom_bridge 不能再访问不存在的 relay_server.relay_ws。

    之前 wecom_bridge.py:231 写的是 `if relay_server.relay_ws and not relay_server.relay_ws.is_closed:`，
    但 server.py 实际只有 `_agent_ws`（私有），没有 `relay_ws`。`if` 表达式求值时直接抛 AttributeError，
    被 try/except 静默吞掉 → 用户点审批按钮全崩。

    这条测试断言该反模式不再出现。
    ★ 用 AST 扫描 ast.Attribute 节点，过滤注释和字符串里的同名引用（解释性文字）。
    """
    src = _read_wecom_bridge_source()
    tree = ast.parse(src)
    # 仅检查实际代码访问：ast.Attribute（属性访问） + 标识符链 relay_server.x
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr != "relay_ws":
            continue
        # 找到调用链根节点（处理 a.b.c 链）
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id == "relay_server":
            raise AssertionError(
                "wecom_bridge.py 又出现 `relay_server.relay_ws` 属性访问（AST 节点） — "
                "该属性根本不存在（实际是私有 _agent_ws），AttributeError 会被静默吞掉，"
                "用户每次点审批按钮都会失败。"
                "请改用 server.is_agent_connected() + send_to_agent() 公共通道。"
            )


def test_wecom_bridge_uses_public_send_to_agent():
    """wecom_bridge 应通过 server.send_to_agent() 公共通道发送（而非直接访问 _agent_ws）。"""
    src = _read_wecom_bridge_source()
    assert "relay_server.send_to_agent" in src, (
        "wecom_bridge.py 未使用 relay_server.send_to_agent() 公共通道 — "
        "应通过公共 API 发送，避免破坏封装。"
    )
