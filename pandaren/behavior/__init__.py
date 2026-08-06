"""Behavior 层：约束 + 安全，运行时的纪律。

包含：
  - 权限守卫、HITL 控制器、执行上限、Token 预算、错误策略
  - ContextWindowBudget（上下文窗口 token 预算分配，单一真相源）
  - harness（运行时安全约束）：频率控制 / 输出截断 / 熔断 / 幂等 / 硬停止

注意：harness 组件依赖 tool 包的模型定义，
为避免循环导入，不在 __init__.py 模块级导入。
使用时请直接导入：
    from pandaren.behavior.harness import RateLimiter, ...

ToolBudget 已迁至 tool 层：from pandaren.tool.tool_budget import ToolBudget
"""

from .permission_guard import PermissionGuard
from .hitl_controller import HITLController
from .execution_limits import ExecutionLimits
from .step_guard import StepGuard, StepUsage, GuardDecision
from .error_policy import ErrorPolicy
from .exceptions import BehaviorConfigError
from .context_window_budget import ContextWindowBudget, SlotSnapshot

__all__ = [
    "PermissionGuard", "HITLController", "ExecutionLimits",
    "StepGuard", "StepUsage", "GuardDecision",
    "ErrorPolicy", "BehaviorConfigError",
    "ContextWindowBudget", "SlotSnapshot",
    # 以下组件需要从子模块直接导入（避免循环依赖）
    # from pandaren.behavior.harness import RateLimiter, OutputGuard, ...
]
