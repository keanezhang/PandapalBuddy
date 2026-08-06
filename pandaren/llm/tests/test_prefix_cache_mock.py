"""
Pandaren Agent SDK · Prefix Cache v1.0 真实测试

覆盖范围
--------
  验证 docs/工程化设计文档/框架设计/13_prefix_cache.md 设计文档落地后的关键不变量：

  PC1 序列化唯一性
    - 跨 turn 相同输入 → system message 字节级一致
    - build_static_context_str 对确定输入的返回值稳定可重复
  PC2 Stable-First Ordering
    - Memory._build_system_content() 只含静态 agent-config 区段，不含 recall 外壳
    - MessageBuilder.build() 把 static_context_str 拼在 system.content 末尾
  PC3 Dynamic-Content Tail Injection（方案 B1）
    - build_dynamic_reminder 返回完整 <system-reminder> 包裹
    - build() 将 dynamic_reminder 作为独立 role=user 消息尾插
    - discovered 变化不影响 <available_tools> 静态清单（清单与 discovered 解耦）
  PC5 双通道一致性
    - messages 通道 <available_tools> 与 tools 通道顺序均按 name 字母序
    - tool_schemas 物理顺序：sorted(ALWAYS) → search_tools → sorted(DEFERRED-loaded)
  PC6 清单对 discovered 免疫
    - get_deferred_tool_catalog() 返回值稳定，不因 DiscoveryManager 中发现状态变化而漂移
  PC7 search_tools.enum 语义纯洁
    - enum 仅包含「待发现」的 DEFERRED（排除已 discovered 的工具）
    - 已 discovered 的工具完整 schema 出现在 tools 列表中
  双 append 不变量（② 分支）
    - 已 discovered 的 DEFERRED 工具：进 schemas_deferred_found + deferred_unfound_summaries，
      不进 available_deferred_names（enum）

  Memory 侧
    - recall_text property 只读暴露 _recall_text
    - _build_system_content() 不再包含 <!-- recall-start/end -->

  回归检查
    - _RECALL_START / _RECALL_END 模块常量仍可导入（向下兼容）

运行方式
--------
  cd pandaren/llm/tests && python test_prefix_cache_mock.py
"""

from __future__ import annotations

import io
import os
import sys

# Windows 控制台 UTF-8 输出
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ═══ SDK 导入 ═══
from pandaren.tool.decorator import tool
from pandaren.tool.types import ToolTier, SensitivityLevel
from pandaren.tool.registry import ToolRegistry
from pandaren.tool import DiscoveredToolEntry

from pandaren.engine.message_builder import MessageBuilder
from pandaren.skill.models import SkillSummary
from pandaren.sub_agent.models import SubAgentSummary
from pandaren.memory import memory as memory_mod
from pandaren.memory.memory import Memory


# ════════════════════════════════════════════════════
#  测试框架（与既有 mock 测试同风格）
# ════════════════════════════════════════════════════

class TestResult:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def ok(self, name: str) -> None:
        self.passed += 1
        print(f"   ✅ {name}")

    def fail(self, name: str, detail: str = "") -> None:
        self.failed += 1
        msg = f"   ❌ {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        self.errors.append(msg)

    def summary(self, section: str = "") -> bool:
        total = self.passed + self.failed
        label = f" [{section}]" if section else ""
        print(f"\n📊{label} 通过={self.passed} / 失败={self.failed} / 总计={total}")
        if self.errors:
            print("   失败列表:")
            for e in self.errors:
                print(f"     {e}")
        return self.failed == 0


result = TestResult()


def check(cond: bool, name: str, detail: str = "") -> None:
    if cond:
        result.ok(name)
    else:
        result.fail(name, detail or "条件为 False")


def eq(actual, expected, name: str) -> None:
    if actual == expected:
        result.ok(name)
    else:
        result.fail(
            name,
            f"expected={expected!r}, actual={actual!r}",
        )


