"""
Pandaren Agent SDK · Agent 模块真实测试

覆盖约束
--------
  - AR1: Agent 注册唯一性（agent_id 重复 → SubAgentRegistrationError）
  - AR2: build_agent_summaries 1% 上下文预算 + 排除调用方 + HEALTHY-only
  - AR3: call_agent 精确查找（agent_name）
  - AR4: call_agent 信任验证 + 循环检测 + 深度检测
  - AR5: refresh_health 存活检测
  - AR6: register_builtin_tools 幂等性
  - AR7: unregister 幂等性
  - AR8: set_status / drain 状态管理
  - AG-S1: EXTERNAL 不可委派；SUB_AGENT 不可向上委派
  - AG-S3: 循环委派检测（_delegate_stack）
  - AG-S7: finally push/pop 对齐
  - E4: SubAgentBlueprint trust_level 为必填，缺失 → ValueError
  - AgentLoader: load_agent_from_file / load_agents_from_dir
  - Agent 类: __repr__ / aclose 幂等 / run 真实调用

运行方式
--------
  cd pandaren/agent/tests && python test_agent.py
  cd pandaren/agent/tests && python test_agent.py --section models
  cd pandaren/agent/tests && python test_agent.py --section registry
  cd pandaren/agent/tests && python test_agent.py --section loader
  cd pandaren/agent/tests && python test_agent.py --section agent_class
  cd pandaren/agent/tests && python test_agent.py --section integration
"""

from __future__ import annotations

import asyncio
import os
import sys
import io
import logging
import tempfile
from pathlib import Path

# Windows 控制台 UTF-8 输出
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ═══ 环境变量加载 ═══
_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env.development")  # 可选：模块目录下的 env 文件
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#"):
                continue
            if "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

# ═══ SDK 导入 ═══
from pandaren.identity.models import Identity, SensitivePermission, PERMISSION_ALL, TrustLevel
from pandaren.agent import AgentStatus
from pandaren.sub_agent.models import (
    SubAgentSource, SubAgentSummary,
    SubAgentDelegateResult, SubAgentBlueprint,
)
from pandaren.sub_agent.exceptions import SubAgentRegistrationError
from pandaren.sub_agent.registry import SubAgentRegistry
from pandaren.sub_agent.loader import load_agent_from_file, load_agents_from_dir
from pandaren.builder import AgentBuilder
from pandaren.llm.client import OpenAICompatibleClient


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


def assert_raises(exc_type, name: str, detail: str = ""):
    """装饰器：断言被装饰的函数会抛出指定异常。"""
    def decorator(fn):
        try:
            fn()
            result.fail(name, f"未抛出 {exc_type.__name__}" + (f": {detail}" if detail else ""))
        except exc_type:
            result.ok(name)
        except Exception as e:
            result.fail(name, f"抛出了 {type(e).__name__}({e}) 而非 {exc_type.__name__}")
    return decorator


def assert_no_raises(name: str, detail: str = ""):
    """装饰器：断言被装饰的函数不会抛出异常。"""
    def decorator(fn):
        try:
            fn()
            result.ok(name)
        except Exception as e:
            result.fail(name, f"意外抛出 {type(e).__name__}({e})" + (f": {detail}" if detail else ""))
    return decorator


# ════════════════════════════════════════════════════
#  辅助：构建 LLM 客户端
# ════════════════════════════════════════════════════

def _make_llm_client():
    api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_name = "qwen-plus"
    if os.getenv("OPENAI_API_KEY") and not os.getenv("DASHSCOPE_API_KEY"):
        base_url = "https://api.openai.com/v1"
        model_name = "gpt-4o-mini"
    return OpenAICompatibleClient(
        api_key=api_key,
        model_name=model_name,
        base_url=base_url,
        timeout=60.0,
    )


def _make_agent(
    agent_id: str = "test.agent.v1",
    agent_name: str = "测试代理",
    when_to_use: str = "用于单元测试",
    trust_level: TrustLevel = TrustLevel.SUB_AGENT,
    llm_client=None,
):
    """构建一个最小化 Agent（不调用 LLM，仅用于注册/结构测试）。"""
    builder = AgentBuilder()
    builder.identity(
        agent_id=agent_id,
        agent_name=agent_name,
        when_to_use=when_to_use,
        sensitive_permissions=PERMISSION_ALL,
        trust_level=trust_level,
    )
    if llm_client is not None:
        builder.llm(llm_client)
    else:
        builder.llm(_make_llm_client())
    return builder.build()


# ════════════════════════════════════════════════════
#  1. 数据模型测试（AgentStatus / SubAgentSource 等）
# ════════════════════════════════════════════════════

