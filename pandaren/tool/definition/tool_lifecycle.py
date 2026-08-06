"""pandaren/tool/definition/tool_lifecycle.py — 工具执行阶段的行为钩子。

与 ToolPolicy 的分工：
  ToolPolicy    = 静态声明性规则（这个工具是什么、受什么限制）
  ToolLifecycle = 动态执行阶段钩子（在执行的哪个阶段做什么）

所有钩子默认 None，框架自动使用默认行为。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import ToolContext
    from .tool_result import ValidationResult


@dataclass(frozen=True)
class ToolLifecycle:
    """工具在暴露/执行/错误三个阶段的动态行为钩子。

    Claude Code 对标：
      is_enabled            → (分散在 GateChain/permissions 中)
      validate_input        → Tool.validateInput()
      format_result_for_llm → Tool.mapToolResultToToolResultBlockParam()
      error_formatter       → (内置在 ToolExecutor 的错误处理中)

    所有钩子都是可选的（None = 使用框架默认行为）。
    """

    # ── 暴露阶段：工具是否对当前 Agent 可见 ──
    # GateChain/GuardChain 通过 update_enabled_tools 缓存后消费此值。
    # - None: 始终可见
    # - Callable: 根据 ToolContext 动态判断
    # 示例: lambda ctx: ctx.metadata.get("user_role") == "admin"
    is_enabled: Callable[["ToolContext"], bool] | None = None

    # ── 执行前：输入校验 ──
    # 在 executor() 调用前执行。失败则直接返回 ToolResult(success=False)，
    # LLM 无需消耗一轮调用才知道参数错误。
    #
    # 签名: (args: dict, ctx: ToolContext) -> ValidationResult | None
    #   - None: 校验通过，继续执行
    #   - ValidationResult: 校验失败，返回给 LLM（应包含建议信息）
    #
    # 用途:
    #   - 路径存在性校验 + Did you mean... 建议
    #   - 参数合法性检查（如 head_limit 必须 >= 0）
    #   - 业务级预检（如 "目标文件已被锁定"）
    validate_input: Callable[[dict, "ToolContext"], "ValidationResult | None"] | None = None

    # ── 执行后：结果格式化（给 LLM 看的文本）──
    # 将工具返回的 data 转换为 LLM 可读的文本。
    #
    # 签名: (data: Any, tool_name: str) -> str
    #
    # 优先级链：
    #   data.__tool_format_for_llm__() → format_result_for_llm(data, name) → str(data)
    #
    # 用途:
    #   - GrepResult → "Found 3 files, limit: 250\nsrc/foo.py\n..."
    #   - 控制 LLM 看到什么（隐藏内部元信息，展示可操作数据）
    format_result_for_llm: Callable[[Any, str], str] | None = None

    # ── 错误时：异常格式化 ──
    # 工具执行抛出异常时，将异常转换为 LLM 可读的错误消息。
    #
    # 签名: (Exception, tool_name: str) -> str
    # - None: 使用默认错误格式 "工具 'X' 执行失败: ExceptionType: message"
    #
    # 用途:
    #   - 提供更友好的错误消息
    #   - 敏感信息脱敏
    error_formatter: Callable[[Exception, str], str] | None = None
