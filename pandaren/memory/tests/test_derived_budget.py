"""派生预算回归：阈值/保留窗口/工具结果上限必须跟着 context_window 走，而非写死绝对值。"""

from __future__ import annotations

import pytest

from pandaren.builder import AgentBuilder
from pandaren.memory.constants import (
    DEFAULT_COMPACT_BUFFER_TOKENS,
    DEFAULT_MAX_KEEP_TOKENS,
    DEFAULT_MICROCOMPACT_SINGLE_RESULT_MAX_TOKENS,
    DEFAULT_MIN_KEEP_TOKENS,
    DEFAULT_TOOL_RESULT_CAP_MAX,
    DEFAULT_TOOL_RESULT_CAP_FLOOR,
    derive_buffer_tokens,
    derive_keep_window,
    derive_single_result_max_tokens,
)

# 1M 模型 · huge 档：CW=600,000，固定槽位 system=24,000 / tool=8,000
CW_1M = 600_000
SYS_1M = 24_000
TOOL_1M = 8_000
CONV_1M = CW_1M - SYS_1M - TOOL_1M  # 568,000
THRESHOLD_1M = CONV_1M - DEFAULT_COMPACT_BUFFER_TOKENS  # 563,000


def _mem(**budget_kwargs):
    builder = AgentBuilder()
    builder.context_budget(**budget_kwargs)
    return builder._build_memory_factory()()


# ── 派生函数本身 ──────────────────────────────────────────────────────────


def test_buffer_never_exceeds_quarter_of_slot() -> None:
    assert derive_buffer_tokens(1_000_000) == DEFAULT_COMPACT_BUFFER_TOKENS
    # 小窗口不被压到 0 以下
    assert derive_buffer_tokens(8_000) == 2_000
    assert derive_buffer_tokens(1) == 0


@pytest.mark.parametrize("threshold", [10_000, 65_400, 563_000])
def test_keep_window_scales_with_threshold(threshold: int) -> None:
    min_keep, max_keep = derive_keep_window(threshold)
    assert 0 < min_keep <= max_keep
    assert max_keep == int(threshold * 0.45)
    assert min_keep == min(int(threshold * 0.12), max_keep)


def test_keep_window_monotonic_in_threshold() -> None:
    small = derive_keep_window(65_400)
    huge = derive_keep_window(563_000)
    assert small[0] < huge[0] and small[1] < huge[1]


def test_keep_window_beats_legacy_hardcoded_values() -> None:
    """回归：旧的 8,000/40,000 绝对值必须被大窗口派生值取代。"""
    min_keep, max_keep = derive_keep_window(THRESHOLD_1M)
    assert min_keep > DEFAULT_MIN_KEEP_TOKENS
    assert max_keep > DEFAULT_MAX_KEEP_TOKENS


def test_single_result_cap_bounded() -> None:
    assert derive_single_result_max_tokens(563_000) == DEFAULT_TOOL_RESULT_CAP_MAX
    assert derive_single_result_max_tokens(65_400) == 9_810
    assert derive_single_result_max_tokens(1_000) == DEFAULT_TOOL_RESULT_CAP_FLOOR


# ── builder → Memory 的真实装配 ───────────────────────────────────────────


def test_builder_derives_all_three_from_budget() -> None:
    mem = _mem(
        context_window=CW_1M,
        system_prompt_tokens_abs=SYS_1M,
        tool_schema_tokens_abs=TOOL_1M,
        recall_ratio=0.0,
    )
    policy = mem._short_term._compaction_policy
    min_keep, max_keep = derive_keep_window(THRESHOLD_1M)

    assert mem._compact_threshold == THRESHOLD_1M
    assert type(policy).__name__ == "WindowedKeepPolicy"
    assert policy._min_tokens == min_keep
    assert policy._max_tokens == max_keep
    assert mem._micro_compactor._single_result_max_tokens == (
        derive_single_result_max_tokens(THRESHOLD_1M)
    )


def test_threshold_scales_across_windows() -> None:
    small = _mem(context_window=102_400, system_prompt_tokens_abs=22_000)
    huge = _mem(context_window=CW_1M, system_prompt_tokens_abs=SYS_1M)
    assert small._compact_threshold < huge._compact_threshold
    assert small._micro_compactor._single_result_max_tokens < (
        huge._micro_compactor._single_result_max_tokens
    )


def test_restore_budget_follows_threshold(monkeypatch) -> None:
    """回归：restore 预算曾写死模块级 64,000，与配置脱钩。"""
    mem = _mem(
        context_window=CW_1M,
        system_prompt_tokens_abs=SYS_1M,
        tool_schema_tokens_abs=TOOL_1M,
        recall_ratio=0.0,
    )
    captured: dict[str, int] = {}

    def fake_load_for_restore(*, session_id: str, token_budget: int):
        captured["token_budget"] = token_budget
        return []

    monkeypatch.setattr(mem._long_term, "load_for_restore", fake_load_for_restore)
    mem.init_from_restore(task="t", session_id="sess-derive")

    assert captured["token_budget"] == THRESHOLD_1M
    assert captured["token_budget"] == mem._compact_threshold


# ── 不覆盖应用层显式配置 ──────────────────────────────────────────────────


def test_explicit_policy_wins() -> None:
    from pandaren.memory.compaction import WindowedKeepPolicy

    custom = WindowedKeepPolicy(min_keep_tokens=1_234, max_keep_tokens=5_678)
    builder = AgentBuilder()
    builder.memory(compaction_policy=custom)
    builder.context_budget(context_window=CW_1M)
    mem = builder._build_memory_factory()()

    assert mem._short_term._compaction_policy is custom
    conv_slot = builder._context_window_budget.get_slot_tokens("conversation")
    assert mem._compact_threshold == conv_slot - DEFAULT_COMPACT_BUFFER_TOKENS


def test_explicit_single_result_cap_wins() -> None:
    builder = AgentBuilder()
    builder.memory(microcompact_single_result_max_tokens=12_345)
    builder.context_budget(context_window=CW_1M)
    mem = builder._build_memory_factory()()

    assert mem._micro_compactor._single_result_max_tokens == 12_345


def test_without_budget_keeps_legacy_defaults() -> None:
    """无 context_budget 时不注入 compaction_policy → Memory 内旧默认值保持不变。"""
    mem = AgentBuilder()._build_memory_factory()()
    policy = mem._short_term._compaction_policy

    assert policy._min_tokens == DEFAULT_MIN_KEEP_TOKENS
    assert policy._max_tokens == DEFAULT_MAX_KEEP_TOKENS
    assert mem._micro_compactor._single_result_max_tokens == (
        DEFAULT_MICROCOMPACT_SINGLE_RESULT_MAX_TOKENS
    )
