"""跨层一致性：builder / footer / Memory 的阈值必须来自**同一个预算对象**。

旧口径是"两边各自调同一个派生函数"（约定）；新口径是"同一个不可变实例"（结构），
漂移在结构上不可能发生（见 COMPACT_BUDGET_LAYERING_SPEC §2.7）。
"""

from __future__ import annotations

import pytest

from pandapal.config.llm.context_budget import (
    SYSTEM_PROMPT_CAP,
    TOOL_SCHEMA_CAP,
    compute_cw,
)
from pandaren.behavior.context_window_budget import ContextWindowBudget
from pandaren.builder import AgentBuilder

M_BY_MODEL = {"m128k": 128_000, "m200k": 200_000, "m400k": 400_000, "m1m": 1_000_000}


def _budget(m: int) -> ContextWindowBudget:
    return ContextWindowBudget(
        context_window=compute_cw(m),
        sys_cap=SYSTEM_PROMPT_CAP,
        tool_cap=TOOL_SCHEMA_CAP,
        system_prompt_tokens=19_203,
        output_tokens_reserve=8_000,
        model_max_context=m,
    )


@pytest.mark.parametrize("model_id", sorted(M_BY_MODEL))
def test_builder_threshold_equals_memory_threshold(model_id: str) -> None:
    budget = _budget(M_BY_MODEL[model_id])
    b = AgentBuilder().context_budget(budget)
    mem = b._build_memory_factory()()

    # 同一个实例：footer 与 Memory 的阈值不可能漂移
    assert b._context_window_budget is budget
    assert budget.compact_threshold == mem._compact_threshold


def test_1m_golden_772297() -> None:
    """1M 口径：CW=800,000 → conv=772,797 → T=772,297。"""
    budget = _budget(1_000_000)
    b = AgentBuilder().context_budget(budget)
    mem = b._build_memory_factory()()

    assert budget.context_window == 800_000
    assert budget.compact_threshold == 772_297
    assert mem._compact_threshold == 772_297
