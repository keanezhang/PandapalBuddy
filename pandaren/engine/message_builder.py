"""pandaren/engine/message_builder.py — 消息拼装（Prefix Cache v1.0）

职责：
  - 把 Memory 层管理的 messages + 运行时传入的工具/技能/子 Agent 目录
    拼装成最终发给 LLM 的 messages。
  - 遵守 Prefix Cache 五大原则：
      PC1 序列化唯一性 — 相同输入 → 完全相同字符串（字节级一致）
      PC2 Stable-First Ordering — 越稳定越靠前（system → 对话 → 动态尾插）
      PC3 Dynamic-Content Tail Injection — 每轮变化的内容放在最后一条消息
      PC4 Minimal Footprint — 对现有数据流改动最小
      PC5 双通道一致性 — messages 通道 <available_tools> XML 与 tools 参数通道
                        使用相同的排序规则

两类输出：
  - static_context_str：init 时一次性序列化的静态前缀，贯穿整个 run 拼在
    system message 末尾不变（服务 PC1 + PC2）。由 AgentLoop 在构造时调用
    ``MessageBuilder.build_static_context_str(...)`` 产生并缓存。
  - dynamic_reminder：每轮变化的内容（本轮激活的 Skill 正文 / recall 结果）
    以独立 role=user ``<system-reminder>`` 消息追加到历史末尾（服务 PC3 /
    方案 B1）。由 AgentLoop 每轮调用 ``build_dynamic_reminder(...)`` 产生。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..tool.definition.tool_schema import ToolSchema
    from ..skill.models import SkillSummary
    from ..agent.models import SubAgentSummary


# ════════════════════════════════════════════════
#  <system-reminder> 动态消息标记（PC3 / 方案 B1）
# ════════════════════════════════════════════════

_SYSTEM_REMINDER_OPEN = "<system-reminder>"
_SYSTEM_REMINDER_CLOSE = "</system-reminder>"


class MessageBuilder:
    """拼装 messages + tool_schemas → 最终发给 LLM 的 messages / tools。

    设计要点：
      - ``build_static_context_str`` / ``build_dynamic_reminder`` 均为 **纯函数**
        （classmethod），不持有状态；便于 AgentLoop 按生命周期不同阶段调用。
      - ``build`` 不再接收 deferred/skill/agent summaries；它只消费外部已经
        序列化好的 ``static_context_str`` 和 ``dynamic_reminder``，保证
        AgentLoop 能缓存静态部分、避免每轮重复序列化（PC1 字节级一致的关键）。
    """

    # ──────────────────────────────────────
    # 静态前缀：init 时一次性序列化
    # ──────────────────────────────────────

    @classmethod
    def build_static_context_str(
        cls,
        deferred_tool_summaries: list[dict[str, str]] | None = None,
        skill_summaries: list[SkillSummary] | None = None,
        agent_summaries: list[SubAgentSummary] | None = None,
    ) -> str | None:
        """序列化对整个 run 而言稳定的三块 XML 清单为单一字符串。

        设计契约：
          - 输入来源应为 **对 run 稳定** 的全量目录：
              * deferred_tool_summaries ← ``ToolRegistry.get_deferred_tool_catalog()``
                （PC6：对 discovered 状态免疫的 (name, when_to_use) 列表）
              * skill_summaries         ← ``SkillRegistry.build_skill_summaries()``
              * agent_summaries         ← ``SubAgentRegistry.build_agent_summaries()``
          - 输出为拼好的字符串，由 AgentLoop 在 ``__init__`` 中调用一次并缓存，
            之后每轮 ``build()`` 直接复用。若未来需要"中途热插拔工具/技能"，再
            触发一次重新序列化即可（失效语义清晰）。

        Args:
            deferred_tool_summaries: DEFERRED 工具全量目录（按 name 字母序）。
            skill_summaries:         可用 Skill 摘要。
            agent_summaries:         可委派子 Agent 摘要。

        Returns:
            拼好的字符串；三者全为空时返回 ``None``，调用方无需特殊处理。
        """
        parts: list[str] = []

        # ── <available_tools>（SR2：对 discovered 免疫的稳定清单，PC6）──
        if deferred_tool_summaries:
            buf = ["\n\n<available_tools>\n"]
            buf.append(
                "  The following tools exist but are NOT yet loaded into your tools list.\n"
                "  Rule:\n"
                "    - If a tool name already appears in your tools parameter (you can call it),\n"
                "      DO NOT call search_tools to load it — call it directly.\n"
                "    - Only call search_tools(tool_name=...) for tools listed BELOW that are\n"
                "      NOT in your tools parameter.\n"
                "    - Calling search_tools for an already-loaded tool wastes a turn.\n"
            )
            for s in deferred_tool_summaries:
                buf.append(
                    f"  <tool>\n"
                    f"    <name>{s['name']}</name>\n"
                    f"    <when_to_use>{s.get('when_to_use', '')}</when_to_use>\n"
                    f"  </tool>\n"
                )
            buf.append("</available_tools>\n")
            parts.append("".join(buf))

        # ── <available_skills> ──
        if skill_summaries:
            buf = ["\n\n<available_skills>\n"]
            buf.append(
                "  Use search_skills to discover and activate a skill before using it.\n"
            )
            for s in skill_summaries:
                buf.append(
                    f"  <skill>\n"
                    f"    <name>{s.name}</name>\n"
                    f"    <when_to_use>{s.when_to_use}</when_to_use>\n"
                    f"  </skill>\n"
                )
            buf.append("</available_skills>\n")
            parts.append("".join(buf))

        # ── <available_agents> ──
        if agent_summaries:
            buf = ["\n\n<available_agents>\n"]
            buf.append(
                "  Call call_agent(agent_name=..., task=...) to delegate a task to "
                "the chosen agent.\n"
            )
            for s in agent_summaries:
                buf.append(
                    f"  <agent>\n"
                    f"    <name>{s.agent_name}</name>\n"
                    f"    <when_to_use>{s.when_to_use}</when_to_use>\n"
                    f"  </agent>\n"
                )
            buf.append("</available_agents>\n")
            parts.append("".join(buf))

        if not parts:
            return None
        return "".join(parts)

    # ──────────────────────────────────────
    # 动态尾插：每轮重新生成
    # ──────────────────────────────────────

    @classmethod
    def build_dynamic_reminder(cls) -> str | None:
        """构造 ``<system-reminder>`` 动态提醒正文（PC3 / 方案 B1）。

        v1.4 重构：去 summary 化后跨 session recall 整条路径已废弃，
        本通道不再承载内容。当前始终返回 None；调用方（plan mode 等）会基于
        返回值是否为 None 决定是否追加额外的 user 消息，并按需自行拼接
        非 recall 类的 reminder 正文。

        说明：
          Skill 正文不通过本通道注入。LLM 通过 ``search_skills`` 工具主动加载
          skill，正文经 ToolResult 进入 Memory 历史（详见
          ``docs/工程化设计文档/框架设计/13_prefix_cache.md`` 5d2 决策注记）。

        Returns:
            None（保留接口形态以便未来扩展）。
        """
        return None

    # ──────────────────────────────────────
    # 每轮主入口
    # ──────────────────────────────────────

    def build(
        self,
        messages: list[dict[str, Any]],
        tool_schemas: list[ToolSchema] | None = None,
        static_context_str: str | None = None,
        dynamic_reminder: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """构建最终发给 LLM 的 messages 与 tools。

        组合顺序（PC2 Stable-First Ordering）：

          1. ``system``（messages[0]）：Memory 给出的原始 system message.content 末尾追加
             ``static_context_str``（若有）。由于 ``static_context_str`` 在整个
             run 中保持不变，system message 的完整文本仍是字节级稳定的静态前缀。

          2. ``conversation``：``messages`` 中除 system 外的所有历史消息，按原顺序。

          3. ``<system-reminder>``：若 ``dynamic_reminder`` 非空，追加一条
             独立的 ``role=user`` 消息到历史末尾（PC3 / 方案 B1）。这样所有
             之前的稳定前缀都能被 prefix cache 命中，仅本条动态消息不被缓存。

        Args:
            messages:            Memory 层管理的完整消息列表（含 system）。
            tool_schemas:        ``ToolRegistry.build_tool_schemas()`` 的返回值。
            static_context_str:  init 时通过 ``build_static_context_str`` 序列化
                                 并由 AgentLoop 缓存的静态前缀字符串；可为 ``None``。
            dynamic_reminder:    每轮通过 ``build_dynamic_reminder`` 重新生成的
                                 动态提醒字符串；可为 ``None``。

        Returns:
            ``(built_messages, tools_for_llm)`` 二元组：
              - ``built_messages``：最终发给 LLM 的 messages 列表。
              - ``tools_for_llm``：OpenAI 兼容的 tools 列表；``tool_schemas``
                为空/None 时返回 ``None``。
        """
        # 复制一份，避免修改原始 messages（浅拷贝每条消息字典即可，内容不改结构）
        result = [msg.copy() for msg in messages]

        # ── 步骤 1：把静态前缀追加到 system message 末尾 ──
        if static_context_str:
            for msg in result:
                if msg.get("role") == "system":
                    msg["content"] = (msg.get("content") or "") + static_context_str
                    break

        # ── 步骤 2：动态 reminder 作为独立 role=user 消息尾插（PC3 / 方案 B1）──
        if dynamic_reminder:
            result.append({"role": "user", "content": dynamic_reminder})

        # ── 步骤 3：构建 OpenAI 兼容的 tools 列表 ──
        tools_for_llm: list[dict[str, Any]] | None = None
        if tool_schemas:
            tools_for_llm = [
                {
                    "type": "function",
                    "function": {
                        "name": s.name,
                        "description": s.description,
                        "parameters": s.parameters,
                    },
                }
                for s in tool_schemas
            ]

        return result, tools_for_llm
