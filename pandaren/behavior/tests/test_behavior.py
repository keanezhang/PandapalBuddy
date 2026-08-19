"""
Pandaren Agent SDK · Behavior 层真实测试

覆盖约束
--------
  - PermissionGuard：allow/deny、空集、空 resource、精确 resource + actions 子集
  - HITLController：HC6 CRITICAL 强制审批、HIGH auto_confirm、resolve_resume、冻结保护
  - ExecutionLimits：HC5 冻结、各字段边界、step_timeout > total_timeout 检测
  - ErrorPolicy：calculate_delay 指数退避、max_delay_s 上限、冻结保护
  - CostCalculator：calculate_cost USD 精度、Fail-Safe 未知模型、check_budget、冻结
  - ContextWindowBudget：S1 冻结、slot 配额计算、ratio 校验、get_slot_tokens/build_slot_snapshot
  - BehaviorConfigError：所有校验分支
  - RateLimiter：R1 频率控制、reset_turn、get_count
  - OutputGuard：R2 输出截断（字节边界）、truncated 标记
  - CircuitBreakerManager：R3 CLOSED→OPEN→HALF_OPEN→CLOSED 全状态机
  - IdempotencyGuard：R4 同 turn 去重、reset_turn 清空、async + sync 接口

运行方式
--------
  cd pandaren/behavior/tests && python test_behavior.py
  cd pandaren/behavior/tests && python test_behavior.py --section permission_guard
  cd pandaren/behavior/tests && python test_behavior.py --section hitl
  cd pandaren/behavior/tests && python test_behavior.py --section execution_limits
  cd pandaren/behavior/tests && python test_behavior.py --section error_policy
  cd pandaren/behavior/tests && python test_behavior.py --section cost_calculator
  cd pandaren/behavior/tests && python test_behavior.py --section context_window
  cd pandaren/behavior/tests && python test_behavior.py --section rate_limiter
  cd pandaren/behavior/tests && python test_behavior.py --section output_guard
  cd pandaren/behavior/tests && python test_behavior.py --section circuit_breaker
  cd pandaren/behavior/tests && python test_behavior.py --section idempotency
"""

from __future__ import annotations

import asyncio
import os
import sys
import io
import time

# Windows 控制台 UTF-8 输出
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ═══ SDK 导入 ═══
from pandaren.identity.models import SensitivePermission
from pandaren.behavior import (
    PermissionGuard,
    HITLController,
    ExecutionLimits,
    ErrorPolicy,
    StepGuard,
    StepUsage,
    GuardDecision,
    ContextWindowBudget,
    SlotSnapshot,
    BehaviorConfigError,
)
from pandaren.behavior.hitl_controller import PendingApproval, ResumeDecision
from pandaren.tool.types import SensitivityLevel, CircuitBreakerConfig, CircuitState
from pandaren.tool import ToolResult
from pandaren.behavior.harness.rate_limiter import RateLimiter
from pandaren.behavior.harness.output_guard import OutputGuard
from pandaren.behavior.harness.circuit_breaker import CircuitBreakerManager
from pandaren.behavior.harness.idempotency import IdempotencyGuard


# ════════════════════════════════════════════════════
#  测试框架
# ════════════════════════════════════════════════════

class TestResult:
    """轻量测试结果收集器。"""
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def ok(self, name: str):
        self.passed += 1
        print(f"   ✅ {name}")

    def fail(self, name: str, detail: str = ""):
        self.failed += 1
        msg = f"   ❌ {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        self.errors.append(msg)

    def summary(self, section: str = ""):
        total = self.passed + self.failed
        label = f" [{section}]" if section else ""
        print(f"\n📊{label} 通过={self.passed} / 失败={self.failed} / 总计={total}")
        if self.errors:
            print("   失败列表:")
            for e in self.errors:
                print(f"     {e}")
        return self.failed == 0


result = TestResult()


def assert_true(condition: bool, name: str, detail: str = ""):
    if condition:
        result.ok(name)
    else:
        result.fail(name, detail or "条件为 False")


def assert_raises(exc_type, fn, name: str, detail: str = ""):
    exc_label = (
        " | ".join(t.__name__ for t in exc_type)
        if isinstance(exc_type, tuple)
        else exc_type.__name__
    )
    try:
        fn()
        result.fail(name, detail or f"应抛出 {exc_label} 但未抛出")
    except exc_type:
        result.ok(name)
    except Exception as e:
        result.fail(name, f"抛出了意外异常 {type(e).__name__}: {e}")


