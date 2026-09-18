"""MCP 能力接入（应用层）。

- :mod:`pandapal.mcp.config_store` —— ``servers.toml`` 的原子读写。
- :mod:`pandapal.mcp.manager` —— 生命周期编排 + 动态工具注册 + 出站事件发射。

底层 MCP 协议客户端 / 工具适配在 SDK 侧：:mod:`pandaren.mcp`。
"""

from __future__ import annotations

from pandapal.mcp.config_store import McpConfigStore
from pandapal.mcp.manager import McpDegradeEvent, McpManager

__all__ = ["McpConfigStore", "McpDegradeEvent", "McpManager"]
