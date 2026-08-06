"""
Pandaren Agent SDK · Tool 模块真实测试

覆盖约束
--------
  - ToolTier: ALWAYS=1, DEFERRED=2（IntEnum 顺序）
  - SensitivityLevel: LOW < MEDIUM < HIGH < CRITICAL（IntEnum 顺序）
  - CircuitState: CLOSED / OPEN / HALF_OPEN
  - CircuitBreakerConfig: __post_init__ 校验（failure_threshold > 0 等）
  - Tool(frozen): HC2 深拷贝 input_schema/output_schema → MappingProxyType，外部 dict 变化不影响
  - Tool.full_name: 有/无 namespace 分支
  - ToolContext(frozen): permissions frozenset; metadata MappingProxyType; 默认 trust_level
  - ToolResult(NOT frozen): 字段可赋值；DiscoveredToolEntry(frozen)
  - @tool.function 装饰器: 自动生成 input_schema; 第一参数跳过; required vs optional; 返回 Tool
  - validate_required_fields: 缺少 name → ToolRegistrationError
  - validate_conflicts: is_reversible=False + LOW → 自动升级 HIGH + ToolValidationWarning
  - validate_conflicts: CRITICAL + audit_required=False → 自动设 True + ToolValidationWarning
  - validate_conflicts: circuit_breaker.failure_threshold <= 0 → ToolRegistrationError
  - ToolRegistry.register_tool: 注册成功 / 重名 → ToolRegistrationError
  - ToolRegistry.set_hooks: HC4 第二次 → RuntimeError
  - ToolRegistry.execute_tool: O3（永不抛异常）; DEFERRED 门控（未发现 → 失败）
  - ToolRegistry.search_tools: DEFERRED-only / 关键词模糊 / 写入 DiscoveryManager
  - 集成: 真实 LLM + AgentBuilder + 工具注册 + 工具调用

运行方式
--------
  cd pandaren/tool/tests && python test_tool.py
  cd pandaren/tool/tests && python test_tool.py --section types
  cd pandaren/tool/tests && python test_tool.py --section tool_model
  cd pandaren/tool/tests && python test_tool.py --section tool_context
  cd pandaren/tool/tests && python test_tool.py --section tool_result
  cd pandaren/tool/tests && python test_tool.py --section decorator
  cd pandaren/tool/tests && python test_tool.py --section registry
  cd pandaren/tool/tests && python test_tool.py --section integration
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import io
import warnings
from typing import Optional

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
from pandaren.tool.types import (
    ToolTier, SensitivityLevel, CircuitState, CircuitBreakerConfig,
)
from pandaren.tool import (
    Tool, ToolContext, ToolResult, DiscoveredToolEntry, ToolSchema, ToolSearchResult,
)
from pandaren.tool.decorator import tool as tool_ns
from pandaren.tool.exceptions import ToolRegistrationError, ToolValidationWarning
from pandaren.tool.registry.validator import validate_required_fields, validate_conflicts
from pandaren.tool.registry import ToolRegistry, create_tool_registry
from pandaren.identity.models import SensitivePermission, PERMISSION_ALL, TrustLevel
from pandaren.builder import AgentBuilder
from pandaren.llm.client import OpenAICompatibleClient
from pandaren.engine.stream import StreamEventType


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


def _make_minimal_tool(
    name: str = "test_tool",
    description: str = "测试用工具",
    tier: ToolTier = ToolTier.ALWAYS,
    sensitivity: SensitivityLevel = SensitivityLevel.LOW,
    namespace: str | None = None,
    is_reversible: bool = True,
    audit_required: bool = False,
    is_idempotent: bool = True,
    circuit_breaker: CircuitBreakerConfig | None = None,
    when_to_use: str = "测试场景下使用",
) -> Tool:
    """构建最小化 Tool 实例（executor 为空函数）。"""
    async def _executor(ctx=None, **kwargs):
        return "ok"

    return Tool(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "输入参数"},
            },
            "required": ["query"],
        },
        executor=_executor,
        tier=tier,
        sensitivity=sensitivity,
        is_reversible=is_reversible,
        audit_required=audit_required,
        is_idempotent=is_idempotent,
        namespace=namespace,
        circuit_breaker=circuit_breaker,
        when_to_use=when_to_use,
    )


# ════════════════════════════════════════════════════
#  1. 枚举与类型测试（ToolTier / SensitivityLevel / CircuitState / CircuitBreakerConfig）
# ════════════════════════════════════════════════════

def test_types():
    print("\n" + "═" * 60)
    print("1️⃣  枚举与类型测试")
    print("═" * 60)

    # ── ToolTier ──
    print("\n  · ToolTier 枚举")
    assert_true(ToolTier.ALWAYS == 1, "ALWAYS == 1")
    assert_true(ToolTier.DEFERRED == 2, "DEFERRED == 2")
    assert_true(ToolTier.ALWAYS < ToolTier.DEFERRED, "ALWAYS < DEFERRED（IntEnum 排序）")
    assert_true(len(ToolTier) == 2, "ToolTier 共 2 个成员")

    # ── SensitivityLevel ──
    print("\n  · SensitivityLevel 枚举")
    assert_true(SensitivityLevel.LOW == 1, "LOW == 1")
    assert_true(SensitivityLevel.MEDIUM == 2, "MEDIUM == 2")
    assert_true(SensitivityLevel.HIGH == 3, "HIGH == 3")
    assert_true(SensitivityLevel.CRITICAL == 4, "CRITICAL == 4")
    assert_true(
        SensitivityLevel.LOW < SensitivityLevel.MEDIUM < SensitivityLevel.HIGH < SensitivityLevel.CRITICAL,
        "LOW < MEDIUM < HIGH < CRITICAL 顺序正确",
    )

    # ── CircuitState ──
    print("\n  · CircuitState 枚举")
    assert_true(CircuitState.CLOSED == 1, "CLOSED == 1")
    assert_true(CircuitState.OPEN == 2, "OPEN == 2")
    assert_true(CircuitState.HALF_OPEN == 3, "HALF_OPEN == 3")

    # ── CircuitBreakerConfig 合法构造 ──
    print("\n  · CircuitBreakerConfig 合法构造")

    @assert_no_raises("CircuitBreakerConfig 合法参数不抛异常")
    def _():
        CircuitBreakerConfig(
            failure_threshold=3,
            recovery_timeout=10.0,
            max_recovery_timeout=60.0,
        )

    # ── CircuitBreakerConfig 非法：failure_threshold <= 0 ──
    @assert_raises(ValueError, "failure_threshold=0 → ValueError")
    def _():
        CircuitBreakerConfig(
            failure_threshold=0,
            recovery_timeout=10.0,
            max_recovery_timeout=60.0,
        )

    @assert_raises(ValueError, "failure_threshold=-1 → ValueError")
    def _():
        CircuitBreakerConfig(
            failure_threshold=-1,
            recovery_timeout=10.0,
            max_recovery_timeout=60.0,
        )

    # ── CircuitBreakerConfig 非法：recovery_timeout <= 0 ──
    @assert_raises(ValueError, "recovery_timeout=0 → ValueError")
    def _():
        CircuitBreakerConfig(
            failure_threshold=3,
            recovery_timeout=0,
            max_recovery_timeout=60.0,
        )

    # ── CircuitBreakerConfig 非法：max_recovery_timeout < recovery_timeout ──
    @assert_raises(ValueError, "max_recovery_timeout < recovery_timeout → ValueError")
    def _():
        CircuitBreakerConfig(
            failure_threshold=3,
            recovery_timeout=30.0,
            max_recovery_timeout=10.0,
        )

    # ── CircuitBreakerConfig frozen ──
    print("\n  · CircuitBreakerConfig 不可变")
    cfg = CircuitBreakerConfig(failure_threshold=5, recovery_timeout=5.0, max_recovery_timeout=60.0)

    @assert_raises(dataclasses.FrozenInstanceError, "CircuitBreakerConfig 不可变（FrozenInstanceError）")
    def _():
        cfg.failure_threshold = 99  # type: ignore[misc]


# ════════════════════════════════════════════════════
#  2. Tool 数据模型测试
# ════════════════════════════════════════════════════

def test_tool_model():
    print("\n" + "═" * 60)
    print("2️⃣  Tool 数据模型测试")
    print("═" * 60)

    # ── 合法构造 ──
    print("\n  · 合法构造")

    @assert_no_raises("_make_minimal_tool() 不抛异常")
    def _():
        _make_minimal_tool()

    t = _make_minimal_tool()
    assert_true(t.name == "test_tool", "name 字段正确")
    assert_true(t.description == "测试用工具", "description 字段正确")
    assert_true(t.tier == ToolTier.ALWAYS, "tier 字段正确")

    # ── full_name：无 namespace ──
    print("\n  · full_name 属性")
    t_no_ns = _make_minimal_tool(name="my_tool", namespace=None)
    assert_true(t_no_ns.full_name == "my_tool", "无 namespace 时 full_name == name")

    t_with_ns = _make_minimal_tool(name="my_tool", namespace="utils")
    assert_true(t_with_ns.full_name == "utils.my_tool", "有 namespace 时 full_name == 'ns.name'")

    # ── HC2: input_schema → MappingProxyType（外部 dict 可安全修改）──
    print("\n  · HC2: input_schema 深拷贝保护")
    original_schema = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
    }
    t2 = Tool(
        name="schema_test",
        description="schema 保护测试",
        input_schema=original_schema,
        executor=lambda ctx=None, **kw: "ok",
        tier=ToolTier.ALWAYS,
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        when_to_use="schema 保护测试场景",
    )
    # 修改原始 dict
    original_schema["properties"]["injected"] = {"type": "string"}
    original_schema["required"].append("injected")
    # Tool 内部的 schema 不受影响
    assert_true(
        "injected" not in t2.input_schema.get("properties", {}),
        "HC2: 外部修改原始 dict 不影响 Tool.input_schema",
    )
    from types import MappingProxyType
    assert_true(
        isinstance(t2.input_schema, MappingProxyType),
        "Tool.input_schema 是 MappingProxyType",
    )

    # ── Tool frozen ──
    print("\n  · Tool 不可变（frozen）")

    @assert_raises(dataclasses.FrozenInstanceError, "Tool 直接赋值 → FrozenInstanceError")
    def _():
        t.name = "hacked"  # type: ignore[misc]

    # ── HC2: output_schema → MappingProxyType（与 input_schema 对称）──
    print("\n  · HC2: output_schema 深拷贝保护")
    original_out_schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
    }
    t_out = Tool(
        name="out_schema_test",
        description="output_schema 保护测试",
        input_schema={"type": "object", "properties": {}},
        output_schema=original_out_schema,
        executor=lambda ctx=None, **kw: "ok",
        tier=ToolTier.ALWAYS,
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        when_to_use="output_schema 保护测试场景",
    )
    # 修改原始 output_schema dict
    original_out_schema["properties"]["injected"] = {"type": "string"}
    # Tool 内部的 output_schema 不受影响
    assert_true(
        "injected" not in t_out.output_schema.get("properties", {}),
        "HC2: 外部修改原始 dict 不影响 Tool.output_schema",
    )
    from types import MappingProxyType as _MappingProxyType
    assert_true(
        isinstance(t_out.output_schema, _MappingProxyType),
        "Tool.output_schema 是 MappingProxyType",
    )


# ════════════════════════════════════════════════════
#  3. ToolContext 测试
# ════════════════════════════════════════════════════

def test_tool_context():
    print("\n" + "═" * 60)
    print("3️⃣  ToolContext 测试")
    print("═" * 60)

    # ── 默认值 ──
    print("\n  · 默认值")
    ctx = ToolContext(run_id="r1", step_n=1, agent_id="agent.test")
    assert_true(ctx.run_id == "r1", "run_id 正确")
    assert_true(ctx.step_n == 1, "step_n 正确")
    assert_true(ctx.agent_id == "agent.test", "agent_id 正确")
    assert_true(ctx.trust_level == TrustLevel.SUB_AGENT, "trust_level 默认 SUB_AGENT")

    # ── permissions 是 frozenset ──
    print("\n  · permissions 是 frozenset")
    ctx2 = ToolContext(
        run_id="r2",
        step_n=2,
        agent_id="agent.test",
        permissions=frozenset(["file:read", "web:search"]),
    )
    assert_true(isinstance(ctx2.permissions, frozenset), "permissions 是 frozenset")
    assert_true("file:read" in ctx2.permissions, "permissions 包含 'file:read'")

    # ── metadata 是 MappingProxyType ──
    print("\n  · metadata 是 MappingProxyType")
    from types import MappingProxyType
    ctx3 = ToolContext(
        run_id="r3",
        step_n=3,
        agent_id="agent.test",
        metadata=MappingProxyType({"key": "val"}),
    )
    assert_true(isinstance(ctx3.metadata, MappingProxyType), "metadata 是 MappingProxyType")
    assert_true(ctx3.metadata.get("key") == "val", "metadata 可读取")

    # ── frozen ──
    print("\n  · ToolContext 不可变（frozen）")

    @assert_raises(dataclasses.FrozenInstanceError, "ToolContext 直接赋值 → FrozenInstanceError")
    def _():
        ctx.run_id = "hacked"  # type: ignore[misc]


# ════════════════════════════════════════════════════
#  4. ToolResult / DiscoveredToolEntry 测试
# ════════════════════════════════════════════════════

def test_tool_result():
    print("\n" + "═" * 60)
    print("4️⃣  ToolResult / DiscoveredToolEntry 测试")
    print("═" * 60)

    # ── ToolResult 可变 ──
    print("\n  · ToolResult 可变（NOT frozen）")
    tr = ToolResult(success=True, data="hello", tool_name="my_tool")
    assert_true(tr.success is True, "ToolResult.success 初始为 True")
    assert_true(tr.data == "hello", "ToolResult.data 初始正确")

    @assert_no_raises("ToolResult 字段可直接赋值（NOT frozen）")
    def _():
        tr.success = False
        tr.data = "updated"
        tr.error = "some error"

    assert_true(tr.success is False, "ToolResult.success 赋值后为 False")
    assert_true(tr.data == "updated", "ToolResult.data 赋值后更新")
    assert_true(tr.error == "some error", "ToolResult.error 赋值后更新")

    # ── ToolResult 默认值 ──
    print("\n  · ToolResult 默认值")
    tr_default = ToolResult(success=False)
    assert_true(tr_default.data == "", "data 默认 \"\"")
    assert_true(tr_default.error == "", "error 默认 \"\"")
    assert_true(tr_default.halt is False, "halt 默认 False")
    assert_true(tr_default.deduplicated is False, "deduplicated 默认 False")
    assert_true(tr_default.truncated is False, "truncated 默认 False")
    assert_true(tr_default.tool_name == "", "tool_name 默认空字符串")
    assert_true(tr_default.duration_ms == 0.0, "duration_ms 默认 0.0")

    # ── DiscoveredToolEntry frozen ──
    print("\n  · DiscoveredToolEntry 不可变（frozen）")
    dte = DiscoveredToolEntry(name="discovered_tool", turn=3)

    @assert_raises(dataclasses.FrozenInstanceError, "DiscoveredToolEntry 直接赋值 → FrozenInstanceError")
    def _():
        dte.name = "hacked"  # type: ignore[misc]

    assert_true(dte.name == "discovered_tool", "name 字段正确")
    assert_true(dte.turn == 3, "turn 字段正确")


# ════════════════════════════════════════════════════
#  5. @tool.function 装饰器测试
# ════════════════════════════════════════════════════

def test_decorator():
    print("\n" + "═" * 60)
    print("5️⃣  @tool.function 装饰器测试")
    print("═" * 60)

    # ── 基本：返回 Tool 实例 ──
    print("\n  · 装饰器返回 Tool 实例")

    @tool_ns.function(
        tier=ToolTier.ALWAYS,
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        description="计算两数之和",
        when_to_use="需要进行整数加法时使用",
    )
    def add(a: int, b: int) -> str:
        """将 a 和 b 相加并返回结果字符串。

        Args:
            a: 第一个整数
            b: 第二个整数
        """
        return str(a + b)

    assert_true(isinstance(add, Tool), "装饰器返回 Tool 实例")
    assert_true(add.name == "add", "name 自动取函数名")
    assert_true("a" in add.input_schema.get("properties", {}), "参数 a 在 input_schema.properties")
    assert_true("b" in add.input_schema.get("properties", {}), "参数 b 在 input_schema.properties")
    assert_true("a" in add.input_schema.get("required", []), "参数 a 在 required（无默认值）")
    assert_true("b" in add.input_schema.get("required", []), "参数 b 在 required（无默认值）")

    # ── 第一参数为 ToolContext 时跳过 ──
    print("\n  · 第一参数为 ToolContext 时自动跳过")

    @tool_ns.function(
        tier=ToolTier.ALWAYS,
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        description="带 ToolContext 的工具",
        when_to_use="测试 ToolContext 参数跳过行为时使用",
    )
    def ctx_tool(ctx: ToolContext, query: str) -> str:
        """带上下文的查询工具。

        Args:
            ctx: 工具上下文（自动跳过）
            query: 查询字符串
        """
        return query

    assert_true(isinstance(ctx_tool, Tool), "返回 Tool 实例")
    props = ctx_tool.input_schema.get("properties", {})
    assert_true("ctx" not in props, "ToolContext 参数 ctx 被跳过（不出现在 schema）")
    assert_true("query" in props, "query 参数出现在 schema")

    # ── 第一参数名为 ctx / context 时跳过 ──
    print("\n  · 第一参数名为 'ctx' 时自动跳过（无类型注解）")

    @tool_ns.function(
        tier=ToolTier.ALWAYS,
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        description="ctx 参数名跳过测试",
        when_to_use="测试 ctx 参数名自动跳过时使用",
    )
    def ctx_name_tool(ctx, value: str) -> str:
        """通过参数名跳过 ctx。

        Args:
            ctx: 上下文
            value: 值
        """
        return value

    props2 = ctx_name_tool.input_schema.get("properties", {})
    assert_true("ctx" not in props2, "'ctx' 参数名被自动跳过")
    assert_true("value" in props2, "value 参数出现在 schema")

    # ── optional 参数（有默认值）不进入 required ──
    print("\n  · optional 参数不进入 required 列表")

    @tool_ns.function(
        tier=ToolTier.ALWAYS,
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        description="含可选参数的工具",
        when_to_use="测试可选参数处理时使用",
    )
    def optional_tool(required_param: str, optional_param: str = "default") -> str:
        """含可选参数的工具。

        Args:
            required_param: 必填参数
            optional_param: 可选参数（有默认值）
        """
        return required_param + optional_param

    required_list = optional_tool.input_schema.get("required", [])
    assert_true("required_param" in required_list, "required_param 在 required 列表")
    assert_true("optional_param" not in required_list, "optional_param（有默认值）不在 required 列表")

    # ── DEFERRED 工具 ──
    print("\n  · DEFERRED tier 工具")

    @tool_ns.function(
        tier=ToolTier.DEFERRED,
        sensitivity=SensitivityLevel.MEDIUM,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        description="延迟加载工具",
        when_to_use="需要进行网络搜索时使用（DEFERRED 延迟加载）",
    )
    def deferred_tool(query: str) -> str:
        """网络搜索工具（DEFERRED）。

        Args:
            query: 搜索关键词
        """
        return f"result for {query}"

    assert_true(deferred_tool.tier == ToolTier.DEFERRED, "tier == DEFERRED")


# ════════════════════════════════════════════════════
#  6. ToolRegistry 测试
# ════════════════════════════════════════════════════

def test_registry():
    print("\n" + "═" * 60)
    print("6️⃣  ToolRegistry 测试")
    print("═" * 60)

    # ── 注册成功 ──
    print("\n  · 注册成功")
    reg = create_tool_registry()
    t1 = _make_minimal_tool(name="tool_a")

    @assert_no_raises("register_tool 合法工具不抛异常")
    def _():
        reg.register_tool(t1)

    tools_list = reg.list_tools()
    assert_true(any(t.name == "tool_a" for t in tools_list), "注册后 list_tools 包含 tool_a")

    # ── 重名 → ToolRegistrationError ──
    print("\n  · 重名注册 → ToolRegistrationError")
    t1_dup = _make_minimal_tool(name="tool_a")

    @assert_raises(ToolRegistrationError, "重名 tool_a → ToolRegistrationError")
    def _():
        reg.register_tool(t1_dup)

    # ── validate_required_fields: name 为空 → ToolRegistrationError ──
    print("\n  · validate_required_fields: 缺 name → ToolRegistrationError")

    @assert_raises(ToolRegistrationError, "name='' → ToolRegistrationError")
    def _():
        empty_name_tool = _make_minimal_tool(name="")
        # 直接调用 validate_required_fields（等同于 registry 内部校验）
        validate_required_fields(empty_name_tool)

    # ── validate_conflicts: is_reversible=False + LOW → 升级 HIGH + 警告 ──
    print("\n  · validate_conflicts: is_reversible=False + LOW → 升级 HIGH + ToolValidationWarning")
    t_irreversible = _make_minimal_tool(
        name="irrev_tool",
        sensitivity=SensitivityLevel.LOW,
        is_reversible=False,
    )
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        upgraded = validate_conflicts(t_irreversible)

    assert_true(upgraded.sensitivity == SensitivityLevel.HIGH, "is_reversible=False+LOW → 升级为 HIGH")
    assert_true(
        any(issubclass(warning.category, ToolValidationWarning) for warning in w),
        "is_reversible=False+LOW → 发出 ToolValidationWarning",
    )

    # ── validate_conflicts: CRITICAL + audit_required=False → 强制 True + 警告 ──
    print("\n  · validate_conflicts: CRITICAL + audit_required=False → 强制 audit_required=True")
    t_critical = _make_minimal_tool(
        name="critical_tool",
        sensitivity=SensitivityLevel.CRITICAL,
        audit_required=False,
        is_reversible=True,  # reversible=True 避免触发第一条规则
    )
    with warnings.catch_warnings(record=True) as w2:
        warnings.simplefilter("always")
        upgraded2 = validate_conflicts(t_critical)

    assert_true(upgraded2.audit_required is True, "CRITICAL + audit_required=False → 强制 True")
    assert_true(
        any(issubclass(warning.category, ToolValidationWarning) for warning in w2),
        "CRITICAL+audit_required=False → 发出 ToolValidationWarning",
    )

    # ── validate_conflicts: circuit_breaker.failure_threshold <= 0 → ToolRegistrationError ──
    print("\n  · validate_conflicts: circuit_breaker.failure_threshold <= 0 → ToolRegistrationError")

    @assert_raises(ToolRegistrationError, "circuit_breaker failure_threshold<=0 → ToolRegistrationError")
    def _():
        # CircuitBreakerConfig 本身校验 failure_threshold>0，所以先绕过构造
        # 通过 dataclasses.replace 注入非法 config 来测 validate_conflicts
        # 实际上 CircuitBreakerConfig 的 __post_init__ 会先拦截，所以测注册路径
        bad_cfg = CircuitBreakerConfig.__new__(CircuitBreakerConfig)
        object.__setattr__(bad_cfg, "failure_threshold", 0)
        object.__setattr__(bad_cfg, "recovery_timeout", 10.0)
        object.__setattr__(bad_cfg, "max_recovery_timeout", 60.0)
        t_bad_cb = _make_minimal_tool(name="bad_cb_tool", circuit_breaker=bad_cfg)
        validate_conflicts(t_bad_cb)

    # ── set_hooks HC4：第二次 → RuntimeError ──
    print("\n  · set_hooks HC4: 第二次调用 → RuntimeError")

    class _DummyHooks:
        pass

    reg2 = create_tool_registry()

    @assert_no_raises("set_hooks 第一次不抛异常")
    def _():
        reg2.set_hooks(_DummyHooks())

    @assert_raises(RuntimeError, "set_hooks 第二次 → RuntimeError（HC4）")
    def _():
        reg2.set_hooks(_DummyHooks())

    # ── execute_tool O3：永不抛异常（不存在的工具名也返回 ToolResult）──
    print("\n  · execute_tool O3: 不存在工具名 → 返回失败 ToolResult，不抛异常")

    @assert_no_raises("execute_tool 不存在工具名不抛异常（O3）")
    def _():
        ctx = ToolContext(run_id="r1", step_n=1, agent_id="test.agent")
        tool_result = asyncio.run(
            create_tool_registry().execute_tool(
                tool_name="nonexistent_tool",
                args={"query": "test"},
                context=ctx,
            )
        )
        assert not tool_result.success, "不存在工具 → success=False"

    # ── DEFERRED 门控：未经 search_tools → 失败 ──
    print("\n  · DEFERRED 门控: 未 search_tools → execute_tool 失败")

    async def _run_deferred_blocked():
        reg3 = create_tool_registry()

        @tool_ns.function(
            tier=ToolTier.DEFERRED,
            sensitivity=SensitivityLevel.LOW,
            sensitive_permission=SensitivePermission.DATA_WRITE,
            is_reversible=True,
            audit_required=False,
            is_idempotent=True,
            description="延迟工具（门控测试）",
            when_to_use="DEFERRED 门控测试场景",
        )
        def deferred_gate_tool(query: str) -> str:
            """延迟工具。

            Args:
                query: 查询
            """
            return f"result: {query}"

        reg3.register_tool(deferred_gate_tool)
        ctx = ToolContext(run_id="r1", step_n=1, agent_id="test.agent")
        tr = await reg3.execute_tool(
            tool_name="deferred_gate_tool",
            args={"query": "hello"},
            context=ctx,
        )
        return tr

    tr_blocked = asyncio.run(_run_deferred_blocked())
    assert_true(not tr_blocked.success, "DEFERRED 未发现 → execute_tool 失败（门控阻断）")

    # ── DEFERRED 门控：经 search_tools 后可执行 ──
    print("\n  · DEFERRED 门控: search_tools 后 → execute_tool 成功")

    async def _run_deferred_unblocked():
        reg4 = create_tool_registry()

        @tool_ns.function(
            tier=ToolTier.DEFERRED,
            sensitivity=SensitivityLevel.LOW,
            sensitive_permission=SensitivePermission.DATA_WRITE,
            is_reversible=True,
            audit_required=False,
            is_idempotent=True,
            description="网络搜索工具（DEFERRED 解锁测试）",
            when_to_use="DEFERRED 解锁后搜索测试场景",
        )
        def search_test_tool(ctx: ToolContext, query: str) -> str:
            """搜索测试工具。

            Args:
                query: 搜索词
            """
            return f"搜索结果: {query}"

        reg4.register_tool(search_test_tool)
        ctx = ToolContext(run_id="r2", step_n=1, agent_id="test.agent")
        # 先执行 search_tools，同步标记为已发现
        reg4.search_tools(tool_name="search_test_tool", context=ctx)
        tr = await reg4.execute_tool(
            tool_name="search_test_tool",
            args={"query": "hello"},
            context=ctx,
        )
        return tr

    tr_unblocked = asyncio.run(_run_deferred_unblocked())
    assert_true(tr_unblocked.success, "DEFERRED 经 search_tools 后 → execute_tool 成功")

    # ── search_tools：只搜 DEFERRED 工具 ──
    print("\n  · search_tools: 只返回 DEFERRED 工具（data 包含工具名）")
    reg5 = create_tool_registry()
    t_always = _make_minimal_tool(name="always_tool", tier=ToolTier.ALWAYS)

    @tool_ns.function(
        tier=ToolTier.DEFERRED,
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        description="可被搜索到的延迟工具",
        when_to_use="搜索工具时可发现此 DEFERRED 工具",
    )
    def searchable_deferred(query: str) -> str:
        """可搜索 DEFERRED 工具。

        Args:
            query: 关键词
        """
        return query

    reg5.register_tool(t_always)
    reg5.register_tool(searchable_deferred)
    ctx5 = ToolContext(run_id="r5", step_n=1, agent_id="test.agent")
    search_tr = reg5.search_tools(tool_name="searchable_deferred", context=ctx5)
    assert_true(search_tr.success, "search_tools 返回 ToolResult.success=True")
    assert_true(
        "searchable_deferred" in str(search_tr.data),
        "search_tools.data 包含 DEFERRED 工具名",
    )
    assert_true(
        "always_tool" not in str(search_tr.data),
        "search_tools.data 不包含 ALWAYS 工具名",
    )
    # 验证工具已被标记为已发现
    assert_true(
        "searchable_deferred" in reg5.discovery.snapshot(),
        "search_tools 后工具已标记为已发现",
    )

    # ── execute_tool O3：工具内部抛异常也返回 ToolResult ──
    print("\n  · execute_tool O3: 工具内部抛异常 → 返回失败 ToolResult，不向上抛")

    async def _run_exception_tool():
        reg6 = create_tool_registry()

        async def _boom(ctx=None, **kwargs):
            raise RuntimeError("内部爆炸！")

        boom_tool = Tool(
            name="boom_tool",
            description="会爆炸的工具",
            input_schema={"type": "object", "properties": {}, "required": []},
            executor=_boom,
            tier=ToolTier.ALWAYS,
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=False,
            is_idempotent=True,
            when_to_use="测试工具内部异常处理时使用",
        )
        reg6.register_tool(boom_tool)
        ctx = ToolContext(run_id="r_exc", step_n=1, agent_id="test.agent")
        tr = await reg6.execute_tool(
            tool_name="boom_tool",
            args={},
            context=ctx,
        )
        return tr

    @assert_no_raises("execute_tool 内部爆炸不向上抛（O3）")
    def _():
        tr_exc = asyncio.run(_run_exception_tool())
        assert not tr_exc.success, "内部异常 → success=False"


# ════════════════════════════════════════════════════
#  7. 集成测试（真实 LLM + 工具调用）
# ════════════════════════════════════════════════════

def test_integration():
    print("\n" + "═" * 60)
    print("7️⃣  集成测试（真实 LLM + 工具调用）")
    print("═" * 60)

    llm_client = _make_llm_client()
    api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        result.fail("集成测试跳过", "未配置 API Key（DASHSCOPE_API_KEY / OPENAI_API_KEY）")
        return

    # ── 构建一个带工具的 Agent ──
    print("\n  · 构建带工具的 Agent")

    @tool_ns.function(
        tier=ToolTier.ALWAYS,
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        description="整数加法计算器，返回 a+b 的结果",
        when_to_use="需要计算两整数之和时调用",
    )
    def calc_add(ctx: ToolContext, a: int, b: int) -> str:
        """计算两整数之和。

        Args:
            a: 第一个整数
            b: 第二个整数
        """
        return f"{a + b}"

    @tool_ns.function(
        tier=ToolTier.ALWAYS,
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        description="返回当前时间戳（秒级）",
        when_to_use="需要获取当前时间戳时调用",
    )
    def get_timestamp(ctx: ToolContext) -> str:
        """获取当前时间戳。"""
        import time
        return str(int(time.time()))

    agent = (
        AgentBuilder()
        .identity(
            agent_id="test.tool.integration.v1",
            agent_name="工具集成测试 Agent",
            when_to_use="测试工具调用",
            sensitive_permissions=PERMISSION_ALL,
            trust_level=TrustLevel.SUB_AGENT,
        )
        .llm(llm_client)
        .tools([calc_add, get_timestamp])
        .system_prompt(
            "你是一个能够使用工具的助手。"
            "当用户需要计算时，请使用 calc_add 工具完成加法运算。"
            "当用户需要时间时，请使用 get_timestamp 工具。"
            "直接返回工具结果，不要额外解释。"
        )
        .behavior(max_steps=5)
        .build()
    )

    assert_true(agent is not None, "Agent 构建成功（含工具注册）")
    assert_true(agent.agent_id == "test.tool.integration.v1", "agent_id 正确")

    # ── 非流式调用：工具加法 ──
    print("\n  · 非流式调用：请 LLM 使用 calc_add 工具计算 37 + 58")

    async def _run_calc():
        result_obj = await agent.run(
            "请用 calc_add 工具计算 37 加 58 等于多少，直接告诉我数字结果。",
            session_id="tool_test_session",
        )
        return result_obj

    calc_result = asyncio.run(_run_calc())
    assert_true(calc_result.success, f"非流式调用成功（success=True）: {calc_result.error}")
    assert_true(calc_result.output is not None, "output 非空")
    assert_true("95" in str(calc_result.output), f"output 包含正确结果 95，实际: {calc_result.output}")
    print(f"   LLM 回答: {str(calc_result.output)[:120]}")
    print(f"   总步数: {calc_result.total_steps}  Token: {calc_result.total_input_tokens}→{calc_result.total_output_tokens}")

    # ── 流式调用：工具加法 ──
    print("\n  · 流式调用：请 LLM 使用 calc_add 工具计算 100 + 200")
    tool_called = False
    tool_success = False
    final_output = ""

    async def _run_stream_calc():
        nonlocal tool_called, tool_success, final_output
        async for event in agent.run_stream(
            "请用 calc_add 工具计算 100 加 200 等于多少，只返回数字。",
            session_id="tool_stream_session",
        ):
            if event.type == StreamEventType.TOOL_CALL_START:
                if event.tool_name == "calc_add":
                    tool_called = True
            elif event.type == StreamEventType.TOOL_CALL_END:
                if event.tool_name == "calc_add":
                    tool_success = event.data.get("success", False)
            elif event.type == StreamEventType.LLM_TOKEN:
                final_output = event.data.get("snapshot", final_output)
            elif event.type == StreamEventType.RUN_END:
                final_output = str(event.data.get("output", final_output))

    asyncio.run(_run_stream_calc())
    assert_true(tool_called, "流式调用中 TOOL_CALL_START(calc_add) 被触发")
    assert_true(tool_success, "流式调用中 calc_add 工具执行成功")
    assert_true("300" in final_output, f"流式调用最终输出包含 300，实际: {final_output[:80]}")
    print(f"   流式最终输出: {final_output[:120]}")

    # ── aclose 幂等 ──
    print("\n  · aclose 幂等性")

    @assert_no_raises("aclose 第一次不抛异常")
    def _():
        asyncio.run(agent.aclose())

    @assert_no_raises("aclose 第二次不抛异常（幂等）")
    def _():
        asyncio.run(agent.aclose())


# ════════════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════════════

SECTIONS = {
    "types": test_types,
    "tool_model": test_tool_model,
    "tool_context": test_tool_context,
    "tool_result": test_tool_result,
    "decorator": test_decorator,
    "registry": test_registry,
    "integration": test_integration,
}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Pandaren Tool 模块真实测试")
    parser.add_argument(
        "--section",
        choices=list(SECTIONS.keys()),
        default=None,
        help="只运行指定 section（默认全部）",
    )
    args = parser.parse_args()

    global result
    overall = TestResult()

    sections_to_run = (
        {args.section: SECTIONS[args.section]}
        if args.section
        else SECTIONS
    )

    for section_name, section_fn in sections_to_run.items():
        section_result = TestResult()
        old_result = result
        result = section_result
        try:
            section_fn()
        except Exception as exc:
            section_result.fail(f"[{section_name}] 未捕获异常", str(exc))
        finally:
            result = old_result

        section_result.summary(section_name)
        overall.passed += section_result.passed
        overall.failed += section_result.failed
        overall.errors.extend(section_result.errors)

    if len(sections_to_run) > 1:
        print("\n" + "═" * 60)
        print("📋 全部 Section 汇总")
        overall.summary("ALL")

    sys.exit(0 if overall.failed == 0 else 1)


if __name__ == "__main__":
    main()
