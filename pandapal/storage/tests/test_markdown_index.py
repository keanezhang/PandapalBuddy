"""Markdown 内存索引改造的测试（unit + integration）。

覆盖设计文档 markdown_index.design.md 的 U1–U9 / I1–I16 用例。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from pandapal.storage.models import Session
from pandapal.storage.repositories._markdown_base import MarkdownBaseRepository
from pandapal.storage.repositories.markdown_session_repo import MarkdownSessionRepository


# ──────────────────────────────────────────────
# 测试辅助
# ──────────────────────────────────────────────

def _write_record(path, data: dict) -> None:
    """以 JSON front matter 格式写入一条记录文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{json.dumps(data)}\n---\n", encoding="utf-8")


def _read_front_matter(path) -> dict | None:
    """解析 JSON front matter（仅测试断言用）。"""
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return None
    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return None
    return json.loads(content[3:end_idx].strip())


# ──────────────────────────────────────────────
# 单元测试（unit）
# ──────────────────────────────────────────────

# inv-1 索引内容 + R1 误纳附属文件 [P0]
def test_base_record_glob_patterns_flat_and_partitioned(tmp_path):
    flat = MarkdownBaseRepository(str(tmp_path), "devices")
    part = MarkdownBaseRepository(str(tmp_path), "run_states", session_partitioned=True)

    assert flat._record_glob_patterns() == [os.path.join(str(tmp_path), "devices", "*.md")]
    assert part._record_glob_patterns() == [
        os.path.join(str(tmp_path), "sessions", "*", "run_states", "*.md")
    ]


# inv-5 legacy 兼容 + R5 legacy 漏读 [P1]
def test_session_record_glob_patterns_new_and_legacy(tmp_path):
    repo = MarkdownSessionRepository(str(tmp_path))

    assert repo._record_glob_patterns() == [
        os.path.join(str(tmp_path), "sessions", "*", "session.md"),
        os.path.join(str(tmp_path), "sessions", "*.md"),
    ]


# inv-2 写穿透一致性 + R2/R3 漂移/污染 [P0]
def test_index_set_del_skip_when_index_none(tmp_path):
    repo = MarkdownBaseRepository(str(tmp_path), "devices")
    assert repo._index is None

    repo._index_set("/tmp/x.md", {"a": 1})
    repo._index_del("/tmp/x.md")

    assert repo._index is None


# inv-6 invalidate 语义 + R6 未重建 [P1]
def test_invalidate_clears_index(tmp_path):
    repo = MarkdownBaseRepository(str(tmp_path), "devices")
    repo._index = {"k": {"a": 1}}

    repo.invalidate()

    assert repo._index is None


# inv-3 懒加载 + inv-4 幂等构建 + R4 并发竞态 [P1]
@pytest.mark.asyncio
async def test_ensure_index_sequential_builds_once(tmp_path, monkeypatch):
    repo = MarkdownSessionRepository(str(tmp_path))
    calls = [0]

    async def fake_build():
        calls[0] += 1
        return {"k": {"session_id": "s1"}}

    monkeypatch.setattr(repo, "_build_index", fake_build)

    await repo._ensure_index()
    assert repo._index == {"k": {"session_id": "s1"}}
    assert calls[0] == 1

    await repo._ensure_index()
    assert calls[0] == 1


# inv-4 幂等构建 + R4 并发竞态 [P1]
@pytest.mark.asyncio
async def test_ensure_index_concurrent_builds_once(tmp_path, monkeypatch):
    repo = MarkdownSessionRepository(str(tmp_path))
    calls = [0]

    async def slow_build():
        calls[0] += 1
        await asyncio.sleep(0.05)
        return {"k": {"session_id": "s1"}}

    monkeypatch.setattr(repo, "_build_index", slow_build)

    await asyncio.gather(*[repo._ensure_index() for _ in range(50)])

    assert calls[0] == 1
    assert repo._index == {"k": {"session_id": "s1"}}


# inv-8 时间解析语义
def test_parse_datetime_aware_invalid_empty_none():
    assert MarkdownSessionRepository._parse_datetime("2024-01-01T12:00:00+00:00") == (
        datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    )
    assert MarkdownSessionRepository._parse_datetime("not-a-date") is None
    assert MarkdownSessionRepository._parse_datetime("") is None
    assert MarkdownSessionRepository._parse_datetime(None) is None


