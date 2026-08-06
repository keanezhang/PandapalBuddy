"""pandaren/behavior/harness/ — 运行时安全机制（Harness）

从 tool/harness/ 迁入 behavior 层：harness 是运行时行为约束，属于行为策略。

R1: rate_limiter     — 调用频率控制
R2: output_guard     — 输出大小控制
R3: circuit_breaker  — 熔断保护
R4: idempotency      — 幂等性保护
S6: halt             — 失败硬停止

HarnessExecutor — 将上述五道 harness 包裹在 ToolRegistry.execute_tool() 外层
"""

from .rate_limiter import RateLimiter
from .output_guard import OutputGuard
from .circuit_breaker import CircuitBreakerManager
from .idempotency import IdempotencyGuard
from .halt import HaltChecker
from .executor import HarnessExecutor

__all__ = [
    "RateLimiter",
    "OutputGuard",
    "CircuitBreakerManager",
    "IdempotencyGuard",
    "HaltChecker",
    "HarnessExecutor",
]
