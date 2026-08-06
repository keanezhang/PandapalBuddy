"""SessionRepository 测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pandapal.storage.models import Session


@pytest.mark.asyncio
async def test_save_and_find(memory_storage):
    """保存后可以按 session_id 查找。"""
    repo = memory_storage.get_session_repo()
    now = datetime.now(timezone.utc)
    session = Session(
        session_id="s1",
        user_id="u1",
        device_id="d1",
        last_active=now,
        created_at=now,
    )
    await repo.save_session(session)
    found = await repo.find_session("s1")

    assert found is not None
    assert found.session_id == "s1"
    assert found.user_id == "u1"
    assert found.device_id == "d1"


@pytest.mark.asyncio
async def test_find_nonexistent_returns_none(memory_storage):
    """查找不存在的 session 返回 None。"""
    repo = memory_storage.get_session_repo()
    assert await repo.find_session("nonexistent") is None


@pytest.mark.asyncio
async def test_find_sessions_by_user(memory_storage):
    """按 user_id 批量查找（D3 No N+1）。"""
    repo = memory_storage.get_session_repo()
    now = datetime.now(timezone.utc)

    for i in range(3):
        await repo.save_session(Session(
            session_id=f"s{i}",
            user_id="u1",
            device_id=f"d{i}",
            last_active=now + timedelta(seconds=i),
            created_at=now,
        ))
    # 另一个用户的 session
    await repo.save_session(Session(
        session_id="other",
        user_id="u2",
        device_id="d_other",
        last_active=now,
        created_at=now,
    ))

    sessions = await repo.find_sessions_by_user("u1")
    assert len(sessions) == 3
    # 按 last_active DESC 排序
    assert sessions[0].session_id == "s2"


@pytest.mark.asyncio
async def test_update_last_active(memory_storage):
    """更新 last_active 时间戳。"""
    repo = memory_storage.get_session_repo()
    now = datetime.now(timezone.utc)
    session = Session(
        session_id="s1", user_id="u1", device_id="d1",
        last_active=now, created_at=now,
    )
    await repo.save_session(session)

    new_time = now + timedelta(hours=1)
    await repo.update_session_last_active("s1", new_time)

    found = await repo.find_session("s1")
    assert found is not None
    assert found.last_active == new_time


@pytest.mark.asyncio
async def test_delete_expired_sessions(memory_storage):
    """删除过期的 sessions。"""
    repo = memory_storage.get_session_repo()
    now = datetime.now(timezone.utc)

    # 创建已过期和未过期的 sessions
    await repo.save_session(Session(
        session_id="expired",
        user_id="u1",
        device_id="d1",
        last_active=now - timedelta(hours=2),
        created_at=now - timedelta(hours=3),
    ))
    await repo.save_session(Session(
        session_id="active",
        user_id="u1",
        device_id="d2",
        last_active=now,
        created_at=now,
    ))

    deleted = await repo.delete_expired_sessions(now - timedelta(hours=1))
    assert deleted == 1
    assert await repo.find_session("expired") is None
    assert await repo.find_session("active") is not None


@pytest.mark.asyncio
async def test_delete_session(memory_storage):
    """删除指定 session（幂等）。"""
    repo = memory_storage.get_session_repo()
    now = datetime.now(timezone.utc)

    await repo.save_session(Session(
        session_id="s1", user_id="u1", device_id="d1",
        last_active=now, created_at=now,
    ))

    await repo.delete_session("s1")
    assert await repo.find_session("s1") is None

    # 再次删除不报错（幂等）
    await repo.delete_session("s1")
