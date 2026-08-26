"""pandaren/tool/tests/test_facade.py — ToolRegistry Facade 端到端链路测试。

风险映射：
  Risk-1（P0）：execute_tool 永远返回 ToolResult（未注册/门控拒绝/执行异常均不抛）
  Risk-2（P0）：DEFERRED 工具成功执行 → 写入 discovery（首次 discover / 再次 update_step 刷新）
  Risk-3（P1）：DiscoveryGuard 拦截未发现的 DEFERRED 工具（必须先 search_tools 加载）
  Risk-4（P1）：ALWAYS 工具执行不写 discovery
  Risk-5（P1）：facade 参数清洗（filter+coerce）→ 多余参数不导致 schema 校验失败
  Risk-6（P2）：jsonschema 校验失败路径返回失败 ToolResult（jsonschema 未装则跳过）
  Risk-7（P2）：agent_whitelist / trust_level 门控拒绝
"""

from __future__ import annotations

import hashlib

import pytest

from pandaren.identity.models import TrustLevel
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.facade import ToolRegistry
from pandaren.tool.types import SensitivityLevel, ToolTier

from .conftest import make_ctx, make_tool


class TestExecuteNotFound:
    async def test_unknown_tool_returns_failure_result(self):
        # Risk-1
        reg = ToolRegistry()
        result = await reg.execute_tool("nope", {}, make_ctx())
        assert result.success is False
        assert "未注册" in result.error


class TestDiscoveryFlow:
    async def test_deferred_unfound_blocked_by_guard(self):
        # Risk-3：未发现 DEFERRED → DiscoveryGuard 拒绝
        reg = ToolRegistry()
        reg.register_tool(make_tool("heavy"))
        result = await reg.execute_tool("heavy", {}, make_ctx())
        assert result.success is False
        assert "尚未加载" in result.error
        assert not reg.discovery.is_discovered("heavy")

    async def test_deferred_success_writes_discovery(self):
        # Risk-2：promote 后执行成功 → discover
        reg = ToolRegistry()
        reg.register_tool(make_tool("heavy"))
        reg.promote_to_discovered("heavy", step_n=1)
        result = await reg.execute_tool("heavy", {}, make_ctx(step_n=2))
        assert result.success is True
        assert reg.discovery.is_discovered("heavy")
        assert reg.discovery.get_step("heavy") == 2  # 用 context.step_n

    async def test_deferred_second_call_refreshes_step(self):
        # Risk-2：update_step 刷新（LRU 按最近使用排序）
        reg = ToolRegistry()
        reg.register_tool(make_tool("heavy"))
        reg.promote_to_discovered("heavy", step_n=1)
        await reg.execute_tool("heavy", {}, make_ctx(step_n=10))
        assert reg.discovery.get_step("heavy") == 10
        await reg.execute_tool("heavy", {}, make_ctx(step_n=99))
        assert reg.discovery.get_step("heavy") == 99

    async def test_execute_failure_does_not_discover(self):
        # 执行失败 → 不写 discovery（result.success 才维护）
        def _exec(ctx, **kw):
            raise RuntimeError("fail")

        reg = ToolRegistry()
        reg.register_tool(make_tool("heavy", executor=_exec))
        reg.promote_to_discovered("heavy", step_n=1)
        result = await reg.execute_tool("heavy", {}, make_ctx(step_n=2))
        assert result.success is False
        assert reg.discovery.get_step("heavy") == 1  # 未刷新

    async def test_always_tool_skips_discovery(self):
        # Risk-4
        reg = ToolRegistry()
        reg.register_tool(make_tool("always1", tier=ToolTier.ALWAYS))
        result = await reg.execute_tool("always1", {}, make_ctx(step_n=5))
        assert result.success is True
        assert not reg.discovery.is_discovered("always1")


