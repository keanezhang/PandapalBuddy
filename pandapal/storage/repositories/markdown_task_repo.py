"""Markdown Task Repository 实现。

使用 Markdown 文件存储 Task 数据。

文件命名规则：
- 任务定义: {task_id}.md（与 SQLite 版 task_definitions 表对应）
- 执行记录: {task_id}_{started_at_iso}.md（与 SQLite 版 task_executions 表对应）
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from pandapal.storage.models import TaskDefinition, TaskExecution, TaskExecutionStatus
from pandapal.storage.repositories._markdown_base import MarkdownBaseRepository

logger = __import__("logging").getLogger(__name__)


class MarkdownTaskRepository(MarkdownBaseRepository):
    """Markdown 任务持久化操作（异步接口）。

    与 SQLite TaskRepository 保持一致的 async 方法签名，
    确保 TaskScheduler 可以透明切换存储后端。
    """

    def __init__(self, base_dir: str, timeout: float = 5.0) -> None:
        super().__init__(base_dir, "tasks", timeout)

    # ──────────────────────────────────────────────
    # TaskDefinition CRUD
    # ──────────────────────────────────────────────

    async def save_task_definition(self, definition: TaskDefinition) -> None:
        """保存任务定义（UPSERT by task_id，幂等）。"""
        file_path = self._get_file_path(definition.task_id)

        data = {
            "task_id": definition.task_id,
            "user_id": definition.user_id,
            "name": definition.name,
            "trigger_rule_json": definition.trigger_rule_json,
            "task_prompt": definition.task_prompt,
            "session_id": definition.session_id,
            "sensitivity": definition.sensitivity,
            "created_at": self._to_iso(definition.created_at) or self._now_iso(),
        }

        title = f"Task: {definition.name}"
        await self._write_entity(file_path, data, title)

    async def find_task_definition(self, task_id: str) -> TaskDefinition | None:
        """按 task_id 查找任务定义。"""
        file_path = self._get_file_path(task_id)
        data = await self._read_entity(file_path)

        if data is None:
            return None

        return self._dict_to_task_definition(data)

    async def find_task_definitions_by_user(self, user_id: str) -> list[TaskDefinition]:
        """按 user_id 查找所有任务定义。"""
        entities = await self._filter_entities(user_id=user_id)
        return [self._dict_to_task_definition(data) for data in entities]

    async def find_all_task_definitions(self) -> list[TaskDefinition]:
        """查找所有任务定义（供 TaskScheduler 启动时全量加载）。"""
        entities = await self._list_entities()
        return [self._dict_to_task_definition(data) for data in entities]

    async def delete_task_definition(self, task_id: str) -> None:
        """删除任务定义，级联删除关联的执行记录（D4）。"""
        # 删除所有执行记录
        exec_files = await self._find_execution_files(task_id)
        for exec_path in exec_files:
            await self._delete_entity(exec_path)

        # 删除定义文件
        file_path = self._get_file_path(task_id)
        await self._delete_entity(file_path)

    # ──────────────────────────────────────────────
    # TaskExecution CRUD
    # ──────────────────────────────────────────────

    async def save_task_execution(self, execution: TaskExecution) -> None:
        """保存任务执行记录（UPSERT by execution_id，幂等）。

        文件名使用 execution_id（UUID），避免 task_id 冲突。
        同时保留 {task_id}_{started_at} 格式的别名链接，方便按任务查询。
        """
        # 主文件：按 execution_id 命名
        file_path = self._get_file_path(execution.execution_id)

        started_at_iso = self._to_iso(execution.started_at) or self._now_iso()

        data = {
            "execution_id": execution.execution_id,
            "task_id": execution.task_id,
            "user_id": execution.user_id,
            "status": execution.status.value if hasattr(execution.status, "value") else str(execution.status),
            "started_at": started_at_iso,
            "completed_at": self._to_iso(execution.completed_at),
            "result_json": execution.result_json,
            "source_channel_id": execution.source_channel_id,
            "error_message": execution.error_message,
        }

        title = f"Execution: {execution.task_id} @ {started_at_iso}"
        await self._write_entity(file_path, data, title)

    async def update_task_execution_status(
        self, execution_id: str, status: TaskExecutionStatus
    ) -> None:
        """更新执行记录状态（原地修改 front matter）。"""
        file_path = self._get_file_path(execution_id)
        data = await self._read_entity(file_path)

        if data is None:
            logger.warning(
                "update_task_execution_status: execution_id=%s not found", execution_id
            )
            return

        data["status"] = status.value if hasattr(status, "value") else str(status)
        if status in (TaskExecutionStatus.COMPLETED, TaskExecutionStatus.FAILED, TaskExecutionStatus.CANCELLED):
            data["completed_at"] = self._now_iso()

        title = f"Execution: {data.get('task_id', 'unknown')} @ {data.get('started_at', 'unknown')}"
        await self._write_entity(file_path, data, title)

    async def find_task_execution(self, execution_id: str) -> TaskExecution | None:
        """按 execution_id 查找执行记录。"""
        file_path = self._get_file_path(execution_id)
        data = await self._read_entity(file_path)

        if data is None:
            return None

        return self._dict_to_task_execution(data)

    async def find_task_executions(
        self, task_id: str, limit: int = 10
    ) -> list[TaskExecution]:
        """查找任务的执行历史（按 started_at 倒序）。"""
        all_data: list[dict[str, Any]] = []

        def _collect() -> None:
            try:
                for filename in os.listdir(self._entity_dir):
                    if not filename.endswith(".md"):
                        continue
                    file_path = os.path.join(self._entity_dir, filename)
                    data = self._sync_read_entity(file_path)
                    if data and data.get("task_id") == task_id:
                        all_data.append(data)
            except Exception:
                pass

        await asyncio.to_thread(_collect)

        # 按 started_at 排序（倒序）
        all_data.sort(key=lambda x: x.get("started_at", ""), reverse=True)

        return [self._dict_to_task_execution(data) for data in all_data[:limit]]

    async def find_pending_task_executions(
        self, user_id: str
    ) -> list[TaskExecution]:
        """查找用户所有未完成的执行记录（status IN pending, running）。"""
        all_data: list[dict[str, Any]] = []

        def _collect() -> None:
            try:
                for filename in os.listdir(self._entity_dir):
                    if not filename.endswith(".md"):
                        continue
                    file_path = os.path.join(self._entity_dir, filename)
                    data = self._sync_read_entity(file_path)
                    if data and data.get("user_id") == user_id and data.get("status") in ("pending", "running"):
                        all_data.append(data)
            except Exception:
                pass

        await asyncio.to_thread(_collect)

        all_data.sort(key=lambda x: x.get("started_at", ""))
        return [self._dict_to_task_execution(data) for data in all_data]

    async def find_all_pending_task_executions(self) -> list[TaskExecution]:
        """跨用户查找所有未完成执行记录（供 TaskScheduler 重启恢复）。"""
        all_data: list[dict[str, Any]] = []

        def _collect() -> None:
            try:
                for filename in os.listdir(self._entity_dir):
                    if not filename.endswith(".md"):
                        continue
                    file_path = os.path.join(self._entity_dir, filename)
                    data = self._sync_read_entity(file_path)
                    if data and data.get("status") in ("pending", "running"):
                        all_data.append(data)
            except Exception:
                pass

        await asyncio.to_thread(_collect)

        all_data.sort(key=lambda x: x.get("started_at", ""))
        return [self._dict_to_task_execution(data) for data in all_data]

    async def _find_execution_files(self, task_id: str) -> list[str]:
        """查找属于某个 task_id 的所有执行记录文件路径。"""
        result: list[str] = []

        def _collect() -> None:
            try:
                for filename in os.listdir(self._entity_dir):
                    if not filename.endswith(".md"):
                        continue
                    file_path = os.path.join(self._entity_dir, filename)
                    data = self._sync_read_entity(file_path)
                    if data and data.get("task_id") == task_id:
                        result.append(file_path)
            except Exception:
                pass

        await asyncio.to_thread(_collect)
        return result

    # ──────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _dict_to_task_definition(data: dict[str, Any]) -> TaskDefinition:
        """将字典转换为 TaskDefinition 模型。"""
        return TaskDefinition(
            task_id=data.get("task_id", ""),
            user_id=data.get("user_id", ""),
            name=data.get("name", ""),
            trigger_rule_json=data.get("trigger_rule_json") or "",
            task_prompt=data.get("task_prompt", ""),
            session_id=data.get("session_id", ""),
            sensitivity=data.get("sensitivity", "medium"),
            created_at=MarkdownTaskRepository._from_iso(data.get("created_at")),
        )

    @staticmethod
    def _dict_to_task_execution(data: dict[str, Any]) -> TaskExecution:
        """将字典转换为 TaskExecution 模型。

        兼容旧格式（run_at/result/error 字段）和新格式（execution_id/started_at/result_json/error_message）。
        """
        status_str = data.get("status", "pending")
        try:
            status = TaskExecutionStatus(status_str)
        except ValueError:
            status = TaskExecutionStatus.PENDING

        return TaskExecution(
            execution_id=data.get("execution_id", data.get("run_at", "")),
            task_id=data.get("task_id", ""),
            user_id=data.get("user_id", ""),
            status=status,
            started_at=MarkdownTaskRepository._from_iso(data.get("started_at")),
            completed_at=MarkdownTaskRepository._from_iso(data.get("completed_at")),
            result_json=data.get("result_json") or data.get("result"),
            source_channel_id=data.get("source_channel_id"),
            error_message=data.get("error_message") or data.get("error"),
        )