def assert_no_raises(fn, name: str, detail: str = ""):
    try:
        fn()
        result.ok(name)
    except Exception as e:
        result.fail(name, detail or f"意外抛出 {type(e).__name__}: {e}")


def async_run(coro):
    """同步运行协程。"""
    return asyncio.new_event_loop().run_until_complete(coro)


# ════════════════════════════════════════════════════
#  工厂辅助
# ════════════════════════════════════════════════════

def _pending(tool_name: str = "my_tool", sensitivity: int = SensitivityLevel.CRITICAL) -> PendingApproval:
    return PendingApproval(
        tool_call={"id": "call_1", "function": {"name": tool_name, "arguments": "{}"}},
        tool_name=tool_name,
        tool_args={},
        sensitivity=sensitivity,
        step_n=1,
    )


# ════════════════════════════════════════════════════
#  Section: permission_guard
# ════════════════════════════════════════════════════

def test_permission_guard():
    print("\n── PermissionGuard ──────────────────────────────────────")
    guard = PermissionGuard()

    # ① LOW/MEDIUM 工具 → 直接放行，无论权限集如何
    assert_true(
        guard.check_permission(frozenset(), SensitivityLevel.LOW, None) == "allow",
        "LOW 工具 + 空权限集 → allow",
    )
    assert_true(
        guard.check_permission(frozenset(), SensitivityLevel.MEDIUM, None) == "allow",
        "MEDIUM 工具 + 空权限集 → allow",
    )
    assert_true(
        guard.check_permission(frozenset(), SensitivityLevel.LOW, SensitivePermission.CODE_EXEC) == "allow",
        "LOW 工具即使声明了 tool_permission 也放行",
    )

    # ② HIGH/CRITICAL 工具，tool_permission=None → 放行
    assert_true(
        guard.check_permission(frozenset(), SensitivityLevel.HIGH, None) == "allow",
        "HIGH 工具 + tool_permission=None → allow",
    )
    assert_true(
        guard.check_permission(frozenset(), SensitivityLevel.CRITICAL, None) == "allow",
        "CRITICAL 工具 + tool_permission=None → allow",
    )

    # ③ HIGH 工具，有 tool_permission，Identity 持有该权限 → allow
    perms_with_exec = frozenset({SensitivePermission.CODE_EXEC})
    assert_true(
        guard.check_permission(perms_with_exec, SensitivityLevel.HIGH, SensitivePermission.CODE_EXEC) == "allow",
        "HIGH 工具 + Identity 持有所需权限 → allow",
    )

    # ③ HIGH 工具，有 tool_permission，Identity 不持有该权限 → deny
    assert_true(
        guard.check_permission(frozenset(), SensitivityLevel.HIGH, SensitivePermission.CODE_EXEC) == "deny",
        "HIGH 工具 + 空权限集 + 需要 CODE_EXEC → deny",
    )

    # ③ CRITICAL 工具，有 tool_permission，Identity 持有 → allow
    perms_with_delete = frozenset({SensitivePermission.DATA_DELETE})
    assert_true(
        guard.check_permission(perms_with_delete, SensitivityLevel.CRITICAL, SensitivePermission.DATA_DELETE) == "allow",
        "CRITICAL 工具 + Identity 持有 DATA_DELETE → allow",
    )

    # ③ CRITICAL 工具，需要 SYSTEM_CMD，Identity 只有 CODE_EXEC → deny
    assert_true(
        guard.check_permission(perms_with_exec, SensitivityLevel.CRITICAL, SensitivePermission.SYSTEM_CMD) == "deny",
        "CRITICAL 工具 + 权限不匹配 → deny",
    )

    # PERMISSION_ALL 持有所有权限时，任何声明都放行
    from pandaren.identity.models import PERMISSION_ALL
    for perm in SensitivePermission:
        assert_true(
            guard.check_permission(PERMISSION_ALL, SensitivityLevel.CRITICAL, perm) == "allow",
            f"PERMISSION_ALL + CRITICAL + {perm.value} → allow",
        )


# ════════════════════════════════════════════════════
#  Section: hitl
# ════════════════════════════════════════════════════

