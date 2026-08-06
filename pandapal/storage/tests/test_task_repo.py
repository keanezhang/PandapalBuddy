"""TaskRepository 测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pandapal.storage.models import (
    TaskDefinition,
    TaskExecution,
    TaskExecutionStatus,
)


@pytest.mark.asyncio
async def test_save_and_find_task_definition(memory_storage):
    """保存并查找 TaskDefinition。"""
    repo = memory_storage.get_task_repo()
    td = TaskDefinition(
        task_id="t1",
        user_id="u1",
        name="Daily Report",
        trigger_rule_json='{"type":"cron","expr":"0 9 * * *"}',
        task_prompt="Generate daily report",
        sensitivity="low",
    )
    await repo.save_task_definition(td)
    found = await repo.find_task_definition("t1")

    assert found is not None
    assert found.task_id == "t1"
    assert found.name == "Daily Report"
    assert found.sensitivity == "low"


@pytest.mark.asyncio
async def test_find_task_definitions_by_user(memory_storage):
    """按 user_id 批量查找。"""
    repo = memory_storage.get_task_repo()
    for i in range(3):
        await repo.save_task_definition(TaskDefinition(
            task_id=f"t{i}",
            user_id="u1",
            name=f"Task {i}",
            trigger_rule_json="{}",
            task_prompt=f"Do task {i}",
        ))

    results = await repo.find_task_definitions_by_user("u1")
    assert len(results) == 3


@pytest.mark.asyncio
async def test_delete_task_definition_cascades(memory_storage):
    """删除 TaskDefinition 级联删除关联的 TaskExecution。"""
    repo = memory_storage.get_task_repo()

    await repo.save_task_definition(TaskDefinition(
        task_id="t1", user_id="u1", name="Task 1",
        trigger_rule_json="{}", task_prompt="Do it",
    ))
    await repo.save_task_execution(TaskExecution(
        execution_id="e1", task_id="t1", user_id="u1",
    ))

    # 删除定义
    await repo.delete_task_definition("t1")

    assert await repo.find_task_definition("t1") is None
    assert await repo.find_task_execution("e1") is None


@pytest.mark.asyncio
async def test_save_and_find_task_execution(memory_storage):
    """保存并查找 TaskExecution。"""
    repo = memory_storage.get_task_repo()
    now = datetime.now(timezone.utc)

    execution = TaskExecution(
        execution_id="e1",
        task_id="t1",
        user_id="u1",
        status=TaskExecutionStatus.RUNNING,
        started_at=now,
    )
    await repo.save_task_execution(execution)
    found = await repo.find_task_execution("e1")

    assert found is not None
    assert found.status == TaskExecutionStatus.RUNNING
    assert found.started_at == now


@pytest.mark.asyncio
async def test_find_pending_task_executions(memory_storage):
    """查找待执行的任务（pending + running）。"""
    repo = memory_storage.get_task_repo()

    await repo.save_task_execution(TaskExecution(
        execution_id="e1", task_id="t1", user_id="u1",
        status=TaskExecutionStatus.PENDING,
    ))
    await repo.save_task_execution(TaskExecution(
        execution_id="e2", task_id="t1", user_id="u1",
        status=TaskExecutionStatus.RUNNING,
    ))
    await repo.save_task_execution(TaskExecution(
        execution_id="e3", task_id="t1", user_id="u1",
        status=TaskExecutionStatus.COMPLETED,
    ))

    pending = await repo.find_pending_task_executions("u1")
    assert len(pending) == 2
    ids = {e.execution_id for e in pending}
    assert "e1" in ids and "e2" in ids


@pytest.mark.asyncio
async def test_update_task_execution_status(memory_storage):
    """更新执行状态。"""
    repo = memory_storage.get_task_repo()

    await repo.save_task_execution(TaskExecution(
        execution_id="e1", task_id="t1", user_id="u1",
        status=TaskExecutionStatus.PENDING,
    ))
    await repo.update_task_execution_status("e1", TaskExecutionStatus.COMPLETED)

    found = await repo.find_task_execution("e1")
    assert found is not None
    assert found.status == TaskExecutionStatus.COMPLETED
