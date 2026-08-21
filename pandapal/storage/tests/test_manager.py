"""StorageManager 测试。"""

from __future__ import annotations

import pytest

from pandapal.storage.exceptions import StorageInitError
from pandapal.storage.manager import StorageManager


@pytest.mark.asyncio
async def test_initialize_file_db(tmp_path):
    """文件数据库可以正常初始化。"""
    db_path = str(tmp_path / "test.db")
    manager = StorageManager(storage_path=db_path)
    await manager.initialize_storage()
    assert manager._initialized is True
    await manager.shutdown_storage()


@pytest.mark.asyncio
async def test_get_repo_before_init_raises():
    """未初始化时获取 Repository 抛出 RuntimeError。"""
    manager = StorageManager(storage_path=":memory:")
    with pytest.raises(RuntimeError, match="not initialized"):
        manager.get_session_repo()


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
