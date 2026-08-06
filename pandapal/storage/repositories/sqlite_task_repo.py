"""Task Repository 实现。

管理 TaskDefinition 和 TaskExecution 两个实体。
D2: 显式查询意图 — find_pending_task_executions, find_task_definitions_by_user,
    find_all_pending_task_executions（跨用户恢复）, find_all_task_definitions（全量加载）。
D4: delete_task_definition 级联删除关联的 executions。
"""

from __future__ import annotations

import aiosqlite

from pandapal.storage.models import (
    TaskDefinition,
    TaskExecution,
    TaskExecutionStatus,
)
from pandapal.storage.repositories._sqlite_base import BaseRepository


class TaskRepository(BaseRepository):
    """任务定义与执行记录持久化操作。"""

    def __init__(self, conn: aiosqlite.Connection, timeout: float = 5.0) -> None:
        super().__init__(conn, timeout)

    # ──────────────────────────────────────────────
    # TaskDefinition CRUD
    # ──────────────────────────────────────────────

    async def save_task_definition(self, definition: TaskDefinition) -> None:
        """保存任务定义（UPSERT by task_id，幂等）。"""
        now = self._to_iso(definition.created_at) or self._now_iso()
        await self._execute(
            "INSERT OR REPLACE INTO task_definitions "
            "(task_id, user_id, name, trigger_rule_json, task_prompt, "
            "session_id, sensitivity, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                definition.task_id,
                definition.user_id,
                definition.name,
                definition.trigger_rule_json,
                definition.task_prompt,
                definition.session_id,
                definition.sensitivity,
                now,
            ),
            operation="save_task_definition",
        )
        await self._commit()

    async def find_task_definition(self, task_id: str) -> TaskDefinition | None:
        """按 task_id 查找任务定义。"""
        row = await self._fetchone(
            "SELECT task_id, user_id, name, trigger_rule_json, task_prompt, "
            "session_id, sensitivity, created_at FROM task_definitions WHERE task_id = ?",
            (task_id,),
            operation="find_task_definition",
        )
        if row is None:
            return None
        return self._row_to_task_definition(row)

    async def find_task_definitions_by_user(
        self, user_id: str
    ) -> list[TaskDefinition]:
        """按 user_id 批量查找任务定义（D3 No N+1）。"""
        rows = await self._fetchall(
            "SELECT task_id, user_id, name, trigger_rule_json, task_prompt, "
            "session_id, sensitivity, created_at FROM task_definitions WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
            operation="find_task_definitions_by_user",
        )
        return [self._row_to_task_definition(row) for row in rows]

    async def find_all_task_definitions(self) -> list[TaskDefinition]:
        """跨用户查找所有任务定义（供 TaskScheduler 启动时全量加载）。"""
        rows = await self._fetchall(
            "SELECT task_id, user_id, name, trigger_rule_json, task_prompt, "
            "session_id, sensitivity, created_at FROM task_definitions ORDER BY created_at ASC",
            operation="find_all_task_definitions",
        )
        return [self._row_to_task_definition(row) for row in rows]

    async def delete_task_definition(self, task_id: str) -> None:
        """删除任务定义，级联删除关联的执行记录（D4）。"""
        await self._execute(
            "DELETE FROM task_executions WHERE task_id = ?",
            (task_id,),
            operation="delete_task_executions_cascade",
        )
        await self._execute(
            "DELETE FROM task_definitions WHERE task_id = ?",
            (task_id,),
            operation="delete_task_definition",
        )
        await self._commit()

    # ──────────────────────────────────────────────
    # TaskExecution CRUD
    # ──────────────────────────────────────────────

    async def save_task_execution(self, execution: TaskExecution) -> None:
        """保存任务执行记录（UPSERT by execution_id，幂等）。"""
        await self._execute(
            "INSERT OR REPLACE INTO task_executions "
            "(execution_id, task_id, user_id, status, started_at, completed_at, "
            "result_json, source_channel_id, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                execution.execution_id,
                execution.task_id,
                execution.user_id,
                execution.status.value if isinstance(execution.status, TaskExecutionStatus) else execution.status,
                self._to_iso(execution.started_at),
                self._to_iso(execution.completed_at),
                execution.result_json,
                execution.source_channel_id,
                execution.error_message,
            ),
            operation="save_task_execution",
        )
        await self._commit()

    async def find_task_execution(self, execution_id: str) -> TaskExecution | None:
        """按 execution_id 查找执行记录。"""
        row = await self._fetchone(
            "SELECT execution_id, task_id, user_id, status, started_at, "
            "completed_at, result_json, source_channel_id, error_message "
            "FROM task_executions WHERE execution_id = ?",
            (execution_id,),
            operation="find_task_execution",
        )
        if row is None:
            return None
        return self._row_to_task_execution(row)

    async def find_pending_task_executions(
        self, user_id: str
    ) -> list[TaskExecution]:
        """查找用户所有未完成的执行记录（status IN pending, running）。"""
        rows = await self._fetchall(
            "SELECT execution_id, task_id, user_id, status, started_at, "
            "completed_at, result_json, source_channel_id, error_message "
            "FROM task_executions WHERE user_id = ? AND status IN (?, ?) "
            "ORDER BY started_at ASC",
            (user_id, TaskExecutionStatus.PENDING.value, TaskExecutionStatus.RUNNING.value),
            operation="find_pending_task_executions",
        )
        return [self._row_to_task_execution(row) for row in rows]

    async def find_all_pending_task_executions(self) -> list[TaskExecution]:
        """跨用户查找所有未完成执行记录（供 TaskScheduler 重启恢复用）。

        不带 user_id 过滤，对称于 find_pending_task_executions(user_id)。
        """
        rows = await self._fetchall(
            "SELECT execution_id, task_id, user_id, status, started_at, "
            "completed_at, result_json, source_channel_id, error_message "
            "FROM task_executions WHERE status IN (?, ?) ORDER BY started_at ASC",
            (TaskExecutionStatus.PENDING.value, TaskExecutionStatus.RUNNING.value),
            operation="find_all_pending_task_executions",
        )
        return [self._row_to_task_execution(row) for row in rows]

    async def update_task_execution_status(
        self, execution_id: str, status: TaskExecutionStatus
    ) -> None:
        """更新执行记录状态。"""
        await self._execute(
            "UPDATE task_executions SET status = ? WHERE execution_id = ?",
            (status.value, execution_id),
            operation="update_task_execution_status",
        )
        await self._commit()

    # ──────────────────────────────────────────────
    # Row → Model 转换
    # ──────────────────────────────────────────────

    @staticmethod
    def _row_to_task_definition(row: tuple) -> TaskDefinition:
        return TaskDefinition(
            task_id=row[0],
            user_id=row[1],
            name=row[2],
            trigger_rule_json=row[3],
            task_prompt=row[4],
            session_id=row[5],
            sensitivity=row[6],
            created_at=BaseRepository._from_iso(row[7]),
        )

    @staticmethod
    def _row_to_task_execution(row: tuple) -> TaskExecution:
        return TaskExecution(
            execution_id=row[0],
            task_id=row[1],
            user_id=row[2],
            status=TaskExecutionStatus(row[3]),
            started_at=BaseRepository._from_iso(row[4]),
            completed_at=BaseRepository._from_iso(row[5]),
            result_json=row[6],
            source_channel_id=row[7],
            error_message=row[8],
        )
