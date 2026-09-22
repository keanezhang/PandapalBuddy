"""pandapal/config/tests/test_context_window_resolver.py — 上下文预算解析器单测。

覆盖：模型 → 上限 → 档位 → 各字段配额（比例 × 模型上限）的全链路。
"""

from __future__ import annotations

from pandapal.config.llm.context_window_resolver import (
    describe_table,
    resolve_budget,
    resolve_model_max_context,
)


# ════════════════════════════════════════════════════════════════
#  CWR-1 模型 → 上下文上限（exact / pattern / default）
# ════════════════════════════════════════════════════════════════


def test_cwr01_pattern_match_1m():
    """CWR-1: 名字含 1m → 命中 1M 档。"""
    m, rule, fell_back = resolve_model_max_context("my-great-1m-model")
    assert m == 1_000_000
    assert rule.startswith("pattern:")
    assert fell_back is False


def test_cwr02_pattern_match_known_vendor():
    """CWR-2: 已核实条目命中对应上限（按厂商+版本）。"""
    assert resolve_model_max_context("deepseek-v3.2")[0] == 131_072   # V3.1/V3.2 = 128K
    assert resolve_model_max_context("deepseek-v4-preview")[0] == 1_000_000  # V4 = 1M
    assert resolve_model_max_context("claude-sonnet-4")[0] == 200_000


def test_cwr02b_stable_alias_not_hardcoded():
    """CWR-2b: 「稳定别名」不写死——其上限随服务端版本漂移（deepseek-chat 是 V3 还是 V4？）。

    未声明 → 走 default + WARNING，提示用户显式声明，而不是给出可能是错的预算。
    """
    m, rule, fell_back = resolve_model_max_context("deepseek-chat")
    assert m == 128_000
    assert rule == "default"
    assert fell_back is True


def test_cwr02c_env_override_wins(monkeypatch):
    """CWR-2c: 环境变量 PANDAPAL_MODEL_MAX_CONTEXT 优先级最高（应对别名漂移）。"""
    monkeypatch.setenv("PANDAPAL_MODEL_MAX_CONTEXT", "1000000")
    m, rule, fell_back = resolve_model_max_context("deepseek-chat")
    assert m == 1_000_000
    assert rule == "env:PANDAPAL_MODEL_MAX_CONTEXT"
    assert fell_back is False

    # 非法值被忽略 → 回落查表
    monkeypatch.setenv("PANDAPAL_MODEL_MAX_CONTEXT", "not-a-number")
    assert resolve_model_max_context("claude-sonnet-4")[0] == 200_000


def test_cwr03_unknown_model_falls_back_with_warning(caplog):
    """CWR-3: 未命中任何规则 → 默认上限 + WARNING（不拒绝启动，E4/E5）。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="pandapal.config.llm.context_window_resolver"):
        m, rule, fell_back = resolve_model_max_context("totally-unknown-model-xyz")
    assert m == 128_000
    assert rule == "default"
    assert fell_back is True
    assert any("未在" in r.getMessage() for r in caplog.records)


# ════════════════════════════════════════════════════════════════
#  CWR-4 档位 → 各字段配额（比例 × M）
# ════════════════════════════════════════════════════════════════


def test_cwr04_1m_model_budget_matches_spec():
    """CWR-4: 1M 模型 → 与 COMPACT_THRESHOLD_SPEC.md §5.2 的实测修正值一致。"""
    b = resolve_budget("vendor-1m-flagship")
    assert b.model_max_context == 1_000_000
    assert b.tier == "huge"
    assert b.context_window == 600_000          # M × 0.60
    assert b.system_prompt_tokens == 24_000      # M × 0.024
    assert b.tool_schema_tokens == 8_000         # M × 0.008
    assert b.recall_tokens == 0
    assert b.conversation_tokens == 568_000      # CW − 24,000 − 8,000
    assert b.fell_back is False


def test_cwr05_ratio_decreases_across_tiers():
    """CWR-5: 「比例随档位递减」——窗口越大，固定槽位比例越小。

    物理事实：system_prompt / tool_schema 的真实占用不随窗口增长
    （实测 coding 17,590、13 个工具 3,374），故比例必须递减，
    否则 1M 模型下 0.15 的比例会给出 150,000 的配额。
    """
    small = resolve_budget("deepseek-chat")        # 131,072 → small
    huge = resolve_budget("x-1m-y")                # 1,000,000 → huge
    assert small.tier == "small"
    assert huge.tier == "huge"
    # 绝对配额：大档位不因窗口放大而膨胀
    assert huge.system_prompt_tokens <= small.system_prompt_tokens * 1.2
    assert huge.tool_schema_tokens <= small.tool_schema_tokens
    # 但 conversation 随窗口显著增长（这才是该动态的部分）
    assert huge.conversation_tokens > small.conversation_tokens * 5


def test_cwr06_fixed_slots_leave_room_for_conversation():
    """CWR-6: 任何档位下，固定槽位都必须给 conversation 留下正数。"""
    for model_id in ("deepseek-chat", "claude-sonnet-4", "gpt-4o", "x-1m-y", "unknown-zzz"):
        b = resolve_budget(model_id)
        fixed = b.system_prompt_tokens + b.tool_schema_tokens + b.recall_tokens
        assert b.conversation_tokens > 0, model_id
        assert fixed + b.conversation_tokens == b.context_window, model_id
        # 固定槽位不应超过窗口的一半（否则对话区被挤爆）
        assert fixed < b.context_window * 0.5, model_id


def test_cwr07_builder_kwargs_shape():
    """CWR-7: to_builder_kwargs() 产出的键必须能被 AgentBuilder.context_budget() 接受。"""
    from pandaren.builder import AgentBuilder

    import inspect

    b = resolve_budget("x-1m-y")
    kwargs = b.to_builder_kwargs()
    accepted = set(inspect.signature(AgentBuilder.context_budget).parameters) - {"self"}
    assert set(kwargs) <= accepted, set(kwargs) - accepted


def test_cwr08_describe_table_renders_all_tiers():
    """CWR-8: 档位表渲染包含全部档位与关键列。"""
    text = describe_table()
    for tier in ("small", "medium", "large", "huge"):
        assert tier in text
    assert "system" in text and "tool" in text and "conversation" in text