# ════════════════════════════════════════════════════
#  测试夹具：构造一个含 ALWAYS + DEFERRED 混合的 ToolRegistry
# ════════════════════════════════════════════════════

def _make_tool(
    name: str,
    tier: ToolTier,
    when_to_use: str = "",
):
    """使用 @tool.function 装饰器构造 Tool 定义。"""
    @tool.function(
        name=name,
        description=f"{name} tool",
        tier=tier,
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        when_to_use=when_to_use or f"use {name}",
    )
    def _impl(ctx, q: str = "") -> str:
        """Dummy tool.

        Args:
            q: query string
        """
        return q

    return _impl


def _build_registry() -> ToolRegistry:
    """构造一个具有多样性的 registry：
      - 3 个 ALWAYS（含内置 search_tools）
      - 3 个 DEFERRED（故意乱序注册，验证字母序重排）
    """
    reg = ToolRegistry(enable_search=True)

    # ALWAYS：反字母序注册，验证最终输出会被重排为字母序
    reg.register_tool(_make_tool("charlie_always", ToolTier.ALWAYS))
    reg.register_tool(_make_tool("alpha_always", ToolTier.ALWAYS))
    reg.register_tool(_make_tool("bravo_always", ToolTier.ALWAYS))

    # DEFERRED：同样故意乱序
    reg.register_tool(_make_tool("zeta_deferred", ToolTier.DEFERRED, "use zeta"))
    reg.register_tool(_make_tool("delta_deferred", ToolTier.DEFERRED, "use delta"))
    reg.register_tool(_make_tool("echo_deferred", ToolTier.DEFERRED, "use echo"))
    return reg


# ════════════════════════════════════════════════════
#  Section 1: ToolRegistry — PC5 / PC6 / PC7 + 双 append 不变量
# ════════════════════════════════════════════════════

