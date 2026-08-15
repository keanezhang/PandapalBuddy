"""SessionManager 测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from pandapal.config.system.manager import ConfigManager
from pandapal.config.system.models import SystemConfig
from pandapal.session.exceptions import SessionExpiredError, SessionNotFoundError
from pandapal.session.manager import SessionManager
from pandapal.storage.manager import StorageManager
from pandapal.storage.models import Session


@pytest_asyncio.fixture
async def setup(tmp_path):
    """提供完整的 SessionManager + 依赖（StorageManager + ConfigManager）。"""
    # Storage
    db_path = str(tmp_path / "test.db")
    storage = StorageManager(storage_path=db_path, storage_mode="sqlite")
    await storage.initialize_storage()

    # Config（使用有效 .env.development）
    env_path = tmp_path / ".env.development"
    env_path.write_text(
        """\
PANDAPAL_RELAY_URL=wss://relay.example.com/ws
PANDAPAL_RELAY_AUTH_TOKEN=token
PANDAPAL_DATA_DIR=~/.pandapal
""",
        encoding="utf-8",
    )

    config_mgr = ConfigManager(str(tmp_path))
    await config_mgr.load_config()

    # Session Manager
    session_mgr = SessionManager(
        session_repo=storage.get_session_repo(),
        config_manager=config_mgr,
    )

    yield session_mgr, storage

    await storage.shutdown_storage()


@pytest.mark.asyncio
async def test_ensure_session_new(setup):
    """不存在时创建新会话记录。"""
    session_mgr, _ = setup
    session = await session_mgr.ensure_session("s1", "u1", "d1")

    assert session.session_id == "s1"
    assert session.user_id == "u1"
    assert session.device_id == "d1"
    assert session.last_active is not None
    assert session.created_at is not None


@pytest.mark.asyncio
async def test_ensure_session_existing(setup):
    """已存在的未过期会话原样返回（幂等）。"""
    session_mgr, _ = setup

    s1 = await session_mgr.ensure_session("s1", "u1", "d1")
    s2 = await session_mgr.ensure_session("s1", "u1", "d1")

    assert s2.session_id == s1.session_id
    assert s2.created_at == s1.created_at


@pytest.mark.asyncio
async def test_ensure_session_expired_is_nondestructive(setup):
    """★ 非破坏性：已过期的会话也原样返回，绝不删表重建（避免清空共管的元数据）。"""
    session_mgr, storage = setup

    # 手动写入一个已过期的 session（last_active 在 2 小时前）
    now = datetime.now(timezone.utc)
    old_session = Session(
        session_id="s1",
        user_id="u1",
        device_id="d1",
        last_active=now - timedelta(hours=2),
        created_at=now - timedelta(hours=3),
    )
    await storage.get_session_repo().save_session(old_session)

    # ensure_session 不管过期与否，都原样返回既有记录（不删不建）
    returned = await session_mgr.ensure_session("s1", "u1", "d1")

    assert returned.session_id == "s1"
    # created_at 与原记录一致 → 证明没有被重建
    assert returned.created_at == old_session.created_at
    # last_active 仍是 2 小时前 → 证明没被刷新/重建
    assert (now - returned.last_active).total_seconds() > 3600


@pytest.mark.asyncio
async def test_validate_session_success(setup):
    """验证有效的会话。"""
    session_mgr, _ = setup
    await session_mgr.ensure_session("s1", "u1", "d1")

    session = await session_mgr.validate_session("s1")
    assert session.session_id == "s1"


@pytest.mark.asyncio
async def test_validate_session_not_found(setup):
    """验证不存在的会话抛出 SessionNotFoundError。"""
    session_mgr, _ = setup

    with pytest.raises(SessionNotFoundError) as exc_info:
        await session_mgr.validate_session("nonexistent")

    assert exc_info.value.session_id == "nonexistent"


@pytest.mark.asyncio
async def test_validate_session_expired(setup):
    """验证已过期的会话抛出 SessionExpiredError。"""
    session_mgr, storage = setup

    now = datetime.now(timezone.utc)
    expired_session = Session(
        session_id="s1",
        user_id="u1",
        device_id="d1",
        last_active=now - timedelta(hours=2),
        created_at=now - timedelta(hours=3),
    )
    await storage.get_session_repo().save_session(expired_session)

    with pytest.raises(SessionExpiredError) as exc_info:
        await session_mgr.validate_session("s1")

    assert exc_info.value.session_id == "s1"


@pytest.mark.asyncio
async def test_refresh_session_activity(setup):
    """刷新会话活跃时间。"""
    session_mgr, storage = setup

    await session_mgr.ensure_session("s1", "u1", "d1")
    await session_mgr.refresh_session_activity("s1")

    session = await storage.get_session_repo().find_session("s1")
    assert session is not None
    # last_active 应该是很近的时间
    elapsed = datetime.now(timezone.utc) - session.last_active
    assert elapsed.total_seconds() < 5


@pytest.mark.asyncio
async def test_refresh_nonexistent_raises(setup):
    """刷新不存在的会话抛出 SessionNotFoundError。"""
    session_mgr, _ = setup

    with pytest.raises(SessionNotFoundError):
        await session_mgr.refresh_session_activity("nonexistent")


@pytest.mark.asyncio
async def test_expire_session(setup):
    """强制过期会话。"""
    session_mgr, storage = setup

    await session_mgr.ensure_session("s1", "u1", "d1")
    await session_mgr.expire_session("s1")

    assert await storage.get_session_repo().find_session("s1") is None


@pytest.mark.asyncio
async def test_expire_nonexistent_is_idempotent(setup):
    """过期不存在的会话不报错（幂等）。"""
    session_mgr, _ = setup
    await session_mgr.expire_session("nonexistent")  # 不应抛异常


@pytest.mark.asyncio
async def test_delete_expired_sessions(setup):
    """批量清理过期会话。"""
    session_mgr, storage = setup
    now = datetime.now(timezone.utc)
    repo = storage.get_session_repo()

    # 创建过期和活跃的 sessions
    await repo.save_session(Session(
        session_id="expired1", user_id="u1", device_id="d1",
        last_active=now - timedelta(hours=2), created_at=now - timedelta(hours=3),
    ))
    await repo.save_session(Session(
        session_id="expired2", user_id="u1", device_id="d2",
        last_active=now - timedelta(hours=1), created_at=now - timedelta(hours=2),
    ))
    await repo.save_session(Session(
        session_id="active", user_id="u1", device_id="d3",
        last_active=now, created_at=now,
    ))

    deleted = await session_mgr.delete_expired_sessions()
    assert deleted == 2

    # active session 仍存在
    assert await repo.find_session("active") is not None


@pytest.mark.asyncio
async def test_get_active_sessions_by_user(setup):
    """获取用户活跃会话（排除过期）。"""
    session_mgr, storage = setup
    now = datetime.now(timezone.utc)
    repo = storage.get_session_repo()

    await repo.save_session(Session(
        session_id="active1", user_id="u1", device_id="d1",
        last_active=now, created_at=now,
    ))
    await repo.save_session(Session(
        session_id="active2", user_id="u1", device_id="d2",
        last_active=now - timedelta(minutes=5), created_at=now - timedelta(minutes=10),
    ))
    await repo.save_session(Session(
        session_id="expired", user_id="u1", device_id="d3",
        last_active=now - timedelta(hours=2), created_at=now - timedelta(hours=3),
    ))

    active = await session_mgr.get_active_sessions_by_user("u1")
    assert len(active) == 2
    ids = {s.session_id for s in active}
    assert "active1" in ids and "active2" in ids
    assert "expired" not in ids
