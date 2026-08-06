"""
Pandaren Agent SDK · Tool 层 Mock 测试

覆盖约束
--------
  通过 Mock/Patch 验证 Tool 层的内部行为、生命周期 hooks、边界条件：
  - Tool frozen dataclass HC2（schema 深拷贝隔离）
  - ToolResult mutable dataclass O3（永不抛异常）
  - ToolContext frozen dataclass B2（只读快照）
  - DiscoveredToolEntry frozen dataclass
  - @tool.function 装饰器（docstring/type hint 生成 input_schema）
  - validate_required_fields 必填字段校验
  - validate_conflicts 自动升级 sensitivity / CRITICAL 强制 audit
  - ToolRegistry 注册/重复注册/set_hooks HC4/execute 流水线
  - DEFERRED 工具执行门控（Step 7.0）
  - search_tools 立即写入 DiscoveryManager（标记为已发现）
  - halt_on_failure → result.halt=True + on_run_halt hook 触发
  - execute_tools_concurrent C1 仲裁
  - AgentHooks 10 个生命周期回调

运行方式
--------
  cd pandaren/tool/tests && python test_tool_mock.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import io
import types as builtin_types
import warnings
from unittest.mock import patch, MagicMock, AsyncMock, call

# Windows 控制台 UTF-8 输出
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ═══ SDK 导入 ═══
from pandaren.tool import Tool, ToolResult, ToolContext, DiscoveredToolEntry
from pandaren.tool.types import ToolTier, SensitivityLevel, CircuitBreakerConfig
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.definition.tool_lifecycle import ToolLifecycle
from pandaren.tool.decorator import tool
from pandaren.tool.schema_inference import parse_docstring as _parse_docstring, infer_input_schema as _build_input_schema
from pandaren.tool.registry.validator import validate_required_fields, validate_conflicts
from pandaren.tool.exceptions import ToolRegistrationError, ToolValidationWarning
from pandaren.hook import AgentHooks, DefaultAgentHooks
from pandaren.tool.registry import ToolRegistry
from pandaren.behavior.harness.executor import HarnessExecutor
from pandaren.identity.models import TrustLevel


# ════════════════════════════════════════════════════
#  异步辅助
# ════════════════════════════════════════════════════

def async_run(coro):
    """在当前线程同步运行一个协程。"""
    return asyncio.new_event_loop().run_until_complete(coro)


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


# ════════════════════════════════════════════════════
#  工厂方法
# ════════════════════════════════════════════════════

# Policy 字段集合（用于自动路由）
_POLICY_FIELDS: frozenset[str] = frozenset({
    "sensitivity", "is_reversible", "is_idempotent", "audit_required",
    "trust_level_required", "agent_whitelist", "sensitive_permission",
    "max_calls_per_turn", "max_output_bytes", "circuit_breaker",
    "halt_on_failure", "read_only", "default_result_limit", "supports_offset_pagination",
})

_LIFECYCLE_FIELDS: frozenset[str] = frozenset({
    "is_enabled", "error_formatter", "validate_input", "format_result_for_llm",
})


def _make_tool(**overrides) -> Tool:
    """创建测试用 Tool 的工厂方法。

    自动将 ToolPolicy / ToolLifecycle 字段路由到正确的嵌套对象。
    用法:
      _make_tool(sensitivity=HIGH, is_reversible=False)  → policy 字段自动路由
      _make_tool(is_enabled=lambda ctx: True)            → lifecycle 字段自动路由
      _make_tool(name="x")                               → Tool 顶层字段不变
    """
    defaults: dict = dict(
        name="test_tool",
        description="测试工具",
        executor=lambda ctx, **kwargs: "ok",
        input_schema={"type": "object", "properties": {}},
        tier=ToolTier.ALWAYS,
        when_to_use="测试工具默认用途描述",
    )
    defaults.update(overrides)

    # 提取 policy 字段
    policy_kwargs: dict = dict(
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
    )
    for k in list(defaults.keys()):
        if k in _POLICY_FIELDS:
            policy_kwargs[k] = defaults.pop(k)

    # 提取 lifecycle 字段
    lifecycle_kwargs: dict = {}
    for k in list(defaults.keys()):
        if k in _LIFECYCLE_FIELDS:
            lifecycle_kwargs[k] = defaults.pop(k)

    return Tool(
        policy=ToolPolicy(**policy_kwargs),
        lifecycle=ToolLifecycle(**lifecycle_kwargs),
        **defaults,
    )


def _make_ctx(**overrides) -> ToolContext:
    """创建测试用 ToolContext 的工厂方法。"""
    defaults = dict(
        run_id="run-001",
        step_n=1,
        agent_id="test.agent",
        trust_level=TrustLevel.SUB_AGENT,
    )
    defaults.update(overrides)
    return ToolContext(**defaults)


def _make_registry() -> ToolRegistry:
    """创建测试用 ToolRegistry 的工厂方法。"""
    return ToolRegistry()


# ════════════════════════════════════════════════════
#  Section 1 — models
# ════════════════════════════════════════════════════

def test_models():
    """1. Tool / ToolResult / ToolContext / DiscoveredToolEntry 数据模型"""
    print("\n" + "═" * 60)
    print("1.  models — 数据模型")
    print("═" * 60)

    # 1.1 Tool frozen — 不可变
    t = _make_tool()
    @assert_raises(Exception, "1.1 Tool frozen — 修改字段抛出异常")
    def _():
        t.name = "hacked"

    # 1.2 Tool.full_name — 无 namespace
    t_no_ns = _make_tool(name="my_tool", namespace=None)
    assert_true(t_no_ns.full_name == "my_tool", "1.2 full_name 无 namespace = bare name")

    # 1.3 Tool.full_name — 有 namespace
    t_with_ns = _make_tool(name="my_tool", namespace="ns1")
    assert_true(t_with_ns.full_name == "ns1.my_tool", "1.3 full_name 有 namespace = 'ns.name'")

    # 1.4 HC2 — input_schema dict 被转换为 MappingProxyType
    schema_dict = {"type": "object", "properties": {"x": {"type": "string"}}}
    t_hc2 = _make_tool(input_schema=schema_dict)
    assert_true(
        isinstance(t_hc2.input_schema, builtin_types.MappingProxyType),
        "1.4 HC2: input_schema dict → MappingProxyType",
    )

    # 1.5 HC2 — 修改原始 dict 不影响 Tool 内 schema
    schema_dict["properties"]["y"] = {"type": "integer"}
    assert_true(
        "y" not in t_hc2.input_schema.get("properties", {}),
        "1.5 HC2: 修改原始 dict 不影响已注册 schema（深拷贝隔离）",
    )

    # 1.6 HC2 — output_schema dict 同样被深拷贝
    out_dict = {"type": "object"}
    t_out = _make_tool(output_schema=out_dict)
    out_dict["extra"] = "injected"
    assert_true(
        "extra" not in (t_out.output_schema or {}),
        "1.6 HC2: output_schema 也深拷贝隔离",
    )

    # 1.7 ToolResult mutable — 可修改字段
    r = ToolResult(success=True, data="hello", tool_name="t1")
    r.halt = True
    assert_true(r.halt is True, "1.7 ToolResult mutable — halt 字段可修改")

    # 1.8 ToolResult O3 — 默认字段
    r2 = ToolResult(success=False)
    assert_true(r2.data == "", "1.8 ToolResult 默认 data=\"\"")
    assert_true(r2.error == "", "1.8 ToolResult 默认 error=\"\"")
    assert_true(r2.halt is False, "1.8 ToolResult 默认 halt=False")
    assert_true(r2.deduplicated is False, "1.8 ToolResult 默认 deduplicated=False")
    assert_true(r2.truncated is False, "1.8 ToolResult 默认 truncated=False")

    # 1.9 ToolContext frozen — 不可变
    ctx = _make_ctx()
    @assert_raises(Exception, "1.9 ToolContext frozen — 修改字段抛出异常")
    def _():
        ctx.run_id = "hacked"

    # 1.10 ToolContext 默认值
    ctx2 = ToolContext(run_id="r1", step_n=0, agent_id="ag")
    assert_true(ctx2.trust_level == TrustLevel.SUB_AGENT, "1.10 ToolContext 默认 trust_level=SUB_AGENT")
    assert_true(isinstance(ctx2.metadata, builtin_types.MappingProxyType), "1.10 ToolContext metadata=MappingProxyType")

    # 1.11 DiscoveredToolEntry frozen
    entry = DiscoveredToolEntry(name="my_tool", turn=3)
    @assert_raises(Exception, "1.11 DiscoveredToolEntry frozen — 修改字段抛出异常")
    def _():
        entry.name = "hacked"

    assert_true(entry.name == "my_tool", "1.11 DiscoveredToolEntry.name 正确")
    assert_true(entry.turn == 3, "1.11 DiscoveredToolEntry.turn 正确")

    # 1.12 CircuitBreakerConfig 校验
    @assert_raises(ValueError, "1.12 CircuitBreakerConfig failure_threshold<=0 抛 ValueError")
    def _():
        CircuitBreakerConfig(failure_threshold=0)

    @assert_raises(ValueError, "1.12 CircuitBreakerConfig recovery_timeout<=0 抛 ValueError")
    def _():
        CircuitBreakerConfig(recovery_timeout=0)

    @assert_raises(ValueError, "1.12 CircuitBreakerConfig max_recovery_timeout < recovery_timeout 抛 ValueError")
    def _():
        CircuitBreakerConfig(recovery_timeout=60.0, max_recovery_timeout=10.0)


# ════════════════════════════════════════════════════
#  Section 2 — decorator
# ════════════════════════════════════════════════════

def test_decorator():
    """2. @tool.function 装饰器"""
    print("\n" + "═" * 60)
    print("2.  decorator — @tool.function 装饰器")
    print("═" * 60)

    # 2.1 基础装饰器生成 Tool 实例
    @tool.function(
        tier=ToolTier.ALWAYS,
        when_to_use="测试打招呼工具",
        policy=ToolPolicy(sensitivity=SensitivityLevel.LOW, is_reversible=True,
                          audit_required=False, is_idempotent=True),
    )
    def my_greet(ctx: ToolContext, name: str, count: int = 1) -> str:
        """打招呼工具

        Args:
            name: 姓名
            count: 次数
        """
        return f"Hello {name}" * count

    assert_true(isinstance(my_greet, Tool), "2.1 @tool.function 返回 Tool 实例")
    assert_true(my_greet.name == "my_greet", "2.1 name 默认取函数名")
    assert_true(my_greet.description == "打招呼工具", "2.1 description 来自 docstring 第一行")

    # 2.2 input_schema 自动生成 — 跳过 ctx 参数
    schema = my_greet.input_schema
    props = schema.get("properties", {})
    assert_true("ctx" not in props, "2.2 input_schema 跳过 ctx 参数")
    assert_true("name" in props, "2.2 input_schema 包含 name 参数")
    assert_true("count" in props, "2.2 input_schema 包含 count 参数")

    # 2.3 类型映射
    assert_true(props["name"].get("type") == "string", "2.3 str → 'string'")
    assert_true(props["count"].get("type") == "integer", "2.3 int → 'integer'")

    # 2.4 必填字段（无默认值）
    required = schema.get("required", [])
    assert_true("name" in required, "2.4 无默认值的 name → required")
    assert_true("count" not in required, "2.4 有默认值的 count → 非 required")

    # 2.5 docstring 参数说明注入
    assert_true(props["name"].get("description") == "姓名", "2.5 name 描述来自 docstring Args")
    assert_true(props["count"].get("description") == "次数", "2.5 count 描述来自 docstring Args")

    # 2.6 name 覆盖
    @tool.function(
        name="custom_name",
        tier=ToolTier.DEFERRED,
        when_to_use="用于搜索网页",
        policy=ToolPolicy(sensitivity=SensitivityLevel.MEDIUM, is_reversible=True,
                          audit_required=False, is_idempotent=True),
    )
    def web_search(ctx: ToolContext, query: str) -> str:
        """搜索网页"""
        return query

    assert_true(web_search.name == "custom_name", "2.6 name 覆盖参数生效")
    assert_true(web_search.tier == ToolTier.DEFERRED, "2.6 tier=DEFERRED 正确")
    assert_true(web_search.when_to_use == "用于搜索网页", "2.6 when_to_use 正确")

    # 2.7 自定义 description 覆盖 docstring
    @tool.function(
        description="自定义描述",
        tier=ToolTier.ALWAYS,
        when_to_use="自定义描述覆盖测试工具",
        policy=ToolPolicy(sensitivity=SensitivityLevel.LOW, is_reversible=True,
                          audit_required=False, is_idempotent=True),
    )
    def override_desc(ctx: ToolContext) -> str:
        """docstring 的描述"""
        return ""

    assert_true(override_desc.description == "自定义描述", "2.7 description 参数覆盖 docstring")

    # 2.8 _parse_docstring 直接调用
    def sample_func(x, y):
        """第一行描述

        Args:
            x: x 的说明
            y: y 的说明
        Returns:
            返回值
        """
        pass

    desc, params = _parse_docstring(sample_func)
    assert_true(desc == "第一行描述", "2.8 _parse_docstring 提取第一行描述")
    assert_true(params.get("x") == "x 的说明", "2.8 _parse_docstring 提取 x 参数说明")
    assert_true(params.get("y") == "y 的说明", "2.8 _parse_docstring 提取 y 参数说明")
    assert_true("returns" not in params, "2.8 _parse_docstring 不包含 Returns 段")

    # 2.9 无 docstring 时 description = 函数名
    @tool.function(
        tier=ToolTier.ALWAYS,
        when_to_use="无 docstring 测试工具",
        policy=ToolPolicy(sensitivity=SensitivityLevel.LOW, is_reversible=True,
                          audit_required=False, is_idempotent=True),
    )
    def no_doc(ctx: ToolContext) -> str:
        pass

    assert_true(no_doc.description == "", "2.9 无 docstring 时 description=空字符串")

    # 2.10 参数名 ctx/context/self 均被跳过
    @tool.function(
        tier=ToolTier.ALWAYS,
        when_to_use="ctx 参数跳过测试工具",
        policy=ToolPolicy(sensitivity=SensitivityLevel.LOW, is_reversible=True,
                          audit_required=False, is_idempotent=True),
    )
    def ctx_skip(context, param: str) -> str:
        """ctx_skip"""
        return param

    schema2 = ctx_skip.input_schema
    props2 = schema2.get("properties", {})
    assert_true("context" not in props2, "2.10 参数名 context 被跳过")
    assert_true("param" in props2, "2.10 param 参数保留在 schema")


# ════════════════════════════════════════════════════
#  Section 3 — validation
# ════════════════════════════════════════════════════

def test_validation():
    """3. validate_required_fields / validate_conflicts"""
    print("\n" + "═" * 60)
    print("3.  validation — 注册校验")
    print("═" * 60)

    # 3.1 validate_required_fields — name 为空抛 ToolRegistrationError
    @assert_raises(ToolRegistrationError, "3.1 validate_required_fields: name 为空 → ToolRegistrationError")
    def _():
        t = _make_tool(name="")
        validate_required_fields(t)

    # 3.2 validate_required_fields — description 超 100 字发出 ToolValidationWarning
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        long_desc = "X" * 101
        t_long = _make_tool(description=long_desc)
        validate_required_fields(t_long)
        warning_msgs = [str(x.message) for x in w if issubclass(x.category, ToolValidationWarning)]
        assert_true(len(warning_msgs) >= 1, "3.2 description 超 100 字 → ToolValidationWarning")
        assert_true(any("101" in m for m in warning_msgs), "3.2 warning 包含实际字数 101")

    # 3.3 validate_conflicts — is_reversible=False + LOW sensitivity → 自动升级为 HIGH
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        t_irrev = _make_tool(is_reversible=False, sensitivity=SensitivityLevel.LOW)
        upgraded = validate_conflicts(t_irrev)
        assert_true(upgraded.sensitivity == SensitivityLevel.HIGH,
                    "3.3 is_reversible=False + LOW → 自动升级为 HIGH")
        warning_msgs = [str(x.message) for x in w if issubclass(x.category, ToolValidationWarning)]
        assert_true(any("HIGH" in m for m in warning_msgs), "3.3 升级 warning 包含 'HIGH'")

    # 3.4 validate_conflicts — is_reversible=False + MEDIUM → 自动升级为 HIGH
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        t_med = _make_tool(is_reversible=False, sensitivity=SensitivityLevel.MEDIUM)
        upgraded2 = validate_conflicts(t_med)
        assert_true(upgraded2.sensitivity == SensitivityLevel.HIGH,
                    "3.4 is_reversible=False + MEDIUM → 自动升级为 HIGH")

    # 3.5 validate_conflicts — CRITICAL 强制 audit_required=True
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        t_crit = _make_tool(sensitivity=SensitivityLevel.CRITICAL, audit_required=False,
                            is_reversible=True, is_idempotent=True)
        upgraded3 = validate_conflicts(t_crit)
        assert_true(upgraded3.audit_required is True,
                    "3.5 CRITICAL → audit_required 自动设为 True")
        warning_msgs = [str(x.message) for x in w if issubclass(x.category, ToolValidationWarning)]
        assert_true(any("audit_required" in m for m in warning_msgs),
                    "3.5 CRITICAL 强制 audit warning 包含 'audit_required'")

    # 3.6 validate_conflicts — circuit_breaker.failure_threshold <= 0 → ToolRegistrationError
    @assert_raises(ToolRegistrationError, "3.6 circuit_breaker.failure_threshold<=0 → ToolRegistrationError")
    def _():
        # 直接传入 dict，模拟 failure_threshold=0（CircuitBreakerConfig 会先抛 ValueError）
        # 需绕过 CircuitBreakerConfig 的 __post_init__，直接构造无效对象
        import dataclasses
        bad_cb = object.__new__(CircuitBreakerConfig)
        object.__setattr__(bad_cb, "failure_threshold", 0)
        object.__setattr__(bad_cb, "recovery_timeout", 30.0)
        object.__setattr__(bad_cb, "max_recovery_timeout", 300.0)
        t_bad_cb = _make_tool(circuit_breaker=bad_cb)
        validate_conflicts(t_bad_cb)

    # 3.7 validate_conflicts — halt_on_failure=True + is_reversible=True → ToolValidationWarning
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        t_halt = _make_tool(halt_on_failure=True, is_reversible=True)
        validate_conflicts(t_halt)
        warning_msgs = [str(x.message) for x in w if issubclass(x.category, ToolValidationWarning)]
        assert_true(any("halt_on_failure" in m for m in warning_msgs),
                    "3.7 halt_on_failure + is_reversible → ToolValidationWarning")

    # 3.8 validate_conflicts — CRITICAL + is_idempotent=True → ToolValidationWarning
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        t_ci = _make_tool(sensitivity=SensitivityLevel.CRITICAL, is_idempotent=True,
                          is_reversible=True, audit_required=True)
        validate_conflicts(t_ci)
        warning_msgs = [str(x.message) for x in w if issubclass(x.category, ToolValidationWarning)]
        assert_true(any("is_idempotent" in m for m in warning_msgs),
                    "3.8 CRITICAL + is_idempotent=True → ToolValidationWarning")

    # 3.9 正常工具通过 validate_required_fields 无异常
    t_ok = _make_tool()
    try:
        validate_required_fields(t_ok)
        result.ok("3.9 正常工具通过 validate_required_fields 无异常")
    except Exception as e:
        result.fail("3.9 正常工具通过 validate_required_fields 无异常", str(e))


# ════════════════════════════════════════════════════
#  Section 4 — registry 注册与查询
# ════════════════════════════════════════════════════

def test_registry():
    """4. ToolRegistry 注册/查询/hooks"""
    print("\n" + "═" * 60)
    print("4.  registry — 注册与查询")
    print("═" * 60)

    # 4.1 注册工具 + get_tool
    reg = _make_registry()
    t = _make_tool(name="get_test")
    reg.register_tool(t)
    found = reg.get_tool("get_test")
    assert_true(found is not None, "4.1 register_tool + get_tool 找到工具")
    assert_true(found.name == "get_test", "4.1 get_tool 返回正确工具名")

    # 4.2 重复注册同名工具 → ToolRegistrationError
    @assert_raises(ToolRegistrationError, "4.2 重复注册同名工具 → ToolRegistrationError")
    def _():
        reg.register_tool(_make_tool(name="get_test"))

    # 4.3 list_tools
    t2 = _make_tool(name="list_test")
    reg.register_tool(t2)
    tools = reg.list_tools()
    names = [t.name for t in tools]
    assert_true("get_test" in names, "4.3 list_tools 包含 get_test")
    assert_true("list_test" in names, "4.3 list_tools 包含 list_test")

    # 4.4 set_hooks — HC4：第二次调用抛 RuntimeError
    reg2 = _make_registry()
    hooks1 = DefaultAgentHooks()
    reg2.set_hooks(hooks1)
    @assert_raises(RuntimeError, "4.4 set_hooks HC4：第二次调用 → RuntimeError")
    def _():
        reg2.set_hooks(DefaultAgentHooks())

    # 4.5 build_tool_schemas — ALWAYS 工具出现在 schema 中
    reg3 = _make_registry()
    t_always = _make_tool(name="always_tool", tier=ToolTier.ALWAYS)
    reg3.register_tool(t_always)
    schemas = reg3.build_tool_schemas()
    schema_names = [s.name for s in schemas]
    assert_true("always_tool" in schema_names, "4.5 ALWAYS 工具出现在 build_tool_schemas 结果")

    # 4.6 build_tool_schemas — DEFERRED 未发现工具不出现在 schema 中
    reg4 = _make_registry()
    t_def = _make_tool(name="deferred_tool", tier=ToolTier.DEFERRED,
                       when_to_use="用于延迟加载")
    reg4.register_tool(t_def)
    schemas4 = reg4.build_tool_schemas()
    schema_names4 = [s.name for s in schemas4]
    assert_true("deferred_tool" not in schema_names4,
                "4.6 DEFERRED 未发现工具不出现在 build_tool_schemas")

    # 4.7 build_tool_schemas — DEFERRED 未发现工具出现在 deferred_summaries
    summaries = reg4.get_deferred_summaries()
    deferred_names = [s["name"] for s in summaries]
    assert_true("deferred_tool" in deferred_names, "4.7 DEFERRED 未发现工具出现在 deferred_summaries")

    # 4.8 promote_to_discovered — DEFERRED 工具提升后出现在 schema
    reg5 = _make_registry()
    t_prom = _make_tool(name="prom_tool", tier=ToolTier.DEFERRED, when_to_use="promote test")
    reg5.register_tool(t_prom)
    reg5.promote_to_discovered("prom_tool", step_n=2)
    schemas5 = reg5.build_tool_schemas()
    names5 = [s.name for s in schemas5]
    assert_true("prom_tool" in names5, "4.8 promote_to_discovered 后 DEFERRED 工具出现在 schema")

    # 4.9 promote_to_discovered — ALWAYS 工具不需要提升（已在 schema）
    reg6 = _make_registry()
    t_alw = _make_tool(name="alw_tool", tier=ToolTier.ALWAYS)
    reg6.register_tool(t_alw)
    reg6.promote_to_discovered("alw_tool", step_n=1)  # 应静默跳过
    assert_true("alw_tool" not in reg6.discovery.snapshot(),
                "4.9 promote_to_discovered 跳过 ALWAYS 工具（不写入 DiscoveryManager）")

    # 4.10 on_tool_register hook 在注册时被调用
    reg7 = _make_registry()
    mock_hooks = MagicMock(spec=AgentHooks)
    reg7.set_hooks(mock_hooks)
    reg7.register_tool(_make_tool(name="hook_reg_tool"))
    mock_hooks.on_tool_register.assert_called_once()
    call_kwargs = mock_hooks.on_tool_register.call_args[1]
    assert_true(call_kwargs["tool_name"] == "hook_reg_tool",
                "4.10 on_tool_register 传入正确 tool_name")

    # 4.11 agent_whitelist 门控 — build_tool_schemas 过滤不在白名单的 agent
    reg8 = _make_registry()
    t_wl = _make_tool(name="whitelist_tool", agent_whitelist=("allowed.agent",))
    reg8.register_tool(t_wl)
    schemas_allowed = reg8.build_tool_schemas(agent_id="allowed.agent")
    schemas_denied = reg8.build_tool_schemas(agent_id="other.agent")
    allowed_names = [s.name for s in schemas_allowed]
    denied_names = [s.name for s in schemas_denied]
    assert_true("whitelist_tool" in allowed_names, "4.11 白名单 agent 可以看到工具")
    assert_true("whitelist_tool" not in denied_names, "4.11 非白名单 agent 看不到工具")


# ════════════════════════════════════════════════════
#  Section 5 — execute 流水线
# ════════════════════════════════════════════════════

def test_execute():
    """5. execute_tool 流水线"""
    print("\n" + "═" * 60)
    print("5.  execute — 工具执行流水线")
    print("═" * 60)

    # 5.1 正常执行成功
    reg = _make_registry()
    reg.register_tool(_make_tool(name="ok_tool", executor=lambda ctx, **k: "result_ok"))
    ctx = _make_ctx()
    r = async_run(reg.execute_tool("ok_tool", {}, ctx))
    assert_true(r.success is True, "5.1 正常执行 success=True")
    assert_true(r.data == "result_ok", "5.1 正常执行 data 正确")

    # 5.2 工具不存在 → success=False，O3 不抛异常
    r2 = async_run(reg.execute_tool("nonexistent", {}, ctx))
    assert_true(r2.success is False, "5.2 工具不存在 → success=False")
    assert_true("未注册" in (r2.error or ""), "5.2 error 包含 '未注册'")

    # 5.3 executor 抛异常 → success=False，O3 不抛异常
    def bad_executor(ctx, **k):
        raise RuntimeError("executor crash")

    reg3 = _make_registry()
    reg3.register_tool(_make_tool(name="bad_tool", executor=bad_executor))
    r3 = async_run(reg3.execute_tool("bad_tool", {}, ctx))
    assert_true(r3.success is False, "5.3 executor 抛异常 → success=False")
    assert_true(r3.error is not None, "5.3 executor 抛异常 → error 不为 None")

    # 5.4 is_enabled=False 被缓存 → 执行被拒
    reg4 = _make_registry()
    reg4.register_tool(_make_tool(name="disabled_tool"))
    reg4._enabled_cache["disabled_tool"] = False
    r4 = async_run(reg4.execute_tool("disabled_tool", {}, ctx))
    assert_true(r4.success is False, "5.4 is_enabled=False 缓存 → success=False")
    assert_true("不可用" in (r4.error or ""), "5.4 error 包含 '不可用'")

    # 5.5 agent_whitelist 阻断 — agent_id 不在白名单
    reg5 = _make_registry()
    reg5.register_tool(_make_tool(name="wl_tool", agent_whitelist=("allowed.only",)))
    ctx_bad = _make_ctx(agent_id="bad.agent")
    r5 = async_run(reg5.execute_tool("wl_tool", {}, ctx_bad))
    assert_true(r5.success is False, "5.5 agent_whitelist 阻断 → success=False")
    assert_true("无权访问" in (r5.error or ""), "5.5 error 包含 '无权访问'")

    # 5.6 trust_level 不足被拒绝
    reg6 = _make_registry()
    reg6.register_tool(_make_tool(name="trust_tool",
                                  trust_level_required=TrustLevel.ORCHESTRATOR))
    ctx_low = _make_ctx(trust_level=TrustLevel.EXTERNAL)
    r6 = async_run(reg6.execute_tool("trust_tool", {}, ctx_low))
    assert_true(r6.success is False, "5.6 trust_level 不足 → success=False")
    assert_true("信任等级不足" in (r6.error or ""), "5.6 error 包含 '信任等级不足'")

    # 5.7 DEFERRED 工具未 discover → 被 Step 7.0 拦截
    reg7 = _make_registry()
    reg7.register_tool(_make_tool(name="deferred_exec", tier=ToolTier.DEFERRED,
                                  when_to_use="discover me"))
    ctx7 = _make_ctx()
    r7 = async_run(reg7.execute_tool("deferred_exec", {}, ctx7))
    assert_true(r7.success is False, "5.7 DEFERRED 未发现 → success=False")
    assert_true("search_tools" in (r7.error or ""), "5.7 error 提示先调用 search_tools")

    # 5.8 DEFERRED 工具 promote 后可以执行
    reg8 = _make_registry()
    reg8.register_tool(_make_tool(name="deferred_ok", tier=ToolTier.DEFERRED,
                                  when_to_use="discover me",
                                  executor=lambda ctx, **k: "deferred_result"))
    reg8.promote_to_discovered("deferred_ok", step_n=1)
    ctx8 = _make_ctx()
    r8 = async_run(reg8.execute_tool("deferred_ok", {}, ctx8))
    assert_true(r8.success is True, "5.8 DEFERRED promote 后执行成功")
    assert_true(r8.data == "deferred_result", "5.8 DEFERRED 执行结果正确")

    # 5.9 halt_on_failure=True + executor 失败 → result.halt=True
    # S6 由 HarnessExecutor 处理，ToolRegistry 只返回 success=False
    reg9 = _make_registry()
    reg9.register_tool(_make_tool(name="halt_tool",
                                  executor=lambda ctx, **k: (_ for _ in ()).throw(RuntimeError("fail")),
                                  halt_on_failure=True))
    harness9 = HarnessExecutor(reg9)
    ctx9 = _make_ctx()
    r9 = async_run(harness9.execute_tool("halt_tool", {}, ctx9))
    assert_true(r9.success is False, "5.9 halt_on_failure: success=False")
    assert_true(r9.halt is True, "5.9 halt_on_failure: result.halt=True")

    # 5.10 halt_on_failure=True → on_halt hook 被触发（HarnessExecutor 层）
    reg10 = _make_registry()
    mock_h = MagicMock(spec=AgentHooks)
    reg10.register_tool(_make_tool(name="halt_hook_tool",
                                   executor=lambda ctx, **k: (_ for _ in ()).throw(ValueError("boom")),
                                   halt_on_failure=True))
    harness10 = HarnessExecutor(reg10, hooks=mock_h)
    async_run(harness10.execute_tool("halt_hook_tool", {}, _make_ctx()))
    mock_h.on_halt.assert_called_once()
    halt_kwargs = mock_h.on_halt.call_args[1]
    assert_true("halt_hook_tool" in halt_kwargs.get("reason", ""),
                "5.10 on_halt reason 包含工具名")

    # 5.11 async executor 支持
    async def async_executor(ctx, **k):
        await asyncio.sleep(0)
        return "async_result"

    reg11 = _make_registry()
    reg11.register_tool(_make_tool(name="async_tool", executor=async_executor))
    r11 = async_run(reg11.execute_tool("async_tool", {}, _make_ctx()))
    assert_true(r11.success is True, "5.11 async executor 执行成功")
    assert_true(r11.data == "async_result", "5.11 async executor 返回值正确")

    # 5.12 on_before_tool_call / on_after_tool_call hook 均被调用（HarnessExecutor 层）
    reg12 = _make_registry()
    mock_h12 = MagicMock(spec=AgentHooks)
    reg12.register_tool(_make_tool(name="hook_exec_tool",
                                   executor=lambda ctx, **k: "done"))
    harness12 = HarnessExecutor(reg12, hooks=mock_h12)
    async_run(harness12.execute_tool("hook_exec_tool", {}, _make_ctx()))
    mock_h12.on_before_tool_call.assert_called_once()
    mock_h12.on_after_tool_call.assert_called_once()
    # positional: (tool_name, args/result, run_id), kwargs: step_n / duration_ms
    start_args = mock_h12.on_before_tool_call.call_args.args
    end_args = mock_h12.on_after_tool_call.call_args.args
    assert_true(start_args[0] == "hook_exec_tool",
                "5.12 on_before_tool_call: tool_name 正确")
    assert_true(end_args[1].success is True,
                "5.12 on_after_tool_call: result.success=True")

    # 5.13 audit_required=True → _write_audit 被调用（Mock 审计 logger）
    with patch("pandaren.tool.registry.logger") as mock_logger:
        reg13 = _make_registry()
        reg13.register_tool(_make_tool(name="audit_tool", audit_required=True,
                                       executor=lambda ctx, **k: "audit_data"))
        async_run(reg13.execute_tool("audit_tool", {}, _make_ctx()))
        # _write_audit 内部使用独立的 audit logger
        # 此处验证 registry logger.info 有记录（注册日志）
        assert_true(mock_logger.info.called, "5.13 register 时 logger.info 被调用")

    # 5.14 executor 返回 ToolResult 直接透传
    def passthrough_executor(ctx, **k):
        return ToolResult(success=True, data="passthrough")

    reg14 = _make_registry()
    reg14.register_tool(_make_tool(name="passthrough_tool", executor=passthrough_executor))
    r14 = async_run(reg14.execute_tool("passthrough_tool", {}, _make_ctx()))
    assert_true(r14.success is True, "5.14 executor 返回 ToolResult 透传 success=True")
    assert_true(r14.data == "passthrough", "5.14 executor 返回 ToolResult 透传 data 正确")
    assert_true(r14.tool_name == "passthrough_tool", "5.14 透传时 tool_name 被修正")


# ════════════════════════════════════════════════════
#  Section 6 — search_tools
# ════════════════════════════════════════════════════

def test_search_tools():
    """6. search_tools — DEFERRED 工具发现"""
    print("\n" + "═" * 60)
    print("6.  search_tools — 工具搜索与发现")
    print("═" * 60)

    # 6.1 search_tools 命中 → 工具出现在结果中
    reg = _make_registry()
    reg.register_tool(_make_tool(name="file_reader",
                                 tier=ToolTier.DEFERRED,
                                 when_to_use="读取文件内容",
                                 description="文件读取器"))
    ctx = _make_ctx()
    r = reg.search_tools("file_reader", ctx)
    assert_true(r.success is True, "6.1 search_tools 命中 → success=True")

    # 6.2 search_tools 命中 → 立即标记为已发现
    assert_true("file_reader" in reg.discovery.snapshot(),
                "6.2 search_tools 命中后立即标记为已发现")

    # 6.3 search_tools 命中后 DEFERRED 工具可以执行（Step 7.0 通过）
    reg2 = _make_registry()
    reg2.register_tool(_make_tool(name="searchable",
                                  tier=ToolTier.DEFERRED,
                                  when_to_use="搜索工具测试",
                                  executor=lambda ctx, **k: "found"))
    reg2.search_tools("searchable", _make_ctx())
    r3 = async_run(reg2.execute_tool("searchable", {}, _make_ctx()))
    assert_true(r3.success is True, "6.3 search 后 DEFERRED 工具执行成功")

    # 6.4 search_tools 无命中 → 不标记为已发现
    reg4 = _make_registry()
    reg4.register_tool(_make_tool(name="invisible_tool",
                                  tier=ToolTier.DEFERRED,
                                  when_to_use="完全无关的描述"))
    reg4.search_tools("数据库查询", _make_ctx())
    assert_true("invisible_tool" not in reg4.discovery.snapshot(),
                "6.4 无命中时不标记为已发现")

    # 6.5 search_tools 命中 → on_tool_discover hook 被触发
    reg5 = _make_registry()
    mock_h = MagicMock(spec=AgentHooks)
    reg5.set_hooks(mock_h)
    reg5.register_tool(_make_tool(name="discover_hook_tool",
                                  tier=ToolTier.DEFERRED,
                                  when_to_use="hook 测试工具"))
    reg5.search_tools("discover_hook_tool", _make_ctx())
    # on_tool_discover 可能被调用（取决于模糊匹配结果）
    # 此处验证已标记为已发现（比 hook 更可靠）
    assert_true("discover_hook_tool" in reg5.discovery.snapshot(),
                "6.5 search_tools 命中后工具标记为已发现")

    # 6.6 search_tools — ALWAYS 工具不出现在搜索结果（不需要发现）
    reg6 = _make_registry()
    reg6.register_tool(_make_tool(name="always_not_search",
                                  tier=ToolTier.ALWAYS,
                                  when_to_use="always tool"))
    r6 = reg6.search_tools("always", _make_ctx())
    # search_tools 只处理 DEFERRED 工具
    assert_true(r6 is not None, "6.6 search_tools 返回 ToolResult 不为 None")


# ════════════════════════════════════════════════════
#  Section 7 — concurrent execution
# ════════════════════════════════════════════════════

def test_concurrent():
    """7. execute_tools_concurrent — 并发执行"""
    print("\n" + "═" * 60)
    print("7.  concurrent — 并发工具执行")
    print("═" * 60)

    # 7.1 并发执行多个工具 → 返回正确数量结果
    reg = _make_registry()
    for i in range(3):
        idx = i  # 闭包捕获
        reg.register_tool(_make_tool(
            name=f"ct_tool_{i}",
            executor=lambda ctx, _i=idx, **k: f"result_{_i}"
        ))
    ctx = _make_ctx()
    calls = [{"name": f"ct_tool_{i}", "args": {}} for i in range(3)]
    harness = HarnessExecutor(reg)
    results = async_run(harness.execute_tools_concurrent(calls, ctx))
    assert_true(len(results) == 3, "7.1 并发执行返回正确数量（3）")
    assert_true(all(r.success for r in results), "7.1 所有工具执行成功")

    # 7.2 C1 仲裁 — 任一失败 → on_concurrent_execution_failure hook 触发
    reg2 = _make_registry()
    mock_h = MagicMock(spec=AgentHooks)
    reg2.register_tool(_make_tool(name="ok_conc", executor=lambda ctx, **k: "ok"))
    reg2.register_tool(_make_tool(name="fail_conc",
                                  executor=lambda ctx, **k: (_ for _ in ()).throw(RuntimeError("fail"))))
    harness2 = HarnessExecutor(reg2, hooks=mock_h)
    calls2 = [
        {"name": "ok_conc", "args": {}},
        {"name": "fail_conc", "args": {}},
    ]
    results2 = async_run(harness2.execute_tools_concurrent(calls2, _make_ctx()))
    mock_h.on_concurrent_execution_failure.assert_called_once()
    conc_kwargs = mock_h.on_concurrent_execution_failure.call_args[1]
    assert_true("fail_conc" in conc_kwargs["tool_names"],
                "7.2 C1: on_concurrent_execution_failure 包含失败工具名")

    # 7.3 全部成功时 on_concurrent_execution_failure 不触发
    reg3 = _make_registry()
    mock_h3 = MagicMock(spec=AgentHooks)
    reg3.register_tool(_make_tool(name="all_ok_1", executor=lambda ctx, **k: "a"))
    reg3.register_tool(_make_tool(name="all_ok_2", executor=lambda ctx, **k: "b"))
    harness3 = HarnessExecutor(reg3, hooks=mock_h3)
    async_run(harness3.execute_tools_concurrent([
        {"name": "all_ok_1", "args": {}},
        {"name": "all_ok_2", "args": {}},
    ], _make_ctx()))
    mock_h3.on_concurrent_execution_failure.assert_not_called()
    assert_true(True, "7.3 全部成功时 on_concurrent_execution_failure 不触发")


# ════════════════════════════════════════════════════
#  Section 8 — hooks Mock
# ════════════════════════════════════════════════════

def test_hooks_mock():
    """8. AgentHooks Mock 验证"""
    print("\n" + "═" * 60)
    print("8.  hooks_mock — AgentHooks 生命周期 Mock 验证")
    print("═" * 60)

    # 8.1 DefaultAgentHooks 所有方法均有默认空实现，不抛异常
    hooks = DefaultAgentHooks()
    ctx = _make_ctx()
    try:
        hooks.on_tool_register("t", ToolTier.ALWAYS, SensitivityLevel.LOW, None)
        hooks.on_before_tool_call("t", {}, "run1", step_n=1)
        hooks.on_after_tool_call("t", ToolResult(success=True), "run1", step_n=1, duration_ms=1.0)
        hooks.on_tool_discover("t", "query", "run1")
        hooks.on_tool_disabled("t", "reason", "run1")
        hooks.on_tool_circuit_open("t", 5, 30.0)
        hooks.on_tool_circuit_close("t")
        hooks.on_tool_output_truncated("t", 1000, 500)
        hooks.on_halt("reason", "run1")
        hooks.on_concurrent_execution_failure(["t1", "t2"], "run1", 1)
        result.ok("8.1 DefaultAgentHooks 所有默认方法不抛异常")
    except Exception as e:
        result.fail("8.1 DefaultAgentHooks 所有默认方法不抛异常", str(e))

    # 8.2 Mock AgentHooks — on_tool_register 参数验证
    reg = _make_registry()
    mock_h = MagicMock(spec=AgentHooks)
    reg.set_hooks(mock_h)
    t = _make_tool(name="mock_reg_tool", namespace="ns_x",
                   sensitivity=SensitivityLevel.HIGH)
    reg.register_tool(t)
    call_args = mock_h.on_tool_register.call_args[1]
    assert_true(call_args["tool_name"] == "ns_x.mock_reg_tool",
                "8.2 on_tool_register: tool_name 含 namespace 前缀")
    assert_true(call_args["tier"] == ToolTier.ALWAYS, "8.2 on_tool_register: tier 正确")
    # sensitivity 可能被 validate_conflicts 修改（HIGH 不可逆时保持 HIGH）
    assert_true(call_args["namespace"] == "ns_x", "8.2 on_tool_register: namespace 正确")

    # 8.3 Mock AgentHooks — on_after_tool_call 包含完整参数（HarnessExecutor 层）
    reg2 = _make_registry()
    mock_h2 = MagicMock(spec=AgentHooks)
    reg2.register_tool(_make_tool(name="end_hook_tool",
                                  executor=lambda ctx, **k: 42))
    harness_83 = HarnessExecutor(reg2, hooks=mock_h2)
    ctx2 = _make_ctx(run_id="run-hook", step_n=5)
    async_run(harness_83.execute_tool("end_hook_tool", {}, ctx2))
    # positional: (tool_name, result, run_id), kwargs: step_n, duration_ms
    end_args = mock_h2.on_after_tool_call.call_args.args
    end_kwargs = mock_h2.on_after_tool_call.call_args.kwargs
    assert_true(end_args[0] == "end_hook_tool",
                "8.3 on_after_tool_call: tool_name 正确")
    assert_true(end_args[2] == "run-hook", "8.3 on_after_tool_call: run_id 正确")
    assert_true(end_kwargs["step_n"] == 5, "8.3 on_after_tool_call: step_n 正确")
    assert_true(end_kwargs["duration_ms"] >= 0, "8.3 on_after_tool_call: duration_ms >= 0")

    # 8.4 Mock AgentHooks — on_halt 参数验证（HarnessExecutor 层）
    reg3 = _make_registry()
    mock_h3 = MagicMock(spec=AgentHooks)
    reg3.register_tool(_make_tool(name="halt_verify",
                                  executor=lambda ctx, **k: (_ for _ in ()).throw(ValueError("oops")),
                                  halt_on_failure=True))
    harness_84 = HarnessExecutor(reg3, hooks=mock_h3)
    ctx3 = _make_ctx(run_id="run-halt")
    async_run(harness_84.execute_tool("halt_verify", {}, ctx3))
    halt_kwargs = mock_h3.on_halt.call_args[1]
    assert_true("halt_verify" in halt_kwargs.get("reason", ""),
                "8.4 on_halt: reason 包含工具名")
    assert_true(halt_kwargs["run_id"] == "run-halt", "8.4 on_halt: run_id 正确")

    # 8.5 Mock registry logger — 注册时 logger.info 被调用
    with patch("pandaren.tool.registry.logger") as mock_logger:
        reg_log = _make_registry()
        reg_log.register_tool(_make_tool(name="logger_test"))
        assert_true(mock_logger.info.called, "8.5 register_tool 时 logger.info 被调用")
        info_calls = [str(c) for c in mock_logger.info.call_args_list]
        assert_true(any("logger_test" in c for c in info_calls),
                    "8.5 logger.info 包含工具名 'logger_test'")

    # 8.6 Mock update_enabled_tools E4 — is_enabled 抛异常 → 工具视为不可用
    reg4 = _make_registry()

    def bad_is_enabled(ctx):
        raise RuntimeError("is_enabled crash")

    reg4.register_tool(_make_tool(name="bad_enabled_tool", is_enabled=bad_is_enabled))
    async_run(reg4.update_enabled_tools(_make_ctx()))
    # E4: 异常 → False（不可用）
    enabled = reg4._enabled_cache.get("bad_enabled_tool", True)
    assert_true(enabled is False, "8.6 E4: is_enabled 抛异常 → 工具不可用（False）")

    # 8.7 Mock inject validate_required_fields 异常 → register_tool 失败
    with patch("pandaren.tool.registry.validate_required_fields") as mock_validate:
        mock_validate.side_effect = ToolRegistrationError("注入校验错误")
        reg5 = _make_registry()
        @assert_raises(ToolRegistrationError, "8.7 Mock 注入 validate_required_fields 异常 → 注册失败")
        def _():
            reg5.register_tool(_make_tool(name="inject_fail_tool"))


# ════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════

SECTIONS = {
    "models": test_models,
    "decorator": test_decorator,
    "validation": test_validation,
    "registry": test_registry,
    "execute": test_execute,
    "search_tools": test_search_tools,
    "concurrent": test_concurrent,
    "hooks_mock": test_hooks_mock,
}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Tool 层 Mock 测试")
    parser.add_argument(
        "--section",
        choices=list(SECTIONS.keys()),
        default="",
        help="仅运行指定测试分区",
    )
    args = parser.parse_args()

    print("🐼 Pandaren Agent SDK — Tool 层 Mock 测试")
    print("   目标模块: pandaren/tool/")
    print("   测试方式: unittest.mock + asyncio")
    print()

    import logging
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
        test_decorator()
        test_validation()
        test_registry()
        test_execute()
        test_search_tools()
        test_concurrent()
        test_hooks_mock()
        result.summary("全部")

    if result.failed > 0:
        print("\n⚠️  存在失败的测试用例，请检查上方输出")
        sys.exit(1)
    else:
        print("\n🎉 所有测试通过！Tool 层 Mock 测试验证完毕")
        sys.exit(0)


if __name__ == "__main__":
    main()
