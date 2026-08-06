"""pandaren/tool/exposure/schema_builder.py — 构建每轮暴露给 LLM 的 tool schemas。

职责单一：从 ToolStore 读取工具定义，经过 GateChain 过滤，
按三段式排列，最终由 ToolBudget 裁剪。
"""

from __future__ import annotations

import logging
import types as builtin_types
from dataclasses import dataclass, field
from typing import Any

from ..definition.tool import Tool
from ..definition.tool_schema import ToolSchema
from ..registry.store import ToolStore
from ..registry.discovery import DiscoveryManager
from ..types import ToolTier
from ..safe_name import to_safe_name
from .gate_chain import GateChain, ExposureContext
from .budget import ToolBudget

logger = logging.getLogger("pandaren.tool.exposure.schema_builder")


@dataclass
class BuildStats:
    """调试统计。"""
    always_count: int = 0
    deferred_found_count: int = 0
    deferred_unfound_count: int = 0
    filtered_count: int = 0


@dataclass
class BuildResult:
    """schema 构建结果。"""
    schemas: list[ToolSchema] = field(default_factory=list)     # 构建完成的tool_schema，发送给LLM
    deferred_catalog: list[dict] = field(default_factory=list)  # 延迟未加载 + 延迟已加载的说明内容（name + when_to_use），存入systemprompt中，一般不会改变，以防影响缓存命中率
    search_enum: list[str] = field(default_factory=list)        # 可被search_tool 搜索的tools枚举值，一定是延迟工具范围内的。如果is_enable的tools是不可能在这个枚举里面的。
    stats: BuildStats = field(default_factory=BuildStats)       # 构建状态


class SchemaBuilder:
    """构建每轮暴露给 LLM 的 tool schemas。

    依赖注入：
      - store: ToolStore（读取工具定义）
      - discovery: DiscoveryManager（读取发现状态）
      - gate_chain: GateChain（过滤）
      - budget: ToolBudget（裁剪）
    """

    def __init__(
        self,
        store: ToolStore,
        discovery: DiscoveryManager,
        gate_chain: GateChain,
        budget: ToolBudget,
    ) -> None:
        self._store = store
        self._discovery = discovery
        self._gate_chain = gate_chain
        self._budget = budget

    def build(
        self,
        ctx: ExposureContext,
        tool_schema_tokens: int | None = None,
    ) -> BuildResult:
        """构建当前轮的工具 schema。

        三段式排列：
          ① ALWAYS（不含 search_tools）
          ② search_tools（带动态 enum）
          ③ 延迟已发现（DEFERRED + discovered）
        """
        result = BuildResult()

        # 获取所有工具并通过门链过滤
        all_tools = self._store.items()
        passed_tools = self._gate_chain.filter(all_tools, ctx)

        result.stats.filtered_count = len(all_tools) - len(passed_tools)

        # 三段分类
        schemas_always: list[ToolSchema] = []
        schemas_deferred_found: list[ToolSchema] = []
        deferred_unfound_summaries: list[dict] = []
        deferred_unfound_names: list[str] = []

        for full_name, tool in passed_tools:
            if tool.tier == ToolTier.ALWAYS:
                if full_name != "search_tools":
                    schemas_always.append(self._to_schema(tool))
                    result.stats.always_count += 1
            elif self._discovery.is_discovered(full_name):
                # 延迟已发现 → 完整 schema
                schemas_deferred_found.append(self._to_schema(tool))
                # 延迟虽然已经发现了，但是还是需要把摘要也保留下来，主要是用于保持缓存命中不被破坏
                deferred_unfound_summaries.append({
                    "name": to_safe_name(full_name),
                    "when_to_use": tool.when_to_use,
                })
                result.stats.deferred_found_count += 1
            else:
                # 延迟未发现 → 仅摘要
                deferred_unfound_summaries.append({
                    "name": to_safe_name(full_name),
                    "when_to_use": tool.when_to_use,
                })
                deferred_unfound_names.append(to_safe_name(full_name))
                result.stats.deferred_unfound_count += 1

        # 各段内按 name 字母序排序
        schemas_always.sort(key=lambda s: s.name)
        schemas_deferred_found.sort(key=lambda s: s.name)
        deferred_unfound_summaries.sort(key=lambda d: d["name"])
        deferred_unfound_names.sort()

        # 三段拼接
        schemas: list[ToolSchema] = list(schemas_always)

        # search_tools
        search_tool = self._store.get("search_tools")
        if search_tool is not None:
            # 检查 search_tools 是否通过了门链
            search_passed = any(fn == "search_tools" for fn, _ in passed_tools)
            if search_passed:
                schemas.append(
                    self._build_search_tool_schema(search_tool, deferred_unfound_names)
                )

        schemas.extend(schemas_deferred_found)

        # Token 预算裁剪
        schemas = self._budget.enforce(schemas, tool_schema_tokens=tool_schema_tokens)

        result.schemas = schemas
        result.deferred_catalog = deferred_unfound_summaries
        result.search_enum = deferred_unfound_names

        logger.info(
            "[schema] 汇总 | ALWAYS=%d | 延迟已发现=%d | 延迟未发现=%d | filtered=%d | enum=%d",
            result.stats.always_count,
            result.stats.deferred_found_count,
            result.stats.deferred_unfound_count,
            result.stats.filtered_count,
            len(deferred_unfound_names),
        )

        # 非 ASCII schema 名将导致 LLM API 400，提前告警
        schema_names = [s.name for s in result.schemas]
        non_ascii = [n for n in schema_names if not n.isascii()]
        if non_ascii:
            logger.warning(
                "[schema] 以下 schema 名称包含非 ASCII 字符（将导致 400）: %s",
                non_ascii,
            )

        return result

    def _to_schema(self, tool: Tool) -> ToolSchema:
        """将 Tool 转换为 ToolSchema（使用 LLM-safe 名称）。"""
        params = self._to_serializable(tool.input_schema)
        safe_name = to_safe_name(tool.full_name)
        return ToolSchema(
            name=safe_name,
            description=tool.description,
            parameters=params,
        )

    # 这个函数主要目的是动态生成search_tools可搜索的tools的范围（枚举值），这主要是为了限定search_tools不要乱搜索，指定范围
    def _build_search_tool_schema(
        self,
        tool: Tool,
        deferred_names: list[str],
    ) -> ToolSchema:
        """为 search_tools 构建带动态 enum 的 ToolSchema。"""
        tool_name_prop: dict = {
            "type": "string",
            "description": "要加载的工具名称，必须从下列候选中原样选择，区分大小写",
        }
        if deferred_names:
            tool_name_prop["enum"] = deferred_names

        return ToolSchema(
            name=tool.full_name,
            description=tool.description,
            parameters={
                "type": "object",
                "properties": {"tool_name": tool_name_prop},
                "required": ["tool_name"],
            },
        )

    @staticmethod
    def _to_serializable(obj: Any) -> Any:
        """深度转换 MappingProxyType → dict。"""
        if isinstance(obj, builtin_types.MappingProxyType):
            return {k: SchemaBuilder._to_serializable(v) for k, v in obj.items()}
        if isinstance(obj, dict):
            return {k: SchemaBuilder._to_serializable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [SchemaBuilder._to_serializable(item) for item in obj]
        return obj
