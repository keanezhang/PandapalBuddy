"""AgentScheduler 测试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json

import pytest
import pytest_asyncio

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.config.system.manager import ConfigManager
from pandapal.router.models import InboundMessage
from pandapal.router.router import MessageRouter
from pandapal.scheduler.scheduler import AgentScheduler
from pandapal.session.manager import SessionManager
from pandapal.storage.manager import StorageManager
from pandapal.storage.models import Session


# ──────────────────────────────────────────────
# Mock Agent
# ──────────────────────────────────────────────


@dataclass
class MockAgentResult:
    success: bool = True
    output: str = "Hello from Agent!"
    error: str = ""
    terminal_reason: str | None = None
    paused: bool = False
    run_state: object | None = None


class MockAgent:
    """模拟 pandaren Agent。"""

    def __init__(self, result: MockAgentResult | None = None):
        self.result = result or MockAgentResult()
        self.run_calls: list[dict] = []

    async def run(self, task, session_id=None, **kwargs):
        self.run_calls.append({
            "task": task,
            "session_id": session_id,
            **kwargs,
        })
        return self.result


class MockGateway:
    def __init__(self):
        self.sent_frames = []

    async def send_message_frame(self, frame):
        self.sent_frames.append(frame)


@pytest_asyncio.fixture
async def scheduler_env(tmp_path):
    """提供完整的 AgentScheduler 测试环境。"""
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

    # Session Manager
    session_mgr = SessionManager(storage.get_session_repo(), config_mgr)

    # 预创建 session
    await session_mgr.ensure_session("s1", "u1", "d1")

    # Router
    router = MessageRouter()

    # Broadcast
    broadcast = MessageBroadcast()
    gateway = MockGateway()
    broadcast.attach_to_gateway(gateway)

    # Mock Agent
    agent = MockAgent()

    # AgentScheduler
    scheduler = AgentScheduler(
        agent=agent,
        session_manager=session_mgr,
        broadcast=broadcast,
        router=router,
        run_state_repo=storage.get_run_state_repo(),
    )
    scheduler.register_route_handlers()

    yield {
        "scheduler": scheduler,
        "agent": agent,
        "storage": storage,
        "router": router,
        "gateway": gateway,
        "session_mgr": session_mgr,
    }

    await storage.shutdown_storage()


# ──────────────────────────────────────────────
# Construction Tests
# ──────────────────────────────────────────────


def test_construct_missing_agent_raises():
    """agent 为 None 时抛出 ValueError。"""
    with pytest.raises(ValueError, match="agent"):
        AgentScheduler(
            agent=None,
            session_manager=object(),
            broadcast=object(),
            router=object(),
            run_state_repo=object(),
        )


# ──────────────────────────────────────────────
# handle_user_instruction Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_user_instruction_success(scheduler_env):
    """正常用户指令执行成功。"""
    env = scheduler_env
    scheduler = env["scheduler"]
    agent = env["agent"]

    msg = InboundMessage(
        msg_id="m1",
        message_type="user_instruction",
        source_channel_id="ch1",
        user_id="u1",
        session_id="s1",
        content="Hello Agent!",
    )
    await scheduler.handle_user_instruction(msg)

    # Agent 应被调用
    assert len(agent.run_calls) == 1
    assert agent.run_calls[0]["task"] == "Hello Agent!"
    assert agent.run_calls[0]["session_id"] == "s1"

    # 应有回复消息发出
    assert len(env["gateway"].sent_frames) >= 1


@pytest.mark.asyncio
async def test_handle_user_instruction_skill_parse(scheduler_env):
    """Skill 指令解析（/arch-design 帮我设计）。"""
    env = scheduler_env
    agent = env["agent"]

    msg = InboundMessage(
        msg_id="m2",
        message_type="user_instruction",
        source_channel_id="ch1",
        user_id="u1",
        session_id="s1",
        content="/arch-design 帮我设计一个模块",
    )
    await env["scheduler"].handle_user_instruction(msg)

    # Agent.run 应收到解析后的参数
    assert agent.run_calls[0]["task"] == "帮我设计一个模块"
    assert agent.run_calls[0]["skill_name"] == "arch-design"


@pytest.mark.asyncio
async def test_handle_user_instruction_no_session_auto_creates(scheduler_env):
    """session_id 为 None 时自动生成 session 并正常执行 Agent（auto-create 语义）。"""
    env = scheduler_env

    msg = InboundMessage(
        msg_id="m3",
        message_type="user_instruction",
        source_channel_id="ch1",
        user_id="u1",
        session_id=None,  # 无 session — 应自动生成 "ch1:u1"
        content="hello",
    )
    # 不应抛异常（O3）
    await env["scheduler"].handle_user_instruction(msg)

    # auto-create：Agent 应被调用（session 自动创建）
    assert len(env["agent"].run_calls) >= 1


@pytest.mark.asyncio
async def test_handle_user_instruction_unknown_session_auto_creates(scheduler_env):
    """未知 session_id 时自动创建 session 并正常执行 Agent（auto-create 语义）。"""
    env = scheduler_env

    msg = InboundMessage(
        msg_id="m4",
        message_type="user_instruction",
        source_channel_id="ch1",
        user_id="u1",
        session_id="nonexistent_session",  # 不存在 — 应自动创建
        content="hello",
    )
    await env["scheduler"].handle_user_instruction(msg)

    # auto-create：Agent 应被调用（session 自动创建）
    assert len(env["agent"].run_calls) >= 1


@pytest.mark.asyncio
async def test_handle_user_instruction_agent_failure(scheduler_env):
    """Agent 执行失败时发布 error_reply。"""
    env = scheduler_env
    env["agent"].result = MockAgentResult(
        success=False, error="Tool execution failed", terminal_reason="TOOL_ERROR"
    )

    msg = InboundMessage(
        msg_id="m5",
        message_type="user_instruction",
        source_channel_id="ch1",
        user_id="u1",
        session_id="s1",
        content="do something",
    )
    await env["scheduler"].handle_user_instruction(msg)

    # 应有错误消息发出
    assert len(env["gateway"].sent_frames) >= 1


# ──────────────────────────────────────────────
# handle_hitl_decision Tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_hitl_decision_approve(scheduler_env):
    """HITL 批准后恢复执行。"""
    env = scheduler_env
    storage = env["storage"]

    # 预存 RunState（JSON 序列化，含 session_id 供 S3 校验）
    fake_run_state = {"run_id": "run1", "step": 3, "session_id": "s1"}
    serialized = json.dumps(fake_run_state, ensure_ascii=False).encode("utf-8")
    await storage.get_run_state_repo().save_run_state("s1", "run1", serialized)

    # 设置 Agent 恢复后的返回
    env["agent"].result = MockAgentResult(success=True, output="Resumed OK!")

    msg = InboundMessage(
        msg_id="m6",
        message_type="hitl_decision",
        source_channel_id="__hitl_bridge__",
        user_id="u1",
        session_id="s1",
        content={"run_id": "run1", "decision": "approve"},
    )
    await env["scheduler"].handle_hitl_decision(msg)

    # Agent 应被调用（resume 路径）
    assert len(env["agent"].run_calls) == 1
    assert env["agent"].run_calls[0]["hitl_decision"] == "approve"

    # RunState 应被删除（BL7）
    assert await storage.get_run_state_repo().get_run_state("s1", "run1") is None


@pytest.mark.asyncio
async def test_handle_hitl_decision_run_not_found(scheduler_env):
    """RunState 不存在时返回错误。"""
    env = scheduler_env

    msg = InboundMessage(
        msg_id="m7",
        message_type="hitl_decision",
        source_channel_id="__hitl_bridge__",
        user_id="u1",
        session_id="s1",
        content={"run_id": "nonexistent", "decision": "approve"},
    )
    # 不应抛异常
    await env["scheduler"].handle_hitl_decision(msg)

    # Agent 不应被调用
    assert len(env["agent"].run_calls) == 0


@pytest.mark.asyncio
async def test_handle_hitl_decision_missing_run_id(scheduler_env):
    """缺少 run_id 时静默返回。"""
    env = scheduler_env

    msg = InboundMessage(
        msg_id="m8",
        message_type="hitl_decision",
        source_channel_id="__hitl_bridge__",
        user_id="u1",
        session_id="s1",
        content={"decision": "approve"},  # 缺 run_id
    )
    await env["scheduler"].handle_hitl_decision(msg)
    assert len(env["agent"].run_calls) == 0