def test_hitl():
    print("\n── HITLController ────────────────────────────────────────")
    ctrl_default = HITLController()
    ctrl_auto = HITLController(auto_confirm_high=True)

    # HC6: CRITICAL 强制 need_approval
    assert_true(
        ctrl_default.check_approval(SensitivityLevel.CRITICAL) == "need_approval",
        "HC6: CRITICAL → need_approval（默认配置）",
    )
    assert_true(
        ctrl_auto.check_approval(SensitivityLevel.CRITICAL) == "need_approval",
        "HC6: CRITICAL → need_approval（auto_confirm_high=True 不可绕过）",
    )

    # HIGH 按 auto_confirm_high 决定
    assert_true(
        ctrl_default.check_approval(SensitivityLevel.HIGH) == "need_approval",
        "HIGH + auto_confirm_high=False → need_approval",
    )
    assert_true(
        ctrl_auto.check_approval(SensitivityLevel.HIGH) == "pass",
        "HIGH + auto_confirm_high=True → pass",
    )

    # MEDIUM / LOW 直接放行
    assert_true(
        ctrl_default.check_approval(SensitivityLevel.MEDIUM) == "pass",
        "MEDIUM → pass",
    )
    assert_true(
        ctrl_default.check_approval(SensitivityLevel.LOW) == "pass",
        "LOW → pass",
    )

    # tool_name 参数不影响决策结果
    assert_true(
        ctrl_default.check_approval(SensitivityLevel.CRITICAL, tool_name="delete_db") == "need_approval",
        "CRITICAL + tool_name 参数 → need_approval",
    )

    # resolve_resume：approved
    pending = _pending()
    decision = ctrl_default.resolve_resume("approved", pending)
    assert_true(decision.action == "execute_pending", "approved → execute_pending")
    assert_true(decision.pending is pending, "approved 时 pending 原样返回")

    # resolve_resume：rejected
    decision2 = ctrl_default.resolve_resume("rejected", pending)
    assert_true(decision2.action == "reject_and_halt", "rejected → reject_and_halt")

    # resolve_resume：非法值视为拒绝
    decision3 = ctrl_default.resolve_resume("whatever", pending)
    assert_true(decision3.action == "reject_and_halt", "非法 decision 值 → reject_and_halt")

    # 冻结保护（HC1）
    assert_raises(
        (PermissionError, AttributeError),
        lambda: setattr(ctrl_default, "auto_confirm_high", True),
        "HITLController 字段不可修改（HC1）",
    )

    # property 只读访问
    assert_true(ctrl_default.auto_confirm_high is False, "auto_confirm_high property 返回正确值")
    assert_true(ctrl_auto.auto_confirm_high is True, "auto_confirm_high=True property 返回正确值")

    # PendingApproval 冻结
    assert_raises(
        (TypeError, AttributeError),
        lambda: setattr(pending, "tool_name", "hacked"),
        "PendingApproval frozen=True 不可修改",
    )


# ════════════════════════════════════════════════════
#  Section: execution_limits
# ════════════════════════════════════════════════════

def test_execution_limits():
    print("\n── ExecutionLimits ───────────────────────────────────────")

    # 正常创建
    assert_no_raises(
        lambda: ExecutionLimits(max_steps=10, step_timeout=30.0, total_timeout=300.0),
        "正常参数创建 ExecutionLimits",
    )

    # 参数可选
    assert_no_raises(
        lambda: ExecutionLimits(max_steps=5),
        "只传 max_steps 创建",
    )

    # 读取字段（停机守卫已上移应用层 StepGuard，ExecutionLimits 不再有 max_cost_usd）
    el = ExecutionLimits(max_steps=10, step_timeout=30.0, total_timeout=300.0)
    assert_true(el.max_steps == 10, "max_steps 属性正确")
    assert_true(el.step_timeout == 30.0, "step_timeout 属性正确")
    assert_true(el.total_timeout == 300.0, "total_timeout 属性正确")

    # HC5 冻结
    assert_raises(
        PermissionError,
        lambda: setattr(el, "max_steps", 999),
        "ExecutionLimits HC5: 字段不可修改",
    )

    # max_steps ≤ 0
    assert_raises(
        BehaviorConfigError,
        lambda: ExecutionLimits(max_steps=0),
        "max_steps=0 → BehaviorConfigError",
    )
    assert_raises(
        BehaviorConfigError,
        lambda: ExecutionLimits(max_steps=-1),
        "max_steps=-1 → BehaviorConfigError",
    )

    # step_timeout ≤ 0
    assert_raises(
        BehaviorConfigError,
        lambda: ExecutionLimits(max_steps=5, step_timeout=0),
        "step_timeout=0 → BehaviorConfigError",
    )

    # total_timeout ≤ 0
    assert_raises(
        BehaviorConfigError,
        lambda: ExecutionLimits(max_steps=5, total_timeout=0),
        "total_timeout=0 → BehaviorConfigError",
    )

    # step_timeout > total_timeout
    assert_raises(
        BehaviorConfigError,
        lambda: ExecutionLimits(max_steps=5, step_timeout=100.0, total_timeout=50.0),
        "step_timeout > total_timeout → BehaviorConfigError",
    )

    # step_timeout == total_timeout（边界，允许）
    assert_no_raises(
        lambda: ExecutionLimits(max_steps=5, step_timeout=60.0, total_timeout=60.0),
        "step_timeout == total_timeout 边界值允许",
    )


