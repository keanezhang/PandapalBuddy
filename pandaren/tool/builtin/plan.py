"""pandaren/tool/builtin/plan.py — Plan Mode 工具工厂。"""

from __future__ import annotations

import logging

from ..definition.tool import Tool

logger = logging.getLogger("pandaren.tool.builtin.plan")


class PlanToolFactory:
    """Plan Mode 工具工厂。

    委托给 pandaren.plan.tools.build_plan_mode_tools()。
    """

    def __init__(
        self,
        *,
        plan_dir: str | None = None,
    ) -> None:
        self._plan_dir = plan_dir

    def create_tools(self) -> list[Tool]:
        from ...plan.tools import build_plan_mode_tools
        return build_plan_mode_tools(
            plan_dir=self._plan_dir,
        )