def test_tool_registry() -> None:
    print("\n━━━ Section 1: ToolRegistry (PC5 / PC6 / PC7) ━━━")

    reg = _build_registry()

    # —— 1.1 PC6: get_deferred_tool_catalog() 对 discovered 完全免疫 ——
    catalog_v0 = reg.get_deferred_tool_catalog()
    names_v0 = [d["name"] for d in catalog_v0]
    eq(
        names_v0,
        ["delta_deferred", "echo_deferred", "zeta_deferred"],
        "1.1 deferred_tool_catalog 按 name 字母序 + 仅含 DEFERRED",
    )
    # 不含 ALWAYS 工具
    check(
        all(n.endswith("_deferred") for n in names_v0),
        "1.2 deferred_tool_catalog 完全排除 ALWAYS 工具",
    )
    # 含 when_to_use
    check(
        all("when_to_use" in d and d["when_to_use"] for d in catalog_v0),
        "1.3 deferred_tool_catalog 条目带 when_to_use",
    )

    # —— 1.4 PC5 双通道一致性: 首轮 build_tool_schemas 返回顺序 ——
    schemas_v0 = reg.build_tool_schemas(agent_id="test_agent", messages=[])
    schema_names_v0 = [s.name for s in schemas_v0]
    expected_v0 = [
        # ① sorted(ALWAYS \ {search_tools})
        "alpha_always", "bravo_always", "charlie_always",
        # ② search_tools
        "search_tools",
        # ③ sorted(DEFERRED-loaded) —— 尚无 discovered，为空
    ]
    eq(schema_names_v0, expected_v0, "1.4 tools 通道三段顺序（无 discovered）")

    # —— 1.5 PC7 search_tools.enum 全部 DEFERRED（尚无 discovered）——
    search_schema_v0 = next(s for s in schemas_v0 if s.name == "search_tools")
    enum_v0 = search_schema_v0.parameters["properties"]["tool_name"].get("enum")
    eq(
        enum_v0,
        ["delta_deferred", "echo_deferred", "zeta_deferred"],
        "1.5 search_tools.enum = 全部 DEFERRED（字母序）",
    )

    # —— 模拟 LLM 通过 search_tools 发现了 delta_deferred ——
    discovered_msg = {
        "role": "tool",
        "tool_name": "search_tools",
        "_discovered_tools": (
            DiscoveredToolEntry(name="delta_deferred", turn=1),
        ),
    }
    schemas_v1 = reg.build_tool_schemas(
        agent_id="test_agent",
        messages=[discovered_msg],
    )
    schema_names_v1 = [s.name for s in schemas_v1]

    # —— 1.6 PC5 三段顺序（有 1 个 discovered）——
    expected_v1 = [
        "alpha_always", "bravo_always", "charlie_always",
        "search_tools",
        "delta_deferred",  # ③ sorted(DEFERRED-loaded)
    ]
    eq(schema_names_v1, expected_v1, "1.6 discovered 后 tools 通道物理顺序")

    # —— 1.7 PC7: enum 收窄，排除已 discovered ——
    search_schema_v1 = next(s for s in schemas_v1 if s.name == "search_tools")
    enum_v1 = search_schema_v1.parameters["properties"]["tool_name"].get("enum")
    eq(
        enum_v1,
        ["echo_deferred", "zeta_deferred"],
        "1.7 search_tools.enum 收窄（排除 discovered）",
    )
    check(
        "delta_deferred" not in enum_v1,
        "1.8 已 discovered 的工具不在 enum（PC7 语义纯洁）",
    )

    # —— 1.9 双 append 不变量：deferred_summaries 仍含 delta（PC6）——
    summaries_v1 = reg.get_deferred_summaries()
    summary_names_v1 = [s["name"] for s in summaries_v1]
    check(
        "delta_deferred" in summary_names_v1,
        "1.9 已 discovered 的工具仍在 deferred_summaries（双 append 的 ②）",
        detail=f"summaries={summary_names_v1}",
    )
    eq(
        summary_names_v1,
        ["delta_deferred", "echo_deferred", "zeta_deferred"],
        "1.10 deferred_summaries 保持完整字母序（PC6）",
    )

    # —— 1.11 PC6: catalog 跨 turn 稳定 ——
    catalog_v1 = reg.get_deferred_tool_catalog()
    eq(
        catalog_v1,
        catalog_v0,
        "1.11 get_deferred_tool_catalog() 对 discovered 免疫（字节级一致）",
    )

    # —— 1.12 catalog 返回副本（防止外部篡改内部状态）——
    catalog_v1.append({"name": "INJECTED", "when_to_use": "x"})
    catalog_v2 = reg.get_deferred_tool_catalog()
    check(
        all(d["name"] != "INJECTED" for d in catalog_v2),
        "1.12 catalog 返回新列表，外部篡改不影响内部状态",
    )


# ════════════════════════════════════════════════════
#  Section 2: MessageBuilder — build_static_context_str / build_dynamic_reminder / build
# ════════════════════════════════════════════════════

