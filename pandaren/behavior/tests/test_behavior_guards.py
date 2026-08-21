"""执行前控制链 + 简单守卫 —— pytest 落地（设计 §4.2/4.3/4.7/4.8/4.9）。

覆盖：
  PG-01..04  PermissionGuard（unit，纯函数零 mock）
  HITL-01..06 HITLController（unit，纯决策零 mock，fail-closed）
  CWB-01..05 ContextWindowBudget（unit，ratio 校验 + 冻结 + slot 计算）
  EL-01..03  ExecutionLimits（冻结 + 配置倒挂）
  EP-01..03  ErrorPolicy（指数退避 + 封顶 + 冻结）
  RL-01..04  RateLimiter（turn 级频率控制）
  HALT-01..03 HaltChecker（失败硬停止）
  SG-01     StepGuard（runtime_checkable Protocol 契约）

确定性：全部同步纯函数，无时间/随机/网络依赖；浮点用 == 精确值（1.0·2^n 可精确表示）。
"""

from __future__ import annotations

import logging
import math

import pytest

from pandaren.behavior.context_window_budget import ContextWindowBudget
from pandaren.behavior.error_policy import ErrorPolicy
from pandaren.behavior.exceptions import BehaviorConfigError
from pandaren.behavior.execution_limits import ExecutionLimits
from pandaren.behavior.harness.halt import HaltChecker
from pandaren.behavior.harness.rate_limiter import RateLimiter
from pandaren.behavior.hitl_controller import (
    HITLController,
    PendingApproval,
    ResumeDecision,
)
from pandaren.behavior.permission_guard import PermissionGuard
from pandaren.behavior.step_guard import GuardDecision, StepGuard, StepUsage
from pandaren.identity.models import SensitivePermission
from pandaren.tool.definition.tool_result import ToolResult
from pandaren.tool.types import SensitivityLevel


# ─── §4.2 PermissionGuard（unit，纯函数零 mock）───────────────────────────


def test_pg_01_ordinary_tool_no_permission_required_allows():
    """PG-01: 普通工具（tool_permission=None）→ allow（inv-PG-1）"""
    result = PermissionGuard().check_permission(frozenset(), SensitivityLevel.LOW, None)
    assert result == "allow"


def test_pg_02_matching_sensitive_permission_allows():
    """PG-02: 身份声明了工具要求的敏感权限 → allow（inv-PG-2）"""
    result = PermissionGuard().check_permission(
        frozenset({SensitivePermission.DATA_DELETE}),
        SensitivityLevel.HIGH,
        SensitivePermission.DATA_DELETE,
    )
    assert result == "allow"


def test_pg_03_missing_permission_denies_and_warns(caplog):
    """PG-03: 身份未声明所需敏感权限（空集）→ deny + warning（inv-PG-3 + Risk-PG-1）"""
    with caplog.at_level(logging.WARNING, logger="pandaren.behavior.permission_guard"):
        result = PermissionGuard().check_permission(
            frozenset(), SensitivityLevel.HIGH, SensitivePermission.DATA_DELETE,
        )
    assert result == "deny"
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_pg_04_holding_other_permission_denies_exact_match():
    """PG-04: 持有其他敏感权限 ≠ 持有工具要求的权限 → deny + 确定性（inv-PG-4/5）"""
    guard = PermissionGuard()
    d1 = guard.check_permission(
        frozenset({SensitivePermission.NETWORK_CALL}),
        SensitivityLevel.HIGH,
        SensitivePermission.DATA_DELETE,
    )
    d2 = guard.check_permission(
        frozenset({SensitivePermission.NETWORK_CALL}),
        SensitivityLevel.HIGH,
        SensitivePermission.DATA_DELETE,
    )
    assert d1 == d2 == "deny"


# ─── §4.3 HITLController（unit，纯决策零 mock，fail-closed）─────────────────


def test_hitl_01_critical_forced_approval_not_bypassed():
    """HITL-01: CRITICAL 强制 need_approval，auto_confirm_high=True 也不放行（HC6，Risk-HITL-1）"""
    ctrl = HITLController(auto_confirm_high=True)
    assert ctrl.check_approval(SensitivityLevel.CRITICAL.value, "delete_all") == "need_approval"


def test_hitl_02_high_without_auto_confirm_requires_approval():
    """HITL-02: HIGH × auto_confirm_high=False → need_approval（inv-HITL-2）"""
    ctrl = HITLController(auto_confirm_high=False)
    assert ctrl.check_approval(SensitivityLevel.HIGH.value, "send_email") == "need_approval"


def test_hitl_03_high_with_auto_confirm_passes():
    """HITL-03: HIGH × auto_confirm_high=True → pass（inv-HITL-2）"""
    ctrl = HITLController(auto_confirm_high=True)
    assert ctrl.check_approval(SensitivityLevel.HIGH.value, "send_email") == "pass"


