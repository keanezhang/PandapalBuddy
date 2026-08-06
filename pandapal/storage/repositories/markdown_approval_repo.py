"""Markdown Approval Repository 实现（异步接口）。

接口与 SQLiteApprovalRepository 完全对齐：
- save_approval_request
- find_approval_request
- find_pending_approval_by_run_id   ← HITLBridge 路径B（文字决策）依赖
- find_all_pending_approval_requests
- find_pending_by_session            ← SessionListManager 删会话时自动拒绝待审批
- resolve_approval_request          ← HITLBridge 决策更新依赖（BL7 原子语义）
- delete_expired_approval_requests
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pandapal.storage.models import ApprovalDecision, ApprovalRequest, ApprovalStatus
from pandapal.storage.repositories._markdown_base import MarkdownBaseRepository

logger = logging.getLogger(__name__)


class MarkdownApprovalRepository(MarkdownBaseRepository):
    """Markdown 审批请求持久化操作（异步接口）。"""

    def __init__(self, base_dir: str, timeout: float = 5.0) -> None:
        super().__init__(base_dir, "approvals", timeout, session_partitioned=True)

    # ──────────────────────────────────────────────
    # CRUD 操作（异步接口）
    # ──────────────────────────────────────────────

    async def save_approval_request(self, request: ApprovalRequest) -> None:
        """保存审批请求。

        注意：枚举字段存储 .value（字符串），避免 yaml.dump 产生
        !!python/object/apply 标签导致 yaml.safe_load 读取失败。
        """
        file_path = self._partition_path(request.session_id or "", request.approval_id)
        data = {
            "approval_id": request.approval_id,
            "user_id": request.user_id,
            "run_id": request.run_id,
            "tool_name": request.tool_name,
            "tool_args_summary": request.tool_args_summary,
            "status": request.status.value if hasattr(request.status, "value") else str(request.status),
            "decision": request.decision,
            "created_at": self._to_iso(request.created_at) or self._now_iso(),
            "resolved_at": self._to_iso(request.resolved_at),
            "decision_user_id": request.decision_user_id,
            "session_id": request.session_id,
            "source_channel_id": request.source_channel_id,
            "reply_id": request.reply_id,
        }
        title = f"Approval: {request.tool_name}"
        await self._write_entity(file_path, data, title)
        logger.info(
            "[MarkdownApprovalRepo] save: approval_id=%s, run_id=%s, tool=%s, status=%s",
            request.approval_id, request.run_id, request.tool_name, data["status"],
        )

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        """按 approval_id 查找审批请求（统一接口名）。"""
        return await self.find_approval_request(approval_id)

    async def find_approval_request(self, approval_id: str) -> ApprovalRequest | None:
        """按 approval_id 查找审批请求（id-only：跨 session 分区扫描定位）。"""
        file_path = await self._find_path_by_id(approval_id)
        if file_path is None:
            return None
        data = await self._read_entity(file_path)
        return self._dict_to_model(data) if data else None

    async def find_pending_approvals(self, user_id: str) -> list[ApprovalRequest]:
        """查找用户的所有待审批请求。"""
        entities = await self._filter_entities(user_id=user_id, status="pending")
        return [self._dict_to_model(data) for data in entities if data]

    async def find_all_pending_approval_requests(self) -> list[ApprovalRequest]:
        """查找所有待审批的请求（status='pending'），不限用户。

        用于进程重启后恢复超时任务（restore_pending_approvals），
        避免依赖空字符串通配约定。
        """
        entities = await self._filter_entities(status="pending")
        return [self._dict_to_model(data) for data in entities if data]

    async def find_pending_by_session(
        self, session_id: str
    ) -> list[ApprovalRequest]:
        """按 session_id 查找所有 pending 审批请求。

        SessionListManager.soft_delete_session 用：删除会话前把该 session
        的所有待审批操作自动拒绝，防止用户看到"孤儿"弹窗。
        """
        entities = await self._filter_entities(
            session_id=session_id, status="pending"
        )
        entities.sort(key=lambda d: d.get("created_at") or "")
        return [self._dict_to_model(data) for data in entities if data]

    async def find_pending_approval_by_run_id(
        self, run_id: str
    ) -> ApprovalRequest | None:
        """按 run_id 查找 pending 审批请求（HITLBridge 路径B：文字决策使用）。

        场景：用户打字"批准"，只有 run_id 没有 approval_id，
        需要反查对应的 pending 审批记录。
        """
        entities = await self._filter_entities(status="pending")
        for data in entities:
            if data and data.get("run_id") == run_id:
                return self._dict_to_model(data)
        return None

    async def resolve_approval_request(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        resolved_at: datetime,
        expected_status: ApprovalStatus = ApprovalStatus.PENDING,
        **kwargs: Any,
    ) -> bool:
        """原子解决审批请求（BL7 compare-and-update 语义）。

        只有当前状态 == expected_status 时才更新，防止重复决策。

        Returns:
            True = 成功更新；False = 状态已被其他路径改变（幂等退出）。
        """
        file_path = await self._find_path_by_id(approval_id)
        data = await self._read_entity(file_path) if file_path else None
        if file_path is None or data is None:
            logger.warning(
                "[MarkdownApprovalRepo] resolve: approval_id=%s not found", approval_id
            )
            return False

        # BL7: compare-and-update — 状态不匹配则拒绝更新
        current_status = data.get("status", "")
        # 兼容两种格式：枚举对象 or 字符串
        expected_value = expected_status.value if hasattr(expected_status, "value") else str(expected_status)
        current_value = current_status.value if hasattr(current_status, "value") else str(current_status)

        if current_value != expected_value:
            logger.warning(
                "[MarkdownApprovalRepo] resolve: status mismatch — "
                "approval_id=%s, current=%s, expected=%s (concurrent decision?)",
                approval_id, current_value, expected_value,
            )
            return False

        # 更新状态
        data["status"] = ApprovalStatus.RESOLVED.value
        data["decision"] = decision.value if hasattr(decision, "value") else str(decision)
        data["resolved_at"] = self._to_iso(resolved_at)
        if kwargs.get("decided_by"):
            data["decision_user_id"] = kwargs["decided_by"]

        title = f"Approval: {data.get('tool_name', '')}"
        await self._write_entity(file_path, data, title)

        logger.info(
            "[MarkdownApprovalRepo] resolve: approval_id=%s → decision=%s",
            approval_id, data["decision"],
        )
        return True

    async def update_approval_decision(
        self, approval_id: str, decision: str, resolved_at: Any,
        decision_user_id: str | None = None,
    ) -> None:
        """更新审批决策。"""
        file_path = await self._find_path_by_id(approval_id)
        data = await self._read_entity(file_path) if file_path else None
        if file_path and data:
            data["status"] = "approved" if decision == "approve" else "rejected"
            data["decision"] = decision
            data["resolved_at"] = self._to_iso(resolved_at)
            data["decision_user_id"] = decision_user_id
            await self._write_entity(file_path, data, f"Approval: {data.get('tool_name', '')}")

    async def delete_approval(self, approval_id: str) -> None:
        """删除审批请求（id-only：跨 session 分区扫描定位）。"""
        file_path = await self._find_path_by_id(approval_id)
        if file_path is not None:
            await self._delete_entity(file_path)

    # ──────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _dict_to_model(data: dict[str, Any]) -> ApprovalRequest:
        """将字典转换为 ApprovalRequest 模型。"""

        def parse_datetime(value):
            if not value:
                return None
            try:
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                return None

        # status 兼容：枚举对象 or 字符串
        raw_status = data.get("status", "pending")
        if hasattr(raw_status, "value"):
            status = raw_status
        else:
            status = ApprovalStatus(str(raw_status)) if raw_status in ("pending", "resolved") else ApprovalStatus.PENDING

        return ApprovalRequest(
            approval_id=data.get("approval_id") or data.get("request_id", ""),
            user_id=data.get("user_id", ""),
            run_id=data.get("run_id", ""),
            tool_name=data.get("tool_name", ""),
            tool_args_summary=data.get("tool_args_summary"),
            status=status,
            decision=data.get("decision"),
            created_at=parse_datetime(data.get("created_at")),
            resolved_at=parse_datetime(data.get("resolved_at")),
            decision_user_id=data.get("decision_user_id"),
            session_id=data.get("session_id"),
            source_channel_id=data.get("source_channel_id") or "",
            reply_id=data.get("reply_id"),
        )
