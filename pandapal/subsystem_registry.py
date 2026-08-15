"""pandapal.subsystem_registry — PandaPal 子系统集中注册表（★ 层次 3 根本解）。

★ 为什么需要这个模块：
  之前 PandaPalApp.start() 是手写 11 步序列（步骤编号 2.5/2.6/2.7/3/5/6/6.5/6.6/7/8），
  每个子系统在 app.py 中 import + 构造 + 启动。
  这意味着：
    - 新增子系统要改 app.py（import + 构造 + 调用 start）
    - 步骤编号随着子系统增多不断膨胀（2.5 → 2.6 → 2.7 → 3 → 5 → 6 → 6.5 → 6.6 → 7 → 8）
    - 依赖顺序是"程序员记得"，而非"系统验证"
    - app.py 知道所有子系统（God Object 反模式）

  本模块用 SubsystemContainer 集中管理所有 PandaPal 子系统：
    - register_pandapal_subsystems(container) 在一处注册所有 spec
    - app.py 只需 import 此模块 + 调一次 container.start_all()
    - 依赖顺序由容器自动拓扑排序
    - 新增子系统 = 1 处 register() 调用，app.py 不变
"""

import logging
import os

from pandapal.subsystem_container import AppContext, SubsystemContainer, SubsystemSpec

# ★ 层次 3（修复 2026-06-11）：
# 之前用 `_make_X.__globals__["YxxYxx"]` hack 在 register() 时拿类型做 needs。
# 这个 hack 的根本错误：函数体内的 `from X import Y` 不会出现在模块 __globals__ 里。
# 修复：把子系统类在模块顶部 import（这些是叶子模块，无循环依赖风险），
#      register() 时直接用 `ClassName` 当 needs。
from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.broadcast.channel_registry import ChannelDispatchPolicy, ChannelRegistry
from pandapal.desktop_ipc.ipc_transport import IpcStdoutTransport
from pandapal.hitl.bridge import HITLBridge
from pandapal.router.router import MessageRouter
from pandapal.scheduler.agent_pool import SessionAgentPool
from pandapal.scheduler.hitl_manager import HITLManager
from pandapal.scheduler.interaction_manager import InteractionManager
from pandapal.scheduler.plan_manager import PlanModeManager
from pandapal.scheduler.scheduler import AgentScheduler
from pandapal.session.session_group_manager import SessionGroupManager
from pandapal.session.session_list_manager import SessionListManager
from pandapal.task_scheduler.task_scheduler import TaskScheduler
from pandapal.tools.agent_task_tools import AgentTaskTools, set_resume_provider
from pandapal.tools.app_data_tools import AppDataTools
from pandapal.tools.progress_tools import ProgressTools
from pandapal.tools.scheduler_tools import SchedulerTools

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Factory 函数（每个子系统一个）
# ══════════════════════════════════════════════════════════════════════════════


def _make_registry() -> ChannelRegistry:
    """ChannelRegistry（无依赖）。"""
    return ChannelRegistry()


def _make_ipc_transport() -> IpcStdoutTransport:
    """IpcStdoutTransport（无依赖）。"""
    return IpcStdoutTransport()


def _make_broadcast(channel_registry: ChannelRegistry) -> MessageBroadcast:
    """MessageBroadcast（依赖 ChannelRegistry）。

    ★ 启动时把 _init_channels() 逻辑搬到 on_init（spec.init 钩子）：
      在容器实例化 broadcast 之后、start() 之前，注册 IPC + WeCom + 虚拟 channel。
    """
    return MessageBroadcast(registry=channel_registry)