# ──────────────────────────────────────────────
# 集成测试（integration，真实文件系统 tmp_path）
# ──────────────────────────────────────────────

# inv-1 索引内容 + R1 误纳附属大文件 [P0]
@pytest.mark.asyncio
async def test_session_index_excludes_aux_files_and_reads_only_records(tmp_path):
    base = tmp_path / "md"
    repo = MarkdownSessionRepository(str(base))

    _write_record(base / "sessions" / "s1" / "session.md", {"session_id": "s1", "user_id": "u1"})
    _write_record(base / "sessions" / "s2" / "session.md", {"session_id": "s2", "user_id": "u1"})
    _write_record(base / "sessions" / "s1" / "raw_log.md", {"raw": "x" * 1000})
    _write_record(base / "sessions" / "s1" / "run_states" / "r1.md", {"run_id": "r1"})
    _write_record(base / "sessions" / "s1" / "approvals" / "a1.md", {"approval_id": "a1"})
    _write_record(base / "sessions" / "s1" / "agent_tasks" / "t1.md", {"task_id": "t1"})
    _write_record(base / "sessions" / "s1" / "meta.json", {"meta": 1})

    original = repo._sync_read_entity
    calls = [0]

    def counting_read(fp):
        calls[0] += 1
        return original(fp)

    repo._sync_read_entity = counting_read

    entities = await repo._list_entities()

    assert {e["session_id"] for e in entities} == {"s1", "s2"}
    assert all(k.endswith("session.md") for k in repo._index)
    assert calls[0] == 2


# inv-5 legacy 兼容 + R5 legacy 漏读 [P1]
@pytest.mark.asyncio
async def test_legacy_flat_session_indexed_in_mixed_layout(tmp_path):
    repo = MarkdownSessionRepository(str(tmp_path))

    _write_record(tmp_path / "sessions" / "s_legacy.md", {"session_id": "s_legacy", "user_id": "u1"})
    _write_record(tmp_path / "sessions" / "s_new" / "session.md", {"session_id": "s_new", "user_id": "u1"})

    entities = await repo._list_entities()

    assert {e["session_id"] for e in entities} == {"s_legacy", "s_new"}


# inv-1 索引内容 + R1 误纳 [P0]
@pytest.mark.asyncio
async def test_base_flat_layout_non_recursive(tmp_path):
    repo = MarkdownBaseRepository(str(tmp_path), "devices")

    _write_record(tmp_path / "devices" / "d1.md", {"device_id": "d1"})
    _write_record(tmp_path / "devices" / "nested" / "d2.md", {"device_id": "d2"})
    (tmp_path / "devices" / "notes.txt").write_text("plain", encoding="utf-8")

    entities = await repo._list_entities()

    assert len(entities) == 1
    assert entities[0]["device_id"] == "d1"


# inv-1 索引内容 + R1 误纳 [P0]
@pytest.mark.asyncio
async def test_partitioned_layout_only_indexes_own_entity(tmp_path):
    repo = MarkdownBaseRepository(str(tmp_path), "run_states", session_partitioned=True)

    _write_record(tmp_path / "sessions" / "s1" / "run_states" / "r1.md", {"run_id": "r1", "session_id": "s1"})
    _write_record(tmp_path / "sessions" / "s1" / "session.md", {"session_id": "s1"})
    _write_record(tmp_path / "sessions" / "s1" / "raw_log.md", {"raw": "x"})
    _write_record(tmp_path / "sessions" / "s1" / "approvals" / "a1.md", {"approval_id": "a1"})

    entities = await repo._list_entities()

    assert len(entities) == 1
    assert entities[0]["run_id"] == "r1"


# R9 解析崩溃/误纳 [P2]
@pytest.mark.asyncio
async def test_unparsable_or_non_dict_front_matter_skipped(tmp_path):
    repo = MarkdownBaseRepository(str(tmp_path), "devices")

    _write_record(tmp_path / "devices" / "good.md", {"device_id": "good"})
    (tmp_path / "devices" / "bad_json.md").write_text("---\n{invalid json\n---", encoding="utf-8")
    (tmp_path / "devices" / "list_fm.md").write_text("---\n[1,2,3]\n---\n", encoding="utf-8")
    (tmp_path / "devices" / "no_fm.md").write_text("plain text without front matter", encoding="utf-8")
    (tmp_path / "devices" / "empty_dict.md").write_text("---\n{}\n---\n", encoding="utf-8")

    entities = await repo._list_entities()

    assert len(entities) == 1
    assert entities[0]["device_id"] == "good"


