"""AgentExecutor — Agent 流式执行引擎。

职责：调用 agent.run_stream()，消费 StreamEvent，转 NormalizedEvent，
广播到 Transport。遇到暂停事件时通过回调通知外层 Manager。

不 import HITLManager / InteractionManager，避免循环依赖。

★ 多 Session 并发改造：Executor 不再持有 Agent 实例，而是通过
  SessionAgentPool.acquire(session_id, user_id) 拿到该 session 专属 Agent。
  全局 _execute_lock 已删除——并发控制交给 Pool 的 semaphore 处理。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.events.normalized import EventType, NormalizedEvent
from pandapal.scheduler.agent_pool import SessionAgentPool
from pandapal.scheduler.reply_manager import ReplyIdManager
from pandapal.scheduler.stream_to_normalized import (
    convert_stream_event_to_normalized,
)
from pandaren.engine.models import RunState

logger = logging.getLogger(__name__)

# ── 暂停回调类型 ────────────────────────────────────────────────────────────────

HitlPauseHandler = Callable[
    [Any, str, str, str, str, str],  # (stream_event, run_id, session_id, user_id, channel, active_app_id)
    Awaitable[None],
]

InteractionPauseHandler = Callable[
    [Any, str, str, str],  # (stream_event, run_id, session_id, active_app_id)
    Awaitable[None],
]

PlanApprovalHandler = Callable[
    [Any, str, str, str, str],  # (stream_event, run_id, session_id, active_app_id, model_id)
    Awaitable[None],
]

# run 结束回调（P2 实时刷新）：memory flush 后触发，参数 (session_id, user_id)
RunFinishedHandler = Callable[[str, str], Awaitable[None]]


# ── AgentExecutor ───────────────────────────────────────────────────────────────

class AgentExecutor:
    """Agent 流式执行引擎（多 Session 并发版）。

    用法::

        executor = AgentExecutor(pool, reply_id_mgr, broadcast)
        executor.set_pause_handlers(
            on_hitl=hitl_mgr.pause,
            on_interaction=interaction_mgr.pause,
            on_plan_approval=plan_mgr.pause,
        )
        await executor.execute(
            run_id=..., session_id=..., user_id=..., user_input=...,
            source_channel_id=...,
        )

    ★ 并发语义：多个 execute() 调用可以并发执行，只要 session_id 不同。
      Pool 内部：
        - 同 session 连发 → per-session Lock 保证顺序（G3）
        - 不同 session → semaphore 控制上限，超过则排队（G1/G4）

    ★ 会话列表钩子（v003）：
        - 首消息前：session_list_mgr.on_first_message(session_id, user_input)
        - 回复结束后：session_list_mgr.touch_activity(session_id, delta=2)
      钩子失败不阻塞生成（best-effort）。
    """

    def __init__(
        self,
        pool: SessionAgentPool,
        reply_id_mgr: ReplyIdManager,
        broadcast: MessageBroadcast,
        session_list_mgr: Any = None,
    ) -> None:
        if pool is None:
            raise ValueError("AgentExecutor requires SessionAgentPool")
        self._pool = pool
        self._reply_id_mgr = reply_id_mgr
        self._broadcast = broadcast
        # ★ 会话列表元数据管理器（可选注入；None 时 first_message/activity 钩子空跑）
        self._session_list_mgr = session_list_mgr
        # ★ 暂停回调由 set_pause_handlers() 注入一次，execute() 直接读实例属性。
        #   所有调用方（Scheduler / HITLManager / InteractionManager / PlanModeManager）
        #   统一生效，不再需要各自传参。
        self._on_hitl: HitlPauseHandler | None = None
        self._on_interaction: InteractionPauseHandler | None = None
        self._on_plan_approval: PlanApprovalHandler | None = None
        # ★ run 结束回调（P2 实时刷新）：flush 落盘后触发看板重推。可选注入。
        self._on_run_finished: RunFinishedHandler | None = None
        # ★ 可用模型清单（AvailableModel 列表）：供 _execute_impl 校验入站 model_id。
        #   由 app 启动后 set_available_models 注入（与 MODEL_LIST 下发同源）。
        self._available_models: list = []

    def set_session_list_manager(self, mgr: Any) -> None:
        """启动后注入 SessionListManager（避免循环依赖）。"""
        self._session_list_mgr = mgr

    def set_run_finished_handler(self, cb: RunFinishedHandler | None) -> None:
        """注入 run 结束回调（P2 实时刷新）。flush 落盘后触发，用于看板重推。"""
        self._on_run_finished = cb

    def set_available_models(self, models: list) -> None:
        """注入可用模型清单（供 _execute_impl 校验入站 model_id 是否已配置）。"""
        self._available_models = models or []

    def set_pause_handlers(
        self,
        *,
        on_hitl: HitlPauseHandler | None = None,
        on_interaction: InteractionPauseHandler | None = None,
        on_plan_approval: PlanApprovalHandler | None = None,
    ) -> None:
        """注入暂停回调（AgentScheduler 启动时调用一次）。"""
        self._on_hitl = on_hitl
        self._on_interaction = on_interaction
        self._on_plan_approval = on_plan_approval

    def _stamp_run_usage(
        self, ev: NormalizedEvent, sdk_run_id: str, duration_ms: float
    ) -> None:
        """给 REPLY_END 事件补本 run 的完整用量+费用汇总（payload["usage"]）。

        真相源是应用层 CostBudgetGuard（pool.cost_source）——它按 **SDK 内部 run_id** 每步
        累加用量与净费用。注意：这里必须用 `sdk_run_id`（= StreamEvent.run_id），不是 executor
        自己的 `r-xxxx` run_id——二者不同，用错键会查不到（历史 bug：费用永远为空）。
        前端在回复末尾直接展示 usage 各字段，绝不重算。
        Fail-Safe：无 guard / 无 summary / 异常 → 不带 usage 字段（前端降级不显示）。
        """
        try:
            if not isinstance(ev.payload, dict):
                return
            if not sdk_run_id:
                # D4：记账键（SDK 内部 run_id）缺失 → 查不到用量，usage 不带（前端降级不显示）。
                # 但不静默——这是"有时 footer 有、有时没有"（footer 静默消失）的根因，必须留痕可排查。
                logger.warning(
                    "[Executor] footer usage dropped: empty sdk_run_id at run end "
                    "(记账键缺失，footer 将降级不显示，见 D4) run_id=%s",
                    getattr(ev, "run_id", "?"),
                )
                return
            guard = self._pool.cost_source
            summarize = getattr(guard, "summary", None)
            if not callable(summarize):
                return
            summary = summarize(sdk_run_id)
            if summary is None:  # 本 run 无 LLM 调用（如纯工具/直接暂停）→ 不展示
                return
            usage = summary.to_dict()  # type: ignore[attr-defined]
            usage["duration_ms"] = round(duration_ms, 1)
            ev.payload["usage"] = usage
        except Exception:
            logger.exception("[Executor] stamp run usage failed sdk_run_id=%s", sdk_run_id)

    @staticmethod
    def _persist_resume_model(se: Any, run_metadata: dict) -> None:
        """暂停前把本 run 的 model_id 写入待序列化 RunState 的 metadata（供 resume 按同一模型续跑）。

        HITL/ask_user/Plan 暂停时，pause 处理器序列化 ``se.data['run_state']``；恢复时
        interaction/hitl manager 从 ``RunState.metadata['model_id']`` 取回所选模型。
        此前 executor 只把 active_app_id 传到 pause 处理器写入，**model_id 漏了**——
        run_metadata 里明明有 model_id，却没落进 RunState.metadata（意图见构造 run_metadata 处
        注释，但链路没接通）。结果：resume 时 model_id=None → 回落默认模型/默认 provider
        （逐条选 deepseek 的会话，答完 ask_user 续跑却切回默认 dashscope）。

        与 storage_mode 无关（markdown/sqlite 都走同一 pause 序列化，且 RunState 经 pickle
        完整 round-trip）——纯粹是暂停持久化链路缺了这一笔。此处一次性补齐。
        """
        model_id = run_metadata.get("model_id")
        if not model_id:
            # run 启动已保证具体 model_id（见 run_metadata 构造：target_model or agent.model_name）；
            # 走到这里说明连 Agent 模型名都取不到，executor 构造处已 error 留痕，此处无可写入的身份。
            return
        data = getattr(se, "data", None)
        if not isinstance(data, dict):
            return  # 非暂停事件：无 run_state 需持久化，正常 no-op
        run_state = data.get("run_state")
        if run_state is None:
            return  # 非暂停事件：同上
        meta = getattr(run_state, "metadata", None)
        if isinstance(meta, dict):
            meta["model_id"] = model_id
        else:
            # 正在暂停持久化 run_state，却无 dict metadata：无法写入 model_id →
            # resume 侧会因缺具体 model_id 而 fail-fast 拒绝恢复。这是「本该能续跑却丢了模型身份」
            # 的 bug，绝不静默（ID 类零 default，见静默降级审计 #10）——error 留痕暴露根因。
            logger.error(
                "[Executor] _persist_resume_model: 暂停 run_state.metadata 非 dict(=%r)，"
                "model_id=%s 无法持久化 → resume 将报错拒绝恢复（见静默降级审计 #10）。",
                meta, model_id,
            )

    def _budget_ledger(self) -> Any:
        """取按 provider 分账的预算账本（CostBudgetGuard.ledger）；未注入/不支持→None。

        账本供本执行器：run 启动 seed、拿到 SDK run_id 后 register_run（归属 user，供
        guard 每步 record_step 按 (user,provider) 分账）、run 结束 flush 落盘 + unregister。
        """
        guard = self._pool.cost_source
        return getattr(guard, "ledger", None)

    def _precheck_budget_halt(
        self, agent: Any, user_id: str, ledger: Any, provider_override: str = "",
    ) -> str | None:
        """新 run 启动前的预算前置拦截判据（AC-08）。返回停机原因文案，或 None=放行。

        provider 优先取 provider_override（本条消息所选模型的 provider，逐条消息可换厂商）；
        未选模型时回落 agent.provider（转发底层 LLM 客户端的 provider 事实，非由 model 名反推）。
        Fail-Safe（O3/E4）：拿不到 provider / 判据异常 → None（不拦截），绝不因限额逻辑
        误停 run；真正耗尽由 record_step 每步判据兜底。
        """
        try:
            provider = provider_override or getattr(agent, "provider", "") or ""
            if not provider:
                return None  # provider 不可得 → 无法按 provider 分账拦截，放行
            if not ledger.is_exhausted(user_id, provider):
                return None
            spent = ledger.spent(user_id, provider)
            return (
                f"{provider} 预算耗尽已暂停（累计已用 ${spent:.4f}）。"
                f"请上调 {provider} 额度后继续。"
            )
        except Exception:  # noqa: BLE001 — Fail-Safe：判据异常一律放行，不炸断 run
            logger.exception("[Executor] budget precheck failed user=%s", user_id)
            return None

    async def _emit_budget_halt(
        self,
        *,
        reply_id: str,
        run_id: str,
        session_id: str,
        reason: str,
        source_channel_id: str,
    ) -> None:
        """前置拦截：广播预算专属停机（AGENT_HALTED + REPLY_END halted），不调用 LLM。

        payload 带 `halt_kind="budget_exhausted"`，供前端区分于普通失败/暂停，
        渲染「{provider} 预算耗尽已暂停」而非泛化停止。
        """
        halt_ev = NormalizedEvent.agent_halted(
            reason=reason, halt_kind="budget_exhausted",
            reply_id=reply_id, run_id=run_id,
        )
        if isinstance(halt_ev.payload, dict):
            halt_ev.payload["session_id"] = session_id
        await self._broadcast.send(halt_ev, origin_channel_id=source_channel_id)
        end_ev = NormalizedEvent.reply_end(
            reply_id=reply_id, output=reason, status="halted", run_id=run_id,
        )
        if isinstance(end_ev.payload, dict):
            end_ev.payload["session_id"] = session_id
            end_ev.payload["halt_kind"] = "budget_exhausted"
        await self._broadcast.send(end_ev, origin_channel_id=source_channel_id)
        logger.info("[Executor] budget precheck halt run=%s: %s", run_id, reason)

    # ── 公开入口 ───────────────────────────────────────────────────────────────

    async def execute(
        self,
        *,
        run_id: str,
        session_id: str,
        user_id: str,
        user_input: str,
        source_channel_id: str,
        active_app_id: str = "",
        mode: str | None = None,
        model_id: str | None = None,
        resume_state: RunState | None = None,
        hitl_decision: str | None = None,
        interaction_response: str | None = None,
        plan_action: str | None = None,
        edited_plan_content: str | None = None,
        target_channel_ids: tuple[str, ...] | None = None,
    ) -> bool:
        """执行一次 Agent run（流式，多 Session 并发）。

        暂停回调通过 set_pause_handlers() 注入，无需每次传参。
        并发语义由 Pool 保障：
          - 不同 session 并发执行（Pool.semaphore 控制上限）
          - 同 session 顺序执行（Pool per-session Lock）
          - Pool 忙时进入排队（前端会收到 SESSION_CONCURRENCY.queued）

        target_channel_ids: 入站消息显式指名的额外目标渠道（R0 指名即达）。
          有指名时 run 期间所有事件发给 {source} ∪ targets；无指名走渠道策略。

        Returns:
            True   → 正常结束（未暂停）
            False  → 因 HITL / Interaction / Plan Approval 暂停返回
        """
        async with self._pool.acquire(session_id, user_id, mode) as agent:
            # ★ 会话列表钩子：首消息命名（仅正常路径，resume 时跳过）
            if (
                resume_state is None
                and self._session_list_mgr is not None
                and user_input
            ):
                try:
                    await self._session_list_mgr.on_first_message(
                        session_id, user_input,
                    )
                except Exception:
                    logger.exception(
                        "[Executor] on_first_message hook failed session=%s",
                        session_id,
                    )
            result = await self._execute_impl(
                agent=agent,
                run_id=run_id,
                session_id=session_id,
                user_id=user_id,
                user_input=user_input,
                source_channel_id=source_channel_id,
                active_app_id=active_app_id,
                model_id=model_id,
                resume_state=resume_state,
                hitl_decision=hitl_decision,
                interaction_response=interaction_response,
                plan_action=plan_action,
                edited_plan_content=edited_plan_content,
                target_channel_ids=target_channel_ids,
            )
            # ★ 会话列表钩子：活跃时间更新（仅正常完成，暂停时不刷新——用户还在等审批）
            if result and self._session_list_mgr is not None:
                try:
                    # message_delta=2: 一条用户消息 + 一条 Agent 回复
                    # resume 路径 user_input 为空 → delta=1（只加 Agent 回复）
                    delta = 2 if user_input else 1
                    await self._session_list_mgr.touch_activity(
                        session_id, message_delta=delta,
                    )
                except Exception:
                    logger.exception(
                        "[Executor] touch_activity hook failed session=%s",
                        session_id,
                    )
            return result

    async def _execute_impl(
        self,
        *,
        agent: Any,
        run_id: str,
        session_id: str,
        user_id: str,
        user_input: str,
        source_channel_id: str,
        active_app_id: str = "",
        model_id: str | None = None,
        resume_state: RunState | None = None,
        hitl_decision: str | None = None,
        interaction_response: str | None = None,
        plan_action: str | None = None,
        edited_plan_content: str | None = None,
        target_channel_ids: tuple[str, ...] | None = None,
    ) -> bool:
        # 1. reply_id
        if resume_state is not None:
            reply_obj = self._reply_id_mgr.resume_reply(run_id)
        else:
            reply_obj = self._reply_id_mgr.new_run_reply(run_id)
        reply_id = reply_obj.value

        # ★ R0 参与者集合：入站显式指名时，run 期间所有事件发给 {源渠道} ∪ 指名集合
        #   （源渠道并入保证 R2 回复恒达会话主）；无指名 → None，走渠道策略分发。
        participants: tuple[str, ...] | None = None
        if target_channel_ids:
            participants = tuple(
                dict.fromkeys((source_channel_id, *target_channel_ids))
            )

        stream = None  # 提前声明，确保 finally 块可访问
        # 本轮耗时基线 + SDK 内部 run_id（记账键）：提前声明，异常收尾路径也可用。
        run_start_mono = time.monotonic()
        sdk_run_id = ""
        # 预算账本（按 provider 分账）：提前取，finally 落盘/清理也可用。
        ledger = self._budget_ledger()
        if ledger is not None:
            # 幂等 seed：首个 run 从持久层载入各 provider 的额度+已花费（跨会话/重启累计）。
            await ledger.seed_from_store()
        try:
            # 2. REPLY_START
            reply_start_ev = NormalizedEvent.reply_start(
                reply_id=reply_id,
                run_id=run_id,
                reply_scope=reply_obj.scope.value,
            )
            reply_start_ev.payload["session_id"] = session_id
            await self._broadcast.send(
                reply_start_ev,
                origin_channel_id=source_channel_id,
                target_channel_ids=participants,
            )

            # 2a. 模型选择（逐条消息）：校验 model_id 白名单 → 定 target_model 与其 provider。
            #     非法/未声明 → warning 留痕 + 回落 default（target_model=None），绝不静默（零降级）。
            #     target_model 为 None 时 run_settings=None，run_core 走 default 模型（无回归）。
            from pandapal.config.llm import model_registry
            from pandaren.llm.types import ModelSettings

            target_model: str | None = None
            chosen_provider: str = ""
            if model_id:
                decl = model_registry.find_available(model_id, self._available_models)
                if decl is not None:
                    target_model = model_id
                    chosen_provider = decl.provider
                else:
                    logger.warning(
                        "[Executor] 非法/未配置 model_id=%r (run=%s user=%s) → 回落 default 模型",
                        model_id, run_id, user_id,
                    )
            run_settings = ModelSettings(target_model=target_model) if target_model else None

            # 2b. 预算前置拦截（AC-08）：新 run（非 resume）启动前，若其 provider 已达额度，
            #     直接不调用 LLM——省得白跑一步再被 record_step 停。resume 是续跑既有 run，
            #     不在此拦（其耗尽由每步 record_step 判据兜住）。Fail-Safe：provider/账本判据
            #     不可得一律放行（O3），绝不因限额逻辑误停。
            #     provider 取所选模型的 provider（逐条消息可换厂商）；未选模型 → precheck 内回落 agent.provider。
            if resume_state is None and ledger is not None:
                halt_reason = self._precheck_budget_halt(
                    agent, user_id, ledger, provider_override=chosen_provider,
                )
                if halt_reason:
                    await self._emit_budget_halt(
                        reply_id=reply_id, run_id=run_id, session_id=session_id,
                        reason=halt_reason, source_channel_id=source_channel_id,
                    )
                    return True  # 已「完成」（被前置拦截，未消耗 LLM）——非暂停

            # 3. 调用 Agent
            run_metadata = {"user_id": user_id}
            if active_app_id:
                run_metadata["active_app_id"] = active_app_id
            # ★ ID 类零 default（静默降级审计 §1.1 原则一 / #1/#6/#8）：run 的 model_id 是该 run 的
            #   身份，必须是**具体值**，且端到端必有——绝不允许下游 resume 时为空后 `or 默认`。
            #   解析优先级：用户逐条所选(target_model) > Agent 自身的具体模型名(agent.model_name)。
            #   后者不是「默认兜底」，而是「本 run 没有人工选模型时，实际所用模型的真实名字」——
            #   headless 渠道（定时任务/企微/音箱，无模型选择器）走这条，得到具体模型身份而非空。
            #   由此建立不变量：**每个 run 的 run_metadata 必有具体 model_id** → 暂停持久化必带 →
            #   resume 取不到即「不变量被破坏 = bug」→ fail-fast（见各 resume 处），而非静默切模型。
            concrete_model_id = target_model or (getattr(agent, "model_name", "") or "")
            if concrete_model_id:
                run_metadata["model_id"] = concrete_model_id
            else:
                # 连 Agent 自身模型名都取不到（纯透传客户端等）：无法确立模型身份。留痕，
                # resume 侧会因缺 model_id 而 fail-fast（宁可报错，绝不静默回落默认 provider）。
                logger.error(
                    "[Executor] 无法解析具体 model_id（target=%r agent.model_name 空）run=%s user=%s"
                    " → 该 run 暂停后将无法 resume（ID 类零 default，见静默降级审计 #1）。",
                    target_model, run_id, user_id,
                )
            if resume_state is not None:
                stream = agent.run_stream(
                    task=user_input,
                    session_id=session_id,
                    resume_state=resume_state,
                    hitl_decision=hitl_decision,
                    interaction_response=interaction_response,
                    plan_action=plan_action,
                    edited_plan_content=edited_plan_content,
                    metadata=run_metadata,
                    settings=run_settings,
                )
            else:
                stream = agent.run_stream(
                    task=user_input,
                    session_id=session_id,
                    plan_action=plan_action,
                    edited_plan_content=edited_plan_content,
                    metadata=run_metadata,
                    settings=run_settings,
                )

            # 4. 消费流事件
            # sdk_run_id 是 SDK 内部 run_id（= StreamEvent.run_id，与 executor 的 r-xxxx
            # 不同），用于向 guard 查记账；run_start_mono 已在 try 外设为本轮耗时基线。
            async for se in stream:
                if getattr(se, "run_id", ""):
                    # 首次拿到 SDK run_id（run_started 先于第一步 should_halt）：登记归属，
                    # 供 guard 每步 record_step 按 (user,provider) 分账。resume 会再次登记（幂等）。
                    if not sdk_run_id and ledger is not None:
                        ledger.register_run(se.run_id, user_id)
                    sdk_run_id = se.run_id
                is_hitl = getattr(se.type, "value", None) == "hitl_requested"

                # ★ 暂停前补齐 RunState.metadata['model_id']（若本 run 选了非默认模型），
                #   使 HITL/ask_user/Plan resume 能按同一模型/厂商续跑（否则回落默认 provider，
                #   引发「deepseek 会话答完 ask_user 却切回 dashscope」这类跨轮换模型问题）。
                #   对非暂停事件 se.data 无 run_state → no-op。
                self._persist_resume_model(se, run_metadata)

                # 4b. HITL 暂停 — 先保存审批记录，再广播（防止"弹窗已出但记录未存"）
                if is_hitl and self._on_hitl:
                    await self._on_hitl(
                        se, run_id, session_id, user_id, source_channel_id,
                        run_metadata.get("active_app_id", ""),
                    )

                # 4a. 转换 + 广播
                for ev in convert_stream_event_to_normalized(
                    event_type=(
                        se.type.value
                        if hasattr(se.type, "value")
                        else str(se.type)
                    ),
                    data=se.data,
                    run_id=run_id,
                    reply_id=reply_id,
                    tool_name=se.tool_name,
                ):
                    # ★ v003：给 payload 补 session_id（用于前端 per-session 分发）
                    if isinstance(ev.payload, dict) and not ev.payload.get("session_id"):
                        ev.payload["session_id"] = session_id
                    # ★ 出站 stamp user_id（远程渠道回包路由依据，如 wecom fallback handler）：
                    #   只补不覆盖——engine 已 stamp 的事件（如 HITL）保持权威。
                    if isinstance(ev.payload, dict) and not ev.payload.get("user_id"):
                        ev.payload["user_id"] = user_id
                    # ★ 会话末尾消耗：REPLY_END 带上本 run 完整用量+费用汇总（应用层 guard
                    #   精算，前端直接展示不重算）。用 sdk_run_id 查记账，见 _stamp_run_usage。
                    if ev.event_type == EventType.REPLY_END:
                        self._stamp_run_usage(
                            ev, sdk_run_id, (time.monotonic() - run_start_mono) * 1000
                        )
                    await self._broadcast.send(
                        ev, origin_channel_id=source_channel_id,
                        target_channel_ids=participants,
                    )

                if is_hitl:
                    return False

                # 4c. Interaction 暂停
                if getattr(se.type, "value", None) == "interaction_requested":
                    logger.info(
                        "[Executor] interaction_requested: run_id=%s "
                        "tool_name=%s",
                        run_id, getattr(se, "tool_name", "?"),
                    )
                    if self._on_interaction:
                        await self._on_interaction(
                            se, run_id, session_id,
                            run_metadata.get("active_app_id", ""),
                        )
                    return False

                # 4d. Plan Approval 暂停（等待用户决策）
                if getattr(se.type, "value", None) == "plan_approval_requested":
                    logger.info(
                        "[Executor] plan_approval_requested: run_id=%s "
                        "plan_path=%s",
                        run_id, getattr(se.data, "plan_path", "?"),
                    )
                    if self._on_plan_approval:
                        # ★ 传 model_id：plan 不存 RunState（见 PlanModeManager），故模型无法像
                        #   HITL/ask_user 那样经 RunState.metadata 透传——必须显式带给 pause 登记，
                        #   否则批准后续跑回落默认 provider（deepseek 会话被切回 dashscope 撞额度）。
                        await self._on_plan_approval(
                            se, run_id, session_id,
                            run_metadata.get("active_app_id", ""),
                            run_metadata.get("model_id", ""),
                        )
                    return False

            return True  # 正常结束

        except Exception as e:
            logger.exception("AgentExecutor.execute failed")
            err_ev = NormalizedEvent.error(
                error_code="agent_run_failed",
                error_message=str(e),
                reply_id=reply_id,
                run_id=run_id,
            )
            err_ev.payload["session_id"] = session_id
            await self._broadcast.send(
                err_ev,
                origin_channel_id=source_channel_id,
                target_channel_ids=participants,
            )
            end_ev = NormalizedEvent.reply_end(
                reply_id=reply_id,
                output=str(e),
                status="error",
                run_id=run_id,
            )
            end_ev.payload["session_id"] = session_id
            # 异常收尾也带上已发生的消耗（若已有 LLM 调用记账）
            self._stamp_run_usage(end_ev, sdk_run_id, (time.monotonic() - run_start_mono) * 1000)
            await self._broadcast.send(
                end_ev,
                origin_channel_id=source_channel_id,
                target_channel_ids=participants,
            )
            return False
        finally:
            # 预算账本 on_run_end 落盘（G2）：把累计的脏账户 spent 批量落盘 + 清理本 run 归属。
            # flush 幂等、失败不影响运行（内存权威）；unregister 幂等。暂停(HITL/交互/Plan)
            # 也会经此——resume 时会重新 seed(幂等)+register，语义安全。
            if ledger is not None and sdk_run_id:
                try:
                    await ledger.flush()
                except Exception:
                    logger.exception("[Executor] budget flush failed sdk_run_id=%s", sdk_run_id)
                ledger.unregister_run(sdk_run_id)
            # ★ 确保 stream generator 被正确关闭，触发 _run_stream_core 的
            #    finally 块（on_run_end + memory flush），避免 HITL/Interaction/
            #    Plan 暂停时 span 状态被下一个 run 的 on_run_start 覆盖。
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception:
                    # aclose 触发 _run_stream_core 的 finally（on_run_end + memory flush）。
                    # 静默失败 → 本轮对话可能未落盘 memory → 下一轮上下文缺失（典型「结果不对但没报错」）。
                    # 绝不 pass（静默降级审计 #9 / §1.1 原则三）——warning 留痕暴露 memory 未 flush 风险。
                    logger.warning(
                        "[Executor] stream.aclose 失败 sdk_run_id=%s：本轮 memory flush 可能未执行，"
                        "下一轮上下文或缺失（见静默降级审计 #9）。",
                        sdk_run_id, exc_info=True,
                    )
            # ★ P2 实时刷新：flush 落盘后重推看板快照（O3：绝不因看板故障影响 run）
            if self._on_run_finished is not None:
                try:
                    await self._on_run_finished(session_id, user_id)
                except Exception:
                    logger.warning("[Executor] on_run_finished (dashboard push) failed", exc_info=True)
