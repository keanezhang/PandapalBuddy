"""pandapal/config/tests/test_compact_budget_cross_layer.py

覆盖用例 ID：XTH-1（builder 阈值 == resolver footer 阈值，R1 [P0] + inv-1）。
"""

from __future__ import annotations

import pytest

from pandapal.config.llm import context_window_resolver as cr
from pandaren.builder import AgentBuilder
from pandaren.memory.constants import derive_compact_threshold


@pytest.fixture(autouse=True)
def _reset_resolver(monkeypatch):
    monkeypatch.delenv("PANDAPAL_MODEL_MAX_CONTEXT", raising=False)
    saved = cr._TABLE
    cr._TABLE = None
    yield
    cr._TABLE = saved


def _four_tier_table() -> dict:
    """model_id → 精确 M（四档），tiers 与 model_context_windows.toml 一致。"""
    return {
        "default": {"value": 128_000},
        "models": {
            "m128k": 128_000,
            "m200k": 200_000,
            "m400k": 400_000,
            "m1m": 1_000_000,
        },
        "patterns": {},
        "tiers": [
            {
                "name": "small",
                "max_context": 160_000,
                "context_window_ratio": 0.80,
                "system_prompt_ratio": 0.172,
                "system_prompt_floor": 22_000,
                "tool_schema_ratio": 0.078,
                "tool_schema_floor": 8_000,
                "recall_ratio": 0.0,
            },
            {
                "name": "medium",
                "max_context": 300_000,
                "context_window_ratio": 0.80,
                "system_prompt_ratio": 0.110,
                "system_prompt_floor": 22_000,
                "tool_schema_ratio": 0.050,
                "tool_schema_floor": 8_000,
                "recall_ratio": 0.0,
            },
            {
                "name": "large",
                "max_context": 600_000,
                "context_window_ratio": 0.80,
                "system_prompt_ratio": 0.060,
                "system_prompt_floor": 22_000,
                "tool_schema_ratio": 0.020,
                "tool_schema_floor": 8_000,
                "recall_ratio": 0.0,
            },
            {
                "name": "huge",
                "max_context": 999_999_999,
                "context_window_ratio": 0.60,
                "system_prompt_ratio": 0.024,
                "system_prompt_floor": 24_000,
                "tool_schema_ratio": 0.008,
                "tool_schema_floor": 8_000,
                "recall_ratio": 0.0,
            },
        ],
    }


# R1 [P0] + inv-1 [property]：resolver footer 阈值 == builder 阈值 == Memory 阈值
@pytest.mark.parametrize("model_id", ["m128k", "m200k", "m400k", "m1m"])
def test_xth01_builder_threshold_equals_resolver_footer(monkeypatch, model_id):
    monkeypatch.setattr(cr, "_load_table", lambda: _four_tier_table())
    budget = cr.resolve_budget(model_id)
    b = AgentBuilder().context_budget(**budget.to_builder_kwargs())
    mem = b._build_memory_factory()()

    t_footer = derive_compact_threshold(budget.conversation_tokens)
    t_builder = derive_compact_threshold(b._context_window_budget.get_slot_tokens("conversation"))

    # 恒等式前提：resolver 与 CWB 的 conversation 配额一致
    assert budget.conversation_tokens == b._context_window_budget.get_slot_tokens("conversation")
    assert t_footer == t_builder == mem._compact_threshold


# R1 [P0] + inv-1 golden：1M 档三者均 == 563_000（手算 568_000 − buffer(5_000)）
def test_xth01_1m_golden_563000(monkeypatch):
    monkeypatch.setattr(cr, "_load_table", lambda: _four_tier_table())
    budget = cr.resolve_budget("m1m")
    b = AgentBuilder().context_budget(**budget.to_builder_kwargs())
    mem = b._build_memory_factory()()

    t_footer = derive_compact_threshold(budget.conversation_tokens)
    t_builder = derive_compact_threshold(b._context_window_budget.get_slot_tokens("conversation"))

    assert t_footer == t_builder == mem._compact_threshold == 563_000
