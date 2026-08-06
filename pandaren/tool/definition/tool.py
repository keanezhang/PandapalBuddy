"""pandaren/tool/definition/tool.py — Tool 核心定义。

Tool = 身份(name/description/executor) + 策略(ToolPolicy) + 生命周期(ToolLifecycle)。
开发者最少只需提供 name + description + executor 三个参数。
"""

from __future__ import annotations

import copy
import types as builtin_types
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from ..types import ToolTier
from .tool_policy import ToolPolicy
from .tool_lifecycle import ToolLifecycle

# 类型别名
JsonSchema = dict[str, Any]
ErrorFormatter = Callable[[Exception, str], str]


@runtime_checkable
class Executor(Protocol):
    """工具执行体协议。

    第一个参数必须是 ToolContext，其余为工具参数。
    支持同步和异步两种形式。
    """
    def __call__(self, ctx: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, kw_only=True)
class Tool:
    """工具定义（开发者面向的核心模型）。

    frozen=True 保证注册后所有字段不可变。
    kw_only=True 允许 required 字段与 optional 字段自由排列。

    三层组合：
      - 身份层：name, description, executor
      - 规则层：ToolPolicy（声明性安全/访问/限制规则）
      - 行为层：ToolLifecycle（执行各阶段动态钩子）

    最简注册：name + description + executor + policy（4 参数，policy 中 sensitivity 必填）。
    """

    # ── 必填（5 个）──
    name: str
    description: str
    executor: Callable[..., Any]
    policy: ToolPolicy  # 必填，sensitivity 无默认值，强制显式声明（E4）
    input_schema: Any   # 必填，由 decorator/loader 调用 infer_input_schema() 自动推导
    when_to_use: str    # 必填，无默认值，不可为空（描述工具的适用场景）
    tier: ToolTier = ToolTier.DEFERRED  # 必填，但是给默认值 DEFERRED 大部分工具应为延迟加载；框架基础设施显式设为 ALWAYS

    # ── 生命周期（动态钩子）──
    lifecycle: ToolLifecycle = field(default_factory=ToolLifecycle)

    # ── LLM 专属使用指南 ──
    # 在 __post_init__ 中自动追加到 description 尾部。
    # 开发者只需在 @tool.function(llm_guide="...") 中填写使用指导，
    # 最终发给 LLM 的 description = 原始描述 + "\n\n" + llm_guide。
    # 用途：纠正 LLM 的常见错误用法、告知最佳实践、禁止事项。
    llm_guide: str | None = None

    # ── 进度展示 ──
    # 当 LLM 发起 tool_calls 但 content 为空时，用此模板生成用户可读的进度文本。
    # 支持占位符：{arg_name} 从 tool_call arguments 中取值。
    # 示例："搜索「{query}」" / "读取文件「{file_path}」" / "写入文件「{file_path}」"
    # None = 不生成进度文本（由前端用默认格式）
    progress_label: str | None = None

    # ── 可选元信息 ──
    namespace: str | None = None
    version: str = "1.0.0"
    output_schema: Any | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # ── llm_guide 追加到 description ──
        if self.llm_guide:
            merged = self.description
            if not merged.endswith("\n"):
                merged += "\n\n"
            else:
                merged += "\n"
            merged += self.llm_guide
            object.__setattr__(self, "description", merged)

        # input_schema 深拷贝保护
        if isinstance(self.input_schema, dict):
            object.__setattr__(
                self, 'input_schema',
                builtin_types.MappingProxyType(copy.deepcopy(self.input_schema))
            )
        if isinstance(self.output_schema, dict):
            object.__setattr__(
                self, 'output_schema',
                builtin_types.MappingProxyType(copy.deepcopy(self.output_schema))
            )

        # read_only=True 且 is_reversible=False 矛盾检查
        if self.policy.read_only and not self.policy.is_reversible:
            raise ValueError(
                f"Tool '{self.name}': policy.read_only=True 但 policy.is_reversible=False，"
                f"只读工具不产生副作用，is_reversible 应为 True"
            )

    @property
    def full_name(self) -> str:
        """完整工具名（含命名空间）。"""
        if self.namespace:
            return f"{self.namespace}_{self.name}"
        return self.name

    # ── ToolPolicy 快捷访问 ──

    @property
    def sensitivity(self) -> Any:
        return self.policy.sensitivity

    @property
    def is_reversible(self) -> bool:
        return self.policy.is_reversible

    @property
    def is_idempotent(self) -> bool:
        return self.policy.is_idempotent

    @property
    def audit_required(self) -> bool:
        return self.policy.audit_required

    @property
    def trust_level_required(self) -> Any:
        return self.policy.trust_level_required

    @property
    def agent_whitelist(self) -> frozenset[str] | None:
        return self.policy.agent_whitelist

    @property
    def sensitive_permission(self) -> Any:
        return self.policy.sensitive_permission

    @property
    def max_calls_per_turn(self) -> int | None:
        return self.policy.max_calls_per_turn

    @property
    def max_output_bytes(self) -> int | None:
        return self.policy.max_output_bytes

    @property
    def circuit_breaker(self) -> Any:
        return self.policy.circuit_breaker

    @property
    def halt_on_failure(self) -> bool:
        return self.policy.halt_on_failure

    @property
    def read_only(self) -> bool:
        return self.policy.read_only

    @property
    def requires_user_interaction(self) -> bool:
        return self.policy.requires_user_interaction

    # ── ToolLifecycle 快捷访问 ──

    @property
    def is_enabled(self) -> Any:
        return self.lifecycle.is_enabled

    @property
    def error_formatter(self) -> Any:
        return self.lifecycle.error_formatter

    @property
    def situation_tags(self) -> tuple[str, ...]:
        """兼容属性：映射 tags → situation_tags。"""
        return self.tags
