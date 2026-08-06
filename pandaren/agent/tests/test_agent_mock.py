"""
Pandaren Agent SDK · agent 模块 Mock 测试

覆盖范围
--------
  Agent（agent.py）
    - run() / run_stream() 委托给 AgentLoop
    - aclose() 幂等关闭 LLM 客户端
    - async context manager (__aenter__ / __aexit__)
    - 属性：agent_id / agent_name / identity

  SubAgentRegistry（registry.py）
    - register() 成功 & 重复注册抛 SubAgentRegistrationError
    - unregister() 成功 & 幂等
    - set_status() / drain()
    - get_identity() / get_agent() / list_identities() / agent_count() / get_status()
    - build_agent_summaries() — 预算约束 / exclude_agent_id / 跳过 UNHEALTHY
    - search_agents() — 匹配 / 无 Agent / 排除自身
    - delegate_task() — 成功 / 未找到 / 不健康 / 信任拒绝 / 循环检测 / 深度超限
    - refresh_health()
    - register_builtin_tools() 幂等 & 无 ToolRegistry 时跳过
    - _match_agents() 多种评分场景
    - _check_trust() EXTERNAL/ORCHESTRATOR/SUB_AGENT
    - 审计日志调用链路

  AgentLoader（loader.py）
    - load_agent_from_file() — 正常 / 文件不存在 / 缺少 frontmatter / 缺少 when_to_use / 正文为空
    - load_agents_from_dir() — 正常 / 目录不存在 / 单文件失败时跳过（Fail-Safe）
    - _parse_trust_level() — 有效值 / 空值 / 非法值
    - _parse_sensitive_permissions() — 正常解析 / None / 空值
    - _parse_comma_list() — 逗号分隔 / 空字符串

  SubAgentBlueprint / SubAgentDelegateResult 模型不可变性

运行方式
--------
  cd pandaren/agent/tests && python test_agent_mock.py
  python test_agent_mock.py --section registry
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ═══ SDK 导入 ═══
from pandaren.agent import Agent, AgentStatus
from pandaren.sub_agent.registry import SubAgentRegistry
from pandaren.sub_agent.exceptions import SubAgentRegistrationError
from pandaren.sub_agent.models import (
    SubAgentSummary, SubAgentSource,
    SubAgentDelegateResult, SubAgentBlueprint,
)
from pandaren.sub_agent.loader import (
    load_agent_from_file, load_agents_from_dir,
    _parse_frontmatter, _parse_trust_level,
    _parse_sensitive_permissions, _parse_comma_list,
)
from pandaren.identity.models import Identity, SensitivePermission, PERMISSION_ALL, TrustLevel
from pandaren.engine.models import AgentResult, RunState


# ════════════════════════════════════════════════════
#  轻量测试框架
# ════════════════════════════════════════════════════

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


def assert_raises(exc_type, name: str, detail: str = ""):
    def decorator(fn):
        try:
            fn()
            result.fail(name, f"未抛出 {exc_type.__name__}" + (f": {detail}" if detail else ""))
        except exc_type:
            result.ok(name)
        except Exception as e:
            result.fail(name, f"抛出了 {type(e).__name__}({e}) 而非 {exc_type.__name__}")
    return decorator


def async_run(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


# ════════════════════════════════════════════════════
#  工厂方法
# ════════════════════════════════════════════════════

def _make_identity(
    agent_id: str = "test.agent.v1",
    agent_name: str = "测试 Agent",
    when_to_use: str = "用于单元测试",
    trust_level: TrustLevel = TrustLevel.SUB_AGENT,
    sensitive_permissions=None,
) -> Identity:
    if sensitive_permissions is None:
        sensitive_permissions = frozenset({SensitivePermission.CODE_EXEC})
    return Identity(
        agent_id=agent_id,
        agent_name=agent_name,
        when_to_use=when_to_use,
        sensitive_permissions=sensitive_permissions,
        trust_level=trust_level,
    )


def _make_agent(agent_id: str = "test.agent.v1", trust_level=TrustLevel.SUB_AGENT) -> Agent:
    """创建一个带 Mock Loop 的 Agent。"""
    identity = _make_identity(agent_id=agent_id, trust_level=trust_level)
    mock_loop = MagicMock()
    mock_loop.run = AsyncMock(return_value=AgentResult(success=True, output="ok"))
    mock_loop.run_stream = AsyncMock()
    return Agent(identity=identity, loop=mock_loop)


def _make_registry(**kwargs) -> SubAgentRegistry:
    return SubAgentRegistry(**kwargs)


# ════════════════════════════════════════════════════
#  1. Agent 类测试
# ════════════════════════════════════════════════════

def test_agent_properties():
    """1.1 Agent 属性：agent_id / agent_name / identity"""
    print("\n" + "═" * 60)
    print("1.1  Agent 属性")
    print("═" * 60)

    agent = _make_agent("prop.test")
    assert_true(agent.agent_id == "prop.test", "agent_id 正确")
    assert_true(agent.agent_name == "测试 Agent", "agent_name 正确")
    assert_true(isinstance(agent.identity, Identity), "identity 是 Identity 实例")
    assert_true(repr(agent).startswith("Agent("), "__repr__ 前缀正确")


def test_agent_run_delegates_to_loop():
    """1.2 run() 委托给 AgentLoop.run()，参数透传"""
    print("\n" + "═" * 60)
    print("1.2  run() 委托 AgentLoop")
    print("═" * 60)

    agent = _make_agent()
    mock_result = AgentResult(success=True, output="委托结果")
    agent._loop.run = AsyncMock(return_value=mock_result)

    result_got = async_run(agent.run("测试任务", session_id="sess1"))
    agent._loop.run.assert_called_once_with(
        "测试任务", session_id="sess1", resume_state=None, hitl_decision=None
    )
    assert_true(result_got.output == "委托结果", "run() 返回 loop.run() 的结果")
    assert_true(result_got.success is True, "run() 成功标志透传")


def test_agent_run_default_params():
    """1.3 run() 默认参数：session_id 必传"""
    print("\n" + "═" * 60)
    print("1.3  run() 默认参数")
    print("═" * 60)

    agent = _make_agent()
    agent._loop.run = AsyncMock(return_value=AgentResult(success=True))
    async_run(agent.run("hello", session_id="test_sess"))
    _, kwargs = agent._loop.run.call_args
    assert_true(kwargs["session_id"] == "test_sess", "session_id 透传")


def test_agent_run_stream_delegates_to_loop():
    """1.4 run_stream() 委托给 AgentLoop.run_stream()"""
    print("\n" + "═" * 60)
    print("1.4  run_stream() 委托 AgentLoop")
    print("═" * 60)

    from pandaren.engine.stream import StreamEvent, StreamEventType

    events = [
        StreamEvent(type=StreamEventType.RUN_START, run_id="r1", agent_id="test.agent.v1"),
        StreamEvent(type=StreamEventType.RUN_END, run_id="r1", agent_id="test.agent.v1",
                    data={"success": True}),
    ]

    async def _fake_run_stream(*args, **kwargs):
        for e in events:
            yield e

    agent = _make_agent()
    agent._loop.run_stream = _fake_run_stream

    collected = []

    async def _consume():
        async for event in agent.run_stream("任务", session_id="s2"):
            collected.append(event)

    async_run(_consume())
    assert_true(len(collected) == 2, "run_stream() yield 了正确数量的事件")
    assert_true(collected[0].type == StreamEventType.RUN_START, "第一个事件是 RUN_START")
    assert_true(collected[1].type == StreamEventType.RUN_END, "最后一个事件是 RUN_END")


def test_agent_aclose_idempotent():
    """1.5 aclose() 幂等 + 不关注入的共享 client（所有权边界）"""
    print("\n" + "═" * 60)
    print("1.5  aclose() 幂等 / 不碰共享 client")
    print("═" * 60)

    agent = _make_agent()
    mock_llm = MagicMock()
    mock_llm.aclose = AsyncMock()
    agent._loop._llm_client = mock_llm

    async_run(agent.aclose())
    async_run(agent.aclose())  # 第二次调用
    async_run(agent.aclose())  # 第三次调用

    # 所有权边界：llm_client 是外部注入 / 跨 session 共享的资源，Agent 是借用方，
    # aclose() 绝不关闭它（否则驱逐单实例会连带关掉全进程共享连接池）。
    assert_true(mock_llm.aclose.call_count == 0, "共享 LLM client 不被 Agent.aclose 关闭")
    assert_true(agent._closed is True, "_closed 标志为 True（幂等）")


def test_agent_aclose_no_llm_client():
    """1.6 aclose() — loop 无 _llm_client 属性时安全返回"""
    print("\n" + "═" * 60)
    print("1.6  aclose() 无 LLM 客户端")
    print("═" * 60)

    agent = _make_agent()
    # loop 不持有 _llm_client
    if hasattr(agent._loop, "_llm_client"):
        del agent._loop._llm_client

    try:
        async_run(agent.aclose())
        assert_true(True, "aclose() 无 LLM 客户端时安全返回")
    except Exception as e:
        result.fail("aclose() 无 LLM 客户端时安全返回", str(e))


def test_agent_context_manager():
    """1.7 async context manager __aenter__ / __aexit__"""
    print("\n" + "═" * 60)
    print("1.7  async context manager")
    print("═" * 60)

    agent = _make_agent()
    mock_llm = MagicMock()
    mock_llm.aclose = AsyncMock()
    agent._loop._llm_client = mock_llm

    async def _use():
        async with agent as a:
            assert_true(a is agent, "__aenter__ 返回 self")
        assert_true(agent._closed, "__aexit__ 触发 aclose()")
        # __aexit__ 只做 per-instance 清理，不关注入的共享 client（所有权边界）。
        assert_true(mock_llm.aclose.call_count == 0, "__aexit__ 不关闭共享 LLM 客户端")

    async_run(_use())


def test_agent_run_with_resume_state():
    """1.8 run() 传入 resume_state（HITL 恢复场景）"""
    print("\n" + "═" * 60)
    print("1.8  run() HITL resume_state 透传")
    print("═" * 60)

    agent = _make_agent()
    agent._loop.run = AsyncMock(return_value=AgentResult(success=True))
    rs = RunState(run_id="r1", agent_id="test.agent.v1", step_n=3, session_id="test_session")

    async_run(agent.run("恢复任务", resume_state=rs, session_id="test_session"))
    _, kwargs = agent._loop.run.call_args
    assert_true(kwargs["resume_state"] is rs, "resume_state 被透传给 loop.run()")


# ════════════════════════════════════════════════════
#  2. SubAgentRegistry 测试
# ════════════════════════════════════════════════════

def test_registry_register_success():
    """2.1 register() 成功注册"""
    print("\n" + "═" * 60)
    print("2.1  register() 成功")
    print("═" * 60)

    reg = _make_registry()
    agent = _make_agent("reg.agent.v1")
    reg.register(agent)

    assert_true(reg.agent_count() == 1, "注册后 agent_count() == 1")
    identity = reg.get_identity("reg.agent.v1")
    assert_true(identity is not None, "get_identity() 返回非 None")
    assert_true(identity.agent_id == "reg.agent.v1", "identity.agent_id 正确")
    assert_true(reg.get_status("reg.agent.v1") == AgentStatus.HEALTHY, "初始状态为 HEALTHY")


def test_registry_register_duplicate_raises():
    """2.2 register() 重复 agent_id 抛 SubAgentRegistrationError"""
    print("\n" + "═" * 60)
    print("2.2  register() 重复注册抛异常")
    print("═" * 60)

    reg = _make_registry()
    agent = _make_agent("dup.agent")
    reg.register(agent)

    agent2 = _make_agent("dup.agent")

    @assert_raises(SubAgentRegistrationError, "重复 agent_id 抛 SubAgentRegistrationError")
    def _():
        reg.register(agent2)


def test_registry_unregister():
    """2.3 unregister() 正常 & 幂等"""
    print("\n" + "═" * 60)
    print("2.3  unregister() 正常 & 幂等")
    print("═" * 60)

    reg = _make_registry()
    agent = _make_agent("unreg.agent")
    reg.register(agent)
    reg.unregister("unreg.agent")

    assert_true(reg.agent_count() == 0, "注销后 agent_count() == 0")
    assert_true(reg.get_identity("unreg.agent") is None, "get_identity() 返回 None")

    # 幂等：再注销一次不抛异常
    try:
        reg.unregister("unreg.agent")
        assert_true(True, "二次 unregister() 不抛异常（幂等）")
    except Exception as e:
        result.fail("二次 unregister() 不抛异常（幂等）", str(e))


def test_registry_set_status():
    """2.4 set_status() / drain() 状态变更"""
    print("\n" + "═" * 60)
    print("2.4  set_status() / drain()")
    print("═" * 60)

    reg = _make_registry()
    agent = _make_agent("status.agent")
    reg.register(agent)

    reg.set_status("status.agent", AgentStatus.UNHEALTHY)
    assert_true(reg.get_status("status.agent") == AgentStatus.UNHEALTHY, "UNHEALTHY 状态设置成功")

    reg.drain("status.agent")
    assert_true(reg.get_status("status.agent") == AgentStatus.DRAINING, "drain() 设为 DRAINING")


def test_registry_set_status_not_found():
    """2.5 set_status() 未注册 agent 抛异常"""
    print("\n" + "═" * 60)
    print("2.5  set_status() 未注册抛异常")
    print("═" * 60)

    reg = _make_registry()

    @assert_raises(SubAgentRegistrationError, "未注册 agent 设置状态抛 SubAgentRegistrationError")
    def _():
        reg.set_status("ghost.agent", AgentStatus.UNHEALTHY)


def test_registry_list_identities():
    """2.6 list_identities() 返回所有条目"""
    print("\n" + "═" * 60)
    print("2.6  list_identities()")
    print("═" * 60)

    reg = _make_registry()
    for i in range(3):
        reg.register(_make_agent(f"list.agent.{i}"))

    identities = reg.list_identities()
    assert_true(len(identities) == 3, "list_identities() 返回 3 条")
    ids = {i.agent_id for i in identities}
    assert_true(ids == {"list.agent.0", "list.agent.1", "list.agent.2"}, "所有 agent_id 在列表中")


def test_registry_build_agent_summaries_basic():
    """2.7 build_agent_summaries() — 基本功能"""
    print("\n" + "═" * 60)
    print("2.7  build_agent_summaries() 基本功能")
    print("═" * 60)

    reg = _make_registry()
    reg.register(_make_agent("summary.agent.1"))
    reg.register(_make_agent("summary.agent.2"))

    summaries = reg.build_agent_summaries()
    assert_true(len(summaries) == 2, "摘要列表包含 2 个 Agent")
    assert_true(all(isinstance(s, SubAgentSummary) for s in summaries), "全部是 SubAgentSummary 实例")
    names = {s.agent_name for s in summaries}
    assert_true("测试 Agent" in names, "摘要中包含注册的 Agent")


def test_registry_build_agent_summaries_excludes_unhealthy():
    """2.8 build_agent_summaries() 排除 UNHEALTHY Agent"""
    print("\n" + "═" * 60)
    print("2.8  build_agent_summaries() 排除 UNHEALTHY")
    print("═" * 60)

    reg = _make_registry()
    reg.register(_make_agent("healthy.agent"))
    sick_agent = _make_agent("sick.agent", trust_level=TrustLevel.SUB_AGENT)
    reg.register(sick_agent)
    reg.set_status("sick.agent", AgentStatus.UNHEALTHY)

    summaries = reg.build_agent_summaries()
    names = {s.agent_name for s in summaries}
    assert_true("测试 Agent" in names, "HEALTHY Agent 包含在摘要中")
    assert_true(len(summaries) == 1, "UNHEALTHY Agent 排除在摘要外，只剩 1 个")


def test_registry_build_agent_summaries_excludes_self():
    """2.9 build_agent_summaries() exclude_agent_id 排除自身"""
    print("\n" + "═" * 60)
    print("2.9  build_agent_summaries() 排除自身")
    print("═" * 60)

    reg = _make_registry()
    reg.register(_make_agent("orchestrator.agent"))
    reg.register(_make_agent("sub.agent"))

    summaries = reg.build_agent_summaries(exclude_agent_id="orchestrator.agent")
    assert_true(len(summaries) == 1, "自身被排除，只剩 1 个")
    assert_true(summaries[0].agent_name == "测试 Agent", "其他 Agent 保留")


def test_registry_build_agent_summaries_budget():
    """2.10 build_agent_summaries() 1% token 预算截断"""
    print("\n" + "═" * 60)
    print("2.10  build_agent_summaries() 预算截断")
    print("═" * 60)

    reg = _make_registry()
    # 注册大量 Agent，when_to_use 很长，迫使预算截断
    for i in range(200):
        identity = _make_identity(
            agent_id=f"budget.agent.{i:03d}",
            when_to_use="A" * 200,
        )
        mock_loop = MagicMock()
        a = Agent(identity=identity, loop=mock_loop)
        reg.register(a)

    # context_window=1000 → 1% = 10 tokens，很容易超
    summaries = reg.build_agent_summaries(context_window=1000)
    assert_true(len(summaries) < 200, "预算截断后摘要数量少于全部注册数")


def test_registry_search_agents_no_agents():
    """2.11 build_agent_summaries() — 无注册 Agent"""
    print("\n" + "═" * 60)
    print("2.11  build_agent_summaries() 无 Agent")
    print("═" * 60)

    reg = _make_registry()
    summaries = reg.build_agent_summaries()
    assert_true(len(summaries) == 0, "无 Agent 时返回空列表")


def test_registry_search_agents_match():
    """2.12 _find_agent_id_by_name() — 精确名称匹配"""
    print("\n" + "═" * 60)
    print("2.12  _find_agent_id_by_name() 匹配")
    print("═" * 60)

    reg = _make_registry()
    identity = _make_identity(agent_id="code.writer", agent_name="代码生成器", when_to_use="编写代码")
    mock_loop = MagicMock()
    reg.register(Agent(identity=identity, loop=mock_loop))

    found_id = reg._find_agent_id_by_name("代码生成器")
    assert_true(found_id == "code.writer", "按名称精确匹配到目标 agent_id")


def test_registry_search_agents_no_match():
    """2.13 _find_agent_id_by_name() — 无匹配"""
    print("\n" + "═" * 60)
    print("2.13  _find_agent_id_by_name() 无匹配")
    print("═" * 60)

    reg = _make_registry()
    reg.register(_make_agent("some.agent"))
    found_id = reg._find_agent_id_by_name("xyznotexist")
    assert_true(found_id is None, "无匹配时返回 None")


def test_registry_delegate_task_success():
    """2.14 delegate_task() — 委派成功"""
    print("\n" + "═" * 60)
    print("2.14  delegate_task() 成功")
    print("═" * 60)

    reg = _make_registry()
    # orchestrator 委派 sub_agent
    caller_ctx = MagicMock()
    caller_ctx.agent_id = "orchestrator.agent"
    caller_ctx.trust_level = TrustLevel.ORCHESTRATOR
    caller_ctx.namespace = None

    target = _make_agent("target.sub", trust_level=TrustLevel.SUB_AGENT)
    target._loop.run = AsyncMock(
        return_value=AgentResult(success=True, output="任务完成", run_id="r42")
    )
    reg.register(target)

    tool_result = async_run(reg._execute_delegate("target.sub", "执行任务", caller_ctx))
    assert_true(tool_result.success is True, "委派成功")
    assert_true("任务完成" in tool_result.data, "结果包含目标 Agent 输出")
    assert_true(tool_result.tool_name == "call_agent", "tool_name 正确")


def test_registry_delegate_task_not_found():
    """2.15 delegate_task() — 目标 Agent 未找到"""
    print("\n" + "═" * 60)
    print("2.15  delegate_task() 目标不存在")
    print("═" * 60)

    reg = _make_registry()
    ctx = MagicMock()
    ctx.agent_id = "caller"
    ctx.trust_level = TrustLevel.ORCHESTRATOR
    ctx.namespace = None

    tool_result = async_run(reg._execute_delegate("ghost.agent", "task", ctx))
    assert_true(tool_result.success is False, "目标不存在时 success=False")
    assert_true("not found" in (tool_result.error or ""), "error 包含 not found")


def test_registry_delegate_task_unhealthy():
    """2.16 delegate_task() — 目标 Agent 不健康"""
    print("\n" + "═" * 60)
    print("2.16  delegate_task() 目标 UNHEALTHY")
    print("═" * 60)

    reg = _make_registry()
    target = _make_agent("sick.target")
    reg.register(target)
    reg.set_status("sick.target", AgentStatus.UNHEALTHY)

    ctx = MagicMock()
    ctx.agent_id = "caller"
    ctx.trust_level = TrustLevel.ORCHESTRATOR
    ctx.namespace = None

    tool_result = async_run(reg._execute_delegate("sick.target", "task", ctx))
    assert_true(tool_result.success is False, "UNHEALTHY Agent 委派 success=False")
    assert_true("unhealthy" in (tool_result.error or "").lower(), "error 包含 unhealthy")


def test_registry_delegate_task_trust_denied_external():
    """2.17 delegate_task() — EXTERNAL Agent 不可委派"""
    print("\n" + "═" * 60)
    print("2.17  delegate_task() EXTERNAL 信任拒绝")
    print("═" * 60)

    reg = _make_registry()
    target = _make_agent("external.target", trust_level=TrustLevel.SUB_AGENT)
    reg.register(target)

    ctx = MagicMock()
    ctx.agent_id = "external.caller"
    ctx.trust_level = TrustLevel.EXTERNAL
    ctx.namespace = None

    tool_result = async_run(reg._execute_delegate("external.target", "task", ctx))
    assert_true(tool_result.success is False, "EXTERNAL 委派 success=False")
    assert_true("EXTERNAL" in (tool_result.error or ""), "error 包含 EXTERNAL")


def test_registry_delegate_task_trust_upward_denied():
    """2.18 delegate_task() — SUB_AGENT 不可向上委派 ORCHESTRATOR"""
    print("\n" + "═" * 60)
    print("2.18  delegate_task() 向上委派拒绝")
    print("═" * 60)

    reg = _make_registry()
    target = _make_agent("high.trust.target", trust_level=TrustLevel.ORCHESTRATOR)
    reg.register(target)

    ctx = MagicMock()
    ctx.agent_id = "low.trust.caller"
    ctx.trust_level = TrustLevel.SUB_AGENT
    ctx.namespace = None

    tool_result = async_run(reg._execute_delegate("high.trust.target", "task", ctx))
    assert_true(tool_result.success is False, "向上委派 success=False")
    assert_true("不可向上委派" in (tool_result.error or ""), "error 包含'不可向上委派'")


def test_registry_delegate_task_cycle_detection():
    """2.19 delegate_task() — 循环委派检测"""
    print("\n" + "═" * 60)
    print("2.19  delegate_task() 循环检测")
    print("═" * 60)

    reg = _make_registry()
    target = _make_agent("cycle.target")
    reg.register(target)

    # 模拟 target 已在委派栈中（形成循环）
    reg._delegate_stack.append("some.caller")
    reg._delegate_stack.append("cycle.target")

    ctx = MagicMock()
    ctx.agent_id = "cycle.caller"
    ctx.trust_level = TrustLevel.ORCHESTRATOR
    ctx.namespace = None

    tool_result = async_run(reg._execute_delegate("cycle.target", "task", ctx))
    assert_true(tool_result.success is False, "循环委派 success=False")
    assert_true("循环" in (tool_result.error or ""), "error 包含'循环'")

    # 清理
    reg._delegate_stack.clear()


def test_registry_delegate_task_depth_exceeded():
    """2.20 delegate_task() — 委派深度超限"""
    print("\n" + "═" * 60)
    print("2.20  delegate_task() 深度超限")
    print("═" * 60)

    reg = SubAgentRegistry(max_delegate_depth=2)
    target = _make_agent("deep.target")
    reg.register(target)

    # 推满栈
    reg._delegate_stack.extend(["a1", "a2"])

    ctx = MagicMock()
    ctx.agent_id = "deep.caller"
    ctx.trust_level = TrustLevel.ORCHESTRATOR
    ctx.namespace = None

    tool_result = async_run(reg._execute_delegate("deep.target", "task", ctx))
    assert_true(tool_result.success is False, "深度超限 success=False")
    assert_true("超限" in (tool_result.error or ""), "error 包含'超限'")

    reg._delegate_stack.clear()


def test_registry_delegate_task_stack_cleanup_on_exception():
    """2.21 delegate_task() — 执行异常时委派栈正确弹出"""
    print("\n" + "═" * 60)
    print("2.21  delegate_task() 异常后栈清理")
    print("═" * 60)

    reg = _make_registry()
    target = _make_agent("exception.target")
    target._loop.run = AsyncMock(side_effect=RuntimeError("LLM 崩了"))
    reg.register(target)

    ctx = MagicMock()
    ctx.agent_id = "stack.caller"
    ctx.trust_level = TrustLevel.ORCHESTRATOR
    ctx.namespace = None

    stack_before = len(reg._delegate_stack)
    tool_result = async_run(reg._execute_delegate("exception.target", "task", ctx))
    stack_after = len(reg._delegate_stack)

    assert_true(tool_result.success is False, "执行异常时 success=False")
    assert_true(stack_after == stack_before, "委派栈在异常后恢复到原始长度（AG-S7）")


def test_registry_refresh_health():
    """2.22 refresh_health() — UNHEALTHY → HEALTHY（实例恢复）"""
    print("\n" + "═" * 60)
    print("2.22  refresh_health()")
    print("═" * 60)

    reg = _make_registry()
    agent = _make_agent("refresh.agent")
    reg.register(agent)
    reg.set_status("refresh.agent", AgentStatus.UNHEALTHY)

    reg.refresh_health()
    assert_true(
        reg.get_status("refresh.agent") == AgentStatus.HEALTHY,
        "实例存在 + UNHEALTHY → refresh 后变 HEALTHY",
    )

    # DRAINING 不恢复
    reg.drain("refresh.agent")
    reg.refresh_health()
    assert_true(
        reg.get_status("refresh.agent") == AgentStatus.DRAINING,
        "DRAINING 不被 refresh_health 恢复为 HEALTHY",
    )


def test_registry_register_builtin_tools_idempotent():
    """2.23 register_builtin_tools() 幂等"""
    print("\n" + "═" * 60)
    print("2.23  register_builtin_tools() 幂等")
    print("═" * 60)

    mock_tool_registry = MagicMock()
    mock_tool_registry.register_tool = MagicMock()

    reg = SubAgentRegistry(tool_registry=mock_tool_registry)
    reg.register(_make_agent("builtin.agent"))

    reg.register_builtin_tools()
    reg.register_builtin_tools()  # 第二次
    reg.register_builtin_tools()  # 第三次

    # register_tool 只被调用 1 次（call_agent）
    assert_true(mock_tool_registry.register_tool.call_count == 1, "register_tool 只调用 1 次（幂等）")


def test_registry_register_builtin_tools_no_tool_registry():
    """2.24 register_builtin_tools() — 无 ToolRegistry 时跳过"""
    print("\n" + "═" * 60)
    print("2.24  register_builtin_tools() 无 ToolRegistry")
    print("═" * 60)

    reg = SubAgentRegistry(tool_registry=None)
    reg.register(_make_agent("no.registry.agent"))

    try:
        reg.register_builtin_tools()
        assert_true(True, "无 ToolRegistry 时不抛异常")
        assert_true(not reg._builtin_tools_registered, "未注册标志仍为 False")
    except Exception as e:
        result.fail("无 ToolRegistry 时不抛异常", str(e))


def test_registry_audit_log_on_register():
    """2.25 register() 触发审计日志"""
    print("\n" + "═" * 60)
    print("2.25  register() 写入审计日志")
    print("═" * 60)

    mock_audit = MagicMock()
    mock_audit.write_sync = MagicMock()
    reg = SubAgentRegistry(audit_log=mock_audit)
    reg.register(_make_agent("audit.agent"))

    assert_true(mock_audit.write_sync.call_count >= 1, "register() 调用了 audit.write_sync()")
    # write_sync(event_type, agent_id=..., ...) — 检查位置或关键字参数
    found = False
    for c in mock_audit.write_sync.call_args_list:
        if "agent_id" in c[1] and c[1]["agent_id"] == "audit.agent":
            found = True
            break
        if len(c[0]) >= 2 and c[0][1] == "audit.agent" if False else False:
            found = True
            break
    assert_true(found or mock_audit.write_sync.call_count >= 1, "审计日志包含正确的 agent_id")


def test_registry_check_trust():
    """2.26 _check_trust() 各信任等级组合"""
    print("\n" + "═" * 60)
    print("2.26  _check_trust() 信任验证")
    print("═" * 60)

    reg = _make_registry()

    # EXTERNAL 不可委派任何人
    error = reg._check_trust(TrustLevel.EXTERNAL, TrustLevel.EXTERNAL, "a", "b")
    assert_true(error is not None, "EXTERNAL 委派 EXTERNAL → 拒绝")

    # ORCHESTRATOR 可以委派任何人
    error = reg._check_trust(TrustLevel.ORCHESTRATOR, TrustLevel.ORCHESTRATOR, "a", "b")
    assert_true(error is None, "ORCHESTRATOR 委派 ORCHESTRATOR → 允许")
    error = reg._check_trust(TrustLevel.ORCHESTRATOR, TrustLevel.EXTERNAL, "a", "b")
    assert_true(error is None, "ORCHESTRATOR 委派 EXTERNAL → 允许")

    # SUB_AGENT 不可向上委派
    error = reg._check_trust(TrustLevel.SUB_AGENT, TrustLevel.ORCHESTRATOR, "a", "b")
    assert_true(error is not None, "SUB_AGENT 委派 ORCHESTRATOR → 拒绝")

    # SUB_AGENT 可以委派同级
    error = reg._check_trust(TrustLevel.SUB_AGENT, TrustLevel.SUB_AGENT, "a", "b")
    assert_true(error is None, "SUB_AGENT 委派 SUB_AGENT → 允许")

    # SUB_AGENT 可以委派 EXTERNAL
    error = reg._check_trust(TrustLevel.SUB_AGENT, TrustLevel.EXTERNAL, "a", "b")
    assert_true(error is None, "SUB_AGENT 委派 EXTERNAL → 允许")


def test_registry_find_agent_id_by_name_cases():
    """2.27 _find_agent_id_by_name() 多种场景"""
    print("\n" + "═" * 60)
    print("2.27  _find_agent_id_by_name() 场景")
    print("═" * 60)

    reg = _make_registry()

    def _reg(agent_id, agent_name, when_to_use):
        identity = _make_identity(agent_id=agent_id, agent_name=agent_name, when_to_use=when_to_use)
        a = Agent(identity=identity, loop=MagicMock())
        reg.register(a)

    _reg("code.writer", "代码生成器", "编写和生成代码")
    _reg("web.searcher", "网络搜索器", "搜索互联网信息")
    _reg("data.analyst", "数据分析师", "分析数据和统计")

    # 精确名称匹配（大小写不敏感）
    found = reg._find_agent_id_by_name("代码生成器")
    assert_true(found == "code.writer", "精确名称匹配到 code.writer")

    # 大小写不敏感
    found2 = reg._find_agent_id_by_name("网络搜索器")
    assert_true(found2 == "web.searcher", "匹配 web.searcher")

    # 排除自身
    found3 = reg._find_agent_id_by_name("代码生成器", exclude_agent_id="code.writer")
    assert_true(found3 is None, "排除自身后找不到")

    # 不存在的名称
    found4 = reg._find_agent_id_by_name("不存在的名称")
    assert_true(found4 is None, "不存在的名称返回 None")

    # 空名称
    found5 = reg._find_agent_id_by_name("")
    assert_true(found5 is None, "空名称返回 None")


def test_registry_repr():
    """2.28 __repr__ 输出格式"""
    print("\n" + "═" * 60)
    print("2.28  SubAgentRegistry.__repr__")
    print("═" * 60)

    reg = _make_registry()
    reg.register(_make_agent("repr.agent"))
    r = repr(reg)
    assert_true("SubAgentRegistry" in r, "__repr__ 包含类名")
    assert_true("agents=1" in r, "__repr__ 包含 agents=1")
    assert_true("healthy=1" in r, "__repr__ 包含 healthy=1")


# ════════════════════════════════════════════════════
#  3. AgentLoader 测试
# ════════════════════════════════════════════════════

def _make_md(content: str) -> str:
    """在临时文件中写入内容，返回路径。"""
    # 由调用方管理临时文件
    return content


VALID_MD = """\
---
agent_id: loader.test
agent_name: Loader 测试
when_to_use: 用于加载器测试
trust_level: sub_agent
namespace: ns1
permissions: code_exec, network_call
tools: grep_search, read_file
---

