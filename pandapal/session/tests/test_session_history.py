"""SessionListManager.get_session_history 集成测试（设计文档 §7 组 C：C1-C7）。

覆盖 rawlog-reasoning-session-history-fix.design.md：
  C1 工具回合完整性（修复 ①②③ 核心回归）—— inv-C1/C2/C3 [P0]
  C2 富 timeline 段序 + 空 reasoning 省略 + 无结果兜底 —— inv-C5/C3 [P1]
  C3 timestamp 逐条透传（缺失 → None）—— inv-C4 [P1]
  C4 广播截断粒度 = 折叠后条目数（limit 边界）—— inv-C1 [P1]
  C5 防御分支矩阵（5 子场景）—— inv-C6 [P2]
  C6 tool 结果 error 判定 + args/tc 容错 + preview 截断 —— inv-C3 [P2]
  C7 广播失败不向上传播 —— inv-C6 [P2]

基建：真 sqlite session_repo（复用 test_session_list_manager.py 的 storage fixture 模式）
+ 局部 Fake raw_log / broadcast；fake 类不跨模块 import。
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio

from pandapal.session.exceptions import SessionNotFoundError
from pandapal.session.session_list_manager import (
    DEFAULT_MAX_SESSIONS,
    SessionListManager,
)
from pandapal.storage.manager import StorageManager
from pandapal.storage.models import Session


# ═══════════════════════════════════════════════════════════
# Fakes / Test Doubles（局部定义，勿 import 其他测试模块）
# ═══════════════════════════════════════════════════════════


class _FakeRawLogBackend:
    """内存 raw_log：load_all 深拷贝（防投影代码原地改测试数据），可注入失败。"""

    def __init__(self, messages: list[dict] | None = None, fail_load: bool = False) -> None:
        self.messages: list[dict] = messages or []
        self.fail_load = fail_load
        self.deleted: list[str] = []

    def load_all(self, session_id: str) -> list[dict]:
        if self.fail_load:
            raise RuntimeError("db down")
        return copy.deepcopy(self.messages)

    def delete_turns(self, session_id: str) -> None:
        self.deleted.append(session_id)


class _RecordingBroadcast:
    """记录所有 send() 调用；不做任何实际广播。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def send(self, event: Any, origin_channel_id: str = "") -> None:
        self.events.append(event)


class _ExplodingBroadcast:
    """send 永远抛异常：验证广播失败不向上传播。"""

    async def send(self, event: Any, origin_channel_id: str = "") -> None:
        raise RuntimeError("broadcast down")


class _FakeAgentPool:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel_session(self, session_id: str) -> None:
        self.cancelled.append(session_id)


class _FakeWorkingMemoryBackend:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)


class _FakeAgentTaskRepo:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_session_tasks(self, session_id: str) -> int:
        self.deleted.append(session_id)
        return 1


