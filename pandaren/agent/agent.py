"""pandaren/agent/agent.py — Agent 顶层运行时类

主 Agent 和子 Agent 在运行时类型完全相同，区别仅在构建方式：
  - 主 Agent：AgentBuilder 代码直构
  - 子 Agent：SubAgentBlueprint → AgentBuilder → Agent

持有 AgentLoop，暴露 run(task) → AgentResult。

生命周期：
  - 推荐通过 `async with agent:` 使用，离开上下文时自动关闭 LLM 连接池。
  - 也可手动调用 `await agent.aclose()` 完成清理（幂等，可多次调用）。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import AsyncGenerator

from ..identity.models import Identity
from ..engine.loop import AgentLoop
from ..engine.models import AgentResult, RunState
from ..engine.stream import StreamEvent
from ..llm.types import ModelSettings

logger = logging.getLogger("pandaren.agent")


class AgentStatus(Enum):
    """Agent 健康状态（主 Agent 和子 Agent 通用）。"""
    HEALTHY = "healthy"         # 可用
    UNHEALTHY = "unhealthy"     # 不可用（健康检查失败）
    DRAINING = "draining"       # 下线中（不再接受新任务，等待当前任务完成）


class Agent:
    """Agent 顶层类。

    Agent.run(task) 转发到 AgentLoop.run(task)。

    用法示例::

        agent = AgentBuilder().identity(...).llm(client).build()
        result = await agent.run("帮我写一首诗")

    ⚠️ 资源所有权：``client`` 由调用方经 ``.llm(client=...)`` 注入，其生命周期
    也归调用方——``agent.aclose()`` **不会**替你关闭它（否则共享同一 client 的
    其他 Agent 会被误伤）。需要收尾时，请关闭你自己创建的 client，或经
    ``AgentBlueprint.aclose()`` 关闭共享 client。
    """

    def __init__(self, *, identity: Identity, loop: AgentLoop) -> None:
        self._identity = identity
        self._loop = loop
        self._closed = False

    @property
    def identity(self) -> Identity:
        return self._identity

    @property
    def agent_id(self) -> str:
        return self._identity.agent_id

    @property
    def agent_name(self) -> str:
        return self._identity.agent_name

    @property
    def provider(self) -> str:
        """激活的 LLM 平台/厂商名（dashscope/volcengine/openai/deepseek），只读。

        转发底层 LLM 客户端的 provider（取自端点能力声明，纯透传模式返回 ""）。
        供应用层在 run 启动前按 provider 做预算前置拦截（BudgetLedger.is_exhausted）。
        底层不可达 → ""（Fail-Safe：不阻断）。
        """
        client = getattr(self._loop, "_llm_client", None)
        return getattr(client, "provider", "") or "" if client is not None else ""

    @property
    def model_name(self) -> str:
        """本 Agent 底层 LLM 客户端的具体模型名（如 qwen-plus / deepseek-v4-pro），只读。

        转发底层 LLM 客户端的 model_name（构造后不可变，见 llm/protocol.py）。供应用层在
        run 启动时把「未显式选模型」的 run 解析成**具体**模型身份落库——这不是「默认兜底」，
        而是该 run 实际所用模型的真实名字。使 model_id 成为端到端必有的 ID（无 default、
        缺失即 bug），杜绝暂停/恢复时静默回落 provider。底层不可达 → ""（Fail-Safe）。
        """
        client = getattr(self._loop, "_llm_client", None)
        return getattr(client, "model_name", "") or "" if client is not None else ""

    async def run(
        self,
        task: str,
        *,
        session_id: str,
        resume_state: RunState | None = None,
        hitl_decision: str | None = None,
        interaction_response: str | None = None,
        metadata: dict | None = None,
        skill_name: str | None = None,
    ) -> AgentResult:
        """执行任务。永远返回 AgentResult，不抛异常。

        Args:
            task:                   用户任务/消息
            session_id:             会话 ID（必传），用于多轮状态管理和隔离
            resume_state:           HITL 暂停后恢复时传入（None = 新对话）
            hitl_decision:          HITL 审批结果，"approved" | "rejected" | None
            interaction_response:   交互型工具的用户回复文本（ask_user 恢复时传入）
            metadata:               透传给 ToolContext.metadata 的任意键值对（可选）
            skill_name:             用户手动指定的 Skill 名称（可选）。由接入层解析
                                    /skill_name 指令后显式传入，SDK 内部做精确查找与预加载。
                                    None 表示不指定，走普通流程。
        """
        return await self._loop.run(
            task,
            resume_state=resume_state,
            session_id=session_id,
            hitl_decision=hitl_decision,
            interaction_response=interaction_response,
            metadata=metadata,
            skill_name=skill_name,
        )

    async def run_stream(
        self,
        task: str,
        *,
        session_id: str,
        resume_state: RunState | None = None,
        hitl_decision: str | None = None,
        interaction_response: str | None = None,
        metadata: dict | None = None,
        skill_name: str | None = None,
        plan_action: str | None = None,
        edited_plan_content: str | None = None,
        settings: "ModelSettings | None" = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """流式执行任务。async generator，逐个 yield StreamEvent。

        与 run() 完全独立——零代码共享。

        Args:
            task:                   用户任务/消息
            session_id:             会话 ID（必传），用于多轮状态管理和隔离
            resume_state:           HITL 暂停后恢复时传入（None = 新对话）
            hitl_decision:          HITL 审批结果，"approved" | "rejected" | None
            interaction_response:   交互型工具的用户回复文本（ask_user 恢复时传入）
            metadata:               透传给 ToolContext.metadata 的任意键值对（可选）
            skill_name:             用户手动指定的 Skill 名称（可选）
            plan_action:            用户对计划的决策 "approve" | "refine" | "abandon"
            edited_plan_content:    批准时用户编辑后的计划文本（可选）
        """
        async for event in self._loop.run_stream(
            task,
            resume_state=resume_state,
            session_id=session_id,
            hitl_decision=hitl_decision,
            interaction_response=interaction_response,
            metadata=metadata,
            skill_name=skill_name,
            plan_action=plan_action,
            edited_plan_content=edited_plan_content,
            settings=settings,
        ):
            yield event

    # ── 生命周期 ──

    async def aclose(self) -> None:
        """关闭 Agent 实例，释放本实例**独有**的资源。

        幂等：多次调用无副作用。

        ⚠️ 所有权边界：``llm_client`` 是外部经 ``.llm(client=...)`` **注入**的、
        且在 blueprint.materialize() 下**跨 session 共享**的资源（谁创建谁关闭）。
        Agent 只是借用方，绝不在此关闭它——否则驱逐/丢弃单个实例会连带关掉
        整个进程共享的 HTTP 连接池，导致其余所有 session「client has been closed」。
        共享 client 的关闭权归其创建者/容器（见 ``AgentBlueprint.aclose()``）。
        """
        if self._closed:
            return
        self._closed = True
        # 当前 Agent 实例无独占的异步资源（Memory / Hooks 交由 GC）；
        # 保留此方法作为未来 per-instance 清理的钩子。

    def cancel(self) -> None:
        """取消当前正在执行的 Agent 循环（协作式取消）。

        设置 AgentLoop._cancelled 标志，Agent 在当前 step 循环头部
        检测后自动退出，yield AGENT_CANCELLED + RUN_END 事件。
        下一次 run_stream() / run() 调用前标志会被自动重置。
        """
        self._loop.cancel()

    def rebind_system_prompt(self, prompt: str) -> None:
        """运行时替换 system prompt（转发到底层 Memory.set_system_prompt）。

        供应用层在同一 session Agent（保留对话历史）上切换人格 / 领域。
        下一次消息构建即生效；调用方负责 delta 判断以保护 prompt cache。
        """
        self._loop._memory.set_system_prompt(prompt)

    async def __aenter__(self) -> "Agent":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"Agent(id='{self.agent_id}', name='{self.agent_name}')"
