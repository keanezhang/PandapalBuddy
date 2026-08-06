"""
Pandaren Agent SDK · Behavior 层 Mock 测试

覆盖约束
--------
  通过 Mock/Patch 验证 Behavior 层的内部行为、logger 日志、hooks 回调：
  - PermissionGuard：_match_permission 内部逻辑、logger.warning patch
  - HITLController：logger.info patch 验证 check_approval 分支日志
  - ExecutionLimits：logger.warning 在修改时触发
  - CostCalculator：未知模型 logger.warning；calculate_cost 精度
  - ContextWindowBudget：默认 context_window 触发 logger.warning；ratio 超限 logger.error
  - OutputGuard：hooks.on_tool_output_truncated 回调；无 hooks 时 logger.warning
  - CircuitBreakerManager：hooks.on_tool_circuit_open / on_tool_circuit_close 回调
  - IdempotencyGuard：并发安全（asyncio.Lock），mock 验证缓存穿透
  - BehaviorConfigError：各组件配置校验注入路径

运行方式
--------
  cd pandaren/behavior/tests && python test_behavior_mock.py
  cd pandaren/behavior/tests && python test_behavior_mock.py --section permission_guard
  cd pandaren/behavior/tests && python test_behavior_mock.py --section hitl
  cd pandaren/behavior/tests && python test_behavior_mock.py --section cost_calculator
  cd pandaren/behavior/tests && python test_behavior_mock.py --section context_window
  cd pandaren/behavior/tests && python test_behavior_mock.py --section output_guard
  cd pandaren/behavior/tests && python test_behavior_mock.py --section circuit_breaker
  cd pandaren/behavior/tests && python test_behavior_mock.py --section idempotency
"""

from __future__ import annotations

import asyncio
import os
import sys
import io
from unittest.mock import patch, MagicMock

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
    StepGuard,
    StepUsage,
    GuardDecision,
    ContextWindowBudget,
    BehaviorConfigError,
)
from pandaren.behavior.hitl_controller import PendingApproval
from pandaren.tool.types import SensitivityLevel, CircuitBreakerConfig
from pandaren.tool import ToolResult
from pandaren.hook import AgentHooks
from pandaren.behavior.harness.output_guard import OutputGuard
from pandaren.behavior.harness.circuit_breaker import CircuitBreakerManager
from pandaren.behavior.harness.idempotency import IdempotencyGuard


# ════════════════════════════════════════════════════
#  异步辅助
# ════════════════════════════════════════════════════

def async_run(coro):
    """同步运行协程。"""
    return asyncio.new_event_loop().run_until_complete(coro)


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
    try:
        fn()
        result.fail(name, detail or f"应抛出 {exc_type} 但未抛出")
    except Exception as e:
        if isinstance(exc_type, tuple):
            if isinstance(e, exc_type):
                result.ok(name)
            else:
                result.fail(name, f"抛出了意外异常 {type(e).__name__}: {e}")
        else:
            if isinstance(e, exc_type):
                result.ok(name)
            else:
                result.fail(name, f"抛出了意外异常 {type(e).__name__}: {e}")


def assert_no_raises(fn, name: str, detail: str = ""):
    try:
        fn()
        result.ok(name)
    except Exception as e:
        result.fail(name, detail or f"意外抛出 {type(e).__name__}: {e}")


# ════════════════════════════════════════════════════
#  工厂辅助
# ════════════════════════════════════════════════════

def _make_hooks() -> MagicMock:
    """创建带完整 spec 的 AgentHooks mock。"""
    return MagicMock(spec=AgentHooks)


def _pending(tool_name: str = "test_tool", sensitivity: int = SensitivityLevel.CRITICAL) -> PendingApproval:
    return PendingApproval(
        tool_call={"id": "call_x", "function": {"name": tool_name, "arguments": "{}"}},
        tool_name=tool_name,
        tool_args={},
        sensitivity=sensitivity,
        step_n=1,
    )


