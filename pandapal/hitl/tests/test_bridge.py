"""HITLBridge 测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.config.system.manager import ConfigManager
from pandapal.hitl.bridge import HITLBridge
from pandapal.router.models import InboundMessage
from pandapal.router.router import MessageRouter
from pandapal.storage.manager import StorageManager
from pandapal.storage.models import ApprovalRequest, ApprovalStatus


class MockGateway:
    """Mock Gateway for Broadcast attachment."""

    def __init__(self):
        self.sent_frames = []

    async def send_message_frame(self, frame):
        self.sent_frames.append(frame)


@pytest_asyncio.fixture
async def hitl_env(tmp_path):
    """提供完整的 HITL Bridge 测试环境。"""
    # Storage
    db_path = str(tmp_path / "test.db")
    storage = StorageManager(storage_path=db_path)
    await storage.initialize_storage()

    # Config
    env_path = tmp_path / ".env.development"
    env_path.write_text(
        """\
PANDAPAL_RELAY_URL=wss://relay.example.com/ws
PANDAPAL_RELAY_AUTH_TOKEN=token
PANDAPAL_DATA_DIR=~/.pandapal
""",
        encoding="utf-8",
    )
    config_mgr = ConfigManager(str(tmp_path))
    await config_mgr.load_config()

    # Router (需要注册 hitl_decision handler)
    router = MessageRouter()
    hitl_decisions = []

    async def handle_hitl_decision(msg: InboundMessage):
        hitl_decisions.append(msg.content)

    router.register_route_handler("hitl_decision", handle_hitl_decision)

    # Broadcast
    broadcast = MessageBroadcast()
    gateway = MockGateway()
    broadcast.attach_to_gateway(gateway)

    # HITL Bridge
    hitl = HITLBridge(
        approval_repo=storage.get_approval_repo(),
        broadcast=broadcast,
        router=router,
        config_manager=config_mgr,
    )
    hitl.register_route_handlers()

    yield {
        "hitl": hitl,
        "storage": storage,
        "router": router,
        "gateway": gateway,
        "hitl_decisions": hitl_decisions,
    }

    await hitl.shutdown()
    await storage.shutdown_storage()


# ──────────────────────────────────────────────
# handle_hitl_request Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_hitl_request_creates_approval(hitl_env):
    """接收 hitl_request 后创建 ApprovalRequest 并广播。"""
    env = hitl_env
    hitl = env["hitl"]

    msg = InboundMessage(
        msg_id="m1",
        message_type="hitl_request",
        source_channel_id="agent",
        user_id="u1",
        session_id="s1",
        content={
            "run_id": "run1",
            "session_id": "s1",
            "tool_name": "file_write",
            "tool_args_summary": "Write /etc/passwd",
        },
    )
    await hitl.handle_hitl_request(msg)

    # 应有广播消息发出
    assert len(env["gateway"].sent_frames) >= 1

    # 应有超时 Task 注册
    assert len(hitl._pending_timers) == 1


@pytest.mark.asyncio
async def test_handle_hitl_request_persist_failure_rejects(hitl_env):
    """持久化失败时直接拒绝（Fail-Safe）。"""
    env = hitl_env
    hitl = env["hitl"]

    # 模拟持久化失败
    original_save = hitl._approval_repo.save_approval_request

    async def failing_save(request):
        raise RuntimeError("DB error")

    hitl._approval_repo.save_approval_request = failing_save

    msg = InboundMessage(
        msg_id="m2",
        message_type="hitl_request",
        source_channel_id="agent",
        user_id="u1",
        content={"run_id": "run2", "tool_name": "bash"},
    )
    await hitl.handle_hitl_request(msg)

    # 应发出 hitl_decision=reject
    assert len(env["hitl_decisions"]) == 1
    assert env["hitl_decisions"][0]["decision"] == "reject"

    # 恢复
    hitl._approval_repo.save_approval_request = original_save


# ──────────────────────────────────────────────
# handle_approval_response Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_approval_response_approve(hitl_env):
    """用户批准审批请求。"""
    env = hitl_env
    hitl = env["hitl"]

    # 先创建一个审批请求
    msg = InboundMessage(
        msg_id="m1",
        message_type="hitl_request",
        source_channel_id="agent",
        user_id="u1",
        content={"run_id": "run1", "tool_name": "bash", "session_id": "s1"},
    )
    await hitl.handle_hitl_request(msg)

    # 获取 approval_id
    approval_id = list(hitl._pending_timers.keys())[0]

    # 模拟用户批准
    response = InboundMessage(
        msg_id="m2",
        message_type="approval_response",
        source_channel_id="ch1",
        user_id="u1",
        session_id="s1",
        content={"approval_id": approval_id, "decision": "approve"},
    )
    await hitl.handle_approval_response(response)

    # 应发出 hitl_decision=approve
    assert any(d["decision"] == "approve" for d in env["hitl_decisions"])

    # 超时 Task 应被取消
    assert approval_id not in hitl._pending_timers


@pytest.mark.asyncio
async def test_handle_approval_response_reject(hitl_env):
    """用户拒绝审批请求。"""
    env = hitl_env
    hitl = env["hitl"]

    msg = InboundMessage(
        msg_id="m1",
        message_type="hitl_request",
        source_channel_id="agent",
        user_id="u1",
        content={"run_id": "run1", "tool_name": "bash"},
    )
    await hitl.handle_hitl_request(msg)

    approval_id = list(hitl._pending_timers.keys())[0]

    response = InboundMessage(
        msg_id="m2",
        message_type="approval_response",
        source_channel_id="ch1",
        user_id="u1",
        content={"approval_id": approval_id, "decision": "reject"},
    )
    await hitl.handle_approval_response(response)

    assert any(d["decision"] == "reject" for d in env["hitl_decisions"])


@pytest.mark.asyncio
async def test_handle_approval_response_unknown_id_idempotent(hitl_env):
    """未知 approval_id 幂等静默返回（BL3）。"""
    env = hitl_env
    hitl = env["hitl"]

    response = InboundMessage(
        msg_id="m1",
        message_type="approval_response",
        source_channel_id="ch1",
        user_id="u1",
        content={"approval_id": "nonexistent-id", "decision": "approve"},
    )
    # 不应抛异常
    await hitl.handle_approval_response(response)
    assert len(env["hitl_decisions"]) == 0


@pytest.mark.asyncio
async def test_handle_approval_response_already_decided_idempotent(hitl_env):
    """已决策的审批重复响应幂等返回（BL3）。"""
    env = hitl_env
    hitl = env["hitl"]

    msg = InboundMessage(
        msg_id="m1",
        message_type="hitl_request",
        source_channel_id="agent",
        user_id="u1",
        content={"run_id": "run1", "tool_name": "bash"},
    )
    await hitl.handle_hitl_request(msg)
    approval_id = list(hitl._pending_timers.keys())[0]

    # 第一次决策
    resp1 = InboundMessage(
        msg_id="m2",
        message_type="approval_response",
        source_channel_id="ch1",
        user_id="u1",
        content={"approval_id": approval_id, "decision": "approve"},
    )
    await hitl.handle_approval_response(resp1)

    decision_count = len(env["hitl_decisions"])

    # 第二次决策（应幂等）
    resp2 = InboundMessage(
        msg_id="m3",
        message_type="approval_response",
        source_channel_id="ch2",
        user_id="u1",
        content={"approval_id": approval_id, "decision": "reject"},
    )
    await hitl.handle_approval_response(resp2)

    # 不应产生新的 hitl_decision
    assert len(env["hitl_decisions"]) == decision_count


# ──────────────────────────────────────────────
# Timeout Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_auto_reject(hitl_env):
    """超时自动拒绝。"""
    env = hitl_env
    hitl = env["hitl"]

    # 直接调用 _trigger_timeout_reject
    # 先创建请求
    now = datetime.now(timezone.utc)
    request = ApprovalRequest(
        approval_id="timeout-test",
        user_id="u1",
        run_id="run1",
        tool_name="dangerous_tool",
        timeout_seconds=1,
        created_at=now - timedelta(seconds=10),  # 已过期
    )
    await env["storage"].get_approval_repo().save_approval_request(request)

    # 触发超时拒绝
    await hitl._trigger_timeout_reject("timeout-test")

    # 应发出 hitl_decision=reject
    assert any(d["decision"] == "reject" for d in env["hitl_decisions"])

    # 状态应为 resolved
    updated = await env["storage"].get_approval_repo().find_approval_request("timeout-test")
    assert updated is not None
    assert updated.status == "resolved"


@pytest.mark.asyncio
async def test_timeout_after_user_decision_idempotent(hitl_env):
    """用户已决策后超时触发无效（BL7）。"""
    env = hitl_env
    hitl = env["hitl"]

    msg = InboundMessage(
        msg_id="m1",
        message_type="hitl_request",
        source_channel_id="agent",
        user_id="u1",
        content={"run_id": "run1", "tool_name": "bash"},
    )
    await hitl.handle_hitl_request(msg)
    approval_id = list(hitl._pending_timers.keys())[0]

    # 用户先批准
    resp = InboundMessage(
        msg_id="m2",
        message_type="approval_response",
        source_channel_id="ch1",
        user_id="u1",
        content={"approval_id": approval_id, "decision": "approve"},
    )
    await hitl.handle_approval_response(resp)

    decision_count = len(env["hitl_decisions"])

    # 超时触发（应幂等退出）
    await hitl._trigger_timeout_reject(approval_id)

    # 不应产生新的 hitl_decision
    assert len(env["hitl_decisions"]) == decision_count


# ──────────────────────────────────────────────
# Shutdown Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_cancels_timers(hitl_env):
    """shutdown 取消所有超时任务。"""
    env = hitl_env
    hitl = env["hitl"]

    msg = InboundMessage(
        msg_id="m1",
        message_type="hitl_request",
        source_channel_id="agent",
        user_id="u1",
        content={"run_id": "run1", "tool_name": "bash"},
    )
    await hitl.handle_hitl_request(msg)
    assert len(hitl._pending_timers) == 1

    await hitl.shutdown()
    # 所有 timer 应被清理
    # (shutdown 中 clear 后 _pending_timers 为空)


# ──────────────────────────────────────────────
# S3 Session Isolation Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_approval_response_session_mismatch_rejected(hitl_env):
    """S3: 不同 session_id 的审批响应被拒绝（跨会话 HITL 伪造防护）。"""
    env = hitl_env
    hitl = env["hitl"]

    # 创建审批请求，session_id = "s1"
    msg = InboundMessage(
        msg_id="m1",
        message_type="hitl_request",
        source_channel_id="agent",
        user_id="u1",
        session_id="s1",
        content={"run_id": "run1", "tool_name": "bash", "session_id": "s1"},
    )
    await hitl.handle_hitl_request(msg)
    approval_id = list(hitl._pending_timers.keys())[0]

    # 用 session_id="s2" 发出审批响应（跨会话伪造攻击）
    response = InboundMessage(
        msg_id="m2",
        message_type="approval_response",
        source_channel_id="ch1",
        user_id="attacker",
        session_id="s2",  # ← 与原始 session_id 不符
        content={"approval_id": approval_id, "decision": "approve"},
    )
    await hitl.handle_approval_response(response)

    # S3 校验阻断：Agent 不应收到任何决策
    assert len(env["hitl_decisions"]) == 0

    # 审批请求仍为 pending（未被伪造决策修改）
    stored = await env["storage"].get_approval_repo().find_approval_request(approval_id)
    assert stored is not None
    assert stored.status == "pending"


# ──────────────────────────────────────────────
# restore_pending_approvals Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_pending_approvals_empty(hitl_env):
    """无 pending 记录时 restore_pending_approvals 正常返回，不注册任何 Timer。"""
    env = hitl_env
    hitl = env["hitl"]

    await hitl.restore_pending_approvals()

    assert len(hitl._pending_timers) == 0
    assert len(env["hitl_decisions"]) == 0


@pytest.mark.asyncio
async def test_restore_pending_approvals_not_expired(hitl_env):
    """未超时的 pending 审批恢复后重新注册超时 Task 并广播。"""
    env = hitl_env
    hitl = env["hitl"]

    now = datetime.now(timezone.utc)
    request = ApprovalRequest(
        approval_id="restore-not-expired",
        user_id="u1",
        run_id="run1",
        tool_name="bash",
        timeout_seconds=300,
        status=ApprovalStatus.PENDING,
        created_at=now,  # 刚创建，距离超时还有 300 秒
        session_id="s1",
    )
    await env["storage"].get_approval_repo().save_approval_request(request)

    frames_before = len(env["gateway"].sent_frames)
    await hitl.restore_pending_approvals()

    # 应重新注册超时 Task
    assert "restore-not-expired" in hitl._pending_timers

    # 应广播一次 approval_request
    assert len(env["gateway"].sent_frames) > frames_before


@pytest.mark.asyncio
async def test_restore_pending_approvals_already_expired(hitl_env):
    """已超时的 pending 审批恢复后立即触发拒绝，不注册 Timer。"""
    env = hitl_env
    hitl = env["hitl"]

    now = datetime.now(timezone.utc)
    request = ApprovalRequest(
        approval_id="restore-expired",
        user_id="u1",
        run_id="run2",
        tool_name="dangerous_tool",
        timeout_seconds=300,
        status=ApprovalStatus.PENDING,
        created_at=now - timedelta(seconds=600),  # 已超时 600 秒
        session_id="s1",
    )
    await env["storage"].get_approval_repo().save_approval_request(request)

    await hitl.restore_pending_approvals()

    # 应发出 hitl_decision=reject
    assert any(d["decision"] == "reject" for d in env["hitl_decisions"])

    # 不应注册超时 Task（已立即处理）
    assert "restore-expired" not in hitl._pending_timers

    # 状态应为 resolved
    stored = await env["storage"].get_approval_repo().find_approval_request("restore-expired")
    assert stored is not None
    assert stored.status == "resolved"
