"""compact-budget.design.md §6.2 — 上下文窗口预算绝对槽位模式单测。

覆盖用例：CWB-1 / CWB-2 / CWB-3 / CWB-4 / CWB-5 / CWB-6 / CWB-7
风险映射：R2 [P0] + inv-2（槽位守恒）+ inv-3（残差正数）
Oracle：golden value（独立手算）+ property（parametrize 覆盖 abs 参数空间）。
纯数据类零 mock。
"""

from __future__ import annotations

import pytest

from pandaren.behavior.context_window_budget import ContextWindowBudget
from pandaren.behavior.exceptions import BehaviorConfigError


# ─────────────────────────────────────────────────────────────
# CWB-1: 默认 ratio 模式（recall 默认 0 回收配额）
# ─────────────────────────────────────────────────────────────


def test_default_ratio_mode_with_recall_zero() -> None:
    b = ContextWindowBudget(context_window=100_000)

    assert b.system_prompt_tokens == 15_000     # floor(100_000 × 0.15)
    assert b.tool_schema_tokens == 10_000       # floor(100_000 × 0.10)
    assert b.recall_tokens == 0                 # G4 回归：recall 不再白占 10_000
    assert b.conversation_tokens == 50_000      # floor(100_000 × 0.50)

    assert b.is_abs_mode is False
    assert b.system_prompt_tokens_abs is None
    assert b.tool_schema_tokens_abs is None
    assert b.get_slot_tokens("recall") == 0


# ─────────────────────────────────────────────────────────────
# CWB-2: 双绝对槽位——conversation 吸收剩余，ratio 被忽略
# ─────────────────────────────────────────────────────────────


def test_dual_abs_mode_conversation_absorbs_remainder() -> None:
    b = ContextWindowBudget(
        context_window=600_000,
        system_prompt_tokens_abs=24_000,
        tool_schema_tokens_abs=8_000,
        conversation_ratio=0.50,
        recall_ratio=0.0,
    )

    assert b.system_prompt_tokens == 24_000
    assert b.tool_schema_tokens == 8_000
    assert b.recall_tokens == 0
    # 600_000 − 24_000 − 8_000 − 0；≠ floor(600_000×0.50)=300_000 → ratio 被忽略
    assert b.conversation_tokens == 568_000
    assert b.conversation_tokens != 300_000
    assert b.is_abs_mode is True

    snap = b.build_slot_snapshot()
    total = (
        snap.system_prompt_tokens
        + snap.tool_schema_tokens
        + snap.conversation_tokens
        + snap.recall_tokens
    )
    assert total == 600_000  # inv-2


# ─────────────────────────────────────────────────────────────
# CWB-3: 单绝对槽位——只给 sys abs，tool 仍走 ratio
# ─────────────────────────────────────────────────────────────


def test_single_abs_mode_tool_still_uses_ratio() -> None:
    b = ContextWindowBudget(
        context_window=600_000,
        system_prompt_tokens_abs=24_000,
        recall_ratio=0.0,
    )

    assert b.system_prompt_tokens == 24_000
    assert b.tool_schema_tokens == 60_000   # floor(600_000 × 0.10)
    assert b.conversation_tokens == 516_000  # 600_000 − 24_000 − 60_000
    assert b.is_abs_mode is True
    assert b.tool_schema_tokens_abs is None


# ─────────────────────────────────────────────────────────────
# CWB-4: 残差 ≤ 0 fail-fast（含恰好 =0 边界）
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "context_window, sys_abs, tool_abs",
    [
        (1_000, 600, 600),  # 残差 = −200 < 0
        (1_200, 600, 600),  # 残差 = 0（恰好边界）
    ],
)
def test_abs_residual_non_positive_fail_fast(
    context_window: int, sys_abs: int, tool_abs: int
) -> None:
    # inv-3：conversation > 0，否则 fail-fast
    with pytest.raises(BehaviorConfigError, match="conversation"):
        ContextWindowBudget(
            context_window=context_window,
            system_prompt_tokens_abs=sys_abs,
            tool_schema_tokens_abs=tool_abs,
            recall_ratio=0.0,
        )


# ─────────────────────────────────────────────────────────────
# CWB-5: abs 槽位非法值 fail-fast
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_abs", [0, -1, 2.5, "24000"])
def test_abs_slot_invalid_value_fail_fast(bad_abs) -> None:
    with pytest.raises(BehaviorConfigError, match="system_prompt_tokens_abs"):
        ContextWindowBudget(
            context_window=100_000,
            system_prompt_tokens_abs=bad_abs,
        )


# ─────────────────────────────────────────────────────────────
# CWB-6: effective_ratios 校验——abs 接管后 conversation_ratio 零化，tool 仍参与
# ─────────────────────────────────────────────────────────────


def test_dual_abs_zeroes_conversation_ratio_in_sum() -> None:
    # 若 conversation_ratio=0.9 未被零化，sum 会超 1.0 → 本应抛错；实际不抛
    b = ContextWindowBudget(
        context_window=100_000,
        system_prompt_tokens_abs=24_000,
        tool_schema_tokens_abs=8_000,
        conversation_ratio=0.9,
        recall_ratio=0.0,
    )
    assert b.conversation_tokens == 68_000  # 100_000 − 24_000 − 8_000 − 0


def test_single_abs_still_validates_tool_and_recall_ratios() -> None:
    # tool 未零化：0.9 + 0.2 = 1.1 > 1.0 → fail-fast
    with pytest.raises(BehaviorConfigError):
        ContextWindowBudget(
            context_window=100_000,
            system_prompt_tokens_abs=24_000,
            tool_schema_ratio=0.9,
            recall_ratio=0.2,
        )


# ─────────────────────────────────────────────────────────────
# CWB-7: 槽位守恒（property 入口，覆盖 abs 参数空间）
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "context_window, sys_abs, tool_abs, recall_ratio",
    [
        (100_000, 24_000, 8_000, 0.0),
        (600_000, 24_000, 8_000, 0.0),
        (600_000, 24_000, None, 0.0),    # 单 sys abs
        (600_000, None, 8_000, 0.0),     # 单 tool abs
        (128_000, 17_590, 3_374, 0.0),
        (200_000, 30_000, 10_000, 0.05),
        (1_000_000, 50_000, 20_000, 0.10),
        (2_000_000, 100_000, 50_000, 0.10),
        (50_000, 10_000, 5_000, 0.0),
        (10_000, 2_000, 1_000, 0.20),
    ],
)
def test_abs_mode_slot_conservation(
    context_window: int, sys_abs: int | None, tool_abs: int | None, recall_ratio: float
) -> None:
    # inv-2：绝对槽位模式下 Σ(system+tool+conversation+recall) == CW
    b = ContextWindowBudget(
        context_window=context_window,
        system_prompt_tokens_abs=sys_abs,
        tool_schema_tokens_abs=tool_abs,
        recall_ratio=recall_ratio,
    )
    snap = b.build_slot_snapshot()
    total = (
        snap.system_prompt_tokens
        + snap.tool_schema_tokens
        + snap.conversation_tokens
        + snap.recall_tokens
    )
    assert total == context_window
    assert snap.conversation_tokens == (
        context_window
        - snap.system_prompt_tokens
        - snap.tool_schema_tokens
        - snap.recall_tokens
    )
