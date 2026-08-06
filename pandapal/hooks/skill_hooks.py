"""pandapal/hooks/skill_hooks.py — Skill 生命周期 hook 的 pandapal 层实现。

通过覆写 pandaren AgentHooks Protocol 中新增的 on_skill_activated / on_skill_cleared，
将 Skill 激活/清除事件通过 MessageBroadcast 推送到前端。

分层合规：
- pandaren 层定义抽象（AgentHooks Protocol）
- pandapal 层注入实现（本文件），构造 NormalizedEvent → MessageBroadcast

延迟绑定：
- SkillAwareHooks 构造时不依赖 MessageBroadcast（此时 broadcast 子系统尚未启动）
- 在 PandaPalApp.start() → container.start_all() 之后，由 app._register_skill_hooks()
  调用 bind_broadcast() 完成绑定
- bind 之前 hook 触发时静默跳过（无 broadcast 可推送）
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pandaren.hook.hooks import DefaultAgentHooks

if TYPE_CHECKING:
    from pandapal.broadcast.broadcaster import MessageBroadcast

logger = logging.getLogger("pandapal.hooks.skill")


class SkillAwareHooks(DefaultAgentHooks):
    """覆写 AgentHooks 中 Skill 生命周期的 2 个 hook，完成 IPC 推送。

    使用方式：
        hooks = SkillAwareHooks()
        agent_builder.hooks(hooks)
        # ... PandaPalApp 启动后 ...
        hooks.bind_broadcast(app.broadcast)
    """

    def __init__(self) -> None:
        super().__init__()
        self._broadcast: MessageBroadcast | None = None

    def bind_broadcast(self, broadcast: MessageBroadcast) -> None:
        """延迟绑定 MessageBroadcast（在容器启动后调用）。"""
        self._broadcast = broadcast

    # ═══ H. Skill 生命周期 ═══

    def on_skill_activated(
        self, skill_name: str, skill_type: str,
        tools: list[str], run_id: str, step_n: int, *, session_id: str = "",
    ) -> None:
        """search_skills 成功后推送 SKILL_ACTIVATED 到前端。

        session_id：AgentHooks 新契约的 run 级关键字参（由 _safe_hook / Composite
        注入）。本 hook 暂不使用，但**必须声明**，否则 Composite 转发时会 TypeError
        并被静默吞掉，导致 SKILL_ACTIVATED 丢推。
        """
        if self._broadcast is None:
            return

        from pandapal.events.normalized import NormalizedEvent

        event = NormalizedEvent.skill_activated(
            skill_name=skill_name,
            skill_type=skill_type,
            tools=tools,
            run_id=run_id,
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._broadcast.send(event))
        except RuntimeError:
            logger.warning("No running event loop, SKILL_ACTIVATED not sent")

    def on_skill_cleared(
        self, skill_name: str, run_id: str, *, session_id: str = "",
    ) -> None:
        """Turn 结束时推送 SKILL_CLEARED 到前端。

        session_id：同 on_skill_activated，必须声明以兼容 Composite 转发。
        """
        if self._broadcast is None:
            return

        from pandapal.events.normalized import NormalizedEvent

        event = NormalizedEvent.skill_cleared(
            skill_name=skill_name,
            run_id=run_id,
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._broadcast.send(event))
        except RuntimeError:
            logger.warning("No running event loop, SKILL_CLEARED not sent")
