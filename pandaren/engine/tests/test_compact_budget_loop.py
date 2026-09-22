"""Loop static_context / 摘要预算用例：覆盖 LOP-1 / LOP-2。

对应设计文档 compact-budget.design.md §6.6：
  - LOP-1 static_context 截断改用 estimator（中文超配额正确截断）
    (a) system_prompt 本身超配额 → static_context 完全丢弃（None + warning）
    (b) system 不超但 static_context 超 → 截断到 <= available
  - LOP-2 skill/agent 摘要预算补传 context_window
"""

from __future__ import annotations

import logging

from pandaren.engine.loop import AgentLoop
from pandaren.engine.message_builder import MessageBuilder


class _CharTokenEstimator:
    """1 字符 = 1 token。区别于 chars/4（同一把尺子的可控 Fake）。"""

    def estimate(self, messages) -> int:
        return sum(len(str(m.get("content", ""))) for m in messages)


class _FakeMemory:
    def __init__(self, system_prompt: str = ""):
        self._system_prompt = system_prompt
        self._estimator = _CharTokenEstimator()
        self.estimate_calls = 0

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def estimate_text(self, text: str) -> int:
        self.estimate_calls += 1
        return self._estimator.estimate([{"role": "user", "content": text}])

    def truncate_text_to_tokens(self, text: str, max_tokens: int) -> str:
        return text[:max_tokens]


class _FakeIdentity:
    agent_id = "agent-1"


class _FakeToolRegistry:
    def __init__(self, catalog: list[dict], version: int = 1):
        self._catalog = catalog
        self.version = version

    def get_deferred_tool_catalog(self, agent_id: str) -> list[dict]:
        return self._catalog


class _FakeCWB:
    def __init__(self, context_window: int, system_prompt_tokens: int):
        self.context_window = context_window
        self.system_prompt_tokens = system_prompt_tokens


class _FakeSkillRegistry:
    def __init__(self, version: int = 1):
        self.version = version
        self.received_context_windows: list[int | None] = []

    def build_skill_summaries(self, context_window: int | None = None) -> list:
        self.received_context_windows.append(context_window)
        return []


class _FakeAgentRegistry:
    def __init__(self, version: int = 1):
        self.version = version
        self.received_context_windows: list[int | None] = []

    def build_agent_summaries(
        self,
        context_window: int | None = None,
        exclude_agent_id: str | None = None,
    ) -> list:
        self.received_context_windows.append(context_window)
        return []


def _make_loop(
    memory: _FakeMemory,
    cwb: _FakeCWB,
    *,
    skill_registry: _FakeSkillRegistry | None = None,
    agent_registry: _FakeAgentRegistry | None = None,
    catalog: list[dict] | None = None,
) -> AgentLoop:
    loop = object.__new__(AgentLoop)
    loop._identity = _FakeIdentity()
    loop._tool_registry = _FakeToolRegistry(catalog or [])
    loop._skill_registry = skill_registry
    loop._agent_registry = agent_registry
    loop._context_window_budget = cwb
    loop._memory = memory
    return loop


def test_lop1_system_prompt_exceeds_quota_discards_static_context(caplog):
    # (a) system_prompt 本身超配额 → available_for_static <= 0 → 完全丢弃
    memory = _FakeMemory(system_prompt="你" * 24_001)  # 24_001 chars → 24_001 tokens
    cwb = _FakeCWB(context_window=100_000, system_prompt_tokens=24_000)
    loop = _make_loop(memory, cwb, catalog=[{"name": "search_tools", "when_to_use": "检索工具"}])

    with caplog.at_level(logging.WARNING):
        result = loop._build_static_context()

    assert result is None
    assert "static_context 被完全丢弃" in caplog.text


def test_lop1_static_context_truncated_with_estimator():
    # (b) system 不超但 static_context 超 → 按同一估算器截断到 <= available
    memory = _FakeMemory(system_prompt="系" * 100)  # 100 chars → 100 tokens
    cwb = _FakeCWB(context_window=100_000, system_prompt_tokens=24_000)
    catalog = [{"name": "search_tools", "when_to_use": "查" * 30_000}]
    loop = _make_loop(memory, cwb, catalog=catalog)

    available = 24_000 - 100  # 手算：system_prompt_tokens - system_base_tokens
    full_static = MessageBuilder.build_static_context_str(deferred_tool_summaries=catalog)

    result = loop._build_static_context()

    assert result is not None
    assert len(full_static) > available  # 确实超配额，触发截断
    assert len(result) == available  # 1 char == 1 token，截断到 available chars
    assert memory.estimate_text(result) == available  # 同一把尺子（非 chars/4）
    assert result == full_static[:available]


def test_lop2_summary_builders_receive_context_window():
    memory = _FakeMemory(system_prompt="")
    cwb = _FakeCWB(context_window=600_000, system_prompt_tokens=600_000)
    skills = _FakeSkillRegistry()
    agents = _FakeAgentRegistry()
    loop = _make_loop(
        memory,
        cwb,
        skill_registry=skills,
        agent_registry=agents,
        catalog=[{"name": "search_tools", "when_to_use": "检索工具"}],
    )

    result = loop._build_static_context()

    assert result is not None
    assert skills.received_context_windows == [600_000]
    assert agents.received_context_windows == [600_000]
