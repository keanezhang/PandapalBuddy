"""pandaren/tool/decorator.py — @tool.function 装饰器实现。

tool 作为命名空间对象（非函数），当前子类型为 tool.function。
预留 tool.mcp / tool.remote 等扩展。
"""

from __future__ import annotations

from typing import Any, Callable

from .types import ToolTier, SensitivityLevel
from .definition.tool import Tool
from .definition.tool_policy import ToolPolicy
from .definition.tool_lifecycle import ToolLifecycle
from .schema_inference import parse_docstring, infer_input_schema


class _ToolNamespace:
    """tool 命名空间对象。

    用法：
      @tool.function(when_to_use="获取指定城市的天气信息")
      def get_weather(ctx: ToolContext, city: str) -> str:
          ...

    高级用法：
      @tool.function(
          when_to_use="搜索文件内容",
          lifecycle=ToolLifecycle(validate_input=_validate_grep_input),
          llm_guide="始终使用本工具而非 bash 中调用 rg...",
      )
      def grep(ctx: ToolContext, pattern: str, ...) -> str:
          ...

    预留扩展：
      @tool.mcp(server_url=..., ...)
      @tool.remote(endpoint=..., ...)
    """

    def function(
        self,
        *,
        when_to_use: str,
        # ── 可选字段 ──
        sensitivity: SensitivityLevel | None = None,  # 未传 policy 时必填；已传 policy 则忽略
        tier: ToolTier = ToolTier.DEFERRED,
        name: str | None = None,
        description: str | None = None,
        version: str = "1.0.0",
        namespace: str | None = None,
        tags: tuple[str, ...] | list[str] = (),
        output_schema: dict[str, Any] | None = None,
        policy: ToolPolicy | None = None,
        lifecycle: ToolLifecycle | None = None,
        llm_guide: str | None = None,
        progress_label: str | None = None,
    ) -> Callable:
        """函数型工具装饰器。

        从 docstring + type hints 自动生成 input_schema。
        安全策略通过 policy 传入（推荐），或通过 sensitivity 快速声明。

        Args:
            when_to_use: 工具适用场景描述（必填）。
            sensitivity: 工具敏感度等级（E4 必填）。已传 policy 时忽略，未传 policy 时必填。
            tier: 分级，默认 DEFERRED。
            name: 工具名，默认使用函数名。
            description: 简短描述，默认从 docstring 第一行提取。
            version: 版本号。
            namespace: 命名空间分组。
            tags: 情境标签。
            output_schema: 输出 schema。
            policy: 安全/行为策略（推荐方式，sensitivity 在其中声明）。
            lifecycle: 执行阶段钩子，不传则使用默认 ToolLifecycle()。
            llm_guide: LLM 专属使用指南（纠正错误/最佳实践），自动追加到 description 尾部。
            progress_label: 进度展示模板，支持 {arg_name} 占位符。
        """
        def decorator(func: Callable) -> Tool:
            tool_name = name or func.__name__
            desc, param_docs = parse_docstring(func)
            tool_desc = description or desc
            input_schema = infer_input_schema(func, param_docs)

            # policy 优先；未传 policy 时用 sensitivity 构造
            if policy is None:
                if sensitivity is None:
                    raise ValueError(
                        f"工具 '{tool_name}' 缺少 sensitivity："
                        f"请显式传入 policy=ToolPolicy(sensitivity=...) 或 sensitivity=..."
                    )
                tool_policy = ToolPolicy(sensitivity=sensitivity)
            else:
                tool_policy = policy

            tool_def = Tool(
                name=tool_name,
                description=tool_desc,
                executor=func,
                policy=tool_policy,
                input_schema=input_schema,
                tier=tier,
                when_to_use=when_to_use,
                namespace=namespace,
                version=version,
                output_schema=output_schema,
                tags=tuple(tags),
                lifecycle=lifecycle or ToolLifecycle(),
                llm_guide=llm_guide,
                progress_label=progress_label,
            )
            return tool_def

        return decorator


# 全局命名空间对象
tool = _ToolNamespace()
