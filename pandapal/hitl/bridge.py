"""HITLBridge — 审批桥接层（5.2 重写版）。

★ 5.2 关键改造：
- 所有出站消息统一用 NormalizedEvent（不再构造 OutboundMessage + bytes envelope）
- 消除 wecom_bridge.py 的 envelope 解包 hack（已不再产生）
- 按 approval_id 精确处理（不再依赖 run_id 反查）
- 单一 Owner：整个系统只有 HITLBridge 能修改审批状态

设计约束（保留）：
- BL1: pending → approved/rejected（不可逆）
- BL3: 幂等（重复点击静默忽略）
- BL5: Fail-Safe（持久化失败时拒绝）
- BL7: 一次决策终结（原子操作）
- O3: handle_* 永不抛异常
- S3: session_id 隔离校验
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from pandapal import session_id as session_id_mod
from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.events.normalized import EventType, NormalizedEvent
from pandapal.hitl.approval_log import ApprovalMarkdownLog
from pandapal.messages.types import (
    HITLDecision,
    RouterMessageType,
)
from pandapal.router.models import InboundMessage
from pandapal.router.router import MessageRouter
from pandapal.storage.models import ApprovalDecision, ApprovalRequest, ApprovalStatus
from pandapal.storage.repositories.sqlite_approval_repo import ApprovalRepository

logger = logging.getLogger(__name__)


def _to_approval_decision(raw: object) -> ApprovalDecision:
    """把入站 decision（IPC 字符串 / 枚举 / HITLDecision 常量）归一为 ApprovalDecision 枚举。

    为什么需要它：`ApprovalRepository.resolve_approval_request(decision: ApprovalDecision)`
    要求枚举（sqlite 后端会取 `decision.value`）；但入站 IPC 的 decision 是**裸字符串**
    （前端发 "approved"/"rejected"，见 desktop/src/types/api.ts）。此前 bridge 直接把
    字符串透传给 repo，markdown 后端容忍（str 兜底）而 sqlite 后端 `.value` 崩，
    切 sqlite 后暴露为 `'str' object has no attribute 'value'`。此处一次归一，消除该分歧。

    未知/非法值 **fail-closed → REJECTED**（HITL 场景宁拒不放），并留痕告警（不静默）。
    """
    if isinstance(raw, ApprovalDecision):
        return raw
    value = getattr(raw, "value", raw)  # 兼容其他枚举/带 .value 的对象
    s = str(value).strip().lower()
    if s == ApprovalDecision.APPROVED.value:  # "approved"
        return ApprovalDecision.APPROVED
    if s == ApprovalDecision.REJECTED.value:  # "rejected"
        return ApprovalDecision.REJECTED
    logger.warning("[HITL] unknown decision %r → fail-closed REJECTED", raw)
    return ApprovalDecision.REJECTED


class HITLBridge:
    """HITL 审批桥接（5.2 重写版）。

    核心思想：整个系统中，只有 HITLBridge 能修改审批状态。
    """

    def __init__(
        self,
        approval_repo: ApprovalRepository,
        broadcast: MessageBroadcast,
        router: MessageRouter,
        approval_log: ApprovalMarkdownLog | None = None,
        run_state_repo: Any = None,
    ) -> None:
        if approval_repo is None:
            raise ValueError("approval_repo cannot be None")
        if broadcast is None:
            raise ValueError("broadcast cannot be None")
        if router is None:
            raise ValueError("router cannot be None")

        self._approval_repo = approval_repo
        self._broadcast = broadcast
        self._router = router
        self._approval_log = approval_log
        self._run_state_repo = run_state_repo  # 可选，用于防呆检查

    # ══════════════════════════════════════════════════════════════════════════════
    # Public Methods
    # ══════════════════════════════════════════════════════════════════════════════

    def register_route_handlers(self) -> None:
        """向路由层注册 HITL 相关 handler（应用启动时调用一次）。"""
        self._router.register_route_handler(
            RouterMessageType.APPROVAL_NEEDED, self.handle_approval_needed
        )
        self._router.register_route_handler(
            RouterMessageType.APPROVAL_RESPONSE, self.handle_approval_response
        )
        logger.info("HITL Bridge route handlers registered")

    async def restore_pending_approvals(self) -> None:
        """进程重启后恢复 pending 审批。

        防呆：检查对应 RunState 是否存在。不存在 = 孤儿审批（RunState
        因进程崩溃/TTL过期/cleanup丢失），自动拒绝不广播。
        """
        try:
            pending = await self._approval_repo.find_all_pending_approval_requests()
        except Exception as e:
            logger.error("Failed to restore pending approvals: %s", e)
            return
        if not pending:
            return

        restored = 0
        expired = 0
        for request in pending:
            # ★ 防呆：检查 RunState 是否存在
            run_state_exists = True
            if self._run_state_repo is not None:
                try:
                    stored = await self._run_state_repo.get_run_state(
                        request.session_id, request.run_id,
                    )
                except Exception:
                    stored = None
                run_state_exists = stored is not None

            if not run_state_exists:
                logger.warning(
                    "[HITL] Orphan approval: approval_id=%s run_id=%s "
                    "— RunState lost, auto-rejecting",
                    request.approval_id, request.run_id,
                )
                # 复用现有拒绝路径，但不通知前端（孤儿审批无恢复可能）
                try:
                    await self._approval_repo.resolve_approval_request(
                        request.approval_id,
                        decision=ApprovalDecision.REJECTED,
                        resolved_at=datetime.now(timezone.utc),
                    )
                except Exception as e:
                    logger.error(
                        "Failed to expire orphan approval %s: %s",
                        request.approval_id, e,
                    )
                # 广播 APPROVAL_RESULT 给前端关闭弹窗
                await self._broadcast_approval_result(
                    approval_id=request.approval_id,
                    decision=HITLDecision.REJECTED,
                    tool_name=request.tool_name,
                    run_id=request.run_id,
                    reply_id=request.reply_id,
                )
                expired += 1
                continue

            await self._broadcast_approval_request(request)
            restored += 1

        logger.info(
            "[HITL] Restored %d pending approvals, expired %d orphans",
            restored, expired,
        )

    async def handle_approval_needed(self, msg: InboundMessage) -> None:
        """处理"Agent 请求审批"消息（O3: 永不向外抛异常）。"""
        run_id = ""
        session_id = ""
        try:
            content = msg.content if isinstance(msg.content, dict) else {}

            # 提取关键字段
            run_id = content.get("run_id", "")
            # 两层信封（消息头与 content）本应同一真相 → 断言一致；不一致即污染，
            # 抛错留痕并终止（对齐 hitl_manager/plan_manager 的 SESSION_ID 契约做法）。
            try:
                session_id = session_id_mod.assert_consistent(
                    content.get("session_id"), msg.session_id,
                    where="hitl.bridge.handle_approval_needed",
                )
            except session_id_mod.SessionIdError as e:
                logger.error("[HITL] handle_approval_needed: %s", e)
                return
            tool_name = content.get("tool_name", "unknown")
            tool_args_summary = content.get("tool_args_summary", "")
            reply_id = content.get("reply_id")  # Option C
            # approval_id 优先用上游注入（保持端到端可追踪）
            approval_id = content.get("approval_id") or f"appr-{uuid.uuid4().hex[:12]}"

            # 构造 ApprovalRequest
            now = datetime.now(timezone.utc)
            request = ApprovalRequest(
                approval_id=approval_id,
                user_id=msg.user_id,
                run_id=run_id,
                tool_name=tool_name,
                tool_args_summary=tool_args_summary,
                status=ApprovalStatus.PENDING,
                created_at=now,
                session_id=session_id,
                source_channel_id=msg.source_channel_id,
                reply_id=reply_id,
            )

            # 持久化
            try:
                await self._approval_repo.save_approval_request(request)
            except Exception as e:
                logger.error(
                    "Failed to persist approval: %s — Fail-Safe: 拒绝并通知",
                    e,
                )
                # 注入 APPROVAL_DECISION(REJECTED) 让 Agent 走拒绝分支
                await self._dispatch_approval_decision(
                    run_id=run_id, session_id=session_id, decision=HITLDecision.REJECTED,
                    approval_id=approval_id, user_id=msg.user_id,
                    source_channel_id=msg.source_channel_id, reason="持久化失败",
                )
                return

            # 写入 Markdown 审计日志
            if self._approval_log is not None:
                try:
                    self._approval_log.log_request(request)
                except Exception as e:
                    logger.warning("Failed to write approval log: %s", e)


            # 广播审批请求（NormalizedEvent.hitl_request）
            await self._broadcast_approval_request(request)

        except Exception as e:
            logger.exception("handle_approval_needed failed: %s", e)
            # 兜底：拒绝
            if run_id and session_id:
                try:
                    await self._dispatch_approval_decision(
                        run_id=run_id, session_id=session_id,
                        decision=HITLDecision.REJECTED,
                        approval_id="",
                        user_id=msg.user_id,
                        source_channel_id=msg.source_channel_id,
                        reason=f"handle_approval_needed 异常: {e}",
                    )
                except Exception:
                    pass

    async def handle_approval_response(self, msg: InboundMessage) -> None:
        """处理来自设备/用户的审批决策。

        ★ 5.2 改造：按 approval_id 精确处理，不再依赖 run_id 反查。
        """
        try:
            content = msg.content if isinstance(msg.content, dict) else {}
            approval_id = content.get("approval_id")
            # 归一：repo 要枚举（sqlite 取 .value），dispatch/broadcast 与 SDK
            # Agent.run(hitl_decision=...) 要裸字符串。一处归一，两种形态各取所需。
            # ★ 决策类字段绝不默认放行（静默降级审计 #2 / §1.1）：此前默认传 APPROVED，
            #   一旦入站漏带 decision 就静默「批准」——门禁 fail-open。改为不给默认，让缺失
            #   （None）落入 _to_approval_decision 已有的 fail-closed 分支 → REJECTED + 留痕。
            decision_enum = _to_approval_decision(
                content.get("decision")
            )
            decision_str = decision_enum.value
            user_id = content.get("user_id") or msg.user_id
            source_channel_id = content.get(
                "source_channel_id", msg.source_channel_id
            )

            if not approval_id:
                logger.warning(
                    "handle_approval_response: missing approval_id, ignoring"
                )
                return

            # 校验 approval 存在
            approval = await self._approval_repo.get(approval_id)
            if not approval:
                logger.warning("approval %s not found", approval_id)
                # 通知用户审批已过期
                if source_channel_id:
                    await self._broadcast.send(
                        NormalizedEvent(
                            event_type=EventType.ERROR,
                            reply_id=None, run_id=None,
                            payload={
                                "error_code": "approval_not_found",
                                "error_message": "该审批已过期或不存在",
                                "error_detail": f"approval_id={approval_id}",
                            },
                        ),
                        origin_channel_id=source_channel_id,
                    )
                return

            # 幂等检查：已决策的静默忽略
            if approval.status != ApprovalStatus.PENDING:
                logger.info(
                    "approval %s already resolved (%s), idempotent skip",
                    approval_id, approval.status,
                )
                return

            # ★ 防呆：检查 RunState 是否还存在（可能被 TTL 清理/手动删除）
            if self._run_state_repo is not None and approval.run_id:
                try:
                    stored = await self._run_state_repo.get_run_state(
                        approval.session_id, approval.run_id,
                    )
                except Exception:
                    stored = None
                if stored is None:
                    logger.warning(
                        "[HITL] RunState lost for approval %s (run_id=%s), "
                        "auto-rejecting",
                        approval_id, approval.run_id,
                    )
                    # 直接拒绝，不恢复 Agent
                    await self._approval_repo.resolve_approval_request(
                        approval_id=approval_id,
                        decision=ApprovalDecision.REJECTED,
                        resolved_at=datetime.now(timezone.utc),
                    )
                    await self._broadcast_approval_result(
                        approval_id=approval_id,
                        decision=HITLDecision.REJECTED,
                        tool_name=approval.tool_name,
                        run_id=approval.run_id,
                        reply_id=approval.reply_id,
                    )
                    return

            # 原子更新（BL7 一次决策终结）
            try:
                resolved = await self._approval_repo.resolve_approval_request(
                    approval_id=approval_id,
                    decision=decision_enum,
                    resolved_at=datetime.now(timezone.utc),
                    decided_by=user_id,
                )
            except Exception as e:
                logger.error(
                    "Failed to resolve approval %s: %s", approval_id, e
                )
                return

            if not resolved:
                # 已被其他设备决策
                logger.info("approval %s resolved by other device", approval_id)
                return

            # 写审计日志
            if self._approval_log is not None:
                try:
                    self._approval_log.log_decision(
                        approval_id=approval_id,
                        decision=decision_str,
                        decided_by=user_id,
                    )
                except Exception as e:
                    logger.warning("Failed to write decision log: %s", e)

            # 广播 APPROVAL_RESULT 给所有渠道
            await self._broadcast_approval_result(
                approval_id=approval_id,
                decision=decision_str,
                tool_name=approval.tool_name,
                run_id=approval.run_id,
                reply_id=approval.reply_id,
            )

            # 注入 APPROVAL_DECISION 给 Scheduler（恢复 Agent）
            await self._dispatch_approval_decision(
                run_id=approval.run_id,
                session_id=approval.session_id,
                decision=decision_str,
                approval_id=approval_id,
                user_id=user_id,
                source_channel_id=source_channel_id,
                reply_id=approval.reply_id,
            )

        except Exception as e:
            logger.exception("handle_approval_response failed: %s", e)

    async def shutdown(self) -> None:
        """关闭 HITL Bridge。"""
        logger.info("HITL Bridge shutdown complete (stateless)")

    # ══════════════════════════════════════════════════════════════════════════════
    # Private: 广播
    # ══════════════════════════════════════════════════════════════════════════════

    async def _broadcast_approval_request(
        self, request: ApprovalRequest
    ) -> None:
        """广播审批请求给所有活跃渠道（NormalizedEvent.hitl_request）。

        ★ 5.2 改造：直接构造 NormalizedEvent，不再 envelope。
        """
        # run_id 在 HITL 场景下 == reply_id（Option C 硬约定）
        # 这里必须用 approval.run_id 作为 run_id（即使它为空）
        run_id = request.run_id or f"ns:system:{uuid.uuid4().hex[:8]}"
        try:
            event = NormalizedEvent.hitl_request(
                approval_id=request.approval_id,
                tool_name=request.tool_name,
                tool_args_summary=_parse_tool_args_summary(
                    request.tool_args_summary
                ),
                session_id=request.session_id or "",
                run_id=run_id,
            )
        except ValueError as e:
            # reply_id == run_id 校验失败 —— 兜底用 system reply_id
            logger.warning(
                "_broadcast_approval_request: %s — using system reply_id", e
            )
            event = NormalizedEvent(
                event_type=EventType.HITL_REQUEST,
                reply_id=f"ns:system:{uuid.uuid4().hex[:8]}",
                run_id=f"ns:system:{uuid.uuid4().hex[:8]}",
                payload={
                    "approval_id": request.approval_id,
                    "tool_name": request.tool_name,
                    "tool_args_summary": _parse_tool_args_summary(
                        request.tool_args_summary
                    ),
                    "session_id": request.session_id or "",
                },
            )
        await self._broadcast.send(
            event, origin_channel_id=request.source_channel_id,
        )

    async def _broadcast_approval_result(
        self,
        approval_id: str,
        decision: str,
        tool_name: str = "",
        run_id: str = "",
        reply_id: str = "",
    ) -> None:
        """广播审批结果（所有渠道）。"""
        # 注意：APPROVAL_RESULT 不是 stream 类事件，
        # 走 broadcast.send 默认 BROADCAST 策略 → 所有渠道收
        await self._broadcast.send(
            NormalizedEvent.approval_result(
                approval_id=approval_id,
                decision=decision,
                reason=tool_name,
                reply_id=reply_id,
                run_id=run_id,
            ),
        )

    # ══════════════════════════════════════════════════════════════════════════════
    # Private: 孤儿审批清理
    # ══════════════════════════════════════════════════════════════════════════════

    async def _cleanup_orphan_approvals(self) -> None:
        """清理 RunState 已丢失的孤儿审批（供调度器/启动恢复调用，不在热路径使用）。"""
        if self._run_state_repo is None:
            return
        try:
            pending = await self._approval_repo.find_all_pending_approval_requests()
        except Exception:
            return
        for approval in pending:
            if not approval.run_id:
                continue
            try:
                stored = await self._run_state_repo.get_run_state(
                    approval.session_id, approval.run_id,
                )
            except Exception:
                continue
            if stored is None:
                logger.info(
                    "[HITL] Orphan approval cleaned: approval_id=%s run_id=%s",
                    approval.approval_id, approval.run_id,
                )
                try:
                    await self._approval_repo.resolve_approval_request(
                        approval.approval_id,
                        decision=ApprovalDecision.REJECTED,
                        resolved_at=datetime.now(timezone.utc),
                    )
                except Exception:
                    pass

    # ══════════════════════════════════════════════════════════════════════════════
    # Private: 注入 APPROVAL_DECISION 给 Scheduler
    # ══════════════════════════════════════════════════════════════════════════════

    async def _dispatch_approval_decision(
        self,
        run_id: str,
        session_id: str,
        decision: str,
        approval_id: str,
        user_id: str,
        source_channel_id: str,
        reply_id: str = "",
        reason: str = "",
    ) -> None:
        """注入 APPROVAL_DECISION 到 Router（Scheduler 接收后恢复 Agent）。"""
        decision_msg = InboundMessage(
            msg_id=str(uuid.uuid4()),
            message_type=RouterMessageType.APPROVAL_DECISION,
            source_channel_id=source_channel_id,
            user_id=user_id,
            session_id=session_id,
            content={
                "run_id": run_id,
                "session_id": session_id,
                "decision": decision,
                "approval_id": approval_id,
                "resume_reply_id": reply_id or run_id,  # Option C
                "user_id": user_id,
                "reason": reason,
            },
        )
        try:
            await self._router.inject_inbound_message(decision_msg)
        except Exception as e:
            logger.error("Failed to inject APPROVAL_DECISION: %s", e)

    # ══════════════════════════════════════════════════════════════════════════════
    # 兼容层（保留旧 API 以便旧测试/调用方平稳过渡）
    # ══════════════════════════════════════════════════════════════════════════════

    async def _publish_error_reply(
        self,
        error_text: str,
        source_channel_id: str,
        user_id: str = "",
    ) -> None:
        """兼容旧 API：发布错误回复。"""
        await self._broadcast.send(
            NormalizedEvent.agent_reply(
                content=error_text,
                session_id="",
                reply_id=f"ns:error:{uuid.uuid4().hex[:8]}",
                run_id=f"ns:error:{uuid.uuid4().hex[:8]}",
            ),
            origin_channel_id=source_channel_id,
        )


def _parse_tool_args_summary(summary: Any) -> dict:
    """把 tool_args_summary 解析为 dict（兼容 string 和 dict 输入）。"""
    if isinstance(summary, dict):
        return summary
    if isinstance(summary, str):
        if not summary:
            return {}
        # 尝试解析 JSON
        import json
        try:
            return json.loads(summary)
        except (json.JSONDecodeError, TypeError):
            # 兜底：作为原始文本
            return {"raw": summary}
    return {"raw": str(summary)}
