"""MessageDict 字段契约 + Memory._with_timestamp 时间戳契约测试（组 B1 / D1）。

覆盖设计用例：
  - B1  MessageDict 契约：timestamp / reasoning_content 为 NotRequired[str]；键名不与 ts 漂移（inv-B1）
  - D1  _with_timestamp：无 timestamp 补 ISO / 不污染原消息 / 已有 timestamp 不覆盖且返回原引用（inv-A2）
"""

from __future__ import annotations

import re
from typing import NotRequired, get_type_hints

from pandaren.memory.memory import Memory
from pandaren.memory.models import MessageDict


# ─────────────────────────────────────────────
# B1: MessageDict 字段契约（inv-B1，防字段漂移）
# ─────────────────────────────────────────────


def test_message_dict_timestamp_and_reasoning_content_are_notrequired_str():
    # 规格守卫：get_session_history 依赖 m.get("timestamp")，字段名/类型漂移会静默取空
    # models.py 有 `from __future__ import annotations`，注解为 ForwardRef，需 get_type_hints 解析
    ann = get_type_hints(MessageDict, include_extras=True)

    assert ann["timestamp"] == NotRequired[str]
    assert ann["reasoning_content"] == NotRequired[str]
    assert "ts" not in ann


# ─────────────────────────────────────────────
# D1: Memory._with_timestamp（inv-A2，生产链路 timestamp 贯通根因）
# ─────────────────────────────────────────────


def test_with_timestamp_injects_iso_and_does_not_mutate_input():
    msg = {"role": "assistant", "content": "hi"}

    out = Memory._with_timestamp(msg)

    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", out["timestamp"])
    assert out["role"] == "assistant"
    assert out["content"] == "hi"
    assert "timestamp" not in msg


def test_with_timestamp_keeps_existing_timestamp_and_returns_same_ref():
    msg = {"role": "user", "content": "x", "timestamp": "2024-01-01T00:00:00+00:00"}

    out = Memory._with_timestamp(msg)

    assert out is msg
    assert out["timestamp"] == "2024-01-01T00:00:00+00:00"
