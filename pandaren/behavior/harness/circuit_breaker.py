"""pandaren/behavior/harness/circuit_breaker.py — R3 熔断保护

连续失败达到阈值 → 自动熔断（OPEN）→ 冷却期后进入 HALF_OPEN → 试探调用。
成功 → 恢复（CLOSED），失败 → 重新熔断（冷却期加倍）。

hooks 由 HarnessExecutor.set_hooks() 注入（统一 AgentHooks 协议）。
状态转换事件同步触发 hooks，保证"看得见"。
"""

from __future__ import annotations

import logging
import time

from ...tool.types import CircuitState, CircuitBreakerConfig
from ...tool.definition.tool_result import ToolResult
from ...hook import AgentHooks

logger = logging.getLogger("pandaren.behavior.harness.circuit_breaker")


class _CircuitBreakerState:
    """单个工具的熔断器状态。"""

    def __init__(self, config: CircuitBreakerConfig) -> None:
        self.config = config
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float = 0.0
        self.current_recovery_timeout = config.recovery_timeout

    def should_allow(self) -> bool:
        """当前是否允许调用。"""
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            elapsed = time.monotonic() - self.last_failure_time
            if elapsed >= self.current_recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        if self.state == CircuitState.HALF_OPEN:
            return True  # half-open 允许一次试探
        return False

    def on_success(self) -> bool:
        """调用成功后更新状态。返回 True 表示刚从 HALF_OPEN 恢复到 CLOSED。"""
        if self.state == CircuitState.HALF_OPEN:
            logger.info("熔断器恢复: HALF_OPEN → CLOSED")
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.current_recovery_timeout = self.config.recovery_timeout
            return True
        # CLOSED 状态成功，重置失败计数
        if self.state == CircuitState.CLOSED:
            self.failure_count = 0
        return False

    def on_failure(self) -> bool:
        """调用失败后更新状态。返回 True 表示刚触发熔断（进入 OPEN）。"""
        self.failure_count += 1
        self.last_failure_time = time.monotonic()

        if self.state == CircuitState.HALF_OPEN:
            # half-open 试探失败 → 重新熔断，冷却期加倍
            self.state = CircuitState.OPEN
            self.current_recovery_timeout = min(
                self.current_recovery_timeout * 2,
                self.config.max_recovery_timeout,
            )
            logger.warning(
                "熔断器重新熔断: HALF_OPEN → OPEN (冷却期: %.1fs)",
                self.current_recovery_timeout,
            )
            return True
        elif self.failure_count >= self.config.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(
                "熔断器触发: CLOSED → OPEN (连续失败: %d)",
                self.failure_count,
            )
            return True
        return False


class CircuitBreakerManager:
    """管理所有工具的熔断器状态。"""

    def __init__(self) -> None:
        self._breakers: dict[str, _CircuitBreakerState] = {}
        self._hooks: AgentHooks | None = None

    def set_hooks(self, hooks: AgentHooks) -> None:
        """注入 hooks（由 HarnessExecutor.set_hooks 统一调用）。"""
        self._hooks = hooks

    def register(self, tool_name: str, config: CircuitBreakerConfig) -> None:
        """为工具注册熔断器。"""
        self._breakers[tool_name] = _CircuitBreakerState(config)

    def check(self, tool_name: str) -> ToolResult | None:
        """检查熔断状态。返回 None 表示通过。"""
        breaker = self._breakers.get(tool_name)
        if breaker is None:
            return None  # 未配置熔断器，通过
        if breaker.should_allow():
            return None
        return ToolResult(
            success=False,
            error=(
                f"Tool '{tool_name}' 已熔断，"
                f"预计 {breaker.current_recovery_timeout:.0f}s 后恢复"
            ),
            tool_name=tool_name,
        )

    def record_success(self, tool_name: str) -> None:
        """记录成功调用。状态从 HALF_OPEN 恢复到 CLOSED 时触发 on_tool_circuit_close。"""
        breaker = self._breakers.get(tool_name)
        if breaker:
            just_recovered = breaker.on_success()
            if just_recovered and self._hooks:
                self._hooks.on_tool_circuit_close(tool_name=tool_name)

    def record_failure(self, tool_name: str) -> None:
        """记录失败调用。状态首次进入 OPEN 时触发 on_tool_circuit_open。"""
        breaker = self._breakers.get(tool_name)
        if breaker:
            just_opened = breaker.on_failure()
            if just_opened and self._hooks:
                self._hooks.on_tool_circuit_open(
                    tool_name=tool_name,
                    failure_count=breaker.failure_count,
                    recovery_timeout=breaker.current_recovery_timeout,
                )

    def is_tripped(self, tool_name: str) -> bool:
        """检查工具是否处于熔断拒绝状态（OPEN，不含 HALF_OPEN）。

        HALF_OPEN 允许试探性调用，不视为"熔断中"。
        用于 update_enabled_tools 判断工具是否不可用。
        """
        breaker = self._breakers.get(tool_name)
        if breaker is None:
            return False
        return breaker.state == CircuitState.OPEN