class _FakeConfigManager:
    def __init__(self, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        self._max = max_sessions

    def get_system_config(self) -> Any:
        class _C:
            pass
        c = _C()
        c.max_sessions = self._max
        return c


class _FrozenClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        n = self._now
        # 每次调用前进 1s，保证 updated_at 递增
        self._now = datetime.fromtimestamp(
            self._now.timestamp() + 1, tz=timezone.utc,
        )
        return n


class _SeqIdGenerator:
    def __init__(self) -> None:
        self._n_sess = 0
        self._n_grp = 0

    def new_session_id(self) -> str:
        self._n_sess += 1
        return f"sess-{self._n_sess:03d}"

    def new_group_id(self) -> str:
        self._n_grp += 1
        return f"grp-{self._n_grp:03d}"


# ═══════════════════════════════════════════════════════════
# Fixtures / 构造辅助
# ═══════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def storage(tmp_path):
    """SQLite 模式 StorageManager（复用 test_session_list_manager.py 模式）。"""
    db_path = str(tmp_path / "test.db")
    mgr = StorageManager(storage_path=db_path, storage_mode="sqlite")
    await mgr.initialize_storage()
    yield mgr
    await mgr.shutdown_storage()


_MISSING = object()


def _make_manager(
    storage: StorageManager,
    *,
    broadcast: Any = None,
    raw_log_backend: Any = _MISSING,
):
    """构造 SessionListManager；默认注入局部 fake 依赖。"""
    if broadcast is None:
        broadcast = _RecordingBroadcast()
    if raw_log_backend is _MISSING:
        raw_log_backend = _FakeRawLogBackend()
    mgr = SessionListManager(
        session_repo=storage.get_session_repo(),
        group_repo=storage.get_session_group_repo(),
        agent_pool=_FakeAgentPool(),
        approval_repo=storage.get_approval_repo(),
        run_state_repo=storage.get_run_state_repo(),
        broadcast=broadcast,
        config_manager=_FakeConfigManager(max_sessions=DEFAULT_MAX_SESSIONS),
        raw_log_backend=raw_log_backend,
        working_memory_backend=_FakeWorkingMemoryBackend(),
        agent_task_repo=_FakeAgentTaskRepo(),
        clock=_FrozenClock(),
        id_generator=_SeqIdGenerator(),
    )
    return mgr, broadcast, raw_log_backend


def _last_history_event(broadcast: _RecordingBroadcast) -> Any:
    events = [
        e for e in broadcast.events
        if getattr(e, "event_type", None) is not None
        and e.event_type.value == "session_history_list"
    ]
    assert events, "应广播 session_history_list 事件"
    return events[-1]


# ═══════════════════════════════════════════════════════════
# C1（P0 核心）：工具回合完整性
# ═══════════════════════════════════════════════════════════


# inv-C1 + inv-C2 + inv-C3 [P0]（Risk-C1/C2/C3）
@pytest.mark.asyncio
async def test_c1_tool_round_integrity(storage):
    mgr, broadcast, raw_log = _make_manager(storage)
    sid = await mgr.create_empty_session("alice")
    raw_log.messages = [
        {"role": "user", "content": "early", "timestamp": "t0"},
        {"role": "assistant", "content": "早答", "timestamp": "t1"},
        {"role": "user", "content": "请查天气", "timestamp": "t2"},
        {"role": "assistant", "content": "", "timestamp": "t3", "tool_calls": [
            {"id": "tc1", "function": {"name": "get_weather", "arguments": '{"city": "上海"}'}},
            {"id": "tc2", "function": {"name": "get_weather", "arguments": '{"city": "北京"}'}},
            {"id": "tc3", "function": {"name": "get_air", "arguments": '{"city": "上海"}'}},
        ]},
        {"role": "tool", "content": '{"temp": 30}', "tool_call_id": "tc1", "timestamp": "t4"},
        {"role": "tool", "content": '{"temp": 28}', "tool_call_id": "tc2", "timestamp": "t5"},
        {"role": "tool", "content": '{"aqi": 80}', "tool_call_id": "tc3", "timestamp": "t6"},
    ]

    ret = await mgr.get_session_history("alice", sid, limit=2)

    # 折叠后 4 条（early / 早答 / 请查天气 / assistant 回合）；limit=2 取最新 2 条
    assert len(ret) == 2
    # 修复②：ret 与广播载荷中均无纯 tool 条目泄漏
    assert all(e["role"] != "tool" for e in ret)

    ev = _last_history_event(broadcast)
    assert ev.payload["session_id"] == sid
    assert ev.payload["messages"] == ret  # 分页后返回值 == 广播载荷

    assert ev.payload["messages"][0] == {"role": "user", "content": "请查天气", "timestamp": "t2"}
    assistant = ev.payload["messages"][1]
    assert assistant["role"] != "tool"

    # 修复①：limit 窗口内 assistant 的 tool_calls 完整（3 条，未被切断）
    assert len(assistant["tool_calls"]) == 3
    # 修复③：tool 结果按 tool_call_id 折叠回 assistant
    assert assistant["tool_calls"][0] == {
        "tool_call_id": "tc1",
        "tool_name": "get_weather",
        "args": {"city": "上海"},
        "status": "done",
        "result": {"preview": '{"temp": 30}', "full": '{"temp": 30}', "error": None},
    }
    assert assistant["tool_calls"][1] == {
        "tool_call_id": "tc2",
        "tool_name": "get_weather",
        "args": {"city": "北京"},
        "status": "done",
        "result": {"preview": '{"temp": 28}', "full": '{"temp": 28}', "error": None},
    }
    assert assistant["tool_calls"][2] == {
        "tool_call_id": "tc3",
        "tool_name": "get_air",
        "args": {"city": "上海"},
        "status": "done",
        "result": {"preview": '{"aqi": 80}', "full": '{"aqi": 80}', "error": None},
    }
    # content 空 → 无 text 段；无 reasoning → 无 reasoning 段
    assert assistant["timeline"] == [
        {"kind": "tool", "tool_call_id": "tc1"},
        {"kind": "tool", "tool_call_id": "tc2"},
        {"kind": "tool", "tool_call_id": "tc3"},
    ]


# ═══════════════════════════════════════════════════════════
# C2：富 timeline 段序 + 空 reasoning 省略 + 无结果兜底
# ═══════════════════════════════════════════════════════════


# inv-C5 + inv-C3 [P1]（Risk-C5）
@pytest.mark.asyncio
async def test_c2_rich_timeline_segment_order(storage):
    mgr, broadcast, raw_log = _make_manager(storage)
    sid = await mgr.create_empty_session("alice")
    raw_log.messages = [
        {"role": "user", "content": "u", "timestamp": "t0"},
        {"role": "assistant", "content": "最终答案", "reasoning_content": "推理过程",
         "timestamp": "t1", "tool_calls": [
             {"id": "r1", "function": {"name": "f1", "arguments": "{}"}},
             {"id": "r2", "function": {"name": "f2", "arguments": "{}"}},
         ]},
        # r2 无对应 tool 结果 → tool_results.get(tcid) 未命中分支
        {"role": "tool", "content": "ok", "tool_call_id": "r1", "timestamp": "t2"},
    ]

    await mgr.get_session_history("alice", sid)

    assistant = _last_history_event(broadcast).payload["messages"][-1]
    # 段序固定：reasoning → text → tool
    assert assistant["timeline"] == [
        {"kind": "reasoning", "text": "推理过程"},
        {"kind": "text", "content": "最终答案"},
        {"kind": "tool", "tool_call_id": "r1"},
        {"kind": "tool", "tool_call_id": "r2"},
    ]
    # r2 无结果兜底：不崩、不误判 error
    assert assistant["tool_calls"][1]["status"] == "done"
    assert assistant["tool_calls"][1]["result"] == {"preview": "", "full": "", "error": None}


# inv-C5：reasoning 为 falsy 时省略 reasoning 段，值不进入任何字段
@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning", [None, "", "   "])
async def test_c2_empty_reasoning_omitted(storage, reasoning):
    mgr, broadcast, raw_log = _make_manager(storage)
    sid = await mgr.create_empty_session("alice")
    raw_log.messages = [
        {"role": "user", "content": "u", "timestamp": "t0"},
        {"role": "assistant", "content": "x", "reasoning_content": reasoning, "timestamp": "t1"},
    ]

    await mgr.get_session_history("alice", sid)

    assistant = _last_history_event(broadcast).payload["messages"][-1]
    kinds = [seg["kind"] for seg in assistant["timeline"]]
    assert "reasoning" not in kinds
    assert "reasoning_content" not in assistant  # 不进入任何字段
    assert assistant["timeline"] == [{"kind": "text", "content": "x"}]


# inv-C5：全空 assistant（content=""/reasoning=None/tool_calls 缺省）→ 空 timeline + 空 tool_calls
@pytest.mark.asyncio
async def test_c2_fully_empty_assistant(storage):
    mgr, broadcast, raw_log = _make_manager(storage)
    sid = await mgr.create_empty_session("alice")
    raw_log.messages = [
        {"role": "user", "content": "u", "timestamp": "t0"},
        {"role": "assistant", "content": "", "timestamp": "t1"},
    ]

    await mgr.get_session_history("alice", sid)

    assistant = _last_history_event(broadcast).payload["messages"][-1]
    assert assistant["timeline"] == []
    assert assistant["tool_calls"] == []


# ═══════════════════════════════════════════════════════════
# C3：timestamp 逐条透传（缺失 → None）
# ═══════════════════════════════════════════════════════════


# inv-C4 [P1]（Risk-C4）
@pytest.mark.asyncio
async def test_c3_timestamp_passthrough(storage):
    mgr, broadcast, raw_log = _make_manager(storage)
    sid = await mgr.create_empty_session("alice")
    raw_log.messages = [
        {"role": "user", "content": "带时间", "timestamp": "2026-07-11T12:00:00+00:00"},
        {"role": "assistant", "content": "带时间", "timestamp": "2026-07-11T12:00:01+00:00"},
        {"role": "user", "content": "无时间"},
    ]

    ret = await mgr.get_session_history("alice", sid)

    assert ret[0]["timestamp"] == "2026-07-11T12:00:00+00:00"
    assert ret[1]["timestamp"] == "2026-07-11T12:00:01+00:00"
    assert ret[2]["timestamp"] is None  # 缺失 → None，不臆造
    # 广播 payload 与 ret 一致（3 条 < 默认 limit=50 → 全量）
    assert _last_history_event(broadcast).payload["messages"] == ret


# ═══════════════════════════════════════════════════════════
# C4：广播截断粒度 = 折叠后条目数（limit 边界）
# ═══════════════════════════════════════════════════════════


# inv-C1 [P1]（Risk-C3）
@pytest.mark.asyncio
async def test_c4_truncation_window_on_folded_entries(storage):
    mgr, broadcast, raw_log = _make_manager(storage)
    sid = await mgr.create_empty_session("alice")
    raw_log.messages = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"msg{i}", "timestamp": f"t{i}"}
        for i in range(60)
    ]

    ret = await mgr.get_session_history("alice", sid, limit=50)

    # 新契约：return == 广播载荷 == 最新 limit 条（折叠后）
    assert len(ret) == 50
    ev = _last_history_event(broadcast)
    payload_msgs = ev.payload["messages"]
    assert len(payload_msgs) == 50
    # simplified[-50:] 首条 = msg10
    assert payload_msgs[0]["content"] == "msg10"
    assert payload_msgs == ret

    # limit > 总条数 → 全量
    await mgr.get_session_history("alice", sid, limit=100)
    assert len(_last_history_event(broadcast).payload["messages"]) == 60


