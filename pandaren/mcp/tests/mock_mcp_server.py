"""pandaren/mcp/tests/mock_mcp_server.py — 供单测/冒烟使用的本地 stdio MCP 服务器。

独立可执行：``python pandaren/mcp/tests/mock_mcp_server.py``（走 stdio 传输）。

只依赖官方 SDK，**不依赖 pandaren**，因此可在子进程里独立启动（模拟真实 server）。
刻意覆盖四类工具，用于验证 sensitivity 判定与调用路径：

  list_items    readOnlyHint=True          → 期望 LOW
  write_file    destructiveHint=True       → 期望 CRITICAL
  echo          无 annotations，名称中性      → 期望 MEDIUM
  delete_thing  无 annotations，"delete"     → 期望 CRITICAL（名称启发式）
  fail_tool     返回 isError=True           → 期望 ToolResult.success=False
  slow_tool     睡眠 3s                     → 期望超时路径
"""

from __future__ import annotations

import time

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer(name="mock-mcp-server", version="0.1.0")


@server.tool(
    name="list_items",
    description="List the items in the store.",
    annotations=ToolAnnotations(read_only_hint=True),
)
def list_items(limit: int = 10) -> str:
    return "\n".join(f"item-{i}" for i in range(limit))


@server.tool(
    name="write_file",
    description="Overwrite a file on disk.",
    annotations=ToolAnnotations(destructive_hint=True),
)
def write_file(path: str, content: str) -> str:
    return f"wrote {len(content)} bytes to {path}"


@server.tool(name="echo", description="Echo back the given text.")
def echo(text: str) -> str:
    return f"echo:{text}"


@server.tool(name="delete_thing", description="Delete a thing.")
def delete_thing(thing_id: str) -> str:
    return f"deleted {thing_id}"


@server.tool(name="fail_tool", description="Always fails.")
def fail_tool() -> str:
    raise ValueError("boom")


@server.tool(name="slow_tool", description="Sleeps for a while.")
def slow_tool(seconds: float = 3.0) -> str:
    time.sleep(seconds)
    return "awake"


@server.tool(name="structured_tool", description="Returns structured content.")
def structured_tool() -> dict:
    return {"ok": True, "count": 3}


if __name__ == "__main__":
    server.run(transport="stdio")
