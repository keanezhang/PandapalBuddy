"""progress_tools — 技能/长任务进度上报工具（对话内进度通道）。

提供 report_progress 工具，让 LLM 在执行**耗时较长、对外静默**的技能或流程时，
主动上报当前进度。进度以 SKILL_PROGRESS 事件广播，前端渲染成对话时间线里的
一个"技能进度块"（运行中活着、终态收起）。

设计定位（与相邻通道的边界）：
- 与 AgentTask（通道②）不同：AgentTask 是用户级**计划**（跨轮次持久、有依赖/验证）；
  本工具是单轮内某个技能的**瞬时进度心跳**，跑完即收起，不进 AgentTask 面板。
- 与 push_app_data（通道③）不同：那是给快应用面板推结构化数据；本工具只推进度文案。

设计约束：
- BL2 (Stateless): session_id 从 ToolContext 传入，不存全局状态。
- BL4 (DI): broadcaster 通过 ProgressTools 构造函数注入。
- 进度上报是"尽力而为"：broadcaster 为 None 或推送失败都静默降级，绝不阻断主流程。
"""

from __future__ import annotations

import asyncio
import logging

from pandaren.tool.decorator import tool
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool import Tool
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.types import SensitivityLevel, ToolTier

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.broadcast.channel_ids import LOCAL_AGENT_TASK_CHANNEL_ID
from pandapal.events.normalized import EventType, NormalizedEvent

logger = logging.getLogger(__name__)

# 合法的进度状态（与前端 / AgentTask 状态词表对齐）
_VALID_STATUS = {"running", "completed", "failed"}


def _make_report_progress(broadcaster: MessageBroadcast | None) -> Tool:
    @tool.function(
        name="report_progress",
        description=(
            "上报当前技能/长任务的执行进度，实时显示在对话里，让用户在等待时看到过程。\n"
            "\n"
            "【何时调用】执行**耗时较长、中途对外静默**的技能或多阶段流程时，"
            "在每个阶段开始/技能完成/技能失败时各调一次。典型：生成 PPT、跑自动化测试、批量处理。\n"
            "\n"
            "【何时不要调用】\n"
            "- 单步、秒级就能返回的操作 → 不必上报，避免刷屏\n"
            "- 用户级多步骤计划的推进 → 用 AgentTask 工具（create/update_agent_task）\n"
            "\n"
            "【参数】\n"
            "- activity: 正在进行的技能/活动名，同一次活动的多次上报请用**同一个** activity"
            "（前端据此归并成一个进度块），如 '生成PPT'、'自动化测试'。\n"
            "- phase: 当前阶段的一句话描述（≤20字），如 '正在渲染第3页'、'校验产物'。\n"
            "- status: 'running'=进行中（每进入新阶段发一次）；'completed'=整个活动成功结束；"
            "'failed'=整个活动失败。发新 phase(running) 会自动把上一阶段标记完成；"
            "整个活动只在最后发一次 completed 或 failed。"
        ),
        when_to_use=(
            "执行耗时长、对外静默的技能/流程时，用它上报阶段性进度，"
            "让用户在等待时看到过程。单步秒级操作不要用；用户级计划推进用 AgentTask。"
        ),
        tier=ToolTier.ALWAYS,
        progress_label="上报进度「{phase}」",
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=False,
            is_idempotent=True,
        ),
    )
    async def report_progress(
        ctx: ToolContext,
        activity: str,
        phase: str,
        status: str = "running",
    ) -> str:
        """上报技能/长任务进度。

        Args:
            ctx: 工具执行上下文。
            activity: 活动名（归并键），同一活动多次上报用同一值。
            phase: 当前阶段一句话描述。
            status: running / completed / failed。

        Returns:
            简短确认（供 LLM 继续推进；进度本身走 SKILL_PROGRESS 事件到前端）。
        """
        norm_status = status if status in _VALID_STATUS else "running"

        if broadcaster is not None:
            payload = {
                "activity": activity,
                "phase": phase,
                "status": norm_status,
                "session_id": ctx.session_id,
            }
            try:
                asyncio.create_task(
                    broadcaster.send(
                        NormalizedEvent(
                            event_type=EventType.SKILL_PROGRESS,
                            payload=payload,
                            origin_channel_id=LOCAL_AGENT_TASK_CHANNEL_ID,
                        )
                    )
                )
            except Exception as e:  # 尽力而为：绝不阻断主流程
                logger.warning("report_progress: 推送失败 %s (activity=%s)", e, activity)
        else:
            logger.debug("report_progress: broadcaster is None, 进度丢弃 (activity=%s)", activity)

        return f"进度已上报：{activity} · {phase} [{norm_status}]"

    return report_progress


class ProgressTools:
    """技能进度上报工具组 Provider。

    构造时显式注入 broadcaster，get_tools() 返回绑定依赖的 Tool 列表。
    """

    def __init__(self, broadcaster: MessageBroadcast | None = None) -> None:
        self._broadcaster = broadcaster

    def get_tools(self) -> list[Tool]:
        return [
            _make_report_progress(self._broadcaster),
        ]
