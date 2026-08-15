"""SessionGroupManager 单元测试。

覆盖拆分后 SessionGroupManager 的核心行为：
  1. create_group（含重复名 / 配额 / 非法名）
  2. rename_group（广播 SESSION_UPDATED 携带新 group_name）
  3. delete_group（不级联：解除关联 + 广播）
  4. assign_to_group（1:1 关联 + 越权保护）
  5. 正向记录维护（assign / on_session_removed / 重新 assign）
  6. list_group_sessions（正向记录快路径）
  7. handler 级联删除（delete_sessions=True）

Fake 依赖复用 test_session_list_manager 的测试替身。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from pandapal.session.exceptions import (
    GroupNameConflict,
    GroupNameInvalid,
    GroupNotFoundError,
    GroupQuotaExceeded,
)
from pandapal.session.session_group_handler import SessionGroupHandler
from pandapal.session.session_group_manager import SessionGroupManager
from pandapal.session.session_list_manager import (
    DEFAULT_MAX_GROUPS,
    SessionListManager,
)
from pandapal.session.tests.test_session_list_manager import (
    _FakeAgentPool,
    _FakeAgentTaskRepo,
    _FakeConfigManager,
    _FakeRawLogBackend,
    _FakeWorkingMemoryBackend,
    _FrozenClock,
    _RecordingBroadcast,
    _SeqIdGenerator,
)
from pandapal.storage.manager import StorageManager


@pytest_asyncio.fixture
async def storage(tmp_path):
    """SQLite 模式 StorageManager。"""
    db_path = str(tmp_path / "test.db")
    mgr = StorageManager(storage_path=db_path, storage_mode="sqlite")
    await mgr.initialize_storage()
    yield mgr
    await mgr.shutdown_storage()


@pytest_asyncio.fixture
async def group_stack(storage):
    """SessionGroupManager + SessionListManager（已接线 on_session_removed 回调）。"""
    broadcast = _RecordingBroadcast()
    clock = _FrozenClock()
    idgen = _SeqIdGenerator()

    group_mgr = SessionGroupManager(
        session_repo=storage.get_session_repo(),
        group_repo=storage.get_session_group_repo(),
        broadcast=broadcast,
        clock=clock,
        id_generator=idgen,
    )

    list_mgr = SessionListManager(
        session_repo=storage.get_session_repo(),
        group_repo=storage.get_session_group_repo(),
        agent_pool=_FakeAgentPool(),
        approval_repo=storage.get_approval_repo(),
        run_state_repo=storage.get_run_state_repo(),
        broadcast=broadcast,
        config_manager=_FakeConfigManager(),
        raw_log_backend=_FakeRawLogBackend(),
        working_memory_backend=_FakeWorkingMemoryBackend(),
        agent_task_repo=_FakeAgentTaskRepo(),
        clock=clock,
        id_generator=idgen,
        on_session_removed=group_mgr.on_session_removed,
    )

    return group_mgr, list_mgr, broadcast


def _session_updated_events(broadcast, sid: str) -> list:
    return [
        e for e in broadcast.events
        if getattr(e, "event_type", None) is not None
        and e.event_type.value == "session_updated"
        and e.payload.get("session_info", {}).get("session_id") == sid
    ]


# ═══════════════════════════════════════════════════════════
# 分组 CRUD
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_group_and_assign_to_session(group_stack, storage):
    group_mgr, list_mgr, *_ = group_stack
    sid = await list_mgr.create_empty_session("alice")
    await list_mgr.on_first_message(sid, "hi")

    gid = await group_mgr.create_group("alice", "工作")
    assert gid.startswith("grp-")

    await group_mgr.assign_to_group(user_id="alice", session_id=sid, group_id=gid)
    s = await storage.get_session_repo().find_session(sid)
    assert s.group_id == gid


@pytest.mark.asyncio
async def test_rename_group_broadcasts_session_updated(group_stack):
    group_mgr, list_mgr, broadcast = group_stack
    sid = await list_mgr.create_empty_session("alice")
    await list_mgr.on_first_message(sid, "hi")
    gid = await group_mgr.create_group("alice", "工作")
    await group_mgr.assign_to_group("alice", sid, gid)

    broadcast.events.clear()
    await group_mgr.rename_group("alice", gid, "生活")

    updated = _session_updated_events(broadcast, sid)
    assert updated, "重命名分组后应广播 SESSION_UPDATED"
    assert updated[-1].payload["session_info"]["group_name"] == "生活"


@pytest.mark.asyncio
async def test_create_group_duplicate_name_raises(group_stack):
    group_mgr, *_ = group_stack
    await group_mgr.create_group("alice", "工作")
    with pytest.raises(GroupNameConflict):
        await group_mgr.create_group("alice", "工作")


@pytest.mark.asyncio
async def test_create_group_quota_exceeded(group_stack):
    group_mgr, *_ = group_stack
    for i in range(DEFAULT_MAX_GROUPS):
        await group_mgr.create_group("alice", f"分组{i}")
    with pytest.raises(GroupQuotaExceeded):
        await group_mgr.create_group("alice", "溢出分组")


@pytest.mark.asyncio
async def test_create_group_invalid_name_empty(group_stack):
    group_mgr, *_ = group_stack
    with pytest.raises(GroupNameInvalid):
        await group_mgr.create_group("alice", "   ")


@pytest.mark.asyncio
async def test_delete_group_clears_association(group_stack, storage):
    group_mgr, list_mgr, broadcast = group_stack
    sid = await list_mgr.create_empty_session("alice")
    await list_mgr.on_first_message(sid, "hi")
    gid = await group_mgr.create_group("alice", "工作")
    await group_mgr.assign_to_group("alice", sid, gid)

    broadcast.events.clear()
    await group_mgr.delete_group("alice", gid)

    s = await storage.get_session_repo().find_session(sid)
    assert s.group_id is None

    updated = _session_updated_events(broadcast, sid)
    assert updated, "detach 删除分组后应广播 SESSION_UPDATED"
    assert updated[-1].payload["session_info"]["group_id"] is None


@pytest.mark.asyncio
async def test_assign_group_not_found_raises(group_stack):
    group_mgr, list_mgr, *_ = group_stack
    with pytest.raises(GroupNotFoundError):
        sid = await list_mgr.create_empty_session("alice")
        await list_mgr.on_first_message(sid, "hi")
        await group_mgr.assign_to_group("alice", sid, "ghost-group")


# ═══════════════════════════════════════════════════════════
# 正向记录维护
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_assign_to_group_maintains_forward_index(group_stack, storage):
    """assign 后 group.session_ids 正向记录应包含该会话。"""
    group_mgr, list_mgr, *_ = group_stack
    sid = await list_mgr.create_empty_session("alice")
    await list_mgr.on_first_message(sid, "hi")
    gid = await group_mgr.create_group("alice", "工作")

    await group_mgr.assign_to_group("alice", sid, gid)

    g = await storage.get_session_group_repo().find_group(gid)
    assert sid in g.session_ids


@pytest.mark.asyncio
async def test_reassign_updates_both_forward_indexes(group_stack, storage):
    """重新 assign 到另一组时，旧组移除、新组加入。"""
    group_mgr, list_mgr, *_ = group_stack
    sid = await list_mgr.create_empty_session("alice")
    await list_mgr.on_first_message(sid, "hi")
    gid_a = await group_mgr.create_group("alice", "A")
    gid_b = await group_mgr.create_group("alice", "B")
    await group_mgr.assign_to_group("alice", sid, gid_a)

    await group_mgr.assign_to_group("alice", sid, gid_b)

    repo = storage.get_session_group_repo()
    assert sid not in (await repo.find_group(gid_a)).session_ids
    assert sid in (await repo.find_group(gid_b)).session_ids


@pytest.mark.asyncio
async def test_on_session_removed_syncs_forward_index(group_stack, storage):
    """soft_delete 会话后，正向记录应同步移除该 session_id。"""
    group_mgr, list_mgr, *_ = group_stack
    sid = await list_mgr.create_empty_session("alice")
    await list_mgr.on_first_message(sid, "hi")
    gid = await group_mgr.create_group("alice", "工作")
    await group_mgr.assign_to_group("alice", sid, gid)

    await list_mgr.soft_delete_session(sid)

    g = await storage.get_session_group_repo().find_group(gid)
    assert sid not in g.session_ids


@pytest.mark.asyncio
async def test_list_group_sessions_uses_forward_index(group_stack):
    """list_group_sessions 只返回组内会话（不返回组外）。"""
    group_mgr, list_mgr, *_ = group_stack
    sid_in = await list_mgr.create_empty_session("alice")
    await list_mgr.on_first_message(sid_in, "in-group")
    sid_out = await list_mgr.create_empty_session("alice")
    await list_mgr.on_first_message(sid_out, "out-of-group")
    gid = await group_mgr.create_group("alice", "工作")
    await group_mgr.assign_to_group("alice", sid_in, gid)

    infos, has_more = await group_mgr.list_group_sessions(
        "alice", gid, page=1, limit=10,
    )
    ids = [i.session_id for i in infos]
    assert sid_in in ids
    assert sid_out not in ids
    assert has_more is False


# ═══════════════════════════════════════════════════════════
# handler 级联删除
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handler_delete_group_cascade_deletes_sessions(group_stack, storage):
    """delete_sessions=True 时组内会话应被级联软删除，组外不受影响。"""
    group_mgr, list_mgr, _ = group_stack
    handler = SessionGroupHandler(
        group_manager=group_mgr, session_list_manager=list_mgr, user_id="alice",
    )
    sid_a = await list_mgr.create_empty_session("alice")
    await list_mgr.on_first_message(sid_a, "in-group")
    sid_b = await list_mgr.create_empty_session("alice")
    await list_mgr.on_first_message(sid_b, "out-of-group")
    gid = await group_mgr.create_group("alice", "工作")
    await group_mgr.assign_to_group("alice", sid_a, gid)

    result = await handler.handle_group_mutate({
        "op": "delete", "group_id": gid, "delete_sessions": True,
    })
    assert result is None  # 成功路径豁免（manager 自广播）

    repo = storage.get_session_repo()
    a = await repo.find_session(sid_a)
    assert a is None or a.is_deleted
    b = await repo.find_session(sid_b)
    assert b is not None and not b.is_deleted
    # 分组本身应被删除
    assert await storage.get_session_group_repo().find_group(gid) is None


# ═══════════════════════════════════════════════════════════
# 正向记录兜底 / backfill
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_group_sessions_fallback_when_forward_index_empty(
    group_stack, storage,
):
    """正向记录为空但组内有会话时，应回退 group_id 反查（兜底）。"""
    group_mgr, list_mgr, *_ = group_stack
    sid = await list_mgr.create_empty_session("alice")
    await list_mgr.on_first_message(sid, "hi")
    gid = await group_mgr.create_group("alice", "工作")
    await group_mgr.assign_to_group("alice", sid, gid)

    # 人为清空正向记录，模拟升级后/漂移
    await storage.get_session_group_repo().set_session_ids(gid, [])

    infos, has_more = await group_mgr.list_group_sessions(
        "alice", gid, page=1, limit=10,
    )
    assert [i.session_id for i in infos] == [sid]
    assert has_more is False


@pytest.mark.asyncio
async def test_backfill_forward_index_repairs_drift(group_stack, storage):
    """backfill 应依据 sessions.group_id 幂等重建正向记录。"""
    group_mgr, list_mgr, *_ = group_stack
    sid = await list_mgr.create_empty_session("alice")
    await list_mgr.on_first_message(sid, "hi")
    gid = await group_mgr.create_group("alice", "工作")
    await group_mgr.assign_to_group("alice", sid, gid)

    # 制造漂移：清空正向记录
    await storage.get_session_group_repo().set_session_ids(gid, [])
    assert await storage.get_session_group_repo().get_session_ids(gid) == []

    changed = await group_mgr.backfill_forward_index("alice")
    assert changed == 1
    assert sid in await storage.get_session_group_repo().get_session_ids(gid)

    # 幂等：再次 backfill 一致，不重写
    changed2 = await group_mgr.backfill_forward_index("alice")
    assert changed2 == 0
