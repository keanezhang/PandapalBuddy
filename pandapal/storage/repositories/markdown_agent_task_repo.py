"""Markdown AgentTask Repository 实现。

使用 Markdown 文件存储 AgentTask 数据，每个任务对应一个 .md 文件。
文件格式使用 JSON front matter 存储结构化数据。

设计约束（与 SQLite 版本一致）：
- D4 (Transaction): add/remove_block_relation 原子双向更新
- D5 (No Business Logic in File): 状态校验在 Python 代码中
- D2 (Explicit Query Intent): 方法名使用业务语义
- D1 (Storage Abstraction): blocks/blocked_by 对外为 list[str]
- I3 (Idempotent): 重复操作不报错
- I1 (Fail Fast): 必填字段缺失立即报错
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from pandapal.storage.models import AgentTask, AgentTaskStatus
from pandapal.storage.repositories._markdown_base import MarkdownBaseRepository

logger = logging.getLogger(__name__)


class MarkdownAgentTaskRepository(MarkdownBaseRepository):
    """Markdown AI 自驱任务持久化仓库。

    文件格式：
    ---
    {json data}
    ---

    # AgentTask: {task_id}
    - **subject**: ...
    - **status**: ...
    ...
    """

    def __init__(self, base_dir: str, timeout: float = 5.0) -> None:
        super().__init__(base_dir, "agent_tasks", timeout, session_partitioned=True)

    # ═══════════════════════════════════════════════════════════════════════
    # 1. create_task
    # ═══════════════════════════════════════════════════════════════════════

    async def create_task(self, task: AgentTask) -> AgentTask:
        if not task.user_id:
            raise ValueError("create_task: user_id must not be empty")
        if not task.session_id:
            raise ValueError("create_task: session_id must not be empty")

        now = datetime.now(timezone.utc)
        now_iso = self._to_iso(now)

        data = {
            "task_id": task.task_id,
            "session_id": task.session_id,
            "user_id": task.user_id,
            "subject": task.subject,
            "description": task.description,
            "active_form": task.active_form,
            "status": task.status.value,
            "blocks": json.dumps(task.blocks or [], ensure_ascii=False),
            "blocked_by": json.dumps(task.blocked_by or [], ensure_ascii=False),
            "order": task.order,
            "created_at": now_iso,
            "updated_at": now_iso,
            "completed_at": self._to_iso(task.completed_at),
            "verify_hint": task.verify_hint,
            "verified": task.verified,
            "verify_evidence": task.verify_evidence,
        }

        file_path = self._partition_path(task.session_id, task.task_id)
        title = f"AgentTask: {task.subject}"
        await self._write_entity(file_path, data, title)

        logger.info(
            "AgentTask created (markdown): task_id=%s, session_id=%s, subject=%s",
            task.task_id, task.session_id, task.subject,
        )

        return AgentTask(
            task_id=task.task_id,
            session_id=task.session_id,
            user_id=task.user_id,
            subject=task.subject,
            description=task.description,
            active_form=task.active_form,
            status=task.status,
            blocks=task.blocks,
            blocked_by=task.blocked_by,
            order=task.order,
            created_at=now,
            updated_at=now,
            completed_at=task.completed_at,
            verify_hint=task.verify_hint,
            verified=task.verified,
            verify_evidence=task.verify_evidence,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # 2. get_task
    # ═══════════════════════════════════════════════════════════════════════

    async def get_task(self, task_id: str) -> AgentTask | None:
        file_path = await self._find_path_by_id(task_id)
        if file_path is None:
            return None
        data = await self._read_entity(file_path)
        if data is None:
            return None
        return self._dict_to_model(data)

    # ═══════════════════════════════════════════════════════════════════════
    # 3. update_task（含状态校验 D5）
    # ═══════════════════════════════════════════════════════════════════════

    async def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        order: int | None = None,
        verified: bool | None = None,
        verify_evidence: str | None = None,
    ) -> AgentTask:
        return await self._update_task_impl(
            task_id,
            status=status,
            subject=subject,
            description=description,
            active_form=active_form,
            order=order,
            verified=verified,
            verify_evidence=verify_evidence,
        )

    async def _update_task_impl(
        self,
        task_id: str,
        *,
        status: str | None = None,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        order: int | None = None,
        verified: bool | None = None,
        verify_evidence: str | None = None,
    ) -> AgentTask:
        current = await self.get_task(task_id)
        if current is None:
            raise ValueError(f"❌ 任务 {task_id} 不存在")

        new_status_val = status if status else current.status.value
        try:
            new_status = AgentTaskStatus(new_status_val)
        except ValueError:
            raise ValueError(
                f"❌ 无效状态 '{new_status_val}'，"
                f"必须是 pending/in_progress/completed/cancelled 之一"
            )

        # ── D5 校验: 阻塞检查（依赖已解除后可并行执行）──
        if new_status == AgentTaskStatus.IN_PROGRESS:
            blocked_by = current.blocked_by or []
            if blocked_by:
                blockers = []
                for blocker_id in blocked_by:
                    blocker = await self.get_task(blocker_id)
                    if blocker is not None and blocker.status not in (
                        AgentTaskStatus.COMPLETED,
                        AgentTaskStatus.CANCELLED,
                    ):
                        blockers.append(blocker_id)
                if blockers:
                    ids = ", ".join(f"#{bid}" for bid in blockers)
                    raise ValueError(f"❌ 任务 #{task_id} 还被 {ids} 阻塞，不能开始")

        # 构建更新数据
        now = datetime.now(timezone.utc)
        now_iso = self._to_iso(now)
        file_path = self._partition_path(current.session_id, task_id)
        existing_data = await self._read_entity(file_path)
        if existing_data is None:
            raise ValueError(f"❌ 任务 {task_id} 不存在")

        if status:
            existing_data["status"] = status
        if subject is not None:
            existing_data["subject"] = subject
        if description is not None:
            existing_data["description"] = description
        if active_form is not None:
            existing_data["active_form"] = active_form
        if order is not None:
            existing_data["order"] = order

        if new_status == AgentTaskStatus.COMPLETED and current.status != AgentTaskStatus.COMPLETED:
            existing_data["completed_at"] = now_iso

        if verified is not None:
            existing_data["verified"] = verified
        if verify_evidence is not None:
            existing_data["verify_evidence"] = verify_evidence

        existing_data["updated_at"] = now_iso

        title = f"AgentTask: {existing_data.get('subject', task_id)}"
        await self._write_entity(file_path, existing_data, title)

        return await self.get_task(task_id)  # type: ignore[return-value]

    # ═══════════════════════════════════════════════════════════════════════
    # 4. delete_task（级联清理依赖 D4）
    # ═══════════════════════════════════════════════════════════════════════

    async def delete_task(self, task_id: str) -> AgentTask | None:
        task = await self.get_task(task_id)
        if task is None:
            return None

        # 级联清理：遍历所有任务，移除对该任务的引用
        all_entities = await self._list_entities()
        cleanup_count = 0
        for data in all_entities:
            other_id = data.get("task_id", "")
            if other_id == task_id:
                continue
            needs_update = False

            blocks = json.loads(data.get("blocks", "[]"))
            if task_id in blocks:
                blocks.remove(task_id)
                needs_update = True

            blocked_by = json.loads(data.get("blocked_by", "[]"))
            if task_id in blocked_by:
                blocked_by.remove(task_id)
                needs_update = True

            if needs_update:
                data["blocks"] = json.dumps(blocks, ensure_ascii=False)
                data["blocked_by"] = json.dumps(blocked_by, ensure_ascii=False)
                fp = self._partition_path(data.get("session_id", ""), other_id)
                title = f"AgentTask: {data.get('subject', other_id)}"
                await self._write_entity(fp, data, title)
                cleanup_count += 1

        # 删除任务文件
        file_path = self._partition_path(task.session_id, task_id)
        await self._delete_entity(file_path)

        logger.info(
            "AgentTask deleted (markdown): task_id=%s, dependency_refs_cleaned=%d",
            task_id, cleanup_count,
        )
        return task

    # ═══════════════════════════════════════════════════════════════════════
    # 5. list_tasks_by_session（D2 意图显式化）
    # ═══════════════════════════════════════════════════════════════════════

    async def list_tasks_by_session(
        self,
        session_id: str,
        *,
        include_completed: bool = True,
        include_cancelled: bool = False,
    ) -> list[AgentTask]:
        all_entities = await self._list_entities()
        tasks: list[AgentTask] = []

        for data in all_entities:
            if data.get("session_id") != session_id:
                continue
            status_val = data.get("status", "")
            if not include_completed and status_val == AgentTaskStatus.COMPLETED.value:
                continue
            if not include_cancelled and status_val == AgentTaskStatus.CANCELLED.value:
                continue
            tasks.append(self._dict_to_model(data))

        tasks.sort(key=lambda t: (t.order, t.created_at or datetime.min.replace(tzinfo=timezone.utc)))
        return tasks

    # ═══════════════════════════════════════════════════════════════════════
    # 6. find_in_progress_task（D2 意图显式化）
    # ═══════════════════════════════════════════════════════════════════════

    async def find_in_progress_task(self, session_id: str) -> AgentTask | None:
        all_entities = await self._list_entities()
        for data in all_entities:
            if data.get("session_id") == session_id and data.get("status") == AgentTaskStatus.IN_PROGRESS.value:
                return self._dict_to_model(data)
        return None

    # ═══════════════════════════════════════════════════════════════════════
    # 7. add_block_relation（D4 原子双向 + 循环检测）
    # ═══════════════════════════════════════════════════════════════════════

    async def add_block_relation(
        self, task_id: str, blocked_by_id: str
    ) -> None:
        if task_id == blocked_by_id:
            raise ValueError(f"❌ 设置失败：任务 #{task_id} 不能阻塞自己")

        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"❌ 任务 #{task_id} 不存在")
        blocker = await self.get_task(blocked_by_id)
        if blocker is None:
            raise ValueError(f"❌ 任务 #{blocked_by_id} 不存在")

        # I3 幂等
        existing_blocked_by = list(task.blocked_by or [])
        if blocked_by_id in existing_blocked_by:
            return

        # 循环检测
        if await self._detect_cycle(blocked_by_id, task_id):
            raise ValueError(
                f"❌ 设置失败：检测到循环依赖 #{task_id} → #{blocked_by_id} → #{task_id}"
            )

        # ── D4 原子双向更新 ──
        task_fp = self._partition_path(task.session_id, task_id)
        blocker_fp = self._partition_path(blocker.session_id, blocked_by_id)

        task_data = await self._read_entity(task_fp)
        blocker_data = await self._read_entity(blocker_fp)
        if task_data is None or blocker_data is None:
            raise ValueError("❌ 任务数据读取失败")

        # A.blocked_by 添加 B
        task_blocked_by = json.loads(task_data.get("blocked_by", "[]"))
        task_blocked_by.append(blocked_by_id)
        task_data["blocked_by"] = json.dumps(task_blocked_by, ensure_ascii=False)

        # B.blocks 添加 A
        blocker_blocks = json.loads(blocker_data.get("blocks", "[]"))
        if task_id not in blocker_blocks:
            blocker_blocks.append(task_id)
        blocker_data["blocks"] = json.dumps(blocker_blocks, ensure_ascii=False)

        # 写入两个文件
        await self._write_entity(task_fp, task_data, f"AgentTask: {task_data.get('subject', '')}")
        await self._write_entity(blocker_fp, blocker_data, f"AgentTask: {blocker_data.get('subject', '')}")

        logger.info(
            "AgentTask block relation added (markdown): #%s blocked_by #%s",
            task_id, blocked_by_id,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # 8. remove_block_relation（D4 原子双向）
    # ═══════════════════════════════════════════════════════════════════════

    async def remove_block_relation(
        self, task_id: str, unblock_id: str
    ) -> None:
        task = await self.get_task(task_id)
        if task is None:
            return

        blocked_by = list(task.blocked_by or [])
        if unblock_id not in blocked_by:
            return

        task_fp = self._partition_path(task.session_id, task_id)
        blocker_fp = await self._find_path_by_id(unblock_id)

        task_data = await self._read_entity(task_fp)
        if task_data is None:
            return

        # A.blocked_by 移除 B
        task_blocked_by = json.loads(task_data.get("blocked_by", "[]"))
        task_blocked_by = [b for b in task_blocked_by if b != unblock_id]
        task_data["blocked_by"] = json.dumps(task_blocked_by, ensure_ascii=False)

        await self._write_entity(task_fp, task_data, f"AgentTask: {task_data.get('subject', '')}")

        # B.blocks 移除 A
        blocker_data = await self._read_entity(blocker_fp) if blocker_fp else None
        if blocker_fp is not None and blocker_data is not None:
            blocker_blocks = json.loads(blocker_data.get("blocks", "[]"))
            blocker_blocks = [b for b in blocker_blocks if b != task_id]
            blocker_data["blocks"] = json.dumps(blocker_blocks, ensure_ascii=False)
            await self._write_entity(blocker_fp, blocker_data, f"AgentTask: {blocker_data.get('subject', '')}")

        logger.info(
            "AgentTask block relation removed (markdown): #%s no longer blocked by #%s",
            task_id, unblock_id,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # 9. cancel_session_tasks
    # ═══════════════════════════════════════════════════════════════════════

    async def cancel_session_tasks(self, session_id: str) -> int:
        now_iso = self._to_iso(datetime.now(timezone.utc))
        all_entities = await self._list_entities()
        cancelled_count = 0

        for data in all_entities:
            if data.get("session_id") != session_id:
                continue
            status_val = data.get("status", "")
            if status_val in (AgentTaskStatus.PENDING.value, AgentTaskStatus.IN_PROGRESS.value):
                data["status"] = AgentTaskStatus.CANCELLED.value
                data["updated_at"] = now_iso
                fp = self._partition_path(data.get("session_id", ""), data.get("task_id", ""))
                title = f"AgentTask: {data.get('subject', '')}"
                await self._write_entity(fp, data, title)
                cancelled_count += 1

        if cancelled_count > 0:
            logger.info(
                "Cancelled %d agent tasks (markdown): session_id=%s",
                cancelled_count, session_id,
            )
        return cancelled_count

    # ═══════════════════════════════════════════════════════════════════════
    # 10. delete_session_tasks
    # ═══════════════════════════════════════════════════════════════════════

    async def delete_session_tasks(self, session_id: str) -> int:
        all_entities = await self._list_entities()
        task_ids_to_delete: list[str] = []
        tasks_to_keep: list[dict[str, Any]] = []

        for data in all_entities:
            if data.get("session_id") == session_id:
                task_ids_to_delete.append(data.get("task_id", ""))
            else:
                tasks_to_keep.append(data)

        # 级联清理：从其他任务中移除对即将删除任务的依赖引用
        for data in tasks_to_keep:
            needs_update = False
            blocks = json.loads(data.get("blocks", "[]"))
            blocked_by = json.loads(data.get("blocked_by", "[]"))

            new_blocks = [b for b in blocks if b not in task_ids_to_delete]
            if len(new_blocks) != len(blocks):
                needs_update = True

            new_blocked_by = [b for b in blocked_by if b not in task_ids_to_delete]
            if len(new_blocked_by) != len(blocked_by):
                needs_update = True

            if needs_update:
                data["blocks"] = json.dumps(new_blocks, ensure_ascii=False)
                data["blocked_by"] = json.dumps(new_blocked_by, ensure_ascii=False)
                fp = self._partition_path(data.get("session_id", ""), data.get("task_id", ""))
                title = f"AgentTask: {data.get('subject', '')}"
                await self._write_entity(fp, data, title)

        # 删除所有匹配的文件（均属于入参 session_id）
        for task_id in task_ids_to_delete:
            fp = self._partition_path(session_id, task_id)
            await self._delete_entity(fp)

        count = len(task_ids_to_delete)
        if count > 0:
            logger.info(
                "Deleted %d agent tasks (markdown): session_id=%s", count, session_id,
            )
        return count

    # ═══════════════════════════════════════════════════════════════════════
    # Private Helpers
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            try:
                dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _dict_to_model(self, data: dict[str, Any]) -> AgentTask:
        blocks_raw = data.get("blocks", "[]")
        blocked_by_raw = data.get("blocked_by", "[]")
        try:
            blocks = json.loads(blocks_raw) if blocks_raw else []
        except (json.JSONDecodeError, TypeError):
            blocks = []
        try:
            blocked_by = json.loads(blocked_by_raw) if blocked_by_raw else []
        except (json.JSONDecodeError, TypeError):
            blocked_by = []

        status_str = data.get("status", "pending")
        try:
            status = AgentTaskStatus(status_str)
        except ValueError:
            status = AgentTaskStatus.PENDING

        return AgentTask(
            task_id=data.get("task_id", ""),
            session_id=data.get("session_id", ""),
            user_id=data.get("user_id", ""),
            subject=data.get("subject", ""),
            description=data.get("description", ""),
            active_form=data.get("active_form", ""),
            status=status,
            blocks=blocks,
            blocked_by=blocked_by,
            order=int(data.get("order", 0)),
            created_at=self._parse_datetime(data.get("created_at")),
            updated_at=self._parse_datetime(data.get("updated_at")),
            completed_at=self._parse_datetime(data.get("completed_at")),
            verify_hint=data.get("verify_hint", ""),
            verified=bool(data.get("verified", False)),
            verify_evidence=data.get("verify_evidence", ""),
        )

    async def _detect_cycle(self, start_id: str, target_id: str) -> bool:
        visited: set[str] = {start_id}
        frontier: list[str] = [start_id]

        while frontier:
            current_id = frontier.pop(0)
            current = await self.get_task(current_id)
            if current is None:
                continue
            for blocked_by_id in (current.blocked_by or []):
                if blocked_by_id == target_id:
                    return True
                if blocked_by_id not in visited:
                    visited.add(blocked_by_id)
                    frontier.append(blocked_by_id)

        return False
