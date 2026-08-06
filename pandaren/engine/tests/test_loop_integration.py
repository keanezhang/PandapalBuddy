"""
Pandaren Agent SDK · Loop 层集成测试（需求驱动黑盒）

覆盖约束
--------
  基于 docs/工程化设计文档/框架设计/04_loop_框架设计.md 验证外部可观察行为：

  - O3 原则     ：run() 永远返回 AgentResult，不向外抛任何异常
  - HC5 原则    ：ExecutionLimits 不可变 + max_steps 有界循环
  - HC3 原则    ：permission_guard 硬编码、不可绕过
  - HC3+HC6     ：HITL 硬编码强制审批，CRITICAL 级别无条件拦截
  - LLM 失败策略：不可重试错误立即终止，可重试错误指数退避
  - cancel()    ：外部取消信号正确终止 run
  - 工具执行    ：正常工具调用、tool_result.halt=True 触发 TOOL_HALT
  - 双入口架构  ：run() 与 run_stream() 共享同一执行体
  - AgentResult ：结构完整性（字段、paused 属性、步骤统计）

运行方式
--------
  cd <仓库根目录>
  python pandaren/engine/tests/test_loop_integration.py
  python pandaren/engine/tests/test_loop_integration.py --section o3
  python pandaren/engine/tests/test_loop_integration.py --section hc5
  python pandaren/engine/tests/test_loop_integration.py --section hc3_guard
  python pandaren/engine/tests/test_loop_integration.py --section hc3_hitl
  python pandaren/engine/tests/test_loop_integration.py --section llm_errors
  python pandaren/engine/tests/test_loop_integration.py --section cancel
  python pandaren/engine/tests/test_loop_integration.py --section tool_exec
  python pandaren/engine/tests/test_loop_integration.py --section dual_entry
  python pandaren/engine/tests/test_loop_integration.py --section agent_result
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Windows 控制台 UTF-8
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ══════════════════════════════════════════════════════
#  SDK 导入
# ══════════════════════════════════════════════════════
from pandaren.engine.models import AgentResult, RunState
from pandaren.engine.stream import StreamEvent, StreamEventType
from pandaren.engine.types import TerminalReason
from pandaren.behavior.execution_limits import ExecutionLimits
from pandaren.behavior.exceptions import BehaviorConfigError
from pandaren.tool.types import SensitivityLevel
from pandaren.tool import ToolResult
from pandaren.llm.exceptions import (
    LLMAuthError,
    LLMRequestError,
    LLMRateLimitError,
    LLMServerError,
    LLMNetworkError,
)
from pandaren.memory.models import MemorySnapshot


# ══════════════════════════════════════════════════════
#  异步辅助
# ══════════════════════════════════════════════════════

def async_run(coro):
    """同步运行协程（兼容 Python 3.12+）。"""
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════
#  测试框架
# ══════════════════════════════════════════════════════

class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def ok(self, name: str):
        self.passed += 1
        print(f"   ✅ {name}")

    def fail(self, name: str, detail: str = ""):
        self.failed += 1
        msg = f"   ❌ {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        self.errors.append(msg)

    def summary(self, section: str = ""):
        total = self.passed + self.failed
        label = f" [{section}]" if section else ""
        print(f"\n📊{label} 通过={self.passed} / 失败={self.failed} / 总计={total}")
        if self.errors:
            print("   失败列表:")
            for e in self.errors:
                print(f"     {e}")
        return self.failed == 0


result = TestResult()


def assert_true(condition: bool, name: str, detail: str = ""):
    if condition:
        result.ok(name)
    else:
        result.fail(name, detail or "条件为 False")


def assert_false(condition: bool, name: str, detail: str = ""):
    assert_true(not condition, name, detail or "条件应为 False")


# ══════════════════════════════════════════════════════
#  工厂辅助
# ══════════════════════════════════════════════════════

def _make_tool_def(
    name: str = "test_tool",
    sensitivity: SensitivityLevel = SensitivityLevel.LOW,
):
    """创建轻量 Tool mock（模拟 ToolRegistry.get_tool() 返回值）。"""
    td = MagicMock()
    td.name = name
    td.sensitivity = sensitivity
    # 非交互型工具：不显式设置时 MagicMock 的 requires_user_interaction 为
    # truthy，会误触发 INTERACTION_PAUSED 路径（见 run_core.py Phase 5）
    td.requires_user_interaction = False
    return td


def _make_loop(
    *,
    llm_response: dict | None = None,
    llm_side_effect=None,
    max_retries: int = 0,
    tool_def=None,
    tool_result: ToolResult | None = None,
    guard_result: str = "allow",
    hitl_result: str = "pass",
    max_steps: int = 5,
    step_timeout: float = 30.0,
    total_timeout: float = 300.0,
    stream: bool = False,
    auto_confirm_high: bool = False,
    system_prompt: str = "你是测试助手",
):
    """
    构建一个完整配置的 AgentLoop，所有外部依赖均为 Mock。

    llm_response：LLM 返回值（None 时使用默认完成响应）。
    llm_side_effect：直接覆盖 call 的 side_effect（用于模拟异常）。
    tool_def：ToolRegistry.get_tool() 的返回值（None 表示未注册）。
    tool_result：execute_tools_concurrent 返回的 ToolResult。
    guard_result：permission_guard.check_permission() 的返回值。
    hitl_result：hitl_controller.check_approval() 的返回值。
    """
    from pandaren.identity.models import Identity, TrustLevel, PERMISSION_ALL
    from pandaren.behavior.execution_limits import ExecutionLimits
    from pandaren.behavior.error_policy import ErrorPolicy
    from pandaren.engine.loop import AgentLoop

    identity = Identity(
        agent_id="test.integration",
        agent_name="集成测试Agent",
        when_to_use="集成测试",
        sensitive_permissions=PERMISSION_ALL,
        trust_level=TrustLevel.SUB_AGENT,
    )

    # ── LLM Mock ──────────────────────────────────────────────────────
    mock_llm = MagicMock()
    mock_llm.model_name = "mock-model"
    mock_llm._on_before_request = None  # 允许被 run_core.py 设置

    default_resp = llm_response or {
        "content": "任务完成",
        "tool_calls": None,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "finish_reason": "stop",
    }
    if llm_side_effect is not None:
        mock_llm.call = AsyncMock(side_effect=llm_side_effect)
    else:
        mock_llm.call = AsyncMock(return_value=default_resp)

    # 没有 stream_response 属性 → 走非流式路径
    # （不要 del，直接用 spec 排除或确保不存在该属性）
    if stream:
        # 流式路径：让 hasattr(llm_client, 'stream_response') 为 True
        mock_llm.stream_response = MagicMock()
        # 流式响应生成器 mock
        async def _stream_gen(*args, **kwargs):
            from pandaren.llm.types import StreamChunk
            yield StreamChunk(
                delta_content="任务完成",
                delta_reasoning_content=None,
                tool_call_delta=None,
                finish_reason=None,
                usage=None,
                refusal_delta=None,
            )
            yield StreamChunk(
                delta_content=None,
                delta_reasoning_content=None,
                tool_call_delta=None,
                finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                refusal_delta=None,
            )
        mock_llm.stream_response.return_value = _stream_gen()
        mock_llm.stream_response.side_effect = lambda *a, **kw: _stream_gen()
    else:
        # 确保没有 stream_response 属性
        if hasattr(mock_llm, "stream_response"):
            del mock_llm.stream_response

    # ── ToolRegistry Mock ──────────────────────────────────────────────
    mock_registry = MagicMock()
    mock_registry.get_deferred_tool_catalog.return_value = []
    mock_registry.always_tools_count = 0
    mock_registry.discovery = MagicMock()
    mock_registry.discovery.snapshot.return_value = {}
    mock_registry.get_tool.return_value = tool_def
    mock_registry.update_enabled_tools = AsyncMock()
    mock_registry.build_tool_schemas.return_value = []
    mock_registry.promote_to_discovered = MagicMock()

    if tool_result is not None:
        mock_registry.execute_tools_concurrent = AsyncMock(return_value=[tool_result])
    else:
        mock_registry.execute_tools_concurrent = AsyncMock(return_value=[])

    # ── HarnessExecutor Mock ──────────────────────────────────────────
    mock_harness_executor = MagicMock()
    mock_harness_executor.reset_turn = MagicMock()
    mock_harness_executor.is_circuit_tripped = MagicMock(return_value=False)
    if tool_result is not None:
        mock_harness_executor.execute_tools_concurrent = AsyncMock(return_value=[tool_result])
    else:
        mock_harness_executor.execute_tools_concurrent = AsyncMock(return_value=[])

    # ── PermissionGuard Mock ───────────────────────────────────────────
    mock_guard = MagicMock()
    mock_guard.check_permission.return_value = guard_result

    # ── HITLController Mock ────────────────────────────────────────────
    from pandaren.behavior.hitl_controller import HITLController, ResumeDecision
    mock_hitl = MagicMock()
    mock_hitl.check_approval.return_value = hitl_result
    # resolve_resume 使用真实逻辑：approved → execute_pending, rejected → reject_and_halt
    def _resolve_resume(hitl_decision, pending):
        if hitl_decision == "approved":
            return ResumeDecision(action="execute_pending", pending=pending)
        return ResumeDecision(action="reject_and_halt", pending=pending)
    mock_hitl.resolve_resume.side_effect = _resolve_resume

    # ── AuditLog Mock ──────────────────────────────────────────────────
    mock_audit = MagicMock()
    mock_audit.write_sync = MagicMock()  # 同步

    # ── Memory Mock ────────────────────────────────────────────────────
    mock_memory = MagicMock()
    mock_memory.system_prompt = system_prompt
    mock_memory.flush_raw_messages = AsyncMock()
    mock_memory.trigger_extraction = AsyncMock()
    mock_memory.end_session = AsyncMock()
    mock_memory.recall_and_inject = MagicMock(return_value=False)
    mock_memory.init_from_restore = MagicMock()
    mock_memory.get_messages = MagicMock(return_value=[])
    mock_memory.resume_context = MagicMock()
    mock_memory.compact_if_needed = AsyncMock(return_value=None)
    mock_memory.add_assistant_message = AsyncMock()
    mock_memory.add_tool_result = AsyncMock()
    mock_memory.recall_text = None
    mock_memory.working_memory_accessor = MagicMock()
    mock_memory.snapshot_for_pause = MagicMock(
        return_value=MemorySnapshot(
            messages=(),
        )
    )

    # ── ExecutionLimits & ErrorPolicy ─────────────────────────────────
    limits = ExecutionLimits(
        max_steps=max_steps,
        step_timeout=step_timeout,
        total_timeout=total_timeout,
    )
    error_policy = ErrorPolicy(max_retries=max_retries, base_delay_s=0.001)

    # ── 构造 AgentLoop ─────────────────────────────────────────────────
    with patch(
        "pandaren.engine.loop.MessageBuilder.build_static_context_str",
        return_value="PLACEHOLDER",
    ):
        loop = AgentLoop(
            identity=identity,
            llm_client=mock_llm,
            tool_registry=mock_registry,
            harness_executor=mock_harness_executor,
            permission_guard=mock_guard,
            hitl_controller=mock_hitl,
            execution_limits=limits,
            error_policy=error_policy,
            audit_log=mock_audit,
            memory=mock_memory,
        )

    return loop


def _make_run_state(
    session_id: str = "sess-001",
    agent_id: str = "test.integration",
    tool_name: str = "test_tool",
    sensitivity: int = SensitivityLevel.CRITICAL,
) -> RunState:
    """构建一个最小化的 RunState（HITL 暂停后恢复所用）。"""
    return RunState(
        run_id="run-paused-001",
        agent_id=agent_id,
        step_n=0,
        session_id=session_id,
        messages=[],
        pending_tool_call=None,
        working={},
        metadata={
            "recall_injected": False,
            "recall_text": None,
            "discovered_set": {},
            "pending_approval": {
                "tool_call": {"id": "call-001", "function": {"name": tool_name, "arguments": "{}"}},
                "tool_name": tool_name,
                "tool_args": {},
                "sensitivity": sensitivity,
                "step_n": 0,
                "approved_calls_before": [],
                "unchecked_calls_after": [],
            },
        },
    )


# ══════════════════════════════════════════════════════
#  Section 1：O3 原则 — run() 永不向外抛异常
# ══════════════════════════════════════════════════════

def test_o3_run_never_raises():
    print("\n── O3 原则：run() 永不向外抛异常 ─────────────────────────────")

    # 1. session_id 为空 → 应返回 AgentResult(success=False)，不抛出
    loop = _make_loop()
    r = async_run(loop.run("测试任务", session_id=""))
    assert_true(isinstance(r, AgentResult), "session_id 空串：返回 AgentResult 实例")
    assert_true(not r.success, "session_id 空串：success=False")
    assert_true(r.error is not None, "session_id 空串：error 非空")

    # 2. session_id 纯空白 → 同样返回 AgentResult(success=False)
    loop2 = _make_loop()
    r2 = async_run(loop2.run("测试任务", session_id="   "))
    assert_true(isinstance(r2, AgentResult), "session_id 纯空白：返回 AgentResult 实例")
    assert_true(not r2.success, "session_id 纯空白：success=False")

    # 3. LLM 抛出未预期异常（RuntimeError）→ run() 不传播，返回 AgentResult
    loop4 = _make_loop(llm_side_effect=RuntimeError("意外崩溃"))
    r4 = async_run(loop4.run("测试任务", session_id="s1"))
    assert_true(isinstance(r4, AgentResult), "LLM RuntimeError：返回 AgentResult 实例")
    assert_true(not r4.success, "LLM RuntimeError：success=False")

    # 4. HITL resume session_id 不匹配 → 不抛出，返回 AgentResult(success=False)
    loop5 = _make_loop()
    run_state = _make_run_state(session_id="sess-A")
    r5 = async_run(loop5.run(
        "恢复任务",
        session_id="sess-B",  # 与 run_state.session_id 不同
        resume_state=run_state,
        hitl_decision="approved",
    ))
    assert_true(isinstance(r5, AgentResult), "HITL session_id 不匹配：返回 AgentResult")
    assert_true(not r5.success, "HITL session_id 不匹配：success=False")


# ══════════════════════════════════════════════════════
#  Section 2：HC5 — ExecutionLimits 不可变 + 有界循环
# ══════════════════════════════════════════════════════

def test_hc5_execution_limits():
    print("\n── HC5：ExecutionLimits 不可变 + 有界循环 ─────────────────────")

    # 1. max_steps <= 0 → BehaviorConfigError
    try:
        ExecutionLimits(max_steps=0)
        result.fail("max_steps=0 应抛出 BehaviorConfigError")
    except BehaviorConfigError:
        result.ok("max_steps=0 抛出 BehaviorConfigError")
    except Exception as e:
        result.fail("max_steps=0 应抛出 BehaviorConfigError", f"实际: {type(e).__name__}: {e}")

    # 2. step_timeout <= 0 → BehaviorConfigError
    try:
        ExecutionLimits(step_timeout=0.0)
        result.fail("step_timeout=0 应抛出 BehaviorConfigError")
    except BehaviorConfigError:
        result.ok("step_timeout=0 抛出 BehaviorConfigError")
    except Exception as e:
        result.fail("step_timeout=0 应抛出 BehaviorConfigError", f"实际: {type(e).__name__}: {e}")

    # 3. total_timeout <= 0 → BehaviorConfigError
    try:
        ExecutionLimits(total_timeout=0.0)
        result.fail("total_timeout=0 应抛出 BehaviorConfigError")
    except BehaviorConfigError:
        result.ok("total_timeout=0 抛出 BehaviorConfigError")
    except Exception as e:
        result.fail("total_timeout=0 应抛出 BehaviorConfigError", f"实际: {type(e).__name__}: {e}")

    # 4. step_timeout > total_timeout → BehaviorConfigError
    try:
        ExecutionLimits(step_timeout=100.0, total_timeout=50.0)
        result.fail("step_timeout > total_timeout 应抛出 BehaviorConfigError")
    except BehaviorConfigError:
        result.ok("step_timeout > total_timeout 抛出 BehaviorConfigError")
    except Exception as e:
        result.fail("step_timeout > total_timeout 应抛出 BehaviorConfigError", f"实际: {type(e).__name__}: {e}")

    # 5. 停机守卫已上移应用层 StepGuard，ExecutionLimits 不再有 max_cost_usd（无需校验）

    # 6. 构造后冻结 → 直接赋值抛 PermissionError
    limits = ExecutionLimits()
    try:
        limits.max_steps = 99  # type: ignore
        result.fail("ExecutionLimits 构造后修改 max_steps 应抛出 PermissionError")
    except PermissionError:
        result.ok("ExecutionLimits 构造后不可修改（PermissionError）")
    except Exception as e:
        result.fail("ExecutionLimits 修改应抛 PermissionError", f"实际: {type(e).__name__}: {e}")

    # 7. max_steps 有界循环：设置 max_steps=1，LLM 始终返回 tool_call（无限循环诱因）
    #    期望：run 返回，terminal_reason = MAX_STEPS_EXCEEDED（不会无限运行）
    tool = _make_tool_def()
    # LLM 始终返回 tool_call，guard=allow，hitl=pass，tool 成功，模拟无终止场景
    def _tool_call_response():
        return {
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "test_tool", "arguments": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "finish_reason": "tool_calls",
        }

    tool_ok = ToolResult(success=True, data="ok", halt=False, duration_ms=1.0)
    loop_bounded = _make_loop(
        llm_response=_tool_call_response(),
        max_steps=1,
        tool_def=tool,
        tool_result=tool_ok,
        guard_result="allow",
        hitl_result="pass",
    )
    # LLM 始终返回 tool_calls（设为 side_effect 使每次都返回）
    loop_bounded._llm_client.call = AsyncMock(return_value=_tool_call_response())
    r_bounded = async_run(loop_bounded.run("无限循环任务", session_id="s-bounded"))
    assert_true(isinstance(r_bounded, AgentResult), "有界循环：返回 AgentResult")
    assert_true(not r_bounded.success, "有界循环：超过 max_steps 后 success=False")
    assert_true(
        r_bounded.terminal_reason == TerminalReason.MAX_STEPS_EXCEEDED,
        "有界循环：terminal_reason=MAX_STEPS_EXCEEDED",
        f"实际: {r_bounded.terminal_reason}",
    )


# ══════════════════════════════════════════════════════
#  Section 3：HC3 — permission_guard 硬编码不可绕过
# ══════════════════════════════════════════════════════

def test_hc3_permission_guard():
    print("\n── HC3：permission_guard 硬编码不可绕过 ─────────────────────────")

    # 1. guard 返回 "deny" → 流中应出现 PERMISSION_DENIED 事件
    tool = _make_tool_def()
    loop_deny = _make_loop(
        llm_response={
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "test_tool", "arguments": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "finish_reason": "tool_calls",
        },
        tool_def=tool,
        guard_result="deny",
        hitl_result="pass",
        max_steps=1,
    )

    events = async_run(_collect_stream_events(loop_deny, "s1"))
    event_types = [e.type for e in events]
    assert_true(
        StreamEventType.PERMISSION_DENIED in event_types,
        "guard=deny：流中出现 PERMISSION_DENIED 事件",
    )

    # 2. guard 返回 "deny" → run() 返回 AgentResult（不抛异常，O3）
    loop_deny2 = _make_loop(
        llm_response={
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "test_tool", "arguments": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "finish_reason": "tool_calls",
        },
        tool_def=tool,
        guard_result="deny",
        max_steps=1,
    )
    r_deny = async_run(loop_deny2.run("测试任务", session_id="s1"))
    assert_true(isinstance(r_deny, AgentResult), "guard=deny：run() 返回 AgentResult")

    # 3. guard 返回 "allow"，tool 成功 → run() 完成，RUN_END 事件中 success=True
    tool_ok = ToolResult(success=True, data="结果数据", halt=False, duration_ms=1.0)
    loop_allow = _make_loop(
        llm_response={
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "test_tool", "arguments": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "finish_reason": "tool_calls",
        },
        tool_def=tool,
        tool_result=tool_ok,
        guard_result="allow",
        hitl_result="pass",
        max_steps=2,
        # 第二步 LLM 返回文本完成
    )
    # 第二轮 LLM 返回 content（结束）
    call_count = [0]
    async def _llm_two_phase(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return {
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "test_tool", "arguments": "{}"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                "finish_reason": "tool_calls",
            }
        return {
            "content": "完成",
            "tool_calls": None,
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            "finish_reason": "stop",
        }
    loop_allow._llm_client.call = _llm_two_phase
    r_allow = async_run(loop_allow.run("测试任务", session_id="s1"))
    assert_true(r_allow.success, "guard=allow + tool 成功：run() 成功完成")
    assert_true(
        StreamEventType.PERMISSION_DENIED not in [e.type for e in async_run(_collect_stream_events(
            _make_loop(
                llm_response={"content": "完成", "tool_calls": None,
                               "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                               "finish_reason": "stop"},
            ), "s1"
        ))],
        "正常完成流：不含 PERMISSION_DENIED 事件",
    )

    # 4. 未注册工具（get_tool 返回 None）→ PERMISSION_DENIED（HC3 红线）
    loop_unreg = _make_loop(
        llm_response={
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "unregistered_tool", "arguments": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "finish_reason": "tool_calls",
        },
        tool_def=None,  # 未注册
        max_steps=1,
    )
    events_unreg = async_run(_collect_stream_events(loop_unreg, "s1"))
    assert_true(
        StreamEventType.PERMISSION_DENIED in [e.type for e in events_unreg],
        "未注册工具：出现 PERMISSION_DENIED 事件（HC3）",
    )


# ══════════════════════════════════════════════════════
#  Section 4：HC3+HC6 — HITL 硬编码强制审批
# ══════════════════════════════════════════════════════

def test_hc3_hc6_hitl():
    print("\n── HC3+HC6：HITL 硬编码强制审批 ─────────────────────────────────")

    # 1. sensitivity=CRITICAL → 强制 HITL_REQUESTED，无论任何配置
    tool_critical = _make_tool_def(sensitivity=SensitivityLevel.CRITICAL)
    loop_crit = _make_loop(
        llm_response={
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "test_tool", "arguments": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "finish_reason": "tool_calls",
        },
        tool_def=tool_critical,
        guard_result="allow",
        hitl_result="need_approval",
        max_steps=2,
    )
    events_crit = async_run(_collect_stream_events(loop_crit, "s1"))
    etypes_crit = [e.type for e in events_crit]
    assert_true(
        StreamEventType.HITL_REQUESTED in etypes_crit,
        "sensitivity=CRITICAL：流中出现 HITL_REQUESTED 事件",
    )

    # 2. CRITICAL → run() 返回 paused=True 的 AgentResult
    r_crit = async_run(loop_crit.run("CRITICAL 任务", session_id="s1"))
    # Note: loop is already consumed, make a fresh one
    loop_crit2 = _make_loop(
        llm_response={
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "test_tool", "arguments": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "finish_reason": "tool_calls",
        },
        tool_def=tool_critical,
        guard_result="allow",
        hitl_result="need_approval",
        max_steps=2,
    )
    r_crit2 = async_run(loop_crit2.run("CRITICAL 任务", session_id="s1"))
    assert_true(isinstance(r_crit2, AgentResult), "CRITICAL HITL：返回 AgentResult")
    assert_true(r_crit2.paused, "CRITICAL HITL：paused=True")
    assert_true(r_crit2.run_state is not None, "CRITICAL HITL：run_state 非 None")

    # 3. sensitivity=HIGH + auto_confirm_high=False → hitl_result="need_approval" → 暂停
    from pandaren.behavior.hitl_controller import HITLController
    real_hitl = HITLController(auto_confirm_high=False)
    assert_true(
        real_hitl.check_approval(SensitivityLevel.HIGH, "tool_high") == "need_approval",
        "HIGH + auto_confirm=False → check_approval 返回 need_approval",
    )

    # 4. sensitivity=HIGH + auto_confirm_high=True → hitl_result="pass" → 继续执行
    real_hitl_auto = HITLController(auto_confirm_high=True)
    assert_true(
        real_hitl_auto.check_approval(SensitivityLevel.HIGH, "tool_high") == "pass",
        "HIGH + auto_confirm=True → check_approval 返回 pass",
    )

    # 5. sensitivity=CRITICAL + auto_confirm_high=True → 仍然 need_approval（HC6 不可绕过）
    assert_true(
        real_hitl_auto.check_approval(SensitivityLevel.CRITICAL, "tool_crit") == "need_approval",
        "CRITICAL + auto_confirm=True → 仍然 need_approval（HC6 不可绕过）",
    )

    # 6. HITL 暂停后 approved 恢复 → 工具被执行
    run_state_resume = _make_run_state(
        session_id="sess-hitl",
        tool_name="test_tool",
        sensitivity=SensitivityLevel.CRITICAL,
    )
    tool_ok = ToolResult(success=True, data="执行成功", halt=False, duration_ms=1.0)
    loop_resume = _make_loop(
        llm_response={"content": "最终完成", "tool_calls": None,
                       "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                       "finish_reason": "stop"},
        tool_def=_make_tool_def(),
        tool_result=tool_ok,
        guard_result="allow",
        hitl_result="pass",
        max_steps=5,
    )
    r_resume = async_run(loop_resume.run(
        "恢复任务",
        session_id="sess-hitl",
        resume_state=run_state_resume,
        hitl_decision="approved",
    ))
    assert_true(isinstance(r_resume, AgentResult), "HITL approved 恢复：返回 AgentResult")
    assert_true(r_resume.success, "HITL approved 恢复：run 最终成功")

    # 7. HITL 暂停后 rejected 恢复 → terminal_reason=HITL_REJECTED
    loop_reject = _make_loop(
        tool_def=_make_tool_def(),
        guard_result="allow",
        hitl_result="pass",
        max_steps=5,
    )
    r_reject = async_run(loop_reject.run(
        "恢复任务",
        session_id="sess-hitl",
        resume_state=_make_run_state(session_id="sess-hitl"),
        hitl_decision="rejected",
    ))
    assert_true(isinstance(r_reject, AgentResult), "HITL rejected 恢复：返回 AgentResult")
    assert_true(not r_reject.success, "HITL rejected 恢复：success=False")
    assert_true(
        r_reject.terminal_reason == TerminalReason.HITL_REJECTED,
        "HITL rejected 恢复：terminal_reason=HITL_REJECTED",
        f"实际: {r_reject.terminal_reason}",
    )

    # 8. HITLController 构造后不可修改（冻结）
    hitl_ctrl = HITLController(auto_confirm_high=False)
    try:
        hitl_ctrl.auto_confirm_high = True  # type: ignore
        result.fail("HITLController 修改 auto_confirm_high 应抛 PermissionError")
    except PermissionError:
        result.ok("HITLController 构造后不可修改（PermissionError）")
    except Exception as e:
        result.fail("HITLController 冻结验证", f"实际: {type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════
#  Section 5：LLM 失败 + 熔断器
# ══════════════════════════════════════════════════════

def test_llm_errors():
    print("\n── LLM 失败 + 熔断器 ─────────────────────────────────────────────")

    # 1. LLMAuthError（不可重试）→ 立即终止，terminal_reason=LLM_ERROR
    loop_auth = _make_loop(llm_side_effect=LLMAuthError("invalid api key"))
    r_auth = async_run(loop_auth.run("任务", session_id="s1"))
    assert_true(isinstance(r_auth, AgentResult), "LLMAuthError：返回 AgentResult")
    assert_true(not r_auth.success, "LLMAuthError：success=False")
    assert_true(
        r_auth.terminal_reason == TerminalReason.LLM_ERROR,
        "LLMAuthError：terminal_reason=LLM_ERROR",
        f"实际: {r_auth.terminal_reason}",
    )

    # 2. LLMRequestError（不可重试）→ 立即终止，terminal_reason=LLM_ERROR
    loop_req = _make_loop(llm_side_effect=LLMRequestError("bad request"))
    r_req = async_run(loop_req.run("任务", session_id="s1"))
    assert_true(isinstance(r_req, AgentResult), "LLMRequestError：返回 AgentResult")
    assert_true(not r_req.success, "LLMRequestError：success=False")
    assert_true(
        r_req.terminal_reason == TerminalReason.LLM_ERROR,
        "LLMRequestError：terminal_reason=LLM_ERROR",
        f"实际: {r_req.terminal_reason}",
    )

    # 3. LLMRateLimitError（可重试）→ max_retries=0 时立即终止
    loop_rate = _make_loop(
        llm_side_effect=LLMRateLimitError("rate limited", retry_after=0.0),
        max_retries=0,
    )
    r_rate = async_run(loop_rate.run("任务", session_id="s1"))
    assert_true(isinstance(r_rate, AgentResult), "LLMRateLimitError(max_retries=0)：返回 AgentResult")
    assert_true(not r_rate.success, "LLMRateLimitError(max_retries=0)：success=False")
    assert_true(
        r_rate.terminal_reason == TerminalReason.LLM_ERROR,
        "LLMRateLimitError(max_retries=0)：terminal_reason=LLM_ERROR",
        f"实际: {r_rate.terminal_reason}",
    )

    # 4. LLMServerError（可重试）→ max_retries=0 时立即终止
    loop_srv = _make_loop(
        llm_side_effect=LLMServerError("internal error", status_code=500),
        max_retries=0,
    )
    r_srv = async_run(loop_srv.run("任务", session_id="s1"))
    assert_true(isinstance(r_srv, AgentResult), "LLMServerError(max_retries=0)：返回 AgentResult")
    assert_true(not r_srv.success, "LLMServerError(max_retries=0)：success=False")

    # 5. LLMNetworkError（可重试）→ max_retries=0 时立即终止
    loop_net = _make_loop(
        llm_side_effect=LLMNetworkError("connection failed"),
        max_retries=0,
    )
    r_net = async_run(loop_net.run("任务", session_id="s1"))
    assert_true(isinstance(r_net, AgentResult), "LLMNetworkError(max_retries=0)：返回 AgentResult")
    assert_true(not r_net.success, "LLMNetworkError(max_retries=0)：success=False")

    # 6. LLMRateLimitError 首次失败后 retry 成功 → run() 成功
    call_count = [0]
    async def _rate_then_ok(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise LLMRateLimitError("rate limited", retry_after=0.0)
        return {
            "content": "完成",
            "tool_calls": None,
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            "finish_reason": "stop",
        }

    loop_retry_ok = _make_loop(max_retries=1)
    loop_retry_ok._llm_client.call = _rate_then_ok
    r_retry = async_run(loop_retry_ok.run("任务", session_id="s1"))
    assert_true(isinstance(r_retry, AgentResult), "RateLimitError 后重试成功：返回 AgentResult")
    assert_true(r_retry.success, "RateLimitError 后重试成功：success=True")


# ══════════════════════════════════════════════════════
#  Section 6：cancel() 外部取消
# ══════════════════════════════════════════════════════

def test_cancel():
    print("\n── cancel() 外部取消 ──────────────────────────────────────────────")

    # 1. run() 开始前调用 cancel() → run 立即以 CANCELLED 终止
    loop_cancel_before = _make_loop()
    loop_cancel_before.cancel()
    r_before = async_run(loop_cancel_before.run("任务", session_id="s1"))
    assert_true(isinstance(r_before, AgentResult), "run 前 cancel：返回 AgentResult")
    assert_true(not r_before.success, "run 前 cancel：success=False")
    # terminal_reason 应为 CANCELLED 或类似
    assert_true(
        r_before.terminal_reason is not None,
        "run 前 cancel：terminal_reason 非 None",
        f"实际: {r_before.terminal_reason}",
    )

    # 2. 外部发送取消信号后 run() 终止（通过 run_stream() 事件序列验证）
    #    不应向外传播 CancelledError
    loop_cancel_mid = _make_loop(
        llm_response={"content": "进行中", "tool_calls": None,
                       "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                       "finish_reason": "stop"},
    )
    loop_cancel_mid.cancel()

    try:
        r_mid = async_run(loop_cancel_mid.run("任务", session_id="s1"))
        assert_true(isinstance(r_mid, AgentResult), "取消后 run()：返回 AgentResult 不抛异常")
    except Exception as e:
        result.fail("取消后 run()：不应向外抛异常", f"抛出: {type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════
#  Section 7：工具执行 + TOOL_HALT
# ══════════════════════════════════════════════════════

def test_tool_execution():
    print("\n── 工具执行 + TOOL_HALT ──────────────────────────────────────────")

    # 1. 正常工具调用 + tool 成功 → TOOL_CALL_START / TOOL_CALL_END 事件出现
    tool = _make_tool_def()
    tool_ok = ToolResult(success=True, data="结果数据", halt=False, duration_ms=5.0)
    call_n = [0]

    async def _two_phase(*args, **kwargs):
        call_n[0] += 1
        if call_n[0] == 1:
            return {
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "test_tool", "arguments": "{}"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                "finish_reason": "tool_calls",
            }
        return {"content": "完成", "tool_calls": None,
                "usage": {"prompt_tokens": 5, "completion_tokens": 2}, "finish_reason": "stop"}

    loop_tool = _make_loop(
        tool_def=tool,
        tool_result=tool_ok,
        guard_result="allow",
        hitl_result="pass",
        max_steps=3,
    )
    loop_tool._llm_client.call = _two_phase
    events_tool = async_run(_collect_stream_events(loop_tool, "s1"))
    etypes_tool = [e.type for e in events_tool]
    assert_true(
        StreamEventType.TOOL_CALL_START in etypes_tool,
        "正常工具调用：出现 TOOL_CALL_START 事件",
    )
    assert_true(
        StreamEventType.TOOL_CALL_END in etypes_tool,
        "正常工具调用：出现 TOOL_CALL_END 事件",
    )

    # 2. tool_result.halt=True → AGENT_HALTED 事件
    tool_halt = ToolResult(success=False, data=None, error="致命错误", halt=True, duration_ms=1.0)
    loop_halt = _make_loop(
        llm_response={
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "test_tool", "arguments": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "finish_reason": "tool_calls",
        },
        tool_def=tool,
        tool_result=tool_halt,
        guard_result="allow",
        hitl_result="pass",
        max_steps=2,
    )
    events_halt = async_run(_collect_stream_events(loop_halt, "s1"))
    etypes_halt = [e.type for e in events_halt]
    assert_true(
        StreamEventType.AGENT_HALTED in etypes_halt,
        "tool_result.halt=True：出现 AGENT_HALTED 事件",
    )

    # 3. tool_result.halt=True → terminal_reason=TOOL_HALT
    loop_halt2 = _make_loop(
        llm_response={
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "test_tool", "arguments": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "finish_reason": "tool_calls",
        },
        tool_def=tool,
        tool_result=tool_halt,
        guard_result="allow",
        hitl_result="pass",
        max_steps=2,
    )
    r_halt = async_run(loop_halt2.run("HALT 任务", session_id="s1"))
    assert_true(not r_halt.success, "TOOL_HALT：success=False")
    assert_true(
        r_halt.terminal_reason == TerminalReason.TOOL_HALT,
        "TOOL_HALT：terminal_reason=TOOL_HALT",
        f"实际: {r_halt.terminal_reason}",
    )


# ══════════════════════════════════════════════════════
#  Section 8：双入口架构
# ══════════════════════════════════════════════════════

async def _collect_stream_events(loop, session_id: str) -> list[StreamEvent]:
    """收集 run_stream() 的所有事件。"""
    events = []
    async for event in loop.run_stream("测试任务", session_id=session_id):
        events.append(event)
    return events


def test_dual_entry():
    print("\n── 双入口架构 ─────────────────────────────────────────────────────")

    # 1. run() 返回 AgentResult（正常完成）
    loop_run = _make_loop()
    r = async_run(loop_run.run("任务", session_id="s1"))
    assert_true(isinstance(r, AgentResult), "run() 返回 AgentResult 实例")
    assert_true(r.success, "run() 正常完成：success=True")
    assert_true(r.output is not None, "run() 正常完成：output 非 None")

    # 2. run_stream() 产出 StreamEvent 序列，最后一个是 RUN_END
    loop_stream = _make_loop()
    events = async_run(_collect_stream_events(loop_stream, "s1"))
    assert_true(len(events) > 0, "run_stream() 产出至少 1 个事件")
    assert_true(
        events[-1].type == StreamEventType.RUN_END,
        "run_stream() 最后一个事件是 RUN_END",
        f"实际最后事件: {events[-1].type if events else '无事件'}",
    )

    # 3. RUN_END 事件携带 AgentResult
    run_end_event = events[-1]
    assert_true(
        "result" in run_end_event.data,
        "RUN_END 事件 data 中包含 'result' 键",
    )
    assert_true(
        isinstance(run_end_event.data["result"], AgentResult),
        "RUN_END 事件 data['result'] 是 AgentResult 实例",
    )

    # 4. run_stream() 产出 RUN_START 事件（第一个）
    assert_true(
        events[0].type == StreamEventType.RUN_START,
        "run_stream() 第一个事件是 RUN_START",
        f"实际第一个事件: {events[0].type if events else '无事件'}",
    )

    # 5. run() 与 run_stream() 结果一致：同一 loop 配置下两者 success 相同
    loop_r = _make_loop()
    loop_s = _make_loop()
    r_run = async_run(loop_r.run("任务", session_id="s1"))
    events_s = async_run(_collect_stream_events(loop_s, "s1"))
    run_end_from_stream = next(
        (e for e in events_s if e.type == StreamEventType.RUN_END), None
    )
    assert_true(run_end_from_stream is not None, "run_stream() 中存在 RUN_END 事件")
    if run_end_from_stream:
        stream_result = run_end_from_stream.data["result"]
        assert_true(
            r_run.success == stream_result.success,
            "run() 与 run_stream() 结果 success 一致",
            f"run={r_run.success}, stream={stream_result.success}",
        )

    # 6. run_stream() 事件中包含 STEP_START / STEP_END
    loop_step = _make_loop()
    events_step = async_run(_collect_stream_events(loop_step, "s1"))
    etypes_step = [e.type for e in events_step]
    assert_true(
        StreamEventType.STEP_START in etypes_step,
        "run_stream() 事件序列包含 STEP_START",
    )
    assert_true(
        StreamEventType.STEP_END in etypes_step,
        "run_stream() 事件序列包含 STEP_END",
    )

    # 7. run_stream() 事件中包含 LLM_CALL_START / LLM_CALL_END
    loop_llm_ev = _make_loop()
    events_llm = async_run(_collect_stream_events(loop_llm_ev, "s1"))
    etypes_llm = [e.type for e in events_llm]
    assert_true(
        StreamEventType.LLM_CALL_START in etypes_llm,
        "run_stream() 事件序列包含 LLM_CALL_START",
    )
    assert_true(
        StreamEventType.LLM_CALL_END in etypes_llm,
        "run_stream() 事件序列包含 LLM_CALL_END",
    )


# ══════════════════════════════════════════════════════
#  Section 9：AgentResult 结构完整性
# ══════════════════════════════════════════════════════

def test_agent_result_structure():
    print("\n── AgentResult 结构完整性 ─────────────────────────────────────────")

    # 1. 正常完成：success=True，output 非 None，error=None
    loop_ok = _make_loop()
    r_ok = async_run(loop_ok.run("任务", session_id="s1"))
    assert_true(r_ok.success, "正常完成：success=True")
    assert_true(r_ok.output is not None, "正常完成：output 非 None")
    assert_true(r_ok.error is None, "正常完成：error=None")
    assert_true(
        r_ok.terminal_reason == TerminalReason.COMPLETED,
        "正常完成：terminal_reason=COMPLETED",
        f"实际: {r_ok.terminal_reason}",
    )

    # 2. 正常完成：run_id 非空字符串
    assert_true(
        isinstance(r_ok.run_id, str) and len(r_ok.run_id) > 0,
        "正常完成：run_id 非空字符串",
    )

    # 3. 正常完成：total_steps >= 1
    assert_true(
        r_ok.total_steps >= 1,
        "正常完成：total_steps >= 1",
        f"实际: {r_ok.total_steps}",
    )

    # 4. 正常完成：started_at / finished_at 非 None
    assert_true(r_ok.started_at is not None, "正常完成：started_at 非 None")
    assert_true(r_ok.finished_at is not None, "正常完成：finished_at 非 None")

    # 5. 失败时：success=False，error 非 None
    loop_fail = _make_loop(llm_side_effect=LLMAuthError("auth error"))
    r_fail = async_run(loop_fail.run("任务", session_id="s1"))
    assert_true(not r_fail.success, "失败时：success=False")
    assert_true(r_fail.error is not None, "失败时：error 非 None")
    assert_true(
        r_fail.terminal_reason is not None,
        "失败时：terminal_reason 非 None",
    )

    # 6. HITL 暂停时：paused 属性为 True
    tool_crit = _make_tool_def(sensitivity=SensitivityLevel.CRITICAL)
    loop_pause = _make_loop(
        llm_response={
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "test_tool", "arguments": "{}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "finish_reason": "tool_calls",
        },
        tool_def=tool_crit,
        guard_result="allow",
        hitl_result="need_approval",
        max_steps=2,
    )
    r_pause = async_run(loop_pause.run("HITL 任务", session_id="s1"))
    assert_true(r_pause.paused, "HITL 暂停时：paused=True")
    assert_true(not r_pause.success, "HITL 暂停时：success=False")
    assert_true(r_pause.run_state is not None, "HITL 暂停时：run_state 非 None")

    # 7. paused=False 时不意外触发
    loop_normal = _make_loop()
    r_normal = async_run(loop_normal.run("任务", session_id="s1"))
    assert_true(not r_normal.paused, "正常完成时：paused=False")

    # 8. steps 元组非 None
    assert_true(
        r_ok.steps is not None,
        "正常完成：steps 非 None",
    )

    # 9. total_input_tokens + total_output_tokens 在成功时非负
    assert_true(
        r_ok.total_input_tokens >= 0,
        "正常完成：total_input_tokens >= 0",
        f"实际: {r_ok.total_input_tokens}",
    )
    assert_true(
        r_ok.total_output_tokens >= 0,
        "正常完成：total_output_tokens >= 0",
        f"实际: {r_ok.total_output_tokens}",
    )


# ══════════════════════════════════════════════════════
#  Section 10：tool_calls 配对原子写入（400 根因回归）
# ══════════════════════════════════════════════════════

def test_tool_step_atomic_commit_after_all_results():
    print("\n── 工具 step 原子提交（assistant 之后跟齐全部结果）────────────")
    call_n = [0]

    async def _two_tools_then_done(*args, **kwargs):
        call_n[0] += 1
        if call_n[0] == 1:
            return {
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "test_tool", "arguments": "{}"}},
                    {"id": "c2", "type": "function", "function": {"name": "test_tool", "arguments": "{}"}},
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                "finish_reason": "tool_calls",
            }
        return {"content": "完成", "tool_calls": None,
                "usage": {"prompt_tokens": 5, "completion_tokens": 2}, "finish_reason": "stop"}

    tool = _make_tool_def()
    ok1 = ToolResult(success=True, data="r1", halt=False, duration_ms=1.0)
    ok2 = ToolResult(success=True, data="r2", halt=False, duration_ms=1.0)
    loop = _make_loop(llm_side_effect=_two_tools_then_done, tool_def=tool, max_steps=3)
    loop._harness_executor.execute_tools_concurrent = AsyncMock(return_value=[ok1, ok2])

    # 记录写入顺序（assistant / tool 结果共用一个 order 列表）
    order = []
    orig_assistant = loop._memory.add_assistant_message
    orig_tool = loop._memory.add_tool_result

    async def _rec_assistant(content, **kw):
        order.append(("assistant", kw.get("tool_calls")))
        return await orig_assistant(content, **kw)

    async def _rec_tool(tc_id, name, text):
        order.append(("tool", tc_id))
        return await orig_tool(tc_id, name, text)

    loop._memory.add_assistant_message = _rec_assistant
    loop._memory.add_tool_result = _rec_tool

    r = async_run(loop.run("任务", session_id="s1"))
    assert_true(r.success, "两个工具调用后正常完成：success=True")

    # 存在带 tool_calls=[c1, c2] 的 assistant 写入
    tool_assistant_idx = next(
        (i for i, (kind, tc) in enumerate(order)
         if kind == "assistant" and tc and any(t.get("id") == "c1" for t in tc)),
        None,
    )
    assert_true(
        tool_assistant_idx is not None,
        "存在 assistant(tool_calls=[c1, c2]) 写入",
        f"order={order}",
    )
    # assistant 之后必须跟齐 c1、c2 两个结果（顺序正确）
    after = order[tool_assistant_idx + 1:]
    tool_ids = [t for kind, t in after if kind == "tool"]
    assert_true(
        tool_ids[:2] == ["c1", "c2"],
        "assistant 之后跟齐 c1、c2 结果（顺序正确）",
        f"实际: {tool_ids}",
    )


def test_stop_during_tool_execution_leaves_no_orphan():
    print("\n── 工具执行中 cancel → memory 无孤儿 assistant(tool_calls) ────")
    from pandaren.engine import run_core as run_core_mod
    _saved_grace = run_core_mod.CANCEL_GRACE_SECONDS
    run_core_mod.CANCEL_GRACE_SECONDS = 0.05  # 压缩 grace 让测试秒级
    try:
        async def _hanging(*args, **kwargs):
            await asyncio.sleep(5.0)
            return [ToolResult(success=True, data="late", halt=False, duration_ms=1.0)]

        tool = _make_tool_def()
        loop = _make_loop(
            llm_response={
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "test_tool", "arguments": "{}"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                "finish_reason": "tool_calls",
            },
            tool_def=tool,
            max_steps=2,
        )
        loop._harness_executor.execute_tools_concurrent = AsyncMock(side_effect=_hanging)

        async def _scenario():
            events = []

            async def _collect():
                async for e in loop.run_stream("任务", session_id="s1"):
                    events.append(e)

            task = asyncio.create_task(_collect())
            await asyncio.sleep(0.2)  # 让主循环进入工具执行
            loop.cancel()
            await task
            return events

        events = async_run(_scenario())
        etypes = [e.type for e in events]
        assert_true(
            StreamEventType.AGENT_CANCELLED in etypes,
            "cancel 后出现 AGENT_CANCELLED 事件",
        )
        # 本轮未提交：memory 无带 tool_calls 的 assistant 写入
        assistant_calls = loop._memory.add_assistant_message.call_args_list
        has_orphan = any(
            kw.get("tool_calls") for _, kw in assistant_calls
        )
        assert_true(
            not has_orphan,
            "memory 无孤儿 assistant(tool_calls)",
            f"实际: {assistant_calls}",
        )
        tool_calls = loop._memory.add_tool_result.call_args_list
        assert_true(
            len(tool_calls) == 0,
            "memory 无任何 tool 结果（未提交）",
            f"实际: {tool_calls}",
        )

        # 下一轮消息可正常发送：再次 run 成功
        loop2 = _make_loop()
        r2 = async_run(loop2.run("下一轮", session_id="s1"))
        assert_true(r2.success, "下一轮 run 正常完成（无孤儿历史污染）")
    finally:
        run_core_mod.CANCEL_GRACE_SECONDS = _saved_grace


def test_tool_halt_commits_full_pair():
    print("\n── 首个工具 halt → 原子提交后配对完整 ─────────────────────────")

    async def _two_tools(*args, **kwargs):
        return {
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "test_tool", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "test_tool", "arguments": "{}"}},
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "finish_reason": "tool_calls",
        }

    tool = _make_tool_def()
    halt_r = ToolResult(success=False, data=None, error="致命错误", halt=True, duration_ms=1.0)
    ok_r = ToolResult(success=True, data="r2", halt=False, duration_ms=1.0)
    loop = _make_loop(llm_side_effect=_two_tools, tool_def=tool, max_steps=2)
    loop._harness_executor.execute_tools_concurrent = AsyncMock(return_value=[halt_r, ok_r])

    order = []
    orig_assistant = loop._memory.add_assistant_message
    orig_tool = loop._memory.add_tool_result

    async def _rec_assistant(content, **kw):
        order.append(("assistant", kw.get("tool_calls")))
        return await orig_assistant(content, **kw)

    async def _rec_tool(tc_id, name, text):
        order.append(("tool", tc_id, text))
        return await orig_tool(tc_id, name, text)

    loop._memory.add_assistant_message = _rec_assistant
    loop._memory.add_tool_result = _rec_tool

    r = async_run(loop.run("任务", session_id="s1"))
    assert_true(not r.success, "tool_halt：success=False")
    assert_true(
        r.terminal_reason == TerminalReason.TOOL_HALT,
        "tool_halt：terminal_reason=TOOL_HALT",
        f"实际: {r.terminal_reason}",
    )

    tool_assistant_idx = next(
        (i for i, (kind, tc) in enumerate(order)
         if kind == "assistant" and tc and any(t.get("id") == "c1" for t in tc)),
        None,
    )
    assert_true(
        tool_assistant_idx is not None,
        "存在 assistant(tool_calls=[c1, c2]) 写入",
        f"order={order}",
    )
    after = order[tool_assistant_idx + 1:]
    tool_entries = [t for t in after if t[0] == "tool"]
    tool_ids = [t[1] for t in tool_entries]
    assert_true(
        "c1" in tool_ids and "c2" in tool_ids,
        "halt 后每个 id 都有 tool 结果",
        f"实际: {tool_ids}",
    )
    c1_text = next(t[2] for t in tool_entries if t[1] == "c1")
    assert_true(
        "致命错误" in c1_text,
        "c1 结果是 halt 错误文本",
        f"实际: {c1_text!r}",
    )
    c2_text = next(t[2] for t in tool_entries if t[1] == "c2")
    assert_true(
        c2_text != "",
        "c2 有结果文本（halt break 后由占位防御补齐）",
        f"实际: {c2_text!r}",
    )


def test_nudge_uses_user_message_no_orphan_tool():
    print("\n── 空响应 nudge → user 消息而非孤儿 tool ──────────────────────")
    loop = _make_loop(
        llm_response={"content": None, "tool_calls": None,
                       "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                       "finish_reason": "stop"},
        max_steps=3,
    )
    loop._memory.inject_user_hint = MagicMock()  # 同步方法 mock

    r = async_run(loop.run("任务", session_id="s1"))
    assert_true(not r.success, "空响应重试耗尽：success=False（LLM_ERROR）")
    # 无 nudge_N 孤儿
    tool_calls = loop._memory.add_tool_result.call_args_list
    nudge_orphans = [c for c in tool_calls if c.args[0].startswith("nudge_")]
    assert_true(
        len(nudge_orphans) == 0,
        "无 tool_call_id='nudge_N' 孤儿",
        f"实际: {tool_calls}",
    )
    assert_true(
        loop._memory.inject_user_hint.called,
        "nudge 走 inject_user_hint（user 消息）",
    )


def test_loop_detect_uses_user_message_no_orphan_tool():
    print("\n── 循环纠正 → user 消息而非孤儿 tool ──────────────────────────")
    call_n = [0]

    async def _repeat_tool(*args, **kwargs):
        call_n[0] += 1
        if call_n[0] <= 6:
            return {
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "test_tool", "arguments": "{}"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                "finish_reason": "tool_calls",
            }
        return {"content": "完成", "tool_calls": None,
                "usage": {"prompt_tokens": 5, "completion_tokens": 2}, "finish_reason": "stop"}

    tool = _make_tool_def()
    ok_r = ToolResult(success=True, data="ok", halt=False, duration_ms=1.0)
    loop = _make_loop(llm_side_effect=_repeat_tool, tool_def=tool, max_steps=8)
    loop._harness_executor.execute_tools_concurrent = AsyncMock(return_value=[ok_r])
    loop._memory.inject_user_hint = MagicMock()

    r = async_run(loop.run("任务", session_id="s1"))
    assert_true(r.success, "循环纠正后正常完成：success=True")
    # 无 loop_detect_N 孤儿
    tool_calls = loop._memory.add_tool_result.call_args_list
    ld_orphans = [c for c in tool_calls if c.args[0].startswith("loop_detect_")]
    assert_true(
        len(ld_orphans) == 0,
        "无 tool_call_id='loop_detect_N' 孤儿",
        f"实际: {tool_calls}",
    )
    assert_true(
        loop._memory.inject_user_hint.called,
        "loop_detect 走 inject_user_hint（user 消息）",
    )


def test_guard_denied_writes_nothing():
    print("\n── guard 全拒 → 整步不写（无孤儿、无半提交）──────────────────────")
    call_n = [0]

    async def _two_tools_then_done(*args, **kwargs):
        call_n[0] += 1
        if call_n[0] == 1:
            return {
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "test_tool", "arguments": "{}"}},
                    {"id": "c2", "type": "function", "function": {"name": "test_tool", "arguments": "{}"}},
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                "finish_reason": "tool_calls",
            }
        return {"content": "完成", "tool_calls": None,
                "usage": {"prompt_tokens": 5, "completion_tokens": 2}, "finish_reason": "stop"}

    tool = _make_tool_def()
    loop = _make_loop(
        llm_side_effect=_two_tools_then_done, tool_def=tool,
        guard_result="deny", max_steps=3,
    )

    order = []
    orig_assistant = loop._memory.add_assistant_message
    orig_tool = loop._memory.add_tool_result

    async def _rec_assistant(content, **kw):
        order.append(("assistant", kw.get("tool_calls")))
        return await orig_assistant(content, **kw)

    async def _rec_tool(tc_id, name, text):
        order.append(("tool", tc_id, text))
        return await orig_tool(tc_id, name, text)

    loop._memory.add_assistant_message = _rec_assistant
    loop._memory.add_tool_result = _rec_tool

    r = async_run(loop.run("任务", session_id="s1"))
    assert_true(r.success, "guard 全拒后 LLM 纠正正常完成：success=True")

    # 全拒时 approved_calls 为空 → 整步不提交：memory 零写入
    # （原子性"不写侧"：不留 assistant(tool_calls) 孤儿、不留半截 tool 结果）
    assert_true(
        len(order) == 1 and order[0] == ("assistant", None),
        "全拒 step 不写 assistant(tool_calls)、不写任何 tool 结果",
        f"order={order}",
    )
    # 拒绝后不应真正执行任何工具
    loop._harness_executor.execute_tools_concurrent.assert_not_called()


# ══════════════════════════════════════════════════════
#  Main 入口
# ══════════════════════════════════════════════════════

SECTIONS = {
    "o3": test_o3_run_never_raises,
    "hc5": test_hc5_execution_limits,
    "hc3_guard": test_hc3_permission_guard,
    "hc3_hitl": test_hc3_hc6_hitl,
    "llm_errors": test_llm_errors,
    "cancel": test_cancel,
    "tool_exec": test_tool_execution,
    "dual_entry": test_dual_entry,
    "agent_result": test_agent_result_structure,
    "tool_pair_atomic": test_tool_step_atomic_commit_after_all_results,
    "tool_pair_cancel": test_stop_during_tool_execution_leaves_no_orphan,
    "tool_pair_halt": test_tool_halt_commits_full_pair,
    "tool_pair_nudge": test_nudge_uses_user_message_no_orphan_tool,
    "tool_pair_loop_detect": test_loop_detect_uses_user_message_no_orphan_tool,
    "tool_pair_guard_denied": test_guard_denied_writes_nothing,
}

if __name__ == "__main__":
    import sys

    selected = None
    if "--section" in sys.argv:
        idx = sys.argv.index("--section")
        if idx + 1 < len(sys.argv):
            selected = sys.argv[idx + 1]

    print("=" * 60)
    print(" Pandaren Loop 层集成测试（需求驱动黑盒）")
    print("=" * 60)

    if selected:
        fn = SECTIONS.get(selected)
        if fn is None:
            print(f"未知 section: {selected}。可用: {list(SECTIONS)}")
            sys.exit(1)
        fn()
        ok = result.summary(selected)
    else:
        for name, fn in SECTIONS.items():
            fn()
        ok = result.summary("全部")

    sys.exit(0 if ok else 1)