def test_models():
    print("\n" + "═" * 60)
    print("1️⃣  Agent 数据模型测试")
    print("═" * 60)

    # ── AgentStatus ──
    print("\n  · AgentStatus 枚举")
    assert_true(AgentStatus.HEALTHY.value == "healthy", "HEALTHY.value == 'healthy'")
    assert_true(AgentStatus.UNHEALTHY.value == "unhealthy", "UNHEALTHY.value == 'unhealthy'")
    assert_true(AgentStatus.DRAINING.value == "draining", "DRAINING.value == 'draining'")
    assert_true(len(AgentStatus) == 3, "AgentStatus 共 3 个成员")

    # ── SubAgentSource ──
    print("\n  · SubAgentSource 枚举（IntEnum，优先级顺序）")
    assert_true(SubAgentSource.DIRECTORY == 1, "DIRECTORY == 1")
    assert_true(SubAgentSource.PROGRAMMATIC == 2, "PROGRAMMATIC == 2")
    assert_true(SubAgentSource.PROGRAMMATIC > SubAgentSource.DIRECTORY, "PROGRAMMATIC > DIRECTORY（优先级更高）")

    # ── SubAgentSummary frozen ──
    print("\n  · SubAgentSummary frozen dataclass")
    summary = SubAgentSummary(
        agent_name="演示",
        when_to_use="用于演示",
    )

    # ── SubAgentDelegateResult frozen ──
    print("\n  · SubAgentDelegateResult frozen dataclass")
    dr = SubAgentDelegateResult(
        success=True,
        output="done",
        target_agent_id="sub.agent",
        target_run_id="run-001",
        duration_ms=123.4,
    )
    assert_true(dr.success is True, "SubAgentDelegateResult.success 正确")
    assert_true(dr.output == "done", "SubAgentDelegateResult.output 正确")
    assert_true(dr.error is None, "SubAgentDelegateResult.error 默认 None")
    assert_true(dr.duration_ms == 123.4, "SubAgentDelegateResult.duration_ms 正确")

    @assert_raises(Exception, "SubAgentDelegateResult frozen — 不可修改字段")
    def _():
        dr.success = False  # type: ignore[misc]

    # ── SubAgentBlueprint frozen ──
    print("\n  · SubAgentBlueprint frozen dataclass")
    bp = SubAgentBlueprint(
        agent_id="bp.agent",
        agent_name="蓝图代理",
        when_to_use="蓝图测试",
        system_prompt="你是测试助手",
        trust_level=TrustLevel.SUB_AGENT,
    )
    assert_true(bp.agent_id == "bp.agent", "SubAgentBlueprint.agent_id 正确")
    assert_true(bp.source == SubAgentSource.DIRECTORY, "SubAgentBlueprint.source 默认 DIRECTORY")
    assert_true(bp.sensitive_permissions == frozenset(), "SubAgentBlueprint.sensitive_permissions 默认空 frozenset")
    assert_true(bp.tools == (), "SubAgentBlueprint.tools 默认空 tuple（Fail-Safe）")
    assert_true(bp.skills == (), "SubAgentBlueprint.skills 默认空 tuple（Fail-Safe）")
    assert_true(bp.sub_agents == (), "SubAgentBlueprint.sub_agents 默认空 tuple（Fail-Safe）")

    @assert_raises(Exception, "SubAgentBlueprint frozen — 不可修改字段")
    def _():
        bp.agent_id = "hacked"  # type: ignore[misc]


# ════════════════════════════════════════════════════
#  2. SubAgentRegistry 测试
# ════════════════════════════════════════════════════

