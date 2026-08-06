"""TaskScheduler 数量上限用例（MAX_TASKS_PER_USER）。

用 MANUAL 触发规避 croniter 依赖，只验证「新任务」在达上限时被拒、
更新既有任务（同 task_id）不受上限拦截。
"""
import json

import pytest

from pandapal.storage.models import TaskDefinition
from pandapal.task_scheduler.task_scheduler import TaskScheduler, MAX_TASKS_PER_USER

MANUAL_RULE = json.dumps({"trigger_type": "manual"})


class _FakeTaskRepo:
    def __init__(self) -> None:
        self.saved: dict[str, TaskDefinition] = {}

    async def find_task_definitions_by_user(self, user_id: str) -> list[TaskDefinition]:
        return [d for d in self.saved.values() if d.user_id == user_id]

    async def save_task_definition(self, definition: TaskDefinition) -> None:
        self.saved[definition.task_id] = definition


def _mgr() -> tuple[TaskScheduler, _FakeTaskRepo]:
    repo = _FakeTaskRepo()
    mgr = TaskScheduler(
        task_repo=repo,
        broadcast=object(),
        router=object(),
        config_manager=object(),
    )
    return mgr, repo


def _make_def(task_id: str, user_id: str = "alice", name: str | None = None) -> TaskDefinition:
    return TaskDefinition(
        task_id=task_id,
        user_id=user_id,
        name=name or task_id,
        trigger_rule_json=MANUAL_RULE,
        task_prompt="do something",
    )


@pytest.mark.asyncio
async def test_register_blocked_at_task_limit():
    mgr, repo = _mgr()
    for i in range(MAX_TASKS_PER_USER):
        await mgr.register_task_definition(_make_def(f"t{i:03d}"))
    assert len(repo.saved) == MAX_TASKS_PER_USER

    with pytest.raises(ValueError, match="数量上限"):
        await mgr.register_task_definition(_make_def("overflow"))
    # 溢出任务未落库
    assert "overflow" not in repo.saved


@pytest.mark.asyncio
async def test_update_existing_task_not_blocked_at_limit():
    """达上限后，更新同 task_id 的既有任务应放行（视为更新而非新增）。"""
    mgr, repo = _mgr()
    for i in range(MAX_TASKS_PER_USER):
        await mgr.register_task_definition(_make_def(f"t{i:03d}"))

    # 更新已存在的 t000（同 task_id，改 prompt）
    updated = TaskDefinition(
        task_id="t000", user_id="alice", name="t000",
        trigger_rule_json=MANUAL_RULE, task_prompt="updated prompt",
    )
    await mgr.register_task_definition(updated)  # 不应抛异常
    assert repo.saved["t000"].task_prompt == "updated prompt"
    assert len(repo.saved) == MAX_TASKS_PER_USER