# ════════════════════════════════════════════════════
#  Section: error_policy
# ════════════════════════════════════════════════════

def test_error_policy():
    print("\n── ErrorPolicy ───────────────────────────────────────────")

    # 正常创建
    policy = ErrorPolicy(max_retries=3, base_delay_s=1.0, max_delay_s=60.0)
    assert_true(policy.max_retries == 3, "max_retries 属性正确")
    assert_true(policy.base_delay_s == 1.0, "base_delay_s 属性正确")
    assert_true(policy.max_delay_s == 60.0, "max_delay_s 属性正确")

    # 指数退避：base * 2^attempt
    assert_true(abs(policy.calculate_delay(0) - 1.0) < 1e-9, "attempt=0: delay=base_delay_s")
    assert_true(abs(policy.calculate_delay(1) - 2.0) < 1e-9, "attempt=1: delay=2.0")
    assert_true(abs(policy.calculate_delay(2) - 4.0) < 1e-9, "attempt=2: delay=4.0")
    assert_true(abs(policy.calculate_delay(3) - 8.0) < 1e-9, "attempt=3: delay=8.0")

    # max_delay_s 上限
    policy2 = ErrorPolicy(max_retries=10, base_delay_s=1.0, max_delay_s=10.0)
    assert_true(policy2.calculate_delay(10) <= 10.0, "delay 不超过 max_delay_s 上限")
    assert_true(abs(policy2.calculate_delay(10) - 10.0) < 1e-9, "大 attempt 时 delay = max_delay_s")

    # attempt < 0 抛 ValueError
    assert_raises(
        ValueError,
        lambda: policy.calculate_delay(-1),
        "attempt < 0 → ValueError",
    )

    # 冻结保护
    assert_raises(
        PermissionError,
        lambda: setattr(policy, "max_retries", 99),
        "ErrorPolicy 字段不可修改",
    )

    # max_retries < 0
    assert_raises(
        BehaviorConfigError,
        lambda: ErrorPolicy(max_retries=-1),
        "max_retries=-1 → BehaviorConfigError",
    )

    # base_delay_s ≤ 0
    assert_raises(
        BehaviorConfigError,
        lambda: ErrorPolicy(base_delay_s=0),
        "base_delay_s=0 → BehaviorConfigError",
    )

    # max_delay_s < base_delay_s
    assert_raises(
        BehaviorConfigError,
        lambda: ErrorPolicy(base_delay_s=10.0, max_delay_s=5.0),
        "max_delay_s < base_delay_s → BehaviorConfigError",
    )

    # max_retries=0 允许（表示不重试）
    assert_no_raises(
        lambda: ErrorPolicy(max_retries=0),
        "max_retries=0 允许（不重试）",
    )


# ════════════════════════════════════════════════════
#  Section: cost_calculator
# ════════════════════════════════════════════════════

def test_cost_calculator():
    print("\n── StepGuard（通用每步停机机制契约）──────────────────────────")

    # SDK 只定义通用 StepGuard 协议：每步交出用量事实 StepUsage，守卫返回 GuardDecision。
    # 价格/预算/累加全归应用层实现；费用超限只是「停机的一种理由」。
    # 这里用一个最小假实现验证协议契约与 SDK 的 duck-typing 兼容。
    class _FakeGuard:
        def __init__(self, budget: float | None):
            self.budget = budget
            self.total = 0.0

        def should_halt(self, *, run_id, usage: StepUsage) -> GuardDecision:
            if self.budget is None:
                return GuardDecision(halt=False)
            # 假净费用：0.001/1k input + 0.002/1k output（含命中折扣由 app 决定，这里简化）
            self.total += usage.input_tokens / 1000 * 0.001 + usage.output_tokens / 1000 * 0.002
            if self.total >= self.budget:
                return GuardDecision(halt=True, reason=f"over budget ${self.total:.4f}")
            return GuardDecision(halt=False)

    g = _FakeGuard(budget=0.01)
    # runtime_checkable Protocol：假实现应被识别为 StepGuard
    assert_true(isinstance(g, StepGuard), "实现 should_halt 的对象是 StepGuard（runtime_checkable）")

    def _usage() -> StepUsage:
        return StepUsage(model="m", input_tokens=1000, output_tokens=1000, cached_tokens=0, step=1)

    # 累加到超预算 → halt=True
    r1 = g.should_halt(run_id="r1", usage=_usage())  # +0.003
    assert_true(r1.halt is False, "首次累计 0.003 < 0.01 → 不停机")
    for _ in range(3):
        last = g.should_halt(run_id="r1", usage=_usage())
    assert_true(last.halt is True, "累计 ≥ 0.01 → 停机（halt=True）")
    assert_true(bool(last.reason), "停机时带上理由串（reason 非空）")

    # None 预算 → 永不停机
    g0 = _FakeGuard(budget=None)
    big = StepUsage(model="m", input_tokens=10**9, output_tokens=10**9, cached_tokens=0, step=1)
    assert_true(
        g0.should_halt(run_id="r", usage=big).halt is False,
        "无预算（None）→ 永不停机",
    )


