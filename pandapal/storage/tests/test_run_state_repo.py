"""RunStateRepository 测试。"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_save_and_get(memory_storage):
    """保存并获取运行状态。"""
    repo = memory_storage.get_run_state_repo()
    state_data = b'{"step": 3, "pending_tool": "web_search"}'

    await repo.save_run_state("session1", "run1", state_data)
    result = await repo.get_run_state("session1", "run1")

    assert result == state_data


@pytest.mark.asyncio
async def test_get_nonexistent_returns_none(memory_storage):
    """获取不存在的状态返回 None。"""
    repo = memory_storage.get_run_state_repo()
    assert await repo.get_run_state("no_session", "no_run") is None


@pytest.mark.asyncio
async def test_delete_then_get_returns_none(memory_storage):
    """BL7: 删除后 get 返回 None（防止二次恢复）。"""
    repo = memory_storage.get_run_state_repo()
    state_data = b"some state"

    await repo.save_run_state("s1", "r1", state_data)
    await repo.delete_run_state("s1", "r1")

    assert await repo.get_run_state("s1", "r1") is None


@pytest.mark.asyncio
async def test_delete_nonexistent_is_idempotent(memory_storage):
    """删除不存在的状态不报错（幂等）。"""
    repo = memory_storage.get_run_state_repo()
    await repo.delete_run_state("no_session", "no_run")


@pytest.mark.asyncio
async def test_upsert_overwrites(memory_storage):
    """重复 save 覆盖已有状态。"""
    repo = memory_storage.get_run_state_repo()

    await repo.save_run_state("s1", "r1", b"version1")
    await repo.save_run_state("s1", "r1", b"version2")

    result = await repo.get_run_state("s1", "r1")
    assert result == b"version2"
