"""pandaren/tool/execution/executor.py — 工具执行器。

完整的执行生命周期：
  1. Pre-Validate    → ToolLifecycle.validate_input(args, ctx)
  2. Execute         → tool.executor(ctx, **args)
  3. Format for LLM  → ToolLifecycle.format_result_for_llm(data, name)
  4. Error Format    → ToolLifecycle.error_formatter(exc, name)

前置检查（GuardChain）由 facade.execute_tool 负责，不在此处。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import types as builtin_types
from typing import Any

from ..definition.tool import Tool
from ..definition.tool_result import ToolResult
from ..definition.context import ToolContext

logger = logging.getLogger("pandaren.tool.execution.executor")


class ToolExecutor:
    """工具执行器。按 4 阶段生命周期执行工具，永远返回 ToolResult。"""

    async def execute(self, tool: Tool, args: dict, context: ToolContext) -> ToolResult:
        """执行工具，永远返回 ToolResult，不抛异常。"""
        tool_name = tool.full_name
        lifecycle = tool.lifecycle

        # 过滤多余参数（LLM 幻觉容错）
        args, filtered_keys = self._filter_extra_args(tool, args)

        # 类型强制转换（LLM 可能传字符串 "20" 而非整数 20）
        args, coerced_info = self._coerce_args(tool, args)

        # 构建参数修正提示（帮助 LLM 学习正确参数名和类型）
        _fix_hint = ""
        if filtered_keys:
            _fix_hint += f"[参数修正] 已自动忽略无效参数：{', '.join(filtered_keys)}。"
        if coerced_info:
            _fix_hint += f"[类型修正] 已将以下字符串转为数字：{', '.join(coerced_info)}。"
        if _fix_hint:
            _fix_hint += " 正确参数名请参考工具声明的 input_schema。\n\n"

        # ── Phase 1: Pre-Validate ──
        if lifecycle.validate_input:
            validation = lifecycle.validate_input(args, context)
            if validation is not None and not validation.valid:
                return ToolResult(
                    success=False,
                    error=validation.message,
                    tool_name=tool_name,
                )

        # ── Phase 2: Execute ──
        logger.info(
            "[execute] Phase 2 | tool=%s | agent_id=%s | step_n=%d",
            tool_name, context.agent_id, context.step_n,
        )

        try:
            if asyncio.iscoroutinefunction(tool.executor):
                raw = await tool.executor(context, **args)
            else:
                # 同步工具丢线程池执行，避免阻塞事件循环：
                # 否则 bash 等工具卡住时，流式输出 / STOP 取消 /
                # step_timeout 兜底会全部失效，前端表现为"死透"
                loop = asyncio.get_running_loop()
                raw = await loop.run_in_executor(
                    None, functools.partial(tool.executor, context, **args)
                )
            if asyncio.iscoroutine(raw):
                raw = await raw

            if isinstance(raw, ToolResult):
                result = ToolResult(
                    success=raw.success,
                    data=raw.data,
                    error=raw.error,
                    halt=raw.halt,
                    tool_name=tool_name,
                    _discovered_tools=raw._discovered_tools,
                    plan_complete=raw.plan_complete,
                    plan_path=raw.plan_path,
                )
               
            else:
                result = ToolResult(success=True, data=raw, tool_name=tool_name)

        except Exception as exc:
            error_msg = self._format_error(tool, exc)
            logger.warning("工具 '%s' 执行异常: %s", tool_name, exc)
            result = ToolResult(
                success=False, error=error_msg, tool_name=tool_name,
            )
            return result

        # ── Phase 3: Format for LLM ──
        if isinstance(result.data, str):
            pass  # 已是 str，无需格式化
        elif isinstance(result.data, dict):
            pass  # dict 保留原样（plan_path/plan_content 等结构化数据）
        elif hasattr(result.data, '__tool_format_for_llm__'):
            result.data = result.data.__tool_format_for_llm__()
        elif lifecycle.format_result_for_llm:
            result.data = lifecycle.format_result_for_llm(result.data, tool_name)
        else:
            result.data = str(result.data)

        # ── Phase 4: Truncation ──
        max_bytes = tool.policy.max_output_bytes
        if max_bytes and isinstance(result.data, str):
            encoded = result.data.encode("utf-8")
            if len(encoded) > max_bytes:
                result.data = encoded[:max_bytes].decode("utf-8", errors="replace")
                result.truncated = True

        # 注入参数修正提示（帮助 LLM 学习正确参数名和类型）
        if result.success and _fix_hint and isinstance(result.data, str):
            result.data = _fix_hint + result.data

        return result

    def _filter_extra_args(self, tool: Tool, args: dict) -> tuple[dict, list[str]]:
        """过滤不在 schema properties 中的参数。
        
        Returns:
            (过滤后的 args, 被移除的参数名列表)
        """
        schema = self._to_serializable(tool.input_schema)
        allowed_keys = set(schema.get("properties", {}).keys()) if isinstance(schema, dict) else None
        removed: list[str] = []
        if allowed_keys is not None:
            extra_keys = set(args.keys()) - allowed_keys
            if extra_keys:
                logger.warning(
                    "[execute] 过滤多余参数 | tool=%s | extra=%s",
                    tool.full_name, extra_keys,
                )
                removed = sorted(extra_keys)
                args = {k: v for k, v in args.items() if k in allowed_keys}
        return args, removed

    def _coerce_args(self, tool: Tool, args: dict) -> tuple[dict, list[str]]:
        """根据 JSON Schema 类型声明对参数做基础类型强制转换。

        LLM 有时会把 integer 值序列化为字符串（如 "20" 而非 20），
        这会导致函数调用时发生 TypeError。此处根据 schema 的 type 字段
        做防御性转换，不依赖 jsonschema 安装。

        支持的类型转换：
          - "integer" / "number": str → int / float
          - "boolean": str "true"/"false" → bool, int 0/1 → bool

        Returns:
            (转换后的 args, 被转换的参数名列表)
        """
        schema = self._to_serializable(tool.input_schema)
        if not isinstance(schema, dict):
            return args, []
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return args, []

        coerced: dict[str, Any] = {}
        changed: list[str] = []
        for key, value in args.items():
            prop_schema = properties.get(key, {})
            if not isinstance(prop_schema, dict):
                coerced[key] = value
                continue

            schema_type = prop_schema.get("type")
            new_value = self._coerce_value(value, schema_type)
            if new_value is not value:
                changed.append(key)
            coerced[key] = new_value

        return coerced, changed

    @staticmethod
    def _coerce_value(value: Any, schema_type: Any) -> Any:
        """将单个值转换为 schema 声明的类型。"""
        if schema_type == "integer":
            if isinstance(value, str):
                try:
                    return int(value)
                except (ValueError, TypeError):
                    logger.warning("[coerce] 无法将 '%s' 转为 integer，保留原值", value)
            if isinstance(value, float) and value == int(value):
                return int(value)
            if isinstance(value, bool):  # bool 是 int 的子类，排在后面
                return int(value)
        elif schema_type == "number":
            if isinstance(value, str):
                try:
                    return float(value)
                except (ValueError, TypeError):
                    logger.warning("[coerce] 无法将 '%s' 转为 number，保留原值", value)
            if isinstance(value, bool):
                return float(value)
        elif schema_type == "boolean":
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("true", "1", "yes"):
                    return True
                if lowered in ("false", "0", "no", ""):
                    return False
                logger.warning("[coerce] 无法将 '%s' 转为 boolean，保留原值", value)
            if isinstance(value, (int, float)):
                return bool(value)
        return value

    def _format_error(self, tool: Tool, exc: Exception) -> str:
        """格式化错误信息。"""
        formatter = tool.lifecycle.error_formatter
        if formatter:
            try:
                return formatter(exc, tool.full_name)
            except Exception:
                pass
        return f"工具 '{tool.full_name}' 执行失败: {type(exc).__name__}: {exc}"

    @staticmethod
    def _to_serializable(obj: Any) -> Any:
        """深度转换 MappingProxyType → dict。"""
        if isinstance(obj, builtin_types.MappingProxyType):
            return {k: ToolExecutor._to_serializable(v) for k, v in obj.items()}
        if isinstance(obj, dict):
            return {k: ToolExecutor._to_serializable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [ToolExecutor._to_serializable(item) for item in obj]
        return obj