# ════════════════════════════════════════════════════
#  Section: permission_guard
# ════════════════════════════════════════════════════

def test_permission_guard_mock():
    print("\n── PermissionGuard (Mock) ────────────────────────────────")
    guard = PermissionGuard()

    # logger.warning 在 empty sensitive_permissions + HIGH 权限工具时触发
    with patch("pandaren.behavior.permission_guard.logger") as mock_log:
        guard.check_permission(
            frozenset(),
            SensitivityLevel.HIGH,
            SensitivePermission.NETWORK_CALL,
        )
        assert_true(mock_log.warning.called, "空 sensitive_permissions 触发 logger.warning")

    # deny 时触发 logger.warning（agent 缺少工具所需权限）
    with patch("pandaren.behavior.permission_guard.logger") as mock_log:
        guard.check_permission(
            frozenset({SensitivePermission.DATA_WRITE}),
            SensitivityLevel.HIGH,
            SensitivePermission.CODE_EXEC,
        )
        assert_true(mock_log.warning.called, "权限拒绝时触发 logger.warning")

    # allow 时不触发 logger.warning（agent 拥有所需权限）
    with patch("pandaren.behavior.permission_guard.logger") as mock_log:
        guard.check_permission(
            frozenset({SensitivePermission.NETWORK_CALL}),
            SensitivityLevel.HIGH,
            SensitivePermission.NETWORK_CALL,
        )
        assert_true(not mock_log.warning.called, "权限放行时不触发 logger.warning")

    # LOW sensitivity + tool_permission=None → 直接放行
    with patch("pandaren.behavior.permission_guard.logger") as mock_log:
        res = guard.check_permission(
            frozenset(),
            SensitivityLevel.LOW,
            None,
        )
        assert_true(res == "allow", "LOW sensitivity + tool_permission=None → allow")
        assert_true(not mock_log.warning.called, "LOW 放行时不触发 logger.warning")

    # agent 拥有权限 → allow
    res_allow = guard.check_permission(
        frozenset({SensitivePermission.CODE_EXEC}),
        SensitivityLevel.CRITICAL,
        SensitivePermission.CODE_EXEC,
    )
    assert_true(res_allow == "allow", "agent 拥有所需权限 → allow")

    # agent 缺少权限 → deny
    res_deny = guard.check_permission(
        frozenset({SensitivePermission.DATA_WRITE}),
        SensitivityLevel.CRITICAL,
        SensitivePermission.CODE_EXEC,
    )
    assert_true(res_deny == "deny", "agent 缺少所需权限 → deny")


# ════════════════════════════════════════════════════
#  Section: hitl
# ════════════════════════════════════════════════════

def test_hitl_mock():
    print("\n── HITLController (Mock) ─────────────────────────────────")
    ctrl = HITLController(auto_confirm_high=False)
    ctrl_auto = HITLController(auto_confirm_high=True)

    # CRITICAL 时 logger.info 包含 "need_approval"
    with patch("pandaren.behavior.hitl_controller.logger") as mock_log:
        ctrl.check_approval(SensitivityLevel.CRITICAL, tool_name="delete_all")
        assert_true(mock_log.info.called, "CRITICAL 时触发 logger.info")
        call_args = mock_log.info.call_args[0][0]
        assert_true("need_approval" in call_args, "CRITICAL 日志含 'need_approval'")

    # HIGH + auto_confirm=True 时 logger.info 包含 "pass"
    with patch("pandaren.behavior.hitl_controller.logger") as mock_log:
        ctrl_auto.check_approval(SensitivityLevel.HIGH, tool_name="export")
        assert_true(mock_log.info.called, "HIGH auto_confirm 时触发 logger.info")
        call_args = mock_log.info.call_args[0][0]
        assert_true("pass" in call_args, "HIGH auto_confirm 日志含 'pass'")

    # HIGH + auto_confirm=False 时 logger.info 包含 "need_approval"
    with patch("pandaren.behavior.hitl_controller.logger") as mock_log:
        ctrl.check_approval(SensitivityLevel.HIGH, tool_name="export")
        call_args = mock_log.info.call_args[0][0]
        assert_true("need_approval" in call_args, "HIGH 无 auto_confirm 日志含 'need_approval'")

    # resolve_resume approved 时 logger.info 包含 "approved"
    with patch("pandaren.behavior.hitl_controller.logger") as mock_log:
        pending = _pending()
        ctrl.resolve_resume("approved", pending)
        assert_true(mock_log.info.called, "approved 时触发 logger.info")
        call_args = mock_log.info.call_args[0][0]
        assert_true("approved" in call_args, "approved 日志含 'approved'")

    # resolve_resume rejected 时 logger.info 包含 "rejected"
    with patch("pandaren.behavior.hitl_controller.logger") as mock_log:
        pending = _pending()
        ctrl.resolve_resume("rejected", pending)
        call_args = mock_log.info.call_args[0][0]
        assert_true("rejected" in call_args, "rejected 日志含 'rejected'")

    # 冻结：__delattr__ 抛 PermissionError
    assert_raises(
        PermissionError,
        lambda: delattr(ctrl, "_auto_confirm_high"),
        "HITLController __delattr__ 抛 PermissionError",
    )


