"""Storage 测试共享 Fixtures。

所有测试使用临时文件或 :memory: SQLite，无外部依赖。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from pandapal.storage.manager import StorageManager


@pytest_asyncio.fixture
async def storage_manager(tmp_path):
    """提供初始化完成的 StorageManager（使用临时文件）。"""
    db_path = str(tmp_path / "test.db")
    manager = StorageManager(storage_path=db_path, storage_mode="sqlite")
    await manager.initialize_storage()
    yield manager
    await manager.shutdown_storage()


@pytest_asyncio.fixture
async def memory_storage():
    """提供 :memory: StorageManager 用于纯内存测试。"""
    manager = StorageManager(storage_path=":memory:", storage_mode="sqlite")
    await manager.initialize_storage()
    yield manager
    await manager.shutdown_storage()