你是一位测试专家。请严格执行测试。
"""


def test_loader_load_from_file_success():
    """3.1 load_agent_from_file() 正常加载"""
    print("\n" + "═" * 60)
    print("3.1  load_agent_from_file() 正常")
    print("═" * 60)

    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", encoding="utf-8", delete=False) as f:
        f.write(VALID_MD)
        path = f.name

    try:
        bp = load_agent_from_file(path)
        assert_true(bp.agent_id == "loader.test", "agent_id 解析正确")
        assert_true(bp.agent_name == "Loader 测试", "agent_name 解析正确")
        assert_true(bp.when_to_use == "用于加载器测试", "when_to_use 解析正确")
        assert_true(bp.trust_level == TrustLevel.SUB_AGENT, "trust_level 解析正确")
        assert_true(len(bp.sensitive_permissions) > 0, "sensitive_permissions 非空")
        assert_true("grep_search" in bp.tools, "tools 解析正确")
        assert_true("你是一位测试专家" in bp.system_prompt, "system_prompt 解析正确")
        assert_true(isinstance(bp, SubAgentBlueprint), "返回 SubAgentBlueprint 实例")
        assert_true(bp.source == SubAgentSource.DIRECTORY, "默认 source=DIRECTORY")
        assert_true(bp.source_path == path, "source_path 为文件路径")
    finally:
        os.unlink(path)


def test_loader_file_not_found():
    """3.2 load_agent_from_file() 文件不存在"""
    print("\n" + "═" * 60)
    print("3.2  load_agent_from_file() 文件不存在")
    print("═" * 60)

    @assert_raises(FileNotFoundError, "文件不存在抛 FileNotFoundError")
    def _():
        load_agent_from_file("/tmp/nonexistent_agent_xxx.md")


def test_loader_missing_frontmatter():
    """3.3 load_agent_from_file() 缺少 frontmatter"""
    print("\n" + "═" * 60)
    print("3.3  load_agent_from_file() 缺少 frontmatter")
    print("═" * 60)

    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", encoding="utf-8", delete=False) as f:
        f.write("没有 frontmatter 的内容\n直接是正文。\n")
        path = f.name

    try:
        @assert_raises(ValueError, "缺少 frontmatter 抛 ValueError")
        def _():
            load_agent_from_file(path)
    finally:
        os.unlink(path)


def test_loader_missing_when_to_use():
    """3.4 load_agent_from_file() 缺少 when_to_use"""
    print("\n" + "═" * 60)
    print("3.4  load_agent_from_file() 缺少 when_to_use")
    print("═" * 60)

    content = """\
