"""pandaren/mcp/tests/test_client.py — 会话句柄序列化与 call_tool 分支（component，Fake）。

设计依据：mcp-core.design.md 分组 C（MCP-31..41）。
风险映射：R-CALL1/2/3/4/5、R-LIFE3、R-RES1。
Oracle：golden value（注入确定返回值，期望可人工推导）。
"""

from __future__ import annotations

import json
import logging

import pytest

from pandaren.mcp.client import McpSessionHandle, _json_dump, _serialize_content
from pandaren.mcp.config import McpServerConfig

from .conftest import FakeContent, FakeResult, FakeSession


def _handle() -> McpSessionHandle:
    cfg = McpServerConfig(name="mock", transport="stdio", command="py")
    return McpSessionHandle(cfg)


# R-CALL1 多文本块拼接 [P1]
def test_serialize_content_joins_text_blocks():
    result = FakeResult(content=[FakeContent("a"), FakeContent("b")])
    assert _serialize_content(result) == "a\nb"


# R-CALL1 非文本块结构化序列化（字段不丢）[P1]
def test_serialize_content_serializes_non_text_block():
    image = {"type": "image", "data": "x"}
    result = FakeResult(content=[image])
    assert json.loads(_serialize_content(result)) == image


# R-CALL5 空 content 回落 structuredContent [P1]
def test_serialize_content_falls_back_to_structured():
    result = FakeResult(content=[], structured_content={"ok": True, "count": 3})
    assert json.loads(_serialize_content(result)) == {"ok": True, "count": 3}


# R-CALL1 _json_dump 不可序列化对象回落 + 留痕 [P2]
def test_json_dump_falls_back_and_logs_warning(caplog):
    class BrokenModelDump:
        def model_dump(self):
            raise RuntimeError("nope")

    broken = BrokenModelDump()
    with caplog.at_level(logging.WARNING, logger="pandaren.mcp.client"):
        dumped = _json_dump(broken)

    assert dumped == str(broken)
    assert any("序列化失败" in r.getMessage() for r in caplog.records)


# R-LIFE3 未连接时 call_tool 返回失败（不外抛）[P0]
@pytest.mark.asyncio
async def test_call_tool_without_session_returns_failure():
    handle = _handle()
    result = await handle.call_tool("echo", {})

    assert result.success is False
    assert "未连接" in result.error


# R-CALL1 正常调用成功且记录入参 [P1]
@pytest.mark.asyncio
async def test_call_tool_success_serializes_content():
    handle = _handle()
    session = FakeSession(returns=FakeResult(content=[FakeContent("echo:hi")]))
    handle._session = session

    result = await handle.call_tool("echo", {"text": "hi"})

    assert result.success is True
    assert result.data == "echo:hi"
    assert session.calls == [("echo", {"text": "hi"})]


# R-CALL2 isError=true → success=False [P0]
@pytest.mark.asyncio
async def test_call_tool_is_error_becomes_failure():
    handle = _handle()
    handle._session = FakeSession(
        returns=FakeResult(content=[FakeContent("boom")], is_error=True)
    )

    result = await handle.call_tool("fail_tool", {})

    assert result.success is False
    assert result.error == "boom"


@pytest.mark.asyncio
async def test_call_tool_is_error_with_empty_content_still_reports():
    handle = _handle()
    handle._session = FakeSession(returns=FakeResult(content=[], is_error=True))

    result = await handle.call_tool("fail_tool", {})

    assert result.success is False
    assert "isError=true" in result.error


# R-CALL3 session.call_tool 抛异常 → success=False [P1]
@pytest.mark.asyncio
async def test_call_tool_exception_becomes_failure():
    handle = _handle()
    handle._session = FakeSession(raises=RuntimeError("kaboom"))

    result = await handle.call_tool("echo", {})

    assert result.success is False
    assert result.error != ""
    assert "kaboom" in result.error


# R-CALL4 超时 → success=False 且 error 含「超时」[P1]
@pytest.mark.asyncio
async def test_call_tool_timeout_becomes_failure():
    handle = _handle()
    handle._session = FakeSession(delay=1.0)

    result = await handle.call_tool("slow_tool", {}, timeout=0.05)

    assert result.success is False
    assert "超时" in result.error
    assert "0.05" in result.error


# R-CALL1 边缘：结果无 content 属性 → 显式失败（不假装成功）[P1]
@pytest.mark.asyncio
async def test_call_tool_result_without_content_fails():
    handle = _handle()
    handle._session = FakeSession(returns=object())

    result = await handle.call_tool("echo", {})

    assert result.success is False
    assert "暂不支持" in result.error or "multi-turn" in result.error


# R-RES1 aclose 未连接时幂等且 is_alive=False [P1]
@pytest.mark.asyncio
async def test_aclose_without_connection_is_idempotent():
    handle = _handle()

    await handle.aclose()
    await handle.aclose()

    assert handle.is_alive is False
