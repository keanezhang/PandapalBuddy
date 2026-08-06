"""pandaren/tool/execution/guard_chain.py — 执行前置门控链。

每道 Guard 独立可测试，返回 None = 通过，返回 ToolResult = 拒绝。

GuardChain vs GateChain 区别
════════════════════════════
项目中有两条链，职责不同、时机不同：

               GateChain（exposure 层）          GuardChain（execution 层，本文件）
  时机        每轮对话 构建 schema 前              工具 真正执行前
  作用        决定 LLM 能「看到」哪些工具           决定 LLM 能「调用」哪些工具
  输入/输出   全部工具列表 → 过滤后列表             单个工具 → 通过 / 拒绝
  类比        餐厅菜单上展示哪些菜品               点菜后厨房检查这道菜能不能做

两层防护确保：即使 LLM 幻觉调用了一个不该调的工具，
GuardChain 也能在执行前拦住并返回错误 ToolResult。

4 道 Guard（按检查顺序）：
  ① EnabledGuard        — 工具是否被启用（运行时动态开关）
  ② AgentWhitelistGuard — Agent 是否在工具白名单内
  ③ TrustLevelGuard     — 调用方信任等级是否足够
  ④ DiscoveryGuard      — DEFERRED 工具是否已通过 search_tools 发现
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from ..definition.tool import Tool
from ..definition.tool_result import ToolResult
from ..definition.context import ToolContext
from ..registry.discovery import DiscoveryManager
from ..types import ToolTier

logger = logging.getLogger("pandaren.tool.execution.guard_chain")


@runtime_checkable
class ExecutionGuard(Protocol):
    """执行前置门控协议。"""
    def check(self, tool: Tool, args: dict, ctx: ToolContext) -> ToolResult | None: ...


class EnabledGuard:
    """is_enabled 检查。"""

    def __init__(self, enabled_cache: dict[str, bool]) -> None:
        self._enabled_cache = enabled_cache

    def check(self, tool: Tool, args: dict, ctx: ToolContext) -> ToolResult | None:
        if not self._enabled_cache.get(tool.full_name, True):
            return ToolResult(
                success=False,
                error=f"Tool '{tool.full_name}' 当前不可用（is_enabled=False）",
                tool_name=tool.full_name,
            )
        return None


class AgentWhitelistGuard:
    """agent_whitelist 检查。"""

    def check(self, tool: Tool, args: dict, ctx: ToolContext) -> ToolResult | None:
        if tool.agent_whitelist and ctx.agent_id not in tool.agent_whitelist:
            return ToolResult(
                success=False,
                error=(
                    f"Agent '{ctx.agent_id}' 无权访问工具 '{tool.full_name}'，"
                    f"该工具仅限白名单 Agent 调用"
                ),
                tool_name=tool.full_name,
            )
        return None


class TrustLevelGuard:
    """trust_level_required 检查。"""

    def check(self, tool: Tool, args: dict, ctx: ToolContext) -> ToolResult | None:
        if ctx.trust_level < tool.trust_level_required:
            return ToolResult(
                success=False,
                error=(
                    f"调用方信任等级不足：需要 {tool.trust_level_required.name}，"
                    f"当前为 {ctx.trust_level.name}"
                ),
                tool_name=tool.full_name,
            )
        return None


class DiscoveryGuard:
    """DEFERRED 未发现拦截。"""

    def __init__(self, discovery: DiscoveryManager) -> None:
        self._discovery = discovery

    def check(self, tool: Tool, args: dict, ctx: ToolContext) -> ToolResult | None:
        if tool.tier != ToolTier.ALWAYS and not self._discovery.is_discovered(tool.full_name):
            return ToolResult(
                success=False,
                error=(
                    f"Tool '{tool.full_name}' 尚未加载 schema，无法调用。"
                    f"请先调用 search_tools(tool_name=\"{tool.full_name}\") 加载该工具。"
                ),
                tool_name=tool.full_name,
            )
        return None


class GuardChain:
    """执行前置检查链。返回 None = 全部通过，返回 ToolResult = 拒绝。"""

    def __init__(self, guards: list[ExecutionGuard] | None = None) -> None:
        self._guards: list[ExecutionGuard] = guards or []

    def add(self, guard: ExecutionGuard) -> None:
        self._guards.append(guard)

    def check_all(self, tool: Tool, args: dict, ctx: ToolContext) -> ToolResult | None:
        """依次检查所有 guard，第一个拒绝即返回。"""
        for guard in self._guards:
            result = guard.check(tool, args, ctx)
            if result is not None:
                logger.info(
                    "[guard] 拒绝 | tool=%s | guard=%s | reason=%s",
                    tool.full_name, type(guard).__name__, result.error,
                )
                return result
        return None