async def _broadcast_init(broadcast, context: AppContext) -> None:
    """broadcast 构造后、start() 前：注册所有 channel。"""
    from pandapal.broadcast.channel_registry import (
        ChannelCapability,
        ChannelInfo,
        ChannelType,
    )

    registry = broadcast.channel_registry

    # ★ 渠道分发策略（2026-06 渠道策略重构）——每渠道一个独立 env 键，所见即所得：
    #     PANDAPAL_CHANNEL_DESKTOP_IPC_POLICY   （默认 source_only）
    #     PANDAPAL_CHANNEL_WECOM_POLICY         （默认 source_only）
    #     PANDAPAL_CHANNEL_XIAOZHI_POLICY       （默认 target_only）
    #   值：shared | source_only | target_only（非法值 → warning + 回落默认）。

    # IPC（LOCAL）— 必有的渠道；默认只看自己来源的事件（防把桌面流式推到远程渠道）
    registry.register(ChannelInfo(
        id="__desktop_ipc__",
        type=ChannelType.LOCAL,
        capabilities=frozenset({
            ChannelCapability.STREAM,
            ChannelCapability.TEXT,
            ChannelCapability.INTERACTIVE,
        }),
        transport=IpcStdoutTransport(),
        user_id=context.user_id,
        dispatch_policy=_channel_policy(
            "DESKTOP_IPC", ChannelDispatchPolicy.SOURCE_ONLY,
        ),
    ))

    # 远程渠道 Transport（WSSGateway；离线模式可为空）
    if context.wss_transport is not None:
        # wecom：默认只收自己来源的事件（防桌面会话串到企微）
        registry.register(ChannelInfo(
            id="wecom",
            type=ChannelType.REMOTE,
            capabilities=frozenset({
                ChannelCapability.TEXT,
                ChannelCapability.TEMPLATE_CARD,
            }),
            transport=context.wss_transport,
            dispatch_policy=_channel_policy(
                "WECOM", ChannelDispatchPolicy.SOURCE_ONLY,
            ),
        ))
        # ★ xiaozhi 独立硬件渠道：默认 TARGET_ONLY ——
        #   R2 规则保证它恒收自己设备来源（origin="xiaozhi:{device_id}"）的事件，
        #   但不收全局事件与别家事件。为什么不用 SOURCE_ONLY（自己+全局）：
        #   全局事件会同时经 wecom / xiaozhi 两个 WSS 渠道出帧，帧内无目标渠道标识，
        #   relay 无法区分 → 企微重复收、音箱反收不到（跨渠道精确路由需帧加
        #   dispatch_channel_id 字段，留作后续扩展）。
        #   origin="xiaozhi:{device_id}" 是本渠道的设备实例 id，
        #   经 origin_aliases 前缀匹配归属本渠道（非跨渠道混合）。
        #   与 wecom 共享同一 WSS 链路（WSSGateway.start/stop 幂等，安全）；
        #   relay 侧 xiaozhi_bridge 按 origin 前缀认领投递到具体设备，
        #   wecom_bridge 白名单拒收 xiaozhi origin——互不串话。
        registry.register(ChannelInfo(
            id="xiaozhi",
            type=ChannelType.REMOTE,
            capabilities=frozenset({
                ChannelCapability.TEXT,
            }),
            transport=context.wss_transport,
            dispatch_policy=_channel_policy(
                "XIAOZHI", ChannelDispatchPolicy.TARGET_ONLY,
            ),
            origin_aliases=("xiaozhi:",),
        ))

    # 内部虚拟 channel（HITLBridge / Scheduler 内部消息用，无 transport）
    registry.register(ChannelInfo(
        id="__hitl_bridge__", type=ChannelType.LOCAL,
        capabilities=frozenset(), transport=None,
    ))
    registry.register(ChannelInfo(
        id="__scheduler__", type=ChannelType.LOCAL,
        capabilities=frozenset(), transport=None,
    ))

    logger.info("Channels registered: %s", [c.id for c in registry.list_active()])