class TestArgCleaning:
    async def test_extra_args_cleaned_before_schema_validation(self):
        # Risk-5：LLM 传多余参数 → facade 清洗 → 不触发 additionalProperties 拒绝
        reg = ToolRegistry()
        tool = make_tool(
            "calc",
            tier=ToolTier.ALWAYS,
            input_schema={
                "type": "object",
                "properties": {"a": {"type": "integer"}},
                "additionalProperties": False,
            },
        )
        reg.register_tool(tool)
        result = await reg.execute_tool("calc", {"a": 1, "bogus": 2}, make_ctx())
        assert result.success is True

    async def test_string_int_coerced_before_validation(self):
        # Risk-5：字符串 "20" → int 20，通过 integer 校验
        reg = ToolRegistry()
        reg.register_tool(
            make_tool(
                "calc",
                tier=ToolTier.ALWAYS,
                input_schema={
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            )
        )
        result = await reg.execute_tool("calc", {"count": "20"}, make_ctx())
        assert result.success is True

    async def test_schema_validation_failure(self):
        # Risk-6：类型不符 → 校验失败 ToolResult（jsonschema 未装则跳过）
        pytest.importorskip("jsonschema")
        reg = ToolRegistry()
        reg.register_tool(
            make_tool(
                "calc",
                tier=ToolTier.ALWAYS,
                input_schema={
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            )
        )
        result = await reg.execute_tool("calc", {"count": "not-a-number"}, make_ctx())
        assert result.success is False
        assert "校验失败" in result.error


class TestGuards:
    async def test_agent_whitelist_rejection(self):
        # Risk-7
        reg = ToolRegistry()
        reg.register_tool(
            make_tool(
                "secret",
                tier=ToolTier.ALWAYS,
                agent_whitelist=frozenset({"allowed-agent"}),
            )
        )
        result = await reg.execute_tool("secret", {}, make_ctx(agent_id="other-agent"))
        assert result.success is False
        assert "无权访问" in result.error

    async def test_agent_whitelist_pass(self):
        reg = ToolRegistry()
        reg.register_tool(
            make_tool(
                "secret",
                tier=ToolTier.ALWAYS,
                agent_whitelist=frozenset({"allowed-agent"}),
            )
        )
        result = await reg.execute_tool("secret", {}, make_ctx(agent_id="allowed-agent"))
        assert result.success is True

    async def test_trust_level_rejection(self):
        # Risk-7：trust_level_required=ORCHESTRATOR 但 ctx 是 EXTERNAL
        reg = ToolRegistry()
        reg.register_tool(
            make_tool(
                "admin_op",
                tier=ToolTier.ALWAYS,
                policy=ToolPolicy(
                    sensitivity=SensitivityLevel.HIGH,
                    trust_level_required=TrustLevel.ORCHESTRATOR,
                ),
            )
        )
        result = await reg.execute_tool("admin_op", {}, make_ctx(trust_level=TrustLevel.EXTERNAL))
        assert result.success is False
        assert "信任等级不足" in result.error


class TestEndToEnd:
    async def test_register_build_execute_roundtrip_non_ascii(self):
        # 端到端：注册非 ASCII 工具 → schema 构建 → 用 safe_name 回传执行
        reg = ToolRegistry()
        reg.register_tool(make_tool("天气预报", namespace="skill"))
        schemas = reg.build_tool_schemas()
        # 未发现 → 不在 schemas 中，但在 deferred catalog
        assert all(s.name != "skill_天气预报" for s in schemas)

        # promote（模拟 search_tools 加载）后用 schema 名反查执行
        catalog = reg.get_deferred_tool_catalog()
        safe_name = catalog[0]["name"]
        assert safe_name.isascii()
        reg.promote_to_discovered(safe_name, step_n=1)
        result = await reg.execute_tool(safe_name, {}, make_ctx(step_n=2))
        assert result.success is True
        assert result.tool_name == "skill_天气预报"  # ToolResult 用真实 full_name

    async def test_roundtrip_underscore_non_ascii_name(self):
        # 核心回归：name 含下划线 + 非 ASCII 的端到端闭环（旧 rsplit 会把 "my_tool" 误拆为 namespace，
        # 产出 "ns_my_tool_<hash>" 的错误 schema 名，导致 LLM 回传后反查不到真实工具）
        reg = ToolRegistry()
        reg.register_tool(make_tool("my_tool_天气", namespace="ns"))

        catalog = reg.get_deferred_tool_catalog()
        safe_name = catalog[0]["name"]
        # 精确计算：ns + md5(完整 name)，前缀不得混入 name 的 "_my_tool_" 段
        expected = f"ns_{hashlib.md5('my_tool_天气'.encode('utf-8')).hexdigest()[:8]}"
        assert safe_name == expected
        assert safe_name.isascii()
        assert "_my_tool_" not in safe_name  # 旧 rsplit 会产出 "ns_my_tool_xxx"

        # 用 safe_name 反查执行必须命中真实工具
        reg.promote_to_discovered(safe_name, step_n=1)
        result = await reg.execute_tool(safe_name, {}, make_ctx(step_n=2))
        assert result.success is True
        assert result.tool_name == "ns_my_tool_天气"  # ToolResult 用真实 full_name
