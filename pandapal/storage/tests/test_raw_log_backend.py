"""SQLiteRawLogBackend 测试。"""

from __future__ import annotations

import pytest

from pandapal.storage.manager import StorageManager


@pytest.fixture
def raw_log_backend(tmp_path):
    """提供 RawLogBackend 实例（同步测试，无需 async）。"""
    import asyncio

    db_path = str(tmp_path / "test.db")

    async def _init():
        manager = StorageManager(storage_path=db_path)
        await manager.initialize_storage()
        return manager

    manager = asyncio.run(_init())
    backend = manager.get_raw_log_backend("user1")
    yield backend
    backend.close()
    asyncio.run(manager.shutdown_storage())


def test_append_and_load(raw_log_backend):
    """追加消息后可以读取。"""
    msg = {"role": "user", "content": "Hello!"}
    raw_log_backend.append_raw_message(msg, "session1")

    msgs = raw_log_backend.load_within_budget("session1", token_budget=1000)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Hello!"


def test_multiple_messages_order(raw_log_backend):
    """多条消息按时间从旧到新排列。"""
    raw_log_backend.append_raw_message({"role": "user", "content": "msg1"}, "s1")
    raw_log_backend.append_raw_message({"role": "assistant", "content": "msg2"}, "s1")
    raw_log_backend.append_raw_message({"role": "user", "content": "msg3"}, "s1")

    msgs = raw_log_backend.load_within_budget("s1", token_budget=10000)
    assert len(msgs) == 3
    assert msgs[0]["content"] == "msg1"
    assert msgs[2]["content"] == "msg3"


def test_compact_boundary(raw_log_backend):
    """写入 compact_boundary 后，load_within_budget 只从边界之后读取。"""
    # 写入一些旧消息
    raw_log_backend.append_raw_message({"role": "user", "content": "old1"}, "s1")
    raw_log_backend.append_raw_message({"role": "assistant", "content": "old2"}, "s1")

    # 写入边界
    boundary = {
        "type": "compact_boundary",
        "timestamp": "2024-01-01T00:00:00",
        "tokens_before": 100,
        "tokens_after": 20,
        "kept_message_count": 1,
    }
    raw_log_backend.append_compact_boundary(boundary, "s1")

    # 写入新消息
    raw_log_backend.append_raw_message({"role": "user", "content": "new1"}, "s1")

    msgs = raw_log_backend.load_within_budget("s1", token_budget=10000)
    # 只应返回边界之后的消息
    assert len(msgs) == 1
    assert msgs[0]["content"] == "new1"


def test_token_budget_limit(raw_log_backend):
    """token_budget 限制读取数量。"""
    # 写入大量消息
    for i in range(20):
        raw_log_backend.append_raw_message(
            {"role": "user", "content": f"message number {i}" * 10}, "s1"
        )

    # 很小的 budget
    msgs = raw_log_backend.load_within_budget("s1", token_budget=50)
    # 至少返回一条（第一条即使超预算也会返回）
    assert 0 < len(msgs) < 20


def test_delete_turns(raw_log_backend):
    """删除 session 的所有日志。"""
    raw_log_backend.append_raw_message({"role": "user", "content": "hi"}, "s1")
    raw_log_backend.append_raw_message({"role": "user", "content": "other"}, "s2")

    raw_log_backend.delete_turns("s1")

    msgs_s1 = raw_log_backend.load_within_budget("s1", token_budget=10000)
    msgs_s2 = raw_log_backend.load_within_budget("s2", token_budget=10000)
    assert len(msgs_s1) == 0
    assert len(msgs_s2) == 1


def test_session_isolation(raw_log_backend):
    """不同 session 的日志互相隔离。"""
    raw_log_backend.append_raw_message({"role": "user", "content": "s1_msg"}, "s1")
    raw_log_backend.append_raw_message({"role": "user", "content": "s2_msg"}, "s2")

    msgs_s1 = raw_log_backend.load_within_budget("s1", token_budget=10000)
    msgs_s2 = raw_log_backend.load_within_budget("s2", token_budget=10000)
    assert len(msgs_s1) == 1 and msgs_s1[0]["content"] == "s1_msg"
    assert len(msgs_s2) == 1 and msgs_s2[0]["content"] == "s2_msg"
