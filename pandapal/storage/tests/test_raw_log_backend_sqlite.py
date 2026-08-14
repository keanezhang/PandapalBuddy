"""pandapal SQLiteRawLogBackend 测试（storage_mode="sqlite"）。

与 test_raw_log_backend.py（markdown 默认模式）互补：
  - load_within_budget 正常运行（回归：_MAX_LOAD_ROWS 重命名为 self._max_load_rows 时
    漏改引用导致的 NameError，markdown 模式测试覆盖不到此路径）
  - load_all 取「最新」N 条并按 turn_index 升序返回（构造参数 / 环境变量覆盖上限）
"""

from __future__ import annotations

import asyncio

import pytest

from pandapal.storage.manager import StorageManager


def _make_backend(db_path: str):
    """初始化 sqlite StorageManager 并返回 (backend, manager)。"""

    async def _init() -> StorageManager:
        manager = StorageManager(storage_path=db_path, storage_mode="sqlite")
        await manager.initialize_storage()
        return manager

    manager = asyncio.run(_init())
    backend = manager.get_raw_log_backend("user1")
    return backend, manager


def test_load_within_budget_works(tmp_path):
    """load_within_budget 正常返回消息（回归 _MAX_LOAD_ROWS NameError）。"""
    db = str(tmp_path / "t.db")
    backend, manager = _make_backend(db)
    try:
        for i in range(5):
            backend.append_raw_message({"role": "user", "content": f"m{i}"}, "s1")

        msgs = backend.load_within_budget("s1", token_budget=100000)
        assert [m["content"] for m in msgs] == ["m0", "m1", "m2", "m3", "m4"]
    finally:
        backend.close()
        asyncio.run(manager.shutdown_storage())


def test_load_all_returns_latest_n_ascending(tmp_path, monkeypatch):
    """load_all 取最新 N 条并按升序返回（环境变量覆盖上限）。"""
    monkeypatch.setenv("PANDAPAL_RAW_LOG_MAX_ROWS", "10")
    db = str(tmp_path / "t.db")
    backend, manager = _make_backend(db)
    try:
        for i in range(15):
            backend.append_raw_message({"role": "user", "content": f"m{i}"}, "s1")

        loaded = backend.load_all("s1")
        # 取最新 10 条（m5..m14），升序（旧→新）
        assert [m["content"] for m in loaded] == [f"m{i}" for i in range(5, 15)]
    finally:
        backend.close()
        asyncio.run(manager.shutdown_storage())
