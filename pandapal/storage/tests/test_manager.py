"""StorageManager 测试。"""

from __future__ import annotations

import pytest

from pandapal.storage.exceptions import StorageInitError
from pandapal.storage.manager import StorageManager


@pytest.mark.asyncio
async def test_initialize_memory_db():
    """:memory: 数据库可以正常初始化。"""
    manager = StorageManager(storage_path=":memory:")
    await manager.initialize_storage()
    assert manager._initialized is True
    await manager.shutdown_storage()


@pytest.mark.asyncio
async def test_initialize_file_db(tmp_path):
    """文件数据库可以正常初始化。"""
    db_path = str(tmp_path / "test.db")
    manager = StorageManager(storage_path=db_path)
    await manager.initialize_storage()
    assert manager._initialized is True
    await manager.shutdown_storage()


@pytest.mark.asyncio
async def test_double_initialize_is_idempotent():
    """重复初始化不报错（幂等）。"""
    manager = StorageManager(storage_path=":memory:")
    await manager.initialize_storage()
    await manager.initialize_storage()  # 第二次应无操作
    assert manager._initialized is True
    await manager.shutdown_storage()


@pytest.mark.asyncio
async def test_get_repo_before_init_raises():
    """未初始化时获取 Repository 抛出 RuntimeError。"""
    manager = StorageManager(storage_path=":memory:")
    with pytest.raises(RuntimeError, match="not initialized"):
        manager.get_session_repo()


@pytest.mark.asyncio
async def test_all_repos_accessible(memory_storage):
    """所有 Repository 访问器可用。"""
    assert memory_storage.get_user_config_repo() is not None
    assert memory_storage.get_session_repo() is not None
    assert memory_storage.get_task_repo() is not None
    assert memory_storage.get_device_repo() is not None
    assert memory_storage.get_approval_repo() is not None
    assert memory_storage.get_avatar_config_repo() is not None
    assert memory_storage.get_run_state_repo() is not None


@pytest.mark.asyncio
async def test_sdk_backend_factory(memory_storage):
    """SDK Backend 工厂方法返回可用实例。"""
    raw_log = memory_storage.get_raw_log_backend("user1")
    assert raw_log is not None
    # v1.4: get_summary_backend() 已移除，RawLogBackend 补充了 load_all / list_sessions
    assert hasattr(raw_log, "load_all")
    assert hasattr(raw_log, "list_sessions")
    raw_log.close()

    # WorkingMemoryBackend 也需要 user_id
    wm_backend = memory_storage.get_working_memory_backend("user1")
    assert wm_backend is not None
    assert hasattr(wm_backend, "save")
    assert hasattr(wm_backend, "load")
    wm_backend.close()


@pytest.mark.asyncio
async def test_init_invalid_path_raises():
    """不可写路径初始化失败（Fail Fast）。"""
    import sys

    if sys.platform == "win32":
        # Windows: 使用无效盘符路径
        invalid_path = "Z:\\nonexistent\\deeply\\nested\\path\\db.sqlite"
    else:
        invalid_path = "/nonexistent/deeply/nested/path/db.sqlite"

    manager = StorageManager(storage_path=invalid_path)
    with pytest.raises((StorageInitError, OSError)):
        await manager.initialize_storage()


@pytest.mark.asyncio
async def test_shutdown_before_init_is_safe():
    """未初始化时 shutdown 不报错。"""
    manager = StorageManager(storage_path=":memory:")
    await manager.shutdown_storage()  # 应无操作