def _channel_policy(env_key: str, default: ChannelDispatchPolicy) -> ChannelDispatchPolicy:
    """读单个渠道的策略 env 键（PANDAPAL_CHANNEL_{env_key}_POLICY）。

    每个渠道一个独立键，所见即所得：
      - 未配置 → 返回 default（代码默认值）
      - 配了非法值 → warning 留痕 + 回落 default（fail-safe，不因拼错改语义）
    """
    raw = os.getenv(f"PANDAPAL_CHANNEL_{env_key}_POLICY", "").strip().lower()
    if not raw:
        return default
    try:
        return ChannelDispatchPolicy(raw)
    except ValueError:
        logger.warning(
            "PANDAPAL_CHANNEL_%s_POLICY=%r 非法（可选 shared|source_only|target_only）"
            " — 按默认 %s 处理",
            env_key, raw, default.value,
        )
        return default


def _make_router() -> MessageRouter:
    """MessageRouter（无依赖）。"""
    return MessageRouter()


def _make_hitl(
    broadcast: MessageBroadcast, router: MessageRouter, context: AppContext
) -> HITLBridge:
    """HITLBridge（依赖 broadcast + router，repo 从 context.storage_manager 取）。"""
    if context.storage_manager is None:
        raise RuntimeError("HITLBridge requires storage_manager in context")
    approval_repo = context.storage_manager.get_approval_repo()
    if approval_repo is None:
        raise RuntimeError("StorageManager.get_approval_repo() returned None")
    run_state_repo = context.storage_manager.get_run_state_repo()
    return HITLBridge(
        approval_repo=approval_repo,
        broadcast=broadcast,
        router=router,
        run_state_repo=run_state_repo,
    )


def _make_interaction_manager(
    broadcast: MessageBroadcast, context: AppContext,
) -> InteractionManager:
    """InteractionManager（依赖 run_state_repo + broadcast）。"""
    from pandapal.scheduler.reply_manager import ReplyIdManager

    run_state_repo = context.storage_manager.get_run_state_repo()
    return InteractionManager(
        repo=run_state_repo,
        broadcast=broadcast,
        reply_id_mgr=ReplyIdManager(),
    )


def _make_hitl_manager(
    broadcast: MessageBroadcast, router: MessageRouter,
    hitl_bridge: HITLBridge, context: AppContext,
) -> HITLManager:
    """HITLManager（依赖 run_state_repo + HITLBridge + broadcast + router）。"""
    from pandapal.scheduler.reply_manager import ReplyIdManager

    run_state_repo = context.storage_manager.get_run_state_repo()
    return HITLManager(
        repo=run_state_repo,
        bridge=hitl_bridge,
        broadcast=broadcast,
        router=router,
        reply_id_mgr=ReplyIdManager(),
    )


def _make_plan_manager(
    broadcast: MessageBroadcast, router: MessageRouter, context: AppContext,
) -> PlanModeManager:
    """PlanModeManager（Plan Mode 审批暂停/恢复管理器）。"""
    run_state_repo = context.storage_manager.get_run_state_repo()
    return PlanModeManager(
        repo=run_state_repo,
        broadcast=broadcast,
        router=router,
    )


def _make_session_pool(
    broadcast: MessageBroadcast, context: AppContext,
) -> SessionAgentPool:
    """SessionAgentPool（多 Session 并发资源管控中心）。

    依赖 broadcast + AgentBlueprint（从 context 取）。
    context.blueprint 缺失时立即抛错（PandaPalApp 已在启动前 fail-fast，
    正常路径不会到这里）。

    并发上限从 config_manager 取；默认 5，可通过环境变量
    PANDAPAL_MAX_CONCURRENT_SESSIONS 覆盖（read_env fallback）。
    """
    if context.blueprint is None:
        raise RuntimeError("SessionAgentPool requires context.blueprint")

    # ★ 并发上限：优先读 config；否则读 env；否则默认 5
    max_concurrent = 5
    idle_ttl = 1800.0
    try:
        env_val = os.getenv("PANDAPAL_MAX_CONCURRENT_SESSIONS")
        if env_val:
            max_concurrent = max(1, int(env_val))
    except Exception:
        pass
    try:
        env_ttl = os.getenv("PANDAPAL_SESSION_IDLE_TTL_SECONDS")
        if env_ttl:
            idle_ttl = max(60.0, float(env_ttl))
    except Exception:
        pass

    return SessionAgentPool(
        blueprint=context.blueprint,
        broadcast=broadcast,
        max_concurrent=max_concurrent,
        idle_ttl_seconds=idle_ttl,
        prompt_by_mode=context.prompt_by_mode,
        default_mode=context.default_mode,
    )


