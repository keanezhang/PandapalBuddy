"""pandaren/behavior/execution_limits.py — 执行上限（完全冻结）

HC5：创建后所有字段冻结，不提供任何修改方法。
"""

from __future__ import annotations

import logging

from .exceptions import BehaviorConfigError

logger = logging.getLogger("pandaren.behavior.execution_limits")

# ── 默认值常量 ─────────────────────────────────────────────────────────────────
DEFAULT_MAX_STEPS: int = 30          # 单次 run 的最大 agent 步数
DEFAULT_STEP_TIMEOUT: float = 120.0  # 单步（LLM 调用 + 工具执行）超时时间（秒）
DEFAULT_TOTAL_TIMEOUT: float = 600.0 # 整个 run 的总超时时间（秒）


class ExecutionLimits:
    """Agent 执行上限声明对象。创建后完全不可变（HC5）。"""

    # 注：花费预算（原 max_cost_usd）已上移到应用层（见 behavior/step_guard.py）：
    # SDK 不再持有金额阈值，费用超限只是通用 StepGuard 的一种停机理由，
    # 由 `.behavior(step_guard=...)` 注入的守卫全权判断。
    __slots__ = ("_max_steps", "_step_timeout", "_total_timeout")

    def __init__(
        self,
        max_steps: int = DEFAULT_MAX_STEPS,
        step_timeout: float = DEFAULT_STEP_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    ) -> None:
        if not isinstance(max_steps, int) or max_steps <= 0:
            raise BehaviorConfigError(
                f"execution_limits.max_steps 必须是正整数，当前值: {max_steps!r}"
            )
        if step_timeout <= 0:
            raise BehaviorConfigError(
                f"execution_limits.step_timeout 必须 > 0，当前值: {step_timeout}"
            )
        if total_timeout <= 0:
            raise BehaviorConfigError(
                f"execution_limits.total_timeout 必须 > 0，当前值: {total_timeout}"
            )
        if step_timeout > total_timeout:
            raise BehaviorConfigError(
                f"execution_limits.step_timeout ({step_timeout}s) 不能大于 "
                f"total_timeout ({total_timeout}s)"
            )

        object.__setattr__(self, "_max_steps", max_steps)
        object.__setattr__(self, "_step_timeout", step_timeout)
        object.__setattr__(self, "_total_timeout", total_timeout)

        logger.info(
            "execution_limits: created max_steps=%d, step_timeout=%.1fs, total_timeout=%.1fs",
            max_steps, step_timeout, total_timeout,
        )

    def __setattr__(self, name: str, value: object) -> None:
        logger.warning("execution_limits: 尝试修改冻结字段 '%s'，已拒绝。", name)
        raise PermissionError(
            f"ExecutionLimits 是不可变对象，禁止修改字段 '{name}'。"
        )

    def __delattr__(self, name: str) -> None:
        raise PermissionError(
            f"ExecutionLimits 是不可变对象，禁止删除字段 '{name}'。"
        )

    @property
    def max_steps(self) -> int:
        return object.__getattribute__(self, "_max_steps")

    @property
    def step_timeout(self) -> float:
        return object.__getattribute__(self, "_step_timeout")

    @property
    def total_timeout(self) -> float:
        return object.__getattribute__(self, "_total_timeout")

    def __repr__(self) -> str:
        return (
            f"ExecutionLimits(max_steps={self.max_steps}, "
            f"step_timeout={self.step_timeout}s, "
            f"total_timeout={self.total_timeout}s)"
        )