# ════════════════════════════════════════════════════
#  Section: cost_calculator
# ════════════════════════════════════════════════════

def test_cost_calculator_mock():
    print("\n── StepGuard 协议契约 (Mock) ─────────────────────────────")

    # SDK 不涉及价格：只定义通用 StepGuard 协议（每步交出 StepUsage，返回 GuardDecision），
    # 价格/预算/累加全归应用层。此处验证协议的 duck-typing 识别。
    class _Guard:
        def should_halt(self, *, run_id, usage: StepUsage) -> GuardDecision:
            return GuardDecision(halt=usage.input_tokens > 1_000_000)  # 简化：超大输入即停机

    class _NotGuard:  # 缺 should_halt
        pass

    def _usage(inp: int) -> StepUsage:
        return StepUsage(model="m", input_tokens=inp, output_tokens=10, cached_tokens=0, step=1)

    assert_true(isinstance(_Guard(), StepGuard), "实现 should_halt → 是 StepGuard")
    assert_true(not isinstance(_NotGuard(), StepGuard), "缺 should_halt → 不是 StepGuard")
    assert_true(
        _Guard().should_halt(run_id="r", usage=_usage(10)).halt is False,
        "小用量不停机",
    )
    assert_true(
        _Guard().should_halt(run_id="r", usage=_usage(2_000_000)).halt is True,
        "超预算用量停机",
    )


# ════════════════════════════════════════════════════
#  Section: context_window
# ════════════════════════════════════════════════════

def test_context_window_mock():
    print("\n── ContextWindowBudget (Mock) ────────────────────────────")

    # 默认 context_window (8192) 触发 logger.warning
    with patch("pandaren.behavior.context_window_budget.logger") as mock_log:
        ContextWindowBudget()  # 使用默认值
        # 应有 warning 调用
        warning_calls = [c for c in mock_log.warning.call_args_list]
        assert_true(len(warning_calls) > 0, "默认 context_window 触发 logger.warning")

    # 显式传入 context_window 不触发 warning
    with patch("pandaren.behavior.context_window_budget.logger") as mock_log:
        ContextWindowBudget(context_window=32768)
        assert_true(not mock_log.warning.called, "显式 context_window 不触发 warning")

    # ratio sum > 1.0 触发 logger.error
    with patch("pandaren.behavior.context_window_budget.logger") as mock_log:
        try:
            ContextWindowBudget(
                context_window=8192,
                system_prompt_ratio=0.5,
                tool_schema_ratio=0.5,
                conversation_ratio=0.3,
                recall_ratio=0.0,
            )
        except BehaviorConfigError:
            pass
        assert_true(mock_log.error.called, "ratio sum > 1.0 触发 logger.error")

    # 修改冻结字段触发 logger.warning 再抛 PermissionError
    with patch("pandaren.behavior.context_window_budget.logger") as mock_log:
        budget = ContextWindowBudget(context_window=8192)
        try:
            budget.context_window = 999  # type: ignore[misc]
        except PermissionError:
            pass
        assert_true(mock_log.warning.called, "修改冻结字段触发 logger.warning")

    # build_slot_snapshot 创建后 logger.info 包含 slots 信息
    with patch("pandaren.behavior.context_window_budget.logger") as mock_log:
        ContextWindowBudget(context_window=8192)
        info_calls = mock_log.info.call_args_list
        assert_true(len(info_calls) > 0, "创建 ContextWindowBudget 触发 logger.info")