def test_message_builder_static_context() -> None:
    print("\n━━━ Section 2: MessageBuilder.build_static_context_str ━━━")

    # 2.1 全 None → 返回 None
    r = MessageBuilder.build_static_context_str(
        deferred_tool_summaries=None,
        skill_summaries=None,
        agent_summaries=None,
    )
    eq(r, None, "2.1 三者全 None → 返回 None")

    # 2.2 全空列表 → 返回 None
    r = MessageBuilder.build_static_context_str(
        deferred_tool_summaries=[],
        skill_summaries=[],
        agent_summaries=[],
    )
    eq(r, None, "2.2 三者全空列表 → 返回 None")

    # 2.3 仅 deferred tools：含 <available_tools> 不含其他两块
    catalog = [
        {"name": "alpha_t", "when_to_use": "use alpha"},
        {"name": "bravo_t", "when_to_use": "use bravo"},
    ]
    r = MessageBuilder.build_static_context_str(
        deferred_tool_summaries=catalog,
    )
    check(r is not None, "2.3a 只提供 deferred → 非 None")
    check("<available_tools>" in r, "2.3b 含 <available_tools>")
    check("<available_skills>" not in r, "2.3c 不含 <available_skills>")
    check("<available_agents>" not in r, "2.3d 不含 <available_agents>")
    check("alpha_t" in r and "bravo_t" in r, "2.3e 含所有工具名")
    # 顺序：alpha 在 bravo 之前
    check(r.index("alpha_t") < r.index("bravo_t"), "2.3f alpha 物理顺序在 bravo 之前")

    # 2.4 三块齐全
    skills = [SkillSummary(name="skill_b", when_to_use="B")]
    agents = [SubAgentSummary(agent_name="agent_c", when_to_use="C")]
    r_full = MessageBuilder.build_static_context_str(
        deferred_tool_summaries=catalog,
        skill_summaries=skills,
        agent_summaries=agents,
    )
    check(
        "<available_tools>" in r_full
        and "<available_skills>" in r_full
        and "<available_agents>" in r_full,
        "2.4a 三块 XML 全部出现",
    )
    # 顺序：tools → skills → agents（PC2 稳定优先）
    check(
        r_full.index("<available_tools>")
        < r_full.index("<available_skills>")
        < r_full.index("<available_agents>"),
        "2.4b 三块 XML 顺序 tools → skills → agents（PC2）",
    )

    # 2.5 PC1 序列化唯一性：相同输入 → 相同输出（字节级）
    r_again = MessageBuilder.build_static_context_str(
        deferred_tool_summaries=catalog,
        skill_summaries=skills,
        agent_summaries=agents,
    )
    check(r_full == r_again, "2.5 相同输入字节级一致（PC1）")


def test_message_builder_dynamic_reminder() -> None:
    print("\n━━━ Section 3: MessageBuilder.build_dynamic_reminder ━━━")

    # 3.1 全空 → None
    r = MessageBuilder.build_dynamic_reminder()
    eq(r, None, "3.1 全空 → None")
    r = MessageBuilder.build_dynamic_reminder(recall_text="")
    eq(r, None, "3.2 空串 → None")

    # 3.3 仅 recall_text（当前唯一支持的来源）
    r = MessageBuilder.build_dynamic_reminder(recall_text="- user prefers dark mode")
    check(r is not None, "3.3a 有 recall → 非 None")
    check(r.startswith("<system-reminder>"), "3.3b 以 <system-reminder> 开头")
    check(r.endswith("</system-reminder>"), "3.3c 以 </system-reminder> 结尾")
    check("<recall>" in r and "</recall>" in r, "3.3d 含 <recall> 内嵌块")
    check("- user prefers dark mode" in r, "3.3e 含 recall 原文")


