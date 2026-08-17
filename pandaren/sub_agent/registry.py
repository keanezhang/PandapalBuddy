"""pandaren/agent/registry.py — SubAgentRegistry 核心实现

职责：
  - Agent 蓝图注册与注册时校验（AR1）
  - Agent 注销（AR7）
  - Agent 状态管理（AR8）
  - Agent 摘要列表构建（AR2, 1% 上下文预算）
  - Agent 委派执行（AR4, delegate_task）
  - 健康刷新（AR5, refresh_health）
  - call_agent 内置 Tool 由 AgentToolFactory 统一注册（见 builder），本类不再自注册

生命周期（多会话并发隔离）：
  - 启动时注册子 Agent 的 **materialize 工厂**（蓝图），不持有常驻实例
  - 每次委派（call_agent）时调用工厂产出一个**全新 Agent 实例**（独立 Memory / Hooks），
    run 完即弃（实例由 GC 回收，共享 llm_client 不关）——不同会话/并发委派天然物理隔离
  - register() 仅接受蓝图（有 materialize()），Agent 实例注册已彻底移除（TypeError）

与 SkillRegistry / ToolRegistry 形成对称三件套设计。
"""

from __future__ import annotations

import contextvars
import logging
import time
from typing import Any, TYPE_CHECKING

from ..agent import AgentStatus
from .models import (
    SubAgentSummary, SubAgentDelegateResult,
)
from .exceptions import SubAgentRegistrationError
# token 估算系数：从全局 constants 统一引用
from ..constants import CHARS_PER_TOKEN as _CHARS_PER_TOKEN

if TYPE_CHECKING:
    from ..agent import Agent
    from ..identity.models import Identity
    from ..tool.registry import ToolRegistry
    from ..observability.audit import AuditLog

logger = logging.getLogger("pandaren.sub_agent.registry")

# description 截断上限
_DEFAULT_MAX_DESCRIPTION_CHARS: int = 200

# 默认最大委派深度
_DEFAULT_MAX_DELEGATE_DEPTH: int = 1  # 仅一层：子 Agent 不可再委派