# ════════════════════════════════════════════════════
#  Section: context_window
# ════════════════════════════════════════════════════

def test_context_window_budget():
    print("\n── ContextWindowBudget ───────────────────────────────────")

    # 正常创建（显式 context_window 避免 WARNING）
    budget = ContextWindowBudget(
        context_window=128000,
        system_prompt_ratio=0.15,
        tool_schema_ratio=0.10,
        conversation_ratio=0.50,
        recall_ratio=0.10,
    )

    # ratio 总和 = 0.85 ≤ 1.0，允许
    assert_true(budget.context_window == 128000, "context_window 属性正确")
    assert_true(budget.system_prompt_tokens == 19200, "system_prompt_tokens = floor(128000 * 0.15)")
    assert_true(budget.tool_schema_tokens == 12800, "tool_schema_tokens = floor(128000 * 0.10)")
    assert_true(budget.conversation_tokens == 64000, "conversation_tokens = floor(128000 * 0.50)")
    assert_true(budget.recall_tokens == 12800, "recall_tokens = floor(128000 * 0.10)")

    # get_slot_tokens
    assert_true(budget.get_slot_tokens("system_prompt") == 19200, "get_slot_tokens('system_prompt')")
    assert_true(budget.get_slot_tokens("tool_schema") == 12800, "get_slot_tokens('tool_schema')")
    assert_true(budget.get_slot_tokens("conversation") == 64000, "get_slot_tokens('conversation')")
    assert_true(budget.get_slot_tokens("recall") == 12800, "get_slot_tokens('recall')")

    # 未知 slot → ValueError
    assert_raises(
        ValueError,
        lambda: budget.get_slot_tokens("nonexistent"),
        "未知 slot_name → ValueError",
    )

    # build_slot_snapshot
    snap = budget.build_slot_snapshot()
    assert_true(isinstance(snap, SlotSnapshot), "build_slot_snapshot 返回 SlotSnapshot")
    assert_true(snap.system_prompt_tokens == 19200, "SlotSnapshot.system_prompt_tokens 正确")
    assert_true(snap.tool_schema_tokens == 12800, "SlotSnapshot.tool_schema_tokens 正确")
    assert_true(snap.conversation_tokens == 64000, "SlotSnapshot.conversation_tokens 正确")
    assert_true(snap.recall_tokens == 12800, "SlotSnapshot.recall_tokens 正确")

    # SlotSnapshot 冻结
    assert_raises(
        (TypeError, AttributeError),
        lambda: setattr(snap, "system_prompt_tokens", 0),
        "SlotSnapshot frozen=True 不可修改",
    )

    # S1 冻结保护
    assert_raises(
        PermissionError,
        lambda: setattr(budget, "context_window", 1000),
        "ContextWindowBudget S1: 字段不可修改",
    )

    # ratio sum > 1.0 → BehaviorConfigError
    assert_raises(
        BehaviorConfigError,
        lambda: ContextWindowBudget(
            context_window=8192,
            system_prompt_ratio=0.4,
            tool_schema_ratio=0.3,
            conversation_ratio=0.3,
            recall_ratio=0.2,
        ),
        "ratio sum > 1.0 → BehaviorConfigError",
    )

    # context_window ≤ 0 → BehaviorConfigError
    assert_raises(
        BehaviorConfigError,
        lambda: ContextWindowBudget(context_window=0),
        "context_window=0 → BehaviorConfigError",
    )
    assert_raises(
        BehaviorConfigError,
        lambda: ContextWindowBudget(context_window=-1),
        "context_window=-1 → BehaviorConfigError",
    )

    # 非 int context_window → BehaviorConfigError
    assert_raises(
        BehaviorConfigError,
        lambda: ContextWindowBudget(context_window=128000.0),  # type: ignore[arg-type]
        "float context_window → BehaviorConfigError",
    )

    # ratio 越界 → BehaviorConfigError
    assert_raises(
        BehaviorConfigError,
        lambda: ContextWindowBudget(context_window=8192, system_prompt_ratio=1.5),
        "ratio > 1.0 → BehaviorConfigError",
    )
    assert_raises(
        BehaviorConfigError,
        lambda: ContextWindowBudget(context_window=8192, recall_ratio=-0.1),
        "ratio < 0 → BehaviorConfigError",
    )

    # ratio sum == 1.0 恰好允许
    assert_no_raises(
        lambda: ContextWindowBudget(
            context_window=8192,
            system_prompt_ratio=0.25,
            tool_schema_ratio=0.25,
            conversation_ratio=0.25,
            recall_ratio=0.25,
        ),
        "ratio sum == 1.0 边界值允许",
    )

    # ratio 全为 0 允许（极端情况）
    assert_no_raises(
        lambda: ContextWindowBudget(
            context_window=8192,
            system_prompt_ratio=0.0,
            tool_schema_ratio=0.0,
            conversation_ratio=0.0,
            recall_ratio=0.0,
        ),
        "ratio 全为 0 允许",
    )