def test_message_builder_build() -> None:
    print("\n━━━ Section 4: MessageBuilder.build (PC2 + PC3) ━━━")

    mb = MessageBuilder()

    base_msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
    ]

    # 4.1 静态前缀被追加到 system 末尾
    built, tools = mb.build(
        messages=base_msgs,
        tool_schemas=None,
        static_context_str="<<STATIC>>",
        dynamic_reminder=None,
    )
    eq(built[0]["role"], "system", "4.1a 第一条仍是 system")
    eq(built[0]["content"], "SYS<<STATIC>>", "4.1b 静态前缀拼在 system.content 末尾（PC2）")

    # 4.1c 不修改原 messages（深拷贝隔离）
    eq(base_msgs[0]["content"], "SYS", "4.1c 原始 messages[0] 未被污染")

    # 4.2 dynamic_reminder 作为独立 role=user 尾插（PC3 / 方案 B1）
    built, _ = mb.build(
        messages=base_msgs,
        tool_schemas=None,
        static_context_str=None,
        dynamic_reminder="<system-reminder>\nHELLO\n</system-reminder>",
    )
    eq(len(built), len(base_msgs) + 1, "4.2a dynamic_reminder 追加一条消息")
    last = built[-1]
    eq(last["role"], "user", "4.2b dynamic_reminder 消息 role=user（方案 B1）")
    check(
        last["content"].startswith("<system-reminder>"),
        "4.2c dynamic_reminder 内容含 <system-reminder> 外壳",
    )

    # 4.3 全空 dynamic → 不追加
    built, _ = mb.build(
        messages=base_msgs,
        tool_schemas=None,
        static_context_str=None,
        dynamic_reminder=None,
    )
    eq(len(built), len(base_msgs), "4.3 dynamic=None 时不追加消息")

    # 4.4 tools 参数构建
    # 复用 ToolRegistry 产出
    reg = _build_registry()
    schemas = reg.build_tool_schemas(agent_id="a1", messages=[])
    built, tools = mb.build(
        messages=base_msgs,
        tool_schemas=schemas,
        static_context_str=None,
        dynamic_reminder=None,
    )
    check(tools is not None, "4.4a tool_schemas 非空 → tools 非 None")
    eq(len(tools), len(schemas), "4.4b tools 长度与 schemas 一致")
    # PC5：tools 通道顺序等于 schemas 顺序
    tool_names = [t["function"]["name"] for t in tools]
    schema_names = [s.name for s in schemas]
    eq(tool_names, schema_names, "4.4c tools 通道保留 PC5 物理顺序")
    # OpenAI 兼容结构
    eq(tools[0]["type"], "function", "4.4d tools[0].type = 'function'")
    check(
        "name" in tools[0]["function"]
        and "description" in tools[0]["function"]
        and "parameters" in tools[0]["function"],
        "4.4e function 字段完整",
    )


# ════════════════════════════════════════════════════
#  Section 5: PC1/PC3 端到端 — 跨 turn 系统前缀字节级一致
# ════════════════════════════════════════════════════

def test_pc1_end_to_end() -> None:
    print("\n━━━ Section 5: PC1 端到端（跨 turn 前缀字节级稳定）━━━")

    reg = _build_registry()

    # ── init 阶段：序列化一次静态前缀 ──
    catalog = reg.get_deferred_tool_catalog()
    static_ctx = MessageBuilder.build_static_context_str(
        deferred_tool_summaries=catalog,
        skill_summaries=[SkillSummary(name="s1", when_to_use="x")],
        agent_summaries=[SubAgentSummary(agent_name="a1", when_to_use="y")],
    )
    check(static_ctx is not None, "5.0 init 阶段 static_ctx 非空")

    mb = MessageBuilder()
    base_system = {"role": "system", "content": "You are a helpful assistant."}

    # ── Turn 1 ──
    msgs_t1 = [base_system, {"role": "user", "content": "task-1"}]
    built_t1, tools_t1 = mb.build(
        messages=msgs_t1,
        tool_schemas=reg.build_tool_schemas(agent_id="a", messages=[]),
        static_context_str=static_ctx,
        dynamic_reminder=None,
    )
    sys_prefix_t1 = built_t1[0]["content"]

    # ── Turn 2：模拟 discovered 发生变化 ──
    discovered_msg = {
        "role": "tool",
        "tool_name": "search_tools",
        "_discovered_tools": (DiscoveredToolEntry(name="echo_deferred", turn=2),),
    }
    msgs_t2 = [
        base_system,
        {"role": "user", "content": "task-1"},
        {"role": "assistant", "content": "", "tool_calls": []},
        discovered_msg,
        {"role": "user", "content": "task-2"},
    ]
    built_t2, tools_t2 = mb.build(
        messages=msgs_t2,
        tool_schemas=reg.build_tool_schemas(agent_id="a", messages=msgs_t2),
        static_context_str=static_ctx,  # 静态前缀 init 时产生，全程不变
        dynamic_reminder=MessageBuilder.build_dynamic_reminder(
            recall_text="- relevant memory"
        ),
    )
    sys_prefix_t2 = built_t2[0]["content"]

    # —— 5.1 PC1: system 前缀字节级一致 ——
    eq(
        sys_prefix_t1,
        sys_prefix_t2,
        "5.1 跨 turn system.content 字节级一致（PC1 核心指标）",
    )

    # —— 5.2 PC6: <available_tools> 内容不因 discovered 变化 ——
    check(
        "echo_deferred" in sys_prefix_t2,  # 仍在清单中
        "5.2 <available_tools> 清单不受 discovered 影响（PC6）",
    )

    # —— 5.3 PC3: 动态 reminder 独立尾插 ——
    check(
        built_t2[-1]["role"] == "user"
        and built_t2[-1]["content"].startswith("<system-reminder>"),
        "5.3 dynamic_reminder 作为独立 user 消息尾插（PC3 / 方案 B1）",
    )

    # —— 5.4 PC7: tools 通道 enum 按 discovered 收窄 ——
    search_t1 = next(t for t in tools_t1 if t["function"]["name"] == "search_tools")
    search_t2 = next(t for t in tools_t2 if t["function"]["name"] == "search_tools")
    enum_t1 = search_t1["function"]["parameters"]["properties"]["tool_name"].get("enum")
    enum_t2 = search_t2["function"]["parameters"]["properties"]["tool_name"].get("enum")
    check(
        "echo_deferred" in enum_t1,
        "5.4a turn-1 enum 含 echo_deferred（尚未 discovered）",
    )
    check(
        "echo_deferred" not in enum_t2,
        "5.4b turn-2 enum 已排除 echo_deferred（PC7）",
    )

    # —— 5.5 PC7: 已 discovered 工具的完整 schema 进 tools 通道 ——
    tool_names_t2 = [t["function"]["name"] for t in tools_t2]
    check(
        "echo_deferred" in tool_names_t2,
        "5.5 已 discovered 的工具完整 schema 出现在 tools 通道",
    )