def test_registry():
    print("\n" + "═" * 60)
    print("2️⃣  SubAgentRegistry 测试")
    print("═" * 60)

    # ── AR1: 注册与唯一性检查 ──
    print("\n  · AR1: 注册 + 唯一性")
    reg = SubAgentRegistry()
    a1 = _make_agent("reg.agent.1", "代理一", "负责任务一", TrustLevel.SUB_AGENT)
    a2 = _make_agent("reg.agent.2", "代理二", "负责任务二", TrustLevel.SUB_AGENT)

    @assert_no_raises("首次注册 agent.1 成功")
    def _():
        reg.register(a1)

    @assert_raises(SubAgentRegistrationError, "AR1: 重复注册同一 agent_id → SubAgentRegistrationError")
    def _():
        reg.register(a1)

    @assert_no_raises("首次注册 agent.2 成功")
    def _():
        reg.register(a2)

    assert_true(reg.agent_count() == 2, "注册后 agent_count == 2")

    # ── AR7: 注销幂等 ──
    print("\n  · AR7: 注销幂等")
    reg2 = SubAgentRegistry()
    ag = _make_agent("unreg.agent", "注销测试", "测试注销用")
    reg2.register(ag)
    assert_true(reg2.agent_count() == 1, "注销前 count == 1")
    reg2.unregister("unreg.agent")
    assert_true(reg2.agent_count() == 0, "注销后 count == 0")

    @assert_no_raises("AR7: 注销不存在的 agent_id 不抛异常（幂等）")
    def _():
        reg2.unregister("not.exists")

    # ── AR8: 状态管理 ──
    print("\n  · AR8: 状态管理")
    reg3 = SubAgentRegistry()
    ag3 = _make_agent("status.agent", "状态测试", "测试状态用")
    reg3.register(ag3)
    assert_true(reg3.get_status("status.agent") == AgentStatus.HEALTHY, "注册后默认 HEALTHY")

    reg3.set_status("status.agent", AgentStatus.UNHEALTHY)
    assert_true(reg3.get_status("status.agent") == AgentStatus.UNHEALTHY, "set_status UNHEALTHY 生效")

    reg3.drain("status.agent")
    assert_true(reg3.get_status("status.agent") == AgentStatus.DRAINING, "drain() 设为 DRAINING")

    @assert_raises(SubAgentRegistrationError, "AR8: 对未注册 agent_id set_status → SubAgentRegistrationError")
    def _():
        reg3.set_status("not.registered", AgentStatus.HEALTHY)

    # ── AR5: refresh_health ──
    print("\n  · AR5: refresh_health")
    reg4 = SubAgentRegistry()
    ag4 = _make_agent("health.agent", "健康测试", "测试健康刷新")
    reg4.register(ag4)
    reg4.set_status("health.agent", AgentStatus.UNHEALTHY)
    assert_true(reg4.get_status("health.agent") == AgentStatus.UNHEALTHY, "手动设 UNHEALTHY")
    reg4.refresh_health()
    # 实例引用存在 → 恢复 HEALTHY
    assert_true(reg4.get_status("health.agent") == AgentStatus.HEALTHY, "AR5: refresh_health 恢复 HEALTHY")

    # DRAINING 不因 refresh_health 恢复
    reg4.drain("health.agent")
    reg4.refresh_health()
    assert_true(reg4.get_status("health.agent") == AgentStatus.DRAINING, "AR5: DRAINING 不被 refresh_health 恢复")

    # ── AR2: build_agent_summaries 1% 预算 ──
    print("\n  · AR2: build_agent_summaries")
    reg5 = SubAgentRegistry()
    for i in range(5):
        ag = _make_agent(
            f"sum.agent.{i}",
            f"摘要代理{i}",
            f"负责任务{i}类型的工作",
        )
        reg5.register(ag)

    summaries = reg5.build_agent_summaries(context_window=128_000)
    assert_true(len(summaries) > 0, "AR2: summaries 非空")
    assert_true(all(isinstance(s, SubAgentSummary) for s in summaries), "AR2: 返回的都是 SubAgentSummary")

    # 排除指定 agent_id
    summaries_excluded = reg5.build_agent_summaries(exclude_agent_id="sum.agent.0")
    names = [s.agent_name for s in summaries_excluded]
    assert_true("摘要代理0" not in names, "AR2: exclude_agent_id 排除自身")

    # UNHEALTHY 不出现在摘要中
    reg5.set_status("sum.agent.1", AgentStatus.UNHEALTHY)
    summaries_healthy = reg5.build_agent_summaries()
    names_h = [s.agent_name for s in summaries_healthy]
    assert_true("摘要代理1" not in names_h, "AR2: UNHEALTHY agent 不出现在摘要中")

    # 极小 context_window → 预算裁剪
    tiny_summaries = reg5.build_agent_summaries(context_window=100)
    assert_true(len(tiny_summaries) <= len(summaries), "AR2: 极小预算时摘要数量 ≤ 正常预算")

    # ── AR3: call_agent 精确查找 ──
    print("\n  · AR3: call_agent 精确查找")
    from pandaren.tool import ToolContext

    reg6 = SubAgentRegistry()
    writer = _make_agent("code.writer", "代码生成器", "编写 Python 代码", TrustLevel.SUB_AGENT)
    reviewer = _make_agent("code.reviewer", "代码审查员", "审查代码质量", TrustLevel.SUB_AGENT)
    reg6.register(writer)
    reg6.register(reviewer)

    ctx = ToolContext(run_id="r1", step_n=1, agent_id="caller.agent")

    # 空 registry 情形
    empty_reg = SubAgentRegistry()
    async def _test_empty_call():
        res_empty = await empty_reg.call_agent("代码", "任务", ctx)
        assert_true(res_empty.success is False, "AR3: 空 registry call_agent → success=False")
    asyncio.run(_test_empty_call())

    # 精确名称查找
    async def _test_exact_name():
        res = await reg6.call_agent("代码生成器", "写个函数", ctx)
        assert_true(isinstance(res.success, bool), "AR3: 精确名称查找返回 ToolResult")
        print(f"   → 精确查找结果: success={res.success}, data={str(res.data)[:120]}")
    asyncio.run(_test_exact_name())

    # 不存在的名称
    async def _test_not_found():
        res = await reg6.call_agent("量子计算器", "任务", ctx)
        assert_true(res.success is False, "AR3: 不存在的名称 → success=False")
        assert_true("未找到" in str(res.error), "AR3: 错误信息包含'未找到'")
    asyncio.run(_test_not_found())

    # ── AR6: register_builtin_tools 幂等 ──
    print("\n  · AR6: register_builtin_tools 幂等")
    from pandaren.tool.registry import create_tool_registry

    tool_reg = create_tool_registry()
    reg7 = SubAgentRegistry(tool_registry=tool_reg)
    ag7 = _make_agent("builtin.agent", "内置工具测试", "测试内置工具注册")
    reg7.register(ag7)

    @assert_no_raises("AR6: 首次 register_builtin_tools 不抛异常")
    def _():
        reg7.register_builtin_tools()

    @assert_no_raises("AR6: 第二次 register_builtin_tools 幂等，不抛异常")
    def _():
        reg7.register_builtin_tools()

    # 确认 call_agent 已注册
    tool_names = [t.name for t in tool_reg.list_tools()]
    assert_true("call_agent" in tool_names, "AR6: call_agent 工具已注册")

    # 无 agent 时不注册
    empty_tool_reg = create_tool_registry()
    reg_empty = SubAgentRegistry(tool_registry=empty_tool_reg)
    reg_empty.register_builtin_tools()
    assert_true(len(empty_tool_reg.list_tools()) <= 1, "AR6: 无 agent 时不注册内置工具")

    # ── AG-S1: 信任验证 ──
    print("\n  · AG-S1: 信任验证（委派权限检查）")

    async def _test_trust():
        from pandaren.tool import ToolContext

        reg8 = SubAgentRegistry()
        orchestrator = _make_agent("orch.agent", "编排器", "主编排 Agent", TrustLevel.ORCHESTRATOR)
        sub = _make_agent("sub.worker", "子工作器", "具体工作", TrustLevel.SUB_AGENT)
        external = _make_agent("ext.user", "外部用户", "外部接入", TrustLevel.EXTERNAL)
        reg8.register(orchestrator)
        reg8.register(sub)
        reg8.register(external)

        # EXTERNAL 不可委派
        ctx_ext = ToolContext(
            run_id="r1", step_n=1, agent_id="ext.user",
            trust_level=TrustLevel.EXTERNAL,
        )
        res_ext = await reg8.call_agent("子工作器", "任务", ctx_ext)
        assert_true(res_ext.success is False, "AG-S1: EXTERNAL 委派 → success=False")
        assert_true("EXTERNAL" in str(res_ext.error), "AG-S1: 错误信息包含 EXTERNAL")

        # ORCHESTRATOR 可委派 SUB_AGENT
        ctx_orch = ToolContext(
            run_id="r2", step_n=1, agent_id="orch.agent",
            trust_level=TrustLevel.ORCHESTRATOR,
        )
        res_orch = await reg8.call_agent("子工作器", "2+2=?", ctx_orch)
        # 真实 LLM 调用，success 应为 True（或至少尝试了委派）
        assert_true(isinstance(res_orch.success, bool), "AG-S1: ORCHESTRATOR 委派返回 ToolResult")
        print(f"   → ORCHESTRATOR 委派结果: success={res_orch.success}, data={str(res_orch.data)[:120]}")

        # SUB_AGENT 不可向上委派 ORCHESTRATOR
        ctx_sub = ToolContext(
            run_id="r3", step_n=1, agent_id="sub.worker",
            trust_level=TrustLevel.SUB_AGENT,
        )
        res_upward = await reg8.call_agent("编排器", "任务", ctx_sub)
        assert_true(res_upward.success is False, "AG-S1: SUB_AGENT 向上委派 → success=False")
        assert_true("向上委派" in str(res_upward.error) or "无法委派" in str(res_upward.error),
                    "AG-S1: 向上委派错误信息正确")

    asyncio.run(_test_trust())

    # ── AG-S3: 循环委派检测 ──
    print("\n  · AG-S3: 循环委派检测")

    async def _test_cycle():
        from pandaren.tool import ToolContext

        reg9 = SubAgentRegistry()
        a = _make_agent("cycle.a", "A代理", "循环测试A", TrustLevel.SUB_AGENT)
        b = _make_agent("cycle.b", "B代理", "循环测试B", TrustLevel.ORCHESTRATOR)
        reg9.register(a)
        reg9.register(b)

        # 人工注入循环状态（模拟 A→B 正在执行中）
        reg9._delegate_stack.append("cycle.a")

        ctx = ToolContext(
            run_id="r1", step_n=1, agent_id="cycle.b",
            trust_level=TrustLevel.SUB_AGENT,
        )
        # cycle.b 试图委派 A代理（cycle.a 已在调用栈中 → 循环）
        res = await reg9.call_agent("A代理", "任务", ctx)
        assert_true(res.success is False, "AG-S3: 循环委派 → success=False")
        assert_true("循环" in str(res.error), "AG-S3: 错误信息包含'循环'")

        # 清理
        reg9._delegate_stack.clear()

    asyncio.run(_test_cycle())

    # ── 深度检测 ──
    print("\n  · 委派深度检测")

    async def _test_depth():
        from pandaren.tool import ToolContext

        reg10 = SubAgentRegistry(max_delegate_depth=2)
        target = _make_agent("depth.target", "目标代理", "深度测试", TrustLevel.SUB_AGENT)
        reg10.register(target)

        # 人工注入超深度调用栈
        reg10._delegate_stack.extend(["agent.0", "agent.1"])

        ctx = ToolContext(
            run_id="r1", step_n=1, agent_id="agent.2",
            trust_level=TrustLevel.ORCHESTRATOR,
        )
        res = await reg10.call_agent("目标代理", "任务", ctx)
        assert_true(res.success is False, "深度超限 → success=False")
        assert_true("深度" in str(res.error) or "超限" in str(res.error), "深度超限错误信息包含'深度'")
        reg10._delegate_stack.clear()

    asyncio.run(_test_depth())

    # ── __repr__ ──
    print("\n  · SubAgentRegistry __repr__")
    reg_repr = SubAgentRegistry()
    repr_str = repr(reg_repr)
    assert_true("SubAgentRegistry" in repr_str, "__repr__ 包含类名")
    assert_true("agents=" in repr_str, "__repr__ 包含 agents=")


