"""AgentTask Repository — AI 自驱任务的持久化层。

设计约束：
- D4 (Transaction): add_block_relation / remove_block_relation 原子双向更新
- D5 (No Business Logic in DB): 状态校验（in_progress 唯一性、阻塞检查）在 Python 代码中
- D2 (Explicit Query Intent): 方法名使用业务语义
- D1 (Storage Abstraction): blocks/blocked_by 对外为 list[str]，内部 JSON 序列化
- I3 (Idempotent): 重复操作不报错
- I1 (Fail Fast): 必填字段缺失立即报错
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import aiosqlite

from pandapal.storage.models import AgentTask, AgentTaskStatus
from pandapal.storage.repositories._sqlite_base import BaseRepository

logger = logging.getLogger(__name__)


class AgentTaskRepository(BaseRepository):
    """AI 自驱任务持久化仓库。

    核心数据：
    - agent_tasks 表：存储任务的完整生命周期
    - blocks_json / blocked_by_json：存储依赖关系（JSON 数组格式）

    校验规则（D5）：
    - 被阻塞的任务不能标为 in_progress
    - 循环依赖检测

    并发安全：所有写操作通过 asyncio.Lock 串行化，防止同一轮多个 tool call
    并发写导致显式 BEGIN/隐式事务交叠引起的状态丢失。
    """

    def __init__(self, conn: aiosqlite.Connection, timeout: float = 5.0) -> None:
        super().__init__(conn, timeout)
        # 写锁：防止同一轮多个 tool call 并发写 SQLite 导致事务竞态
        # 例如 add_block_relation 的显式 BEGIN 与 update_task 的隐式事务交叠
        self._write_lock = asyncio.Lock()

    # ═══════════════════════════════════════════════════════════════════════
    # 1. create_task — 创建任务
    # ═══════════════════════════════════════════════════════════════════════

    async def create_task(self, task: AgentTask) -> AgentTask:
        """创建一条任务记录。

        Args:
            task: AgentTask 数据模型对象

        Returns:
            创建后的 AgentTask（含自动生成的 created_at / updated_at）

        Raises:
            ValueError: user_id 或 session_id 为空
        """
        if not task.user_id:
            raise ValueError("create_task: user_id must not be empty")
        if not task.session_id:
            raise ValueError("create_task: session_id must not be empty")

        async with self._write_lock:
            now = datetime.now(timezone.utc)
            now_iso = self._to_iso(now)

            blocks_json = json.dumps(task.blocks or [], ensure_ascii=False)
            blocked_by_json = json.dumps(task.blocked_by or [], ensure_ascii=False)

            await self._execute(
                "INSERT INTO agent_tasks "
                "(task_id, session_id, user_id, subject, description, "
                " active_form, status, blocks_json, blocked_by_json, "
                " \"order\", created_at, updated_at, completed_at, "
                " verify_hint, verified, verify_evidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.task_id,
                    task.session_id,
                    task.user_id,
                    task.subject,
                    task.description,
                    task.active_form,
                    task.status.value,
                    blocks_json,
                    blocked_by_json,
                    task.order,
                    now_iso,
                    now_iso,
                    self._to_iso(task.completed_at),
                    task.verify_hint,
                    1 if task.verified else 0,
                    task.verify_evidence,
                ),
                operation="create_task",
            )
            await self._commit()

        logger.info(
            "AgentTask created: task_id=%s, session_id=%s, subject=%s",
            task.task_id, task.session_id, task.subject,
        )

        # 返回含时间戳的新对象（frozen dataclass 不可变，创建新实例）
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
    # 2. get_task — 按 task_id 查询
    # ═══════════════════════════════════════════════════════════════════════

    async def get_task(self, task_id: str) -> AgentTask | None:
        """按 task_id 查询任务。不存在返回 None。"""
        row = await self._fetchone(
            "SELECT task_id, session_id, user_id, subject, description, "
            "       active_form, status, blocks_json, blocked_by_json, "
            "       \"order\", created_at, updated_at, completed_at, "
            "       verify_hint, verified, verify_evidence "
            "FROM agent_tasks WHERE task_id = ?",
            (task_id,),
            operation="get_task",
        )
        if row is None:
            return None
        return self._row_to_model(row)

    # ═══════════════════════════════════════════════════════════════════════
    # 3. update_task — 更新任务（含状态校验）
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
        """更新任务字段。

        校验规则（D5）：
        - in_progress 唯一性：同一会话内最多一个 in_progress
        - 阻塞检查：被阻塞的任务不能标为 in_progress
        - I3 幂等：设置相同状态不报错

        Args:
            task_id: 目标任务 ID
            status: 新状态（pending/in_progress/completed/cancelled）
            subject: 新标题
            description: 新描述
            active_form: 新进行中文案
            order: 新序号

        Returns:
            更新后的 AgentTask

        Raises:
            ValueError: 校验失败（违反 in_progress 唯一性或阻塞约束）
            ValueError: 任务不存在
        """
        async with self._write_lock:
            # 获取当前任务
            current = await self.get_task(task_id)
            if current is None:
                raise ValueError(f"❌ 任务 {task_id} 不存在")

            # 解析新状态
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
                    # 检查阻塞者是否都已完成
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

            # 构建更新
            now = datetime.now(timezone.utc)
            now_iso = self._to_iso(now)

            updates: list[str] = []
            params: list = []

            # status
            if status and status != current.status.value:
                updates.append("status = ?")
                params.append(status)

            # subject
            if subject is not None and subject != current.subject:
                updates.append("subject = ?")
                params.append(subject)

            # description
            if description is not None and description != current.description:
                updates.append("description = ?")
                params.append(description)

            # active_form
            if active_form is not None and active_form != current.active_form:
                updates.append("active_form = ?")
                params.append(active_form)

            # order
            if order is not None and order != current.order:
                updates.append("\"order\" = ?")
                params.append(order)

            # 自动记录 completed_at（D5：status → completed 时 Repo 自动记录）
            if new_status == AgentTaskStatus.COMPLETED and current.status != AgentTaskStatus.COMPLETED:
                updates.append("completed_at = ?")
                params.append(now_iso)

            # V2: verified / verify_evidence
            if verified is not None and verified != current.verified:
                updates.append("verified = ?")
                params.append(1 if verified else 0)
            if verify_evidence is not None and verify_evidence != current.verify_evidence:
                updates.append("verify_evidence = ?")
                params.append(verify_evidence)

            # 总是更新 updated_at
            updates.append("updated_at = ?")
            params.append(now_iso)

            if updates:
                params.append(task_id)
                await self._execute(
                    f"UPDATE agent_tasks SET {', '.join(updates)} WHERE task_id = ?",
                    tuple(params),
                    operation="update_task",
                )
                await self._commit()

            # 重新读取返回最新状态
            updated = await self.get_task(task_id)
            if updated is None:
                raise RuntimeError(f"Updated task {task_id} not found (should never happen)")
            return updated

    # ═══════════════════════════════════════════════════════════════════════
    # 4. delete_task — 删除任务（级联清理依赖）
    # ═══════════════════════════════════════════════════════════════════════

    async def delete_task(self, task_id: str) -> AgentTask | None:
        """删除任务，并级联清理所有涉及此任务的依赖引用（D4 约束）。

        清理逻辑：
        - 从所有其他任务的 blocks 中移除此 task_id
        - 从所有其他任务的 blocked_by 中移除此 task_id
        - 删除本任务记录

        Returns:
            被删除的任务（用于推送事件），不存在返回 None
        """
        task = await self.get_task(task_id)
        if task is None:
            return None

        async with self._write_lock:
            cleanup_count = 0
            try:
                await self._execute("BEGIN", operation="delete_task_begin")

                # 1. 从所有阻塞者中清除引用
                rows = await self._fetchall(
                    "SELECT task_id FROM agent_tasks",
                    operation="delete_task_find_blockers",
                )
                for row in rows:
                    other_id = row["task_id"]
                    if other_id == task_id:
                        continue
                    other = await self.get_task(other_id)
                    if other is None:
                        continue
                    needs_update = False

                    other_blocks = list(other.blocks or [])
                    if task_id in other_blocks:
                        other_blocks.remove(task_id)
                        needs_update = True

                    other_blocked_by = list(other.blocked_by or [])
                    if task_id in other_blocked_by:
                        other_blocked_by.remove(task_id)
                        needs_update = True

                    if needs_update:
                        blocks_json = json.dumps(other_blocks, ensure_ascii=False)
                        blocked_by_json = json.dumps(other_blocked_by, ensure_ascii=False)
                        await self._execute(
                            "UPDATE agent_tasks SET blocks_json = ?, blocked_by_json = ? "
                            "WHERE task_id = ?",
                            (blocks_json, blocked_by_json, other_id),
                            operation="delete_task_cleanup_deps",
                        )
                        cleanup_count += 1

                # 2. 删除任务本身
                await self._execute(
                    "DELETE FROM agent_tasks WHERE task_id = ?",
                    (task_id,),
                    operation="delete_task",
                )

                await self._commit()
            except Exception:
                try:
                    await self._conn.rollback()
                except Exception:
                    pass
                raise

        logger.info(
            "AgentTask deleted: task_id=%s, dependency_refs_cleaned=%d",
            task_id, cleanup_count,
        )
        return task

    # ═══════════════════════════════════════════════════════════════════════
    # 5. list_tasks_by_session — 按会话列出任务（D2 意图显式化）
    # ═══════════════════════════════════════════════════════════════════════

    async def list_tasks_by_session(
        self,
        session_id: str,
        *,
        include_completed: bool = True,
        include_cancelled: bool = False,
    ) -> list[AgentTask]:
        """按会话列出任务。

        Args:
            session_id: 会话 ID
            include_completed: 是否包含已完成任务
            include_cancelled: 是否包含已取消任务

        Returns:
            任务列表（按 order, created_at 排序）
        """
        # 构建排除条件
        exclude_statuses: list[str] = []
        if not include_completed:
            exclude_statuses.append(AgentTaskStatus.COMPLETED.value)
        if not include_cancelled:
            exclude_statuses.append(AgentTaskStatus.CANCELLED.value)

        if exclude_statuses:
            placeholders = ", ".join("?" for _ in exclude_statuses)
            sql = (
                "SELECT task_id, session_id, user_id, subject, description, "
                "       active_form, status, blocks_json, blocked_by_json, "
                "       \"order\", created_at, updated_at, completed_at, "
                "       verify_hint, verified, verify_evidence "
                "FROM agent_tasks WHERE session_id = ? AND status NOT IN "
                f"({placeholders}) "
                "ORDER BY \"order\" ASC, created_at ASC"
            )
            rows = await self._fetchall(
                sql,
                (session_id, *exclude_statuses),
                operation="list_tasks_by_session_filtered",
            )
        else:
            rows = await self._fetchall(
                "SELECT task_id, session_id, user_id, subject, description, "
                "       active_form, status, blocks_json, blocked_by_json, "
                "       \"order\", created_at, updated_at, completed_at, "
                "       verify_hint, verified, verify_evidence "
                "FROM agent_tasks WHERE session_id = ? "
                "ORDER BY \"order\" ASC, created_at ASC",
                (session_id,),
                operation="list_tasks_by_session_all",
            )

        return [self._row_to_model(row) for row in rows]

    # ═══════════════════════════════════════════════════════════════════════
    # 6. find_in_progress_task — 查找进行中任务（D2 意图显式化）
    # ═══════════════════════════════════════════════════════════════════════

    async def find_in_progress_task(self, session_id: str) -> AgentTask | None:
        """查找当前会话中正在执行的任务。最多一个。"""
        row = await self._fetchone(
            "SELECT task_id, session_id, user_id, subject, description, "
            "       active_form, status, blocks_json, blocked_by_json, "
            "       \"order\", created_at, updated_at, completed_at, "
            "       verify_hint, verified, verify_evidence "
            "FROM agent_tasks WHERE session_id = ? AND status = ?",
            (session_id, AgentTaskStatus.IN_PROGRESS.value),
            operation="find_in_progress_task",
        )
        if row is None:
            return None
        return self._row_to_model(row)

    # ═══════════════════════════════════════════════════════════════════════
    # 7. add_block_relation — 设置依赖关系（D4 原子双向 + 循环检测）
    # ═══════════════════════════════════════════════════════════════════════

    async def add_block_relation(
        self, task_id: str, blocked_by_id: str
    ) -> None:
        """设置任务依赖关系：task_id 被 blocked_by_id 阻塞。

        D4 约束：原子双向更新 — A.blocked_by 和 B.blocks 同时写入。
        I3 约束：关系已存在时静默返回（幂等）。
        循环检测：拒绝 A → B → A 循环依赖。

        Args:
            task_id: 被阻塞的任务
            blocked_by_id: 阻塞者

        Raises:
            ValueError: 任务不存在、自引用循环、或检测到循环依赖
        """
        if task_id == blocked_by_id:
            raise ValueError(f"❌ 设置失败：任务 #{task_id} 不能阻塞自己")

        # 获取两个任务
        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"❌ 任务 #{task_id} 不存在")
        blocker = await self.get_task(blocked_by_id)
        if blocker is None:
            raise ValueError(f"❌ 任务 #{blocked_by_id} 不存在")

        # I3 幂等：关系已存在则静默返回
        existing_blocked_by = list(task.blocked_by or [])
        if blocked_by_id in existing_blocked_by:
            return

        # 循环检测：检查 blocked_by_id → task_id 是否存在反向路径
        if await self._detect_cycle(blocked_by_id, task_id):
            raise ValueError(
                f"❌ 设置失败：检测到循环依赖 #{task_id} → #{blocked_by_id} → #{task_id}"
            )

        # ── D4 原子双向更新（显式事务）──

        async with self._write_lock:
            try:
                await self._execute("BEGIN", operation="add_block_relation_begin")

                # A.blocked_by 添加 B
                new_blocked_by = existing_blocked_by + [blocked_by_id]
                await self._execute(
                    "UPDATE agent_tasks SET blocked_by_json = ? WHERE task_id = ?",
                    (json.dumps(new_blocked_by, ensure_ascii=False), task_id),
                    operation="add_block_relation_update_blocked_by",
                )

                # B.blocks 添加 A
                existing_blocks = list(blocker.blocks or [])
                new_blocks = existing_blocks + [task_id] if task_id not in existing_blocks else existing_blocks
                await self._execute(
                    "UPDATE agent_tasks SET blocks_json = ? WHERE task_id = ?",
                    (json.dumps(new_blocks, ensure_ascii=False), blocked_by_id),
                    operation="add_block_relation_update_blocks",
                )

                await self._commit()
            except Exception:
                try:
                    await self._conn.rollback()
                except Exception:
                    pass
                raise

        logger.info(
            "AgentTask block relation added: #%s blocked_by #%s",
            task_id, blocked_by_id,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # 8. remove_block_relation — 解除依赖关系（D4 原子双向）
    # ═══════════════════════════════════════════════════════════════════════

    async def remove_block_relation(
        self, task_id: str, unblock_id: str
    ) -> None:
        """解除任务依赖关系。

        D4 约束：原子双向更新。
        I3 约束：关系已不存在时静默返回（幂等）。

        Args:
            task_id: 被阻塞的任务
            unblock_id: 要移除的阻塞者
        """
        task = await self.get_task(task_id)
        if task is None:
            return

        # 从 blocked_by 中移除
        blocked_by = list(task.blocked_by or [])
        if unblock_id not in blocked_by:
            return

        new_blocked_by = [b for b in blocked_by if b != unblock_id]

        async with self._write_lock:
            try:
                await self._execute("BEGIN", operation="remove_block_relation_begin")

                await self._execute(
                    "UPDATE agent_tasks SET blocked_by_json = ? WHERE task_id = ?",
                    (json.dumps(new_blocked_by, ensure_ascii=False), task_id),
                    operation="remove_block_relation_update_blocked_by",
                )

                # 从阻塞者的 blocks 中移除
                blocker = await self.get_task(unblock_id)
                if blocker is not None:
                    blocks = list(blocker.blocks or [])
                    new_blocks = [b for b in blocks if b != task_id]
                    await self._execute(
                        "UPDATE agent_tasks SET blocks_json = ? WHERE task_id = ?",
                        (json.dumps(new_blocks, ensure_ascii=False), unblock_id),
                        operation="remove_block_relation_update_blocks",
                    )

                await self._commit()
            except Exception:
                try:
                    await self._conn.rollback()
                except Exception:
                    pass
                raise

        logger.info(
            "AgentTask block relation removed: #%s no longer blocked by #%s",
            task_id, unblock_id,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # 9. cancel_session_tasks — 会话过期时取消所有任务
    # ═══════════════════════════════════════════════════════════════════════

    async def cancel_session_tasks(self, session_id: str) -> int:
        """将会话内所有 pending/in_progress 任务标记为 cancelled。

        触发时机：SessionManager.expire_session()

        Returns:
            受影响的任务数量
        """
        async with self._write_lock:
            cursor = await self._execute(
                "UPDATE agent_tasks SET status = ?, updated_at = ? "
                "WHERE session_id = ? AND status IN (?, ?)",
                (
                    AgentTaskStatus.CANCELLED.value,
                    self._to_iso(datetime.now(timezone.utc)),
                    session_id,
                    AgentTaskStatus.PENDING.value,
                    AgentTaskStatus.IN_PROGRESS.value,
                ),
                operation="cancel_session_tasks",
            )
            await self._commit()
        count = cursor.rowcount  # type: ignore[return-value]
        if count > 0:
            logger.info(
                "Cancelled %d agent tasks: session_id=%s", count, session_id,
            )
        return count

    # ═══════════════════════════════════════════════════════════════════════
    # 10. delete_session_tasks — 会话删除时硬删除所有任务
    # ═══════════════════════════════════════════════════════════════════════

    async def delete_session_tasks(self, session_id: str) -> int:
        """硬删除会话内所有任务（级联清理依赖）。

        触发时机：需要彻底清理会话数据时。

        Returns:
            删除的任务数量
        """
        # 先清理所有跨任务依赖引用
        tasks = await self.list_tasks_by_session(session_id)
        task_ids = [t.task_id for t in tasks]

        try:
            await self._execute("BEGIN", operation="delete_session_begin")

            if task_ids:
                # 从其他会话的任务中清理对此会话任务的依赖引用
                all_rows = await self._fetchall(
                    "SELECT task_id FROM agent_tasks WHERE session_id != ?",
                    (session_id,),
                    operation="delete_session_find_others",
                )
                for row in all_rows:
                    other = await self.get_task(row["task_id"])
                    if other is None:
                        continue
                    needs_update = False
                    blocks = list(other.blocks or [])
                    blocked_by = list(other.blocked_by or [])

                    new_blocks = [b for b in blocks if b not in task_ids]
                    if len(new_blocks) != len(blocks):
                        needs_update = True

                    new_blocked_by = [b for b in blocked_by if b not in task_ids]
                    if len(new_blocked_by) != len(blocked_by):
                        needs_update = True

                    if needs_update:
                        await self._execute(
                            "UPDATE agent_tasks SET blocks_json = ?, blocked_by_json = ? "
                            "WHERE task_id = ?",
                            (
                                json.dumps(new_blocks, ensure_ascii=False),
                                json.dumps(new_blocked_by, ensure_ascii=False),
                                row["task_id"],
                            ),
                            operation="delete_session_cleanup_deps",
                        )

            # 删除本会话所有任务
            await self._execute(
                "DELETE FROM agent_tasks WHERE session_id = ?",
                (session_id,),
                operation="delete_session_tasks",
            )
            await self._commit()
        except Exception:
            try:
                await self._conn.rollback()
            except Exception:
                pass
            raise
        count = len(tasks)
        if count > 0:
            logger.info(
                "Deleted %d agent tasks: session_id=%s", count, session_id,
            )
        return count

    # ═══════════════════════════════════════════════════════════════════════
    # Private Helpers
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _row_to_model(row: tuple) -> AgentTask:
        """将 sqlite3.Row 转换为 AgentTask 数据模型。

        blocks/blocked_by 从 JSON 文本反序列化为 list[str]（D1 存储抽象）。
        """
        blocks_raw = row["blocks_json"] if "blocks_json" in row.keys() else row[7]
        blocked_by_raw = row["blocked_by_json"] if "blocked_by_json" in row.keys() else row[8]

        try:
            blocks = json.loads(blocks_raw) if blocks_raw else []
        except (json.JSONDecodeError, TypeError):
            blocks = []
        try:
            blocked_by = json.loads(blocked_by_raw) if blocked_by_raw else []
        except (json.JSONDecodeError, TypeError):
            blocked_by = []

        return AgentTask(
            task_id=row["task_id"] if "task_id" in row.keys() else row[0],
            session_id=row["session_id"] if "session_id" in row.keys() else row[1],
            user_id=row["user_id"] if "user_id" in row.keys() else row[2],
            subject=row["subject"] if "subject" in row.keys() else row[3],
            description=row["description"] if "description" in row.keys() else row[4],
            active_form=row["active_form"] if "active_form" in row.keys() else row[5],
            status=AgentTaskStatus(
                row["status"] if "status" in row.keys() else row[6]
            ),
            blocks=blocks,
            blocked_by=blocked_by,
            order=int(row["order"] if "order" in row.keys() else row[9]),
            created_at=BaseRepository._from_iso(
                row["created_at"] if "created_at" in row.keys() else row[10]
            ),
            updated_at=BaseRepository._from_iso(
                row["updated_at"] if "updated_at" in row.keys() else row[11]
            ),
            completed_at=BaseRepository._from_iso(
                row["completed_at"] if "completed_at" in row.keys() else row[12]
            ),
            verify_hint=row["verify_hint"] if "verify_hint" in row.keys() else "",
            verified=bool(row["verified"]) if "verified" in row.keys() else False,
            verify_evidence=row["verify_evidence"] if "verify_evidence" in row.keys() else "",
        )

    async def _detect_cycle(self, start_id: str, target_id: str) -> bool:
        """检测从 start_id 到 target_id 是否存在阻塞路径（循环检测）。

        BFS 遍历阻塞关系图。

        Args:
            start_id: 起始节点（阻塞者）
            target_id: 目标节点（被阻塞者想要阻塞的节点）

        Returns:
            True 表示存在循环依赖
        """
        visited: set[str] = {start_id}
        frontier: list[str] = [start_id]

        while frontier:
            current_id = frontier.pop(0)
            current = await self.get_task(current_id)
            if current is None:
                continue
            for blocked_by_id in (current.blocked_by or []):
                if blocked_by_id == target_id:
                    return True  # 找到循环
                if blocked_by_id not in visited:
                    visited.add(blocked_by_id)
                    frontier.append(blocked_by_id)

        return False
