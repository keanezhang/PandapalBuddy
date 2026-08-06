"""SQLiteSummaryBackend 测试。"""

from __future__ import annotations

import pytest

from pandapal.storage.manager import StorageManager


@pytest.fixture
def summary_backend(tmp_path):
    """提供 SummaryBackend 实例（同步测试）。"""
    import asyncio

    db_path = str(tmp_path / "test.db")

    async def _init():
        manager = StorageManager(storage_path=db_path)
        await manager.initialize_storage()
        return manager

    manager = asyncio.run(_init())
    backend = manager.get_summary_backend("user1")
    yield backend
    backend.close()
    asyncio.run(manager.shutdown_storage())


def test_store_and_search(summary_backend):
    """存储摘要后可以关键词搜索。"""
    metadata = {
        "type": "summary",
        "session_id": "s1",
        "timestamp": "2024-01-01T00:00:00",
    }
    entry_id = summary_backend.store(
        "The user asked about Python programming", metadata, "s1"
    )
    assert entry_id is not None and len(entry_id) > 0

    results = summary_backend.search("Python", top_k=10, session_id="s1")
    assert len(results) == 1
    assert "Python" in results[0]["content"]
    assert results[0]["entry_id"] == entry_id


def test_search_no_match(summary_backend):
    """搜索无匹配返回空列表。"""
    metadata = {"type": "summary", "session_id": "s1", "timestamp": "2024-01-01T00:00:00"}
    summary_backend.store("About weather today", metadata, "s1")

    results = summary_backend.search("quantum physics", top_k=10, session_id="s1")
    assert len(results) == 0


def test_get_recent(summary_backend):
    """按时间倒序获取最新摘要。"""
    metadata = {"type": "summary", "session_id": "s1", "timestamp": "2024-01-01T00:00:00"}
    summary_backend.store("First summary", metadata, "s1")
    summary_backend.store("Second summary", metadata, "s1")
    summary_backend.store("Third summary", metadata, "s1")

    results = summary_backend.get_recent(top_k=2, session_id="s1")
    assert len(results) == 2
    # 最新的在前
    assert results[0]["content"] == "Third summary"
    assert results[1]["content"] == "Second summary"


def test_delete(summary_backend):
    """按 entry_id 删除摘要。"""
    metadata = {"type": "summary", "session_id": "s1", "timestamp": "2024-01-01T00:00:00"}
    entry_id = summary_backend.store("To be deleted", metadata, "s1")

    summary_backend.delete(entry_id, "s1")

    results = summary_backend.search("deleted", top_k=10, session_id="s1")
    assert len(results) == 0


def test_session_isolation(summary_backend):
    """不同 session 的摘要互相隔离。"""
    metadata_s1 = {"type": "summary", "session_id": "s1", "timestamp": "2024-01-01T00:00:00"}
    metadata_s2 = {"type": "summary", "session_id": "s2", "timestamp": "2024-01-01T00:00:00"}

    summary_backend.store("Session 1 content", metadata_s1, "s1")
    summary_backend.store("Session 2 content", metadata_s2, "s2")

    results_s1 = summary_backend.search("content", top_k=10, session_id="s1")
    results_s2 = summary_backend.search("content", top_k=10, session_id="s2")
    assert len(results_s1) == 1
    assert len(results_s2) == 1
    assert "Session 1" in results_s1[0]["content"]
    assert "Session 2" in results_s2[0]["content"]