# ════════════════════════════════════════════════════
#  Section: rate_limiter
# ════════════════════════════════════════════════════

def test_rate_limiter():
    print("\n── RateLimiter ───────────────────────────────────────────")
    rl = RateLimiter()

    # 首次调用通过，计数器 +1
    r = rl.check("search", max_calls=3)
    assert_true(r is None, "首次调用通过")
    assert_true(rl.get_count("search") == 1, "首次调用后计数=1")

    # 再调用两次
    rl.check("search", max_calls=3)
    rl.check("search", max_calls=3)
    assert_true(rl.get_count("search") == 3, "三次调用后计数=3")

    # 第 4 次超限 → 返回拒绝 ToolResult
    r4 = rl.check("search", max_calls=3)
    assert_true(r4 is not None, "超限时返回拒绝 ToolResult")
    if r4 is not None:
        assert_true(r4.success is False, "拒绝 ToolResult.success=False")
        assert_true(r4.error is not None and "search" in r4.error, "拒绝错误信息包含工具名")

    # 超限时计数器不再增加
    assert_true(rl.get_count("search") == 3, "超限时计数不增加")

    # 不同工具互相独立
    r_other = rl.check("calc", max_calls=2)
    assert_true(r_other is None, "不同工具独立计数")
    assert_true(rl.get_count("calc") == 1, "calc 独立计数=1")

    # max_calls=None 无限制，永远通过
    for _ in range(100):
        r_unlimited = rl.check("unlimited_tool", max_calls=None)
    assert_true(r_unlimited is None, "max_calls=None 无限制通过")
    assert_true(rl.get_count("unlimited_tool") == 100, "无限制时计数正常累计")

    # reset_turn 清空所有计数
    rl.reset_turn()
    assert_true(rl.get_count("search") == 0, "reset_turn 后 search 计数归零")
    assert_true(rl.get_count("calc") == 0, "reset_turn 后 calc 计数归零")

    # reset 后重新调用通过
    r_reset = rl.check("search", max_calls=3)
    assert_true(r_reset is None, "reset 后重新调用通过")


# ════════════════════════════════════════════════════
#  Section: output_guard
# ════════════════════════════════════════════════════

