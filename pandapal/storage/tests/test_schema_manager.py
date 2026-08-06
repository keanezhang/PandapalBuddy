"""SchemaManager 测试。"""

from __future__ import annotations

import pytest

from pandapal.storage.manager import StorageManager


@pytest.mark.asyncio
async def test_schema_version_after_init(memory_storage):
    """初始化后 schema_version 应为 1（执行了 v001 迁移）。"""
    version = await memory_storage._schema_manager.get_current_version()
    assert version == 1


@pytest.mark.asyncio
async def test_migrations_are_idempotent(memory_storage):
    """重复执行迁移应返回 0（无新迁移）。"""
    count = await memory_storage._schema_manager.run_migrations()
    assert count == 0


@pytest.mark.asyncio
async def test_tables_created(memory_storage):
    """验证所有 11 个表已创建。"""
    expected_tables = {
        "schema_version",
        "user_configs",
        "sessions",
        "task_definitions",
        "task_executions",
        "device_registrations",
        "approval_requests",
        "avatar_configs",
        "run_states",
        "raw_log",
        "session_summaries",
    }

    conn = memory_storage._connection
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    rows = await cursor.fetchall()
    actual_tables = {row[0] for row in rows}

    assert expected_tables.issubset(actual_tables), (
        f"Missing tables: {expected_tables - actual_tables}"
    )