# ════════════════════════════════════════════════════
#  Section: output_guard
# ════════════════════════════════════════════════════

def test_output_guard_mock():
    print("\n── OutputGuard (Mock) ────────────────────────────────────")
    guard = OutputGuard()
    hooks = _make_hooks()
    guard.set_hooks(hooks)

    # 截断时触发 on_tool_output_truncated hook
    long_data = "B" * 300
    r = ToolResult(success=True, data=long_data, tool_name="big_tool")
    guard.check(r, max_bytes=50)

    assert_true(hooks.on_tool_output_truncated.called, "截断时触发 on_tool_output_truncated")

    # 验证回调参数
    call_kwargs = hooks.on_tool_output_truncated.call_args
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
    assert_true(kwargs.get("tool_name") == "big_tool", "on_tool_output_truncated: tool_name 正确")
    assert_true("original_size" in kwargs, "on_tool_output_truncated: original_size 参数存在")
    assert_true("max_size" in kwargs, "on_tool_output_truncated: max_size 参数存在")
    assert_true(kwargs.get("max_size") == 50, "on_tool_output_truncated: max_size=50 正确")

    # 未截断时不触发 hook
    hooks2 = _make_hooks()
    guard2 = OutputGuard()
    guard2.set_hooks(hooks2)
    short_data = "short"
    r_short = ToolResult(success=True, data=short_data, tool_name="small_tool")
    guard2.check(r_short, max_bytes=10000)
    assert_true(not hooks2.on_tool_output_truncated.called, "未截断时不触发 hook")

    # 无 hooks 时截断触发 logger.warning
    guard3 = OutputGuard()  # 未注入 hooks
    with patch("pandaren.behavior.harness.output_guard.logger") as mock_log:
        r_nohook = ToolResult(success=True, data="C" * 300, tool_name="nohook_tool")
        guard3.check(r_nohook, max_bytes=10)
        assert_true(mock_log.warning.called, "无 hooks 截断时触发 logger.warning")

    # data=None → on_tool_output_truncated 不触发
    hooks4 = _make_hooks()
    guard4 = OutputGuard()
    guard4.set_hooks(hooks4)
    r_none = ToolResult(success=True, data=None, tool_name="none_tool")
    guard4.check(r_none, max_bytes=10)
    assert_true(not hooks4.on_tool_output_truncated.called, "data=None 不触发 hook")

    # 多次截断多次触发（hooks 调用次数正确）
    hooks5 = _make_hooks()
    guard5 = OutputGuard()
    guard5.set_hooks(hooks5)
    for i in range(3):
        ri = ToolResult(success=True, data="D" * 200, tool_name=f"tool_{i}")
        guard5.check(ri, max_bytes=10)
    assert_true(hooks5.on_tool_output_truncated.call_count == 3, "3 次截断触发 hook 3 次")


# ════════════════════════════════════════════════════
#  Section: circuit_breaker
# ════════════════════════════════════════════════════