# inv-2 写穿透一致性 + R2 写后漂移 [P0]
@pytest.mark.asyncio
async def test_save_session_write_through(tmp_path):
    base = tmp_path
    repo = MarkdownSessionRepository(str(base))
    await repo._list_entities()

    now = datetime.now(timezone.utc)
    await repo.save_session(
        Session(session_id="s1", user_id="u1", device_id="d1", last_active=now, created_at=now)
    )

    entities = await repo._list_entities()
    assert len(entities) == 1
    assert entities[0]["session_id"] == "s1"

    assert [s.session_id for s in await repo.find_sessions_by_user("u1")] == ["s1"]

    file_path = base / "sessions" / "s1" / "session.md"
    assert file_path.exists()
    key = os.path.normpath(str(file_path))
    assert key in repo._index
    assert _read_front_matter(file_path) == repo._index[key]


# inv-2 写穿透一致性 + R2 写后漂移 [P0]
@pytest.mark.asyncio
async def test_update_session_meta_write_through(tmp_path):
    base = tmp_path
    repo = MarkdownSessionRepository(str(base))
    now = datetime.now(timezone.utc)
    await repo.save_session(
        Session(
            session_id="s1",
            user_id="u1",
            device_id="d1",
            title="old",
            last_active=now,
            created_at=now,
        )
    )
    await repo._list_entities()

    await repo.update_session_meta("s1", title="New Title")

    found = await repo.find_sessions_by_user("u1")
    assert found[0].title == "New Title"
    assert _read_front_matter(base / "sessions" / "s1" / "session.md")["title"] == "New Title"


# inv-2 写穿透一致性 + R2 写后漂移 [P0]
@pytest.mark.asyncio
async def test_soft_delete_write_through(tmp_path):
    repo = MarkdownSessionRepository(str(tmp_path))
    now = datetime.now(timezone.utc)
    for sid in ("s1", "s2"):
        await repo.save_session(
            Session(
                session_id=sid,
                user_id="u1",
                device_id="d1",
                is_empty=False,
                is_deleted=False,
                last_active=now,
                created_at=now,
            )
        )

    assert await repo.count_visible_sessions("u1") == 2

    await repo.soft_delete_session("s1")

    assert await repo.count_visible_sessions("u1") == 1
    assert (await repo.find_session("s1")).is_deleted is True


# inv-2 写穿透一致性 + R2 写后漂移 [P0]
@pytest.mark.asyncio
async def test_hard_delete_write_through(tmp_path):
    base = tmp_path
    repo = MarkdownSessionRepository(str(base))
    now = datetime.now(timezone.utc)
    for sid in ("s1", "s2"):
        await repo.save_session(
            Session(session_id=sid, user_id="u1", device_id="d1", last_active=now, created_at=now)
        )
    await repo._list_entities()

    await repo.delete_session("s1")

    assert [e["session_id"] for e in await repo._list_entities()] == ["s2"]
    assert os.path.normpath(str(base / "sessions" / "s1" / "session.md")) not in repo._index
    assert not (base / "sessions" / "s1" / "session.md").exists()


# inv-2 写穿透一致性 + inv-3 懒加载
@pytest.mark.asyncio
async def test_write_before_index_build_is_picked_up_on_first_list(tmp_path):
    repo = MarkdownSessionRepository(str(tmp_path))
    assert repo._index is None

    now = datetime.now(timezone.utc)
    await repo.save_session(
        Session(session_id="s1", user_id="u1", device_id="d1", last_active=now, created_at=now)
    )

    entities = await repo._list_entities()

    assert {e["session_id"] for e in entities} == {"s1"}
    assert repo._index is not None
    assert {e["session_id"] for e in repo._index.values()} == {"s1"}


