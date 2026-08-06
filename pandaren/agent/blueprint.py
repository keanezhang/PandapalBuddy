"""pandaren/agent/blueprint.py — AgentBlueprint 配置快照 + 共享组件容器

上游需求：docs/design/multi-session-concurrency-reform.md §3.3 + §4.1
详细设计：docs/design/AgentBlueprint-详细设计方案.md

角色定位：
  - AgentBuilder 是"配置收集器"，收集完成即完成使命
  - AgentBlueprint 是"配置的运行时化身"，生命周期贯穿整个 App
  - Agent 是"运行时实例"，由 blueprint.materialize() 按需产出

组件共享契约（详见 §3.3）：
  ─── 共享（多 session 复用同一实例）───────────────────────────────
    identity              Identity           HC1 物理不可变
    llm_client            LLMClient          httpx 连接池天然并发安全
    llm_settings          ModelSettings      不可变配置
    tool_registry         ToolRegistry       启动后只读
    skill_registry        SkillRegistry      只读读取 version
    agent_registry        SubAgentRegistry   只读读取 version
    permission_guard      PermissionGuard    无状态函数
    hitl_controller       HITLController     无自身状态
    harness_executor      HarnessExecutor    无状态
    audit_log             AuditLog           HC4 单一审计通道
    execution_limits      ExecutionLimits    不可变配置
    error_policy          ErrorPolicy        不可变配置
    step_guard            StepGuard          通用每步停机守卫（应用层实现，可选）
    context_window_budget ContextWindowBudget 不可变配置
    system_prompt         str                静态字符串
    stream                bool               静态配置

  ─── 独立（每 session 一份，通过工厂产出）─────────────────────────
    memory                Memory             STM/session_meta 按 session 隔离
    hooks                 CompositeAgentHooks 通过 clone() 独立化

新增字段流程：
  1. 先在 docs/design/multi-session-concurrency-reform.md §3.3 契约表登记
  2. 给出共享性论证（"无状态 / 只读 / 按外部键索引" 至少满足一项）
  3. 再动 blueprint 代码，加到本类的字段列表
  4. Code Review 强制审查
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    # 仅供类型注解解析：运行时 Agent 走 materialize() 内的延迟 import（循环依赖规避）
    from .agent import Agent

from ..identity.models import Identity
from ..behavior.harness.executor import HarnessExecutor
from ..behavior.permission_guard import PermissionGuard
from ..behavior.hitl_controller import HITLController
from ..behavior.execution_limits import ExecutionLimits
from ..behavior.error_policy import ErrorPolicy
from ..behavior.step_guard import StepGuard
from ..behavior.context_window_budget import ContextWindowBudget
from ..engine.loop import AgentLoop
from ..hook.hooks import CompositeAgentHooks
from ..llm.protocol import LLMClient
from ..llm.types import ModelSettings
from ..memory.memory import Memory
from ..observability.audit import AuditLog
from ..tool.registry import ToolRegistry

logger = logging.getLogger("pandaren.agent.blueprint")


@dataclass(frozen=True)
class AgentBlueprint:
    """Agent 配置快照 + 共享组件容器。

    构造后所有字段冻结（frozen=True），运行时不可修改。
    通过 ``materialize()`` 产出独立 Agent 实例（Memory / Hooks / _cancelled 独立，
    其余组件共享）。

    典型用法::

        # 启动时构造一次
        blueprint = AgentBuilder().identity(...).llm(...).build_blueprint()

        # 运行时每 session 产出一份
        agent_a = blueprint.materialize()   # session A
        agent_b = blueprint.materialize()   # session B
        # agent_a._loop._memory is not agent_b._loop._memory  ✓
        # agent_a._loop._llm_client is agent_b._loop._llm_client  ✓

    Fail-Safe：任何必填字段缺失 → __post_init__ 立即 raise ValueError。
    不接受静默默认值（HC4 / E4）。
    """

    # ─── 共享组件（materialize 时传引用） ───
    identity: Identity
    llm_client: LLMClient
    tool_registry: ToolRegistry
    permission_guard: PermissionGuard
    hitl_controller: HITLController
    harness_executor: HarnessExecutor
    audit_log: AuditLog
    execution_limits: ExecutionLimits
    error_policy: ErrorPolicy
    system_prompt: str

    # ─── 独立组件工厂（每次 materialize 生成新实例） ───
    memory_factory: Callable[[], Memory]
    hooks_template: CompositeAgentHooks

    # ─── 共享组件（可选） ───
    llm_settings: ModelSettings | None = None
    skill_registry: Any | None = None
    agent_registry: Any | None = None
    step_guard: StepGuard | None = None
    context_window_budget: ContextWindowBudget | None = None
    stream: bool = True

    def __post_init__(self) -> None:
        """必填字段校验（E4 Fail-Safe）。

        dataclass(frozen=True) 已保证类型层面的字段存在；这里对必填字段做
        None 校验和 memory_factory / hooks_template 的鸭子类型校验，
        任何缺失/错型立即 raise。
        """
        required = {
            "identity": self.identity,
            "llm_client": self.llm_client,
            "tool_registry": self.tool_registry,
            "permission_guard": self.permission_guard,
            "hitl_controller": self.hitl_controller,
            "harness_executor": self.harness_executor,
            "audit_log": self.audit_log,
            "execution_limits": self.execution_limits,
            "error_policy": self.error_policy,
            "system_prompt": self.system_prompt,
            "memory_factory": self.memory_factory,
            "hooks_template": self.hooks_template,
        }
        for name, value in required.items():
            if value is None:
                raise ValueError(f"AgentBlueprint requires {name} (got None)")

        if not callable(self.memory_factory):
            raise TypeError(
                f"AgentBlueprint.memory_factory must be callable, "
                f"got {type(self.memory_factory).__name__}"
            )

        if not hasattr(self.hooks_template, "clone"):
            raise TypeError(
                "AgentBlueprint.hooks_template must have clone() method "
                "(use CompositeAgentHooks)"
            )

    def materialize(self) -> "Agent":
        """产出全新 Agent 实例。

        5 步流程（详见 AgentBlueprint 详细设计方案 §6.3）：
          1. memory_factory() → 新 Memory
          2. hooks_template.clone() → 新 CompositeAgentHooks
          3. 构造 AgentLoop（共享 15 个引用 + 独立 memory + 独立 hooks）
          4. 构造 Agent 包装
          5. return

        所有异常向上传播（Blueprint 不吞异常）：
          - memory_factory() 失败 → 消费方（Pool.acquire）释放 semaphore
          - hooks_template.clone() 失败 → 同上
          - AgentLoop 构造失败 → 同上
        """
        # 局部 import 打破循环依赖（Agent 依赖 AgentLoop，Blueprint 依赖 Agent）
        from .agent import Agent

        # Step 1: 新 Memory
        memory = self.memory_factory()

        # Step 2: 新 hooks（clone 内部列表引用，元素不变）
        hooks = self.hooks_template.clone()

        # Step 3: 构造 AgentLoop
        loop = AgentLoop(
            identity=self.identity,
            llm_client=self.llm_client,
            llm_settings=self.llm_settings,
            tool_registry=self.tool_registry,
            harness_executor=self.harness_executor,
            permission_guard=self.permission_guard,
            hitl_controller=self.hitl_controller,
            execution_limits=self.execution_limits,
            error_policy=self.error_policy,
            step_guard=self.step_guard,
            context_window_budget=self.context_window_budget,
            audit_log=self.audit_log,
            memory=memory,
            hooks=hooks,
            skill_registry=self.skill_registry,
            agent_registry=self.agent_registry,
            stream=self.stream,
        )

        # Step 4-5: Agent 包装 + 返回
        agent = Agent(identity=self.identity, loop=loop)
        logger.debug(
            "AgentBlueprint.materialize: agent_id=%s new memory + hooks",
            self.identity.agent_id,
        )
        return agent

    async def aclose(self) -> None:
        """关闭 blueprint 持有的共享资源（当前为 ``llm_client`` 的连接池）。

        blueprint 是「共享组件容器」，也是这些共享引用的 owner-of-record：
        materialize 出的每个 Agent 只是**借用** ``llm_client``，无权关闭它
        （见 ``Agent.aclose()`` 的所有权说明）。因此共享 client 的唯一合法
        关闭点在此——由持有 blueprint 的一方（如 PandaPalApp）在进程停机、
        且确认无 in-flight Agent 时调用**一次**。

        幂等：底层 client.aclose()（httpx / LLMRouter）本身可重复调用；
        单个 client 关闭失败不阻断整体清理。
        """
        close = getattr(self.llm_client, "aclose", None)
        if close is None:
            return
        try:
            await close()
        except Exception as exc:  # noqa: BLE001 — best-effort teardown
            logger.warning("AgentBlueprint.aclose: llm_client close error: %s", exc)

    def __repr__(self) -> str:
        """脱敏 repr：不暴露 llm_client 的 api_key。"""
        return (
            f"AgentBlueprint(identity={self.identity.agent_id!r}, "
            f"tools={len(self.tool_registry.list_tools())}, "
            f"stream={self.stream})"
        )