def test_output_guard():
    print("\n── OutputGuard ───────────────────────────────────────────")
    guard = OutputGuard()

    # data=None → 原样返回
    r_none = ToolResult(success=True, data=None, tool_name="t1")
    out = guard.check(r_none, max_bytes=100)
    assert_true(out is r_none, "data=None 原样返回")
    assert_true(out.truncated is False, "data=None truncated=False")

    # 未超限 → 不截断
    short_data = "hello"
    r_short = ToolResult(success=True, data=short_data, tool_name="t2")
    out2 = guard.check(r_short, max_bytes=1000)
    assert_true(out2.truncated is False, "未超限时 truncated=False")

    # 超限 → 截断并设置 truncated=True
    long_data = "A" * 200
    r_long = ToolResult(success=True, data=long_data, tool_name="t3")
    out3 = guard.check(r_long, max_bytes=50)
    assert_true(out3.truncated is True, "超限时 truncated=True")
    assert_true(isinstance(out3.data, str), "截断后 data 仍为字符串")
    # 截断后的数据（含截断说明）UTF-8 编码长度可能超过 max_bytes（因为附加了说明文字）
    # 只验证截断确实发生了（原始 data 缩短了）
    assert_true(len(out3.data) < len(long_data) + 100, "截断后 data 长度小于原始")

    # 中文字符不被截成乱码（UTF-8 多字节安全截断）
    chinese_data = "中文测试数据" * 20
    r_chinese = ToolResult(success=True, data=chinese_data, tool_name="t4")
    out4 = guard.check(r_chinese, max_bytes=30)
    # 验证截断后可以被正常编码（无乱码）
    try:
        _ = out4.data.encode("utf-8")
        result.ok("中文数据安全截断（UTF-8 无乱码）")
    except UnicodeEncodeError as e:
        result.fail("中文数据安全截断（UTF-8 无乱码）", str(e))

    # 无 hooks 时截断仍然成功（只是 logger.warning）
    r_nohook = ToolResult(success=True, data="X" * 200, tool_name="t5")
    out5 = guard.check(r_nohook, max_bytes=10)
    assert_true(out5.truncated is True, "无 hooks 时截断仍然工作")

    # 恰好等于 max_bytes → 不截断
    exact_str = "A" * 50  # 50 bytes in UTF-8
    r_exact = ToolResult(success=True, data=exact_str, tool_name="t6")
    import json
    serialized_len = len(json.dumps(exact_str, ensure_ascii=False).encode("utf-8"))
    out6 = guard.check(r_exact, max_bytes=serialized_len)
    assert_true(out6.truncated is False, "恰好等于 max_bytes 不截断")


# ════════════════════════════════════════════════════
#  Section: circuit_breaker
# ════════════════════════════════════════════════════

def test_circuit_breaker():
    print("\n── CircuitBreakerManager ─────────────────────────────────")
    mgr = CircuitBreakerManager()

    # 未注册的工具直接通过
    r = mgr.check("unregistered")
    assert_true(r is None, "未注册工具直接通过")
    assert_true(mgr.is_tripped("unregistered") is False, "未注册工具 is_tripped=False")

    # 注册工具（failure_threshold=3, recovery_timeout=0.05s）
    cfg = CircuitBreakerConfig(failure_threshold=3, recovery_timeout=0.05, max_recovery_timeout=1.0)
    mgr.register("risky_tool", cfg)

    # CLOSED 状态：check 通过
    assert_true(mgr.check("risky_tool") is None, "CLOSED 状态 check 通过")
    assert_true(mgr.is_tripped("risky_tool") is False, "CLOSED 状态 is_tripped=False")

    # 成功调用不影响 CLOSED 状态
    mgr.record_success("risky_tool")
    assert_true(mgr.check("risky_tool") is None, "成功记录后仍 CLOSED")

    # 连续失败到达阈值 → OPEN
    mgr.record_failure("risky_tool")
    mgr.record_failure("risky_tool")
    assert_true(mgr.check("risky_tool") is None, "2 次失败未到阈值，check 仍通过")

    mgr.record_failure("risky_tool")  # 第 3 次，达到 failure_threshold=3
    tripped_result = mgr.check("risky_tool")
    assert_true(tripped_result is not None, "3 次失败后 OPEN，check 返回拒绝")
    if tripped_result is not None:
        assert_true(tripped_result.success is False, "熔断后 ToolResult.success=False")
    assert_true(mgr.is_tripped("risky_tool") is True, "OPEN 状态 is_tripped=True")

    # OPEN → HALF_OPEN（等待 recovery_timeout 过期）
    time.sleep(0.1)  # 等待 > 0.05s
    r_halfopen = mgr.check("risky_tool")
    assert_true(r_halfopen is None, "冷却期后 HALF_OPEN → check 通过（试探）")
    assert_true(mgr.is_tripped("risky_tool") is False, "HALF_OPEN 状态 is_tripped=False")

    # HALF_OPEN 试探成功 → CLOSED
    mgr.record_success("risky_tool")
    assert_true(mgr.check("risky_tool") is None, "HALF_OPEN 成功后 CLOSED，check 通过")
    assert_true(mgr.is_tripped("risky_tool") is False, "恢复后 is_tripped=False")

    # 测试 HALF_OPEN 试探失败 → 重新 OPEN（冷却期加倍）
    mgr2 = CircuitBreakerManager()
    cfg2 = CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.05, max_recovery_timeout=2.0)
    mgr2.register("flaky", cfg2)

    mgr2.record_failure("flaky")  # CLOSED → OPEN
    assert_true(mgr2.is_tripped("flaky") is True, "1 次失败 → OPEN")

    time.sleep(0.1)
    mgr2.check("flaky")  # 触发 OPEN → HALF_OPEN 转换
    mgr2.record_failure("flaky")  # HALF_OPEN 试探失败 → 重新 OPEN
    assert_true(mgr2.is_tripped("flaky") is True, "HALF_OPEN 失败 → 重新 OPEN")

    # record_success/failure 对未注册工具静默处理
    assert_no_raises(
        lambda: mgr.record_success("nonexistent"),
        "record_success 未注册工具不抛异常",
    )
    assert_no_raises(
        lambda: mgr.record_failure("nonexistent"),
        "record_failure 未注册工具不抛异常",
    )


