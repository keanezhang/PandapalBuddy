"""SessionListManager.rename_session 单元测试（9 用例种子）。

覆盖 rename_session.design.md 的核心用例：
  1. 更新标题并广播 reason="renamed"
  2. 空白标题幂等（不修改、不广播）
  3. 超长标题截断到 50
  4. 恰好 50 字符不截断
  5. 去除首尾空白
  6. 先 strip 再截断
  7. 缺失会话抛 SessionNotFoundError
  8. 已删除会话抛 SessionNotFoundError
  9. 不触碰时间戳

用 sqlite :memory: + Fake broadcast 隔离测试。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio

from pandapal.session.exceptions import SessionNotFoundError
from pandapal.session.session_list_manager import (
    DEFAULT_MAX_SESSIONS,
    MAX_SESSION_TITLE_LENGTH,
    SessionListManager,
)
from pandapal.storage.manager import StorageManager
from pandapal.storage.models import Session


# ═══════════════════════════════════════════════════════════
# Fakes / Test Doubles（局部定义，勿跨模块 import）
# ═══════════════════════════════════════════════════════════


class _RecordingBroadcast:
    """记录所有 send() 调用；不做任何实际广播。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def send(self, event: Any, origin_channel_id: str = "") -> None:
        self.events.append(event)


class _FakeAgentPool:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel_session(self, session_id: str) -> None:
        self.cancelled.append(session_id)


class _FakeRawLogBackend:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_turns(self, session_id: str) -> None:
        self.deleted.append(session_id)


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
# Fixtures
# ═══════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def storage(tmp_path):
    """SQLite 模式 StorageManager。"""
    db_path = str(tmp_path / "test.db")
    mgr = StorageManager(storage_path=db_path, storage_mode="sqlite")
    await mgr.initialize_storage()
    yield mgr
    await mgr.shutdown_storage()


@pytest_asyncio.fixture
async def session_list_mgr(storage):
    """SessionListManager + recording broadcast。"""
    broadcast = _RecordingBroadcast()
    mgr = SessionListManager(
        session_repo=storage.get_session_repo(),
        group_repo=storage.get_session_group_repo(),
        agent_pool=_FakeAgentPool(),
        approval_repo=storage.get_approval_repo(),
        run_state_repo=storage.get_run_state_repo(),
        broadcast=broadcast,
        config_manager=_FakeConfigManager(max_sessions=DEFAULT_MAX_SESSIONS),
        raw_log_backend=_FakeRawLogBackend(),
        working_memory_backend=_FakeWorkingMemoryBackend(),
        agent_task_repo=_FakeAgentTaskRepo(),
        clock=_FrozenClock(),
        id_generator=_SeqIdGenerator(),
    )
    return mgr, broadcast


# ═══════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════


def _renamed_events(broadcast: _RecordingBroadcast) -> list[Any]:
    """过滤出 reason="renamed" 的 SESSION_UPDATED 广播事件。"""
    return [
        e for e in broadcast.events
        if getattr(e, "event_type", None) is not None
        and e.event_type.value == "session_updated"
        and e.payload.get("reason") == "renamed"
    ]


# ═══════════════════════════════════════════════════════════
# 用例
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_updates_title_and_broadcasts_renamed(session_list_mgr, storage):
    """用例 1：更新标题 + 广播 reason="renamed"。"""
    mgr, broadcast = session_list_mgr
    sid = await mgr.create_empty_session("alice")

    await mgr.rename_session(sid, "项目计划")

    s = await storage.get_session_repo().find_session(sid)
    assert s is not None
    assert s.title == "项目计划"

    renamed = _renamed_events(broadcast)
    assert len(renamed) == 1
    assert renamed[0].payload["session_info"]["title"] == "项目计划"


@pytest.mark.asyncio
async def test_whitespace_title_is_idempotent(session_list_mgr, storage):
    """用例 2：空白标题幂等（不修改、不广播）。"""
    mgr, broadcast = session_list_mgr
    sid = await mgr.create_empty_session("alice")
    repo = storage.get_session_repo()

    await mgr.rename_session(sid, "   ")

    s = await repo.find_session(sid)
    assert s is not None
    assert s.title == ""  # 未被修改

    assert _renamed_events(broadcast) == []


@pytest.mark.asyncio
async def test_truncates_overlong_title_to_50(session_list_mgr, storage):
    """用例 3：超长标题截断到 MAX_SESSION_TITLE_LENGTH。"""
    mgr, _ = session_list_mgr
    sid = await mgr.create_empty_session("alice")

    await mgr.rename_session(sid, "x" * 60)

    s = await storage.get_session_repo().find_session(sid)
    assert s is not None
    assert s.title == "x" * MAX_SESSION_TITLE_LENGTH


@pytest.mark.asyncio
async def test_exact_50_chars_not_truncated(session_list_mgr, storage):
    """用例 4：恰好 50 字符不截断。"""
    mgr, _ = session_list_mgr
    sid = await mgr.create_empty_session("alice")

    await mgr.rename_session(sid, "y" * MAX_SESSION_TITLE_LENGTH)

    s = await storage.get_session_repo().find_session(sid)
    assert s is not None
    assert s.title == "y" * MAX_SESSION_TITLE_LENGTH


@pytest.mark.asyncio
async def test_trims_surrounding_whitespace(session_list_mgr, storage):
    """用例 5：去除首尾空白。"""
    mgr, _ = session_list_mgr
    sid = await mgr.create_empty_session("alice")

    await mgr.rename_session(sid, "  项目计划  ")

    s = await storage.get_session_repo().find_session(sid)
    assert s is not None
    assert s.title == "项目计划"


@pytest.mark.asyncio
async def test_trims_before_truncating(session_list_mgr, storage):
    """用例 6：先 strip 再截断。"""
    mgr, _ = session_list_mgr
    sid = await mgr.create_empty_session("alice")

    await mgr.rename_session(sid, "  " + "x" * 60 + "  ")

    s = await storage.get_session_repo().find_session(sid)
    assert s is not None
    assert s.title == "x" * MAX_SESSION_TITLE_LENGTH


@pytest.mark.asyncio
async def test_missing_session_raises(session_list_mgr):
    """用例 7：缺失会话抛 SessionNotFoundError。"""
    mgr, _ = session_list_mgr
    with pytest.raises(SessionNotFoundError):
        await mgr.rename_session("ghost-sess-id", "标题")


@pytest.mark.asyncio
async def test_deleted_session_raises(session_list_mgr, storage):
    """用例 8：已删除会话抛 SessionNotFoundError。"""
    mgr, _ = session_list_mgr
    repo = storage.get_session_repo()
    ts = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    await repo.save_session(Session(
        session_id="del-1",
        user_id="alice",
        device_id="d1",
        last_active=ts,
        created_at=ts,
        updated_at=ts,
        title="t",
        preview="",
        message_count=1,
        is_empty=False,
        is_deleted=True,
    ))

    with pytest.raises(SessionNotFoundError):
        await mgr.rename_session("del-1", "标题")


@pytest.mark.asyncio
async def test_does_not_touch_timestamps(session_list_mgr, storage):
    """用例 9：重命名不触碰 last_active/updated_at/created_at。"""
    mgr, _ = session_list_mgr
    sid = await mgr.create_empty_session("alice")
    repo = storage.get_session_repo()

    before = await repo.find_session(sid)
    assert before is not None

    await mgr.rename_session(sid, "新标题")

    after = await repo.find_session(sid)
    assert after is not None
    assert after.created_at == before.created_at
    assert after.updated_at == before.updated_at
    assert after.last_active == before.last_active
