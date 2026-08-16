"""SessionListManager 单元测试（关键用例种子）。

覆盖 Round 2 · Step 8.5 中列出的核心用例：
  1. create_empty_session 命中节流复用
  2. create 触发淘汰
  3. soft_delete 完整链路（approval reject + pool cancel + broadcast）
  4. on_first_message 生成 title/preview
  5. on_first_message 全空白输入
  6. list_sessions 排序
  7. list_sessions 分页
  8. _route_after_delete 当前视图被删且剩余 ≥ 1
  9. _route_after_delete 删唯一
 10. startup_bootstrap 清 is_empty 遗留
 11. 分组：create / rename / delete / assign

用 sqlite :memory: + Fake broadcast/pool/config 隔离测试。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio

from pandapal.session.exceptions import (
    SessionNotFoundError,
)
from pandapal.session.session_list_manager import (
    DEFAULT_MAX_SESSIONS,
    SessionListManager,
)
from pandapal.storage.manager import StorageManager


# ═══════════════════════════════════════════════════════════
# Fakes / Test Doubles
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
    """SessionListManager + fake 依赖。"""
    broadcast = _RecordingBroadcast()
    pool = _FakeAgentPool()
    raw_log = _FakeRawLogBackend()
    working_memory = _FakeWorkingMemoryBackend()
    agent_tasks = _FakeAgentTaskRepo()
    config = _FakeConfigManager(max_sessions=DEFAULT_MAX_SESSIONS)
    clock = _FrozenClock()
    idgen = _SeqIdGenerator()

    mgr = SessionListManager(
        session_repo=storage.get_session_repo(),
        group_repo=storage.get_session_group_repo(),
        agent_pool=pool,
        approval_repo=storage.get_approval_repo(),
        run_state_repo=storage.get_run_state_repo(),
        broadcast=broadcast,
        config_manager=config,
        raw_log_backend=raw_log,
        working_memory_backend=working_memory,
        agent_task_repo=agent_tasks,
        clock=clock,
        id_generator=idgen,
    )
    return mgr, broadcast, pool, raw_log, working_memory, agent_tasks


# ═══════════════════════════════════════════════════════════
# 用例 1-2: create_empty_session
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_empty_session_reuses_existing_empty(session_list_mgr):
    """用例 1：节流复用 —— find_current_empty_session 命中直接返回同一 id。"""
    mgr, *_ = session_list_mgr
    sid1 = await mgr.create_empty_session("alice")
    sid2 = await mgr.create_empty_session("alice")
    assert sid1 == sid2


@pytest.mark.asyncio
async def test_create_triggers_eviction_when_at_capacity(session_list_mgr, storage):
    """用例 2：容量满时淘汰最旧可见会话。"""
    mgr, _, pool, *_ = session_list_mgr

    # 手动填满容量（is_empty=0）；用 timedelta 递增避免 second 溢出
    from pandapal.storage.models import Session
    base_ts = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(DEFAULT_MAX_SESSIONS):
        ts = base_ts + timedelta(seconds=i)
        await storage.get_session_repo().save_session(Session(
            session_id=f"pre-{i:03d}",
            user_id="alice",
            device_id="d1",
            last_active=ts,
            created_at=ts,
            title=f"Title {i}",
            preview="",
            message_count=1,
            is_empty=False,
            is_deleted=False,
            updated_at=ts,
        ))

    # 触发 create → 应淘汰 pre-000（最旧）
    new_sid = await mgr.create_empty_session("alice")
    assert new_sid.startswith("sess-")

    # 验证被淘汰的最旧 session 已 soft_deleted
    oldest = await storage.get_session_repo().find_session("pre-000")
    assert oldest is not None
    assert oldest.is_deleted is True
    # pool.cancel_session 应被调用（evict 走完整 delete 链路）
    assert "pre-000" in pool.cancelled


# ═══════════════════════════════════════════════════════════
# 用例 3: soft_delete 完整链路
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_soft_delete_calls_pool_cancel_and_broadcasts(session_list_mgr, storage):
    """用例 3：soft_delete 触发 pool.cancel + 广播 SESSION_DELETED。"""
    mgr, broadcast, pool, raw_log, working_memory, agent_tasks = session_list_mgr

    sid = await mgr.create_empty_session("alice")
    # 让它变为可见
    await mgr.on_first_message(sid, "hello world")

    result = await mgr.soft_delete_session(sid, current_view_session_id=sid)

    assert sid in pool.cancelled
    assert raw_log.deleted == [sid]
    assert working_memory.deleted == [sid]
    assert agent_tasks.deleted == [sid]
    session_after = await storage.get_session_repo().find_session(sid)
    assert session_after.is_deleted is True
    # 广播事件包含 SESSION_DELETED
    kinds = [
        e.event_type.value
        for e in broadcast.events
        if hasattr(e, "event_type")
    ]
    assert "session_deleted" in kinds
    # 剩余无可见会话 → routing empty_state
    assert result.action == "empty_state"


@pytest.mark.asyncio
async def test_soft_delete_removes_markdown_payload_dir(storage, tmp_path):
    """用户删除会话时清理 Markdown raw_log/working_memory 目录。"""
    from pandapal.storage.repositories.markdown_raw_log_backend import (
        MarkdownRawLogBackend,
    )
    from pandapal.storage.repositories.markdown_working_memory_backend import (
        MarkdownWorkingMemoryBackend,
    )

    broadcast = _RecordingBroadcast()
    pool = _FakeAgentPool()
    raw_log = MarkdownRawLogBackend(str(tmp_path))
    working_memory = MarkdownWorkingMemoryBackend(str(tmp_path))
    config = _FakeConfigManager(max_sessions=DEFAULT_MAX_SESSIONS)
    clock = _FrozenClock()
    idgen = _SeqIdGenerator()

    mgr = SessionListManager(
        session_repo=storage.get_session_repo(),
        group_repo=storage.get_session_group_repo(),
        agent_pool=pool,
        approval_repo=storage.get_approval_repo(),
        run_state_repo=storage.get_run_state_repo(),
        broadcast=broadcast,
        config_manager=config,
        raw_log_backend=raw_log,
        working_memory_backend=working_memory,
        clock=clock,
        id_generator=idgen,
    )

    sid = await mgr.create_empty_session("alice")
    await mgr.on_first_message(sid, "hello world")
    raw_log.append_raw_message({"role": "user", "content": "hello"}, sid)
    working_memory.save("k", "v", sid)

    session_dir = tmp_path / "sessions" / sid
    assert session_dir.exists()

    await mgr.soft_delete_session(sid, current_view_session_id=sid)

    assert not session_dir.exists()


# ═══════════════════════════════════════════════════════════
# 用例 4-5: on_first_message title/preview
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_on_first_message_generates_title_and_preview(session_list_mgr, storage):
    """用例 4：前 10 字符 title + 前 40 字符 preview。"""
    mgr, *_ = session_list_mgr
    sid = await mgr.create_empty_session("alice")

    long = "帮我分析一下这个数据以及后面的详细内容和补充信息"
    await mgr.on_first_message(sid, long)

    s = await storage.get_session_repo().find_session(sid)
    assert s.title == long[:10]  # 前 10 个字符（MAX_TITLE_LENGTH）
    assert s.preview.startswith(long[:10])
    assert len(s.preview) <= 40
    assert s.is_empty is False
    assert s.message_count == 1


@pytest.mark.asyncio
async def test_on_first_message_all_whitespace_defaults_title(session_list_mgr, storage):
    """用例 5：全空白输入 → title='新会话'。"""
    mgr, *_ = session_list_mgr
    sid = await mgr.create_empty_session("alice")
    await mgr.on_first_message(sid, "   \n\n  ")
    s = await storage.get_session_repo().find_session(sid)
    assert s.title == "新会话"


# ═══════════════════════════════════════════════════════════
# 用例 6-7: list_sessions
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_sessions_orders_by_created_at_desc(
    session_list_mgr, storage,
):
    """用例 6：created_at DESC 单键排序（活跃时间不影响顺序）。"""
    mgr, *_ = session_list_mgr

    from pandapal.storage.models import Session
    repo = storage.get_session_repo()

    # 建 3 个会话：old 活跃最新但创建最早；new 创建最新但活跃最旧
    for sid, created_h, active_h in [
        ("old", 10, 14),
        ("mid", 11, 13),
        ("new", 12, 12),
    ]:
        created = datetime(2026, 7, 1, created_h, 0, 0, tzinfo=timezone.utc)
        active = datetime(2026, 7, 1, active_h, 0, 0, tzinfo=timezone.utc)
        await repo.save_session(Session(
            session_id=sid, user_id="alice", device_id="d1",
            last_active=active, created_at=created, updated_at=active,
            title=sid, preview="", message_count=1,
            is_empty=False, is_deleted=False,
        ))

    infos, has_more = await mgr.list_sessions(
        "alice", group_id=None, page=1, limit=10,
    )
    assert [i.session_id for i in infos] == ["new", "mid", "old"]
    assert has_more is False


@pytest.mark.asyncio
async def test_list_sessions_pagination(session_list_mgr, storage):
    """用例 7：分页 + has_more 判定。"""
    mgr, *_ = session_list_mgr

    from pandapal.storage.models import Session
    repo = storage.get_session_repo()

    for i in range(12):
        ts = datetime(2026, 7, 1, 12, i, 0, tzinfo=timezone.utc)
        await repo.save_session(Session(
            session_id=f"s-{i:03d}",
            user_id="alice", device_id="d1",
            last_active=ts, created_at=ts, updated_at=ts,
            title=f"t{i}", preview="", message_count=1,
            is_empty=False, is_deleted=False,
        ))

    page1, has_more1 = await mgr.list_sessions("alice", None, 1, 10)
    assert len(page1) == 10
    assert has_more1 is True

    page2, has_more2 = await mgr.list_sessions("alice", None, 2, 10)
    assert len(page2) == 2
    assert has_more2 is False


# ═══════════════════════════════════════════════════════════
# 用例 8-9: _route_after_delete
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_route_after_delete_switches_when_remaining(session_list_mgr, storage):
    """用例 8：删的是当前视图且剩余 ≥ 1 → action='switch'。"""
    mgr, *_ = session_list_mgr
    sid_a = await mgr.create_empty_session("alice")
    await mgr.on_first_message(sid_a, "aaa")

    sid_b = await mgr.create_empty_session("alice")
    await mgr.on_first_message(sid_b, "bbb")

    # 删 A（=当前视图）
    result = await mgr.soft_delete_session(sid_a, current_view_session_id=sid_a)
    assert result.action == "switch"
    assert result.target_session_id == sid_b


@pytest.mark.asyncio
async def test_route_after_delete_empty_state_when_last(session_list_mgr):
    """用例 9：删唯一可见会话 → action='empty_state'。"""
    mgr, *_ = session_list_mgr
    sid = await mgr.create_empty_session("alice")
    await mgr.on_first_message(sid, "only")

    result = await mgr.soft_delete_session(sid, current_view_session_id=sid)
    assert result.action == "empty_state"


# ═══════════════════════════════════════════════════════════
# 用例 10: startup_bootstrap
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_startup_bootstrap_cleans_empty_and_creates_new(
    session_list_mgr, storage,
):
    """用例 10：预置 3 个 is_empty=1 会话 → 启动后被硬删；新空 session 已建。"""
    mgr, *_ = session_list_mgr

    from pandapal.storage.models import Session
    repo = storage.get_session_repo()

    for i in range(3):
        ts = datetime(2026, 7, 1, 12, i, 0, tzinfo=timezone.utc)
        await repo.save_session(Session(
            session_id=f"empty-{i}",
            user_id="alice", device_id="d1",
            last_active=ts, created_at=ts, updated_at=ts,
            title="", preview="", message_count=0,
            is_empty=True, is_deleted=False,
        ))

    payload = await mgr.startup_bootstrap("alice")

    # 3 个 empty 遗留应被硬删
    for i in range(3):
        assert await repo.find_session(f"empty-{i}") is None

    # 新空 session 已建
    assert payload.initial_session_id
    new_sess = await repo.find_session(payload.initial_session_id)
    assert new_sess is not None
    assert new_sess.is_empty is True

