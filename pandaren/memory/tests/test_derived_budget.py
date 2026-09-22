"""派生预算回归：阈值 / 保留窗口 / 工具结果上限必须由「同一个预算对象 + profile」派生，
而非两处写死绝对值（见 COMPACT_BUDGET_LAYERING_SPEC §2.7）。
"""

from __future__ import annotations

import pytest

from pandaren.behavior.context_window_budget import (
    SDK_FALLBACK_BUDGET,
    ContextWindowBudget,
    SlotBudget,
)
from pandaren.builder import AgentBuilder
from pandaren.memory.constants import (
    BALANCED,
    TOOL_RESULT_CAP_FLOOR,
    TOOL_RESULT_CAP_MAX,
)

# 1M 模型 · 固定比例 0.80：CW=800,000；熔断线 system=24,000 / tool=8,000；
# 记账：sys 实占 19,203、tool 用熔断线 8,000 → conv=772,797 → T=772,297
CW_1M = 800_000
M_1M = 1_000_000
SYS_CAP = SlotBudget(24_000, 0.26)
TOOL_CAP = SlotBudget(8_000, 0.10)
SYS_MEASURED = 19_203
OUTPUT_RESERVE = 32_000


def _budget(
    cw: int = CW_1M,
    m: int = M_1M,
    sys_measured: int = SYS_MEASURED,
    output_reserve: int = OUTPUT_RESERVE,
) -> ContextWindowBudget:
    return ContextWindowBudget(
        context_window=cw,
        sys_cap=SYS_CAP,
        tool_cap=TOOL_CAP,
        system_prompt_tokens=sys_measured,
        output_tokens_reserve=output_reserve,
        model_max_context=m,
    )


def _mem(budget: ContextWindowBudget | None = None, **memory_kwargs):
    builder = AgentBuilder()
    if budget is not None:
        builder.context_budget(budget)
    if memory_kwargs:
        builder.memory(**memory_kwargs)
    return builder._build_memory_factory()()


# ── profile 派生本身 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("T", [10_000, 74_697, 772_297])
def test_keep_window_scales_with_threshold(T: int) -> None:
    min_keep = BALANCED.min_keep_tokens(T)
    max_keep = BALANCED.max_keep_tokens(T)
    assert 0 < min_keep <= max_keep
    assert max_keep == max(1, int(T * BALANCED.max_keep_ratio))
    assert min_keep == max(1, min(int(T * BALANCED.min_keep_ratio), max_keep))


def test_keep_window_monotonic_in_threshold() -> None:
    assert BALANCED.min_keep_tokens(74_697) < BALANCED.min_keep_tokens(772_297)
    assert BALANCED.max_keep_tokens(74_697) < BALANCED.max_keep_tokens(772_297)


def test_single_result_cap_bounded() -> None:
    assert BALANCED.single_result_max_tokens(772_297) == TOOL_RESULT_CAP_MAX
    assert BALANCED.single_result_max_tokens(74_697) == 11_204
    assert BALANCED.single_result_max_tokens(1_000) == TOOL_RESULT_CAP_FLOOR


def test_single_result_cap_beats_legacy_hardcoded_value() -> None:
    """回归：旧代码写死 20,000；新口径必须按 T 派生（大窗口 = 30,000）。"""
    assert BALANCED.single_result_max_tokens(772_297) == TOOL_RESULT_CAP_MAX


def test_budget_derivations_match_spec() -> None:
    budget = _budget()
    assert budget.tool_cap_tokens == 8_000
    assert budget.sys_cap_tokens == 24_000
    assert budget.conversation_tokens == 800_000 - 19_203 - 8_000
    assert budget.compact_threshold == 772_797 - 500


# ── builder → Memory 的真实装配 ───────────────────────────────────────────


def test_builder_derives_all_three_from_budget() -> None:
    budget = _budget()
    mem = _mem(budget)
    policy = mem._short_term._compaction_policy
    T = budget.compact_threshold

    assert mem._compact_threshold == T
    assert type(policy).__name__ == "WindowedKeepPolicy"
    assert policy._min_tokens == BALANCED.min_keep_tokens(T)
    assert policy._max_tokens == BALANCED.max_keep_tokens(T)
    assert policy._min_text_messages == BALANCED.min_keep_text_messages
    assert mem._micro_compactor._single_result_max_tokens == (
        BALANCED.single_result_max_tokens(T)
    )


def test_threshold_scales_across_windows() -> None:
    small = _mem(_budget(cw=102_400, m=128_000, sys_measured=19_203))
    huge = _mem(_budget())
    assert small._compact_threshold < huge._compact_threshold
    assert (
        small._micro_compactor._single_result_max_tokens
        < huge._micro_compactor._single_result_max_tokens
    )


def test_restore_budget_follows_threshold(monkeypatch) -> None:
    """回归：restore 预算曾写死模块级常量，与配置脱钩。"""
    mem = _mem(_budget())
    captured: dict[str, int] = {}

    def fake_load_for_restore(*, session_id: str, token_budget: int):
        captured["token_budget"] = token_budget
        return []

    monkeypatch.setattr(mem._long_term, "load_for_restore", fake_load_for_restore)
    mem.init_from_restore(task="t", session_id="sess-derive")

    assert captured["token_budget"] == mem._compact_threshold


# ── 不覆盖应用层显式配置 ──────────────────────────────────────────────────


def test_explicit_policy_wins() -> None:
    from pandaren.memory.compaction import WindowedKeepPolicy

    custom = WindowedKeepPolicy(
        min_keep_tokens=1_234, min_keep_text_messages=2, max_keep_tokens=5_678
    )
    budget = _budget()
    mem = _mem(budget, compaction_policy=custom)

    assert mem._short_term._compaction_policy is custom
    assert mem._compact_threshold == budget.compact_threshold


def test_explicit_single_result_cap_wins() -> None:
    mem = _mem(_budget(), microcompact_single_result_max_tokens=12_345)
    assert mem._micro_compactor._single_result_max_tokens == 12_345


def test_without_budget_uses_sdk_fallback() -> None:
    """无 context_budget 时由 SDK 兜底预算对象派生（SDK 可独立运行）。"""
    mem = AgentBuilder()._build_memory_factory()()
    policy = mem._short_term._compaction_policy
    T = SDK_FALLBACK_BUDGET.compact_threshold

    assert mem._compact_threshold == T
    assert policy._min_tokens == BALANCED.min_keep_tokens(T)
    assert policy._max_tokens == BALANCED.max_keep_tokens(T)
    assert mem._micro_compactor._single_result_max_tokens == (
        BALANCED.single_result_max_tokens(T)
    )
