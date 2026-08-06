"""pandaren/tool/builtin/search.py — search_tools 工厂。

executor 不再闭包捕获 ToolRegistry。
运行时通过 ctx.metadata["discovery_manager"] / ctx.metadata["tool_store"] 获取依赖。
"""

from __future__ import annotations

import logging

from ..definition.tool import Tool
from ..definition.tool_result import ToolResult, DiscoveredToolEntry
from ..definition.context import ToolContext
from ..definition.tool_policy import ToolPolicy
from ..types import ToolTier, SensitivityLevel

logger = logging.getLogger("pandaren.tool.builtin.search")


class SearchToolFactory:
    """search_tools 工厂（无状态）。"""

    def create_tools(self) -> list[Tool]:
        def _executor(ctx: ToolContext, tool_name: str) -> ToolResult:
            """加载指定工具的完整 schema，使其可被调用。

            Args:
                tool_name: 要加载的工具名称（必须与 available_tools 中列出的名称完全一致）。
            """
            from ..registry.store import ToolStore
            from ..registry.discovery import DiscoveryManager

            store: ToolStore = ctx.metadata["tool_store"]
            discovery: DiscoveryManager = ctx.metadata["discovery_manager"]

            tool = store.get(tool_name)
            if tool is None or tool.tier == ToolTier.ALWAYS:
                return ToolResult(
                    success=True,
                    data=f"未找到工具 '{tool_name}'，请检查名称是否正确（区分大小写）",
                    tool_name="search_tools",
                )

            # is_enabled 过滤（使用原始 full_name 查询缓存）
            enabled_cache = ctx.metadata.get("enabled_cache", {})
            if not enabled_cache.get(tool.full_name, True):
                return ToolResult(
                    success=True,
                    data=f"未找到工具 '{tool_name}'，请检查名称是否正确（区分大小写）",
                    tool_name="search_tools",
                )

            # 标记为已发现（使用原始 full_name 写入 DiscoveryManager）
            discovery.discover(tool.full_name, ctx.step_n)

            # 构建结果
            result_text = (
                f"找到 1 个匹配工具：\n"
                f"  - {tool_name}：{tool.description}"
                f"（适用场景：{tool.when_to_use}）"
            )

            # Sentinel 字段
            discovered = (DiscoveredToolEntry(name=tool_name, turn=ctx.step_n),)

            return ToolResult(
                success=True,
                data=result_text,
                tool_name="search_tools",
                _discovered_tools=discovered,
            )

        return [Tool(
            name="search_tools",
            description="按名称加载 DEFERRED 工具的完整 schema，加载后方可调用该工具",
            executor=_executor,
            input_schema={
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                    }
                },
                "required": ["tool_name"],
            },
            tier=ToolTier.ALWAYS,
            when_to_use="当需要加载 DEFERRED 工具的完整 schema 以便调用时使用。",
            policy=ToolPolicy(
                sensitivity=SensitivityLevel.LOW,
                is_idempotent=True,
                max_calls_per_turn=10,
            ),
        )]