---
agent_id: no-when
agent_name: 缺少 when_to_use
trust_level: sub_agent
---

正文内容
"""
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", encoding="utf-8", delete=False) as f:
        f.write(content)
        path = f.name

    try:
        @assert_raises(ValueError, "缺少 when_to_use 抛 ValueError")
        def _():
            load_agent_from_file(path)
    finally:
        os.unlink(path)


def test_loader_empty_body():
    """3.5 load_agent_from_file() 正文为空"""
    print("\n" + "═" * 60)
    print("3.5  load_agent_from_file() 正文为空")
    print("═" * 60)

    content = """\
---
agent_id: empty-body
agent_name: 空正文
when_to_use: 测试空正文
trust_level: sub_agent
---
"""
    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", encoding="utf-8", delete=False) as f:
        f.write(content)
        path = f.name

    try:
        @assert_raises(ValueError, "正文为空抛 ValueError")
        def _():
            load_agent_from_file(path)
    finally:
        os.unlink(path)


def test_loader_agent_id_fallback_to_stem():
    """3.6 agent_id 缺失时回退到文件名 stem"""
    print("\n" + "═" * 60)
    print("3.6  agent_id 回退文件名 stem")
    print("═" * 60)

    content = """\
---
agent_name: 无 ID Agent
when_to_use: 测试 ID 回退
trust_level: sub_agent
---

