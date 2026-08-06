"""Approval Request Repository 实现。

BL7 核心约束：resolve_approval_request 必须为原子 compare-and-update 操作。
防止一次 HITL 请求被二次决策。

特殊点：save_approval_request 使用 INSERT（不允许覆盖已有请求），
如果 approval_id 已存在则抛出 StorageDuplicateError。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import aiosqlite

from pandapal.storage.exceptions import StorageDuplicateError
from pandapal.storage.models import ApprovalDecision, ApprovalRequest, ApprovalStatus
from pandapal.storage.repositories._sqlite_base import BaseRepository


class ApprovalRepository(BaseRepository):
    """HITL 审批请求持久化操作。"""

    def __init__(self, conn: aiosqlite.Connection, timeout: float = 5.0) -> None:
        super().__init__(conn, timeout)

    async def save_approval_request(self, request: ApprovalRequest) -> None:
        """保存审批请求（INSERT only，不允许覆盖）。

        Raises:
            StorageDuplicateError: approval_id 已存在。
        """
        created_at = self._to_iso(request.created_at) or self._now_iso()
        try:
            await self._execute(
                "INSERT INTO approval_requests "
                "(approval_id, user_id, run_id, tool_name, tool_args_summary, "
                "status, decision, created_at, resolved_at, "
                "decision_user_id, session_id, source_channel_id, reply_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.approval_id,
                    request.user_id,
                    request.run_id,
                    request.tool_name,
                    request.tool_args_summary,
                    request.status,
                    request.decision,
                    created_at,
                    self._to_iso(request.resolved_at),
                    request.decision_user_id,
                    request.session_id,
                    request.source_channel_id,
                    request.reply_id,
                ),
                operation="save_approval_request",
            )
            await self._commit()
        except sqlite3.IntegrityError as e:
            raise StorageDuplicateError(
                "ApprovalRequest", request.approval_id
            ) from e

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        """按 approval_id 查找审批请求（统一接口名 find_approval_request 的别名）。"""
        return await self.find_approval_request(approval_id)

    async def find_approval_request(
        self, approval_id: str
    ) -> ApprovalRequest | None:
        """按 approval_id 查找审批请求。"""
        row = await self._fetchone(
            "SELECT approval_id, user_id, run_id, tool_name, tool_args_summary, "
            "status, decision, created_at, resolved_at, "
            "decision_user_id, session_id, source_channel_id, reply_id "
            "FROM approval_requests WHERE approval_id = ?",
            (approval_id,),
            operation="find_approval_request",
        )
        if row is None:
            return None
        return self._row_to_model(row)

    async def find_pending_approval_requests(
        self, user_id: str
    ) -> list[ApprovalRequest]:
        """查找用户所有待审批的请求（status='pending'）。"""
        rows = await self._fetchall(
            "SELECT approval_id, user_id, run_id, tool_name, tool_args_summary, "
            "status, decision, created_at, resolved_at, "
            "decision_user_id, session_id, source_channel_id, reply_id "
            "FROM approval_requests WHERE user_id = ? AND status = ? "
            "ORDER BY created_at ASC",
            (user_id, ApprovalStatus.PENDING.value),
            operation="find_pending_approval_requests",
        )
        return [self._row_to_model(row) for row in rows]

    async def find_all_pending_approval_requests(self) -> list[ApprovalRequest]:
        """查找所有待审批的请求（status='pending'），不限用户。

        用于进程重启后恢复超时任务（restore_pending_approvals），
        避免依赖空字符串通配约定。
        """
        rows = await self._fetchall(
            "SELECT approval_id, user_id, run_id, tool_name, tool_args_summary, "
            "status, decision, created_at, resolved_at, "
            "decision_user_id, session_id, source_channel_id, reply_id "
            "FROM approval_requests WHERE status = ? "
            "ORDER BY created_at ASC",
            (ApprovalStatus.PENDING.value,),
            operation="find_all_pending_approval_requests",
        )
        return [self._row_to_model(row) for row in rows]

    async def resolve_approval_request(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        resolved_at: datetime,
        expected_status: ApprovalStatus = ApprovalStatus.PENDING,
        **kwargs: Any,
    ) -> bool:
        """原子解决审批请求（BL7 compare-and-update）。

        使用 WHERE status = expected_status 保证原子性。
        如果状态已被其他路径（另一设备）改变，返回 False。

        Returns:
            True = 成功更新；False = 状态已被其他路径改变（幂等退出）。
        """
        decided_by = kwargs.get("decided_by")
        cursor = await self._execute(
            "UPDATE approval_requests "
            "SET status = ?, decision = ?, resolved_at = ?"
            + (", decision_user_id = ?" if decided_by else "")
            + " "
            "WHERE approval_id = ? AND status = ?",
            tuple(filter(None, [
                ApprovalStatus.RESOLVED.value,
                # 容忍枚举或裸字符串（与 MarkdownApprovalRepository 同口径）：调用方应传
                # ApprovalDecision，但历史上 HITLBridge 曾透传 IPC 裸字符串，此处兜底防
                # 'str' object has no attribute 'value'，杜绝两后端行为分歧。
                decision.value if hasattr(decision, "value") else str(decision),
                resolved_at.isoformat(),
                decided_by,
                approval_id,
                expected_status,
            ])),
            operation="resolve_approval_request",
        )
        await self._commit()
        return cursor.rowcount > 0  # type: ignore[return-value]

    async def find_pending_approval_by_run_id(
        self, run_id: str
    ) -> ApprovalRequest | None:
        """按 run_id 查找 pending 审批请求（文字 HITL 决策路径使用）。

        用于 AgentScheduler 处理用户文字同意/拒绝时，定位对应的 ApprovalRequest
        并通过 compare-and-update 解决，防止 HITLBridge 在进程重启后重广播。
        """
        row = await self._fetchone(
            "SELECT approval_id, user_id, run_id, tool_name, tool_args_summary, "
            "status, decision, created_at, resolved_at, "
            "decision_user_id, session_id, source_channel_id, reply_id "
            "FROM approval_requests WHERE run_id = ? AND status = ? LIMIT 1",
            (run_id, ApprovalStatus.PENDING.value),
            operation="find_pending_approval_by_run_id",
        )
        if row is None:
            return None
        return self._row_to_model(row)

    async def find_pending_by_session(
        self, session_id: str
    ) -> list[ApprovalRequest]:
        """按 session_id 查找所有 pending 审批请求。

        SessionListManager.soft_delete_session 用：删除会话前把该 session
        的所有待审批操作自动拒绝，防止用户看到"孤儿"弹窗。
        """
        rows = await self._fetchall(
            "SELECT approval_id, user_id, run_id, tool_name, tool_args_summary, "
            "status, decision, created_at, resolved_at, "
            "decision_user_id, session_id, source_channel_id, reply_id "
            "FROM approval_requests WHERE session_id = ? AND status = ? "
            "ORDER BY created_at ASC",
            (session_id, ApprovalStatus.PENDING.value),
            operation="find_pending_by_session",
        )
        return [self._row_to_model(row) for row in rows]

    async def delete_expired_approval_requests(self, before: datetime) -> int:
        """删除过期的审批请求（batch cleanup）。返回删除行数。"""
        cursor = await self._execute(
            "DELETE FROM approval_requests WHERE created_at < ?",
            (before.isoformat(),),
            operation="delete_expired_approval_requests",
        )
        await self._commit()
        return cursor.rowcount  # type: ignore[return-value]

    @staticmethod
    def _row_to_model(row: Any) -> ApprovalRequest:
        return ApprovalRequest(
            approval_id=row[0],
            user_id=row[1],
            run_id=row[2],
            tool_name=row[3],
            tool_args_summary=row[4],
            status=ApprovalStatus(row[5]),
            decision=row[6],
            created_at=BaseRepository._from_iso(row[7]),
            resolved_at=BaseRepository._from_iso(row[8]),
            decision_user_id=row[9],
            session_id=row[10],
            source_channel_id=row[11] or "",
            reply_id=row[12] if len(row) > 12 else None,
        )
