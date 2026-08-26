"""pandaren/tool/tests/conftest.py — 工具模块测试共享 fixtures。

背景：pandaren/tool 旧测试文件曾被整目录删除（仅剩空 __init__.py），
导致 04-tool.md §9 遗留问题（safe_name 误拆、store 索引不一致、
命名空间清理失效等）无回归防护。本套测试为修复后 API 建立回归护栏。
"""

from __future__ import annotations

from typing import Any

import pytest

from pandaren.identity.models import TrustLevel
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool import Tool
from pandaren.tool.definition.tool_lifecycle import ToolLifecycle
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.types import SensitivityLevel, ToolTier


def make_tool(
    name: str,
    namespace: str | None = None,
    *,
    tier: ToolTier = ToolTier.DEFERRED,
    executor: Any = None,
    input_schema: dict | None = None,
    description: str = "测试工具",
    when_to_use: str = "测试场景使用",
    policy: ToolPolicy | None = None,
    lifecycle: ToolLifecycle | None = None,
    tags: frozenset[str] = frozenset(),
    agent_whitelist: frozenset[str] | None = None,
) -> Tool:
    """构造最小合法 Tool（必填：name/description/executor/policy/input_schema/when_to_use）。

    input_schema 默认给空 object schema（properties 为空），避免 validator 报错。
    """
    if executor is None:
        async def _default_executor(ctx: ToolContext, **kwargs: Any) -> str:
            return f"ok:{kwargs}"

        executor = _default_executor
    if policy is None:
        policy_kwargs = {}
        if agent_whitelist is not None:
            policy_kwargs["agent_whitelist"] = agent_whitelist
        policy = ToolPolicy(sensitivity=SensitivityLevel.LOW, **policy_kwargs)
    return Tool(
        namespace=namespace,
        name=name,
        description=description,
        when_to_use=when_to_use,
        policy=policy,
        input_schema=input_schema or {"type": "object", "properties": {}},
        tier=tier,
        executor=executor,
        lifecycle=lifecycle or ToolLifecycle(),
        tags=tags,
    )


def make_ctx(
    *,
    run_id: str = "run-1",
    step_n: int = 1,
    agent_id: str = "agent-1",
    session_id: str = "session-1",
    trust_level: TrustLevel = TrustLevel.SUB_AGENT,
    namespace: str | None = None,
) -> ToolContext:
    """构造最小合法 ToolContext（run_id/step_n/agent_id/session_id 必填）。"""
    return ToolContext(
        run_id=run_id,
        step_n=step_n,
        agent_id=agent_id,
        session_id=session_id,
        trust_level=trust_level,
        namespace=namespace,
    )


@pytest.fixture
def tool_factory():
    return make_tool


@pytest.fixture
def ctx_factory():
    return make_ctx
