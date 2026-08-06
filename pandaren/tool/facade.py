"""pandaren/tool/facade.py — ToolRegistry Facade（组合内部组件，对外暴露统一 API）。

顶层 Facade 持有：
  - store: ToolStore
  - discovery: DiscoveryManager
  - schema_builder: SchemaBuilder
  - executor: ToolExecutor
  - guard_chain: GuardChain
  - gate_chain: GateChain

对外 API 签名不变，内部实现委托给各组件。
"""

from __future__ import annotations

import asyncio
import logging
import types as builtin_types
from typing import Any

from .definition.tool import Tool
from .definition.tool_result import ToolResult
from .definition.tool_schema import ToolSchema
from .definition.context import ToolContext
from .types import ToolTier
from .registry.store import ToolStore
from .registry.discovery import DiscoveryManager
from .exposure.gate_chain import GateChain, ExposureContext
from .exposure.schema_builder import SchemaBuilder
from .exposure.budget import ToolBudget
from .execution.executor import ToolExecutor
from .execution.guard_chain import (
    GuardChain, EnabledGuard,
    AgentWhitelistGuard, TrustLevelGuard, DiscoveryGuard,
)
from .builtin.protocol import BuiltinToolFactory
from ..hook import AgentHooks

logger = logging.getLogger("pandaren.tool.facade")


