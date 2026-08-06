"""pandapal.app — PandaPal Backend 唯一启动入口（★ 层次 3 改造 2026-06-11）。

★ 唯一启动入口：
  - 类的实例：PandaPalApp
  - 工厂函数：run_pandapal

★ ★ 层次 3 根本解改造（2026-06-11）：
  之前：手写 11 步序列（步骤编号 2.5/2.6/2.7/3/5/6/6.5/6.6/7/8），每个子系统在 app.py 中
        import + 构造 + 启动 + 注册 handlers。新增子系统要改 5+ 处（class import、构造、
        步骤编号、inject_xxx、AST 测试列表），且步骤编号不断膨胀。
  之后：app.py 只持有 SubsystemContainer，启动期调一次 container.start_all()。
        所有子系统的 import / 构造 / 启动 / 拓扑排序全部委托给容器（pandapal.subsystem_container）
        和集中注册表（pandapal.subsystem_registry）。
        新增子系统 = 1 处 SubsystemSpec 注册，app.py 不用改。

★ 设计原则：
  - 容器制：PandaPalApp 不再 import 子系统类（IPC Server 除外，它依赖 shutdown_event）
  - 依赖图集中：所有依赖关系在 subsystem_registry.py 一处声明
  - 启动自检：container.start_all() 内置失败隔离 + 启动自检日志
  - 零"记着调"：没有 inject_xxx() 全局副作用，没有"步骤 X.Y"的脆弱编号
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.broadcast.channel_registry import ChannelRegistry
from pandapal.broadcast.transport import Transport
from pandapal.desktop_ipc.message_codec import ScheduledTaskItem
from pandapal.desktop_ipc.stdio_ipc import StdioIpcServer
from pandapal.desktop_ipc.message_codec import IpcMessageType
from pandapal.desktop_ipc.inbound_adapter import IpcInboundAdapter
from pandapal.dispatch.dispatcher import InboundDispatcher
from pandapal.dispatch.pipeline import InboundPipeline
from pandapal.events.normalized import EventType, NormalizedEvent
from pandapal.gateway.inbound_adapter import GatewayInboundAdapter
from pandapal.scheduler.reply_manager import ReplyIdManager
from pandapal.session.manager import SessionManager
from pandapal.subsystem_container import AppContext, SubsystemContainer
from pandapal.subsystem_registry import register_pandapal_subsystems

from pandapal.degradation import DegradationEvent, report_degradation

logger = logging.getLogger(__name__)


class PandaPalApp:
    """PandaPal Backend 应用容器（唯一启动入口）。

    ★ 层次 3：app.py 不再 import 子系统类，只持有 SubsystemContainer。
    所有子系统通过 subsystem_registry.register_pandapal_subsystems() 注册，
    容器自动按依赖图拓扑排序启动。

    使用：
        app = PandaPalApp(
            config={"user_id": "alice"},
            blueprint=blueprint,
            session_manager=session_manager,
            storage_manager=storage_manager,
            wss_transport=wss_transport,  # 可选（WSSGateway，承载所有远程渠道）
        )
        await app.start()
        await app.shutdown_event.wait()  # 等待 stdin EOF / SIGINT / SIGTERM
        await app.stop()

    公共属性（start 之后可用）：
        - broadcast:    MessageBroadcast
        - router:       MessageRouter
        - hitl:         HITLBridge
        - scheduler:    AgentScheduler
        - task_scheduler: TaskScheduler
        - ipc_server:   StdioIpcServer
        - shutdown_event: asyncio.Event
        - container:    SubsystemContainer（★ 层次 3 新增：所有子系统引用）
    """

    def __init__(
        self,
        config: dict[str, Any],
        blueprint: Any,
        session_manager: SessionManager | None = None,
        storage_manager: Any = None,
        wss_transport: Transport | None = None,
        config_manager: Any = None,
        prompt_by_mode: dict[str, str] | None = None,
        default_mode: str = "",
        available_models: list[Any] | None = None,
        default_model_id: str = "",
    ) -> None:
        if blueprint is None:
            raise ValueError("PandaPalApp requires blueprint")
        self._config = config
        self._blueprint = blueprint
        self._session_manager = session_manager
        self._storage_manager = storage_manager
        self._wss_transport = wss_transport
        self._config_manager = config_manager
        # 模型选择：可选模型清单（AvailableModel 列表）+ 默认模型，供 MODEL_LIST 下发。
        self._available_models = available_models or []
        self._default_model_id = default_model_id
        # 双层 Prompt：{mode: 完整prompt} + 缺省模式，供 SessionAgentPool 做 delta-rebind。
        self._prompt_by_mode = prompt_by_mode or {}
        self._default_mode = default_mode

        # Reply ID manager（轻量，无依赖）
        self._reply_id_mgr = ReplyIdManager()

        # Shutdown 事件：stdin EOF / SIGINT / SIGTERM 都会 set
        self._shutdown_event = asyncio.Event()
        self._started = False

        # ★ 层次 3：SubsystemContainer（start 时构造并装配）
        self._container: SubsystemContainer | None = None
        # IPC server 特殊处理（依赖 shutdown_event，仍由 app.py 启动）
        self._ipc_server: StdioIpcServer | None = None
        # SessionListHandler（会话列表 IPC 处理）
        self._session_list_handler: Any = None

    # ══════════════════════════════════════════════════════════════════════════════
    # 启动 / 关闭（★ 层次 3：委托给容器，11 步手写 → 1 行启动）
    # ══════════════════════════════════════════════════════════════════════════════

    def _register_provider_tools(self) -> None:
        """将容器中 Provider 的工具注册到 Agent ToolRegistry。

        Provider（AgentTaskTools / SchedulerTools）的依赖在
        container.start_all() 之后才到位，无法在 blueprint 构造时注册。
        此方法在 start_all 之后调用，自动发现所有带 get_tools() 的实例。

        ToolRegistry 从 blueprint 取（共享），注册后所有 materialize 出的
        Agent 都能看到。

        设计原则：
          - 自动发现：不硬编码 Provider 名，新增 Provider 只需在
            subsystem_registry.py 注册 SubsystemSpec，此处零改动。
          - 容忍缺失：get_tools() 失败或 ToolRegistry 不可访问时记录 warning 而非崩溃。
          - 幂等：skip_if_exists=True，重复注册静默跳过。
        """
        if self._container is None:
            return

        tool_registry = getattr(self._blueprint, "tool_registry", None)
        if tool_registry is None:
            logger.warning(
                "_register_provider_tools: blueprint.tool_registry 不可访问，"
                "Provider 工具将不可用"
            )
            return

        registered = 0
        total = 0

        for name, instance in self._container._instances.items():
            get_tools = getattr(instance, "get_tools", None)
            if not callable(get_tools):
                continue

            try:
                tools = get_tools()
            except Exception as e:
                logger.error("_register_provider_tools: %s.get_tools() 失败: %s", name, e)
                continue

            if not isinstance(tools, list):
                continue

            for tool in tools:
                try:
                    tool_registry.register_tool(tool, skip_if_exists=True)
                    total += 1
                except Exception as e:
                    logger.warning(
                        "_register_provider_tools: 注册 '%s' (from %s) 失败: %s",
                        getattr(tool, "full_name", "?"), name, e,
                    )

            registered += 1
            logger.info("_register_provider_tools: %s → %d tools", name, len(tools))

        if registered > 0:
            logger.info(
                "_register_provider_tools: %d provider(s) → %d tools",
                registered, total,
            )

    def _register_skill_hooks(self) -> None:
        """延迟绑定 SkillAwareHooks 的 MessageBroadcast。

        SkillAwareHooks 在 blueprint 构造时无 broadcast，broadcast 子系统在
        container.start_all() 之后才就绪。此方法从 blueprint.hooks_template
        中找到 SkillAwareHooks 实例并完成绑定（共享；bind_broadcast 会影响
        所有 materialize 出的 Agent）。

        设计原则：
          - 容忍缺失：hooks 不可访问或类型不匹配时静默跳过。
          - 幂等：多次调用 bind_broadcast 仅更新引用。
        """
        if self._container is None:
            return

        hooks = getattr(self._blueprint, "hooks_template", None)
        if hooks is None:
            return

        from pandapal.hooks.skill_hooks import SkillAwareHooks

        # CompositeAgentHooks 内可能包含多个子 hook，需要遍历找 SkillAwareHooks
        skill_hooks: Any = None
        if isinstance(hooks, SkillAwareHooks):
            skill_hooks = hooks
        else:
            inner = getattr(hooks, "_hooks", None)
            if isinstance(inner, list):
                for h in inner:
                    if isinstance(h, SkillAwareHooks):
                        skill_hooks = h
                        break

        if skill_hooks is None:
            # 未启用 SkillAwareHooks 时静默跳过
            return

        broadcast = self._container.get("broadcast")
        if broadcast is None:
            logger.warning(
                "_register_skill_hooks: broadcast 子系统不可用，"
                "Skill 生命周期事件无法推送"
            )
            return

        skill_hooks.bind_broadcast(broadcast)
        logger.info("_register_skill_hooks: SkillAwareHooks.broadcast 绑定完成")

    async def _ipc_send_token_refreshed(self, new_token: str) -> None:
        """Gateway JWT 刷新成功 → 通知前端回写 auth_store.json。

        全局级 IPC 控制面消息（明确不带 session_id），write_raw 直通，
        前端收到后 invoke("auth_update_token") 回写 store。
        """
        try:
            ipc_transport = self._container.get("ipc_transport")
            ipc_transport.write_raw({
                "type":      IpcMessageType.AUTH_TOKEN_REFRESHED,
                "msg_id":    uuid.uuid4().hex,
                "timestamp": time.time() * 1000,
                "token":     new_token,
            })
            logger.info("AUTH_TOKEN_REFRESHED pushed to desktop (store write-back)")
        except Exception as e:
            # 发送失败仅影响下次冷启动体验（需重登一次），当前会话不阻断（O3）
            logger.warning("AUTH_TOKEN_REFRESHED push failed: %s", e)

    async def _ipc_send_auth_expired(self) -> None:
        """Refresh 被 Relay 401 拒绝（超宽限期/签名无效）→ 通知前端登出 + 跳登录页。

        全局级 IPC 控制面消息（明确不带 session_id），Gateway 侧已停止重连。
        """
        try:
            ipc_transport = self._container.get("ipc_transport")
            ipc_transport.write_raw({
                "type":      IpcMessageType.AUTH_EXPIRED,
                "msg_id":    uuid.uuid4().hex,
                "timestamp": time.time() * 1000,
            })
            logger.info("AUTH_EXPIRED pushed to desktop (re-login required)")
        except Exception as e:
            logger.warning("AUTH_EXPIRED push failed: %s", e)

    async def start(self) -> None:
        """启动所有子系统（I3 幂等）。

        ★ 层次 3：所有子系统创建 + 启动 + 拓扑排序全部委托给 SubsystemContainer。
        本方法只做 3 件事：
          1. 校验外部依赖（Storage）
          2. 构造容器 + 注册子系统 + 启动
          3. 启动 IPC server（特殊子系统：依赖 shutdown_event）
        """
        if self._started:
            return
        logger.info("PandaPal app starting...")

        # 1. 外部依赖校验（启动期 fail-fast）
        if self._storage_manager is None:
            raise RuntimeError(
                "PandaPalApp requires storage_manager. "
                "Call await storage_manager.initialize_storage() before app.start()."
            )

        # 2. ★ 层次 3：构造容器 + 注册 + 启动（一行搞定所有子系统）
        context = AppContext(
            blueprint=self._blueprint,
            session_manager=self._session_manager,
            storage_manager=self._storage_manager,
            config_manager=self._config_manager,
            wss_transport=self._wss_transport,
            shutdown_event=self._shutdown_event,
            user_id=self._config.get("user_id", ""),
            prompt_by_mode=self._prompt_by_mode,
            default_mode=self._default_mode,
        )
        self._container = SubsystemContainer(context=context)
        register_pandapal_subsystems(self._container)
        await self._container.start_all()

        # ★ 防时序竞态：WSS 已连接 Relay，立刻获取 router、创建 dispatcher、接线 Gateway
        # 入站 handler。从 Gateway._on_message_received 到 dispatcher.dispatch 的全链路
        # 必须在 start_all 后尽早就绪，避免 Relay 离线推送消息在 handler 注册前抵达而被
        # 静默丢弃。
        router = self._container.get("router")
        # ★ 直通路径集中式转发：所有直通 handler 只构建并返回 NormalizedEvent（或 None），
        #   由 dispatcher 统一 broadcast.send() 并注入 origin_channel_id（修复企微等渠道
        #   直通请求收不到响应的问题）；豁免路径（非请求触发的自主推送）仍自广播。
        dispatcher = InboundDispatcher(
            router=router,
            broadcast=self._container.get("broadcast"),
        )
        if self._wss_transport is not None:
            gateway_pipeline = InboundPipeline(
                GatewayInboundAdapter(),
                dispatcher,
            )
            self._wss_transport.register_inbound_handler(gateway_pipeline.handle)
            # ★ JWT 自动续期接线：Gateway refresh 成功/失败 → IPC 通知前端。
            #   全局级控制面消息（明确不带 session_id，SESSION_ID 契约 §八 #4），
            #   走 write_raw 直通（与 PONG 同模式），不经 NormalizedEvent/broadcast
            #   —— 企微/XiaoZhi 渠道无感知（见计划「不在变更范围」）。
            self._wss_transport.register_on_token_refreshed_callback(
                self._ipc_send_token_refreshed
            )
            self._wss_transport.register_on_auth_expired_callback(
                self._ipc_send_auth_expired
            )
            logger.info("Gateway inbound + token-refresh callbacks wired (race-free)")

        # 2.4 将 Provider 工具注入 Agent ToolRegistry
        #   Provider 工具（AgentTaskTools / SchedulerTools）的依赖在
        #   container.start_all() 之后才到位，因此在此处延迟注册。
        self._register_provider_tools()

        # 2.4.1 延迟绑定 Skill 生命周期 Hooks
        #   SkillAwareHooks 依赖 MessageBroadcast，broadcast 在
        #   container.start_all() 之后才就绪，因此在此延迟绑定。
        self._register_skill_hooks()

        # 2.5 启动自检：打印每个 transport 的真实状态
        broadcast = self._container.get("broadcast")
        lifecycle = broadcast.get_lifecycle_snapshot()
        not_started = [s for s in lifecycle if not s["is_started"]]
        if not_started:
            logger.warning(
                "PandaPalApp startup self-check: %d transport(s) NOT started: %s",
                len(not_started), not_started,
            )
        for s in lifecycle:
            logger.info(
                "channel=%s transport=%s is_started=%s",
                s["channel_id"], s["transport"], s["is_started"],
            )

        # 3. Router 注册 handlers（HITL + Scheduler + TaskScheduler）
        # ★ 入站归一化：dispatcher 已在 start_all 后立即创建并接线 Gateway；
        #   此处继续注册 Router 路由 handler（直通类 + Router 类）。
        hitl = self._container.get("hitl")
        agent_scheduler = self._container.get("agent_scheduler")
        task_scheduler = self._container.get("task_scheduler")

        if hitl is not None:
            hitl.register_route_handlers()
            # ★ 防呆：恢复所有 pending 审批（自动过期孤儿审批）
            try:
                await hitl.restore_pending_approvals()
            except Exception as e:
                logger.warning("HITL restore_pending_approvals failed: %s", e)
        if agent_scheduler is not None:
            agent_scheduler.register_route_handlers()
            # ★ 防呆：TTL 清理 + 恢复未过期的 ask_user 问卷
            try:
                await agent_scheduler.startup_maintenance()
            except Exception as e:
                logger.warning("AgentScheduler startup_maintenance failed: %s", e)
        if task_scheduler is not None:
            task_scheduler.register_route_handlers()
            # 启动调度引擎（恢复 pending + 加载 cron 任务）
            try:
                await task_scheduler.initialize_scheduler()
                logger.info(
                    "TaskScheduler initialized (cron_jobs=%d, event_types=%d)",
                    len(task_scheduler._cron_handles),
                    len(task_scheduler._event_handlers),
                )
            except Exception as e:
                # HC3 Fail-Safe：TaskScheduler 不可用不阻塞整体启动
                logger.warning("TaskScheduler init failed: %s", e)

        # 4. IPC Server（含 IpcStdoutTransport，依赖 shutdown_event）
        ipc_transport = self._container.get("ipc_transport")
        self._ipc_server = StdioIpcServer(
            transport=ipc_transport,
            channel_id="__desktop_ipc__",
            user_id=self._config.get("user_id", ""),
            shutdown_event=self._shutdown_event,  # stdin EOF → 触发 shutdown
        )
        # ★ 入站归一化：复用上方共享 dispatcher，绑定 IPC 专属 adapter 组成 pipeline
        ipc_pipeline = InboundPipeline(
            IpcInboundAdapter(config_user_id=self._config.get("user_id", "")),
            dispatcher,
        )
        self._ipc_server.set_inbound_pipeline(ipc_pipeline)

        # 桌面专属能力（本地文件路径 / 本地凭据文件）：限定仅桌面渠道放行
        IPC_ONLY = frozenset({"__desktop_ipc__"})

        # ★ 定时任务列表：build_task_list_event（构建）+ push_task_list（豁免自广播）+ 直通注册
        #   直通路径集中式转发改造：handler 只构建事件返回，Dispatcher 统一转发并注入
        #   origin_channel_id；任务变更回调（非请求触发，豁免路径）仍自广播。
        if task_scheduler is not None:
            task_repo = self._storage_manager.get_task_repo()
            broadcast = self._container.get("broadcast")

            async def build_task_list_event() -> NormalizedEvent:
                """查询所有任务定义 → 构建 SCHEDULED_TASK_LIST 事件。"""
                import dataclasses
                import json as _json

                try:
                    definitions = await task_repo.find_all_task_definitions()
                except Exception as e:
                    logger.error("build_task_list_event: query definitions failed: %s", e)
                    definitions = []

                tasks: list[dict] = []
                for d in definitions:
                    # 解析 trigger_rule
                    trigger_type_val = "manual"
                    cron_expr = ""
                    try:
                        data = _json.loads(d.trigger_rule_json)
                        trigger_type_val = data.get("trigger_type", "manual")
                        cron_expr = data.get("cron_expression", "") or ""
                    except Exception:
                        # trigger_rule_json 损坏 → 回落 manual/空 cron（UI 展示类），留痕暴露脏数据。
                        report_degradation(
                            DegradationEvent.TRIGGER_RULE_JSON_CORRUPT,
                            category="display", source="app.build_task_list_event",
                            fallback="manual", dedup_key=f"trigger_rule:{d.task_id}",
                            exc_info=True,
                        )

                    item = ScheduledTaskItem(
                        task_id=d.task_id,
                        name=d.name,
                        trigger_type=trigger_type_val,
                        cron_expression=cron_expr,
                        task_prompt=d.task_prompt or "",
                        session_id=d.session_id or "",
                        sensitivity=d.sensitivity or "medium",
                        created_at=d.created_at.isoformat() if d.created_at else "",
                    )
                    tasks.append(dataclasses.asdict(item))

                logger.debug("build_task_list_event: built %d tasks", len(tasks))
                return NormalizedEvent(
                    event_type=EventType.SCHEDULED_TASK_LIST,
                    payload={"tasks": tasks},
                )

            async def push_task_list() -> None:
                """任务变更回调（豁免路径：非请求触发，自广播）。"""
                if broadcast is not None:
                    await broadcast.send(await build_task_list_event())

            async def delete_task(task_id: str) -> NormalizedEvent | None:
                """确定性删除定时任务（前端删除按钮直连，绕过 LLM）。

                unregister_task_definition 内部会持久化删除（含 markdown .md 文件）
                并触发 _on_task_list_changed → push_task_list 回推最新列表（豁免路径）。
                成功返回 None（避免与回调自广播重复推送）；失败返回最新列表事件，
                由 Dispatcher 转发，让前端乐观删除的条目复原（最终对账）。
                """
                try:
                    await task_scheduler.unregister_task_definition(task_id)
                    logger.info("delete_task: task_id=%s unregistered", task_id)
                    return None
                except Exception as e:
                    logger.error("delete_task: failed task_id=%s: %s", task_id, e)
                    return await build_task_list_event()

            async def _on_delete_scheduled_task(_t: str, d: dict, _c) -> NormalizedEvent | None:
                # 空 task_id 守卫（原 stdio_ipc 内联逻辑搬家）
                task_id = str(d.get("task_id", ""))
                if task_id:
                    return await delete_task(task_id)
                logger.warning("DELETE_SCHEDULED_TASK missing task_id")
                return None

            dispatcher.register(
                IpcMessageType.REQUEST_SCHEDULED_TASKS,
                lambda _t, _d, _c: build_task_list_event(),
            )
            dispatcher.register(IpcMessageType.DELETE_SCHEDULED_TASK, _on_delete_scheduled_task)
            task_scheduler.set_task_list_changed_callback(push_task_list)
            logger.info("task list handler injected (D1 Pull + D2 Push + delete)")

        # ★ 注入 Skill 资源管理 handler
        from pathlib import Path
        from pandapal.resources.skill_manager import SkillManager

        # system/: 随 sidecar 打包，只读，用 __file__ 定位
        resources_dir = Path(__file__).resolve().parent / "resources"
        # user/:  用 user_resources_dir（~/.pandapal），持久化、不受升级影响
        user_dir = Path(self._config.get("user_resources_dir", "")) / "skills"
        user_dir.mkdir(parents=True, exist_ok=True)
        skill_manager = SkillManager(
            system_dir=resources_dir / "skills" / "system",
            user_dir=user_dir,
        )
        # 字段提取逻辑从原 stdio_ipc if-else 搬家至此（data 现由原样透传）。
        # 直通路径集中式转发：handler 只构建并返回事件，由 Dispatcher 统一转发。
        dispatcher.register(
            IpcMessageType.SKILL_LIST,
            lambda _t, _d, _c: skill_manager.build_skill_list_event(),
        )
        dispatcher.register(
            IpcMessageType.SKILL_GET,
            lambda _t, d, _c: skill_manager.build_skill_detail_event(
                str(d.get("skill_name", ""))),
        )

        async def _on_skill_save(_t: str, d: dict, _c) -> NormalizedEvent:
            payload = {
                "description": str(d.get("description", "")),
                "when_to_use": str(d.get("when_to_use", "")),
                "content": str(d.get("content", "")),
                "tags": d.get("tags"),
            }
            return await skill_manager.save_and_build_event(
                str(d.get("skill_name", "")), payload)

        dispatcher.register(IpcMessageType.SKILL_SAVE, _on_skill_save)
        dispatcher.register(
            IpcMessageType.SKILL_DELETE,
            lambda _t, d, _c: skill_manager.delete_and_build_event(
                str(d.get("skill_name", ""))),
        )

        async def _on_skill_import(_t: str, d: dict, _c) -> NormalizedEvent:
            sp = d.get("source_path")
            return await skill_manager.import_and_build_event(
                content=str(d.get("content", "")),
                fmt=str(d.get("format", "md")),
                overwrite=bool(d.get("overwrite", False)),
                source_path=str(sp) if sp is not None else None,
            )

        async def _on_skill_export(_t: str, d: dict, _c) -> NormalizedEvent:
            tp = d.get("target_path")
            return await skill_manager.export_and_build_event(
                str(d.get("skill_name", "")), str(d.get("format", "md")),
                str(tp) if tp is not None else None,
            )

        # 桌面专属（带本地文件路径）：限定仅桌面渠道
        dispatcher.register(IpcMessageType.SKILL_IMPORT, _on_skill_import, channels=IPC_ONLY)
        dispatcher.register(IpcMessageType.SKILL_EXPORT, _on_skill_export, channels=IPC_ONLY)
        logger.info("SkillManager injected (resources/skills/{system,user})")

        # ★ 注入模型选择 handler：前端 MODEL_LIST_REQUEST → 回推 MODEL_LIST（可选清单 + default）。
        #   请求-响应（拉取）模式：handler 只构建事件返回，Dispatcher 统一转发；
        #   IPC 消息无重放，由前端在 ready 后主动拉取，规避主动推送早于前端订阅导致的丢失。
        from pandapal.config.llm.model_registry import to_model_list_payload

        async def build_model_list_event(_t: str, _d: dict, _c) -> NormalizedEvent | None:
            try:
                payload = to_model_list_payload(
                    self._available_models, self._default_model_id,
                )
                return NormalizedEvent(
                    event_type=EventType.MODEL_LIST,
                    payload=payload,
                )
            except Exception as e:
                logger.error("build_model_list_event failed: %s", e)
                return None

        dispatcher.register(
            IpcMessageType.MODEL_LIST_REQUEST,
            build_model_list_event,
        )
        logger.info(
            "model_list handler injected (%d models, default=%s)",
            len(self._available_models), self._default_model_id,
        )

        # ★ 注入 SessionListManager + SessionListHandler（UI 会话列表）
        try:
            from pandapal.session.session_list_handler import SessionListHandler
            session_list_mgr = self._container.get("session_list_manager")
            broadcast = self._container.get("broadcast")
            session_handler = SessionListHandler(
                manager=session_list_mgr,
                user_id=self._config.get("user_id", ""),
            )
            self._session_list_handler = session_handler

            # ★ 看板 handler（复用 session_list 分派通道）。数据源按 storage_mode 二分：
            #   markdown → _storage_path 是 pandapal_md/users/{uid} 目录（扫 .md）
            #   sqlite   → _storage_path 是 users/{uid}/pandapal.db（查库，observability.db 同目录）
            from pandapal.dashboard.handler import DashboardHandler
            dashboard_handler = DashboardHandler(
                storage_path=getattr(self._storage_manager, "_storage_path", ""),
                storage_mode=getattr(self._storage_manager, "_storage_mode", "markdown"),
                broadcast=broadcast,
            )
            self._dashboard_handler = dashboard_handler

            # ★ 预算额度 handler（按 provider 分账；复用 session_list 分派通道）。
            #   账本取自守卫（pool.cost_source.ledger，经 executor 暴露）；未注入 → handler no-op。
            from pandapal.budget.handler import BudgetHandler
            _budget_ledger = None
            try:
                _sched_for_budget = self._container.get("agent_scheduler")
                _exec_for_budget = getattr(_sched_for_budget, "_executor", None)
                _get_ledger = getattr(_exec_for_budget, "_budget_ledger", None)
                _budget_ledger = _get_ledger() if callable(_get_ledger) else None
            except Exception as _be:
                logger.warning("resolve budget ledger failed: %s", _be)
            budget_handler = BudgetHandler(
                ledger=_budget_ledger,
                broadcast=broadcast,
                user_id=self._config.get("user_id", ""),
            )
            self._budget_handler = budget_handler

            # ★ LLM 凭据 handler（BYOK：用户自填 LLM 配置）
            _cred_user_id = self._config.get("user_id", "")
            _cred_data_dir = self._config.get("data_dir", "")
            if _cred_user_id and _cred_data_dir:
                from pathlib import Path
                from pandapal.config.llm.credentials_store import CredentialStore
                from pandapal.config.llm.credentials_handler import CredentialsHandler

                _cred_dir = Path(_cred_data_dir)
                _cred_store = CredentialStore(_cred_dir)
                self._credentials_handler = CredentialsHandler(
                    store=_cred_store,
                    user_id=_cred_user_id,
                )
                # 注册直通 handler（凭据为本地凭据文件，桌面专属渠道限定）
                ch = self._credentials_handler
                dispatcher.register(
                    IpcMessageType.LOAD_CREDENTIALS,
                    lambda _t, _d, _c: ch.handle_load(), channels=IPC_ONLY)
                dispatcher.register(
                    IpcMessageType.SAVE_LLM_CREDENTIALS,
                    lambda _t, d, _c: ch.handle_save(d), channels=IPC_ONLY)
                dispatcher.register(
                    IpcMessageType.VERIFY_CREDENTIALS,
                    lambda _t, d, _c: ch.handle_verify(d), channels=IPC_ONLY)
                dispatcher.register(
                    IpcMessageType.GET_CREDENTIALS_STATUS,
                    lambda _t, _d, _c: ch.handle_status(), channels=IPC_ONLY)
                logger.info("CredentialsHandler injected (BYOK)")

            async def _session_list_dispatch(
                msg_type: str, data: dict, _ctx,
            ) -> NormalizedEvent | list[NormalizedEvent] | None:
                # 直通路径集中式转发：各 handler 只构建并返回事件（成功路径豁免的返回 None），
                # 由 Dispatcher 统一转发并注入 origin_channel_id。
                if msg_type == IpcMessageType.DASHBOARD_REQUEST:
                    return await dashboard_handler.handle_dashboard_request(data)
                if msg_type == IpcMessageType.SET_BUDGET:
                    return await budget_handler.handle_set_budget(data)
                if msg_type == IpcMessageType.BUDGET_QUERY:
                    return await budget_handler.handle_budget_query(data)
                if msg_type == IpcMessageType.SESSION_LIST_REQUEST:
                    return await session_handler.handle_session_list_request(data)
                elif msg_type == IpcMessageType.SESSION_CREATE:
                    return await session_handler.handle_session_create(data)
                elif msg_type == IpcMessageType.SESSION_SWITCH:
                    return await session_handler.handle_session_switch(data)
                elif msg_type == IpcMessageType.SESSION_DELETE:
                    return await session_handler.handle_session_delete(data)
                elif msg_type == IpcMessageType.SESSION_FAVORITE_TOGGLE:
                    return await session_handler.handle_session_favorite_toggle(data)
                elif msg_type == IpcMessageType.SESSION_GROUP_MUTATE:
                    return await session_handler.handle_session_group_mutate(data)
                elif msg_type == IpcMessageType.SESSION_HISTORY_REQUEST:
                    return await session_handler.handle_session_history_request(data)
                return None

            # 10 种共享同一分派（session×7 + dashboard + budget×2）
            for _t in (
                IpcMessageType.SESSION_LIST_REQUEST, IpcMessageType.SESSION_CREATE,
                IpcMessageType.SESSION_SWITCH, IpcMessageType.SESSION_DELETE,
                IpcMessageType.SESSION_FAVORITE_TOGGLE, IpcMessageType.SESSION_GROUP_MUTATE,
                IpcMessageType.SESSION_HISTORY_REQUEST, IpcMessageType.DASHBOARD_REQUEST,
                IpcMessageType.SET_BUDGET, IpcMessageType.BUDGET_QUERY,
            ):
                dispatcher.register(_t, _session_list_dispatch)

            # ★ 注入全局搜索 handler（命令面板 ⌘K，复用 SessionListManager）
            _search_user_id = self._config.get("user_id", "")

            async def _search_dispatch(query: str) -> NormalizedEvent:
                try:
                    result = await session_list_mgr.search(_search_user_id, query)
                except Exception as e:
                    logger.error("search handler failed: %s", e)
                    result = {"sessions": [], "messages": []}
                return NormalizedEvent.search_result(
                    query=query,
                    sessions=result.get("sessions", []),
                    messages=result.get("messages", []),
                )

            dispatcher.register(
                IpcMessageType.SEARCH,
                lambda _t, d, _c: _search_dispatch(str(d.get("query", ""))),
            )

            # 把 SessionListManager 注入 AgentExecutor（用于 on_first_message /
            # touch_activity 钩子）
            try:
                scheduler = self._container.get("agent_scheduler")
                # AgentScheduler 内部持有 _executor
                executor = getattr(scheduler, "_executor", None)
                if executor is not None and hasattr(executor, "set_session_list_manager"):
                    executor.set_session_list_manager(session_list_mgr)
                    logger.info(
                        "SessionListManager injected into AgentExecutor hooks",
                    )
                # ★ 注入可用模型清单：executor 校验入站 model_id 用（与 MODEL_LIST 下发同源）
                if executor is not None and hasattr(executor, "set_available_models"):
                    executor.set_available_models(self._available_models)
                # ★ P2 实时刷新：run 结束（flush 落盘后）自动重推看板快照 + 预算额度态
                if executor is not None and hasattr(executor, "set_run_finished_handler"):
                    async def _on_run_finished(session_id: str, user_id: str) -> None:
                        # 看板（仅在打开过时推）
                        await dashboard_handler.push_if_active(session_id, user_id)
                        # 额度条：run 消费后 spent 变了，重推 BUDGET_STATUS 让额度条实时更新
                        try:
                            await budget_handler.handle_budget_query({})
                        except Exception:
                            logger.warning("[Budget] run-finished status push failed", exc_info=True)
                    executor.set_run_finished_handler(_on_run_finished)
                    logger.info("Dashboard auto-push wired to AgentExecutor run-finished")
            except Exception as e:
                logger.warning(
                    "Failed to inject SessionListManager into AgentExecutor: %s", e,
                )

            logger.info("SessionListHandler injected (UI session list)")
        except Exception as e:
            # HC3 Fail-Safe：会话列表故障不阻塞其他功能
            logger.exception("SessionListHandler injection failed: %s", e)
            self._session_list_handler = None

        # ★ 入站归一化：所有直通 handler 注册完毕，冻结后运行期只读
        dispatcher.freeze()

        await self._ipc_server.start()

        # ★ 会话列表启动引导：清 is_empty 遗留 + 建初始空 session + 广播首屏
        if self._session_list_handler is not None:
            try:
                await self._session_list_handler.bootstrap()
                logger.info("SessionList bootstrap completed")
            except Exception as e:
                logger.warning("SessionList bootstrap failed: %s", e)

        # ★ 预算额度启动引导：连接后首屏推一次 BUDGET_STATUS（额度条初始态）
        _bh = getattr(self, "_budget_handler", None)
        if _bh is not None:
            try:
                await _bh.bootstrap()
                logger.info("Budget bootstrap completed")
            except Exception as e:
                logger.warning("Budget bootstrap failed: %s", e)

        # 注：此处原有「BYOK 凭据 → 注入 os.environ」的启动引导，已随 LLMConfig 一并删除。
        # 那条链是 BYOK 改造前的遗留：inject_to_environ 把凭据写进环境变量，专供
        # LLMConfig.from_env() 读取——而 LLMConfig 早已无人调用（run_local 直接从
        # CredentialStore.load_all_raw() 构造 client）。删除同时缩小了明文 key 的暴露面
        # （环境变量对子进程可见、会进崩溃转储）。

        self._started = True
        logger.info("PandaPal app started")

    async def stop(self) -> None:
        """优雅关闭（I3 幂等）。

        ★ 层次 3：委托 container.stop_all() 对称关闭所有子系统。
        """
        if not self._started:
            return
        logger.info("PandaPal app stopping...")

        # 1. IPC server 单独停（最早停：防止新消息进来）
        if self._ipc_server:
            try:
                await self._ipc_server.stop()
            except Exception as e:
                logger.warning("IPC server stop error: %s", e)
            self._ipc_server = None

        # 2. 容器对称关闭（按启动顺序逆序）
        #    ★ 此步会停掉 SessionAgentPool：cancel 所有 in-flight Agent 并丢弃实例。
        if self._container is not None:
            try:
                await self._container.stop_all()
            except Exception as e:
                logger.warning("Container.stop_all error: %s", e)

        # 3. 关闭共享 LLM client（唯一合法关闭点）
        #    blueprint 是共享组件容器 / owner-of-record；池子只管 Agent 实例，
        #    无权关闭跨 session 共享的 client。此处在池子停完（无 in-flight Agent）
        #    之后关一次，避免「单实例 aclose 连带关掉整个共享连接池」的事故。
        blueprint = getattr(self, "_blueprint", None)
        aclose = getattr(blueprint, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception as e:
                logger.warning("Blueprint.aclose error: %s", e)

        self._started = False
        logger.info("PandaPal app stopped")

    # ══════════════════════════════════════════════════════════════════════════════
    # 公共属性（向后兼容：start 之后可用）
    # ══════════════════════════════════════════════════════════════════════════════

    @property
    def shutdown_event(self) -> asyncio.Event:
        return self._shutdown_event

    @property
    def container(self) -> SubsystemContainer | None:
        """★ 层次 3 新增：SubsystemContainer（start 之后可用）。"""
        return self._container

    def _get(self, name: str) -> Any:
        """从容器拿子系统（start 之前/之后都可调，未启动时返回 None）。"""
        if self._container is None or not self._container.has(name):
            return None
        return self._container.get(name)

    @property
    def broadcast(self) -> MessageBroadcast | None:
        return self._get("broadcast")

    @property
    def router(self):
        return self._get("router")

    @property
    def scheduler(self):
        return self._get("agent_scheduler")

    @property
    def session_pool(self):
        """★ 多 Session 并发新增：SessionAgentPool（start 之后可用）。"""
        return self._get("session_pool")

    @property
    def hitl(self):
        return self._get("hitl")

    @property
    def task_scheduler(self):
        return self._get("task_scheduler")

    @property
    def scheduler_tools(self):
        """★ 层次 3 新增：SchedulerTools Provider（替代 inject_task_scheduler）。"""
        return self._get("scheduler_tools")

    @property
    def ipc_server(self) -> StdioIpcServer | None:
        return self._ipc_server

    @property
    def channel_registry(self) -> ChannelRegistry | None:
        if self._container is None or not self._container.has("registry"):
            return None
        return self._container.get("registry")

    @property
    def reply_id_manager(self) -> ReplyIdManager:
        return self._reply_id_mgr


async def run_pandapal(
    config: dict[str, Any],
    blueprint: Any,
    session_manager: SessionManager | None = None,
    storage_manager: Any = None,
    wss_transport: Transport | None = None,
) -> PandaPalApp:
    """工厂函数：构造 + 启动，返回 PandaPalApp 实例。

    示例::

        app = await run_pandapal(
            config={"user_id": "alice"},
            blueprint=blueprint,
            session_manager=session_manager,
            storage_manager=storage_manager,
            wss_transport=wss_transport,
        )
    """
    app = PandaPalApp(
        config=config,
        blueprint=blueprint,
        session_manager=session_manager,
        storage_manager=storage_manager,
        wss_transport=wss_transport,
    )
    await app.start()
    return app