# ════════════════════════════════════════════════════
#  3. AgentLoader 测试
# ════════════════════════════════════════════════════

def test_loader():
    print("\n" + "═" * 60)
    print("3️⃣  AgentLoader 测试")
    print("═" * 60)

    # ── 正常加载 ──
    print("\n  · 正常 Markdown 文件加载")
    valid_content = """\
---
agent_id: loader.test.agent
agent_name: 加载测试代理
when_to_use: 用于测试 AgentLoader 加载功能
trust_level: sub_agent
permissions: file:read, file:write
---

你是一个加载测试专用的助手。
当用户请求读取文件时，请认真执行。
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8", delete=False
    ) as f:
        f.write(valid_content)
        tmp_path = f.name

    try:
        @assert_no_raises("正常 .md 文件加载不抛异常")
        def _():
            pass

        bp = load_agent_from_file(tmp_path)
        assert_true(bp.agent_id == "loader.test.agent", "loader: agent_id 正确")
        assert_true(bp.agent_name == "加载测试代理", "loader: agent_name 正确")
        assert_true("加载功能" in bp.when_to_use, "loader: when_to_use 正确")
        assert_true(bp.trust_level == TrustLevel.SUB_AGENT, "loader: trust_level 正确")
        assert_true(len(bp.sensitive_permissions) > 0, "loader: sensitive_permissions 非空")
        assert_true("你是一个加载测试专用的助手" in bp.system_prompt, "loader: system_prompt 为 Markdown 正文")
        assert_true(bp.source == SubAgentSource.DIRECTORY, "loader: source 默认 DIRECTORY")
        assert_true(bp.source_path == str(Path(tmp_path).resolve()) or bp.source_path == tmp_path,
                    "loader: source_path 记录文件路径")
    finally:
        os.unlink(tmp_path)

    # ── E4: trust_level 必填 ──
    print("\n  · E4: trust_level 必填")
    no_trust_content = """\
