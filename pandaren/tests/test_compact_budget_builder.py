"""compact-budget.design.md §6.3 — AgentBuilder 装配单测。

覆盖用例：BLD-1 / BLD-2 / BLD-3 / BLD-4
风险映射：R1 [P0]（阈值跨层漂移）+ inv-1 / inv-4 / inv-8 / R12 [P1]（子 Agent 继承）
Oracle：golden value（独立手算）。
component(fake)：Memory 为内存对象，零 I/O；FakeTokenEstimator / FakeLLM 可注入。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pandaren.builder import AgentBuilder
from pandaren.identity.models import TrustLevel
from pandaren.memory.compaction import WindowedKeepPolicy
from pandaren.sub_agent.models import SubAgentBlueprint


class FakeTokenEstimator:
    """可控 token 估算器：按消息条数返回固定 token，避免真实 tokenizer 不确定性。"""

    def __init__(self, tokens_per_message: int = 100) -> None:
        self.tokens_per_message = tokens_per_message

    def estimate(self, messages: list) -> int:
        return self.tokens_per_message * max(1, len(messages))


class _FakeCompactionPolicy:
    """自定义 CompactionPolicy 哨兵（BLD-2 用于验证不被派生覆盖）。"""

    def split(self, messages: list, max_tokens: int):
        raise NotImplementedError("fake policy, never invoked at build time")


class _FakeLLM:
    """空 LLM 客户端哨兵（BLD-4 装配测试不调用真实 LLM）。"""


# ─────────────────────────────────────────────────────────────
# BLD-1: 透传 abs 并派生 threshold / keep_window / tool_cap
# ─────────────────────────────────────────────────────────────


def test_context_budget_derives_threshold_keep_window_and_tool_cap() -> None:
    b = AgentBuilder().context_budget(
        context_window=600_000,
        system_prompt_tokens_abs=24_000,
        tool_schema_tokens_abs=8_000,
        recall_ratio=0.0,
    )
    mem = b._build_memory_factory()()

    # inv-1：conversation 配额 = 568_000，threshold = derive_compact_threshold(568_000)
    assert b._context_window_budget.get_slot_tokens("conversation") == 568_000
    assert mem._compact_threshold == 563_000

    policy = mem._short_term._compaction_policy
    assert isinstance(policy, WindowedKeepPolicy)
    assert policy._min_tokens == 67_560
    assert policy._max_tokens == 253_350

    assert mem._micro_compactor._single_result_max_tokens == 30_000


# ─────────────────────────────────────────────────────────────
# BLD-2: 用户显式设置优先于派生
# ─────────────────────────────────────────────────────────────


def test_explicit_compaction_policy_not_overridden() -> None:
    custom = _FakeCompactionPolicy()
    b = (
        AgentBuilder()
        .memory(compaction_policy=custom)
        .context_budget(context_window=600_000)
    )
    mem = b._build_memory_factory()()

    assert mem._short_term._compaction_policy is custom
    # 阈值仍派生：conv = floor(600_000×0.50) = 300_000 → 300_000 − 5_000
    assert mem._compact_threshold == 295_000


def test_explicit_microcompact_cap_not_overridden() -> None:
    b = (
        AgentBuilder()
        .memory(microcompact_single_result_max_tokens=12_345)
        .context_budget(context_window=600_000)
    )
    mem = b._build_memory_factory()()

    assert mem._micro_compactor._single_result_max_tokens == 12_345


# ─────────────────────────────────────────────────────────────
# BLD-3: token_estimator 贯穿注入（同一把尺子）
# ─────────────────────────────────────────────────────────────


def test_token_estimator_injected_through_memory_and_tool_budget() -> None:
    est = FakeTokenEstimator()
    b = (
        AgentBuilder()
        .memory(token_estimator=est)
        .context_budget(context_window=100_000, system_prompt_tokens_abs=24_000)
    )
    mem = b._build_memory_factory()()

    # inv-8：同一把尺子贯穿 Memory / WindowedKeepPolicy / MicroCompactor
    assert mem._token_estimator is est
    assert mem._short_term._compaction_policy._token_estimator is est
    assert mem._micro_compactor._token_estimator is est

    # ToolBudget 注入点：经 _build_tool_layer 验证
    tool_registry, _ = b._build_tool_layer(audit_log=MagicMock(), hooks=MagicMock())
    assert tool_registry._budget._token_estimator is est


# ─────────────────────────────────────────────────────────────
# BLD-4: 子 Agent 继承 abs 槽位 + token_estimator
# ─────────────────────────────────────────────────────────────


def test_sub_agent_inherits_abs_slots_and_token_estimator() -> None:
    est = FakeTokenEstimator()
    parent = (
        AgentBuilder()
        .context_budget(
            context_window=600_000,
            system_prompt_tokens_abs=24_000,
            tool_schema_tokens_abs=8_000,
            recall_ratio=0.0,
        )
        .memory(token_estimator=est)
    )

    bp = SubAgentBlueprint(
        agent_id="sub1",
        agent_name="Sub",
        when_to_use="测试子 Agent",
        system_prompt="You are a sub agent.",
        trust_level=TrustLevel.SUB_AGENT,
        tools=(),
        skills=(),
    )

    sub = parent._build_sub_agent_from_blueprint(
        bp, _FakeLLM(), [], [], MagicMock()
    )

    # R12：子 Agent 继承父级绝对槽位（含 abs 值，避免阈值漂移）
    budget = sub.context_window_budget
    assert budget.system_prompt_tokens_abs == 24_000
    assert budget.tool_schema_tokens_abs == 8_000
    assert budget.context_window == 600_000

    # inv-8：子 Agent 继承同一把尺子
    mem = sub.memory_factory()
    assert mem._token_estimator is est
    # inv-1：子 Agent 阈值与父级一致，无漂移（568_000 − 5_000）
    assert mem._compact_threshold == 563_000