class ToolRegistry:
    """顶层 Facade（组合内部组件，对外暴露统一 API）。

    内部持有：
      - store: ToolStore（纯存储）
      - discovery: DiscoveryManager（发现状态）
      - schema_builder: SchemaBuilder（暴露策略）
      - executor: ToolExecutor（执行器）

    对外 API 签名与旧版兼容。
    """

    def __init__(self, budget: ToolBudget | None = None) -> None:
        self._store = ToolStore()
        self._discovery = DiscoveryManager(
            max_discovered=(budget.max_discovered_per_session if budget else 20)
        )
        self._budget = budget or ToolBudget()
        self._gate_chain = GateChain.default()
        self._schema_builder = SchemaBuilder(
            store=self._store,
            discovery=self._discovery,
            gate_chain=self._gate_chain,
            budget=self._budget,
        )
        self._executor = ToolExecutor()

        # 可用性缓存
        self._enabled_cache: dict[str, bool] = {}

        # Hooks
        self._hooks: AgentHooks | None = None
        self._hooks_locked: bool = False

        # deferred 摘要缓存
        self._deferred_summaries: list[dict] = []

    # ─── 属性访问 ────────────────────────────────────────

    @property
    def store(self) -> ToolStore:
        """暴露 ToolStore 供内部组件直接访问。"""
        return self._store

    @property
    def discovery(self) -> DiscoveryManager:
        """暴露 DiscoveryManager 供内部组件直接访问。"""
        return self._discovery

    @property
    def version(self) -> int:
        """注册表版本号，每次 register/unregister 递增。供脏检查用。"""
        return self._store.version

    @property
    def always_tools_count(self) -> int:
        """ALWAYS 工具数量（不含 search_tools）。"""
        count = 0
        for full_name, tool in self._store.items():
            if tool.tier == ToolTier.ALWAYS and full_name != "search_tools":
                count += 1
        return count

    # ─── Hooks ────────────────────────────────────────

    def set_hooks(self, hooks: AgentHooks) -> None:
        """注入观测 hooks（只允许调用一次）。"""
        if self._hooks_locked:
            raise RuntimeError("ToolRegistry.hooks 已注入，不允许二次替换。")
        self._hooks = hooks
        self._hooks_locked = True

    # ═══════════════════════════════════════════
    #  工具注册，注册的本质就是把所有工具引用和名字一对一存储在字典里，self._tools[full_name] = tool
    # ═══════════════════════════════════════════

    def register_tool(self, tool: Tool, *, skip_if_exists: bool = False) -> None:
        """注册工具。"""
        self._store.register(tool, skip_if_exists=skip_if_exists)

        if self._hooks:
            self._hooks.on_tool_register(
                tool_name=tool.full_name,
                tier=tool.tier,
                sensitivity=tool.sensitivity,
                namespace=tool.namespace,
            )

    def unregister_tool(self, tool_name: str) -> bool:
        """注销工具。

        同时清理：
          - ToolStore 中的注册
          - _enabled_cache 中的缓存
          - DiscoveryManager 中的发现状态

        Args:
            tool_name: 工具全名（如 "skill.weather"）或 safe_name。

        Returns:
            True 表示成功注销，False 表示工具不存在。
        """
        tool = self._store.get(tool_name)
        if tool is None:
            return False

        full_name = tool.full_name
        ok = self._store.unregister(full_name)
        if ok:
            self._enabled_cache.pop(full_name, None)
            # 从 discovery 中移除
            self._discovery.undiscover(full_name)
            logger.info("工具已注销: %s", full_name)
        return ok

    def register_builtin_factories(self, factories: list[BuiltinToolFactory]) -> None:
        """统一注册所有内置工具（新 API）。"""
        total = 0
        for factory in factories:
            tools = factory.create_tools()
            for tool in tools:
                self.register_tool(tool)
                total += 1
        if total > 0:
            logger.info("内置工具注册完成: 共 %d 个 (%d 个 Factory)", total, len(factories))

    # ═══════════════════════════════════════════
    #  分级暴露
    # ═══════════════════════════════════════════

    def build_tool_schemas(
        self,
        agent_id: str | None = None,
        agent_allowed_tools: set[str] | None = None,
        messages: list[dict] | None = None,
        skill_allowed_tools: tuple[str, ...] | None = None,
        *,
        tool_schema_tokens: int | None = None,
    ) -> list[ToolSchema]:
        """构建当前轮暴露给 LLM 的工具 schema 列表。"""
        # 构建 ExposureContext
        ctx = ExposureContext(
            agent_id=agent_id,                          # 当前agent 的 ID，主要用于白名单匹配
            agent_allowed_tools=agent_allowed_tools,    # agent级白名单（only allow these tools）
            skill_allowed_tools=skill_allowed_tools,    # skills 激活时的工具白名单
            enabled_cache=self._enabled_cache,          # 工具的动态可用性缓存，里面存储的是当前可用的tools有哪些
        )

        result = self._schema_builder.build(ctx, tool_schema_tokens=tool_schema_tokens)
        self._deferred_summaries = result.deferred_catalog

        logger.info("[facade] build_tool_schemas 返回 %d 个 schema", len(result.schemas))
        return result.schemas

    def get_deferred_summaries(self) -> list[dict]:
        """获取 DEFERRED 未发现工具的摘要列表。"""
        return list(self._deferred_summaries)

    def get_deferred_tool_catalog(self) -> list[dict]:
        """获取全量 DEFERRED 工具目录（对 discovered 免疫）。

        返回的名称使用 safe_name，确保 system prompt 中的
        <available_tools> 与 search_tools enum 一致。
        """
        from .safe_name import to_safe_name

        catalog: list[dict] = []
        for full_name, tool in self._store.items():
            if tool.tier != ToolTier.ALWAYS:
                catalog.append({
                    "name": to_safe_name(full_name),
                    "when_to_use": tool.when_to_use,
                })
        catalog.sort(key=lambda d: d["name"])
        return catalog

    def promote_to_discovered(self, tool_name: str, step_n: int) -> None:
        """将指定 DEFERRED Tool 标记为已发现（写入 DiscoveryManager）。

        tool_name 可能是 safe_name（来自 LLM）或原始 full_name，
        store.get() 支持双向查找，兜底使用 tool.full_name 确保 discovery key 统一。
        """
        tool = self._store.get(tool_name)
        if tool is None:
            return
        if tool.tier != ToolTier.ALWAYS:
            self._discovery.discover(tool.full_name, step_n)

    # ═══════════════════════════════════════════
    #  工具执行
    # ═══════════════════════════════════════════

    async def execute_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """执行工具。永远返回 ToolResult，不抛异常。"""
        # 查找工具
        tool = self._store.get(tool_name)
        if tool is None:
            logger.info("[facade] 工具未注册: '%s'", tool_name)
            return ToolResult(
                success=False,
                error=f"Tool '{tool_name}' 未注册",
                tool_name=tool_name,
            )

        # 构建 GuardChain
        guard_chain = GuardChain(guards=[
            EnabledGuard(self._enabled_cache),
            AgentWhitelistGuard(),
            TrustLevelGuard(),
            DiscoveryGuard(self._discovery),
        ])

        # 前置检查
        rejection = guard_chain.check_all(tool, args, context)
        if rejection is not None:
            logger.info("[facade] 前置检查被拒绝: %s", rejection.error)
            return rejection

        # 参数清洗：过滤多余参数 + 类型强制转换（在 Schema 校验之前）
        # LLM 可能传入不存在的参数名（如 time_range/num_results）或字符串类型值（如 "5"），
        # 必须先清洗再校验，否则 _validate_args 的 additionalProperties: false 会直接拒绝。
        args, _ = self._executor._filter_extra_args(tool, args)
        args, _ = self._executor._coerce_args(tool, args)

        # 参数 JSON Schema 校验
        validation_error = self._validate_args(tool, args)
        if validation_error:
            logger.info("[facade] Schema 校验失败: %s", validation_error)
            return ToolResult(
                success=False,
                error=f"Tool '{tool_name}' 参数校验失败: {validation_error}",
                tool_name=tool_name,
            )

        # 执行
        result = await self._executor.execute(tool, args, context)

        # 发现状态维护（使用 tool.full_name 确保 discovery key 统一）
        if result.success and tool.tier != ToolTier.ALWAYS:
            self._discovery.discover(tool.full_name, context.step_n)

        logger.info(
            "[facade] execute_tool 完成 | tool=%s | success=%s",
            tool.full_name, result.success,
        )
        return result

    # ═══════════════════════════════════════════
    #  动态可用性
    # ═══════════════════════════════════════════

    async def update_enabled_tools(
        self,
        context: ToolContext | None = None,
        *,
        is_circuit_tripped: Any | None = None,
    ) -> None:
        """每轮开始前调用，并发重新计算所有工具的可用性。"""
        async def _check_one(name: str, tool: Tool) -> tuple[str, bool]:
            if is_circuit_tripped is not None and is_circuit_tripped(name):
                if context and self._hooks:
                    self._hooks.on_tool_disabled(
                        tool_name=name, reason="circuit_breaker_open",
                        run_id=context.run_id, session_id=context.session_id,
                    )
                return (name, False)

            if tool.is_enabled is not None and callable(tool.is_enabled):
                try:
                    if context is None:
                        return (name, True)
                    result = tool.is_enabled(context)
                    if asyncio.iscoroutine(result):
                        result = await result
                    enabled = bool(result)
                    if not enabled and context and self._hooks:
                        self._hooks.on_tool_disabled(
                            tool_name=name, reason="is_enabled returned False",
                            run_id=context.run_id, session_id=context.session_id,
                        )
                    return (name, enabled)
                except Exception as exc:
                    logger.warning("工具 '%s' is_enabled() 异常: %s", name, exc)
                    return (name, False)
            else:
                return (name, True)

        results = await asyncio.gather(
            *[_check_one(name, tool) for name, tool in self._store.items()]
        )
        self._enabled_cache = dict(results)

    # ═══════════════════════════════════════════
    #  查询 API
    # ═══════════════════════════════════════════

    def get_tool(self, tool_name: str) -> Tool | None:
        return self._store.get(tool_name)

    def list_tools(self) -> list[Tool]:
        return self._store.list_all()

    def list_tool_names(self) -> list[str]:
        return self._store.list_names()

    # ═══════════════════════════════════════════
    #  内部辅助
    # ═══════════════════════════════════════════

    def _validate_args(self, tool: Tool, args: dict) -> str | None:
        """使用 JSON Schema 校验参数结构。"""
        try:
            import jsonschema
            schema = self._to_serializable(tool.input_schema)
            jsonschema.validate(instance=args, schema=schema)
            return None
        except ImportError:
            _jsonschema_missing_warned = getattr(ToolRegistry, "_jsonschema_missing_warned", False)
            if not _jsonschema_missing_warned:
                logger.warning(
                    "jsonschema 未安装，工具参数将跳过 JSON Schema 校验。"
                    "安装以获得更严格的参数校验：pip install jsonschema"
                )
                ToolRegistry._jsonschema_missing_warned = True
            return None
        except Exception as e:
            return str(e)

    @staticmethod
    def _to_serializable(obj: Any) -> Any:
        """深度转换 MappingProxyType → dict。"""
        if isinstance(obj, builtin_types.MappingProxyType):
            return {k: ToolRegistry._to_serializable(v) for k, v in obj.items()}
        if isinstance(obj, dict):
            return {k: ToolRegistry._to_serializable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [ToolRegistry._to_serializable(item) for item in obj]
        return obj


def create_tool_registry(
    budget: ToolBudget | None = None,
    hooks: AgentHooks | None = None,
) -> ToolRegistry:
    """创建 ToolRegistry 实例。"""
    registry = ToolRegistry(budget=budget)
    if hooks:
        registry.set_hooks(hooks)
    return registry