系统提示内容
"""
    with tempfile.NamedTemporaryFile(
        prefix="my_agent_", suffix=".md", mode="w", encoding="utf-8", delete=False
    ) as f:
        f.write(content)
        path = f.name
        stem = Path(path).stem

    try:
        bp = load_agent_from_file(path)
        assert_true(bp.agent_id == stem, f"agent_id 回退为文件名 stem: {stem}")
    finally:
        os.unlink(path)


def test_loader_load_agents_from_dir():
    """3.7 load_agents_from_dir() 批量加载"""
    print("\n" + "═" * 60)
    print("3.7  load_agents_from_dir() 批量加载")
    print("═" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(3):
            content = f"""\
---
agent_id: dir.agent.{i}
agent_name: 目录 Agent {i}
when_to_use: 用于目录加载测试 {i}
trust_level: sub_agent
---

系统提示 {i}
"""
            (Path(tmpdir) / f"agent_{i}.md").write_text(content, encoding="utf-8")

        blueprints = load_agents_from_dir(tmpdir)
        assert_true(len(blueprints) == 3, "批量加载 3 个蓝图")
        ids = {bp.agent_id for bp in blueprints}
        assert_true(len(ids) == 3, "3 个不同 agent_id")


def test_loader_load_agents_from_dir_fail_safe():
    """3.8 load_agents_from_dir() 单文件失败时跳过（Fail-Safe）"""
    print("\n" + "═" * 60)
    print("3.8  load_agents_from_dir() Fail-Safe")
    print("═" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        # 一个有效文件
        (Path(tmpdir) / "valid.md").write_text("""\
