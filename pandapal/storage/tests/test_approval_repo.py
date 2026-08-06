"""ApprovalRepository 测试。

重点验证 BL7 原子 compare-and-update 语义。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pandapal.storage.exceptions import StorageDuplicateError
from pandapal.storage.manager import StorageManager
from pandapal.storage.models import ApprovalDecision, ApprovalRequest


@pytest.mark.asyncio
async def test_save_and_find(memory_storage):
    """保存并查找审批请求。"""
    repo = memory_storage.get_approval_repo()
    now = datetime.now(timezone.utc)
    request = ApprovalRequest(
        approval_id="r1",
        user_id="u1",
        run_id="run1",
        tool_name="file_write",
        tool_args_summary="Write to /etc/passwd",
        timeout_seconds=300,
        created_at=now,
    )
    await repo.save_approval_request(request)
    found = await repo.find_approval_request("r1")

    assert found is not None
    assert found.approval_id == "r1"
    assert found.tool_name == "file_write"
    assert found.status == "pending"


@pytest.mark.asyncio
async def test_duplicate_insert_raises(memory_storage):
    """重复 INSERT 同一个 approval_id 抛出 StorageDuplicateError。"""
    repo = memory_storage.get_approval_repo()
    request = ApprovalRequest(
        approval_id="r1", user_id="u1", run_id="run1", tool_name="bash",
    )
    await repo.save_approval_request(request)

    with pytest.raises(StorageDuplicateError):
        await repo.save_approval_request(request)


@pytest.mark.asyncio
async def test_find_pending(memory_storage):
    """查找待审批请求。"""
    repo = memory_storage.get_approval_repo()

    await repo.save_approval_request(ApprovalRequest(
        approval_id="r1", user_id="u1", run_id="run1", tool_name="bash",
    ))
    await repo.save_approval_request(ApprovalRequest(
        approval_id="r2", user_id="u1", run_id="run2", tool_name="file_ops",
    ))

    pending = await repo.find_pending_approval_requests("u1")
    assert len(pending) == 2


@pytest.mark.asyncio
async def test_resolve_success(memory_storage):
    """成功解决审批请求（BL7）。"""
    repo = memory_storage.get_approval_repo()
    now = datetime.now(timezone.utc)

    await repo.save_approval_request(ApprovalRequest(
        approval_id="r1", user_id="u1", run_id="run1", tool_name="bash",
    ))

    result = await repo.resolve_approval_request(
        "r1", ApprovalDecision.APPROVED, now
    )
    assert result is True

    found = await repo.find_approval_request("r1")
    assert found is not None
    assert found.status == "resolved"
    assert found.decision == "approved"


@pytest.mark.asyncio
async def test_resolve_with_bare_string_decision_sqlite(tmp_path):
    """回归：sqlite 后端 decision 传裸字符串（非 ApprovalDecision 枚举）不得崩。

    历史事故：HITLBridge 曾把入站 IPC 的裸字符串 decision（前端发 "approved"）直接
    透传给 repo，sqlite 后端 `decision.value` 崩为 `'str' object has no attribute 'value'`
    （markdown 后端有 str 兜底故不崩），切 sqlite 后才暴露。此处显式用 **sqlite** 存储，
    锁定它与 markdown 同口径：枚举或裸字符串都能解析并落对值。
    """
    manager = StorageManager(storage_path=str(tmp_path / "t.db"), storage_mode="sqlite")
    await manager.initialize_storage()
    try:
        repo = manager.get_approval_repo()
        now = datetime.now(timezone.utc)
        await repo.save_approval_request(ApprovalRequest(
            approval_id="r-str", user_id="u1", run_id="run1", tool_name="bash",
        ))

        # 关键：传裸字符串而非 ApprovalDecision.REJECTED（修复前此调用即崩）
        result = await repo.resolve_approval_request("r-str", "rejected", now)  # type: ignore[arg-type]
        assert result is True

        found = await repo.find_approval_request("r-str")
        assert found is not None
        assert found.status == "resolved"
        assert found.decision == "rejected"
    finally:
        await manager.shutdown_storage()


@pytest.mark.asyncio
async def test_resolve_already_resolved_returns_false(memory_storage):
    """BL7: 已解决的请求再次 resolve 返回 False（幂等退出）。"""
    repo = memory_storage.get_approval_repo()
    now = datetime.now(timezone.utc)

    await repo.save_approval_request(ApprovalRequest(
        approval_id="r1", user_id="u1", run_id="run1", tool_name="bash",
    ))

    # 第一次解决
    result1 = await repo.resolve_approval_request(
        "r1", ApprovalDecision.APPROVED, now
    )
    assert result1 is True

    # 第二次解决（BL7: 不允许二次决策）
    result2 = await repo.resolve_approval_request(
        "r1", ApprovalDecision.REJECTED, now
    )
    assert result2 is False

    # 状态不变
    found = await repo.find_approval_request("r1")
    assert found is not None
    assert found.decision == "approved"


@pytest.mark.asyncio
async def test_resolve_timeout(memory_storage):
    """超时自动解决标记 is_auto_timeout。"""
    repo = memory_storage.get_approval_repo()
    now = datetime.now(timezone.utc)

    await repo.save_approval_request(ApprovalRequest(
        approval_id="r1", user_id="u1", run_id="run1", tool_name="bash",
    ))

    result = await repo.resolve_approval_request(
        "r1", ApprovalDecision.TIMEOUT, now
    )
    assert result is True

    found = await repo.find_approval_request("r1")
    assert found is not None
    assert found.is_auto_timeout is True


@pytest.mark.asyncio
async def test_delete_expired(memory_storage):
    """删除过期审批请求。"""
    repo = memory_storage.get_approval_repo()
    now = datetime.now(timezone.utc)

    await repo.save_approval_request(ApprovalRequest(
        approval_id="old",
        user_id="u1",
        run_id="run1",
        tool_name="bash",
        created_at=now - timedelta(days=7),
    ))
    await repo.save_approval_request(ApprovalRequest(
        approval_id="new",
        user_id="u1",
        run_id="run2",
        tool_name="file_ops",
        created_at=now,
    ))

    deleted = await repo.delete_expired_approval_requests(now - timedelta(days=1))
    assert deleted == 1
    assert await repo.find_approval_request("old") is None
    assert await repo.find_approval_request("new") is not None
