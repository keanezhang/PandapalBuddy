"""pandapal SQLiteRawLogBackend content_json 整条往返测试（组 E1，inv-A1/inv-A2）。

生产链路实际使用 pandapal backend：append_raw_message 将 MessageDict 整条
json.dumps 进 content_json，load_all 原样 json.loads 返回。
E1 守护 timestamp / reasoning_content / tool_calls 随整条 MessageDict 往返保真。
"""

from __future__ import annotations

import asyncio

import pytest

from pandapal.storage.manager import StorageManager


@pytest.fixture
def raw_log_backend(tmp_path):
    """真 sqlite 落 tmp_path（沿用 test_raw_log_backend.py 的 fixture 模式）。"""
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


def test_message_dict_roundtrip_preserves_reasoning_timestamp_tool_calls(raw_log_backend):
    """E1: reasoning_content / timestamp / tool_calls 往返保真。"""
    msg = {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "thinking...",
        "timestamp": "2024-06-01T12:00:00+00:00",
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": '{"p":"x"}'}},
        ],
    }

    raw_log_backend.append_raw_message(msg, "s1")
    loaded = raw_log_backend.load_all("s1")

    assert len(loaded) == 1
    assert loaded[0]["reasoning_content"] == "thinking..."
    assert loaded[0]["timestamp"] == "2024-06-01T12:00:00+00:00"
    assert loaded[0]["tool_calls"][0]["id"] == "call_1"
