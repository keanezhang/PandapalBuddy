"""InteractionManager — ask_user 交互完整生命周期管理。

暂停来源：ask_user 工具声明 requires_user_interaction。
存储：共享 run_states 表，通过 metadata.pending_interaction 隔离。
会话锁机制（与 HITL 一致）：用户发新消息时被阻塞，必须完成问卷后才能继续。
"""

from __future__ import annotations

import logging
from typing import Any

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.events.normalized import NormalizedEvent
from pandapal.scheduler.background import spawn_background
from pandapal.scheduler.reply_manager import ReplyIdManager
from pandapal.scheduler.stream_to_normalized import _extract_all_questions
from pandapal import session_id as session_id_mod

from pandapal.degradation import DegradationEvent, report_degradation

logger = logging.getLogger(__name__)


class InteractionManager:
    """ask_user 交互完整生命周期管理。"""

    def __init__(
        self,
        repo: Any,  # RunStateRepository
        broadcast: MessageBroadcast,
        reply_id_mgr: ReplyIdManager,
    ) -> None:
        self._repo = repo
        self._broadcast = broadcast
        self._reply_mgr = reply_id_mgr
        # 由 AgentScheduler 在构造后注入
        self._executor: Any = None  # AgentExecutor

    # ── Executor 回调 ──────────────────────────────────────────────────────────

    async def pause(
        self,
        stream_event: Any,
        run_id: str,
        session_id: str,
        active_app_id: str = "",
    ) -> None:
        """Executor 检测到 interaction_requested 时回调。"""
        data = stream_event.data or {}
        run_state = data.get("run_state")
        if run_state is not None:
            # ★ 把 active_app_id 写入 RunState.metadata（跨轮恢复时取回）
            if active_app_id and hasattr(run_state, "metadata"):
                if isinstance(run_state.metadata, dict):
                    run_state.metadata["active_app_id"] = active_app_id
            try:
                serialized = self._serialize(run_state, session_id)
                await self._repo.save_run_state(session_id, run_id, serialized)
            except Exception as e:
                logger.error(
                    "InteractionManager.pause: save failed: %s", e,
                )

    # ── 会话锁 ────────────────────────────────────────────────────────────────

    async def get_pending_questionnaire(
        self, session_id: str,
    ) -> tuple[str, str, list[dict]] | None:
        """查询该 session 是否有待回答的问卷，如果有则返回问卷数据。

        不查 HITL 类型的 RunState，与审批互不干扰。

        Returns:
            (run_id, tool_name, questions) 或 None。
            questions 是已解析的题目列表，可直接用于 interaction_request 事件。
        """
        try:
            all_states = await self._repo.list_all_run_states()
        except Exception:
            # 问卷锁查询失败 → 返回 None（fail-open：视作无待答问卷）。同 HITLManager.is_pending：
            # 门禁类查询，fail-open 可能放行本该被「未完成问卷」锁拦住的新消息；不 fail-closed 是避免
            # 一次 repo 抖动永久 wedge 该 session。折中：保持放行但 error 留痕（静默降级审计 #5）。
            logger.error(
                "[InteractionManager] get_pending_questionnaire: list_all_run_states 失败 session=%s"
                " → 返回 None（问卷锁本次查询失效，可能放行未完成问卷的消息，见静默降级审计 #5）。",
                session_id, exc_info=True,
            )
            return None
        for _sid, run_id, serialized in all_states:
            if _sid != session_id:
                continue
            try:
                rs = self._deserialize(serialized)
            except Exception:
                logger.warning(
                    "[InteractionManager] get_pending_questionnaire: RunState 反序列化失败 session=%s"
                    " run=%s → 跳过该条（可能漏判问卷锁，见静默降级审计 #5/#7）。",
                    session_id, run_id, exc_info=True,
                )
                continue
            raw = (rs.metadata or {}).get("pending_interaction")
            if not raw:
                continue
            tool_args = raw.get("tool_args") or {}
            questions_json = tool_args.get("questions_json")
            if not questions_json:
                continue
            questions = _extract_all_questions(questions_json)
            if not questions:
                continue
            tool_name = raw.get("tool_name", "ask_user")
            return (run_id, tool_name, questions)
        return None

    # ── Router handler ─────────────────────────────────────────────────────────

    async def resume(self, msg: Any) -> None:
        """INTERACTION_RESPONSE 路由的 handler。

        读 RunState → 删旧 → Executor resume → except 兜底删。
        """
        content = msg.content if isinstance(msg.content, dict) else {}
        run_id = content.get("run_id", "")
        response = content.get("response", "")
        # 两层信封本应同一真相 → 断言一致；不一致即污染，抛错留痕并终止。
        try:
            session_id = session_id_mod.assert_consistent(
                content.get("session_id"), msg.session_id,
                where="interaction_manager.resume",
            )
        except session_id_mod.SessionIdError as e:
            logger.error("[InteractionManager] resume: %s", e)
            return

        if not run_id:
            logger.warning("InteractionManager.resume: missing run_id")
            return

        try:
            serialized = await self._repo.get_run_state(session_id, run_id)
            if not serialized:
                logger.warning(
                    "InteractionManager.resume: RunState not found "
                    "session=%s run=%s", session_id, run_id,
                )
                report_degradation(
                    DegradationEvent.RUN_STATE_NOT_FOUND,
                    category="capability", source="interaction_manager.resume",
                    session_id=session_id, run_id=run_id,
                )
                err_ev = NormalizedEvent.error(
                    error_code="run_state_not_found",
                    error_message="该问卷的回答已超时，请重新发起对话。",
                    error_detail=f"run_id={run_id}",
                    run_id=run_id,
                )
                err_ev.payload["session_id"] = session_id  # 前端按 session_id 分桶
                await self._broadcast.send(
                    err_ev, origin_channel_id=msg.source_channel_id,
                )
                return

            run_state = self._deserialize(serialized)

            # ★ 显式断言 RunState 归属与请求 session 一致（详见 HITLManager.resume 同款护栏）：
            #   把「按复合键读取」的隐式兜底变成显式护栏，拦住任何跨会话恢复。
            rs_sid = getattr(run_state, "session_id", session_id)
            if rs_sid and rs_sid != session_id:
                logger.error(
                    "[InteractionManager] resume: RunState session 不一致 请求=%s 实际=%s "
                    "run=%s，拒绝恢复以防跨会话执行。",
                    session_id, rs_sid, run_id,
                )
                return

            # ★ 从 RunState 取回 active_app_id + model_id（跨 ask_user 轮透传，保证同模型续跑）。
            #   model_id 是 ID 类字段：**没有 default，缺失即报错**（静默降级审计 #6 / §1.1 原则一）。
            #   同 HITLManager.resume：run 启动时 executor 已把具体 model_id 写入 RunState.metadata
            #   （每个 run 必带具体模型的不变量）。此处取不到 = 不变量被破坏 = bug → fail-fast 报错中止，
            #   绝不 `or None` 回落默认模型（那正是本次事故：答完 ask_user 续跑却切回默认 dashscope）。
            active_app_id = ""
            saved_meta = getattr(run_state, "metadata", None)
            if isinstance(saved_meta, dict):
                active_app_id = str(saved_meta.get("active_app_id", ""))
            model_id = saved_meta.get("model_id") if isinstance(saved_meta, dict) else None
            if not model_id:
                logger.error(
                    "[InteractionManager] resume: RunState 缺具体 model_id session=%s run=%s metadata=%r"
                    " → 拒绝恢复（ID 类零 default，绝不静默回落默认模型/provider，见静默降级审计 #6）。",
                    session_id, run_id, saved_meta,
                )
                report_degradation(
                    DegradationEvent.MODEL_ID_MISSING_IN_RESUME,
                    category="id", severity="abort", source="interaction_manager.resume",
                    expected="concrete model_id in RunState.metadata", fallback=None,
                    session_id=session_id, run_id=run_id,
                )
                err_ev = NormalizedEvent.error(
                    error_code="resume_model_id_missing",
                    error_message="该问卷的模型信息已丢失，无法安全恢复，请重新发起对话。",
                    error_detail=f"run_id={run_id}",
                    run_id=run_id,
                )
                err_ev.payload["session_id"] = session_id
                await self._broadcast.send(
                    err_ev, origin_channel_id=msg.source_channel_id,
                )
                return
            logger.info(
                "[InteractionManager] resume: active_app_id=%s model_id=%s", active_app_id, model_id,
            )

            # 先删旧 RunState，再恢复
            await self._repo.delete_run_state(session_id, run_id)

            # ★ resume 就是 Agent 从暂停点接着跑，同样可能跑几分钟——丢后台，别卡 stdin 读取循环。
            spawn_background(
                self._executor.execute(
                    run_id=run_id,
                    session_id=session_id,
                    user_id=msg.user_id,
                    user_input="",
                    source_channel_id=msg.source_channel_id,
                    active_app_id=active_app_id,
                    model_id=model_id,
                    resume_state=run_state,
                    interaction_response=response,
                ),
                label=f"interaction_resume:{run_id}",
            )

        except Exception as e:
            logger.exception("InteractionManager.resume failed: %s", e)
            if run_id and session_id:
                try:
                    await self._repo.delete_run_state(session_id, run_id)
                except Exception:
                    pass

    # ── 放弃废弃问卷 ───────────────────────────────────────────────────────────

    async def abandon_pending(self, session_id: str) -> None:
        """清理该 session 下所有过期/残留的问卷 RunState。

        正常流程中 interaction 锁会先触发恢复，不会走到这里。
        这个方法用于清理 TTL 过期等残留场景。
        """
        try:
            all_rows = await self._repo.list_all_run_states()
        except Exception:
            logger.error(
                "[InteractionManager] abandon_pending: list_all_run_states 失败 session=%s → 放弃清理"
                "（残留问卷 RunState 可能未清，后续锁查询会误判，见静默降级审计 #7）。",
                session_id, exc_info=True,
            )
            return
        for _sid, run_id, serialized in all_rows:
            if _sid != session_id:
                continue
            try:
                rs = self._deserialize(serialized)
            except Exception:
                logger.warning(
                    "[InteractionManager] abandon_pending: RunState 反序列化失败 session=%s run=%s"
                    " → 跳过（该残留可能漏清，见静默降级审计 #7）。",
                    session_id, run_id, exc_info=True,
                )
                continue
            if "pending_interaction" not in (rs.metadata or {}):
                continue
            try:
                await self._repo.delete_run_state(session_id, run_id)
            except Exception:
                logger.warning(
                    "[InteractionManager] abandon_pending: 删除残留 RunState 失败 session=%s run=%s"
                    "（下次 TTL 清理再试，见静默降级审计 #7）。",
                    session_id, run_id, exc_info=True,
                )

    # ── 启动恢复 ───────────────────────────────────────────────────────────────

    async def restore_on_startup(self, channel_id: str = "__desktop_ipc__") -> None:
        """扫描 RunState，恢复未过期的问卷到前端（全渠道广播）。"""
        try:
            all_states = await self._repo.list_all_run_states()
        except Exception as e:
            logger.error("InteractionManager.restore: list failed: %s", e)
            return

        restored = 0
        for session_id, run_id, serialized in all_states:
            try:
                run_state = self._deserialize(serialized)
            except Exception:
                logger.warning(
                    "[InteractionManager] restore: RunState 反序列化失败 session=%s run=%s → 跳过"
                    "（该问卷启动时无法恢复到前端，见静默降级审计 #7）。",
                    session_id, run_id, exc_info=True,
                )
                continue

            raw = (run_state.metadata or {}).get("pending_interaction")
            if not raw:
                continue

            tool_args = raw.get("tool_args") or {}
            questions_json = tool_args.get("questions_json")
            if not questions_json:
                continue

            questions = _extract_all_questions(questions_json)
            if not questions:
                continue

            reply_obj = self._reply_mgr.resume_reply(run_id)
            # ★ 出站必须带 session_id：前端按 payload.session_id 分桶，缺失会把恢复的
            #   问卷渲染到错误会话。
            rs_ev = NormalizedEvent.reply_start(
                reply_id=reply_obj.value,
                run_id=run_id,
                reply_scope=reply_obj.scope.value,
            )
            rs_ev.payload["session_id"] = session_id
            await self._broadcast.send(rs_ev)
            ir_ev = NormalizedEvent.interaction_request(
                request_id=f"restore-{run_id}",
                questions=questions,
                tool_name=raw.get("tool_name", "ask_user"),
                reply_id=reply_obj.value,
                run_id=run_id,
            )
            ir_ev.payload["session_id"] = session_id
            await self._broadcast.send(ir_ev)
            restored += 1

        if restored:
            logger.info(
                "[InteractionManager] Restored %d pending interaction(s)",
                restored,
            )

    # ── TTL 清理 ───────────────────────────────────────────────────────────────

    async def cleanup_expired(self, ttl_seconds: int) -> None:
        """清理超过 TTL 的 RunState。"""
        try:
            await self._repo.cleanup_expired_run_states(ttl_seconds)
        except Exception as e:
            logger.warning("InteractionManager TTL cleanup: %s", e)


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