# ════════════════════════════════════════════════════
#  Section 6: Memory — _build_system_content + recall_text property
# ════════════════════════════════════════════════════

def _make_memory_stub(
    system_prompt: str = "You are a helpful assistant.",
    agent_config_text: str = "",
    recall_text: str | None = None,
) -> Memory:
    """绕过 Memory.__init__（重 IO），直接构造一个可测 _build_system_content 的实例。

    仅设置 _build_system_content / recall_text 依赖的最小字段。
    """
    m = Memory.__new__(Memory)
    m._system_prompt = system_prompt
    m._agent_config_text = agent_config_text
    m._recall_text = recall_text
    return m


def test_memory_system_content() -> None:
    print("\n━━━ Section 6: Memory._build_system_content / recall_text ━━━")

    # —— 6.1 无 agent_config_text：仅 system_prompt 被 agent-config 标签包裹 ——
    m = _make_memory_stub(
        system_prompt="BASE PROMPT",
        agent_config_text="",
        recall_text=None,
    )
    content = m._build_system_content()
    check("BASE PROMPT" in content, "6.1a system_prompt 内容存在")
    check(memory_mod._AGENT_CONFIG_START in content, "6.1b 含 agent-config-start 标签")
    check(memory_mod._AGENT_CONFIG_END in content, "6.1c 含 agent-config-end 标签")

    # —— 6.2 不再包含 recall 外壳（Prefix Cache v1.0 核心改动）——
    check(
        "<!-- recall-start -->" not in content,
        "6.2a 不再包含 recall-start 外壳（PC2 稳定前缀）",
    )
    check(
        "<!-- recall-end -->" not in content,
        "6.2b 不再包含 recall-end 外壳",
    )

    # —— 6.3 即便 _recall_text 非空，也不进入 system message ——
    m_with_recall = _make_memory_stub(
        system_prompt="P",
        agent_config_text="",
        recall_text="- memory X",
    )
    content_r = m_with_recall._build_system_content()
    check(
        "- memory X" not in content_r,
        "6.3 recall 文本不再出现在 system message（方案 B1 改走 MessageBuilder 尾插）",
    )
    check(
        "<!-- recall-start -->" not in content_r,
        "6.4 recall_text 非空时也不写 recall 标签外壳",
    )

    # —— 6.5 带 agent_config_text：正确拼装 ——
    m_cfg = _make_memory_stub(
        system_prompt="P",
        agent_config_text="CFG",
        recall_text=None,
    )
    content_cfg = m_cfg._build_system_content()
    check(
        "P" in content_cfg and "CFG" in content_cfg,
        "6.5a system_prompt + agent_config_text 同时出现",
    )
    check(
        content_cfg.index("P") < content_cfg.index("CFG"),
        "6.5b system_prompt 在 agent_config_text 之前",
    )

    # —— 6.6 recall_text property 只读暴露 _recall_text ——
    m2 = _make_memory_stub(recall_text="RT")
    eq(m2.recall_text, "RT", "6.6a recall_text property 返回 _recall_text")
    m3 = _make_memory_stub(recall_text=None)
    eq(m3.recall_text, None, "6.6b recall_text = None 时 property 返回 None")

    # —— 6.7 PC1: 相同输入 → 相同 system content（字节级）——
    m_a = _make_memory_stub(system_prompt="SP", agent_config_text="AC")
    m_b = _make_memory_stub(system_prompt="SP", agent_config_text="AC")
    eq(
        m_a._build_system_content(),
        m_b._build_system_content(),
        "6.7 相同输入 system content 字节级一致（PC1）",
    )

    # —— 6.8 recall_text 变化不影响 system content（核心回归断言）——
    m_no_r = _make_memory_stub(system_prompt="SP", agent_config_text="AC", recall_text=None)
    m_with_r = _make_memory_stub(system_prompt="SP", agent_config_text="AC", recall_text="ANYTHING")
    eq(
        m_no_r._build_system_content(),
        m_with_r._build_system_content(),
        "6.8 recall_text 变化不影响 system content（PC2 稳定前缀 / 关键回归）",
    )


