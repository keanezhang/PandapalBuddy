"""pandaren/mcp — MCP（Model Context Protocol）集成能力。

设计依据：docs/design/mcp-capability.md。

对外契约：
  - ``McpClientManager``  多服务器生命周期 + 工具调用转发
  - ``McpServerConfig``   fail-fast 配置模型（不做 IO）
  - ``McpToolDescriptor`` 归一化后的工具描述
  - ``McpServerStatus``   运行时状态枚举

分层：本包位于 capability 层（与 ``pandaren.tool`` 同级），只依赖 tool / identity，
不 import 应用层（pandapal）任何模块。
"""

from __future__ import annotations

from .client import McpSessionHandle, McpToolDescriptor
from .config import (
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    McpConfigError,
    McpConnectError,
    McpError,
    McpServerConfig,
    McpServerStatus,
    McpTransport,
)
from .manager import McpClientManager
from .tool_adapter import MCP_TOOL_MAX_OUTPUT_BYTES, McpToolAdapter

__all__ = [
    # manager
    "McpClientManager",
    # client
    "McpSessionHandle",
    "McpToolDescriptor",
    # config
    "McpServerConfig",
    "McpServerStatus",
    "McpTransport",
    "McpError",
    "McpConfigError",
    "McpConnectError",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_CALL_TIMEOUT",
    # adapter
    "McpToolAdapter",
    "MCP_TOOL_MAX_OUTPUT_BYTES",
]
