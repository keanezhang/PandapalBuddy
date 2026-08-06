"""pandaren/engine/loop.py — AgentLoop 核心实现

执行内核由 RunCoreMixin._run_stream_core() 统一提供（8 Phase 单一执行体）。
run_stream() 和 run() 均为 RunCoreMixin 的包装层，共享同一内核逻辑。

HC3：permission_guard.check_permission() 和 hitl_controller.check_approval() 硬编码在主路径
HC4：audit_log.write_sync() 硬编码在关键节点，AuditWriteError 不可忽略
HC5：for step in range(max_steps) 有界循环，StepCounter 只增不减
HC6：sensitivity >= CRITICAL 时无视 auto_confirm_high
O3 ：run() 永远返回 AgentResult，不向外抛异常
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .types import TerminalReason
from ..cancellation import CancelToken
from .models import AgentResult, RunState, StepRecord
from .message_builder import MessageBuilder
from .output_parser import OutputParser
from ..hook import DefaultAgentHooks as DefaultLoopHooks
from .run_core import RunCoreMixin

from ..llm.protocol import LLMClient
from ..llm.types import ModelSettings
from ..behavior.permission_guard import PermissionGuard
from ..behavior.hitl_controller import HITLController
from ..behavior.execution_limits import ExecutionLimits
from ..behavior.error_policy import ErrorPolicy
from ..behavior.step_guard import StepGuard
from ..behavior.context_window_budget import ContextWindowBudget
from ..identity.models import Identity
from ..tool.registry import ToolRegistry
from ..behavior.harness.executor import HarnessExecutor
from ..observability.audit import AuditLog
from ..memory.memory import Memory

logger = logging.getLogger("pandaren.engine.loop")


class AgentLoop(RunCoreMixin):
    """Agent 核心 ReAct 循环。

    执行内核由 RunCoreMixin._run_stream_core() 统一提供：
      - run()        → drain 消费内核，永远返回 AgentResult（O3）
      - run_stream() → passthrough，逐个 yield StreamEvent
      - _safe_hook() → 定义在 RunCoreMixin，直接可用

    安全关键依赖（构造后冻结，运行时不可替换）：
      _identity / _llm_client / _tool_registry / _permission_guard /
      _hitl_controller / _audit_log / _limits / _memory / _message_builder / _output_parser
    """

    __slots__ = (
        "_identity", "_llm_client", "_tool_registry", "_harness_executor",
        "_permission_guard",
        "_hitl_controller", "_audit_log", "_limits", "_memory",
        "_error_policy", "_step_guard", "_hooks", "_message_builder",
        "_output_parser", "_skill_registry", "_agent_registry",
        # 协作式取消令牌（替代原 _cancelled bool）。每次 run 入口重建，非冻结。
        # _cancelled 保留为只读 property（读 token），对内读点零改动。
        "_cancel_token", "_consecutive_permission_denied_rounds", "_initialized",
        "_llm_settings", "_static_context_str", "_static_context_version",
        "_stream", "_context_window_budget",
        # ★ 多 session 并发 · 数据隔离：本次 run 的 session_id，写审计/tracer/logger 时透传。
        #   每次 _run_stream_core 入口设置，run 结束或异常 finally 里清空。
        "_current_session_id",
    )

    # 构造后冻结的属性（运行时不可替换，防止安全关键组件被篡改）
    # 注意：_static_context_str / _static_context_version 不在冻结集合内 —
    # 它们是 run 入口可重建的缓存（见 RunCoreMixin._run_stream_core），
    # 重新构建是合法且必要的（Skill/Tool 增删后下次对话生效）。
    _FROZEN_ATTRS = frozenset({
        "_identity", "_llm_client", "_tool_registry", "_harness_executor",
        "_permission_guard",
        "_hitl_controller", "_audit_log", "_limits", "_memory",
        "_message_builder", "_output_parser",
    })

    def __init__(
        self,
        *,
        identity: Identity,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        harness_executor: HarnessExecutor,
        permission_guard: PermissionGuard,
        hitl_controller: HITLController,
        execution_limits: ExecutionLimits,
        error_policy: ErrorPolicy,
        step_guard: StepGuard | None = None,  # 通用每步停机守卫（应用层实现）
        context_window_budget: ContextWindowBudget | None = None,
        audit_log: AuditLog,
        memory: Memory,
        message_builder: MessageBuilder | None = None,
        output_parser: OutputParser | None = None,
        hooks: Any | None = None,
        skill_registry: Any | None = None,
        agent_registry: Any | None = None,
        llm_settings: ModelSettings | None = None,
        stream: bool = True,
    ) -> None:
        self._identity = identity
        self._llm_client = llm_client
        self._llm_settings = llm_settings
        self._stream = stream
        self._tool_registry = tool_registry
        self._harness_executor = harness_executor
        self._permission_guard = permission_guard
        self._hitl_controller = hitl_controller
        self._audit_log = audit_log
        self._limits = execution_limits
        self._memory = memory
        self._error_policy = error_policy
        self._step_guard = step_guard
        self._context_window_budget = context_window_budget
        self._hooks = hooks or DefaultLoopHooks()
        self._message_builder = message_builder or MessageBuilder()
        self._output_parser = output_parser or OutputParser()
        self._skill_registry = skill_registry  # SkillRegistry | None
        self._agent_registry = agent_registry  # SubAgentRegistry | None
        # 协作式取消令牌。_run_stream_core 入口会重建，确保每次 run 干净起点。
        self._cancel_token = CancelToken()
        self._consecutive_permission_denied_rounds = 0
        # ★ 当前 run 的 session_id（多 session 并发下用于审计/tracer 路径分片）
        #   _run_stream_core 入口设置，finally 清空
        self._current_session_id = ""

        # ── Prefix Cache v1.0：静态前缀一次性序列化 ──
        # PC1 序列化唯一性 + PC2 Stable-First Ordering 的前置条件：
        # 把对 run 而言稳定的三块 XML 清单（<available_tools> /
        # <available_skills> / <available_agents>）在构造时序列化成单一字符串，
        # 整个 run 每轮 build() 直接复用，避免每轮重新拼装导致字节不稳。
        #
        # 每次 run() / run_stream() 入口处也会重建，确保 Skill 增删后下次对话生效。
        self._static_context_str = self._build_static_context()
        self._initialized = True

    def __setattr__(self, name: str, value: Any) -> None:
        if (
            name != "_initialized"
            and getattr(self, "_initialized", False)
            and name in AgentLoop._FROZEN_ATTRS
        ):
            raise AttributeError(
                f"AgentLoop.{name} is frozen after initialization. "
                f"Security-critical dependencies cannot be replaced at runtime."
            )
        object.__setattr__(self, name, value)

    def cancel(self) -> None:
        """外部取消信号（协作式）。触发取消令牌，检查点检测后退出。

        检查点（见 docs/design/取消语义-契约.md）：
          - Layer 0：step 循环头（run_core.py）
          - Layer 1：LLM 流式逐 chunk（run_core.py）
          - Layer 2/3：工具边界 / 子 Agent（后续阶段）
        """
        logger.info(
            "[cancel] AgentLoop.cancel() ◀ external signal · agent_id=%s session_id=%s",
            getattr(self._identity, "agent_id", "?"),
            getattr(self, "_current_session_id", "") or "-",
        )
        self._cancel_token.cancel()

    @property
    def _cancelled(self) -> bool:
        """只读：是否已取消（读取消令牌）。保留此名以最小化内部读点改动。"""
        return self._cancel_token.cancelled

    def _build_static_context(self) -> str | None:
        """从 Registry 拉取最新目录，构建 static_context_str 并应用 token 配额。

        __init__ 和 _run_stream_core 入口处均调用。通过三注册表的 version 做脏检查，
        版本号不变则直接返回缓存的 _static_context_str，确保 LLM Prefix Cache 命中率。
        """
        from ..constants import CHARS_PER_TOKEN

        # ── 脏检查：三注册表版本号均未变 → 直接返回缓存 ──
        tool_ver = self._tool_registry.version
        skill_ver = self._skill_registry.version if self._skill_registry is not None else 0
        agent_ver = self._agent_registry.version if self._agent_registry is not None else 0
        new_version = (tool_ver, skill_ver, agent_ver)
        if (cached := getattr(self, "_static_context_version", None)) is not None:
            if new_version == cached:
                return self._static_context_str  # 未变化，LLM Prefix Cache 可命中

        deferred_tool_catalog = self._tool_registry.get_deferred_tool_catalog()

        skill_summaries_static = None
        if self._skill_registry is not None:
            skill_summaries_static = self._skill_registry.build_skill_summaries()

        agent_summaries_static = None
        if self._agent_registry is not None:
            agent_summaries_static = self._agent_registry.build_agent_summaries(
                exclude_agent_id=self._identity.agent_id,
            )

        result = MessageBuilder.build_static_context_str(
            deferred_tool_summaries=deferred_tool_catalog,
            skill_summaries=skill_summaries_static,
            agent_summaries=agent_summaries_static,
        )

        # ── system_prompt token 配额校验（ContextWindowBudget 场景 5）──
        if self._context_window_budget is not None and result:
            system_prompt_budget = self._context_window_budget.system_prompt_tokens
            system_base_tokens = int(len(self._memory.system_prompt or "") / CHARS_PER_TOKEN)
            available_for_static = system_prompt_budget - system_base_tokens
            static_context_tokens = int(len(result) / CHARS_PER_TOKEN)

            if available_for_static <= 0:
                logger.warning(
                    "context_window_budget: system_prompt 本身 (%d tokens) 已超出 "
                    "system_prompt_tokens 配额 (%d)，static_context 被完全丢弃。",
                    system_base_tokens, system_prompt_budget,
                )
                self._static_context_version = new_version
                return None
            elif static_context_tokens > available_for_static:
                max_chars = int(available_for_static * CHARS_PER_TOKEN)
                logger.warning(
                    "context_window_budget: static_context (%d tokens) 超出 "
                    "system_prompt 剩余配额 (%d tokens)，已截断至 %d 字符。",
                    static_context_tokens, available_for_static, max_chars,
                )
                result = result[:max_chars]

        self._static_context_version = new_version
        return result

    def _build_result(
        self,
        *,
        success: bool,
        run_id: str,
        output: Any = None,
        error: str | None = None,
        terminal_reason: TerminalReason | None = None,
        run_state: RunState | None = None,
        steps: list[StepRecord],
        started_at: datetime,
        start_mono: float,
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        plan_path: str | None = None,
    ) -> AgentResult:
        return AgentResult(
            success=success,
            output=output,
            error=error,
            terminal_reason=terminal_reason,
            run_id=run_id,
            total_steps=len(steps),
            total_duration_ms=(time.monotonic() - start_mono) * 1000,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            steps=tuple(steps),
            run_state=run_state,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc) if not run_state else None,
            plan_path=plan_path,
        )
