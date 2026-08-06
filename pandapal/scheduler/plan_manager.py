"""pandapal.scheduler.plan_manager — Plan Mode 审批暂停/恢复管理器。

类似 HITLManager：
  - pause(): Executor 检测到 PLAN_APPROVAL_REQUESTED 时回调，保存 RunState
  - resume(): 用户决策后恢复 Agent 执行

用户决策三向路径：
  - approve  → plan_action="approve"  → Agent 进入执行阶段
  - refine   → plan_action="refine"   → Agent 重新进入规划（完善模式）
  - abandon  → plan_action="abandon"  → 清理状态，退出 Plan Mode
"""

from __future__ import annotations

import logging
from typing import Any

from pandapal.events.normalized import NormalizedEvent
from pandapal.scheduler.background import spawn_background
from pandapal import session_id as session_id_mod

from pandapal.degradation import DegradationEvent, report_degradation

logger = logging.getLogger(__name__)


class PlanModeManager:
    """Plan Mode 审批暂停/恢复管理器。"""

    _OWNER_CAP = 256  # _pending_owner 上限，超出按 FIFO 淘汰防内存泄漏

    def __init__(self, repo: Any, broadcast: Any, router: Any) -> None:
        self._repo = repo  # RunStateRepository
        self._broadcast = broadcast  # MessageBroadcast
        self._router = router  # MessageRouter
        self._executor: Any = None  # 由 AgentScheduler 在构造后注入
        # ★ run_id → 提交计划的「所属 session_id」。审批恢复时以此为准，
        #   杜绝前端/Rust 误传 session_id 导致的跨会话串台（长任务批准后在
        #   错误会话里恢复执行）。HITL/Interaction 靠 RunState(按 session 分片)
        #   天然有此保护，Plan 路径不存 RunState，故在此显式登记归属。
        self._pending_owner: dict[str, str] = {}
        # ★ run_id → 提交计划时所选 model_id。plan 路径不存 RunState，模型无法经
        #   RunState.metadata 透传（HITL/ask_user 靠它），故在此按 run_id 内存登记，
        #   resume 时取回，保证批准后按同一模型/厂商续跑（否则回落默认 provider，
        #   引发「deepseek 会话批准计划后被切回 dashscope 撞额度」）。与 _pending_owner 同生命周期。
        self._pending_model: dict[str, str] = {}

    # ── Executor 回调：暂停 ────────────────────────────────────────────────

    async def pause(
        self,
        stream_event: Any,
        run_id: str,
        session_id: str,
        active_app_id: str = "",
        model_id: str = "",
    ) -> None:
        """Plan Mode 提交审批时，Executor 回调此方法。

        登记 run_id → session_id 归属（防串台）+ run_id → model_id（保同模型续跑），供 resume()。
        """
        logger.info(
            "[PlanManager] pause: run_id=%s session_id=%s model_id=%s",
            run_id, session_id, model_id or "(default)",
        )
        if run_id and session_id:
            self._pending_owner[run_id] = session_id
            if model_id:
                self._pending_model[run_id] = model_id
            # 有界防泄漏：进程长跑下未被 abandon 清理的条目做 FIFO 淘汰（仅内存归属提示，
            # 淘汰后仍可回退到入站 session_id——前端/Rust 已修复为携带正确 session）。
            if len(self._pending_owner) > self._OWNER_CAP:
                oldest = next(iter(self._pending_owner))
                self._pending_model.pop(oldest, None)
                self._pending_owner.pop(oldest, None)

    # ── 恢复：用户决策处理 ────────────────────────────────────────────────

    async def resume(self, msg: Any) -> None:  # msg: InboundMessage
        """用户对计划做出决策后，Router 路由到此方法。

        msg.content 结构:
          {
            "plan_action": "approve" | "refine" | "abandon",
            "run_id": "...",
            "session_id": "...",
            "user_id": "...",
            "edited_plan_content": None | "...",
            "user_text": "..."  # refine 时用户输入的完善指令
          }
        """
        content = msg.content if isinstance(msg.content, dict) else {}
        # ★ 决策类字段 fail-fast（静默降级审计 #2 / §1.1 原则一）：plan_action 表达用户对计划的
        #   决策（approve/refine/abandon），是「门禁类」字段——绝不允许缺失时默认放行。此前
        #   `content.get("plan_action", "approve")` 会把「前端漏传/字段名写错」静默当成「用户批准」，
        #   本该等审批的计划被直接执行。前端契约稳定携带（Rust String 必填、TS 必填枚举），故缺失=bug：
        #   立即中止 + 冒泡 error 到 UI，绝不默认 approve。
        run_id = content.get("run_id", "")
        plan_action = content.get("plan_action")
        if plan_action not in ("approve", "refine", "abandon"):
            logger.error(
                "[PlanManager] resume: plan_action 非法/缺失 run_id=%s 实际=%r，"
                "拒绝恢复（决策类字段绝不默认放行，见静默降级审计 #2）。",
                run_id, plan_action,
            )
            report_degradation(
                DegradationEvent.PLAN_ACTION_MISSING,
                category="decision", severity="abort", source="plan_manager.resume",
                expected="approve|refine|abandon", fallback=repr(plan_action),
                run_id=run_id,
            )
            err_ev = NormalizedEvent.error(
                error_code="plan_action_missing",
                error_message="计划决策丢失，未能执行。请在计划审批弹窗中重新点击「批准 / 完善 / 放弃」。",
                error_detail=f"plan_action={plan_action!r} run_id={run_id}",
                run_id=run_id,
            )
            # 前端按 session_id 分桶；此处 session 尚未经 assert，用入站信封值尽力而为（仅用于展示分桶）。
            err_ev.payload["session_id"] = (
                content.get("session_id") or getattr(msg, "session_id", "") or ""
            )
            await self._broadcast.send(err_ev, origin_channel_id=msg.source_channel_id)
            return
        # 两层信封（content 与消息头）本应同一真相 → 断言一致（不一致即污染，抛错留痕）。
        try:
            session_id = session_id_mod.assert_consistent(
                content.get("session_id"), msg.session_id, where="plan_manager.resume",
            )
        except session_id_mod.SessionIdError as e:
            logger.error("[PlanManager] resume: %s", e)
            return

        # ★ 以「暂停时登记的归属 session」为准，校正前端/Rust 可能误传的 session_id。
        #   这是防跨会话串台的最后一道闸：即便入站 session_id 指向别的会话，
        #   计划也只会在真正提交它的 session 里恢复。
        #   用 get 而非 pop：决策被去重后重放 / 前端重发时，第二次仍能拿到归属校正，
        #   不会退回信任入站 session_id。清理只在终态 abandon 时做（见下）。
        owner_session = self._pending_owner.get(run_id) if run_id else None
        if owner_session and owner_session != session_id:
            logger.warning(
                "[PlanManager] resume: session_id 校正 %s → %s (run_id=%s)，"
                "入站 session 与计划归属不符，已按归属恢复以防串台。",
                session_id, owner_session, run_id,
            )
            session_id = owner_session
        elif owner_session:
            session_id = owner_session

        if not session_id:
            logger.error("[PlanManager] resume: session_id is required")
            return

        # abandon 是终态（用户放弃，不会再 approve）→ 立即清理归属，避免泄漏。
        # approve/refine 保留归属：refine 会重新进入 plan（pause 覆盖同 run_id），
        # approve 后若有重放仍能被正确校正；有界字典由 pause 的 FIFO 淘汰兜底。
        if plan_action == "abandon" and run_id:
            self._pending_owner.pop(run_id, None)
            self._pending_model.pop(run_id, None)
        user_id = content.get("user_id", msg.user_id)
        user_text = content.get("user_text", "")
        edited_plan_content = content.get("edited_plan_content")
        source_channel_id = msg.source_channel_id

        logger.info(
            "[PlanManager] resume: plan_action=%s run_id=%s",
            plan_action, run_id,
        )

        if self._executor is None:
            logger.error("[PlanManager] resume: executor not injected")
            return

        # 通过 executor.execute() 重新启动 Agent run，传入 plan_action
        active_app_id = str(content.get("active_app_id", ""))
        # ★ 模型选择：优先取 pause 时按 run_id 登记的 model_id（plan 不存 RunState，靠内存登记保同模型续跑）；
        #   内存缺失（如进程在待审批期间重启）再取 content.model_id。model_id 是 ID 类字段——
        #   **没有 default，缺失即报错**（静默降级审计 #8 / §1.1 原则一）：run 启动时 executor 已把具体
        #   model_id 传给 plan pause 登记，故正常流程必有；两处都取不到 = 无法确立模型身份 → fail-fast
        #   报错中止，绝不回落默认 provider（那正是「deepseek 会话批准计划后被切回 dashscope 撞额度」）。
        # 注：_pending_model 是 {run_id: model_id}，下面用 run_id 作 key 查回的是 model_id（不是 run_id）。
        registered_model_id = self._pending_model.get(run_id)  # run_id → 该 run 暂停时登记的 model_id
        model_id = registered_model_id or content.get("model_id") or None
        if not model_id:
            logger.error(
                "[PlanManager] resume: model_id 恢复失败 run_id=%s（内存登记缺失且 content 未带，"
                "多因进程在待审批期间重启）→ 拒绝恢复（ID 类零 default，绝不静默回落默认模型/provider，"
                "见静默降级审计 #8）。", run_id,
            )
            report_degradation(
                DegradationEvent.MODEL_ID_MISSING_IN_RESUME,
                category="id", severity="abort", source="plan_manager.resume",
                expected="registered/content model_id", fallback=None, run_id=run_id,
            )
            err_ev = NormalizedEvent.error(
                error_code="resume_model_id_missing",
                error_message="该计划的模型信息已丢失，无法安全恢复，请重新发起该任务。",
                error_detail=f"run_id={run_id}",
                run_id=run_id,
            )
            err_ev.payload["session_id"] = session_id
            await self._broadcast.send(err_ev, origin_channel_id=source_channel_id)
            return
        try:
            # ★ 批准计划后 Agent 从暂停点接着把方案跑完，是长任务——丢后台，别卡 stdin 读取循环。
            spawn_background(
                self._executor.execute(
                    run_id=run_id or f"r-plan-{id(msg)}",
                    session_id=session_id,
                    user_id=user_id,
                    user_input=user_text or "",
                    source_channel_id=source_channel_id,
                    active_app_id=active_app_id,
                    model_id=model_id,
                    plan_action=plan_action,
                    edited_plan_content=edited_plan_content,
                ),
                label=f"plan_resume:{run_id or 'r-plan'}",
            )
        except Exception as e:
            logger.exception("[PlanManager] resume execute failed: %s", e)
            err_ev = NormalizedEvent.error(
                error_code="plan_resume_failed",
                error_message=str(e),
                run_id=run_id,
            )
            err_ev.payload["session_id"] = session_id  # 前端按 session_id 分桶
            await self._broadcast.send(
                err_ev, origin_channel_id=source_channel_id,
            )