def _make_session_group_manager(
    broadcast: MessageBroadcast,
    context: AppContext,
) -> SessionGroupManager:
    """SessionGroupManager（分组 CRUD + 正向记录维护）。

    依赖：
      - broadcast 广播 SESSION_GROUP_LIST / SESSION_UPDATED
      - 从 context.storage_manager 取 session_repo / group_repo
    """
    sm = context.storage_manager
    if sm is None:
        raise RuntimeError(
            "SessionGroupManager requires storage_manager in context"
        )
    session_repo = sm.get_session_repo()
    group_repo = sm.get_session_group_repo()  # SQLite / Markdown 均返回实例
    return SessionGroupManager(
        session_repo=session_repo,
        group_repo=group_repo,
        broadcast=broadcast,
    )


def _make_session_list_manager(
    pool: SessionAgentPool,
    broadcast: MessageBroadcast,
    group_manager: SessionGroupManager,
    context: AppContext,
) -> SessionListManager:
    """SessionListManager（UI 会话列表元数据管理）。

    依赖：
      - Pool 用于 soft_delete 时 cancel_session
      - broadcast 广播 SESSION_* 事件
      - group_manager 提供 on_session_removed 回调（同步正向记录）
      - 从 context.storage_manager 取 session_repo / group_repo /
        approval_repo / run_state_repo / raw_log_backend
    """
    sm = context.storage_manager
    if sm is None:
        raise RuntimeError(
            "SessionListManager requires storage_manager in context"
        )
    session_repo = sm.get_session_repo()
    group_repo = sm.get_session_group_repo()  # SQLite / Markdown 均返回实例
    approval_repo = sm.get_approval_repo()
    run_state_repo = sm.get_run_state_repo()
    # raw_log_backend 用于 SESSION_HISTORY_REQUEST 回补；用户删除会话时也用于清 payload。
    from pandapal.degradation import DegradationEvent, report_degradation
    try:
        raw_log_backend = sm.get_raw_log_backend(context.user_id or "")
    except Exception:
        report_degradation(
            DegradationEvent.BACKEND_UNAVAILABLE, category="capability",
            source="subsystem_registry.raw_log_backend", fallback=None, exc_info=True,
        )
        raw_log_backend = None
    try:
        working_memory_backend = sm.get_working_memory_backend(context.user_id or "")
    except Exception:
        report_degradation(
            DegradationEvent.BACKEND_UNAVAILABLE, category="capability",
            source="subsystem_registry.working_memory_backend", fallback=None, exc_info=True,
        )
        working_memory_backend = None
    try:
        agent_task_repo = sm.get_agent_task_repo()
    except Exception:
        report_degradation(
            DegradationEvent.BACKEND_UNAVAILABLE, category="capability",
            source="subsystem_registry.agent_task_repo", fallback=None, exc_info=True,
        )
        agent_task_repo = None
    return SessionListManager(
        session_repo=session_repo,
        group_repo=group_repo,
        agent_pool=pool,
        approval_repo=approval_repo,
        run_state_repo=run_state_repo,
        broadcast=broadcast,
        config_manager=context.config_manager,
        raw_log_backend=raw_log_backend,
        working_memory_backend=working_memory_backend,
        agent_task_repo=agent_task_repo,
        on_session_removed=group_manager.on_session_removed,
    )