# inv-C1：limit=0 → 空窗口（且不抛）
@pytest.mark.asyncio
async def test_c4_limit_zero_returns_empty_payload(storage):
    mgr, broadcast, raw_log = _make_manager(storage)
    sid = await mgr.create_empty_session("alice")
    raw_log.messages = [
        {"role": "user", "content": f"msg{i}", "timestamp": f"t{i}"}
        for i in range(3)
    ]

    await mgr.get_session_history("alice", sid, limit=0)

    assert _last_history_event(broadcast).payload["messages"] == []


# ═══════════════════════════════════════════════════════════
# C5：防御分支矩阵（5 子场景）
# ═══════════════════════════════════════════════════════════


# inv-C6 [P2]（Risk-C8）
@pytest.mark.asyncio
async def test_c5a_session_not_found_raises(storage):
    mgr, _, _ = _make_manager(storage)
    with pytest.raises(SessionNotFoundError):
        await mgr.get_session_history("alice", "ghost")


@pytest.mark.asyncio
async def test_c5b_deleted_session_raises(storage):
    mgr, _, _ = _make_manager(storage)
    ts = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    await storage.get_session_repo().save_session(Session(
        session_id="del-1",
        user_id="alice",
        device_id="d1",
        last_active=ts,
        created_at=ts,
        updated_at=ts,
        title="",
        preview="",
        message_count=1,
        is_empty=False,
        is_deleted=True,
    ))
    with pytest.raises(SessionNotFoundError):
        await mgr.get_session_history("alice", "del-1")


