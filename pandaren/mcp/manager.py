"""pandaren/mcp/manager.py — 多 MCP 服务器生命周期管理。

设计依据：docs/design/mcp-capability.md（§4 D6/D9、§6.1 manager.py、§7.4 签名）。

职责：按 server name 维护 ``McpSessionHandle``，把「连接 → 列工具 → 适配」
聚合成一次 ``connect()`` 调用，并暴露 ``disconnect`` / ``status`` / ``call_tool`` /
``aclose_all``。

不负责：**主动**把 Tool 注册进 ToolRegistry（工具已适配好、可通过 ``tools_for``
取用，谁注册、注册进哪个 registry 由调用方决定）。D6 为此留了一条缝
``register_tools_into(registry)``：pandaren 层不持有「共享 registry」概念，调用方
自行传入目标 registry，因此同一条缝对主 Agent registry 与子 Agent registry 通用。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..tool.definition.tool import Tool
from ..tool.definition.tool_result import ToolResult
from .client import McpSessionHandle
from .config import McpServerConfig, McpServerStatus
from .tool_adapter import McpToolAdapter

if TYPE_CHECKING:
    from ..tool.facade import ToolRegistry

logger = logging.getLogger("pandaren.mcp.manager")


class McpClientManager:
    """管理所有 MCP 服务器会话（每个 server 一个 handle + 一份工具列表）。"""

    def __init__(self) -> None:
        self._handles: dict[str, McpSessionHandle] = {}
        self._tools: dict[str, list[Tool]] = {}
        self._status: dict[str, McpServerStatus] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── 生命周期 ──────────────────────────────────────────

    async def connect(self, config: McpServerConfig) -> list[Tool]:
        """连接一个服务器并返回其适配后的工具列表。

        重复连接同名服务器会先断开旧会话（幂等）。失败时状态置 ERROR 并上抛。
        """
        config.validate()
        name = config.name
        async with self._lock(name):
            await self._disconnect_locked(name)
            self._status[name] = McpServerStatus.CONNECTING

            handle = McpSessionHandle(config)
            try:
                await handle.connect()
            except Exception:
                self._status[name] = McpServerStatus.ERROR
                raise

            adapter = McpToolAdapter(config, handle)
            tools = [adapter.adapt(d) for d in handle.list_tools()]

            self._handles[name] = handle
            self._tools[name] = tools
            self._status[name] = McpServerStatus.CONNECTED
            logger.info(
                "MCP server '%s' 已连接，注册 %d 个工具", name, len(tools)
            )
            return tools

    async def disconnect(self, name: str) -> list[str]:
        """断开一个服务器，返回被移除工具的 full_name 列表。"""
        async with self._lock(name):
            removed = await self._disconnect_locked(name)
            self._status[name] = McpServerStatus.DISCONNECTED
            return removed

    async def aclose_all(self) -> None:
        """关闭所有服务器（幂等；单个失败不阻塞其余）。"""
        for name in list(self._handles.keys()):
            try:
                await self.disconnect(name)
            except Exception as exc:  # noqa: BLE001 - 故障隔离点，逐个留痕继续
                logger.warning("关闭 MCP server '%s' 失败：%s", name, exc)

    # ── 查询 ──────────────────────────────────────────────

    def status(self, name: str) -> McpServerStatus:
        """返回服务器状态；handle 存活则权威判定为 CONNECTED。"""
        handle = self._handles.get(name)
        if handle is None:
            return self._status.get(name, McpServerStatus.DISCONNECTED)
        return (
            McpServerStatus.CONNECTED
            if handle.is_alive
            else McpServerStatus.DISCONNECTED
        )

    def tools_for(self, name: str) -> list[Tool]:
        """返回某服务器已适配的工具（未连接则为空）。"""
        return list(self._tools.get(name, []))

    def register_tools_into(
        self,
        registry: "ToolRegistry",
        *,
        server_name: str | None = None,
        skip_if_exists: bool = True,
    ) -> list[str]:
        """把已适配的 MCP 工具注册进调用方传入的 registry（D6 扩展缝）。

        pandaren 层刻意不持有「共享 registry」概念：目标 registry 由调用方决定，
        因此同一条缝既能注册进主 Agent 的 registry，也能注册进子 Agent 的 registry。

        Args:
            registry: 目标 ``ToolRegistry``（duck-typed，只需有 ``register_tool``）。
            server_name: 只注册该服务器；None 表示注册全部已连接服务器的工具。
            skip_if_exists: True 时遇到同名工具跳过（默认，避免覆盖同名内置工具）。

        Returns:
            本次注册（或已存在被跳过）的 tool ``full_name`` 列表。
        """
        names = [server_name] if server_name is not None else list(self._tools.keys())
        registered: list[str] = []
        for name in names:
            for tool in self._tools.get(name, []):
                registry.register_tool(tool, skip_if_exists=skip_if_exists)
                registered.append(tool.full_name)
        return registered

    # ── 工具调用 ──────────────────────────────────────────

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> ToolResult:
        """按服务器转发一次工具调用（未连接时返回失败 ToolResult，不外抛）。"""
        handle = self._handles.get(server_name)
        if handle is None:
            return ToolResult(
                success=False,
                error=f"MCP server '{server_name}' 未连接",
                tool_name=tool_name,
            )
        return await handle.call_tool(tool_name, arguments, timeout=timeout)

    # ── 内部 ──────────────────────────────────────────────

    def _lock(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        return lock

    async def _disconnect_locked(self, name: str) -> list[str]:
        tools = self._tools.pop(name, [])
        handle = self._handles.pop(name, None)
        if handle is not None:
            await handle.aclose()
        return [t.full_name for t in tools]
