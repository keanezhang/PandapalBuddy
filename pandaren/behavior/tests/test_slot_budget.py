"""SlotBudget 与 ContextWindowBudget 的预算语义单测。

承接旧 ``test_compact_budget_abs_slots.py``：**abs 槽位模式已删除**，
其语义并入「熔断线（SlotBudget）+ 实占（system_prompt_tokens）」，
见 COMPACT_BUDGET_LAYERING_SPEC §2.3 / §2.7。

覆盖的不变式（inv-A..inv-F）：
  inv-A  I2 守恒：sys 实占 + tool 熔断线 + recall + conv == CW
  inv-B  双尺子：cap == min(绝对上限, floor(CW × share))
  inv-C  单调：CW 增大 → cap 不减、conv 增
  inv-D  参数非法 → fail-fast（BehaviorConfigError）
  inv-E  不可变（frozen）
  inv-F  误差两项：cushion == round(CW×ε)，overspend == M − 预留 − CW×(1+ε)
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from pandaren.behavior.context_window_budget import (
    SDK_FALLBACK_BUDGET,
    ContextWindowBudget,
    SlotBudget,
)
from pandaren.behavior.exceptions import BehaviorConfigError

SYS_CAP = SlotBudget(24_000, 0.26)
TOOL_CAP = SlotBudget(8_000, 0.10)


def _budget(
    cw: int = 800_000,
    m: int = 1_000_000,
    sys_measured: int = 19_203,
    reserve: int = 32_000,
    recall: int = 0,
) -> ContextWindowBudget:
    return ContextWindowBudget(
        context_window=cw,
        sys_cap=SYS_CAP,
        tool_cap=TOOL_CAP,
        system_prompt_tokens=sys_measured,
        output_tokens_reserve=reserve,
        model_max_context=m,
        recall_tokens=recall,
    )


# ─────────────────────────────────────────────────────────────
# SlotBudget（inv-B / inv-D）
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cap, cw, expected",
    [
        (SlotBudget(24_000, 0.26), 800_000, 24_000),   # 大窗口 → 绝对值生效
        (SlotBudget(24_000, 0.26), 102_400, 24_000),   # floor(102400×0.26)=26,624 > 24,000
        (SlotBudget(24_000, 0.26), 50_000, 13_000),    # 极小窗口 → 占比生效
        (SlotBudget(8_000, 0.10), 80_000, 8_000),
        (SlotBudget(8_000, 0.10), 79_999, 7_999),      # 占比生效（floor）
    ],
)
def test_slot_budget_double_ruler(cap: SlotBudget, cw: int, expected: int) -> None:
    assert cap.resolve(cw) == min(cap.absolute_cap, math.floor(cw * cap.cw_share))
    assert cap.resolve(cw) == expected


@pytest.mark.parametrize(
    "absolute_cap, cw_share",
    [(0, 0.1), (-1, 0.1), (1_000, -0.01), (1_000, 1.01)],
)
def test_slot_budget_rejects_invalid_args(absolute_cap: int, cw_share: float) -> None:
    with pytest.raises(BehaviorConfigError):
        SlotBudget(absolute_cap, cw_share)


# ─────────────────────────────────────────────────────────────
# inv-A：I2 守恒（与旧 abs 模式的 slot conservation 等价，但口径换成实占+熔断线）
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cw, m, sys_measured, reserve, recall",
    [
        (800_000, 1_000_000, 19_203, 32_000, 0),
        (102_400, 128_000, 19_203, 8_000, 0),
        (160_000, 200_000, 1_849, 16_000, 0),
        (240_000, 400_000, 19_203, 32_000, 0),
        (20_000, 25_000, 2_000, 2_000, 0),
        (100_000, 128_000, 5_000, 10_000, 1_000),
    ],
)
def test_i2_slot_conservation(cw: int, m: int, sys_measured: int, reserve: int, recall: int) -> None:
    b = _budget(cw, m, sys_measured, reserve, recall)
    total = b.system_prompt_tokens + b.tool_cap_tokens + b.recall_tokens + b.conversation_tokens
    assert total == cw


def test_1m_golden() -> None:
    """1M 口径金标：CW=800,000 → conv=772,797 → T=772,297。"""
    b = _budget()
    assert b.sys_cap_tokens == 24_000
    assert b.tool_cap_tokens == 8_000
    assert b.conversation_tokens == 772_797
    assert b.compact_threshold == 772_297


# ─────────────────────────────────────────────────────────────
# inv-C：单调性
# ─────────────────────────────────────────────────────────────


def test_conversation_grows_with_window() -> None:
    prev_cap = prev_conv = 0
    for cw, m in [(50_000, 62_500), (102_400, 128_000), (800_000, 1_000_000)]:
        b = _budget(cw=cw, m=m)
        assert b.tool_cap_tokens >= prev_cap
        assert b.conversation_tokens > prev_conv
        prev_cap, prev_conv = b.tool_cap_tokens, b.conversation_tokens


# ─────────────────────────────────────────────────────────────
# inv-D：fail-fast（O3；不拒绝启动的是"未配置"，不是"配错"）
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cw", [0, -1])
def test_rejects_nonpositive_context_window(cw: int) -> None:
    with pytest.raises(BehaviorConfigError, match="context_window"):
        _budget(cw=cw)


@pytest.mark.parametrize("m", [0, -5])
def test_rejects_nonpositive_model_max_context(m: int) -> None:
    with pytest.raises(BehaviorConfigError, match="model_max_context"):
        _budget(m=m)


@pytest.mark.parametrize(
    "field, value",
    [
        ("system_prompt_tokens", -1),
        ("output_tokens_reserve", -1),
        ("recall_tokens", -1),
    ],
)
def test_rejects_negative_accounting_values(field: str, value: int) -> None:
    kwargs = {"system_prompt_tokens": 100, "output_tokens_reserve": 100, "recall_tokens": 0}
    kwargs[field] = value
    with pytest.raises(BehaviorConfigError, match=field):
        ContextWindowBudget(
            context_window=10_000,
            sys_cap=SYS_CAP,
            tool_cap=TOOL_CAP,
            model_max_context=12_500,
            **kwargs,
        )


def test_rejects_nonpositive_conversation() -> None:
    """固定槽位挤死对话区 → 必须 fail-fast，而不是给个负的 T。"""
    with pytest.raises(BehaviorConfigError, match="conversation"):
        ContextWindowBudget(
            context_window=10_000,
            sys_cap=SYS_CAP,
            tool_cap=TOOL_CAP,
            system_prompt_tokens=9_999,
            output_tokens_reserve=1_000,
            model_max_context=12_500,
        )


def test_rejects_invalid_estimator_error_ratio() -> None:
    with pytest.raises(BehaviorConfigError, match="estimator_error_ratio"):
        ContextWindowBudget(
            context_window=100_000,
            sys_cap=SYS_CAP,
            tool_cap=TOOL_CAP,
            system_prompt_tokens=1_000,
            output_tokens_reserve=1_000,
            model_max_context=125_000,
            estimator_error_ratio=1.5,
        )


# ─────────────────────────────────────────────────────────────
# inv-E：不可变
# ─────────────────────────────────────────────────────────────


def test_budget_is_frozen() -> None:
    b = _budget()
    with pytest.raises(FrozenInstanceError):
        b.context_window = 1  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────
# inv-F：误差两项（I1 的组成）
# ─────────────────────────────────────────────────────────────


def test_error_cushion_is_epsilon_times_cw() -> None:
    b = _budget()
    assert b.error_cushion_tokens == round(800_000 * 0.15) == 120_000


@pytest.mark.parametrize(
    "cw, m, reserve, expected",
    [
        (800_000, 1_000_000, 32_000, 48_000),    # 1M：容错上限 21%
        (320_000, 400_000, 32_000, 0),           # 恰好临界（零容错）
        (160_000, 200_000, 32_000, -16_000),     # 违反 I1（负缓冲）
    ],
)
def test_error_overspend_buffer(cw: int, m: int, reserve: int, expected: int) -> None:
    b = _budget(cw=cw, m=m, reserve=reserve)
    assert b.error_overspend_buffer_tokens == expected


def test_sdk_fallback_budget_matches_spec() -> None:
    """SDK 兜底：128,000 × 0.80 = 102,400；sys_cap=24,000；T=74,697。"""
    assert SDK_FALLBACK_BUDGET.context_window == 102_400
    assert SDK_FALLBACK_BUDGET.sys_cap_tokens == 24_000
    assert SDK_FALLBACK_BUDGET.tool_cap_tokens == 8_000
    assert SDK_FALLBACK_BUDGET.system_prompt_tokens == 19_203
    assert SDK_FALLBACK_BUDGET.compact_threshold == 74_697
