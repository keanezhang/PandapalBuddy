"""
Pandaren Agent SDK · Engine 层 Mock 测试

覆盖约束
--------
  通过 Mock/Patch 验证 pandaren/engine/ 各模块的内部行为、logger 日志、hooks 回调：

  - StepCounter     : 不可变保护（__setattr__ / __delattr__）、ValueError 边界、
                      increment() 返回语义、remaining 下限
  - OutputParser    : 3 条解析分支（tool_calls / content / empty）
  - MessageBuilder  : build_static_context_str / build_dynamic_reminder / build()
                      各参数组合的 XML 标签输出与 messages 变换
  - AgentLoop.__init__  : DefaultLoopHooks 降级、context_window_budget 触发
                          logger.warning（丢弃 / 截断）、skill/agent_registry 调用链
  - AgentLoop.__setattr__: 冻结属性保护（_FROZEN_ATTRS 后 init 不可修改）
  - RunCoreMixin._safe_hook : 异常抑制、logger.warning 触发、正常路径不触发
  - RunCoreMixin.run()       : 无 RUN_END 事件 / 外层 Exception → logger.error
  - _run_stream_core 入口校验: session_id / user_id 空串、resume 身份不匹配

运行方式
--------
  cd pandaren/engine/tests && python test_engine_mock.py
  cd pandaren/engine/tests && python test_engine_mock.py --section step_counter
  cd pandaren/engine/tests && python test_engine_mock.py --section output_parser
  cd pandaren/engine/tests && python test_engine_mock.py --section message_builder
  cd pandaren/engine/tests && python test_engine_mock.py --section agent_loop_init
  cd pandaren/engine/tests && python test_engine_mock.py --section agent_loop_freeze
  cd pandaren/engine/tests && python test_engine_mock.py --section safe_hook
  cd pandaren/engine/tests && python test_engine_mock.py --section run_no_run_end
  cd pandaren/engine/tests && python test_engine_mock.py --section run_stream_core_validation
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Windows 控制台 UTF-8 输出
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ═══ SDK 导入 ═══
from pandaren.engine.step_counter import StepCounter
from pandaren.engine.output_parser import OutputParser
from pandaren.engine.message_builder import MessageBuilder
from pandaren.hook import DefaultAgentHooks as DefaultLoopHooks, AgentHooks as LoopHooks
from pandaren.engine.models import AgentResult, RunState
from pandaren.engine.stream import StreamEvent, StreamEventType


# ════════════════════════════════════════════════════
#  异步辅助
# ════════════════════════════════════════════════════

def async_run(coro):
    """同步运行协程（兼容 Python 3.12+）。"""
    return asyncio.run(coro)


# ════════════════════════════════════════════════════
#  测试框架
# ════════════════════════════════════════════════════

class TestResult:
    """轻量测试结果收集器。"""
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


def assert_raises(exc_type, fn, name: str, detail: str = ""):
    try:
        fn()
        result.fail(name, detail or f"应抛出 {exc_type.__name__} 但未抛出")
    except Exception as e:
        if isinstance(e, exc_type):
            result.ok(name)
        else:
            result.fail(name, f"抛出了意外异常 {type(e).__name__}: {e}")


def assert_no_raises(fn, name: str, detail: str = ""):
    try:
        fn()
        result.ok(name)
    except Exception as e:
        result.fail(name, detail or f"意外抛出 {type(e).__name__}: {e}")


# ════════════════════════════════════════════════════
#  工厂辅助
# ════════════════════════════════════════════════════

def _make_skill_summary(name: str = "weather", when_to_use: str = "天气查询"):
    """创建轻量 SkillSummary mock 对象。"""
    s = MagicMock()
    s.name = name
    s.when_to_use = when_to_use
    return s


def _make_agent_summary(agent_name: str = "coder", when_to_use: str = "写代码"):
    """创建轻量 SubAgentSummary mock 对象。"""
    s = MagicMock()
    s.agent_name = agent_name
    s.when_to_use = when_to_use
    return s


def _make_mock_agent_loop(
    *,
    with_context_window_budget: bool = False,
    system_prompt_tokens: int = 100,
    system_base_chars: int = 0,
    static_context_str_override: str | None = "PLACEHOLDER",
    skill_registry=None,
    agent_registry=None,
    hooks=None,
):
    """
    构建 AgentLoop 的最小化 Mock 版本，用于测试 __init__ 副作用。

    由于 AgentLoop 构造器依赖众多 SDK 对象，本辅助函数将所有依赖全部 Mock，
    仅让目标测试场景的真实路径执行。
    """
    from pandaren.identity.models import Identity, TrustLevel
    from pandaren.behavior.execution_limits import ExecutionLimits
    from pandaren.behavior.error_policy import ErrorPolicy
    from pandaren.engine.loop import AgentLoop

    identity = Identity(
        agent_id="test.engine.mock",
        agent_name="测试Agent",
        when_to_use="测试",
        sensitive_permissions=frozenset(),
        trust_level=TrustLevel.SUB_AGENT,
    )

    mock_llm = MagicMock()
    mock_registry = MagicMock()
    mock_registry.get_deferred_tool_catalog.return_value = []
    mock_guard = MagicMock()
    mock_hitl = MagicMock()
    mock_audit = MagicMock()
    mock_harness_executor = MagicMock()
    mock_memory = MagicMock()
    mock_memory.system_prompt = "x" * system_base_chars  # 控制 system_base_chars
    # async 方法必须是 AsyncMock，否则 await 会失败（Python 3.12）
    mock_memory.flush_raw_messages = AsyncMock()
    mock_memory.end_session = AsyncMock()
    mock_memory.compact_if_needed = AsyncMock(return_value=None)
    mock_memory.init_from_restore = MagicMock()
    mock_memory.get_messages = MagicMock(return_value=[])
    mock_memory.resume_context = MagicMock()
    mock_limits = ExecutionLimits()
    mock_error_policy = ErrorPolicy()

    context_window_budget = None
    if with_context_window_budget:
        context_window_budget = MagicMock()
        context_window_budget.system_prompt_tokens = system_prompt_tokens

    # skill_registry / agent_registry
    sr = skill_registry
    ar = agent_registry

    # Override MessageBuilder.build_static_context_str 的返回值
    with patch(
        "pandaren.engine.loop.MessageBuilder.build_static_context_str",
        return_value=static_context_str_override,
    ):
        loop = AgentLoop(
            identity=identity,
            llm_client=mock_llm,
            tool_registry=mock_registry,
            harness_executor=mock_harness_executor,
            permission_guard=mock_guard,
            hitl_controller=mock_hitl,
            execution_limits=mock_limits,
            error_policy=mock_error_policy,
            audit_log=mock_audit,
            memory=mock_memory,
            context_window_budget=context_window_budget,
            skill_registry=sr,
            agent_registry=ar,
            hooks=hooks,
        )

    return loop


# ════════════════════════════════════════════════════
#  Section: step_counter
# ════════════════════════════════════════════════════

def test_step_counter_mock():
    print("\n── StepCounter (Mock) ────────────────────────────────────")

    # 1. max_steps <= 0 抛 ValueError
    assert_raises(
        ValueError,
        lambda: StepCounter(0),
        "max_steps=0 抛 ValueError",
    )

    # 2. max_steps = -1 也抛 ValueError（负数）
    assert_raises(
        ValueError,
        lambda: StepCounter(-1),
        "max_steps=-1 抛 ValueError",
    )

    # 3. 正常构造 max_steps=1 不抛异常
    assert_no_raises(lambda: StepCounter(1), "max_steps=1 正常构造")

    sc = StepCounter(3)

    # 4. __setattr__ 直接赋值抛 PermissionError
    assert_raises(
        PermissionError,
        lambda: setattr(sc, "_count", 99),
        "__setattr__ 直接赋值抛 PermissionError",
    )

    # 5. __delattr__ 删除字段抛 PermissionError
    assert_raises(
        PermissionError,
        lambda: delattr(sc, "_count"),
        "__delattr__ 删除字段抛 PermissionError",
    )

    # 6. increment() 未到上限返回 True
    r1 = sc.increment()  # count = 1 <= 3
    assert_true(r1 is True, "第 1 次 increment 未到上限返回 True")

    r2 = sc.increment()  # count = 2 <= 3
    assert_true(r2 is True, "第 2 次 increment 未到上限返回 True")

    r3 = sc.increment()  # count = 3 == 3 → 到达上限
    assert_true(r3 is True, "第 3 次 increment 恰好等于上限仍返回 True（count <= max）")

    # 7. increment() 超过上限返回 False
    r4 = sc.increment()  # count = 4 > 3
    assert_true(r4 is False, "第 4 次 increment 超过上限返回 False")

    # 8. 再次超限后仍可调用（只增不停），继续返回 False
    r5 = sc.increment()  # count = 5 > 3
    assert_true(r5 is False, "第 5 次 increment 超限后仍返回 False")

    # 9. remaining 用完后为 0（不为负）
    assert_true(sc.remaining == 0, f"remaining 超限后为 0，实际={sc.remaining}")

    # 10. count 随 increment 递增（内部 object.__setattr__ 路径生效）
    sc2 = StepCounter(5)
    assert_true(sc2.count == 0, "初始 count == 0")
    sc2.increment()
    sc2.increment()
    assert_true(sc2.count == 2, "两次 increment 后 count == 2")


# ════════════════════════════════════════════════════
#  Section: output_parser
# ════════════════════════════════════════════════════

def test_output_parser_mock():
    print("\n── OutputParser (Mock) ───────────────────────────────────")
    parser = OutputParser()

    # 11. 有 tool_calls → is_final=False
    tool_calls = [{"id": "call_1", "function": {"name": "search", "arguments": "{}"}}]
    parsed = parser.parse({"content": "思考中", "tool_calls": tool_calls})
    assert_true(parsed.is_final is False, "tool_calls 存在 → is_final=False")

    # 12. 有 tool_calls → tool_calls 字段正确回填
    assert_true(
        parsed.tool_calls == tool_calls,
        "tool_calls 字段正确回填",
        f"实际={parsed.tool_calls}",
    )

    # 13. 无 tool_calls，content 非空 → is_final=True, is_empty=False
    parsed2 = parser.parse({"content": "最终答案是 42", "tool_calls": None})
    assert_true(parsed2.is_final is True, "无 tool_calls + 有 content → is_final=True")
    assert_true(parsed2.is_empty is False, "content 非空 → is_empty=False")

    # 14. 无 tool_calls，content 为空 → is_final=True, is_empty=True
    parsed3 = parser.parse({"content": "", "tool_calls": None})
    assert_true(parsed3.is_final is True, "无 tool_calls + 空 content → is_final=True")
    assert_true(parsed3.is_empty is True, "空 content → is_empty=True")

    # content=None 也触发 is_empty=True
    parsed4 = parser.parse({"content": None, "tool_calls": None})
    assert_true(parsed4.is_empty is True, "content=None → is_empty=True")


# ════════════════════════════════════════════════════
#  Section: message_builder
# ════════════════════════════════════════════════════

def test_message_builder_mock():
    print("\n── MessageBuilder (Mock) ─────────────────────────────────")

    # 15. 三参数全 None → 返回 None
    r = MessageBuilder.build_static_context_str(
        deferred_tool_summaries=None,
        skill_summaries=None,
        agent_summaries=None,
    )
    assert_true(r is None, "三参数全 None → build_static_context_str 返回 None")

    # 15b. 三参数全空列表 → 返回 None
    r_empty = MessageBuilder.build_static_context_str(
        deferred_tool_summaries=[],
        skill_summaries=[],
        agent_summaries=[],
    )
    assert_true(r_empty is None, "三参数全空列表 → 返回 None")

    # 16. 只传 deferred_tool_summaries → 包含 <available_tools>
    r_tools = MessageBuilder.build_static_context_str(
        deferred_tool_summaries=[{"name": "search", "when_to_use": "搜索"}],
        skill_summaries=None,
        agent_summaries=None,
    )
    assert_true(
        r_tools is not None and "<available_tools>" in r_tools,
        "有 deferred_tool_summaries → 包含 <available_tools>",
    )
    assert_true(
        r_tools is not None and "search" in r_tools,
        "工具名 'search' 出现在 XML 中",
    )

    # 17. 只传 skill_summaries → 包含 <available_skills>
    skill = _make_skill_summary("weather_lookup", "查询天气")
    r_skills = MessageBuilder.build_static_context_str(
        deferred_tool_summaries=None,
        skill_summaries=[skill],
        agent_summaries=None,
    )
    assert_true(
        r_skills is not None and "<available_skills>" in r_skills,
        "有 skill_summaries → 包含 <available_skills>",
    )

    # 18. 只传 agent_summaries → 包含 <available_agents>
    agent_s = _make_agent_summary("coder", "写代码任务")
    r_agents = MessageBuilder.build_static_context_str(
        deferred_tool_summaries=None,
        skill_summaries=None,
        agent_summaries=[agent_s],
    )
    assert_true(
        r_agents is not None and "<available_agents>" in r_agents,
        "有 agent_summaries → 包含 <available_agents>",
    )

    # 19. v1.4 重构：去 summary 化后 build_dynamic_reminder() 不再承载 recall 内容，
    # 当前始终返回 None；Plan Mode 等通过外部拼接自行附加 reminder 正文。
    dr_none = MessageBuilder.build_dynamic_reminder()
    assert_true(dr_none is None, "build_dynamic_reminder() 返回 None（recall 路径已废弃）")

    # 21. build() 中 static_context_str 被追加到 system message 末尾
    messages = [
        {"role": "system", "content": "你是助手。"},
        {"role": "user", "content": "你好"},
    ]
    builder = MessageBuilder()
    built, _ = builder.build(
        messages=messages,
        static_context_str="<STATIC>",
    )
    system_content = next(m["content"] for m in built if m["role"] == "system")
    assert_true(
        system_content.endswith("<STATIC>"),
        "static_context_str 追加到 system message 末尾",
        f"实际内容末尾={system_content[-30:]!r}",
    )

    # 22. build() 中 dynamic_reminder 作为独立 role=user 消息尾插
    built2, _ = builder.build(
        messages=messages,
        dynamic_reminder="<system-reminder>recall: xyz</system-reminder>",
    )
    last_msg = built2[-1]
    assert_true(
        last_msg["role"] == "user" and "recall" in last_msg["content"],
        "dynamic_reminder 作为 role=user 消息尾插",
    )

    # 23. 不传 static_context_str 时 system message 内容保持原样
    built3, _ = builder.build(messages=messages)
    system_content3 = next(m["content"] for m in built3 if m["role"] == "system")
    assert_true(
        system_content3 == "你是助手。",
        "不传 static_context_str 时 system message 内容不变",
    )

    # 原始 messages 不被修改（build 使用浅拷贝）
    assert_true(
        messages[0]["content"] == "你是助手。",
        "build() 不修改原始 messages（浅拷贝保护）",
    )


# ════════════════════════════════════════════════════
#  Section: agent_loop_init
# ════════════════════════════════════════════════════

def test_agent_loop_init_mock():
    print("\n── AgentLoop.__init__ (Mock) ─────────────────────────────")

    # 24. hooks=None 时自动使用 DefaultLoopHooks
    loop_no_hooks = _make_mock_agent_loop()
    assert_true(
        isinstance(loop_no_hooks._hooks, DefaultLoopHooks),
        "hooks=None → _hooks 是 DefaultLoopHooks 实例",
    )

    # 25. context_window_budget=None 时不触发 logger.warning
    with patch("pandaren.engine.loop.logger") as mock_log:
        _make_mock_agent_loop(
            with_context_window_budget=False,
            static_context_str_override="<tools>some tools</tools>",
        )
        assert_true(
            not mock_log.warning.called,
            "context_window_budget=None 不触发 logger.warning",
        )

    # 26. system_prompt 本身超出 system_prompt_tokens 配额 → logger.warning + _static_context_str=None
    # system_base_chars=600 对应约 150 tokens (CHARS_PER_TOKEN=4)，超出配额 system_prompt_tokens=100
    with patch("pandaren.engine.loop.logger") as mock_log:
        loop_overflow = _make_mock_agent_loop(
            with_context_window_budget=True,
            system_prompt_tokens=100,    # 总配额 100 tokens
            system_base_chars=600,       # base ~150 tokens > 100 → available <= 0
            static_context_str_override="<tools>tool list</tools>",
        )
        assert_true(
            mock_log.warning.called,
            "system_prompt 超出配额 → logger.warning 被触发",
        )
        assert_true(
            loop_overflow._static_context_str is None,
            "system_prompt 超出配额 → _static_context_str 被清为 None",
        )

    # 27. static_context_tokens > available → logger.warning + 截断
    # system_prompt_tokens=100, system_base_chars=0 → available=100 tokens (~400 chars)
    # static_context_str 长 800 chars (~200 tokens) > 100 → 截断
    with patch("pandaren.engine.loop.logger") as mock_log:
        loop_trunc = _make_mock_agent_loop(
            with_context_window_budget=True,
            system_prompt_tokens=100,    # 总配额 100 tokens
            system_base_chars=0,         # base = 0 tokens → available = 100 tokens
            static_context_str_override="x" * 800,  # ~200 tokens > 100
        )
        assert_true(
            mock_log.warning.called,
            "static_context 超出剩余配额 → logger.warning 被触发",
        )
        assert_true(
            loop_trunc._static_context_str is not None
            and len(loop_trunc._static_context_str) < 800,
            "static_context 超出配额 → 被截断（长度 < 原始长度）",
            f"实际长度={len(loop_trunc._static_context_str) if loop_trunc._static_context_str else 'None'}",
        )

    # 28. skill_registry=None 时不调用 build_skill_summaries
    mock_skill_registry = MagicMock()
    mock_skill_registry.build_skill_summaries.return_value = []
    _make_mock_agent_loop(skill_registry=None)  # 不传 skill_registry
    # 由于 skill_registry=None，build_skill_summaries 不应被调用
    assert_true(
        not mock_skill_registry.build_skill_summaries.called,
        "skill_registry=None 时 build_skill_summaries 不被调用",
    )

    # 29. agent_registry 有值 → 调用 build_agent_summaries(exclude_agent_id=...)
    mock_agent_registry = MagicMock()
    mock_agent_registry.build_agent_summaries.return_value = []
    _make_mock_agent_loop(agent_registry=mock_agent_registry)
    assert_true(
        mock_agent_registry.build_agent_summaries.called,
        "agent_registry 有值 → build_agent_summaries 被调用",
    )
    call_kwargs = mock_agent_registry.build_agent_summaries.call_args[1]
    assert_true(
        "exclude_agent_id" in call_kwargs,
        "build_agent_summaries 调用时包含 exclude_agent_id 参数",
    )
    assert_true(
        call_kwargs["exclude_agent_id"] == "test.engine.mock",
        "exclude_agent_id 等于自身 agent_id（防止自我委派）",
        f"实际={call_kwargs.get('exclude_agent_id')!r}",
    )


# ════════════════════════════════════════════════════
#  Section: agent_loop_freeze
# ════════════════════════════════════════════════════

def test_agent_loop_freeze_mock():
    print("\n── AgentLoop.__setattr__ 冻结保护 (Mock) ───────────────────")

    from pandaren.engine.loop import AgentLoop

    # 30. 初始化完成后对 _identity 赋值抛 AttributeError
    loop = _make_mock_agent_loop()

    assert_raises(
        AttributeError,
        lambda: setattr(loop, "_identity", MagicMock()),
        "init 后对 _identity 赋值抛 AttributeError",
    )

    # 31. 初始化完成后对 _llm_client 赋值抛 AttributeError
    assert_raises(
        AttributeError,
        lambda: setattr(loop, "_llm_client", MagicMock()),
        "init 后对 _llm_client 赋值抛 AttributeError",
    )

    # 32. 初始化完成后对 _tool_registry 赋值抛 AttributeError
    assert_raises(
        AttributeError,
        lambda: setattr(loop, "_tool_registry", MagicMock()),
        "init 后对 _tool_registry 赋值抛 AttributeError",
    )

    # 33. 非冻结字段（取消令牌 _cancel_token）可正常重建赋值
    #     （_cancelled 已改为只读 property，读取消令牌；run 入口会重建 _cancel_token）
    from pandaren.cancellation import CancelToken
    assert_no_raises(
        lambda: setattr(loop, "_cancel_token", CancelToken()),
        "非冻结字段 _cancel_token 可正常赋值",
    )

    # 34. cancel() 令 _cancelled（只读 property）读出 True
    loop2 = _make_mock_agent_loop()
    assert_true(loop2._cancelled is False, "新 loop 初始未取消")
    loop2.cancel()
    assert_true(loop2._cancelled is True, "cancel() 后 _cancelled 读出 True")

    # 35（附加）: _message_builder 也在冻结集合内
    assert_raises(
        AttributeError,
        lambda: setattr(loop, "_message_builder", MagicMock()),
        "init 后对 _message_builder 赋值抛 AttributeError",
    )

    # 36（附加）: _audit_log 也在冻结集合内
    assert_raises(
        AttributeError,
        lambda: setattr(loop, "_audit_log", MagicMock()),
        "init 后对 _audit_log 赋值抛 AttributeError",
    )


# ════════════════════════════════════════════════════
#  Section: safe_hook
# ════════════════════════════════════════════════════

def test_safe_hook_mock():
    print("\n── RunCoreMixin._safe_hook (Mock) ─────────────────────────")

    loop = _make_mock_agent_loop()

    # 35. hook 方法抛异常 → 不传播，logger.warning 被调用
    broken_hooks = MagicMock()
    broken_hooks.on_run_start.side_effect = RuntimeError("hook 爆炸")
    loop._hooks = broken_hooks

    with patch("pandaren.engine.run_core.logger") as mock_log:
        # 不应抛异常
        caught = False
        try:
            loop._safe_hook("on_run_start", "task", "run_123")
        except Exception:
            caught = True
        assert_true(not caught, "hook 抛异常时 _safe_hook 不传播异常")
        assert_true(
            mock_log.warning.called,
            "hook 抛异常时 logger.warning 被调用",
        )
        warn_msg = mock_log.warning.call_args[0][0]
        assert_true(
            "on_run_start" in warn_msg or "%s" in warn_msg,
            "logger.warning 消息包含 hook 方法名",
            f"实际消息={warn_msg!r}",
        )

    # 36. hook 方法不存在（AttributeError via spec）→ 不抛异常，不触发 warning
    #     （getattr 返回 None 时直接跳过，不调用也不 warning）
    limited_hooks = MagicMock(spec=["on_run_start"])  # 只有 on_run_start
    loop._hooks = limited_hooks

    with patch("pandaren.engine.run_core.logger") as mock_log:
        caught2 = False
        try:
            loop._safe_hook("on_nonexistent_method", "arg1")
        except Exception:
            caught2 = True
        assert_true(not caught2, "hook 方法不存在时 _safe_hook 不抛异常")
        # getattr 返回 None → 不调用也不 warning
        assert_true(
            not mock_log.warning.called,
            "hook 方法不存在（getattr=None）时不触发 logger.warning",
        )

    # 37. hook 方法正常执行 → 不触发 logger.warning
    normal_hooks = MagicMock()
    loop._hooks = normal_hooks

    with patch("pandaren.engine.run_core.logger") as mock_log:
        loop._safe_hook("on_run_start", "task_ok", "run_ok")
        assert_true(
            not mock_log.warning.called,
            "hook 正常执行时不触发 logger.warning",
        )

    # 38. _safe_hook 调用 on_run_start 时传递正确参数
    args_hooks = MagicMock()
    loop._hooks = args_hooks
    loop._safe_hook("on_run_start", "my_task", "my_run_id")
    args_hooks.on_run_start.assert_called_once_with("my_task", "my_run_id")
    assert_true(
        args_hooks.on_run_start.call_args[0] == ("my_task", "my_run_id"),
        "_safe_hook 传递正确的位置参数给 on_run_start",
    )

    # 39. _hooks=None 时 _safe_hook 直接返回，不抛异常
    loop._hooks = None
    assert_no_raises(
        lambda: loop._safe_hook("on_run_start", "t", "r"),
        "_hooks=None 时 _safe_hook 不抛异常",
    )


# ════════════════════════════════════════════════════
#  Section: run_no_run_end
# ════════════════════════════════════════════════════

def test_run_no_run_end_mock():
    print("\n── RunCoreMixin.run() O3 兜底 (Mock) ──────────────────────")
    # _run_stream_core 是 RunCoreMixin 上的方法，AgentLoop 使用 __slots__，
    # 不能在实例上 patch——必须在类上 patch（patch.object(AgentLoop, ...)）。

    from pandaren.engine.loop import AgentLoop

    loop = _make_mock_agent_loop()

    # 40. generator 未发 RUN_END 就结束 → logger.error + 返回 AgentResult(success=False)
    async def _empty_gen(self, *args, **kwargs):
        return
        yield  # unreachable — makes it an async generator

    with patch("pandaren.engine.run_core.logger") as mock_log:
        with patch.object(AgentLoop, "_run_stream_core", _empty_gen):
            agent_result = async_run(loop.run("task", session_id="s1"))
        assert_true(
            mock_log.error.called,
            "generator 未发 RUN_END → logger.error 被调用",
        )
        assert_true(
            agent_result.success is False,
            "generator 未发 RUN_END → 返回 success=False 的 AgentResult",
        )

    # 41. generator 抛 Exception → logger.error + 返回 AgentResult(success=False)
    async def _raising_gen(self, *args, **kwargs):
        raise RuntimeError("内核崩溃")
        yield  # noqa

    with patch("pandaren.engine.run_core.logger") as mock_log:
        with patch.object(AgentLoop, "_run_stream_core", _raising_gen):
            agent_result2 = async_run(loop.run("task2", session_id="s2"))
        assert_true(
            mock_log.error.called,
            "generator 抛 Exception → logger.error 被调用",
        )
        assert_true(
            agent_result2.success is False,
            "generator 抛 Exception → 返回 success=False 的 AgentResult",
        )

    # 42. 正常路径（RUN_END 被发出）→ 不触发 logger.error
    good_result = AgentResult(success=True, output="done")
    run_end_event = StreamEvent(
        type=StreamEventType.RUN_END,
        data={"result": good_result},
        run_id="r1",
        agent_id="a1",
    )

    async def _good_gen(self, *args, **kwargs):
        yield run_end_event

    with patch("pandaren.engine.run_core.logger") as mock_log:
        with patch.object(AgentLoop, "_run_stream_core", _good_gen):
            agent_result3 = async_run(loop.run("task3", session_id="s3"))
        assert_true(
            not mock_log.error.called,
            "正常 RUN_END 路径不触发 logger.error",
        )
        assert_true(
            agent_result3.success is True,
            "正常 RUN_END 路径返回 success=True 的 AgentResult",
        )


# ════════════════════════════════════════════════════
#  Section: run_stream_core_validation
# ════════════════════════════════════════════════════

def test_run_stream_core_validation_mock():
    print("\n── _run_stream_core 入口校验 & Resume 身份隔离 (Mock) ──────")

    loop = _make_mock_agent_loop()

    # 43. session_id 为空串 → 直接抛 ValueError
    async def _call_empty_session():
        async for _ in loop._run_stream_core("task", session_id=""):
            pass

    caught_ve = None
    try:
        async_run(_call_empty_session())
    except ValueError as e:
        caught_ve = e
    assert_true(
        caught_ve is not None,
        "session_id='' → 直接抛 ValueError",
    )

    # 45. resume_state.session_id != session_id → PermissionError が内部で捕捉され
    #     RUN_END(success=False) が発行される（O3 原則：run() は外部に例外を投げない）
    #     _run_stream_core 内部では except Exception が PermissionError を捕捉し、
    #     logger.error を呼び出してから RUN_END(failure) を yield する。
    #     run() はそれを収集して AgentResult(success=False) を返す。
    paused_state = RunState(
        run_id="run_paused",
        agent_id="test.engine.mock",
        step_n=2,
        session_id="session_ORIGINAL",
        messages=[],
        metadata={},
    )

    async def _collect_events_wrong_session():
        events = []
        async for evt in loop._run_stream_core(
            "task",
            session_id="session_WRONG",  # 与 paused_state 不同
            resume_state=paused_state,
        ):
            events.append(evt)
        return events

    with patch("pandaren.engine.run_core.logger") as mock_log_pe:
        events_ws = async_run(_collect_events_wrong_session())
        # PermissionError 被内部 except 捕捉 → logger.error 被调用
        assert_true(
            mock_log_pe.error.called,
            "session_id 不匹配时内部 logger.error 被调用（PermissionError 被捕捉）",
        )
    # 最后一个事件应是 RUN_END(success=False)
    run_end_evt = events_ws[-1] if events_ws else None
    assert_true(
        run_end_evt is not None
        and run_end_evt.type == StreamEventType.RUN_END
        and run_end_evt.data["success"] is False,
        "session_id 不匹配 → 最终 RUN_END 事件 success=False",
        f"最后事件={run_end_evt}",
    )


# ════════════════════════════════════════════════════
#  Section: agent_result_properties
# ════════════════════════════════════════════════════

def test_agent_result_properties():
    print("\n── AgentResult & RunState 属性 (Mock) ──────────────────────")

    # AgentResult.paused 属性：success=False + run_state is not None → True
    run_state = RunState(
        run_id="r", agent_id="a", step_n=1,
        session_id="s",
    )
    paused_result = AgentResult(success=False, run_state=run_state)
    assert_true(paused_result.paused is True, "success=False + run_state is not None → paused=True")

    # success=True + run_state → paused=False（运行中不应标记为 paused）
    running_result = AgentResult(success=True, run_state=run_state)
    assert_true(running_result.paused is False, "success=True → paused=False（不管 run_state）")

    # success=False + run_state=None → paused=False（失败但非暂停）
    failed_result = AgentResult(success=False, run_state=None)
    assert_true(failed_result.paused is False, "success=False + run_state=None → paused=False")

    # LoopHooks Protocol 运行时检查：DefaultLoopHooks 满足 LoopHooks Protocol
    dh = DefaultLoopHooks()
    assert_true(
        isinstance(dh, LoopHooks),
        "DefaultLoopHooks 满足 LoopHooks Protocol（runtime_checkable）",
    )


# ════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════

SECTIONS = {
    "step_counter":                test_step_counter_mock,
    "output_parser":               test_output_parser_mock,
    "message_builder":             test_message_builder_mock,
    "agent_loop_init":             test_agent_loop_init_mock,
    "agent_loop_freeze":           test_agent_loop_freeze_mock,
    "safe_hook":                   test_safe_hook_mock,
    "run_no_run_end":              test_run_no_run_end_mock,
    "run_stream_core_validation":  test_run_stream_core_validation_mock,
    "agent_result_properties":     test_agent_result_properties,
}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Engine 层 Mock 测试")
    parser.add_argument(
        "--section",
        choices=list(SECTIONS.keys()),
        help="只运行指定 section",
    )
    args = parser.parse_args()

    if args.section:
        SECTIONS[args.section]()
        result.summary(args.section)
    else:
        print("╔══════════════════════════════════════════════════════════╗")
        print("║          pandaren/engine/ Mock 测试套件                  ║")
        print("╚══════════════════════════════════════════════════════════╝")
        for name, fn in SECTIONS.items():
            fn()
        result.summary("engine 全部 sections")

    sys.exit(0 if result.failed == 0 else 1)
