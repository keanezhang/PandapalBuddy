"""上下文预算解析器单测：模型 id → ``(max_context, max_output)``。

只覆盖**模型事实**解析；窗口比例 / 槽位预算在 ``context_budget.py``（见 SPEC §3.2）。
"""

from __future__ import annotations

import logging

from pandapal.config.llm.context_window_resolver import resolve_model_max_context


# ════════════════════════════════════════════════════════════════
#  模型 → 上下文上限（exact / pattern / default）
# ════════════════════════════════════════════════════════════════


def test_pattern_match_1m():
    assert resolve_model_max_context("my-great-1m-model")[0] == 1_000_000


def test_known_vendor_true_values():
    """真值层照抄官方（2 的幂）。"""
    assert resolve_model_max_context("deepseek-v3.2")[0] == 131_072
    assert resolve_model_max_context("deepseek-v4-preview")[0] == 1_000_000
    assert resolve_model_max_context("gemini-2.5-pro")[0] == 1_048_576
    assert resolve_model_max_context("claude-sonnet-4")[0] == 200_000


def test_stable_alias_not_hardcoded():
    """稳定别名不写死 → 走 default（128K）。"""
    assert resolve_model_max_context("deepseek-chat")[0] == 128_000


def test_llama_3_8b_must_not_hit_131072():
    """回归：真值段通配必须精确到版本；写宽会把 8K 模型估大 → 直接 400。"""
    assert resolve_model_max_context("llama-3-8b")[0] == 128_000
    assert resolve_model_max_context("llama-3.1-8b")[0] == 131_072


def test_default_carries_max_output():
    """[default] 声明了 max_output → 未命中规则的模型拿到它（输出预留来源②）。"""
    assert resolve_model_max_context("totally-unknown-model-xyz") == (128_000, 32_000)


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("PANDAPAL_MODEL_MAX_CONTEXT", "1000000")
    assert resolve_model_max_context("deepseek-chat")[0] == 1_000_000

    # 非法值被忽略 → 回落查表
    monkeypatch.setenv("PANDAPAL_MODEL_MAX_CONTEXT", "not-a-number")
    assert resolve_model_max_context("claude-sonnet-4")[0] == 200_000


def test_unknown_model_warns(caplog):
    with caplog.at_level(
        logging.WARNING, logger="pandapal.config.llm.context_window_resolver"
    ):
        m, _out = resolve_model_max_context("totally-unknown-model-xyz")
    assert m == 128_000
    assert any("未在" in r.getMessage() for r in caplog.records)


def test_empty_model_id_uses_default():
    assert resolve_model_max_context("") == (128_000, 32_000)
