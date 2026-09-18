"""pandaren/mcp/tests/test_integration_stdio.py - 真实子进程 stdio 端到端用例。

设计依据：pandaren/mcp/tests/design/mcp-core.design.md（分组 E：MCP-50..MCP-57）。
覆盖风险：R-SEC1/SEC2（annotations 贯通，P0）、R-CALL1/3/4/5（调用路径，P1）、R-RES1（子进程回收，P1）。
替身：全程真实子进程（conftest.mock_mcp_server.py），零 mock。
Oracle：蜕变关系 / 容错断言，不硬编码"跑一遍抄来的"输出值。
"""

from __future__ import annotations

import pytest

from pandaren.mcp.config import McpServerStatus
from pandaren.mcp.manager import McpClientManager
from pandaren.tool.types import SensitivityLevel

EXPECTED_TOOL_NAMES = {
    "mock__list_items",
    "mock__write_file",
    "mock__delete_thing",
    "mock__echo",
    "mock__fail_tool",
    "mock__slow_tool",
    "mock__structured_tool",
}


# R-LIFE1 真实 list_tools：7 工具名称集合正确（set 相等，不依赖顺序）
@pytest.mark.asyncio
async def test_real_tools_listed(connected_manager):
    tools = connected_manager.tools_for("mock")

    assert {t.name for t in tools} == EXPECTED_TOOL_NAMES


# R-SEC1/SEC2 annotations 真实流过 SDK：write_file=CRITICAL、list_items=LOW、无 HIGH
@pytest.mark.asyncio
async def test_annotations_propagate_end_to_end_no_high(connected_manager):
    tools = connected_manager.tools_for("mock")
    by_name = {t.name: t for t in tools}

    assert by_name["mock__write_file"].sensitivity == SensitivityLevel.CRITICAL
    assert by_name["mock__list_items"].sensitivity == SensitivityLevel.LOW
    assert by_name["mock__delete_thing"].sensitivity == SensitivityLevel.CRITICAL

    # 核心安全断言：免审批档绝不出现
    assert SensitivityLevel.HIGH not in {t.sensitivity for t in tools}


# R-CALL1 真实 echo 成功且 data 非空
@pytest.mark.asyncio
async def test_call_tool_echo_success(connected_manager):
    result = await connected_manager.call_tool("mock", "echo", {"text": "hello"})

    assert result.success is True
    assert result.data != ""
    assert "echo:hello" in str(result.data)


# R-CALL3 真实 fail_tool（服务端抛异常）-> success=False 且 error 非空
@pytest.mark.asyncio
async def test_call_tool_fail_tool_returns_failure(connected_manager):
    result = await connected_manager.call_tool("mock", "fail_tool", {})

    assert result.success is False
    assert result.error != ""


# R-CALL4 真实 slow_tool 超时 -> success=False 且 error 含「超时」
@pytest.mark.asyncio
async def test_call_tool_timeout_returns_failure(connected_manager):
    result = await connected_manager.call_tool(
        "mock", "slow_tool", {"seconds": 3}, timeout=0.5
    )

    assert result.success is False
    assert "超时" in result.error


# R-CALL5 真实 structured_tool -> 成功且序列化含预期键（不硬编码整串）
@pytest.mark.asyncio
async def test_call_tool_structured_fallback(connected_manager):
    result = await connected_manager.call_tool("mock", "structured_tool", {})

    assert result.success is True
    assert "count" in str(result.data)
    assert "3" in str(result.data)


# R-RES1 子进程回收：aclose_all 后 handle 不再存活，且可再次 connect
@pytest.mark.asyncio
async def test_subprocess_recycled_after_close(mock_stdio_config):
    mgr = McpClientManager()
    try:
        await mgr.connect(mock_stdio_config())
        handle = mgr._handles["mock"]
        assert handle.is_alive is True

        await mgr.aclose_all()

        assert handle.is_alive is False
        assert mgr.status("mock") == McpServerStatus.DISCONNECTED

        # 回收后仍可重新连接（无残留阻塞）
        tools = await mgr.connect(mock_stdio_config())
        assert len(tools) == 7
    finally:
        await mgr.aclose_all()
