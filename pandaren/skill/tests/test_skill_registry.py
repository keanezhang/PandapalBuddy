"""SkillRegistry 改动回归测试（设计文档 skill_registry.design.md，U1~U13）。

覆盖不变式 inv-1~inv-10 与风险 R1~R11；按 P0×5 / P1×3 / P2×3 / P3×2 落地。
U13（E4 Fail-Safe：cleanup 注销失败吞异常）为 v2 补充，见设计文档 §7 U13 与 §8 豁免声明。
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
    make_registry, make_context, tool_registry,
):
    registry = make_registry(tool_registry=tool_registry)
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
# U4 超长 description 截断后 Action Skill 保字段（inv-1 + R3, P0, integration）
# ═══════════════════════════════════════════════════════════════════════


def test_long_description_action_skill_preserves_fields(
    write_skill_script, make_registry, tool_registry,
):
    # inv-1 + R3：修复②——截断重建不丢 script/entry_function，is_action 保持 True
    base = write_skill_script("tool.py")
    skill = Skill(
        name="trunc-action", description="长描述" * 100, when_to_use="w", content="c",
        source=SkillSource.USER, base_path=base, script="tool.py", entry_function="run",
    )
    registry = make_registry(tool_registry=tool_registry)

    registry.register_skill(skill)

    s = registry.get_skill("trunc-action")
    assert s.is_action is True          # 核心断言：未退化为 Knowledge
    assert s.script == "tool.py"
    assert s.entry_function == "run"
    assert len(s.description) == 250    # 300 字符截断到上限
    # Tool 已预构建缓存，但懒注册（未触发 search_skills 前不入 ToolRegistry）
    assert registry.get_action_tool_name("trunc-action") == "skill_trunc-action"
    assert "trunc-action" in registry._action_tools_cache
    assert tool_registry.get_tool("skill_trunc-action") is None


# ═══════════════════════════════════════════════════════════════════════
# U5 同名 Action Skill 覆盖 → 旧 Tool 注销 + 新 Tool 重注册（inv-2 + R4, P1, integration）
# ═══════════════════════════════════════════════════════════════════════


def test_same_name_action_override_rebuilds_tool(
    write_skill_script, make_registry, make_context, tool_registry,
):
    # inv-2 + R4：修复③——覆盖必须注销旧 Tool，否则懒注册幂等检查跳过新 Tool
    base = write_skill_script("v1.py", 'f"processed:{query}"')
    v1 = Skill(
        name="overlap", description="v1 desc", when_to_use="w", content="c1",
        source=SkillSource.PROJECT, base_path=base, script="v1.py", entry_function="run",
    )
    registry = make_registry(tool_registry=tool_registry)
    registry.register_skill(v1)
    executor = SkillToolFactory().create_tools()[0].executor
    ctx = make_context(registry)

    r1 = executor(ctx, skill_name="overlap")
    assert r1.success is True
    assert tool_registry.get_tool("skill_overlap").description == "v1 desc"

    # v2（USER 优先级更高）覆盖 v1
    write_skill_script("v2.py", 'f"processed_v2:{query}"')
    v2 = Skill(
        name="overlap", description="v2 desc", when_to_use="w", content="c2",
        source=SkillSource.USER, base_path=base, script="v2.py", entry_function="run",
    )
    registry.register_skill(v2)

    # 断言点 A：旧 Tool 已注销、映射已重建为新 Tool
    assert tool_registry.get_tool("skill_overlap") is None
    assert registry.get_action_tool_name("overlap") == "skill_overlap"

    # 断言点 B：重新懒注册后新 Tool 生效（若无 cleanup，此处仍是 v1 → 用例即失败）
    r2 = executor(ctx, skill_name="overlap")
    assert r2.success is True
    new_tool = tool_registry.get_tool("skill_overlap")
    assert new_tool is not None
    assert new_tool.description == "v2 desc"
    assert new_tool.executor(ctx, query="hello").data == "processed_v2:hello"
    assert registry.get_skill("overlap").description == "v2 desc"
    assert registry._action_tools_cache["overlap"].description == "v2 desc"
    # version：v1 注册 +1，v2 覆盖 +1
    assert registry.version == 2


# ═══════════════════════════════════════════════════════════════════════
# U6 unregister 全面清理（inv-3 + R5, P1, integration）
# ═══════════════════════════════════════════════════════════════════════


def test_unregister_action_skill_cleans_all_state(
    write_skill_script, make_registry, make_context, tool_registry,
):
    # inv-3 + R5：注销后 _skills / 缓存 / 映射 / ToolRegistry / discovery 均无痕迹
    base = write_skill_script("tool.py")
    skill = Skill(
        name="ghost-action", description="d", when_to_use="w", content="c",
        source=SkillSource.USER, base_path=base, script="tool.py", entry_function="run",
    )
    registry = make_registry(tool_registry=tool_registry)
    registry.register_skill(skill)
    executor = SkillToolFactory().create_tools()[0].executor
    ctx = make_context(registry)
    executor(ctx, skill_name="ghost-action")
    assert tool_registry.get_tool("skill_ghost-action") is not None
    assert "skill_ghost-action" in tool_registry.discovery._discovered

    v_before = registry.version
    ok = registry.unregister_skill("ghost-action")

    assert ok is True
    assert registry.get_skill("ghost-action") is None
    assert registry.get_action_tool_name("ghost-action") is None
    assert "ghost-action" not in registry._action_tools_cache
    assert tool_registry.get_tool("skill_ghost-action") is None
    assert "skill_ghost-action" not in tool_registry.list_tool_names()
    assert "skill_ghost-action" not in tool_registry.discovery._discovered
    assert registry.version == v_before + 1
    # 补充：不存在 → False，无副作用，version 不变
    assert registry.unregister_skill("ghost-action") is False
    assert registry.version == v_before + 1


# ═══════════════════════════════════════════════════════════════════════
# U7 低优先级覆盖跳过且零副作用（inv-4 + R6, P1, integration）
# ═══════════════════════════════════════════════════════════════════════


def test_lower_priority_override_skipped_zero_side_effect(
    write_skill_script, make_registry, make_context, tool_registry,
):
    # inv-4 + R6：跳过分支不得误 cleanup 正在生效的旧 Action Tool
    base = write_skill_script("tool.py")
    v1 = Skill(
        name="prio-guard", description="keep me", when_to_use="w", content="c1",
        source=SkillSource.USER, base_path=base, script="tool.py", entry_function="run",
    )
    registry = make_registry(tool_registry=tool_registry)
    registry.register_skill(v1)
    executor = SkillToolFactory().create_tools()[0].executor
    ctx = make_context(registry)
    executor(ctx, skill_name="prio-guard")
    assert tool_registry.get_tool("skill_prio-guard").description == "keep me"

    v_before = registry.version
    write_skill_script("v2.py")
    v2 = Skill(
        name="prio-guard", description="intruder", when_to_use="w", content="c2",
        source=SkillSource.BUILTIN, base_path=base, script="v2.py", entry_function="run",
    )
    registry.register_skill(v2)

    kept = registry.get_skill("prio-guard")
    assert kept.source == SkillSource.USER
    assert kept.description == "keep me"
    # R6 直接证明：旧 Tool 未被 cleanup
    assert tool_registry.get_tool("skill_prio-guard").description == "keep me"
    assert registry._action_tools_cache["prio-guard"].description == "keep me"
    assert registry.version == v_before  # version 不变 = 零副作用锚点


# ═══════════════════════════════════════════════════════════════════════
# U8 _cleanup_action_tool 在 tool_registry=None 时安全（inv-7 + R7, P2, unit）
# ═══════════════════════════════════════════════════════════════════════


def test_cleanup_action_tool_without_tool_registry_is_safe(make_registry):
    # inv-7 + R7：tool_registry=None 不抛异常，且缓存仍被清理
    registry = make_registry(tool_registry=None)
    registry._action_tools_cache["ghost"] = object()
    registry._action_skill_tools["ghost"] = "skill_ghost"

    registry._cleanup_action_tool("ghost")

    assert "ghost" not in registry._action_tools_cache
    assert "ghost" not in registry._action_skill_tools
    registry._cleanup_action_tool("不存在的skill")  # 不抛异常


# ═══════════════════════════════════════════════════════════════════════
# U9 Knowledge → Knowledge 覆盖不触发 cleanup（inv-8 + R8, P2, component）
# ═══════════════════════════════════════════════════════════════════════


def test_knowledge_override_no_cleanup_no_tool_side_effect(make_registry, tool_registry):
    # inv-8 + R8：existing.is_action 为假 → 不调 cleanup，无 Tool 副作用
    v1 = Skill(
        name="kb", description="v1", when_to_use="w", content="c1",
        source=SkillSource.USER,
    )
    registry = make_registry(tool_registry=tool_registry)
    registry.register_skill(v1)
    v_before = registry.version
    v2 = Skill(
        name="kb", description="v2", when_to_use="w", content="c2",
        source=SkillSource.PROGRAMMATIC,
    )
    registry.register_skill(v2)

    assert registry.get_skill("kb").description == "v2"
    assert registry.get_action_tool_name("kb") is None
    assert "kb" not in registry._action_tools_cache
    assert "skill_kb" not in tool_registry.list_tool_names()
    assert registry.version == v_before + 1  # 仅覆盖 +1，无额外变更


# ═══════════════════════════════════════════════════════════════════════
# U10 覆盖审计事件（R9, P3, component）
# ═══════════════════════════════════════════════════════════════════════


def test_override_audit_events(make_registry, tool_registry, fake_audit):
    # R9：覆盖成功写 SKILL_OVERRIDDEN；低优先级跳过不写
    registry = make_registry(tool_registry=tool_registry, audit_log=fake_audit)

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


# ═══════════════════════════════════════════════════════════════════════
# U12 脚本缺失 → 无半残缓存（R10, P3, integration）
# ═══════════════════════════════════════════════════════════════════════


def test_missing_script_no_half_baked_cache(make_registry, tool_registry, tmp_path):
    # R10：SK7 Fail-Safe——脚本加载失败不抛异常、不留半残缓存
    skill = Skill(
        name="broken", description="d", when_to_use="w", content="c",
        base_path=str(tmp_path), script="missing.py", entry_function="run",
    )
    registry = make_registry(tool_registry=tool_registry)

    registry.register_skill(skill)  # 不抛异常

    s = registry.get_skill("broken")
    assert s is not None
    assert s.is_action is True  # Skill 定义保留（script 字段仍在）
    assert "broken" not in registry._action_tools_cache
    assert registry.get_action_tool_name("broken") is None
    assert "skill_broken" not in tool_registry.list_tool_names()


# ═══════════════════════════════════════════════════════════════════════
# U13 _cleanup_action_tool 注销失败吞异常（inv-10 + R11, P2）
#     场景 A：unit——直接调私有方法（ExplodingToolRegistry stub）
#     场景 B：integration——真实覆盖链路 + monkeypatch unregister_tool
# ═══════════════════════════════════════════════════════════════════════


class ExplodingToolRegistry:
    """E4 故障注入 stub：unregister_tool 必然抛异常，其余无关。"""

    def unregister_tool(self, name: str) -> None:
        raise RuntimeError("simulated unregister failure")


def test_cleanup_action_tool_unregister_failure_swallowed(
    make_registry, caplog,
):
    # inv-10 + R11 场景 A：注销失败被吞（E4），缓存仍清理（pop 在 try 外），debug 留痕
    registry = make_registry(tool_registry=ExplodingToolRegistry())
    registry._action_tools_cache["ghost"] = object()
    registry._action_skill_tools["ghost"] = "skill_ghost"

    with caplog.at_level("DEBUG", logger="pandaren.skill.registry"):
        registry._cleanup_action_tool("ghost")  # 不抛异常

    assert "ghost" not in registry._action_tools_cache
    assert "ghost" not in registry._action_skill_tools
    assert any("skill_ghost" in r.message for r in caplog.records)


def test_override_unregister_failure_does_not_block_main_flow(
    write_skill_script, make_registry, make_context, tool_registry, monkeypatch,
):
    # inv-10 + R11 场景 B：覆盖链路中 unregister_tool 抛异常 → 主流程不阻塞 + 缓存重建
    def _explode(self_unused, name):
        raise RuntimeError("simulated unregister failure")

    monkeypatch.setattr(tool_registry, "unregister_tool", _explode)

    base = write_skill_script("v1.py", 'f"processed:{query}"')
    v1 = Skill(
        name="overlap", description="v1 desc", when_to_use="w", content="c1",
        source=SkillSource.PROJECT, base_path=base, script="v1.py", entry_function="run",
    )
    registry = make_registry(tool_registry=tool_registry)
    registry.register_skill(v1)
    executor = SkillToolFactory().create_tools()[0].executor
    ctx = make_context(registry)
    assert executor(ctx, skill_name="overlap").success is True
    assert tool_registry.get_tool("skill_overlap") is not None  # 旧 Tool 已懒注册

    write_skill_script("v2.py", 'f"processed_v2:{query}"')
    v2 = Skill(
        name="overlap", description="v2 desc", when_to_use="w", content="c2",
        source=SkillSource.USER, base_path=base, script="v2.py", entry_function="run",
    )
    registry.register_skill(v2)  # 覆盖路径触发 _cleanup_action_tool → unregister 抛异常被吞

    # 主流程不阻塞：覆盖成功、version +1、缓存与映射已重建为新 Tool
    assert registry.get_skill("overlap").description == "v2 desc"
    assert registry.version == 2  # v1 注册 +1，v2 覆盖 +1
    cached_tool = registry._action_tools_cache["overlap"]
    assert cached_tool.description == "v2 desc"
    # 新 Tool 本体执行新脚本
    assert cached_tool.executor(ctx, query="hello").data == "processed_v2:hello"
    assert registry.get_action_tool_name("overlap") == "skill_overlap"
    # 注：旧 Tool 因 unregister 失败残留于 ToolRegistry，懒注册幂等检查将跳过新 Tool
    #     再注册——这是 E4 降级的显式留痕（debug 日志），非静默失败；下次 cleanup
    #     成功时自然收敛（完整收敛链路由 U5 的断言点 A/B 验证）