---
agent_id: notrust.agent
agent_name: 无信任等级代理
when_to_use: 测试缺少 trust_level
---

系统提示词内容。
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8", delete=False
    ) as f:
        f.write(no_trust_content)
        tmp_no_trust = f.name

    try:
        @assert_raises(ValueError, "E4: 缺少 trust_level → ValueError")
        def _():
            load_agent_from_file(tmp_no_trust)
    finally:
        os.unlink(tmp_no_trust)

    # ── E4: 无效 trust_level 值 ──
    bad_trust_content = """\
---
agent_id: badtrust.agent
agent_name: 无效信任等级
when_to_use: 测试非法 trust_level
trust_level: super_admin
---

系统提示词。
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8", delete=False
    ) as f:
        f.write(bad_trust_content)
        tmp_bad_trust = f.name

    try:
        @assert_raises(ValueError, "E4: 非法 trust_level 值 → ValueError")
        def _():
            load_agent_from_file(tmp_bad_trust)
    finally:
        os.unlink(tmp_bad_trust)

    # ── 文件不存在 ──
    @assert_raises(FileNotFoundError, "loader: 文件不存在 → FileNotFoundError")
    def _():
        load_agent_from_file("/nonexistent/path/agent.md")

    # ── 缺少 frontmatter ──
    no_fm_content = "# 这是一个没有 frontmatter 的文件\n\n内容。\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8", delete=False
    ) as f:
        f.write(no_fm_content)
        tmp_no_fm = f.name

    try:
        @assert_raises(ValueError, "loader: 缺少 frontmatter → ValueError")
        def _():
            load_agent_from_file(tmp_no_fm)
    finally:
        os.unlink(tmp_no_fm)

    # ── 缺少 when_to_use ──
    no_wtu_content = """\
