"""HITLManager — HITL 审批完整生命周期管理。

暂停来源：安全意识层判定（sensitivity=HIGH），非工具声明。
存储：共享 run_states 表，通过 metadata.pending_approval 隔离。
依赖：HITLBridge（审批持久化 + 广播弹窗）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.events.normalized import NormalizedEvent
from pandapal.hitl.bridge import HITLBridge
from pandapal.messages.types import HITLDecision, RouterMessageType
from pandapal.router.router import MessageRouter
from pandapal.scheduler.background import spawn_background
from pandapal.scheduler.reply_manager import ReplyIdManager, ReplyScope
from pandapal import session_id as session_id_mod

from pandapal.degradation import DegradationEvent, report_degradation

logger = logging.getLogger(__name__)

_APPROVE_KW = frozenset({"同意", "是", "yes", "y", "approve", "ok", "✅"})
_REJECT_KW = frozenset({"拒绝", "否", "no", "n", "reject", "❌"})


class HITLManager:
    """HITL 审批完整生命周期管理。"""

    def __init__(
        self,
        repo: Any,  # RunStateRepository
        bridge: HITLBridge,
        broadcast: MessageBroadcast,
        router: MessageRouter,
        reply_id_mgr: ReplyIdManager,
    ) -> None:
        self._repo = repo
        self._bridge = bridge
        self._broadcast = broadcast
        self._router = router
        self._reply_mgr = reply_id_mgr
        # 由 AgentScheduler 在构造后注入
        self._executor: Any = None  # AgentExecutor

    # ── Executor 回调 ──────────────────────────────────────────────────────────

    async def pause(
        self,
        stream_event: Any,
        run_id: str,
        session_id: str,
        user_id: str,
        source_channel_id: str,
        active_app_id: str = "",
    ) -> None:
        """Executor 检测到 hitl_requested 时回调。"""
        data = stream_event.data or {}
        run_state = data.get("run_state")
        tool_name = (
            getattr(stream_event, "tool_name", None)
            or data.get("pending_tool_name", "unknown")
        )
        pending_tool_args = data.get("pending_tool_args", {})
        tool_args_summary = (
            str(pending_tool_args)[:500] if pending_tool_args else ""
        )
        approval_id = data.get("approval_id") or f"appr-{uuid.uuid4().hex[:12]}"

        # ★ 把 active_app_id 写入 RunState.metadata（跨轮恢复时取回）
        if active_app_id and run_state is not None and hasattr(run_state, "metadata"):
            if isinstance(run_state.metadata, dict):
                run_state.metadata["active_app_id"] = active_app_id
        # 保存 RunState
        if run_state is not None:
            try:
                serialized = self._serialize(run_state, session_id)
                await self._repo.save_run_state(session_id, run_id, serialized)
            except Exception as e:
                logger.error("HITLManager.pause: save failed: %s", e)

        # 注入 APPROVAL_NEEDED → HITLBridge（持久化审批 + push 弹窗）
        from pandapal.router.models import InboundMessage as IIM

        hitl_msg = IIM(
            msg_id=str(uuid.uuid4()),
            message_type=RouterMessageType.APPROVAL_NEEDED,
            source_channel_id=source_channel_id,
            user_id=user_id,
            session_id=session_id,
            content={
                "run_id": run_id,
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_args_summary": tool_args_summary,
                "reply_id": run_id,
                "approval_id": approval_id,
            },
        )
        try:
            await self._router.inject_inbound_message(hitl_msg)
        except Exception as e:
            logger.error("HITLManager.pause: inject APPROVAL_NEEDED failed: %s", e)

    # ── Router handler ─────────────────────────────────────────────────────────

    async def resume(self, msg: Any) -> None:
        """APPROVAL_DECISION 路由的 handler。

        读 RunState → 删旧 → Executor resume → except 兜底删。
        """
        logger.info(
            "[HITLManager] resume: msg_id=%s run=%s decision=%s",
            msg.msg_id,
            msg.content.get("run_id") if isinstance(msg.content, dict) else None,
            msg.content.get("decision") if isinstance(msg.content, dict) else None,
        )
        run_id: str | None = None
        session_id: str = ""
        try:
            content = msg.content if isinstance(msg.content, dict) else {}
            run_id = content.get("run_id")
            decision = content.get("decision")
            # 两层信封（content 与消息头）由 HITLBridge 从同一 approval.session_id 写入
            # → 断言一致；不一致即污染，抛错留痕并终止（详见 SESSION_ID 契约）。
            try:
                session_id = session_id_mod.assert_consistent(
                    content.get("session_id"), msg.session_id,
                    where="hitl_manager.resume",
                )
            except session_id_mod.SessionIdError as e:
                logger.error("[HITLManager] resume: %s", e)
                return
            user_id = content.get("user_id") or msg.user_id

            if not run_id:
                logger.warning("HITLManager.resume: missing run_id")
                return

            # ★ 决策类字段 fail-fast（静默降级审计 #2 / §1.1 原则一）：decision 表达用户对 HITL
            #   请求的批准/拒绝，是「门禁类」字段——缺失/非法绝不默认放行。此前 `content.get(
            #   "decision", HITLDecision.APPROVED)` 会把「入站漏带/字段名写错」静默当成「批准」，
            #   本该等审批的暂停点被直接放行续跑。前端契约稳定携带（TS 必填枚举），故缺失=bug：
            #   立即中止 + 冒泡 error 到 UI，绝不默认 approve（参照 PlanManager.resume 同款处理）。
            if decision not in (HITLDecision.APPROVED, HITLDecision.REJECTED):
                logger.error(
                    "[HITLManager] resume: decision 非法/缺失 session=%s run=%s 实际=%r，"
                    "拒绝恢复（决策类字段绝不默认放行，见静默降级审计 #2）。",
                    session_id, run_id, decision,
                )
                report_degradation(
                    DegradationEvent.HITL_DECISION_MISSING,
                    category="decision", severity="abort", source="hitl_manager.resume",
                    expected="approved|rejected", fallback=repr(decision),
                    session_id=session_id, run_id=run_id,
                )
                err_ev = NormalizedEvent.error(
                    error_code="hitl_decision_missing",
                    error_message="审批决策丢失，未能执行。请在审批弹窗中重新点击「批准 / 拒绝」。",
                    error_detail=f"decision={decision!r} run_id={run_id}",
                    run_id=run_id,
                )
                err_ev.payload["session_id"] = session_id  # 前端按 session_id 分桶
                await self._broadcast.send(
                    err_ev, origin_channel_id=msg.source_channel_id,
                )
                return

            serialized = await self._repo.get_run_state(session_id, run_id)
            if not serialized:
                logger.warning(
                    "HITLManager.resume: RunState not found "
                    "session=%s run=%s", session_id, run_id,
                )
                report_degradation(
                    DegradationEvent.RUN_STATE_NOT_FOUND,
                    category="capability", source="hitl_manager.resume",
                    session_id=session_id, run_id=run_id,
                )
                err_ev = NormalizedEvent.error(
                    error_code="run_state_not_found",
                    error_message="该操作的运行状态已失效，请重新发送指令。",
                    error_detail=f"run_id={run_id}",
                    run_id=run_id,
                )
                err_ev.payload["session_id"] = session_id  # 前端按 session_id 分桶
                await self._broadcast.send(
                    err_ev, origin_channel_id=msg.source_channel_id,
                )
                return

            run_state = self._deserialize(serialized)

            # ★ 显式断言 RunState 归属与请求 session 一致：把「按复合键读取」的隐式兜底
            #   变成显式护栏。即便未来有人改用绕过 session 的查询（get_run_state_by_run_id），
            #   这里也能拦住跨会话恢复。
            rs_sid = getattr(run_state, "session_id", session_id)
            if rs_sid and rs_sid != session_id:
                logger.error(
                    "[HITLManager] resume: RunState session 不一致 请求=%s 实际=%s "
                    "run=%s，拒绝恢复以防跨会话执行。",
                    session_id, rs_sid, run_id,
                )
                return

            # ★ 从 RunState 取回 active_app_id + model_id（跨 HITL 轮透传，保证同模型续跑）。
            #   model_id 是 ID 类字段：**没有 default，缺失即报错**（静默降级审计 #1 / §1.1 原则一）。
            #   run 启动时 executor 已把具体 model_id（用户所选或 Agent 自身模型名）写入 RunState.metadata，
            #   建立「每个持久化 run 必带具体 model_id」的不变量。此处取不到 = 不变量被破坏 = bug：
            #   fail-fast 报错中止，绝不 `or None` 回落默认模型（那正是本次事故——静默切 provider 撞额度）。
            active_app_id = ""
            saved_meta = getattr(run_state, "metadata", None)
            if isinstance(saved_meta, dict):
                active_app_id = str(saved_meta.get("active_app_id", ""))
            model_id = saved_meta.get("model_id") if isinstance(saved_meta, dict) else None
            if not model_id:
                logger.error(
                    "[HITLManager] resume: RunState 缺具体 model_id session=%s run=%s metadata=%r"
                    " → 拒绝恢复（ID 类零 default，绝不静默回落默认模型/provider，见静默降级审计 #1）。",
                    session_id, run_id, saved_meta,
                )
                report_degradation(
                    DegradationEvent.MODEL_ID_MISSING_IN_RESUME,
                    category="id", severity="abort", source="hitl_manager.resume",
                    expected="concrete model_id in RunState.metadata", fallback=None,
                    session_id=session_id, run_id=run_id,
                )
                err_ev = NormalizedEvent.error(
                    error_code="resume_model_id_missing",
                    error_message="该操作的模型信息已丢失，无法安全恢复，请重新发送指令。",
                    error_detail=f"run_id={run_id}",
                    run_id=run_id,
                )
                err_ev.payload["session_id"] = session_id
                await self._broadcast.send(
                    err_ev, origin_channel_id=msg.source_channel_id,
                )
                return

            # 先删旧 RunState，再恢复
            await self._repo.delete_run_state(session_id, run_id)

            # ★ 审批通过后 Agent 从暂停点接着跑，同样是长任务——丢后台，别卡 stdin 读取循环。
            spawn_background(
                self._executor.execute(
                    run_id=run_id,
                    session_id=session_id,
                    user_id=user_id,
                    user_input="",
                    source_channel_id=msg.source_channel_id,
                    active_app_id=active_app_id,
                    model_id=model_id,
                    resume_state=run_state,
                    hitl_decision=decision,
                ),
                label=f"hitl_resume:{run_id}",
            )

        except Exception as e:
            logger.exception("HITLManager.resume failed: %s", e)
            if run_id and session_id:
                try:
                    await self._repo.delete_run_state(session_id, run_id)
                except Exception:
                    pass
            await self._broadcast_error(
                str(e), msg.source_channel_id, user_id or msg.user_id,
                session_id=session_id,
            )

    # ── 会话锁 ────────────────────────────────────────────────────────────────

    async def is_pending(self, session_id: str) -> str | None:
        """查询该 session 是否有待审批的 RunState（只查 HITL 类型）。

        不查 interaction 类型的 RunState，与 ask_user 互不干扰。
        """
        try:
            all_states = await self._repo.list_all_run_states()
        except Exception:
            # 审批锁查询失败 → 返回 None（fail-open：本 session 视作无待审批）。这是「门禁类」查询，
            # fail-open 意味着可能放行本该被审批门拦住的新消息——之所以不 fail-closed（返回「有锁」），
            # 是因为一次 repo 抖动就会永久 wedge 该 session。折中：**保持放行但必须 error 留痕**，
            # 绝不静默（静默降级审计 #4 / §1.1 原则一「门禁类绝不静默」）。
            logger.error(
                "[HITLManager] is_pending: list_all_run_states 失败 session=%s → 返回 None"
                "（审批锁本次查询失效，可能放行未审批消息，见静默降级审计 #4）。",
                session_id, exc_info=True,
            )
            return None
        hitl_ids: list[str] = []
        for _sid, run_id, serialized in all_states:
            if _sid != session_id:
                continue
            try:
                rs = self._deserialize(serialized)
            except Exception:
                logger.warning(
                    "[HITLManager] is_pending: RunState 反序列化失败 session=%s run=%s → 跳过该条"
                    "（可能漏判审批锁，见静默降级审计 #4/#7）。",
                    session_id, run_id, exc_info=True,
                )
                continue
            if "pending_approval" in (rs.metadata or {}):
                hitl_ids.append(run_id)
        return hitl_ids[-1] if hitl_ids else None

    # ── 文字决策 ──────────────────────────────────────────────────────────────

    async def handle_text_decision(
        self,
        session_id: str,
        run_id: str,
        user_text: str,
        source_channel_id: str,
        user_id: str = "",
    ) -> None:
        """用户文字"同意/拒绝" → 构造 APPROVAL_DECISION 注入 Router。"""
        if user_text in _APPROVE_KW:
            decision = HITLDecision.APPROVED
        elif user_text in _REJECT_KW:
            decision = HITLDecision.REJECTED
        else:
            return  # 不应该走到这里

        from pandapal.router.models import InboundMessage as IIM

        msg = IIM(
            msg_id=str(uuid.uuid4()),
            message_type=RouterMessageType.APPROVAL_DECISION,
            source_channel_id=source_channel_id,
            user_id=user_id,
            session_id=session_id,
            content={
                "run_id": run_id,
                "session_id": session_id,
                "decision": decision,
                "resume_reply_id": run_id,
                "user_id": user_id,
            },
        )
        try:
            await self._router.inject_inbound_message(msg)
        except Exception as e:
            logger.error(
                "HITLManager.handle_text_decision: inject failed: %s", e,
            )

    # ── 启动恢复 ──────────────────────────────────────────────────────────────

    async def restore_on_startup(self) -> None:
        """恢复 pending 审批（委托 HITLBridge）。"""
        await self._bridge.restore_pending_approvals()

    # ── TTL 清理 ───────────────────────────────────────────────────────────────

    async def cleanup_expired(self, ttl_seconds: int) -> None:
        """清理超过 TTL 的 RunState。"""
        try:
            await self._repo.cleanup_expired_run_states(ttl_seconds)
        except Exception as e:
            logger.warning("HITLManager TTL cleanup: %s", e)

    # ── 内部辅助 ───────────────────────────────────────────────────────────────

    def _serialize(self, run_state: Any, session_id: str) -> bytes:
        if hasattr(run_state, "to_bytes"):
            return run_state.to_bytes(session_id=session_id)
        import pickle
        return pickle.dumps({"session_id": session_id, "state": run_state})

    def _deserialize(self, data: bytes) -> Any:
        try:
            import pickle
            obj = pickle.loads(data)
            return obj["state"] if isinstance(obj, dict) else obj
        except Exception:
            pass
        from pandaren.engine.models import RunState
        if hasattr(RunState, "from_bytes"):
            return RunState.from_bytes(data)
        raise ValueError("Cannot deserialize RunState")

    async def _broadcast_error(
        self, text: str, channel: str, user_id: str, session_id: str = "",
    ) -> None:
        rid = self._reply_mgr.system_reply(ReplyScope.ERROR)
        await self._broadcast.send(
            NormalizedEvent.agent_reply(
                content=f"[HITL Error] {text}",
                session_id=session_id,  # 前端按 session_id 分桶，勿留空
                reply_id=rid.value,
                run_id=rid.value,
            ),
            origin_channel_id=channel,
        )