def test_circuit_breaker_mock():
    print("\n── CircuitBreakerManager (Mock) ──────────────────────────")

    # on_tool_circuit_open hook 在 CLOSED→OPEN 时触发
    mgr = CircuitBreakerManager()
    hooks = _make_hooks()
    mgr.set_hooks(hooks)

    cfg = CircuitBreakerConfig(failure_threshold=2, recovery_timeout=60.0, max_recovery_timeout=120.0)
    mgr.register("db_tool", cfg)

    mgr.record_failure("db_tool")  # 第 1 次失败，未到阈值
    assert_true(not hooks.on_tool_circuit_open.called, "1 次失败未到阈值，不触发 hook")

    mgr.record_failure("db_tool")  # 第 2 次失败，触发熔断
    assert_true(hooks.on_tool_circuit_open.called, "达到 failure_threshold=2 触发 on_tool_circuit_open")

    # 验证 on_tool_circuit_open 回调参数
    open_kwargs = hooks.on_tool_circuit_open.call_args.kwargs
    assert_true(open_kwargs.get("tool_name") == "db_tool", "on_tool_circuit_open: tool_name 正确")
    assert_true(open_kwargs.get("failure_count") == 2, "on_tool_circuit_open: failure_count=2")
    assert_true("recovery_timeout" in open_kwargs, "on_tool_circuit_open: recovery_timeout 参数存在")

    # on_tool_circuit_close hook 在 HALF_OPEN→CLOSED 时触发
    mgr2 = CircuitBreakerManager()
    hooks2 = _make_hooks()
    mgr2.set_hooks(hooks2)

    cfg2 = CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.01, max_recovery_timeout=1.0)
    mgr2.register("cache_tool", cfg2)

    mgr2.record_failure("cache_tool")  # CLOSED → OPEN
    import time
    time.sleep(0.05)

    mgr2.check("cache_tool")  # 触发 OPEN → HALF_OPEN
    mgr2.record_success("cache_tool")  # HALF_OPEN → CLOSED → 触发 on_tool_circuit_close
    assert_true(hooks2.on_tool_circuit_close.called, "HALF_OPEN→CLOSED 触发 on_tool_circuit_close")
    close_kwargs = hooks2.on_tool_circuit_close.call_args.kwargs
    assert_true(close_kwargs.get("tool_name") == "cache_tool", "on_tool_circuit_close: tool_name 正确")

    # CLOSED 状态成功不触发 on_tool_circuit_close
    hooks3 = _make_hooks()
    mgr3 = CircuitBreakerManager()
    mgr3.set_hooks(hooks3)
    cfg3 = CircuitBreakerConfig(failure_threshold=3, recovery_timeout=60.0, max_recovery_timeout=120.0)
    mgr3.register("stable_tool", cfg3)
    mgr3.record_success("stable_tool")  # CLOSED 状态成功
    assert_true(not hooks3.on_tool_circuit_close.called, "CLOSED 成功不触发 on_tool_circuit_close")

    # 无 hooks 时 OPEN 不崩溃
    mgr4 = CircuitBreakerManager()  # 未注入 hooks
    cfg4 = CircuitBreakerConfig(failure_threshold=1, recovery_timeout=60.0, max_recovery_timeout=120.0)
    mgr4.register("no_hook_tool", cfg4)
    assert_no_raises(
        lambda: mgr4.record_failure("no_hook_tool"),
        "无 hooks 时 record_failure 不崩溃",
    )

    # on_tool_circuit_open 只在首次触发时调用一次（不重复）
    hooks5 = _make_hooks()
    mgr5 = CircuitBreakerManager()
    mgr5.set_hooks(hooks5)
    cfg5 = CircuitBreakerConfig(failure_threshold=1, recovery_timeout=60.0, max_recovery_timeout=120.0)
    mgr5.register("flaky2", cfg5)
    mgr5.record_failure("flaky2")
    assert_true(hooks5.on_tool_circuit_open.call_count == 1, "on_tool_circuit_open 恰好触发 1 次")
    # 已经 OPEN，再次失败（从 OPEN 调用 record_failure 不合常规，但验证不崩溃）
    assert_no_raises(
        lambda: mgr5.record_failure("flaky2"),
        "OPEN 状态再次 record_failure 不崩溃",
    )