---
agent_id: nowtu.agent
agent_name: 无 when_to_use
trust_level: sub_agent
---

系统提示词。
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8", delete=False
    ) as f:
        f.write(no_wtu_content)
        tmp_no_wtu = f.name

    try:
        @assert_raises(ValueError, "loader: 缺少 when_to_use → ValueError")
        def _():
            load_agent_from_file(tmp_no_wtu)
    finally:
        os.unlink(tmp_no_wtu)

    # ── 正文为空 ──
    empty_body_content = """\
---
agent_id: emptybody.agent
agent_name: 空正文代理
when_to_use: 测试空正文
trust_level: sub_agent
---

"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8", delete=False
    ) as f:
        f.write(empty_body_content)
        tmp_empty_body = f.name

    try:
        @assert_raises(ValueError, "loader: 正文为空 → ValueError")
        def _():
            load_agent_from_file(tmp_empty_body)
    finally:
        os.unlink(tmp_empty_body)

    # ── load_agents_from_dir Fail-Safe ──
    print("\n  · load_agents_from_dir Fail-Safe 跳过错误文件")
    with tempfile.TemporaryDirectory() as tmpdir:
        # 写入一个有效文件
        valid = """\
---
agent_id: dir.valid.agent
agent_name: 目录有效代理
when_to_use: 批量加载有效文件测试
trust_level: sub_agent
---

你是目录批量加载测试助手。
"""
        (Path(tmpdir) / "valid.md").write_text(valid, encoding="utf-8")

        # 写入一个无效文件（缺少 trust_level）
        invalid = """\
---
agent_id: dir.invalid.agent
agent_name: 目录无效代理
when_to_use: 应该被跳过
---

系统提示词。
"""
        (Path(tmpdir) / "invalid.md").write_text(invalid, encoding="utf-8")

        blueprints = load_agents_from_dir(tmpdir)
        assert_true(len(blueprints) == 1, "load_agents_from_dir: Fail-Safe 跳过无效文件，成功加载 1 个")
        assert_true(blueprints[0].agent_id == "dir.valid.agent", "load_agents_from_dir: 加载的是有效文件")

    # ── 目录不存在 → 返回空列表 ──
    bps = load_agents_from_dir("/nonexistent/directory")
    assert_true(bps == [], "load_agents_from_dir: 目录不存在 → 返回空列表")

    # ── agent_id 回退到 file stem ──
    no_id_content = """\
---
agent_name: 无 ID 代理
when_to_use: 测试 agent_id 回退到文件名
trust_level: orchestrator
---

