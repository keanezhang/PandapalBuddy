"""pandaren SQLiteRawLogBackend 集成测试（组 A：A1-A6，真 sqlite 落 tmp_path）。

覆盖设计文档 §7 组 A 用例：
  - A1  reasoning_content / tool_calls / timestamp 往返无损 + 键名契约（inv-A1/A2/A3/B1）
  - A2  无 reasoning_content 输入 → 输出键缺省（inv-A2/A5）
  - A3  旧库迁移：自动补列、读旧写新、重开幂等（inv-A4/A2/A3/A5）
  - A4  多模态 content(list) 深等往返（inv-A1）
  - A5  load_within_budget：升序 + reasoning 保留 + budget<=0 空 + 确定性（inv-A2/A3）
  - A6  坏 tool_calls JSON 容错降级 []（Risk-A6）

同步测试，零 mock，零 async。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

import pytest

from pandaren.memory.backends.sqlite_raw_log import SQLiteRawLogBackend


@pytest.fixture
def backend(tmp_path):
    b = SQLiteRawLogBackend(db_path=str(tmp_path / "x.db"))
    yield b
    b.close()


def _assert_iso_timestamp_within_5min(ts: str) -> None:
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts)
    parsed = datetime.fromisoformat(ts)
    assert abs((parsed - datetime.now(timezone.utc)).total_seconds()) < 300


# ─────────────────────────────────────────────
# A1: reasoning_content + timestamp + tool_calls 往返无损（inv-A1/A2/A3/B1）
# ─────────────────────────────────────────────


def test_a1_reasoning_tool_calls_timestamp_roundtrip(backend):
    msg = {
        "role": "assistant",
        "content": "答案是 42",
        "reasoning_content": "先算再答",
        "tool_calls": [{"id": "tc-1", "function": {"name": "calc", "arguments": "{\"expr\": \"6*7\"}"}}],
    }

    backend.append_raw_message(msg, session_id="s1")
    loaded = backend.load_all("s1")

    assert len(loaded) == 1
    assert loaded[0]["role"] == "assistant"
    assert loaded[0]["content"] == "答案是 42"
    assert loaded[0]["reasoning_content"] == "先算再答"
    assert loaded[0]["tool_calls"] == [
        {"id": "tc-1", "function": {"name": "calc", "arguments": "{\"expr\": \"6*7\"}"}},
    ]
    # 键名契约：输出键是 timestamp 而非 ts
    assert "timestamp" in loaded[0]
    assert "ts" not in loaded[0]
    _assert_iso_timestamp_within_5min(loaded[0]["timestamp"])


# ─────────────────────────────────────────────
# A2: 无 reasoning_content → 输出键缺省（inv-A2/A5）
# ─────────────────────────────────────────────


def test_a2_missing_reasoning_content_omits_key(backend):
    backend.append_raw_message({"role": "user", "content": "hi"}, session_id="s1")
    loaded = backend.load_all("s1")

    assert len(loaded) == 1
    assert loaded[0]["content"] == "hi"
    assert "reasoning_content" not in loaded[0]
    assert "timestamp" in loaded[0]


# ─────────────────────────────────────────────
# A3: 旧库迁移（legacy schema → 补列、读旧写新、幂等）
# ─────────────────────────────────────────────


def test_a3_legacy_schema_migration(tmp_path):
    db = str(tmp_path / "a3.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE raw_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL,
          content TEXT, tool_calls TEXT, tool_call_id TEXT,
          ts TEXT NOT NULL, run_id TEXT, step INTEGER);
        CREATE INDEX idx_raw_session_seq ON raw_messages(session_id, seq);
        INSERT INTO raw_messages (session_id, seq, role, content, tool_calls, ts) VALUES
         ('s1', 1, 'user', '旧问题', NULL, '2026-01-01T00:00:00+00:00'),
         ('s1', 2, 'assistant', '旧答案',
          '[{"id":"old-1","function":{"name":"f","arguments":"{}"}}]', '2026-01-02T00:00:00+00:00');
        """
    )
    conn.close()

    # 构造即触发迁移，不抛
    backend = SQLiteRawLogBackend(db_path=db)

    # 迁移生效：列集合含 reasoning_content
    cols = {r[1] for r in backend._conn.execute("PRAGMA table_info(raw_messages)")}
    assert "reasoning_content" in cols

    # 旧数据原样读回：ts 还原、无 reasoning_content 键
    loaded = backend.load_all("s1")
    assert len(loaded) == 2
    assert loaded[0]["timestamp"] == "2026-01-01T00:00:00+00:00"
    assert loaded[0]["content"] == "旧问题"
    assert "reasoning_content" not in loaded[0]
    assert loaded[1]["tool_calls"] == [{"id": "old-1", "function": {"name": "f", "arguments": "{}"}}]
    assert "reasoning_content" not in loaded[1]

    # 迁移后写路径可用：新消息带 reasoning_content
    backend.append_raw_message(
        {"role": "assistant", "content": "新", "reasoning_content": "思考"}, session_id="s1",
    )
    loaded = backend.load_all("s1")
    assert len(loaded) == 3
    assert loaded[2]["content"] == "新"
    assert loaded[2]["reasoning_content"] == "思考"

    # 幂等：关闭后再次构造不抛、数据仍在
    backend.close()
    backend2 = SQLiteRawLogBackend(db_path=db)
    assert len(backend2.load_all("s1")) == 3
    backend2.close()


