"""pandaren/tool — 工具层（重构版）。

对外暴露：
  - tool（命名空间对象，@tool.function 装饰器）
  - Tool, ToolPolicy, ToolResult, ToolContext, ToolSchema, ToolSearchResult, DiscoveredToolEntry
  - ToolRegistry, create_tool_registry
  - ToolTier, SensitivityLevel, CircuitState, CircuitBreakerConfig
  - ToolBudget（工具 schema token 预算控制）
  - ToolRegistrationError, ToolValidationWarning
  - load_tool_from_file, load_tools_from_dir（文件加载器）
  - BuiltinToolFactory（内置工具工厂协议）
  - SearchToolFactory, PlanToolFactory,
    SkillToolFactory, AgentToolFactory（内置工具 Factory 实现）
  - ToolStore, DiscoveryManager（注册中心内部组件，需要时可直接使用）
  - GateChain, ExposureContext（暴露层组件）
  - ToolExecutor, GuardChain（执行层组件）
  - JsonSchema, ErrorFormatter, Executor（类型别名/协议）

架构总览：
  definition/     纯数据模型层（零依赖）
  registry/       注册中心（存储 + 发现 + 校验）
  exposure/       暴露策略（门链 + schema 构建 + 预算）
  execution/      执行层（执行器 + 门控链）
  builtin/        内置工具工厂（无状态，不依赖 Registry）
  facade.py       Facade（组合以上组件，对外统一 API）
"""

# ── 枚举与基础类型 ──
from .types import ToolTier, SensitivityLevel, CircuitState, CircuitBreakerConfig

# ── 核心模型（definition 层）──
from .definition import (
    Tool, ToolPolicy, ToolLifecycle, ToolResult, ToolContext,
    ToolSchema, ToolSearchResult, DiscoveredToolEntry,
    ValidationResult, HasLLMFormat,
    JsonSchema, ErrorFormatter, Executor,
)

# ── Facade（对外统一 API）──
from .facade import ToolRegistry, create_tool_registry

# ── 装饰器 ──
from .decorator import tool

# ── Token 预算 ──
from .exposure.budget import ToolBudget

# ── 异常 ──
from .exceptions import ToolRegistrationError, ToolValidationWarning

# ── 文件加载器 ──
from .loader import load_tool_from_file, load_tools_from_dir

# ── 内置工具 Factory ──
from .builtin import (
    BuiltinToolFactory,
    SearchToolFactory,
    PlanToolFactory,
    SkillToolFactory,
    AgentToolFactory,
)

# ── 注册中心组件（高级用法）──
from .registry.store import ToolStore
from .registry.discovery import DiscoveryManager

# ── 暴露层组件 ──
from .exposure.gate_chain import GateChain, ExposureContext

# ── 执行层组件 ──
from .execution.executor import ToolExecutor
from .execution.guard_chain import GuardChain

# ── Schema 推导 ──
from .schema_inference import infer_input_schema, parse_docstring


__all__ = [
    # 枚举与类型
    "ToolTier", "SensitivityLevel", "CircuitState", "CircuitBreakerConfig",
    # 核心模型
    "Tool", "ToolPolicy", "ToolLifecycle", "ToolResult", "ToolContext",
    "ToolSchema", "ToolSearchResult", "DiscoveredToolEntry",
    "ValidationResult", "HasLLMFormat",
    "JsonSchema", "ErrorFormatter", "Executor",
    # Facade
    "ToolRegistry", "create_tool_registry",
    # 装饰器
    "tool",
    # Token 预算
    "ToolBudget",
    # 异常
    "ToolRegistrationError", "ToolValidationWarning",
    # 文件加载器
    "load_tool_from_file", "load_tools_from_dir",
    # 内置工具 Factory
    "BuiltinToolFactory",
    "SearchToolFactory", "PlanToolFactory",
    "SkillToolFactory", "AgentToolFactory",
    # 注册中心组件
    "ToolStore", "DiscoveryManager",
    # 暴露层
    "GateChain", "ExposureContext",
    # 执行层
    "ToolExecutor", "GuardChain",
    # Schema 推导
    "infer_input_schema", "parse_docstring",
]
