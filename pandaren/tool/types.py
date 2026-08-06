"""pandaren/tool/types.py — 枚举与基础类型定义

注意：Permission 和 TrustLevel 定义在 identity.models 中，tool 层复用同一类型。
CircuitBreakerConfig 是 tool 层自有类型（只有 tool 层使用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ToolTier(IntEnum):
    """工具加载分级。

    ALWAYS:   完整 schema 始终出现在每次 API 请求中，数量 ≤ 15
    DEFERRED: 延迟加载，默认只暴露 name + when_to_use，通过 search_tool 发现后注入完整 schema
    """
    ALWAYS = 1
    DEFERRED = 2


class SensitivityLevel(IntEnum):
    """工具敏感度等级（IntEnum，保证大小比较语义正确）。

    使用 IntEnum 而非 str Enum，保证大小比较语义正确（LOW < MEDIUM < HIGH < CRITICAL）。
    不能用 str 枚举做大小比较——Python 字符串按字典序，"low" > "high" 结果为 True，
    会导致不可逆操作的 sensitivity 自动升级逻辑永远不触发（HC1 Bug 修复）。

    LOW:      无风险操作（读取公开数据）→ 直接放行
    MEDIUM:   低风险操作（写入非关键数据）→ 直接放行，记录审计日志（若 audit_required=True）
    HIGH:     高风险操作（删除文件、修改配置）→ 按 agent.behavior.auto_confirm_high 决定是否 HITL
    CRITICAL: 不可逆高危操作（删生产库、部署生产）→ 强制 HITL，无论任何配置
    """
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class CircuitState(IntEnum):
    """熔断器状态（R3 原则）。"""
    CLOSED = 1        # 正常工作
    OPEN = 2          # 熔断中，拒绝调用
    HALF_OPEN = 3     # 冷却期结束，允许试探性调用


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """熔断器配置（R3 原则）。

    failure_threshold: 连续失败次数阈值，达到后触发熔断
    recovery_timeout:  冷却期（秒），熔断后等待多久进入 half-open
    max_recovery_timeout: 冷却期上限（秒），指数退避不超过此值

    校验：failure_threshold <= 0 无意义，注册时 ERROR 拒绝。
    """
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    max_recovery_timeout: float = 300.0

    def __post_init__(self) -> None:
        if self.failure_threshold <= 0:
            raise ValueError(
                f"circuit_breaker.failure_threshold 必须 > 0，"
                f"当前值: {self.failure_threshold}"
            )
        if self.recovery_timeout <= 0:
            raise ValueError("circuit_breaker.recovery_timeout 必须 > 0")
        if self.max_recovery_timeout < self.recovery_timeout:
            raise ValueError(
                "circuit_breaker.max_recovery_timeout 必须 >= recovery_timeout"
            )