@pytest.mark.asyncio
async def test_c5c_user_mismatch_raises(storage):
    mgr, _, _ = _make_manager(storage)
    sid = await mgr.create_empty_session("alice")
    with pytest.raises(SessionNotFoundError):
        await mgr.get_session_history("bob", sid)


@pytest.mark.asyncio
async def test_c5d_backend_none_returns_empty(storage):
    mgr, broadcast, _ = _make_manager(storage, raw_log_backend=None)
    sid = await mgr.create_empty_session("alice")

    ret = await mgr.get_session_history("alice", sid)

    assert ret == []  # 不抛
    assert broadcast.events == []  # 无广播事件


@pytest.mark.asyncio
async def test_c5e_load_all_error_returns_empty_no_broadcast(storage):
    mgr, broadcast, raw_log = _make_manager(storage)
    raw_log.fail_load = True
    sid = await mgr.create_empty_session("alice")

    ret = await mgr.get_session_history("alice", sid)

    assert ret == []
    history_events = [
        e for e in broadcast.events
        if getattr(e, "event_type", None) is not None
        and e.event_type.value == "session_history_list"
    ]
    assert history_events == []  # 副作用验证：load_all 失败不广播


# ═══════════════════════════════════════════════════════════
# C6：tool 结果 error 判定 + args/tc 容错
# ═══════════════════════════════════════════════════════════