class SubAgentRegistry:
    """Agent 系统核心管理器。

    生命周期：
      - 启动时注册子 Agent 的 materialize 工厂（蓝图），不持有常驻实例
      - 每轮 Phase 1 调用 build_agent_summaries() 注入 system prompt
      - call_agent 作为 ALWAYS 级 Tool 注册到 ToolRegistry
      - 每轮开始时 refresh_health() 刷新健康状态
      - 每次委派时工厂产出全新 Agent 实例（独立 Memory），run 完即弃（多会话并发隔离）

    与 SkillRegistry / ToolRegistry 形成对称三件套：
      Tool   = 我能做什么（原子操作）
      Skill  = 我能知道什么（知识注入）
      Agent  = 我能委托谁（任务委派）
    """

    # ── 类级变量：委派调用栈（contextvars.ContextVar，每个异步 task 独立）──
    _delegate_stack: contextvars.ContextVar = contextvars.ContextVar(
        "agent_delegate_stack", default=None,
    )

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry | None = None,
        audit_log: AuditLog | None = None,
        max_description_chars: int = _DEFAULT_MAX_DESCRIPTION_CHARS,
        max_delegate_depth: int = _DEFAULT_MAX_DELEGATE_DEPTH,
    ) -> None:
        # ── A 类：Agent 定义存储 ──
        # materialize 工厂（agent_id → Callable[[], Agent]，委派时产出全新实例）
        self._factories: dict[str, Any] = {}
        # Identity 元数据缓存（agent_id → Identity，只读共享，供摘要/查询/审计）
        self._identities: dict[str, Identity] = {}

        # ── 配置（构造后只读）──
        self._max_description_chars = max_description_chars
        self._max_delegate_depth = max_delegate_depth
        self._tool_registry = tool_registry
        self._audit_log = audit_log

        # ── B 类：运行时状态 ──
        # 健康状态（B 类，运行时可变）
        self._status: dict[str, AgentStatus] = {}

        # 委派调用栈（AG-S3 循环检测，B 类运行时状态）
        # P1: 并发场景使用 contextvars.ContextVar，每个异步 task 独立栈
        # default=None → 新 context 或无 context 时 get() 返回 None

        self._version: int = 0  # register/unregister 时递增，供脏检查

    # ════════════════════════════════════════════════
    #  配置只读属性
    # ════════════════════════════════════════════════

    @property
    def max_description_chars(self) -> int:
        return self._max_description_chars

    @property
    def max_delegate_depth(self) -> int:
        return self._max_delegate_depth

    @property
    def tool_registry(self) -> ToolRegistry | None:
        return self._tool_registry

    @property
    def version(self) -> int:
        """注册表版本号，每次 register/unregister 递增。供脏检查用。"""
        return self._version

    # ════════════════════════════════════════════════
    #  AR1：Agent 蓝图注册
    # ════════════════════════════════════════════════

    def register(self, blueprint: Any) -> None:
        """注册子 Agent 蓝图（仅接受蓝图，不接受 Agent 实例）。

        Args:
            blueprint: AgentBlueprint（或任何有 ``materialize()`` + ``identity``
              的对象）。委派时每次调用 materialize() 产出全新实例（独立
              Memory / Hooks）→ 多会话并发隔离。

        agent_id 唯一性检查（已存在 → 抛 SubAgentRegistrationError）。
        元数据从 blueprint.identity 提取。

        Raises:
            TypeError: 传入对象没有 materialize()（如 Agent 实例）——SDK
              自 v0.2 起只接受蓝图，实例注册属于旧用法，请改传 build_blueprint()
              的产物。
        """
        if not hasattr(blueprint, "materialize"):
            raise TypeError(
                "SubAgentRegistry.register() 只接受蓝图（有 materialize() 的对象），"
                f"收到 {type(blueprint).__name__}。请改用 AgentBuilder.build_blueprint()"
                " 产出蓝图后再注册——SDK 已移除 Agent 实例注册的兼容路径。"
            )
        identity = blueprint.identity
        factory = blueprint.materialize

        agent_id = identity.agent_id

        # 唯一性检查
        if agent_id in self._factories:
            raise SubAgentRegistrationError(
                f"Agent '{agent_id}' 已注册。"
                f"如需替换，请先调用 unregister('{agent_id}') 注销。"
            )

        # 存储
        self._factories[agent_id] = factory
        self._identities[agent_id] = identity
        self._status[agent_id] = AgentStatus.HEALTHY

        # 审计
        self._write_audit_event(
            "AGENT_REGISTERED",
            agent_id=agent_id,
            detail=f"Agent registered: {agent_id} ({identity.agent_name}), "
                   f"trust={identity.trust_level.name}",
        )

        # logger.info(
        #     "Agent 已注册: %s [name=%s, trust=%s]",
        #     agent_id, identity.agent_name, identity.trust_level.name,
        # )
        self._version += 1

    # ════════════════════════════════════════════════
    #  AR7：注销 Agent
    # ════════════════════════════════════════════════

    def unregister(self, agent_id: str) -> None:
        """注销 Agent（幂等，不存在时静默返回）。"""
        if agent_id not in self._factories:
            return

        self._factories.pop(agent_id, None)
        self._identities.pop(agent_id, None)
        self._status.pop(agent_id, None)

        self._write_audit_event(
            "AGENT_UNREGISTERED",
            agent_id=agent_id,
            detail=f"Agent unregistered: {agent_id}",
        )

        logger.info("Agent 已注销: %s", agent_id)
        self._version += 1

    # ════════════════════════════════════════════════
    #  AR8：状态管理
    # ════════════════════════════════════════════════

    def set_status(self, agent_id: str, status: AgentStatus) -> None:
        """设置 Agent 健康状态。"""
        if agent_id not in self._factories:
            raise SubAgentRegistrationError(
                f"Agent '{agent_id}' 未注册，无法设置状态"
            )

        old_status = self._status.get(agent_id, AgentStatus.HEALTHY)
        self._status[agent_id] = status

        self._write_audit_event(
            "AGENT_STATUS_CHANGED",
            agent_id=agent_id,
            detail=f"Agent status changed: {agent_id}, "
                   f"{old_status.value} → {status.value}",
        )

        logger.info(
            "Agent 状态变更: %s (%s → %s)",
            agent_id, old_status.value, status.value,
        )

    def drain(self, agent_id: str) -> None:
        """优雅下线（语义糖，等价于 set_status(DRAINING)）。"""
        self.set_status(agent_id, AgentStatus.DRAINING)

    # ════════════════════════════════════════════════
    #  查询
    # ════════════════════════════════════════════════

    def get_identity(self, agent_id: str) -> Identity | None:
        """精确查找 Identity（从注册元数据缓存读取，只读共享）。"""
        return self._identities.get(agent_id)

    def get_agent(self, agent_id: str) -> None:
        """⚠️ deprecated：registry 不再持有常驻 Agent 实例。

        委派时经工厂（materialize）产出全新实例，用后即弃，无法也不应返回
        "当前实例"。如需调试，请直接调用 factory（`registry._factories[agent_id]()`）。
        统一返回 None，防止误用共享实例导致上下文串扰。
        """
        return None

    def list_identities(self) -> tuple[Identity, ...]:
        """枚举所有已注册 Identity。"""
        return tuple(self._identities.values())

    def agent_count(self) -> int:
        """已注册 Agent 数量。"""
        return len(self._factories)

    def get_status(self, agent_id: str) -> AgentStatus | None:
        """获取 Agent 健康状态。"""
        return self._status.get(agent_id)

    # ════════════════════════════════════════════════
    #  AR2：build_agent_summaries（注入 system prompt）
    # ════════════════════════════════════════════════

    def build_agent_summaries(
        self,
        context_window: int = 128_000,
        exclude_agent_id: str | None = None,
    ) -> list[SubAgentSummary]:
        """构建 Agent 摘要列表，受 1% 上下文预算约束。

        Args:
            context_window: 上下文窗口大小（token 数），默认 128K。
            exclude_agent_id: 排除的 agent_id（通常是调用方自身）。

        Returns:
            按 agent_id 排序的摘要列表，超出 1% 预算时裁剪。
        """
        if not self._identities:
            return []

        budget_tokens = int(context_window * 0.01)
        summaries: list[SubAgentSummary] = []
        used_tokens = 0

        for agent_id, identity in sorted(self._identities.items()):
            # 排除调用方自身
            if exclude_agent_id and agent_id == exclude_agent_id:
                continue

            # 仅返回 HEALTHY 的 Agent
            if self._status.get(agent_id) != AgentStatus.HEALTHY:
                continue

            desc = self._truncate_description(
                identity.when_to_use, self._max_description_chars,
            )
            # 粗略估算 token 数
            entry_tokens = (
                len(identity.agent_id) + len(identity.agent_name) + len(desc)
            ) // _CHARS_PER_TOKEN + 5

            if used_tokens + entry_tokens > budget_tokens:
                logger.debug(
                    "Agent 摘要预算已满（%d/%d tokens），跳过 '%s'",
                    used_tokens, budget_tokens, agent_id,
                )
                continue

            summaries.append(SubAgentSummary(
                agent_name=identity.agent_name,
                when_to_use=desc,
            ))
            used_tokens += entry_tokens

        return summaries

    # ════════════════════════════════════════════════
    #  AR3：call_agent（LLM 决策 + 执行一体化工具）
    # ════════════════════════════════════════════════

    async def call_agent(
        self,
        agent_name: str,
        task: str,
        context: Any,
    ) -> Any:
        """委派任务给指定 Agent。

        LLM 通过 system prompt 中的 agent_name + when_to_use 自行判断选谁，
        直接传 agent_name 调用本方法，代码只做精确查找 + 执行。

        Args:
            agent_name: 目标 Agent 的名称（来自 system prompt 中的 <name>）。
            task: 要委派的任务描述。
            context: ToolContext。

        Returns:
            ToolResult。
        """
        from ..tool.definition.tool_result import ToolResult

        caller_agent_id = getattr(context, "agent_id", "")

        # 按 agent_name 精确查找 agent_id
        target_agent_id = self._find_agent_id_by_name(agent_name, exclude_agent_id=caller_agent_id)
        if target_agent_id is None:
            return ToolResult(
                success=False,
                error=f"未找到名称为 '{agent_name}' 的 Agent，请检查名称是否正确。",
                tool_name="call_agent",
            )

        return await self._execute_delegate(target_agent_id, task, context)

    # ════════════════════════════════════════════════
    #  AR4：_execute_delegate（内部执行，不对外暴露）
    # ════════════════════════════════════════════════

    async def _execute_delegate(
        self,
        agent_id: str,
        task: str,
        context: Any,
    ) -> Any:
        """内部执行委派。信任验证 / 循环检测 / 审计硬编码，不可绕过。

        Args:
            agent_id: 目标 Agent ID（内部使用，LLM 不感知）。
            task: 委派的任务描述。
            context: ToolContext。

        Returns:
            ToolResult。
        """
        from ..tool.definition.tool_result import ToolResult
        from ..identity.models import TrustLevel

        caller_agent_id = getattr(context, "agent_id", "")
        caller_trust = getattr(context, "trust_level", TrustLevel.SUB_AGENT)

        # ── Step 1: 查找并产出目标 Agent（工厂 materialize，每次委派全新实例）──
        factory = self._factories.get(agent_id)
        if factory is None:
            return ToolResult(
                success=False,
                error=f"Agent '{agent_id}' not found",
                tool_name="call_agent",
            )

        # 从注册元数据缓存取 Identity（委派实例产出前即可完成信任/健康校验）
        target_identity = self._identities.get(agent_id)

        # ── Step 2: 健康检查 ──
        status = self._status.get(agent_id, AgentStatus.UNHEALTHY)
        if status != AgentStatus.HEALTHY:
            return ToolResult(
                success=False,
                error=f"Agent '{agent_id}' is {status.value}",
                tool_name="call_agent",
            )

        # ── Step 3: 信任验证（AG-S1，硬编码不可绕过）──
        trust_error = self._check_trust(
            caller_trust, target_identity.trust_level,
            caller_agent_id, agent_id,
        )
        if trust_error:
            self._write_audit_event(
                "AGENT_DELEGATE_DENIED",
                agent_id=caller_agent_id,
                detail=f"Delegate denied: {caller_agent_id} → {agent_id}, "
                       f"reason={trust_error}",
                context=context,
            )
            return ToolResult(
                success=False,
                error=trust_error,
                tool_name="call_agent",
            )

        # ── 获取当前调用栈（ContextVar，每个异步 task 独立）──
        stack: list[str] = self._delegate_stack.get(None)
        if stack is None:
            stack = []

        # ── Step 4: 循环委派检测（AG-S3）──
        if agent_id in stack:
            error_msg = (
                f"循环委派检测：{agent_id} 已在委派链中 "
                f"({' → '.join(stack)} → {agent_id})"
            )
            self._write_audit_event(
                "AGENT_DELEGATE_CYCLE",
                agent_id=caller_agent_id,
                detail=error_msg,
                context=context,
            )
            return ToolResult(
                success=False,
                error=error_msg,
                tool_name="call_agent",
            )

        # ── Step 5: 深度检查 ──
        if len(stack) >= self._max_delegate_depth:
            error_msg = (
                f"委派深度超限：当前深度 {len(stack)} "
                f">= 上限 {self._max_delegate_depth}"
            )
            self._write_audit_event(
                "AGENT_DELEGATE_DEPTH_EXCEEDED",
                agent_id=caller_agent_id,
                detail=error_msg,
                context=context,
            )
            return ToolResult(
                success=False,
                error=error_msg,
                tool_name="call_agent",
            )

        # ── Step 6: 审计 AGENT_DELEGATED ──
        self._write_audit_event(
            "AGENT_DELEGATED",
            agent_id=caller_agent_id,
            detail=f"Delegate: {caller_agent_id} → {agent_id}, task={task[:200]}",
            context=context,
        )

        # ── Step 7: 执行（AG-S7: push/pop 对齐，finally 保证弹出）──
        stack.append(caller_agent_id)
        self._delegate_stack.set(stack)
        start_time = time.monotonic()
        # Layer 3：把父 cancel_token 透传给子 Agent，令其 run 入口 link 父子取消链
        # （见契约 §3.6 方案 B）。父取消 → 子级联取消，多层委派天然递归。
        # 父 token 由 run_core 注入 ctx.metadata["cancel_token"]。
        _parent_cancel_token = None
        _ctx_meta = getattr(context, "metadata", None)
        if _ctx_meta is not None:
            _parent_cancel_token = _ctx_meta.get("cancel_token")
        _delegate_metadata = (
            {"parent_cancel_token": _parent_cancel_token}
            if _parent_cancel_token is not None
            else None
        )
        if _delegate_metadata is not None:
            logger.info(
                "[cancel] Layer3 · delegating %s → %s WITH parent cancel token (cascade armed)",
                caller_agent_id, agent_id,
            )
        # 每次委派 materialize 全新实例（独立 Memory / Hooks）→ 多会话并发隔离。
        # materialize 放 try 内：失败 → ToolResult(success=False)，不向上抛。
        try:
            target_agent = factory()
            agent_result = await target_agent.run(
                task,
                session_id=getattr(context, "session_id", None) or "delegate",
                metadata=_delegate_metadata,
            )
        except Exception as e:
            duration_ms = (time.monotonic() - start_time) * 1000
            logger.error("Agent '%s' 委派执行异常: %s", agent_id, e)
            delegate_result = SubAgentDelegateResult(
                success=False,
                error=str(e),
                target_agent_id=agent_id,
                duration_ms=duration_ms,
            )
            return self._build_delegate_tool_result(delegate_result)
        finally:
            # AG-S7: finally 中 pop，保证异常时也弹出
            stack_final = self._delegate_stack.get(None)
            if stack_final and stack_final[-1] == caller_agent_id:
                stack_final.pop()
                if stack_final:
                    self._delegate_stack.set(stack_final)

        duration_ms = (time.monotonic() - start_time) * 1000

        # ── Step 8: 审计 AGENT_DELEGATE_COMPLETED ──
        self._write_audit_event(
            "AGENT_DELEGATE_COMPLETED",
            agent_id=caller_agent_id,
            detail=f"Delegate completed: {caller_agent_id} → {agent_id}, "
                   f"success={agent_result.success}, "
                   f"duration={duration_ms:.1f}ms",
            context=context,
        )

        # ── Step 9: 包装结果 ──
        delegate_result = SubAgentDelegateResult(
            success=agent_result.success,
            output=agent_result.output,
            error=agent_result.error,
            target_agent_id=agent_id,
            target_run_id=agent_result.run_id,
            duration_ms=duration_ms,
        )

        return self._build_delegate_tool_result(delegate_result)

    # ════════════════════════════════════════════════
    #  AR5：refresh_health（健康刷新）
    # ════════════════════════════════════════════════

    def refresh_health(self) -> None:
        """刷新所有已注册 Agent 的健康状态。

        由 AgentLoop 在每轮 Phase 1 Prepare 中调用。
        当前实现：蓝图存在性检测（蓝图常驻注册表，与运行实例无关）。
          - agent_id 还在 _identities 中且 status != DRAINING → HEALTHY
          - 蓝图注销（unregister）→ UNHEALTHY
          - 未来可扩展：心跳 / ping / 状态回调
        """
        for agent_id in list(self._status.keys()):
            if agent_id not in self._identities:
                # 蓝图已注销 → UNHEALTHY
                self._status[agent_id] = AgentStatus.UNHEALTHY
                continue

            current = self._status[agent_id]
            if current == AgentStatus.DRAINING:
                # DRAINING 不恢复为 HEALTHY
                continue

            if current == AgentStatus.UNHEALTHY:
                # 蓝图注册仍在 → HEALTHY
                self._status[agent_id] = AgentStatus.HEALTHY

    # ════════════════════════════════════════════════
    #  内部方法
    # ════════════════════════════════════════════════

    def _find_agent_id_by_name(
        self, agent_name: str, *, exclude_agent_id: str = "",
    ) -> str | None:
        """按 agent_name 精确查找 agent_id。

        Args:
            agent_name: LLM 传入的 agent_name。
            exclude_agent_id: 排除的 agent_id（调用方自身）。

        Returns:
            agent_id 或 None（未找到）。
        """
        name_lower = agent_name.lower().strip()
        for agent_id, identity in self._identities.items():
            if agent_id == exclude_agent_id:
                continue
            if self._status.get(agent_id) != AgentStatus.HEALTHY:
                continue
            if identity.agent_name.lower() == name_lower:
                return agent_id
        return None

    def _check_trust(
        self,
        caller_trust: Any,
        target_trust: Any,
        caller_id: str,
        target_id: str,
    ) -> str | None:
        """信任验证（AG-S1 硬约束）。

        Returns:
            None = 允许委派，str = 拒绝原因。
        """
        from ..identity.models import TrustLevel

        # EXTERNAL 不可委派任何 Agent
        if caller_trust == TrustLevel.EXTERNAL:
            return (
                f"EXTERNAL Agent '{caller_id}' 不可委派任务"
                f"（信任等级不足）"
            )

        # ORCHESTRATOR 可以委派任何 Agent
        if caller_trust == TrustLevel.ORCHESTRATOR:
            return None

        # SUB_AGENT 只能委派 trust_level ≤ 自己
        if caller_trust < target_trust:
            return (
                f"Agent '{caller_id}'（{caller_trust.name}）无法委派 "
                f"Agent '{target_id}'（{target_trust.name}）：不可向上委派"
            )

        return None

    def _build_delegate_tool_result(self, delegate_result: SubAgentDelegateResult) -> Any:
        """将 SubAgentDelegateResult 序列化为 ToolResult。"""
        from ..tool.definition.tool_result import ToolResult

        agent_id = delegate_result.target_agent_id

        if delegate_result.success:
            data_text = (
                f"✅ Agent '{agent_id}' 执行完成\n\n"
                f"{delegate_result.output}"
            )
        else:
            data_text = (
                f"❌ Agent '{agent_id}' 执行失败\n\n"
                f"错误: {delegate_result.error}"
            )

        return ToolResult(
            success=delegate_result.success,
            data=data_text,
            error=delegate_result.error if not delegate_result.success else None,
            tool_name="call_agent",
            duration_ms=delegate_result.duration_ms,
        )

    @staticmethod
    def _truncate_description(text: str, max_chars: int) -> str:
        """描述截断。"""
        if len(text) <= max_chars:
            return text
        return text[:max_chars - 3] + "..."

    # ════════════════════════════════════════════════
    #  审计事件写入
    # ════════════════════════════════════════════════

    def _write_audit_event(
        self,
        event_name: str,
        *,
        agent_id: str,
        detail: str,
        context: Any = None,
    ) -> None:
        """写入审计事件（AG-S4 硬编码，不可跳过）。"""
        if self._audit_log is None:
            return
        try:
            from ..observability.types import AuditEventType

            event_type_map = {
                "AGENT_REGISTERED": AuditEventType.AGENT_REGISTERED,
                "AGENT_UNREGISTERED": AuditEventType.AGENT_UNREGISTERED,
                "AGENT_STATUS_CHANGED": AuditEventType.AGENT_STATUS_CHANGED,
                "AGENT_DELEGATED": AuditEventType.AGENT_DELEGATED,
                "AGENT_DELEGATE_COMPLETED": AuditEventType.AGENT_DELEGATE_COMPLETED,
                "AGENT_DELEGATE_DENIED": AuditEventType.AGENT_DELEGATE_DENIED,
                "AGENT_DELEGATE_CYCLE": AuditEventType.AGENT_DELEGATE_CYCLE,
                "AGENT_DELEGATE_DEPTH_EXCEEDED": AuditEventType.AGENT_DELEGATE_CYCLE,
            }
            event_type = event_type_map.get(
                event_name, AuditEventType.AGENT_REGISTERED,
            )

            self._audit_log.write_sync(
                event_type,
                agent_id=agent_id,
                run_id=getattr(context, "run_id", "") if context else "",
                detail=detail,
                step_n=getattr(context, "step_n", None) if context else None,
            )
        except Exception as e:
            logger.warning("Agent 审计写入失败: %s", e)

    def __repr__(self) -> str:
        stack = self._delegate_stack.get(None)
        depth = len(stack) if stack else 0
        return (
            f"SubAgentRegistry(agents={len(self._factories)}, "
            f"healthy={sum(1 for s in self._status.values() if s == AgentStatus.HEALTHY)}, "
            f"delegate_depth={depth})"
        )