def _make_agent_scheduler(
    pool: SessionAgentPool,
    broadcast: MessageBroadcast, router: MessageRouter,
    hitl_mgr: HITLManager,
    interaction_mgr: InteractionManager,
    plan_mgr: PlanModeManager,
    task_scheduler: TaskScheduler,
    context: AppContext,
) -> AgentScheduler:
    """AgentScheduler（纯路由，依赖 SessionAgentPool + 三个 Manager + TaskScheduler）。"""
    return AgentScheduler(
        pool=pool,
        session_manager=context.session_manager,
        broadcast=broadcast,
        router=router,
        hitl_mgr=hitl_mgr,
        interaction_mgr=interaction_mgr,
        plan_mgr=plan_mgr,
        task_scheduler=task_scheduler,
    )


def _make_task_scheduler(
    broadcast: MessageBroadcast, router: MessageRouter, context: AppContext
) -> TaskScheduler:
    """TaskScheduler（依赖 broadcast + router，task_repo/config_manager 从 context 取）。"""
    task_repo = context.storage_manager.get_task_repo()
    if task_repo is None:
        raise RuntimeError("StorageManager.get_task_repo() returned None")
    return TaskScheduler(
        task_repo=task_repo,
        broadcast=broadcast,
        router=router,
        config_manager=context.config_manager,  # 可选 None
    )


# ══════════════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════════════