@pytest.mark.parametrize(
    "sensitivity",
    [SensitivityLevel.LOW, SensitivityLevel.MEDIUM],
)
def test_hitl_04_low_medium_passes(sensitivity):
    """HITL-04: MEDIUM/LOW → pass（低风险放行，inv-HITL-3）"""
    ctrl = HITLController(auto_confirm_high=False)
    assert ctrl.check_approval(sensitivity.value, "read_file") == "pass"


def _make_pending() -> PendingApproval:
    return PendingApproval(
        tool_call={"id": "call_1", "function": {"name": "send_email", "arguments": "{}"}},
        tool_name="send_email",
        tool_args={"to": "alice"},
        sensitivity=SensitivityLevel.HIGH.value,
        step_n=1,
    )


def test_hitl_05_resume_approved_executes_pending():
    """HITL-05: resolve_resume(approved) → execute_pending（执行 pending 中保存的调用，inv-HITL-4）"""
    pending = _make_pending()
    decision = HITLController().resolve_resume("approved", pending)
    assert isinstance(decision, ResumeDecision)
    assert decision.action == "execute_pending"
    assert decision.pending is pending


def test_hitl_06_resume_rejected_halts_fail_closed():
    """HITL-06: resolve_resume(rejected) → reject_and_halt（终止 run，pending 绝不执行，Risk-HITL-2）"""
    pending = _make_pending()
    decision = HITLController().resolve_resume("rejected", pending)
    assert isinstance(decision, ResumeDecision)
    assert decision.action == "reject_and_halt"
    assert decision.pending is pending


# ─── §4.7 ContextWindowBudget（unit，ratio 校验 + 冻结 + slot 计算）─────────


def test_cwb_01_default_ratio_slot_tokens_floor():
    """CWB-01: 默认比值 × 131072 → floor 取整配额（inv-CWB-3，0.15×131072=19660.8→19660）"""
    budget = ContextWindowBudget(context_window=131072)
    assert budget.get_slot_tokens("system_prompt") == math.floor(0.15 * 131072)


def test_cwb_02_ratio_sum_over_one_raises():
    """CWB-02: ratio 之和 1.1 > 1.0 → BehaviorConfigError（构造期 fail-fast，Risk-CWB-1）"""
    with pytest.raises(BehaviorConfigError):
        ContextWindowBudget(
            context_window=131072,
            system_prompt_ratio=0.5,
            tool_schema_ratio=0.3,
            conversation_ratio=0.2,
            recall_ratio=0.1,
        )


def test_cwb_03_single_ratio_out_of_range_raises():
    """CWB-03: 单 ratio 越界（conversation_ratio=1.5 > 1.0）→ BehaviorConfigError（inv-CWB-1）"""
    with pytest.raises(BehaviorConfigError):
        ContextWindowBudget(context_window=131072, conversation_ratio=1.5)


def test_cwb_04_frozen_setattr_raises():
    """CWB-04: 冻结 —— 修改 system_prompt_ratio → PermissionError（配额声明后不可篡改，Risk-CWB-2）"""
    budget = ContextWindowBudget(context_window=131072)
    with pytest.raises(PermissionError):
        budget.system_prompt_ratio = 0.9


def test_cwb_05_default_window_snapshot_and_warning(caplog):
    """CWB-05: 未显式传 context_window → 默认值 + warning；build_slot_snapshot 全量（inv-CWB-4/5）"""
    with caplog.at_level(logging.WARNING, logger="pandaren.behavior.context_window_budget"):
        budget = ContextWindowBudget()
    snapshot = budget.build_slot_snapshot()
    assert snapshot.system_prompt_tokens == math.floor(0.15 * 128000)  # 19200
    assert snapshot.tool_schema_tokens == math.floor(0.10 * 128000)   # 12800
    assert snapshot.conversation_tokens == math.floor(0.50 * 128000)  # 64000
    assert snapshot.recall_tokens == math.floor(0.10 * 128000)        # 12800
    assert any(r.levelname == "WARNING" for r in caplog.records)


# ─── §4.8 ExecutionLimits / ErrorPolicy（unit，零 mock）─────────────────────


def test_el_01_default_values():
    """EL-01: ExecutionLimits 默认值 30 / 120.0 / 600.0（inv-EL-1）"""
    limits = ExecutionLimits()
    assert limits.max_steps == 30
    assert limits.step_timeout == 120.0
    assert limits.total_timeout == 600.0


def test_el_02_step_timeout_exceeds_total_raises():
    """EL-02: step_timeout(600) > total_timeout(120) 倒挂 → BehaviorConfigError（Risk-EL-1）"""
    with pytest.raises(BehaviorConfigError):
        ExecutionLimits(max_steps=30, step_timeout=600.0, total_timeout=120.0)


def test_el_03_frozen_setattr_and_delattr_raise():
    """EL-03: 冻结 —— setattr 与 delattr 均抛 PermissionError（HC5 完全冻结，inv-EL-3）"""
    limits = ExecutionLimits()
    with pytest.raises(PermissionError):
        limits.max_steps = 100
    with pytest.raises(PermissionError):
        del limits.max_steps