# inv-2 写穿透一致性 + R3 落盘失败污染 [P0]
@pytest.mark.asyncio
async def test_write_failure_keeps_index_unchanged(tmp_path):
    base = tmp_path
    repo = MarkdownBaseRepository(str(base), "devices")
    await repo._write_entity(str(base / "devices" / "d1.md"), {"device_id": "d1"})
    await repo._list_entities()

    (base / "devices" / "blocked").write_text("", encoding="utf-8")

    with pytest.raises((FileExistsError, OSError)):
        await repo._write_entity(
            str(base / "devices" / "blocked" / "d2.md"), {"device_id": "d2"}
        )

    assert set(repo._index) == {os.path.normpath(str(base / "devices" / "d1.md"))}
    assert not (base / "devices" / "blocked" / "d2.md").exists()


# inv-2 写穿透一致性 + R3 删盘失败污染 [P0]
@pytest.mark.asyncio
async def test_delete_failure_keeps_index_unchanged(tmp_path, monkeypatch):
    base = tmp_path
    repo = MarkdownBaseRepository(str(base), "devices")
    await repo._write_entity(str(base / "devices" / "d1.md"), {"device_id": "d1"})
    await repo._list_entities()

    def raise_oserror(path):
        raise OSError("disk error")

    monkeypatch.setattr(os, "remove", raise_oserror)

    result = await repo._delete_entity(str(base / "devices" / "d1.md"))

    assert result is False
    assert os.path.normpath(str(base / "devices" / "d1.md")) in repo._index
    assert (base / "devices" / "d1.md").exists()


# inv-3 懒加载 + R1 误纳附属文件 [P0]
@pytest.mark.asyncio
async def test_lazy_load_reads_once(tmp_path):
    base = tmp_path
    repo = MarkdownSessionRepository(str(base))
    _write_record(base / "sessions" / "s1" / "session.md", {"session_id": "s1", "user_id": "u1"})
    _write_record(base / "sessions" / "s2" / "session.md", {"session_id": "s2", "user_id": "u1"})
    _write_record(base / "sessions" / "s1" / "raw_log.md", {"raw": "x"})

    original = repo._sync_read_entity
    calls = [0]

    def counting_read(fp):
        calls[0] += 1
        return original(fp)

    repo._sync_read_entity = counting_read

    assert repo._index is None

    await repo._list_entities()
    reads_after_first = calls[0]
    await repo._list_entities()

    assert reads_after_first == 2
    assert calls[0] == reads_after_first
    assert repo._index is not None


# inv-5 legacy 兼容 + R5 legacy 未清理 [P1]
@pytest.mark.asyncio
async def test_legacy_end_to_end_save_cleans_up(tmp_path):
    base = tmp_path
    repo = MarkdownSessionRepository(str(base))
    legacy_path = base / "sessions" / "s_old.md"
    new_path = base / "sessions" / "s_old" / "session.md"

    _write_record(
        legacy_path,
        {"session_id": "s_old", "user_id": "u1", "is_empty": False, "is_deleted": False, "title": "legacy"},
    )

    found = await repo.find_session("s_old")
    assert found is not None
    assert found.session_id == "s_old"
    assert {s.session_id for s in await repo.find_sessions_by_user("u1")} == {"s_old"}

    now = datetime.now(timezone.utc)
    await repo.save_session(
        Session(session_id="s_old", user_id="u1", device_id="d1", last_active=now, created_at=now)
    )

    assert not legacy_path.exists()
    assert new_path.exists()
    assert [e["session_id"] for e in await repo._list_entities()] == ["s_old"]
    assert set(repo._index) == {os.path.normpath(str(new_path))}


# inv-6 invalidate 语义 + R6 未重建 [P1]
@pytest.mark.asyncio
async def test_invalidate_rebuild_reads_disk(tmp_path):
    base = tmp_path
    repo = MarkdownSessionRepository(str(base))
    now = datetime.now(timezone.utc)
    await repo.save_session(
        Session(
            session_id="s1",
            user_id="u1",
            device_id="d1",
            title="old",
            last_active=now,
            created_at=now,
        )
    )
    await repo._list_entities()

    _write_record(
        base / "sessions" / "s1" / "session.md",
        {"session_id": "s1", "user_id": "u1", "device_id": "d1", "title": "new"},
    )

    assert (await repo._list_entities())[0]["title"] == "old"

    repo.invalidate()
    assert repo._index is None

    assert (await repo._list_entities())[0]["title"] == "new"