# ─────────────────────────────────────────────
# A4: 多模态 content(list) 深等往返（inv-A1）
# ─────────────────────────────────────────────


# [known-gap] 设计规格 A4 要求 content(list) 深等还原（inv-A1）；
# 现状 _row_to_message（sqlite_raw_log.py:67）对 content 不做 json.loads，list 序列化后
# 以 JSON 字符串原样返回，非深等。实现补齐 content 解析后 strict xfail 会报警提醒移除标记。
@pytest.mark.xfail(
    reason="_row_to_message 未解析 content 为 list，返回 JSON 字符串（sqlite_raw_log.py:67）",
    strict=True,
)
def test_a4_multimodal_content_list_roundtrip(backend):
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "你好"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ],
        "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{\"a\": 1}"}}],
    }

    backend.append_raw_message(msg, session_id="s1")
    loaded = backend.load_all("s1")

    assert loaded[0]["content"] == msg["content"]
    assert loaded[0]["tool_calls"] == msg["tool_calls"]


# ─────────────────────────────────────────────
# A5: load_within_budget 顺序 + reasoning 保留 + 预算边界 + 确定性（inv-A2/A3）
# ─────────────────────────────────────────────


def test_a5_load_within_budget_order_reasoning_and_budget_edge(backend):
    backend.append_raw_message({"role": "user", "content": "q1"}, session_id="s1")
    backend.append_raw_message({"role": "assistant", "content": "答", "reasoning_content": "推理"}, session_id="s1")
    backend.append_raw_message({"role": "user", "content": "q2"}, session_id="s1")

    r = backend.load_within_budget("s1", token_budget=10_000)
    assert [m["content"] for m in r] == ["q1", "答", "q2"]
    assert r[1]["reasoning_content"] == "推理"
    assert all("timestamp" in m for m in r)

    # 预算边界：<= 0 早退
    assert backend.load_within_budget("s1", token_budget=0) == []
    assert backend.load_within_budget("s1", token_budget=-5) == []

    # 确定性：连调两次深等
    assert backend.load_within_budget("s1", token_budget=10_000) == r


# ─────────────────────────────────────────────
# A6: 历史坏 tool_calls JSON 容错（不崩溃，降级 []）
# ─────────────────────────────────────────────


def test_a6_bad_tool_calls_json_degrades_to_empty_list(backend):
    with backend._conn:
        backend._conn.execute(
            "INSERT INTO raw_messages (session_id, seq, role, content, tool_calls, ts) "
            "VALUES ('s1', 1, 'assistant', 'x', 'not-json{', '2026-01-01T00:00:00+00:00')"
        )

    loaded = backend.load_all("s1")

    assert len(loaded) == 1
    assert "tool_calls" in loaded[0]
    assert loaded[0]["tool_calls"] == []
