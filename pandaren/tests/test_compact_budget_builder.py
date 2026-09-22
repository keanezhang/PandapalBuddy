"""AgentBuilder 装配单测（新口径：预算对象 + CompactionProfile 派生）。

风险映射：R1 [P0] 阈值跨层漂移 + inv-1 一致性 + R12 [P1] 子 Agent 继承。
Oracle：golden value（按 SPEC §5 公式独立手算，非跑实现抄来）。
component(fake)：Memory 为内存对象，零 I/O。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pandapal.config.llm.context_budget import SYSTEM_PROMPT_CAP, TOOL_SCHEMA_CAP
from pandaren.behavior.context_window_budget import ContextWindowBudget
from pandaren.builder import AgentBuilder
from pandaren.identity.models import TrustLevel
from pandaren.memory.compaction import WindowedKeepPolicy
from pandaren.memory.constants import BALANCED
from pandaren.sub_agent.models import SubAgentBlueprint

SYS_MEASURED = 19_203
RESERVE = 32_000


class FakeTokenEstimator:
    """可控 token 估算器：按消息条数返回固定 token（避免真实 tokenizer 不确定性）。"""

    def __init__(self, tokens_per_message: int = 100) -> None:
        self.tokens_per_message = tokens_per_message

    def estimate(self, messages: list) -> int:
        return self.tokens_per_message * max(1, len(messages))


class _FakeCompactionPolicy:
    """自定义 CompactionPolicy 哨兵（BLD-2 验证不被派生覆盖）。"""

    def split(self, messages: list, max_tokens: int):
        raise NotImplementedError("fake policy, never invoked at build time")


class _FakeLLM:
    """空 LLM 客户端哨兵（装配测试不调用真实 LLM）。"""


def _budget(cw: int, m: int) -> ContextWindowBudget:
    return ContextWindowBudget(
        context_window=cw,
        sys_cap=SYSTEM_PROMPT_CAP,
        tool_cap=TOOL_SCHEMA_CAP,
        system_prompt_tokens=SYS_MEASURED,
        output_tokens_reserve=RESERVE,
        model_max_context=m,
    )


# ─────────────────────────────────────────────────────────────
# BLD-1: 由预算对象 + profile 派生 threshold / keep_window / 单条结果上限
# ─────────────────────────────────────────────────────────────


def test_context_budget_derives_threshold_keep_window_and_tool_cap() -> None:
    budget = _budget(600_000, 750_000)
    b = AgentBuilder().context_budget(budget)
    mem = b._build_memory_factory()()

    # inv-1：conv = 600,000 − 19,203 − 8,000 = 572,797 → T = 572,297
    T = budget.compact_threshold
    assert T == 572_297
    assert mem._compact_threshold == T

    policy = mem._short_term._compaction_policy
    assert isinstance(policy, WindowedKeepPolicy)
    assert policy._min_tokens == BALANCED.min_keep_tokens(T) == 68_675
    assert policy._max_tokens == BALANCED.max_keep_tokens(T) == 257_533
    assert mem._micro_compactor._single_result_max_tokens == 30_000


# ─────────────────────────────────────────────────────────────
# BLD-2: 用户显式设置优先于派生
# ─────────────────────────────────────────────────────────────


def test_explicit_compaction_policy_not_overridden() -> None:
    custom = _FakeCompactionPolicy()
    budget = _budget(600_000, 750_000)
    b = AgentBuilder().memory(compaction_policy=custom).context_budget(budget)
    mem = b._build_memory_factory()()

    assert mem._short_term._compaction_policy is custom
    assert mem._compact_threshold == budget.compact_threshold


def test_explicit_microcompact_cap_not_overridden() -> None:
    budget = _budget(600_000, 750_000)
    b = (
        AgentBuilder()
        .memory(microcompact_single_result_max_tokens=12_345)
        .context_budget(budget)
    )
    mem = b._build_memory_factory()()

    assert mem._micro_compactor._single_result_max_tokens == 12_345


# ─────────────────────────────────────────────────────────────
# BLD-3: token_estimator 贯穿注入（同一把尺子）
# ─────────────────────────────────────────────────────────────


def test_token_estimator_injected_through_memory_and_tool_budget() -> None:
    est = FakeTokenEstimator()
    budget = _budget(102_400, 128_000)
    b = AgentBuilder().memory(token_estimator=est).context_budget(budget)
    mem = b._build_memory_factory()()

    # inv-8：同一把尺子贯穿 Memory / WindowedKeepPolicy / MicroCompactor
    assert mem._token_estimator is est
    assert mem._short_term._compaction_policy._token_estimator is est
    assert mem._micro_compactor._token_estimator is est

    tool_registry, _ = b._build_tool_layer(audit_log=MagicMock(), hooks=MagicMock())
    assert tool_registry._budget._token_estimator is est
    # tool 熔断线取预算对象的 tool_cap_tokens（记账与运行时裁剪同一把尺子）
    assert tool_registry._budget.tool_schema_max_tokens == budget.tool_cap_tokens


# ─────────────────────────────────────────────────────────────
# BLD-4: 子 Agent 继承同一预算实例 + token_estimator
# ─────────────────────────────────────────────────────────────


def test_sub_agent_inherits_same_budget_instance_and_token_estimator() -> None:
    est = FakeTokenEstimator()
    budget = _budget(600_000, 750_000)
    parent = AgentBuilder().context_budget(budget).memory(token_estimator=est)

    bp = SubAgentBlueprint(
        agent_id="sub1",
        agent_name="Sub",
        when_to_use="测试子 Agent",
        system_prompt="You are a sub agent.",
        trust_level=TrustLevel.SUB_AGENT,
        tools=(),
        skills=(),
    )

    sub = parent._build_sub_agent_from_blueprint(bp, _FakeLLM(), [], [], MagicMock())

    # R12：继承的是**同一个不可变实例**，所以阈值在结构上不可能漂移
    assert sub.context_window_budget is budget

    # inv-8：子 Agent 继承同一把尺子
    mem = sub.memory_factory()
    assert mem._token_estimator is est
    assert mem._compact_threshold == budget.compact_threshold