系统提示词内容。
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8",
        delete=False, prefix="my_fallback_id"
    ) as f:
        f.write(no_id_content)
        tmp_no_id = f.name

    try:
        bp_no_id = load_agent_from_file(tmp_no_id)
        assert_true(bp_no_id.agent_id == Path(tmp_no_id).stem,
                    "loader: 缺 agent_id 时回退为文件名 stem")
        assert_true(bp_no_id.trust_level == TrustLevel.ORCHESTRATOR,
                    "loader: trust_level=orchestrator 正确解析")
    finally:
        os.unlink(tmp_no_id)

    # ── tools / sub_agents 字段解析 ──
    resource_content = """\
---
agent_id: resource.agent
agent_name: 资源声明代理
when_to_use: 测试资源字段解析
trust_level: sub_agent
tools: grep_search, read_file, *
sub_agents: reviewer, tester
---

资源声明测试助手。
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8", delete=False
    ) as f:
        f.write(resource_content)
        tmp_res = f.name

    try:
        bp_res = load_agent_from_file(tmp_res)
        assert_true("grep_search" in bp_res.tools, "loader: tools 解析包含 grep_search")
        assert_true("read_file" in bp_res.tools, "loader: tools 解析包含 read_file")
        assert_true("*" in bp_res.tools, "loader: tools 解析包含 *")
        assert_true("reviewer" in bp_res.sub_agents, "loader: sub_agents 解析包含 reviewer")
        assert_true("tester" in bp_res.sub_agents, "loader: sub_agents 解析包含 tester")
    finally:
        os.unlink(tmp_res)


# ════════════════════════════════════════════════════
#  4. Agent 类测试
# ════════════════════════════════════════════════════

def test_agent_class():
    print("\n" + "═" * 60)
    print("4️⃣  Agent 类测试")
    print("═" * 60)

    # ── __repr__ ──
    print("\n  · Agent.__repr__")
    agent = _make_agent("repr.agent", "表示测试", "repr 测试用")
    repr_str = repr(agent)
    assert_true("Agent" in repr_str, "__repr__ 包含 'Agent'")
    assert_true("repr.agent" in repr_str, "__repr__ 包含 agent_id")
    assert_true("表示测试" in repr_str, "__repr__ 包含 agent_name")

    # ── 属性只读 ──
    print("\n  · Agent 属性访问")
    assert_true(agent.agent_id == "repr.agent", "agent_id 属性正确")
    assert_true(agent.agent_name == "表示测试", "agent_name 属性正确")
    assert_true(isinstance(agent.identity, Identity), "identity 属性返回 Identity 实例")

    # ── aclose 幂等 ──
    print("\n  · aclose 幂等性")

    async def _test_aclose():
        ag = _make_agent("close.agent", "关闭测试", "aclose 幂等测试")

        @assert_no_raises("aclose 首次调用不抛异常")
        def _():
            pass

        await ag.aclose()
        result.ok("aclose 首次调用不抛异常")

        @assert_no_raises("aclose 第二次调用幂等，不抛异常")
        def _():
            pass

        await ag.aclose()
        result.ok("aclose 第二次调用幂等，不抛异常")

    asyncio.run(_test_aclose())

    # ── async context manager ──
    print("\n  · async context manager（async with）")

    async def _test_ctx_manager():
        ag = _make_agent("ctx.agent", "上下文管理测试", "测试 async with 用法")
        try:
            async with ag as a:
                assert_true(a is ag, "async with 返回 Agent 自身")
            result.ok("async with 正常退出不抛异常")
        except Exception as e:
            result.fail("async with 正常退出不抛异常", str(e))

    asyncio.run(_test_ctx_manager())


# ════════════════════════════════════════════════════
#  5. 集成测试（真实 LLM）
# ════════════════════════════════════════════════════

def test_integration():
    print("\n" + "═" * 60)
    print("5️⃣  集成测试（真实 LLM 调用）")
    print("═" * 60)

    async def _run_all():
        # ── Agent.run() 真实调用 ──
        print("\n  · Agent.run() 真实 LLM 调用")

        agent = (
            AgentBuilder()
            .identity(
                agent_id="integration.test.agent",
                agent_name="集成测试助手",
                when_to_use="用于集成测试的通用助手",
                sensitive_permissions=PERMISSION_ALL,
                trust_level=TrustLevel.SUB_AGENT,
            )
            .llm(_make_llm_client())
            .system_prompt("你是一个集成测试助手。请直接回答问题，不要多余解释。")
            .behavior(max_steps=3)
            .build()
        )

        async with agent:
            result_obj = await agent.run("1 + 1 等于几？请只回答数字。", session_id="integration-test-1")
            assert_true(result_obj.success is True, "integration: run() success=True")
            assert_true(result_obj.output is not None, "integration: run() output 非 None")
            assert_true(len(str(result_obj.output)) > 0, "integration: run() output 非空")
            assert_true(result_obj.run_id != "", "integration: run_id 非空")
            assert_true(result_obj.total_steps >= 1, "integration: total_steps >= 1")
            print(f"   → LLM 输出: {str(result_obj.output)[:80]}")
            print(f"   → Token: {result_obj.total_input_tokens}→{result_obj.total_output_tokens}")

        # ── run_stream() 真实流式调用 ──
        print("\n  · Agent.run_stream() 真实流式调用")
        from pandaren.engine.stream import StreamEventType

        stream_agent = (
            AgentBuilder()
            .identity(
                agent_id="stream.test.agent",
                agent_name="流式测试助手",
                when_to_use="用于流式集成测试",
                sensitive_permissions=PERMISSION_ALL,
                trust_level=TrustLevel.SUB_AGENT,
            )
            .llm(_make_llm_client())
            .system_prompt("你是流式测试助手。请用一句话回答。")
            .behavior(max_steps=3)
            .build()
        )

        events_seen = set()
        output_text = ""

        async with stream_agent:
            async for event in stream_agent.run_stream("你好，请自我介绍一下。", session_id="stream-test-1"):
                events_seen.add(event.type)
                if event.type == StreamEventType.LLM_TOKEN:
                    output_text = event.data.get("snapshot", "")

        assert_true(StreamEventType.RUN_START in events_seen, "stream: 收到 RUN_START 事件")
        assert_true(StreamEventType.RUN_END in events_seen, "stream: 收到 RUN_END 事件")
        assert_true(StreamEventType.LLM_TOKEN in events_seen, "stream: 收到 LLM_TOKEN 事件")
        assert_true(len(output_text) > 0, "stream: output_text 非空")
        print(f"   → 流式输出: {output_text[:80]}")

        # ── 多轮对话（session 上下文串联）──
        print("\n  · 多轮对话（同一 session_id）")
        import uuid

        mt_agent = (
            AgentBuilder()
            .identity(
                agent_id="multi.turn.agent",
                agent_name="多轮对话测试助手",
                when_to_use="多轮对话集成测试",
                sensitive_permissions=PERMISSION_ALL,
                trust_level=TrustLevel.SUB_AGENT,
            )
            .llm(_make_llm_client())
            .system_prompt("你是一个有记忆的测试助手。请记住用户说的信息。")
            .behavior(max_steps=3)
            .build()
        )

        session_id = str(uuid.uuid4())
        async with mt_agent:
            r1 = await mt_agent.run("我的幸运数字是 42。", session_id=session_id)
            assert_true(r1.success is True, "多轮: 第一轮 success=True")

            r2 = await mt_agent.run("我的幸运数字是多少？", session_id=session_id)
            assert_true(r2.success is True, "多轮: 第二轮 success=True")
            assert_true("42" in str(r2.output), "多轮: 第二轮记住了第一轮的内容（42）")
            print(f"   → 第一轮: {str(r1.output)[:60]}")
            print(f"   → 第二轮: {str(r2.output)[:60]}")

        # ── SubAgentRegistry.call_agent() 真实委派 ──
        print("\n  · SubAgentRegistry.call_agent() 真实委派")
        from pandaren.tool import ToolContext

        sub_agent = (
            AgentBuilder()
            .identity(
                agent_id="delegate.sub.worker",
                agent_name="委派子工作器",
                when_to_use="被编排器委派的计算工作器",
                sensitive_permissions=PERMISSION_ALL,
                trust_level=TrustLevel.SUB_AGENT,
            )
            .llm(_make_llm_client())
            .system_prompt("你是一个计算助手，只回答数学问题，给出最简洁的答案。")
            .behavior(max_steps=3)
            .build()
        )

        reg = SubAgentRegistry()
        reg.register(sub_agent)

        ctx = ToolContext(
            run_id="delegate-run-1",
            step_n=1,
            agent_id="orchestrator.main",
            trust_level=TrustLevel.ORCHESTRATOR,
        )
        res = await reg.call_agent(
            "委派子工作器",
            "5 × 7 等于多少？请只回答数字。",
            ctx,
        )
        assert_true(res.success is True, "delegate: 委派执行 success=True")
        assert_true("35" in str(res.data), "delegate: 委派结果包含正确答案 35")
        print(f"   → 委派结果: {str(res.data)[:120]}")

    asyncio.run(_run_all())


# ════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════

SECTIONS = {
    "models": test_models,
    "registry": test_registry,
    "loader": test_loader,
    "agent_class": test_agent_class,
    "integration": test_integration,
}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Agent 模块真实测试")
    parser.add_argument(
        "--section",
        choices=list(SECTIONS.keys()),
        default="",
        help="仅运行指定测试分区",
    )
    args = parser.parse_args()

    print("🐼 Pandaren Agent SDK — Agent 模块真实测试")
    print("   目标模块: pandaren/agent/")
    print("   包含类: Agent, SubAgentRegistry, SubAgentBlueprint, AgentLoader")
    print()

    logging.getLogger("pandaren.agent").setLevel(logging.WARNING)
    logging.getLogger("pandaren.sub_agent.registry").setLevel(logging.WARNING)
    logging.getLogger("pandaren.sub_agent.loader").setLevel(logging.WARNING)
    logging.getLogger("pandaren.engine").setLevel(logging.WARNING)
    logging.getLogger("pandaren.tool").setLevel(logging.WARNING)

    if args.section:
        section_name = args.section
        section_fn = SECTIONS[section_name]
        section_result = TestResult()

        global result
        old_result = result
        result = section_result

        section_fn()

        result = old_result
        result.passed += section_result.passed
        result.failed += section_result.failed
        result.errors.extend(section_result.errors)

        section_result.summary(section_name)
    else:
        test_models()
        test_registry()
        test_loader()
        test_agent_class()
        test_integration()
        result.summary("全部")

    if result.failed > 0:
        print("\n⚠️  存在失败的测试用例，请检查上方输出")
        sys.exit(1)
    else:
        print("\n🎉 所有测试通过！Agent 模块真实测试验证完毕")
        sys.exit(0)


if __name__ == "__main__":
    main()
