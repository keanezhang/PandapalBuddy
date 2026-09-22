"""步数上限统一常量 + 父/子 Agent 生效值回归。

需求：父/主 Agent 500 步、委派型子 Agent 300 步，二者均由
`pandaren/behavior/execution_limits.py` 的常量单一声明（消除散落字面量）。

覆盖点：
  - 常量值：DEFAULT_MAX_STEPS=500 / DEFAULT_SUB_AGENT_MAX_STEPS=300
  - ExecutionLimits() 默认跟随 DEFAULT_MAX_STEPS
  - AgentBuilder.behavior() 签名默认跟随 DEFAULT_MAX_STEPS
  - 父 Agent（未显式设置）经 behavior() 生效 DEFAULT_MAX_STEPS
  - 子 Agent 经 _build_sub_agent_from_blueprint 生效 DEFAULT_SUB_AGENT_MAX_STEPS
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from pandaren.behavior.execution_limits import (
    DEFAULT_MAX_STEPS,
    DEFAULT_SUB_AGENT_MAX_STEPS,
    ExecutionLimits,
)
from pandaren.builder import AgentBuilder
from pandaren.identity.models import TrustLevel
from pandaren.sub_agent.models import SubAgentBlueprint


class _FakeLLM:
    """空 LLM 客户端哨兵（装配测试不调用真实 LLM）。"""


def test_step_constants_values() -> None:
    assert DEFAULT_MAX_STEPS == 500
    assert DEFAULT_SUB_AGENT_MAX_STEPS == 300


def test_execution_limits_default_follows_constant() -> None:
    assert ExecutionLimits().max_steps == DEFAULT_MAX_STEPS


def test_behavior_default_follows_constant() -> None:
    default = inspect.signature(AgentBuilder.behavior).parameters["max_steps"].default
    assert default == DEFAULT_MAX_STEPS


def test_parent_agent_uses_max_steps_constant() -> None:
    # 父/主 Agent 未显式设置时，经 behavior() 生效 DEFAULT_MAX_STEPS（=500）
    builder = AgentBuilder().behavior()
    assert builder._execution_limits.max_steps == DEFAULT_MAX_STEPS


def test_sub_agent_uses_dedicated_max_steps_constant() -> None:
    parent = AgentBuilder()
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

    assert sub.execution_limits.max_steps == DEFAULT_SUB_AGENT_MAX_STEPS
    assert sub.execution_limits.max_steps == 300
    # 子 Agent 步数与父级不同（各自独立声明，非继承）
    assert sub.execution_limits.max_steps != DEFAULT_MAX_STEPS