def register_pandapal_subsystems(container: SubsystemContainer) -> None:
    """注册所有 PandaPal 子系统到容器。

    拓扑顺序（容器自动决定）：
        registry → broadcast → router → hitl/agent_scheduler/task_scheduler

    新增子系统：在下面 add 一个 container.register(SubsystemSpec(...)) 即可。
    app.py 不用改。
    """
    # 1. ChannelRegistry（无依赖，根）
    container.register(SubsystemSpec(
        name="registry",
        factory=_make_registry,
        start=False,
    ))

    # 2. IPC Transport（无依赖）
    container.register(SubsystemSpec(
        name="ipc_transport",
        factory=_make_ipc_transport,
        start=False,
    ))

    # 3. MessageBroadcast（依赖 registry；构造后用 init 钩子注册 channels）
    container.register(SubsystemSpec(
        name="broadcast",
        factory=_make_broadcast,
        needs=(ChannelRegistry,),
        init=lambda b: _broadcast_init(b, container.context),
        start=True,
    ))

    # 4. MessageRouter（无依赖）
    container.register(SubsystemSpec(
        name="router",
        factory=_make_router,
        start=False,
    ))

    # 5. HITLBridge（依赖 broadcast + router）
    container.register(SubsystemSpec(
        name="hitl",
        factory=_make_hitl,
        needs=(MessageBroadcast, MessageRouter),
        context_needs=(AppContext,),
        start=False,
    ))

    # 6. InteractionManager（依赖 broadcast + run_state_repo from context）
    container.register(SubsystemSpec(
        name="interaction_manager",
        factory=_make_interaction_manager,
        needs=(MessageBroadcast,),
        context_needs=(AppContext,),
        start=False,
    ))

    # 7. HITLManager（依赖 broadcast + router + HITLBridge + run_state_repo）
    container.register(SubsystemSpec(
        name="hitl_manager",
        factory=_make_hitl_manager,
        needs=(MessageBroadcast, MessageRouter, HITLBridge),
        context_needs=(AppContext,),
        start=False,
    ))

    # 8. PlanModeManager（依赖 broadcast + router + run_state_repo）
    container.register(SubsystemSpec(
        name="plan_manager",
        factory=_make_plan_manager,
        needs=(MessageBroadcast, MessageRouter),
        context_needs=(AppContext,),
        start=False,
    ))

    # 8.5 SessionAgentPool（多 Session 并发资源管控；从 context.blueprint materialize Agent）
    container.register(SubsystemSpec(
        name="session_pool",
        factory=_make_session_pool,
        needs=(MessageBroadcast,),
        context_needs=(AppContext,),
        start=True,  # 启动后台 evict 循环
    ))

    # 8.55 SessionGroupManager（分组 CRUD + 正向记录；依赖 broadcast + storage）
    container.register(SubsystemSpec(
        name="session_group_manager",
        factory=_make_session_group_manager,
        needs=(MessageBroadcast,),
        context_needs=(AppContext,),
        start=False,
    ))

    # 8.6 SessionListManager（UI 会话列表元数据；依赖 Pool + broadcast + GroupManager + storage）
    container.register(SubsystemSpec(
        name="session_list_manager",
        factory=_make_session_list_manager,
        needs=(SessionAgentPool, MessageBroadcast, SessionGroupManager),
        context_needs=(AppContext,),
        start=False,
    ))

    # 9. AgentScheduler（依赖 SessionAgentPool + broadcast + router + 三个 Manager + TaskScheduler）
    container.register(SubsystemSpec(
        name="agent_scheduler",
        factory=_make_agent_scheduler,
        needs=(SessionAgentPool, MessageBroadcast, MessageRouter, HITLManager, InteractionManager, PlanModeManager, TaskScheduler),
        context_needs=(AppContext,),
        start=False,
    ))

    # 7. TaskScheduler（依赖 broadcast + router）
    container.register(SubsystemSpec(
        name="task_scheduler",
        factory=_make_task_scheduler,
        needs=(MessageBroadcast, MessageRouter),
        context_needs=(AppContext,),
        start=True,  # start() 内部有异步初始化
    ))

    # 8. SchedulerTools Provider（★ 层次 3 核心：依赖通过构造注入，无 module-level 单例）
    def _make_scheduler_tools(
        broadcast: MessageBroadcast, task_scheduler: TaskScheduler
    ) -> SchedulerTools:
        """SchedulerTools Provider（替代 module-level 单例 + inject_task_scheduler）。

        ★ 不能用 lambda：容器需要 factory.__annotations__['return'] 来建立 type→name 索引。
        """
        return SchedulerTools(task_scheduler=task_scheduler, broadcast=broadcast)

    container.register(SubsystemSpec(
        name="scheduler_tools",
        factory=_make_scheduler_tools,
        needs=(MessageBroadcast, TaskScheduler),
        start=False,
    ))

    # 9. AgentTaskTools Provider（★ 层次 3 核心：依赖通过构造注入，无 module-level 单例）
    #     注意：AgentTaskTools 需要 repo（从 storage_manager.get_agent_task_repo() 取）
    #           + broadcaster。从 context 取 repo，从容器取 broadcast。
    def _make_agent_task_tools(
        broadcast: MessageBroadcast, context: AppContext
    ) -> AgentTaskTools:
        if context.storage_manager is None:
            raise RuntimeError("AgentTaskTools requires storage_manager in context")
        repo = context.storage_manager.get_agent_task_repo()
        if repo is None:
            raise RuntimeError("StorageManager.get_agent_task_repo() returned None")
        provider = AgentTaskTools(repo=repo, broadcaster=broadcast)
        set_resume_provider(provider)
        return provider

    container.register(SubsystemSpec(
        name="agent_task_tools",
        factory=_make_agent_task_tools,
        needs=(MessageBroadcast,),
        context_needs=(AppContext,),
        start=False,
    ))

    # 10. AppDataTools Provider（快应用数据推送工具）
    def _make_app_data_tools(
        broadcast: MessageBroadcast,
    ) -> AppDataTools:
        return AppDataTools(broadcaster=broadcast)

    container.register(SubsystemSpec(
        name="app_data_tools",
        factory=_make_app_data_tools,
        needs=(MessageBroadcast,),
        start=False,
    ))

    # 11. ProgressTools Provider（技能/长任务进度上报工具）
    def _make_progress_tools(
        broadcast: MessageBroadcast,
    ) -> ProgressTools:
        return ProgressTools(broadcaster=broadcast)

    container.register(SubsystemSpec(
        name="progress_tools",
        factory=_make_progress_tools,
        needs=(MessageBroadcast,),
        start=False,
    ))

    logger.info("PandaPal subsystems registered: %d specs", len(container._specs))