# inv-C3 [P2]（Risk-C6/C7）
@pytest.mark.asyncio
async def test_c6_tool_result_error_and_tolerance(storage):
    mgr, broadcast, raw_log = _make_manager(storage)
    sid = await mgr.create_empty_session("alice")
    raw_log.messages = [
        {"role": "user", "content": "u", "timestamp": "t0"},
        {"role": "assistant", "content": "", "timestamp": "t1", "tool_calls": [
            {"id": "e1", "function": {"name": "f", "arguments": '{"k": 1}'}},
            {"id": "e2", "function": {"name": "g", "arguments": "not-json"}, "name": "fallback_g"},
            "junk-not-dict",
            {"id": "e3", "function": "not-dict", "name": "top_name"},
        ]},
        {"role": "tool", "content": "❌ 权限不足", "tool_call_id": "e1", "timestamp": "t2"},
    ]

    await mgr.get_session_history("alice", sid)

    tc_list = _last_history_event(broadcast).payload["messages"][-1]["tool_calls"]
    assert len(tc_list) == 3  # "junk-not-dict"（非 dict）被跳过

    # e1：❌ 前缀 → is_error
    assert tc_list[0]["status"] == "error"
    assert tc_list[0]["tool_name"] == "f"
    assert tc_list[0]["args"] == {"k": 1}
    assert tc_list[0]["result"]["error"] == "❌ 权限不足"
    assert tc_list[0]["result"]["preview"] == "❌ 权限不足"

    # e2：arguments "not-json" 解析失败 → args {}，不崩
    # 注：设计文档 C6 Then 期望 tool_name=="fallback_g"，但其 Given 中 e2.function 是 dict
    # （name="g"），与"function 非 dict 才兜底 tc[name]"自相矛盾；此处按 Given 数据断 "g"，
    # 兜底分支由 e3（function="not-dict"）覆盖。
    assert tc_list[1]["args"] == {}
    assert tc_list[1]["tool_name"] == "g"
    assert tc_list[1]["status"] == "done"
    assert tc_list[1]["result"]["error"] is None

    # e3：function 非 dict → 兜底 tc["name"]；无结果 → done
    assert tc_list[2]["tool_name"] == "top_name"
    assert tc_list[2]["args"] == {}
    assert tc_list[2]["status"] == "done"
    assert tc_list[2]["result"] == {"preview": "", "full": "", "error": None}


# inv-C3：result.preview 500 截断 / full 20000 上限（600 < 20000 不截）
@pytest.mark.asyncio
async def test_c6_preview_truncation(storage):
    mgr, broadcast, raw_log = _make_manager(storage)
    sid = await mgr.create_empty_session("alice")
    long_text = "x" * 600
    raw_log.messages = [
        {"role": "user", "content": "u", "timestamp": "t0"},
        {"role": "assistant", "content": "", "timestamp": "t1", "tool_calls": [
            {"id": "e4", "function": {"name": "h", "arguments": "{}"}},
        ]},
        {"role": "tool", "content": long_text, "tool_call_id": "e4", "timestamp": "t2"},
    ]

    await mgr.get_session_history("alice", sid)

    tc = _last_history_event(broadcast).payload["messages"][-1]["tool_calls"][0]
    assert tc["status"] == "done"
    assert tc["result"]["preview"] == "x" * 500
    assert tc["result"]["full"] == "x" * 600


# ═══════════════════════════════════════════════════════════
# C7：广播失败不向上传播
# ═══════════════════════════════════════════════════════════


# inv-C6 [P2]（Risk-C9）
@pytest.mark.asyncio
async def test_c7_broadcast_failure_swallowed(storage):
    broadcast = _ExplodingBroadcast()
    mgr, _, raw_log = _make_manager(storage, broadcast=broadcast)
    sid = await mgr.create_empty_session("alice")
    raw_log.messages = [{"role": "user", "content": "hi", "timestamp": "t0"}]

    ret = await mgr.get_session_history("alice", sid)

    # 不抛；返回仍正常，广播异常仅 warning
    assert ret == [{"role": "user", "content": "hi", "timestamp": "t0"}]
