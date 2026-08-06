"""pandaren/tool/builtin/agent.py — call_agent 工具工厂。

executor 通过 ctx.metadata["agent_registry"] 获取 SubAgentRegistry。
"""

from __future__ import annotations

import logging
from typing import Any

from ..definition.tool import Tool
from ..definition.context import ToolContext
from ..definition.tool_policy import ToolPolicy
from ..types import ToolTier, SensitivityLevel

logger = logging.getLogger("pandaren.tool.builtin.agent")


class AgentToolFactory:
    """call_agent 工具工厂（无状态）。"""

    def create_tools(self) -> list[Tool]:
        async def _executor(
            ctx: ToolContext, agent_name: str = "", task: str = "",
        ) -> Any:
            """委派任务给指定 Agent。

            Args:
                agent_name: 目标 Agent 的名称。
                task: 要委派的任务描述。
            """
            registry = ctx.metadata["agent_registry"]
            return await registry.call_agent(agent_name, task, ctx)

        return [Tool(
            name="call_agent",
            description=(
                "委派任务给指定 Agent。"
                "从 system prompt 的 <available_agents> 中选择合适的 Agent，"
                "将其 <name> 作为 agent_name 传入。"
            ),
            executor=_executor,
            input_schema={
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": "目标 Agent 的名称",
                    },
                    "task": {
                        "type": "string",
                        "description": "要委派的任务描述",
                    },
                },
                "required": ["agent_name", "task"],
            },
            tier=ToolTier.ALWAYS,
            when_to_use="当你需要将任务委派给另一个 Agent 执行时使用。",
            policy=ToolPolicy(
                sensitivity=SensitivityLevel.HIGH,
                is_reversible=False,
                audit_required=True,
                is_idempotent=False,
            ),
        )]
