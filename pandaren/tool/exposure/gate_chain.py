"""pandaren/tool/exposure/gate_chain.py — 可插拔的过滤门链。

每道门是独立可测试的对象，通过链式组合。
新增过滤维度只需实现 ToolGate Protocol + 注册到链中。

总体设计思路
═══════════
GateChain 决定「每轮对话中 LLM 能看到/调用哪些工具」。

■ 组合逻辑：AND（交集）
  一个工具必须通过 **所有** 门才会暴露给 LLM。
  任何一道门拦截 → 该工具本轮不可见（短路 break）。

■ 为什么不会把工具全过滤掉？
  每道门都遵循「未配置 = 全放行」原则：
    - 对应的 context 字段为 None / 工具自身字段为空 → return True（透明）
    - 只有开发者 **主动配置** 了某个维度，那道门才真正起过滤作用
  所以正常情况下，大多数门是透明的，不会出现"交集为空"。

■ 4 道门分两类：

  持久约束（由注册/配置决定，长期生效）：
    ① AllowListGate   — Agent 级白名单：该 Agent 只允许用哪些工具
    ② EnabledGate      — 运行时动态开关：某工具被临时禁用
    ③ AgentWhitelistGate — 工具级反向白名单：该工具只给哪些 Agent 用

  临时约束（由运行时状态决定，状态结束自动恢复全放行）：
    ④ SkillWhitelistGate — Skill 激活期间，只暴露 Skill 声明的工具
                           （轮次结束 clear_active_skill() 后回到 None = 全放行）

■ 注意：GateChain 只控制「工具是否暴露」，不影响 LLM 的纯文本回复能力。
  即使所有工具都被过滤掉，LLM 依然可以正常生成文本回答用户。

■ GateChain vs GuardChain 区别：

               GateChain（本文件，exposure 层）    GuardChain（execution 层）
  时机        每轮对话 构建 schema 前              工具 真正执行前
  作用        决定 LLM 能「看到」哪些工具           决定 LLM 能「调用」哪些工具
  输入/输出   全部工具列表 → 过滤后列表             单个工具 → 通过 / 拒绝
  类比        餐厅菜单上展示哪些菜品               点菜后厨房检查这道菜能不能做

  两层防护确保：即使 LLM 幻觉调用了一个不该调的工具，
  GuardChain 也能在执行前拦住并返回错误 ToolResult。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..definition.tool import Tool

logger = logging.getLogger("pandaren.tool.exposure.gate_chain")


@dataclass(frozen=True)
class ExposureContext:
    """暴露阶段上下文（每轮构建一次）。"""
    agent_id: str | None = None
    agent_allowed_tools: set[str] | None = None
    skill_allowed_tools: tuple[str, ...] | None = None
    enabled_cache: dict[str, bool] | None = None


@runtime_checkable
class ToolGate(Protocol):
    """单道过滤门协议。"""

    @property
    def name(self) -> str: ...

    def should_pass(self, tool: Tool, context: ExposureContext) -> bool: ...


# ════════════════════════════════════════════════
#  内置门实现
# ════════════════════════════════════════════════

class AllowListGate:
    """Agent 级白名单过滤（第 1 道门）。"""

    @property
    def name(self) -> str:
        return "allow_list"

    def should_pass(self, tool: Tool, context: ExposureContext) -> bool:
        if context.agent_allowed_tools is None:
            return True
        return tool.full_name in context.agent_allowed_tools


class EnabledGate:
    """is_enabled 缓存过滤（第 2 道门）。"""

    @property
    def name(self) -> str:
        return "enabled"

    def should_pass(self, tool: Tool, context: ExposureContext) -> bool:
        if context.enabled_cache is None:
            return True
        return context.enabled_cache.get(tool.full_name, True)


class AgentWhitelistGate:
    """tool.agent_whitelist 匹配过滤（第 3 道门）。"""

    @property
    def name(self) -> str:
        return "agent_whitelist"

    def should_pass(self, tool: Tool, context: ExposureContext) -> bool:
        if not tool.agent_whitelist:
            return True
        if not context.agent_id:
            return True
        return context.agent_id in tool.agent_whitelist


class SkillWhitelistGate:
    """Skill 激活时的工具白名单（第 4 道门）。"""

    @property
    def name(self) -> str:
        return "skill_whitelist"

    def should_pass(self, tool: Tool, context: ExposureContext) -> bool:
        if context.skill_allowed_tools is None:
            return True
        return tool.full_name in context.skill_allowed_tools


# ════════════════════════════════════════════════
#  GateChain
# ════════════════════════════════════════════════

class GateChain:
    """过滤门链。可动态添加/移除门。"""

    def __init__(self, gates: list[ToolGate] | None = None) -> None:
        self._gates: list[ToolGate] = gates or []

    def add(self, gate: ToolGate) -> None:
        """添加一道门。"""
        self._gates.append(gate)

    def filter(self, tools: list[tuple[str, Tool]], ctx: ExposureContext) -> list[tuple[str, Tool]]:
        """依次通过所有门，返回通过全部门的工具列表。

        Args:
            tools: (full_name, Tool) 列表。
            ctx: 暴露上下文。

        Returns:
            通过所有门的 (full_name, Tool) 列表。
        """
        logger.debug(
            "[gate] filter start: tools=%d, gates=%d, agent_id=%s",
            len(tools), len(self._gates), ctx.agent_id,
        )

        result = []
        for full_name, tool in tools:
            passed = True
            for gate in self._gates:
                if not gate.should_pass(tool, ctx):
                    logger.debug(
                        "[gate] FILTERED(%s) %s", gate.name, full_name,
                    )
                    passed = False
                    break
            if passed:
                result.append((full_name, tool))

        logger.debug(
            "[gate] filter done: passed=%d, filtered=%d",
            len(result), len(tools) - len(result),
        )
        return result

    @classmethod
    def default(cls) -> "GateChain":
        """创建默认门链（5 道门，固定顺序）。"""
        return cls(gates=[
            AllowListGate(),
            EnabledGate(),
            AgentWhitelistGate(),
            SkillWhitelistGate(),
        ])