# ════════════════════════════════════════════════════
#  Section: idempotency
# ════════════════════════════════════════════════════

def test_idempotency_mock():
    print("\n── IdempotencyGuard (Mock) ───────────────────────────────")

    # 通过 patch asyncio.Lock 验证并发锁被使用
    guard = IdempotencyGuard()

    # 模拟并发：两个协程同时 check 相同 key，第二个应命中缓存
    async def concurrent_test():
        args = {"key": "value"}
        result_obj = ToolResult(success=True, data="cached", tool_name="tool")

        # 先 store
        await guard.store("tool", args, result_obj)

        # 两个并发 check
        r1, r2 = await asyncio.gather(
            guard.check("tool", args),
            guard.check("tool", args),
        )
        return r1, r2

    r1, r2 = async_run(concurrent_test())
    assert_true(r1 is not None, "并发 check 第一个命中缓存")
    assert_true(r2 is not None, "并发 check 第二个命中缓存")
    if r1 is not None:
        assert_true(r1.data == "cached", "并发命中缓存数据正确")

    # 验证 _make_key 调用确定性（通过 mock）
    guard2 = IdempotencyGuard()
    with patch.object(guard2, "_make_key", wraps=guard2._make_key) as mock_key:
        async_run(guard2.check("search", {"q": "test"}))
        assert_true(mock_key.called, "_make_key 被 check 调用")
        args_used = mock_key.call_args[0]
        assert_true(args_used[0] == "search", "_make_key 第一个参数是 tool_name")
        assert_true(args_used[1] == {"q": "test"}, "_make_key 第二个参数是 args")

    # store 覆盖旧值（相同 key）
    guard3 = IdempotencyGuard()
    r_old = ToolResult(success=True, data="old", tool_name="t")
    r_new = ToolResult(success=True, data="new", tool_name="t")
    async_run(guard3.store("t", {"x": 1}, r_old))
    async_run(guard3.store("t", {"x": 1}, r_new))
    cached = async_run(guard3.check("t", {"x": 1}))
    assert_true(cached is r_new, "store 相同 key 覆盖旧值")

    # reset_turn 后 _locks 也清空（确保 lock 不泄漏）
    guard4 = IdempotencyGuard()
    async_run(guard4.check("tool_a", {"n": 1}))  # 创建 lock
    assert_true(len(guard4._locks) > 0, "check 后 _locks 非空")
    guard4.reset_turn()
    assert_true(len(guard4._locks) == 0, "reset_turn 后 _locks 清空（无 lock 泄漏）")

    # sync check_sync 不受 async 缓存影响（共享同一 _cache）
    guard5 = IdempotencyGuard()
    r5 = ToolResult(success=True, data="sync_cached", tool_name="sync_tool")
    guard5.store_sync("sync_tool", {"a": 1}, r5)
    # async check 也能命中 sync store 的缓存
    async_cached = async_run(guard5.check("sync_tool", {"a": 1}))
    assert_true(async_cached is r5, "async check 能命中 sync store 的缓存（共享 _cache）")


# ════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════

from typing import Callable
SECTIONS: dict[str, Callable[[], None]] = {
    "permission_guard": test_permission_guard_mock,
    "hitl": test_hitl_mock,
    "cost_calculator": test_cost_calculator_mock,
    "context_window": test_context_window_mock,
    "output_guard": test_output_guard_mock,
    "circuit_breaker": test_circuit_breaker_mock,
    "idempotency": test_idempotency_mock,
}

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--section", choices=list(SECTIONS.keys()), default=None)
    args = parser.parse_args()

    print("=" * 60)
    print("  Behavior 层 Mock 测试")
    print("=" * 60)

    if args.section:
        SECTIONS[args.section]()
        result.summary(args.section)
    else:
        for name, fn in SECTIONS.items():
            fn()
        result.summary("all")

    sys.exit(0 if result.failed == 0 else 1)
