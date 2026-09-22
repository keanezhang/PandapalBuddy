"""pandapal/knowledge_base/tests/test_protocol.py — KB_* 事件契约。

覆盖设计文档用例 2、22（inv-2 / R9.1）：
  - 全部 KB_* 事件 scope 为 global 且不含 session_id（含 kb_tree_result）
  - kb_tree_result / kb_documents_changed / kb_build_failed / kb_build_progress 的 payload 形状
  - kb_build_progress 的 incremental 默认值为 False（KB_BUILD_PROGRESS_DEFAULT_INCREMENTAL）
"""

from __future__ import annotations

import pytest

from pandapal.events.normalized import (
    EVENT_SCOPE_KEY,
    KB_BUILD_PROGRESS_DEFAULT_INCREMENTAL,
    SCOPE_GLOBAL,
    EventType,
    NormalizedEvent,
)

_KB_CONSTRUCTORS = [
    lambda: NormalizedEvent.kb_list_result([]),
    lambda: NormalizedEvent.kb_get_result({}),
    lambda: NormalizedEvent.kb_saved("K"),
    lambda: NormalizedEvent.kb_deleted("K"),
    lambda: NormalizedEvent.kb_build_progress("K", "vector", 40, "m"),
    lambda: NormalizedEvent.kb_build_done("K"),
    lambda: NormalizedEvent.kb_build_failed("K", "err"),
    lambda: NormalizedEvent.kb_search_result({"knowledge_base": "K"}),
    lambda: NormalizedEvent.kb_documents_changed("K", [], []),
    lambda: NormalizedEvent.kb_tree_result("K", []),
]


# ── inv-2：KB_* 一律 global scope，不带会话 ID ─────────────────────────────


@pytest.mark.parametrize("build", _KB_CONSTRUCTORS)
def test_kb_events_are_global_and_session_free(build):
    event = build()
    assert event.payload[EVENT_SCOPE_KEY] == SCOPE_GLOBAL
    assert "session_id" not in event.payload


# ── payload 形状 ───────────────────────────────────────────────────────────


def test_kb_documents_changed_payload_shape():
    uploaded = [{"name": "a.pdf", "suffix": ".pdf"}]
    rejected = [{"name": "b.exe", "reason": "不支持的格式 .exe"}]

    event = NormalizedEvent.kb_documents_changed("K", uploaded, rejected)

    assert event.event_type == EventType.KB_DOCUMENTS_CHANGED
    assert event.payload == {
        "name": "K",
        "uploaded": uploaded,
        "rejected": rejected,
        EVENT_SCOPE_KEY: SCOPE_GLOBAL,
    }


def test_kb_build_failed_payload_shape():
    event = NormalizedEvent.kb_build_failed("K", "磁盘写入失败")

    assert event.event_type == EventType.KB_BUILD_FAILED
    assert event.payload == {"name": "K", "error": "磁盘写入失败", EVENT_SCOPE_KEY: SCOPE_GLOBAL}


def test_kb_tree_result_payload_shape():
    """用例 2：KB_TREE_RESULT 事件契约（payload 形状 + global + 无 session_id）。"""
    tree = [
        {"path": "a.md", "name": "a.md", "is_dir": False, "size": 3,
         "suffix": ".md", "mtime": 1.0, "children": None}
    ]

    event = NormalizedEvent.kb_tree_result("K", tree)

    assert event.event_type == EventType.KB_TREE_RESULT
    assert event.payload == {"name": "K", "tree": tree, EVENT_SCOPE_KEY: SCOPE_GLOBAL}
    assert "session_id" not in event.payload


def test_kb_build_progress_defaults_to_incremental_false():
    assert KB_BUILD_PROGRESS_DEFAULT_INCREMENTAL is False

    default = NormalizedEvent.kb_build_progress("K", "vector", 40, "m")
    assert default.payload == {
        "name": "K",
        "stage": "vector",
        "percent": 40,
        "message": "m",
        "incremental": False,
        EVENT_SCOPE_KEY: SCOPE_GLOBAL,
    }

    explicit = NormalizedEvent.kb_build_progress("K", "vector", 40, "m", incremental=True)
    assert explicit.payload["incremental"] is True
