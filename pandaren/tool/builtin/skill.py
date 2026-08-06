"""pandaren/tool/builtin/skill.py — search_skills 工具工厂。

executor 通过 ctx.metadata["skill_registry"] 获取 SkillRegistry。
"""

from __future__ import annotations

import logging

from ..definition.tool import Tool
from ..definition.tool_result import ToolResult
from ..definition.context import ToolContext
from ..definition.tool_policy import ToolPolicy
from ..types import ToolTier, SensitivityLevel

logger = logging.getLogger("pandaren.tool.builtin.skill")


class SkillToolFactory:
    """search_skills 工具工厂（无状态）。"""

    def create_tools(self) -> list[Tool]:
        def _executor(ctx: ToolContext, skill_name: str = "") -> ToolResult:
            """search_skills 内置工具的 executor。

            Args:
                skill_name: 要加载的技能名称。
            """
            registry = ctx.metadata["skill_registry"]
            return registry.search_skills(skill_name, ctx)

        return [Tool(
            name="search_skills",
            description=(
                "按名称精准加载技能（知识包），加载后技能内容将注入上下文指导后续行为。"
                "从 system prompt 的 <available_skills> 中选择合适的技能，"
                "将其 <name> 精准传入即可加载。"
            ),
            executor=_executor,
            input_schema={
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "要加载的技能名称（取 <available_skills> 中技能的 <name> 值）",
                    },
                },
                "required": ["skill_name"],
            },
            tier=ToolTier.ALWAYS,
            when_to_use="当你需要加载特定领域的专项知识、操作流程或方法论时使用。",
            policy=ToolPolicy(
                sensitivity=SensitivityLevel.LOW,
                is_idempotent=True,
            ),
        )]