@pytest.mark.parametrize(
    "attempt, expected",
    [
        (0, 1.0),  # 未封顶区：base × 2^0
        (1, 2.0),  # 未封顶区：base × 2^1
        (2, 4.0),  # 未封顶区：base × 2^2
    ],
)
def test_ep_01_backoff_growth(attempt, expected):
    """EP-01: calculate_delay 未封顶区 = min(base × 2^attempt, max)（inv-EP-1）"""
    policy = ErrorPolicy(max_retries=3, base_delay_s=1.0, max_delay_s=30.0)
    assert policy.calculate_delay(attempt) == expected


@pytest.mark.parametrize(
    "attempt, expected",
    [
        (4, 16.0),  # 未封顶：2^4=16 < 30
        (5, 30.0),  # 封顶：2^5=32 > 30
        (6, 30.0),  # 封顶：2^6=64 > 30，不再无界增长
    ],
)
def test_ep_02_backoff_capped_at_max_delay(attempt, expected):
    """EP-02: 退避封顶 —— 2^n ≥ max/base 时恒为 max_delay（Risk-EP-1）"""
    policy = ErrorPolicy(max_retries=3, base_delay_s=1.0, max_delay_s=30.0)
    assert policy.calculate_delay(attempt) == expected


def test_ep_03_frozen_setattr_raises():
    """EP-03: ErrorPolicy 冻结 —— 修改 max_retries → PermissionError（inv-EP-2）"""
    policy = ErrorPolicy()
    with pytest.raises(PermissionError):
        policy.max_retries = 10


# ─── §4.9 RateLimiter / HaltChecker / StepGuard ─────────────────────────────


def test_rl_01_under_limit_passes():
    """RL-01: 未超限 → 放行（None）+ 计数（inv-RL-1）"""
    rl = RateLimiter()
    result = rl.check("write_file", max_calls=5)
    assert result is None
    assert rl.get_count("write_file") == 1


def test_rl_02_over_limit_returns_rejection_toolresult():
    """RL-02: 超限 → 拒绝 ToolResult（非 None，success=False，Risk-RL-1）"""
    rl = RateLimiter()
    rl.check("write_file", max_calls=2)
    rl.check("write_file", max_calls=2)

    result = rl.check("write_file", max_calls=2)

    assert result is not None
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.tool_name == "write_file"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known-gap: 设计 inv-RL-2 要求超限调用同样计数（get_count==3），"
        "实现超限分支不递增计数器（当前 get_count==2）。修复后本用例应转 passed。"
    ),
)
def test_rl_02_rejected_call_still_counts():
    """RL-02(副作用): 超限调用同样计数（设计预期 ==3，inv-RL-2）"""
    rl = RateLimiter()
    rl.check("write_file", max_calls=2)
    rl.check("write_file", max_calls=2)
    rl.check("write_file", max_calls=2)
    assert rl.get_count("write_file") == 3


def test_rl_03_no_max_calls_counts_only():
    """RL-03: 未配置 max_calls（显式 None）→ 只计数不拦截（inv-RL-3）"""
    rl = RateLimiter()
    for _ in range(10):
        assert rl.check("write_file", max_calls=None) is None
    assert rl.get_count("write_file") == 10


def test_rl_04_reset_turn_clears_counters():
    """RL-04: reset_turn 清零计数（跨 turn 重新计数，inv-RL-4）"""
    rl = RateLimiter()
    for _ in range(3):
        rl.check("write_file", max_calls=5)
    assert rl.get_count("write_file") == 3

    rl.reset_turn()

    assert rl.get_count("write_file") == 0


def test_halt_01_failure_with_halt_on_failure_halts():
    """HALT-01: success=False × halt_on_failure=True → halt（硬停止信号，Risk-HALT-1）"""
    assert HaltChecker().should_halt(success=False, halt_on_failure=True) is True


def test_halt_02_success_with_halt_on_failure_no_halt():
    """HALT-02: success=True × halt_on_failure=True → 不停（成功不硬停）"""
    assert HaltChecker().should_halt(success=True, halt_on_failure=True) is False


def test_halt_03_failure_without_halt_on_failure_no_halt():
    """HALT-03: success=False × halt_on_failure=False → 不停（仅失败不硬停）"""
    assert HaltChecker().should_halt(success=False, halt_on_failure=False) is False


class GoodGuard:
    """满足 StepGuard 契约的鸭子类型实现。"""

    def should_halt(self, *, run_id: str, usage: StepUsage) -> GuardDecision:
        return GuardDecision(halt=False, reason="")


class BadGuard:
    """缺 should_halt 方法，不满足契约。"""

    pass


def test_sg_01_step_guard_protocol_duck_typing():
    """SG-01: StepGuard runtime_checkable —— 鸭子类型实现可识别、缺方法不可识别（inv-SG-1）"""
    assert isinstance(GoodGuard(), StepGuard) is True
    assert isinstance(BadGuard(), StepGuard) is False
    # 语义：未注入 step_guard 时 executor 全链路不受影响（由 EX-01 隐含验证，SDK 不因无守卫而停机）
