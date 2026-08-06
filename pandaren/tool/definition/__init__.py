"""pandaren/tool/definition — 工具定义层（纯数据，零依赖）。

对外暴露：
  - Tool: 工具核心定义
  - ToolPolicy: 安全/行为策略（静态规则）
  - ToolLifecycle: 执行阶段钩子（动态行为）
  - ToolResult: 执行结果
  - ToolContext: 执行上下文
  - ToolSchema: 暴露给 LLM 的 schema
  - ToolSearchResult: 搜索结果
  - DiscoveredToolEntry: 发现记录
  - ValidationResult: Pre-validate 校验结果
  - HasLLMFormat: 结构化输出格式化协议
  - JsonSchema, ErrorFormatter: 类型别名
  - Executor: 执行体协议
"""

from .tool import Tool
from .tool_policy import ToolPolicy
from .tool_lifecycle import ToolLifecycle
from .tool_result import ToolResult, DiscoveredToolEntry, ValidationResult, HasLLMFormat
from .tool_schema import ToolSchema, ToolSearchResult
from .context import ToolContext
from .tool import JsonSchema, ErrorFormatter, Executor

__all__ = [
    "Tool",
    "ToolPolicy",
    "ToolLifecycle",
    "ToolResult",
    "DiscoveredToolEntry",
    "ValidationResult",
    "HasLLMFormat",
    "ToolSchema",
    "ToolSearchResult",
    "ToolContext",
    "JsonSchema",
    "ErrorFormatter",
    "Executor",
]