def test_backward_compat() -> None:
    print("\n━━━ Section 7: 向下兼容 & 回归检查 ━━━")

    # 7.1 _RECALL_START / _RECALL_END 常量仍可导入（deprecated 但保留）
    check(
        hasattr(memory_mod, "_RECALL_START")
        and hasattr(memory_mod, "_RECALL_END"),
        "7.1 deprecated 常量 _RECALL_START/_RECALL_END 仍可导入",
    )

    # 7.2 ToolRegistry 向下兼容：get_deferred_summaries 仍存在
    reg = ToolRegistry(enable_search=False)
    check(
        callable(getattr(reg, "get_deferred_summaries", None)),
        "7.2 get_deferred_summaries 仍可调用（向下兼容）",
    )

    # 7.3 新 API 存在性
    check(
        callable(getattr(reg, "get_deferred_tool_catalog", None)),
        "7.3 新增 get_deferred_tool_catalog 可调用",
    )
    check(
        callable(getattr(MessageBuilder, "build_static_context_str", None)),
        "7.4 MessageBuilder.build_static_context_str 可调用（classmethod）",
    )
    check(
        callable(getattr(MessageBuilder, "build_dynamic_reminder", None)),
        "7.5 MessageBuilder.build_dynamic_reminder 可调用（classmethod）",
    )


# ════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════

def main() -> int:
    print("═══════════════════════════════════════════════════")
    print("   Prefix Cache v1.0 真实测试")
    print("   设计文档：docs/工程化设计文档/框架设计/13_prefix_cache.md")
    print("═══════════════════════════════════════════════════")

    test_tool_registry()
    test_message_builder_static_context()
    test_message_builder_dynamic_reminder()
    test_message_builder_build()
    test_pc1_end_to_end()
    test_memory_system_content()
    test_backward_compat()

    ok = result.summary("PREFIX-CACHE-V1.0")
    print("═══════════════════════════════════════════════════")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