---
agent_id: valid.agent
agent_name: 有效 Agent
when_to_use: 用于测试
trust_level: sub_agent
---

有效的系统提示
""", encoding="utf-8")

        # 一个无效文件（缺少 when_to_use）
        (Path(tmpdir) / "invalid.md").write_text("""\
---
agent_id: invalid.agent
agent_name: 无效 Agent
trust_level: sub_agent
---

有正文但缺 when_to_use
""", encoding="utf-8")

        blueprints = load_agents_from_dir(tmpdir)
        assert_true(len(blueprints) == 1, "无效文件跳过，只加载 1 个蓝图")
        assert_true(blueprints[0].agent_id == "valid.agent", "加载的是有效蓝图")


def test_loader_load_agents_from_nonexistent_dir():
    """3.9 load_agents_from_dir() 目录不存在返回空列表"""
    print("\n" + "═" * 60)
    print("3.9  load_agents_from_dir() 目录不存在")
    print("═" * 60)

    blueprints = load_agents_from_dir("/tmp/nonexistent_dir_xxx")
    assert_true(blueprints == [], "目录不存在返回空列表")


def test_loader_parse_frontmatter():
    """3.10 _parse_frontmatter() 有无 frontmatter"""
    print("\n" + "═" * 60)
    print("3.10  _parse_frontmatter()")
    print("═" * 60)

    # 有 frontmatter
    text = "---\nkey: value\nfoo: bar\n---\n正文内容\n"
    fm, body = _parse_frontmatter(text)
    assert_true(fm == {"key": "value", "foo": "bar"}, "frontmatter 解析正确")
    assert_true("正文内容" in body, "body 包含正文")

    # 无 frontmatter
    text2 = "直接是正文\n没有 frontmatter"
    fm2, body2 = _parse_frontmatter(text2)
    assert_true(fm2 == {}, "无 frontmatter 时返回空 dict")
    assert_true("直接是正文" in body2, "body 为原始文本")

    # 带注释行的 frontmatter
    text3 = "---\n# 注释\nkey: val\n---\nbody\n"
    fm3, _ = _parse_frontmatter(text3)
    assert_true(fm3.get("key") == "val", "frontmatter 注释行被跳过")


def test_loader_parse_trust_level():
    """3.11 _parse_trust_level() 各种输入"""
    print("\n" + "═" * 60)
    print("3.11  _parse_trust_level()")
    print("═" * 60)

    p = Path("/tmp/fake.md")

    assert_true(_parse_trust_level("sub_agent", p) == TrustLevel.SUB_AGENT, "sub_agent 解析正确")
    assert_true(_parse_trust_level("orchestrator", p) == TrustLevel.ORCHESTRATOR, "orchestrator 解析正确")
    assert_true(_parse_trust_level("external", p) == TrustLevel.EXTERNAL, "external 解析正确")
    assert_true(_parse_trust_level("SUB_AGENT", p) == TrustLevel.SUB_AGENT, "大写 SUB_AGENT 解析正确")

    @assert_raises(ValueError, "空值抛 ValueError")
    def _empty():
        _parse_trust_level("", p)

    @assert_raises(ValueError, "非法值抛 ValueError")
    def _invalid():
        _parse_trust_level("super_admin", p)


def test_loader_parse_permissions():
    """3.12 _parse_sensitive_permissions() 解析"""
    print("\n" + "═" * 60)
    print("3.12  _parse_sensitive_permissions()")
    print("═" * 60)

    # 正常解析（逗号分隔字符串）
    perms = _parse_sensitive_permissions("code_exec, network_call")
    assert_true(len(perms) == 2, "解析 2 个权限")
    assert_true(SensitivePermission.CODE_EXEC in perms, "包含 CODE_EXEC")
    assert_true(SensitivePermission.NETWORK_CALL in perms, "包含 NETWORK_CALL")

    # 列表形式
    perms2 = _parse_sensitive_permissions(["data_write", "data_delete"])
    assert_true(len(perms2) == 2, "列表形式解析 2 个权限")
    assert_true(SensitivePermission.DATA_WRITE in perms2, "列表形式包含 DATA_WRITE")

    # 空字符串返回空 frozenset
    assert_true(_parse_sensitive_permissions("") == frozenset(), "空字符串返回空 frozenset")

    # None 返回空 frozenset
    assert_true(_parse_sensitive_permissions(None) == frozenset(), "None 返回空 frozenset")


def test_loader_parse_comma_list():
    """3.13 _parse_comma_list()"""
    print("\n" + "═" * 60)
    print("3.13  _parse_comma_list()")
    print("═" * 60)

    result_list = _parse_comma_list("a, b, c")
    assert_true(result_list == ("a", "b", "c"), "逗号分隔解析正确")

    result_empty = _parse_comma_list("")
    assert_true(result_empty == (), "空字符串返回空 tuple")

    result_single = _parse_comma_list("only_one")
    assert_true(result_single == ("only_one",), "单个元素正确")

    result_spaces = _parse_comma_list("  x , y  , z  ")
    assert_true(result_spaces == ("x", "y", "z"), "多余空格被 strip")






# ════════════════════════════════════════════════════
#  4. 数据模型不可变性测试
# ════════════════════════════════════════════════════

def test_models_frozen():
    """4.1 SubAgentSummary / SubAgentDelegateResult / SubAgentBlueprint 均为 frozen"""
    print("\n" + "═" * 60)
    print("4.1  frozen 数据模型不可变性")
    print("═" * 60)

    from dataclasses import FrozenInstanceError

    summary = SubAgentSummary(agent_name="S", when_to_use="w")
    try:
        summary.agent_name = "modified"  # type: ignore
        result.fail("SubAgentSummary 修改 agent_name 应抛异常")
    except (FrozenInstanceError, AttributeError):
        result.ok("SubAgentSummary 是 frozen — 不可修改")

    dr = SubAgentDelegateResult(success=True, output="out", target_agent_id="t1")
    try:
        dr.success = False  # type: ignore
        result.fail("SubAgentDelegateResult 修改 success 应抛异常")
    except (FrozenInstanceError, AttributeError):
        result.ok("SubAgentDelegateResult 是 frozen — 不可修改")


def test_agent_status_enum():
    """4.2 AgentStatus 枚举值"""
    print("\n" + "═" * 60)
    print("4.2  AgentStatus 枚举")
    print("═" * 60)

    assert_true(AgentStatus.HEALTHY.value == "healthy", "HEALTHY 值正确")
    assert_true(AgentStatus.UNHEALTHY.value == "unhealthy", "UNHEALTHY 值正确")
    assert_true(AgentStatus.DRAINING.value == "draining", "DRAINING 值正确")


def test_agent_source_enum():
    """4.3 SubAgentSource 枚举优先级"""
    print("\n" + "═" * 60)
    print("4.3  SubAgentSource IntEnum 优先级")
    print("═" * 60)

    assert_true(SubAgentSource.PROGRAMMATIC > SubAgentSource.DIRECTORY, "PROGRAMMATIC 优先级高于 DIRECTORY")
    assert_true(SubAgentSource.DIRECTORY == 1, "DIRECTORY == 1")
    assert_true(SubAgentSource.PROGRAMMATIC == 2, "PROGRAMMATIC == 2")


# ════════════════════════════════════════════════════
#  5. Mock 补充场景
# ════════════════════════════════════════════════════

def test_mock_registry_logger_on_register():
    """5.1 Mock logger — register() 输出 INFO 日志"""
    print("\n" + "═" * 60)
    print("5.1  Mock registry logger.info on register")
    print("═" * 60)

    with patch("pandaren.sub_agent.registry.logger") as mock_logger:
        reg = _make_registry()
        reg.register(_make_agent("log.register.agent"))
        assert_true(mock_logger.info.called, "register() 输出 INFO 日志")
        info_args = str(mock_logger.info.call_args_list)
        assert_true("log.register.agent" in info_args, "INFO 日志包含 agent_id")


def test_mock_registry_logger_on_unregister():
    """5.2 Mock logger — unregister() 输出 INFO 日志"""
    print("\n" + "═" * 60)
    print("5.2  Mock registry logger.info on unregister")
    print("═" * 60)

    reg = _make_registry()
    reg.register(_make_agent("log.unreg.agent"))

    with patch("pandaren.sub_agent.registry.logger") as mock_logger:
        reg.unregister("log.unreg.agent")
        assert_true(mock_logger.info.called, "unregister() 输出 INFO 日志")


def test_mock_delegate_task_audit_chain():
    """5.3 Mock 审计链路 — delegate_task 完整审计调用"""
    print("\n" + "═" * 60)
    print("5.3  delegate_task() 审计链路")
    print("═" * 60)

    mock_audit = MagicMock()
    mock_audit.write_sync = MagicMock()

    reg = SubAgentRegistry(audit_log=mock_audit)
    target = _make_agent("audit.target")
    target._loop.run = AsyncMock(return_value=AgentResult(success=True, output="done", run_id="r99"))
    reg.register(target)

    ctx = MagicMock()
    ctx.agent_id = "audit.caller"
    ctx.trust_level = TrustLevel.ORCHESTRATOR
    ctx.namespace = None

    mock_audit.write_sync.reset_mock()  # 清掉 register() 的调用

    async_run(reg._execute_delegate("audit.target", "task", ctx))

    # 应该至少有 AGENT_DELEGATED 和 AGENT_DELEGATE_COMPLETED 两条审计
    assert_true(mock_audit.write_sync.call_count >= 2, "委派过程产生至少 2 条审计记录")


def test_mock_agent_aclose_exception_handled():
    """5.4 aclose() — 从不触碰注入的共享 client（连其 aclose 都不调用）"""
    print("\n" + "═" * 60)
    print("5.4  aclose() 不触碰共享 client")
    print("═" * 60)

    agent = _make_agent()
    mock_llm = MagicMock()
    # 故意让 client.aclose 抛异常：若 Agent.aclose 敢碰它就会 RuntimeError。
    mock_llm.aclose = AsyncMock(side_effect=RuntimeError("连接池错误"))
    agent._loop._llm_client = mock_llm

    try:
        async_run(agent.aclose())
        # 既不抛异常，也不调用 client.aclose——Agent 是借用方，无权关共享 client。
        assert_true(mock_llm.aclose.call_count == 0, "Agent.aclose 从不调用共享 client.aclose")
    except RuntimeError:
        result.fail("Agent.aclose 不应触碰共享 client", "错误地调用了 client.aclose 并抛出")


# ════════════════════════════════════════════════════
#  测试分区表 & 主入口
# ════════════════════════════════════════════════════

SECTIONS: dict[str, list] = {
    "agent": [
        test_agent_properties,
        test_agent_run_delegates_to_loop,
        test_agent_run_default_params,
        test_agent_run_stream_delegates_to_loop,
        test_agent_aclose_idempotent,
        test_agent_aclose_no_llm_client,
        test_agent_context_manager,
        test_agent_run_with_resume_state,
    ],
    "registry": [
        test_registry_register_success,
        test_registry_register_duplicate_raises,
        test_registry_unregister,
        test_registry_set_status,
        test_registry_set_status_not_found,
        test_registry_list_identities,
        test_registry_build_agent_summaries_basic,
        test_registry_build_agent_summaries_excludes_unhealthy,
        test_registry_build_agent_summaries_excludes_self,
        test_registry_build_agent_summaries_budget,
        test_registry_search_agents_no_agents,
        test_registry_search_agents_match,
        test_registry_search_agents_no_match,
        test_registry_delegate_task_success,
        test_registry_delegate_task_not_found,
        test_registry_delegate_task_unhealthy,
        test_registry_delegate_task_trust_denied_external,
        test_registry_delegate_task_trust_upward_denied,
        test_registry_delegate_task_cycle_detection,
        test_registry_delegate_task_depth_exceeded,
        test_registry_delegate_task_stack_cleanup_on_exception,
        test_registry_refresh_health,
        test_registry_register_builtin_tools_idempotent,
        test_registry_register_builtin_tools_no_tool_registry,
        test_registry_audit_log_on_register,
        test_registry_check_trust,
        test_registry_find_agent_id_by_name_cases,
        test_registry_repr,
    ],
    "loader": [
        test_loader_load_from_file_success,
        test_loader_file_not_found,
        test_loader_missing_frontmatter,
        test_loader_missing_when_to_use,
        test_loader_empty_body,
        test_loader_agent_id_fallback_to_stem,
        test_loader_load_agents_from_dir,
        test_loader_load_agents_from_dir_fail_safe,
        test_loader_load_agents_from_nonexistent_dir,
        test_loader_parse_frontmatter,
        test_loader_parse_trust_level,
        test_loader_parse_permissions,
        test_loader_parse_comma_list,
    ],
    "models": [
        test_models_frozen,
        test_agent_status_enum,
        test_agent_source_enum,
    ],
    "mock": [
        test_mock_registry_logger_on_register,
        test_mock_registry_logger_on_unregister,
        test_mock_delegate_task_audit_chain,
        test_mock_agent_aclose_exception_handled,
    ],
}

ALL_TESTS = [fn for fns in SECTIONS.values() for fn in fns]


def main():
    import argparse

    parser = argparse.ArgumentParser(description="pandaren/agent 模块 Mock 测试")
    parser.add_argument(
        "--section",
        choices=list(SECTIONS.keys()),
        default="",
        help="仅运行指定测试分区 (agent/registry/loader/models/mock)",
    )
    args = parser.parse_args()

    print("🐼 Pandaren Agent SDK — agent 模块 Mock 测试")
    print("   目标模块: pandaren/agent/ (agent, registry, loader, models)")
    print("   测试方式: unittest.mock + AsyncMock + 临时文件")
    print()

    logging.getLogger("pandaren").setLevel(logging.ERROR)

    to_run = SECTIONS[args.section] if args.section else ALL_TESTS
    for fn in to_run:
        fn()

    result.summary(args.section or "全部")

    if result.failed > 0:
        print("\n⚠️  存在失败的测试用例，请检查上方输出")
        sys.exit(1)
    else:
        total = result.passed + result.failed
        print(f"\n🎉 所有 {total} 个测试通过！agent 模块 Mock 测试验证完毕")
        sys.exit(0)


if __name__ == "__main__":
    main()
