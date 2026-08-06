"""pandaren/tool/definition/tool_policy.py — 工具安全与行为策略（纯静态配置）。

与 ToolLifecycle 的分工：
  ToolPolicy    = 静态声明性规则（这个工具是什么、受什么限制）
  ToolLifecycle = 动态执行阶段钩子（在执行的哪个阶段做什么）

原 ToolPolicy 中的 is_enabled / error_formatter 已迁移到 ToolLifecycle。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..types import SensitivityLevel, CircuitBreakerConfig
from ...identity.models import SensitivePermission, TrustLevel


@dataclass(frozen=True)
class ToolPolicy:
    """工具安全与行为策略（纯静态配置，零 Callable）。

    sensitivity 无默认值，强制开发者显式声明。
    audit_required 默认 False，需要时可覆盖为 True。
    其余字段提供安全默认值。

    设计思路:
        - 将安全、访问控制、执行限制等横切关注点从 Tool 定义中分离
        - 核心安全字段无默认值，遵循"默认安全"原则
        - 所有字段均为声明性值（enum / bool / int / None），不包含动态回调
    """

    # ── 安全声明 ──
    sensitivity: SensitivityLevel  # 必填，无默认值（E4: fail-safe）
    audit_required: bool = False   # 必填给默认，可覆盖（O2: 审计链路不可绕过）
    is_reversible: bool = True
    is_idempotent: bool = True

    # ── 访问控制 ──
    trust_level_required: TrustLevel = TrustLevel.SUB_AGENT
    agent_whitelist: frozenset[str] | None = None
    sensitive_permission: SensitivePermission | None = None

    # ── 执行限制 ──
    max_calls_per_turn: int | None = None
    max_output_bytes: int | None = None
    circuit_breaker: CircuitBreakerConfig | None = None
    halt_on_failure: bool = False

    # ── 工具性质 ──
    read_only: bool = False

    # ── 输出控制 ──
    default_result_limit: int | None = None
    supports_offset_pagination: bool = False

    # ── 交互型工具 ──
    requires_user_interaction: bool = False
    # True → 工具执行前暂停 Agent Loop，等待用户交互后恢复执行。
    # 用于 ask_user 等需要用户交互的工具。
