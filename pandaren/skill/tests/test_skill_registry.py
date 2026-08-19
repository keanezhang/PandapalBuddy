"""SkillRegistry 改动回归测试（设计文档 skill_registry.design.md，U1~U13）。

覆盖不变式 inv-1~inv-10 与风险 R1~R11。

注：原 U4~U8、U12~U13 为 Action Skill 机制（script → 工具预构建缓存、
_cleanup_action_tool、is_action/entry_function）测试。该机制已随 SDK 演进
删除——Skill 不再保留 script 字段、不再代为加载脚本（见
pandaren/skill/models.py 注释），对应用例连同 script_loader 依赖一并移除；
U9 改写为纯 Knowledge 覆盖断言。
"""
from __future__ import annotations

import pytest

from pandaren.builder import AgentBuilder
from pandaren.observability.types import AuditEventType
from pandaren.skill.models import Skill, SkillSource
from pandaren.skill.registry import SkillRegistry
from pandaren.tool.builtin.skill import SkillToolFactory
from pandaren.tool.facade import ToolRegistry
from pandaren.tool.types import ToolTier, SensitivityLevel

# ═══════════════════════════════════════════════════════════════════════
# U1 死代码防复活（inv-6 + R1, P0, unit）
# ═══════════════════════════════════════════════════════════════════════


def test_registry_has_no_register_builtin_tools_dead_code():
    # inv-6：改动①删除的死代码不得复活（类级 + 实例级均无）
    registry = SkillRegistry()
    assert hasattr(SkillRegistry, "register_builtin_tools") is False
    assert hasattr(SkillRegistry, "_builtin_tools_registered") is False
    assert hasattr(registry, "_builtin_tools_registered") is False


# ═══════════════════════════════════════════════════════════════════════
# U2 工厂生成 search_skills 定义正确（inv-5 + R2, P0, component）
# ═══════════════════════════════════════════════════════════════════════


def test_search_skills_tool_definition_golden():
    # inv-5：name/tier/schema/policy 为规格 golden value，可独立推导
    tools = SkillToolFactory().create_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "search_skills"
    assert tool.tier == ToolTier.ALWAYS
    assert tool.input_schema["required"] == ["skill_name"]
    assert tool.policy.is_idempotent is True
    assert tool.policy.sensitivity == SensitivityLevel.LOW
    assert callable(tool.executor)


# ═══════════════════════════════════════════════════════════════════════
# U3 executor 经 ctx.metadata 路由到 registry（inv-5 + R2, P0, component）
# ═══════════════════════════════════════════════════════════════════════


def test_search_skills_executor_routes_via_metadata(
    make_registry, make_context,
):
    registry = make_registry()
    registry.register_skill(Skill(
        name="route-test", description="d", when_to_use="w",
        content="指引内容 $ARGUMENTS",
    ))
    executor = SkillToolFactory().create_tools()[0].executor
    ctx = make_context(registry)

    result = executor(ctx, skill_name="route-test")

    assert result.success is True
    assert result.tool_name == "search_skills"
    assert "route-test" in result.data
    # search_skills 成功路径写入激活状态
    assert registry.get_active_skill_name() == "route-test"


# ═══════════════════════════════════════════════════════════════════════
# U9 Knowledge → Knowledge 覆盖无 Tool 副作用（inv-8 + R8, P2, component）
# ═══════════════════════════════════════════════════════════════════════


def test_knowledge_override_no_tool_side_effect(make_registry, tool_registry):
    # inv-8 + R8：Knowledge → Knowledge 覆盖只替换定义，不产生任何 Tool 副作用
    v1 = Skill(
        name="kb", description="v1", when_to_use="w", content="c1",
        source=SkillSource.USER,
    )
    registry = make_registry()
    registry.register_skill(v1)
    v_before = registry.version
    v2 = Skill(
        name="kb", description="v2", when_to_use="w", content="c2",
        source=SkillSource.PROGRAMMATIC,
    )
    registry.register_skill(v2)

    assert registry.get_skill("kb").description == "v2"
    assert "skill_kb" not in tool_registry.list_tool_names()  # 无 Action Skill 工具
    assert registry.version == v_before + 1  # 仅覆盖 +1，无额外变更


# ═══════════════════════════════════════════════════════════════════════
# U10 覆盖审计事件（R9, P3, component）
# ═══════════════════════════════════════════════════════════════════════


def test_override_audit_events(make_registry, fake_audit):
    # R9：覆盖成功写 SKILL_OVERRIDDEN；低优先级跳过不写
    registry = make_registry(audit_log=fake_audit)

    # 场景 A：USER → PROGRAMMATIC 覆盖成功
    v1a = Skill(
        name="kb-a", description="v1", when_to_use="w", content="c1",
        source=SkillSource.USER,
    )
    v2a = Skill(
        name="kb-a", description="v2", when_to_use="w", content="c2",
        source=SkillSource.PROGRAMMATIC,
    )
    registry.register_skill(v1a)
    registry.register_skill(v2a)

    # 场景 B：USER → BUILTIN 覆盖跳过
    v1b = Skill(
        name="kb-b", description="v1", when_to_use="w", content="c1",
        source=SkillSource.USER,
    )
    v2b = Skill(
        name="kb-b", description="v2", when_to_use="w", content="c2",
        source=SkillSource.BUILTIN,
    )
    registry.register_skill(v1b)
    registry.register_skill(v2b)

    overridden = [
        detail for ev, detail in fake_audit.events
        if ev == AuditEventType.SKILL_OVERRIDDEN
    ]
    assert len(overridden) == 1
    assert "USER → PROGRAMMATIC" in overridden[0]


# ═══════════════════════════════════════════════════════════════════════
# U11 builder 装配 search_skills（inv-5 + R2, P0, component）
# ═══════════════════════════════════════════════════════════════════════


def test_builder_resolve_skill_registry_registers_search_skills(fake_audit, tool_registry):
    # inv-5 + R2：生产注册路径（改动①死代码的替代路径）
    builder = AgentBuilder()
    builder._skill_list = [Skill(name="asm", description="d", when_to_use="w", content="c")]

    registry = builder._resolve_skill_registry(fake_audit, tool_registry)

    assert registry is not None
    assert registry.skill_count() == 1
    search_tool = tool_registry.get_tool("search_skills")
    assert search_tool is not None
    assert search_tool.name == "search_skills"
    assert search_tool.tier == ToolTier.ALWAYS
    # 补充：空 _skill_list → None（不装配）
    builder2 = AgentBuilder()
    assert builder2._resolve_skill_registry(fake_audit, tool_registry) is None