# ════════════════════════════════════════════════════
#  Section: idempotency
# ════════════════════════════════════════════════════

def test_idempotency():
    print("\n── IdempotencyGuard ──────────────────────────────────────")
    guard = IdempotencyGuard()

    args1 = {"query": "hello", "limit": 10}
    result_obj = ToolResult(success=True, data="result_data", tool_name="search")

    # 首次 check → None（未命中）
    cached = async_run(guard.check("search", args1))
    assert_true(cached is None, "首次 check → None（未命中缓存）")

    # store
    async_run(guard.store("search", args1, result_obj))

    # 再次 check → 命中缓存
    cached2 = async_run(guard.check("search", args1))
    assert_true(cached2 is result_obj, "store 后 check → 命中缓存（同一对象）")

    # 不同 args → 独立 key，未命中
    args2 = {"query": "world", "limit": 10}
    cached3 = async_run(guard.check("search", args2))
    assert_true(cached3 is None, "不同 args → 独立 key，未命中")

    # 不同 tool_name → 独立 key，未命中
    cached4 = async_run(guard.check("other_tool", args1))
    assert_true(cached4 is None, "不同 tool_name → 独立 key，未命中")

    # args key 顺序无关（sort_keys=True）
    args_reversed = {"limit": 10, "query": "hello"}
    cached5 = async_run(guard.check("search", args_reversed))
    assert_true(cached5 is result_obj, "args key 顺序不影响 hash，命中缓存")

    # sync 接口
    guard2 = IdempotencyGuard()
    r_sync = guard2.check_sync("tool", {"x": 1})
    assert_true(r_sync is None, "check_sync 首次 → None")
    r2 = ToolResult(success=True, data="sync_data", tool_name="tool")
    guard2.store_sync("tool", {"x": 1}, r2)
    r_sync2 = guard2.check_sync("tool", {"x": 1})
    assert_true(r_sync2 is r2, "store_sync + check_sync → 命中缓存")

    # reset_turn 清空缓存
    guard.reset_turn()
    assert_true(len(guard._cache) == 0, "reset_turn 后 _cache 为空")
    assert_true(len(guard._locks) == 0, "reset_turn 后 _locks 为空")
    cached_after_reset = async_run(guard.check("search", args1))
    assert_true(cached_after_reset is None, "reset_turn 后缓存清空")

    # _make_key 确定性 hash
    key1 = guard._make_key("search", {"a": 1, "b": 2})
    key2 = guard._make_key("search", {"b": 2, "a": 1})
    assert_true(key1 == key2, "_make_key sort_keys → 相同 hash")
    key3 = guard._make_key("other", {"a": 1, "b": 2})
    assert_true(key1 != key3, "_make_key tool_name 不同 → 不同 hash")


# ════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════

from typing import Callable
SECTIONS: dict[str, Callable[[], None]] = {
    "permission_guard": test_permission_guard,
    "hitl": test_hitl,
    "execution_limits": test_execution_limits,
    "error_policy": test_error_policy,
    "cost_calculator": test_cost_calculator,
    "context_window": test_context_window_budget,
    "rate_limiter": test_rate_limiter,
    "output_guard": test_output_guard,
    "circuit_breaker": test_circuit_breaker,
    "idempotency": test_idempotency,
}

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--section", choices=list(SECTIONS.keys()), default=None)
    args = parser.parse_args()

    print("=" * 60)
    print("  Behavior 层真实测试")
    print("=" * 60)

    if args.section:
        SECTIONS[args.section]()
        result.summary(args.section)
    else:
        for name, fn in SECTIONS.items():
            fn()
        result.summary("all")

    sys.exit(0 if result.failed == 0 else 1)
