"""AgentScheduler — Agent 调度层（纯路由）。

职责：消息分发、依赖组装。暂停/恢复/生命周期逻辑全部在
HITLManager / InteractionManager 中，Executor 负责流式执行。

路由：
  USER_INSTRUCTION    → handle_user_instruction
  APPROVAL_DECISION   → hitl_mgr.resume
  INTERACTION_RESPONSE → interaction_mgr.resume
  TASK_INSTRUCTION    → handle_task_instruction

设计约束：
- O3 (Never Throw): handle_* 永不向外抛异常
- E1: 必填依赖构造时校验
- ★ 多 Session 并发：不再持有单 Agent 实例，而是通过 SessionAgentPool
  按需 acquire。stop_generation 只 cancel 指定 session。
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.broadcast.channel_ids import LOCAL_SCHEDULER_CHANNEL_ID
from pandapal.events.normalized import NormalizedEvent
from pandapal.messages.types import RouterMessageType
from pandapal.router.models import InboundMessage
from pandapal.router.router import MessageRouter
from pandapal.scheduler.agent_pool import SessionAgentPool
from pandapal.scheduler.background import spawn_background
from pandapal import session_id as session_id_mod
from pandapal.scheduler.executor import AgentExecutor
from pandapal.scheduler.hitl_manager import HITLManager
from pandapal.scheduler.interaction_manager import InteractionManager
from pandapal.scheduler.plan_manager import PlanModeManager
from pandapal.scheduler.reply_manager import ReplyIdManager, ReplyScope
from pandapal.session.manager import SessionManager

if TYPE_CHECKING:
    from pandapal.task_scheduler.task_scheduler import TaskScheduler

logger = logging.getLogger(__name__)

API_CHANNEL_ID = "__desktop_ipc__"
_RUN_STATE_TTL_SECONDS = 30 * 60  # 30 分钟


class AgentScheduler:
    """Agent 调度层（纯路由）。

    不含任何暂停/恢复/生命周期逻辑——全部委托给 HITLManager
    和 InteractionManager。

    ★ 多 Session 并发：通过 SessionAgentPool 按需 materialize Agent，
      每个 session 拥有独立 Agent 实例，互不干扰。
    """

    def __init__(
        self,
        pool: SessionAgentPool,
        session_manager: SessionManager,
        broadcast: MessageBroadcast,
        router: MessageRouter,
        hitl_mgr: HITLManager,
        interaction_mgr: InteractionManager,
        task_scheduler: "TaskScheduler | None" = None,
        plan_mgr: PlanModeManager | None = None,
    ) -> None:
        if pool is None:
            raise ValueError("AgentScheduler requires SessionAgentPool")

        # 基础依赖
        self._pool = pool
        self._session_manager = session_manager
        self._broadcast = broadcast
        self._router = router

        # 独立的 Manager
        self._hitl_mgr = hitl_mgr
        self._interaction_mgr = interaction_mgr
        self._plan_mgr = plan_mgr

        # TaskScheduler（handle_task_instruction 执行完回调 resolve_task_execution）
        self._task_scheduler = task_scheduler

        # reply_id 管理
        self._reply_id_mgr = ReplyIdManager()

        # Executor（不依赖 Manager，通过回调通信；从 pool acquire Agent）
        self._executor = AgentExecutor(pool, self._reply_id_mgr, broadcast)

        # 注入 Executor 到所有 Manager（用于 resume 时执行 Agent）
        self._hitl_mgr._executor = self._executor
        self._interaction_mgr._executor = self._executor
        if self._plan_mgr:
            self._plan_mgr._executor = self._executor

        # ★ Executor 暂停回调：一次注入，所有 execute() 调用自动生效。
        self._executor.set_pause_handlers(
            on_hitl=self._hitl_mgr.pause,
            on_interaction=self._interaction_mgr.pause,
            on_plan_approval=self._plan_mgr.pause if self._plan_mgr else None,
        )

    # ═══════════════════════════════════════════════════════════════════
    # 路由注册
    # ═══════════════════════════════════════════════════════════════════

    def register_route_handlers(self) -> None:
        self._router.register_route_handler(
            RouterMessageType.USER_INSTRUCTION,
            self.handle_user_instruction,
        )
        self._router.register_route_handler(
            RouterMessageType.APPROVAL_DECISION,
            self._hitl_mgr.resume,
        )
        self._router.register_route_handler(
            RouterMessageType.INTERACTION_RESPONSE,
            self._interaction_mgr.resume,
        )
        self._router.register_route_handler(
            RouterMessageType.TASK_INSTRUCTION,
            self.handle_task_instruction,
        )
        if self._plan_mgr:
            self._router.register_route_handler(
                RouterMessageType.PLAN_APPROVAL_DECISION,
                self._plan_mgr.resume,
            )
        self._router.register_route_handler(
            RouterMessageType.STOP_GENERATION,
            self.handle_stop_generation,
        )
        logger.info("AgentScheduler route handlers registered")

    # ═══════════════════════════════════════════════════════════════════
    # 启动维护
    # ═══════════════════════════════════════════════════════════════════

    async def startup_maintenance(self) -> None:
        """委托各 Manager 做各自的事。"""
        # TTL 清理
        for mgr in (self._hitl_mgr, self._interaction_mgr):
            try:
                await mgr.cleanup_expired(_RUN_STATE_TTL_SECONDS)
            except Exception as e:
                logger.warning("startup TTL cleanup failed: %s", e)

        # 恢复 pending 审批
        try:
            await self._hitl_mgr.restore_on_startup()
        except Exception as e:
            logger.warning("restore HITL failed: %s", e)

        # 恢复 pending ask_user 问卷
        try:
            await self._interaction_mgr.restore_on_startup()
        except Exception as e:
            logger.warning("restore interactions failed: %s", e)

    # ═══════════════════════════════════════════════════════════════════
    # Handler: 用户指令
    #
    # 一条用户消息进来，走以下路径之一：
    #
    #   有 HITL 锁 + 文字"同意" → hitl_mgr.handle_text_decision()
    #   有 HITL 锁 + 文字"拒绝" → hitl_mgr.handle_text_decision()
    #   有 HITL 锁 + 其他文字   → 报错「当前有待审批操作」
    #   有 Interaction 锁       → 报错「当前有未完成的问卷，请先回答问卷问题」
    #   无锁                    → 清理废弃问卷 → executor.execute()
    #
    # ⚠️ ask_user 回复不走此方法。用户提交问卷后前端发出
    #    INTERACTION_RESPONSE → router → interaction_mgr.resume()
    # ═══════════════════════════════════════════════════════════════════

    async def handle_user_instruction(self, msg: InboundMessage) -> None:
        logger.info(
            "[Scheduler] handle_user_instruction: msg_id=%s user=%s channel=%s",
            msg.msg_id, msg.user_id, msg.source_channel_id,
        )
        # ★ require 成功后才有权威 session_id；except 中仅当它存在才发错误回复——
        #   拿不到说明消息本应在 adapter 已被拦截（契约零兜底：没有就报错，不兜底回复）。
        session_id: str | None = None
        try:
            session_id = session_id_mod.require(
                msg.session_id, where="scheduler.handle_user_instruction",
            )

            # Step 1: HITL 锁（只查 HITL，不查 ask_user）
            pending_run_id = await self._hitl_mgr.is_pending(session_id)
            if pending_run_id:
                user_text = (
                    msg.content if isinstance(msg.content, str) else ""
                ).strip()
                if user_text in {
                    "同意", "是", "yes", "y", "approve", "ok", "✅",
                    "拒绝", "否", "no", "n", "reject", "❌",
                }:
                    await self._hitl_mgr.handle_text_decision(
                        session_id=session_id,
                        run_id=pending_run_id,
                        user_text=user_text,
                        source_channel_id=msg.source_channel_id,
                        user_id=msg.user_id,
                    )
                else:
                    hint = ""
                    try:
                        serialized = await self._hitl_mgr._repo.get_run_state(
                            session_id, pending_run_id,
                        )
                        if serialized:
                            rs = self._hitl_mgr._deserialize(serialized)
                            p = (rs.metadata or {}).get("pending_approval") or {}
                            tn = p.get("tool_name", "")
                            if tn:
                                hint = f"（操作: {tn}）"
                    except Exception:
                        pass
                    await self._publish_error_reply(
                        f"⏳ 当前有待审批操作{hint}，请回复「同意」批准或「拒绝」取消。",
                        msg.source_channel_id,
                        user_id=msg.user_id,
                        session_id=session_id,
                    )
                return

            # Step 2: Interaction 锁（阻塞 ask_user 问卷未完成时的新消息）
            # 如果有 pending 问卷，重新推送给前端展示，让用户回答
            pending_qa = await self._interaction_mgr.get_pending_questionnaire(session_id)
            if pending_qa:
                run_id, tool_name, questions = pending_qa
                reply_obj = self._reply_id_mgr.resume_reply(run_id)
                # ★ 出站必须带 session_id：前端按 payload.session_id 分桶，缺失会把问卷
                #   渲染到错误会话（多会话下常态触发）。
                rs_ev = NormalizedEvent.reply_start(
                    reply_id=reply_obj.value,
                    run_id=run_id,
                    reply_scope=reply_obj.scope.value,
                )
                rs_ev.payload["session_id"] = session_id
                await self._broadcast.send(
                    rs_ev, origin_channel_id=msg.source_channel_id,
                )
                ir_ev = NormalizedEvent.interaction_request(
                    request_id=f"re-{run_id}",
                    questions=questions,
                    tool_name=tool_name,
                    reply_id=reply_obj.value,
                    run_id=run_id,
                )
                ir_ev.payload["session_id"] = session_id
                await self._broadcast.send(
                    ir_ev, origin_channel_id=msg.source_channel_id,
                )
                return

            # Step 3: 清理废弃的 interaction RunState（过期残留）
            try:
                await self._interaction_mgr.abandon_pending(session_id)
            except Exception:
                pass

            # Step 4: session 保活
            await self._ensure_session(session_id, msg.user_id)

            # Step 5: 解析用户输入
            user_text = (
                msg.content if isinstance(msg.content, str)
                else str(msg.content or "")
            )
            if isinstance(msg.content, dict):
                user_text = (
                    msg.content.get("text")
                    or msg.content.get("raw", {}).get("content", "")
                )

            # Step 6: 回显
            await self._broadcast.send(
                NormalizedEvent.user_input_echo(
                    user_id=msg.user_id,
                    content=user_text,
                    session_id=session_id,
                ),
                origin_channel_id=msg.source_channel_id,
            )

            # Step 7: 执行 Agent
            # ★ 提取 active_app_id，透传到 Agent metadata
            active_app_id: str = ""
            mode: str | None = None
            model_id: str | None = None
            target_channel_ids: tuple[str, ...] | None = None
            if isinstance(msg.content, dict):
                active_app_id = str(msg.content.get("active_app_id", ""))
                # ★ prompt 模式（coding/office）：透传给 Pool 做 delta-rebind。
                #   None（缺省/非桌面渠道）→ Pool 保持该 session 当前绑定（新 session = default_mode）。
                mode = msg.content.get("mode") or None
                # ★ 模型选择（逐条消息）：透传给 executor → run_stream(settings=target_model)。
                #   None（缺省/非法）→ 走 default 模型（executor 内再做白名单校验）。
                model_id = msg.content.get("model_id") or None
                # ★ R0 指名渠道（可选）：入站显式指定额外投递渠道。
                #   None（缺省）→ 走渠道策略分发；非法类型 → warning 留痕 + 忽略（不静默）。
                raw_targets = msg.content.get("target_channel_ids")
                if isinstance(raw_targets, (list, tuple)):
                    target_channel_ids = tuple(
                        str(t) for t in raw_targets if isinstance(t, str) and t
                    ) or None
                elif raw_targets is not None:
                    logger.warning(
                        "[Scheduler] target_channel_ids invalid type=%s, ignored",
                        type(raw_targets).__name__,
                    )
            run_id = f"r-{uuid.uuid4().hex[:8]}"
            # ★ 长任务丢后台：Agent run 可能跑几分钟，绝不能内联 await 卡住 stdin 读取循环
            #   （否则同期的 SESSION_HISTORY_REQUEST / 会话切换 / STOP 全部读不出来）。
            #   并发/串行交给 SessionAgentPool（per-session 锁 + semaphore）。
            spawn_background(
                self._executor.execute(
                    run_id=run_id,
                    session_id=session_id,
                    user_id=msg.user_id,
                    user_input=user_text,
                    source_channel_id=msg.source_channel_id,
                    active_app_id=active_app_id,
                    mode=mode,
                    model_id=model_id,
                    target_channel_ids=target_channel_ids,
                ),
                label=f"user_instruction:{run_id}",
            )

        except Exception as e:
            logger.exception("handle_user_instruction failed: %s", e)
            # ★ 仅当 require() 已成功才发错误回复（消费点零兜底，删除 `or ""`）；
            #   session_id 拿不到说明消息本应在 adapter 已被拦截，此处只 log 留痕。
            if session_id is not None:
                await self._publish_error_reply(
                    f"[Scheduler Error] {e}",
                    msg.source_channel_id,
                    user_id=msg.user_id,
                    session_id=session_id,
                )

    # ═══════════════════════════════════════════════════════════════════
    # Handler: 停止生成
    # ═══════════════════════════════════════════════════════════════════

    async def handle_stop_generation(self, msg: InboundMessage) -> None:
        """取消当前 Session 正在执行的 Agent（协作式取消）。

        ★ 多 Session 并发：只 cancel 目标 session，不影响其他 session。
        Pool.cancel_session 会：
          - running Agent → agent.cancel()（AgentLoop 循环头检查退出）
          - 排队中的 acquire Task → task.cancel()（触发 CancelledError）
          - 两者都无 → no-op
        """
        logger.info(
            "[Scheduler] handle_stop_generation: msg_id=%s user=%s channel=%s session=%s",
            msg.msg_id, msg.user_id, msg.source_channel_id, msg.session_id,
        )
        try:
            session_id = msg.session_id or ""
            if not session_id:
                logger.warning(
                    "[Scheduler] stop_generation without session_id, ignore"
                )
                return
            # ★ 归属校验：只允许该 session 的归属用户停止它，防止用错/伪造 session_id
            #   停掉别的会话正在跑的长任务（跨会话误杀）。
            cancelled = await self._pool.cancel_session(
                session_id, expected_user_id=msg.user_id or "",
            )
            logger.info(
                "[Scheduler] cancel signal to session=%s (cancelled=%s)",
                session_id, cancelled,
            )
        except Exception as e:
            logger.exception("handle_stop_generation failed: %s", e)

    # ═══════════════════════════════════════════════════════════════════
    # Handler: 定时任务指令
    # ═══════════════════════════════════════════════════════════════════

    async def handle_task_instruction(self, msg: InboundMessage) -> None:
        """定时任务指令（无暂停回调）。

        定时任务是用户级别的，不绑定具体 session。
        若 msg 未携带 session_id 或原 session 已销毁，则自动创建新 session。

        执行完成后回调 TaskScheduler.resolve_task_execution 释放 Future，
        让 TaskScheduler 继续执行通知分发。
        """
        content = msg.content if isinstance(msg.content, dict) else {}
        run_id = content.get("run_id", f"task-{uuid.uuid4().hex[:8]}")
        execution_id = content.get("execution_id", "")
        task_id = content.get("task_id", "")

        # ★ 定时任务在【独立隔离会话】里执行，绝不复用「创建它的 owning session」：
        #   否则若用户此刻正打开该 session，任务会与用户共享同一个池内 Agent 实例 +
        #   Memory，任务那一轮对话会污染用户实时会话的 raw_log 与上下文。
        #   会话 id 经由命根子模块创建（勿在此散落 uuid，见 session_id.py / CLAUDE.md 契约）。
        session_id = session_id_mod.new_task(task_id, execution_id)

        # ★ P1-2：注入端用的键是 "task_input"（见 task_scheduler._inject_task_instruction），
        #   此前这里读 "instruction" → 恒空 → 定时任务收到空指令、整段执行形同虚设。
        #   以 task_input 为准，保留 instruction 作兼容回退。
        user_input = content.get("task_input") or content.get("instruction", "")
        source_channel_id = content.get(
            "source_channel_id",
            msg.source_channel_id or LOCAL_SCHEDULER_CHANNEL_ID,
        )

        # ★ 空指令 fail-fast：task_input/instruction 两键都空 → 定时任务会带空指令空跑一整轮
        #   （浪费一次 LLM 调用、产出无意义回复）。这不是可降级项——空指令没有「default」可言，
        #   直接跳过不执行 + warning 留痕，并把该次执行标记 skipped 释放 Future（否则上游等超时）。
        #   （静默降级审计 #11 / §1.1：宁可跳过并留痕，绝不静默空跑。）
        if not (user_input and user_input.strip()):
            logger.warning(
                "handle_task_instruction: 空指令 task_id=%s execution_id=%s（task_input/instruction 均空）"
                " → 跳过本次执行，不空跑 LLM（见静默降级审计 #11）。",
                task_id, execution_id,
            )
            if execution_id and self._task_scheduler is not None:
                try:
                    await self._task_scheduler.resolve_task_execution(
                        execution_id,
                        {"output": "", "status": "skipped", "error": "empty_task_input"},
                    )
                except Exception:
                    logger.warning(
                        "handle_task_instruction: 空指令跳过后 resolve_task_execution 失败 "
                        "execution_id=%s", execution_id, exc_info=True,
                    )
            return

        # ★ 长任务丢后台：Agent run + resolve 回调一并放进后台协程，避免内联 await 卡住
        #   触发它的循环（定时器循环）、也避免被路由层 600s wait_for 误杀。
        #   resolve_task_execution 必须在 Agent 真正跑完之后回调，所以它留在协程内 execute 之后。
        async def _run_task() -> None:
            try:
                await self._ensure_session(session_id, msg.user_id)
                await self._executor.execute(
                    run_id=run_id,
                    session_id=session_id,
                    user_id=msg.user_id,
                    user_input=user_input,
                    source_channel_id=source_channel_id,
                    # 无暂停回调
                )
                # ★ 对称路由：执行完成后回调 TaskScheduler，释放 _execute_task 中的 Future
                if execution_id and self._task_scheduler is not None:
                    try:
                        await self._task_scheduler.resolve_task_execution(
                            execution_id,
                            {"output": user_input, "status": "completed"},
                        )
                    except Exception as cb_err:
                        logger.warning(
                            "handle_task_instruction: resolve_task_execution failed "
                            "execution_id=%s: %s", execution_id, cb_err
                        )
            except Exception as e:
                logger.exception("handle_task_instruction failed: %s", e)
                # 即使执行失败，也要释放 Future 避免超时等待
                if execution_id and self._task_scheduler is not None:
                    try:
                        await self._task_scheduler.resolve_task_execution(
                            execution_id,
                            {"output": "", "status": "error", "error": str(e)},
                        )
                    except Exception:
                        pass

        spawn_background(_run_task(), label=f"task_instruction:{run_id}")

    # ═══════════════════════════════════════════════════════════════════
    # 内部工具
    # ═══════════════════════════════════════════════════════════════════

    async def _ensure_session(self, session_id: str, user_id: str) -> None:
        # ★ 直接显式调用，不再用 hasattr 鸭子探测——此前探测的是 "ensure_session"/
        #   "get_or_create"，而真实方法名曾是 get_or_create_session，两者都不命中，
        #   导致本方法长期静默 no-op（会话记录没被确保）。现方法名已统一为 ensure_session。
        #   ensure_session 是幂等且非破坏性的：已存在→原样返回；不存在→建 is_empty 记录。
        try:
            await self._session_manager.ensure_session(session_id, user_id)
        except Exception as e:
            logger.warning("ensure_session failed: session=%s: %s", session_id, e)

    async def _publish_error_reply(
        self,
        error_text: str,
        source_channel_id: str,
        session_id: str,
        user_id: str = "",
    ) -> None:
        # ★ session_id 必填（契约零兜底，消费点无默认值）；调用方拿不到 session_id
        #   时不得调用本函数发兜底回复（见 handle_user_instruction except 块）。
        rid = self._reply_id_mgr.system_reply(ReplyScope.ERROR)
        ev = NormalizedEvent.agent_reply(
            content=error_text,
            session_id=session_id,  # 前端按 session_id 分桶，勿留空
            reply_id=rid.value,
            run_id=rid.value,
        )
        # ★ 出站 stamp user_id：远程渠道（wecom fallback handler）据此路由回包
        ev.payload["user_id"] = user_id
        await self._broadcast.send(ev, origin_channel_id=source_channel_id)
