"""pandaren/mcp/tests/test_manager.py - McpClientManager 生命周期/注册/转发用例。

设计依据：pandaren/mcp/tests/design/mcp-core.design.md（分组 D：MCP-42..MCP-49）。
覆盖风险：R-LIFE1/2/3/4/5（生命周期，P0）、R-ADPT1（命名/注册，P1）。
替身：生命周期用真实 mock 子进程（conftest.connected_manager / mock_stdio_config）；
      register_tools_into 用 duck-typed FakeRegistry。
"""

from __future__ import annotations

import pytest

from pandaren.mcp.config import McpConnectError, McpServerStatus
from pandaren.mcp.manager import McpClientManager
from pandaren.mcp.tests.conftest import FakeRegistry
from pandaren.tool.definition.tool_result import ToolResult

EXPECTED_FULL_NAMES = {
    "mcp_mock__list_items",
    "mcp_mock__write_file",
    "mcp_mock__delete_thing",
    "mcp_mock__echo",
    "mcp_mock__fail_tool",
    "mcp_mock__slow_tool",
    "mcp_mock__structured_tool",
}


# ── connect ────────────────────────────────────────────────


# R-LIFE1 connect 成功 -> CONNECTED 且 7 工具、full_name 前缀正确
@pytest.mark.asyncio
async def test_connect_returns_seven_tools_and_connected(mock_stdio_config):
    mgr = McpClientManager()
    try:
        tools = await mgr.connect(mock_stdio_config())

        assert len(tools) == 7
        assert {t.full_name for t in tools} == EXPECTED_FULL_NAMES
        assert all(t.full_name.startswith("mcp_mock__") for t in tools)
        assert mgr.status("mock") == McpServerStatus.CONNECTED
        assert len(mgr.tools_for("mock")) == 7
        assert {t.full_name for t in mgr.tools_for("mock")} == EXPECTED_FULL_NAMES
    finally:
        await mgr.aclose_all()


# R-LIFE4 connect 失败（坏可执行文件）-> 抛 McpConnectError 且 status=ERROR
@pytest.mark.asyncio
async def test_connect_failure_raises_and_sets_error_status(mock_stdio_config):
    mgr = McpClientManager()
    cfg = mock_stdio_config(
        command="definitely_not_a_real_binary_xyz_123", connect_timeout=5.0
    )

    with pytest.raises(McpConnectError):
        await mgr.connect(cfg)

    assert mgr.status("mock") == McpServerStatus.ERROR
    assert mgr.tools_for("mock") == []


# ── disconnect ─────────────────────────────────────────────


# R-LIFE2 disconnect -> 返回被移除 full_name 集合、status DISCONNECTED、工具清空
@pytest.mark.asyncio
async def test_disconnect_removes_tools_and_marks_disconnected(connected_manager):
    mgr = connected_manager

    removed = await mgr.disconnect("mock")

    assert set(removed) == EXPECTED_FULL_NAMES
    assert mgr.status("mock") == McpServerStatus.DISCONNECTED
    assert mgr.tools_for("mock") == []


# R-LIFE2 重复 disconnect 幂等 -> 第二次返回空列表
@pytest.mark.asyncio
async def test_disconnect_is_idempotent(connected_manager):
    mgr = connected_manager

    await mgr.disconnect("mock")

    assert await mgr.disconnect("mock") == []
    assert mgr.status("mock") == McpServerStatus.DISCONNECTED


# ── call_tool 未连接分支 ────────────────────────────────────


# R-LIFE3 从未 connect 的 server 调 call_tool -> 失败 ToolResult，不外抛
@pytest.mark.asyncio
async def test_call_tool_unknown_server_returns_failure():
    mgr = McpClientManager()

    result = await mgr.call_tool("ghost", "echo", {})

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.error != ""
    assert result.tool_name == "echo"


# R-LIFE3 断开后调 call_tool -> 失败 ToolResult，绝不外抛
@pytest.mark.asyncio
async def test_call_tool_after_disconnect_returns_failure(connected_manager):
    mgr = connected_manager

    await mgr.disconnect("mock")
    result = await mgr.call_tool("mock", "echo", {})

    assert result.success is False
    assert result.error != ""


# ── aclose_all ─────────────────────────────────────────────


# R-LIFE5 aclose_all 幂等 -> 连续两次不抛，最终 DISCONNECTED
@pytest.mark.asyncio
async def test_aclose_all_is_idempotent(mock_stdio_config):
    mgr = McpClientManager()
    await mgr.connect(mock_stdio_config())

    await mgr.aclose_all()
    await mgr.aclose_all()

    assert mgr.status("mock") == McpServerStatus.DISCONNECTED


# R-LIFE5 对已断开（无存活 handle）的集合调 aclose_all -> 不抛异常
@pytest.mark.asyncio
async def test_aclose_all_after_disconnect_does_not_raise(mock_stdio_config):
    mgr = McpClientManager()
    await mgr.connect(mock_stdio_config())
    await mgr.disconnect("mock")

    await mgr.aclose_all()
    await mgr.aclose_all()

    assert mgr.status("mock") == McpServerStatus.DISCONNECTED


# ── register_tools_into ────────────────────────────────────


# R-ADPT1 全量注册 -> 7 个 full_name，skip_if_exists=True 逐个透传
def test_register_tools_into_all_servers(connected_manager):
    reg = FakeRegistry()

    names = connected_manager.register_tools_into(reg)

    assert set(names) == EXPECTED_FULL_NAMES
    assert {fn for fn, _ in reg.register_calls} == EXPECTED_FULL_NAMES
    assert all(skip is True for _, skip in reg.register_calls)


# R-ADPT1 server_name="mock" 只注册该 server（结果与全量一致）
def test_register_tools_into_named_server(connected_manager):
    reg = FakeRegistry()

    names = connected_manager.register_tools_into(reg, server_name="mock")

    assert set(names) == EXPECTED_FULL_NAMES
    assert len(reg.register_calls) == 7


# R-ADPT1 未知 server_name -> 空列表且不触发任何注册
def test_register_tools_into_unknown_server_returns_empty(connected_manager):
    reg = FakeRegistry()

    names = connected_manager.register_tools_into(reg, server_name="ghost")

    assert names == []
    assert reg.register_calls == []


# R-ADPT1 skip_if_exists=False 透传（不覆盖为默认值）
def test_register_tools_into_skip_flag_passthrough(connected_manager):
    reg = FakeRegistry()

    connected_manager.register_tools_into(reg, skip_if_exists=False)

    assert reg.register_calls != []
    assert all(skip is False for _, skip in reg.register_calls)
