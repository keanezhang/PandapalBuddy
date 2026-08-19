"""pandaren/skill/registry.py — SkillRegistry 核心实现

职责：
  - Skill 注册与注册时校验（SK1, SK5）
  - Skill 摘要列表构建（SK4 Token 预算）
  - Skill 搜索与一步加载（search_skills）
  - 门禁检查（SK3 allow_auto_trigger）
  - 多来源合并与优先级覆盖
  - DEFERRED Tool 主动提升（KD3）
  - Turn 级 allowed_tools 激活管理（SK2）
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from .models import Skill, SkillResult, SkillSummary
from .exceptions import SkillRegistrationError
# token 估算系数：从全局 constants 统一引用
from ..constants import CHARS_PER_TOKEN as _CHARS_PER_TOKEN

if TYPE_CHECKING:
    from ..tool.registry import ToolRegistry
    from ..tool.definition.tool import Tool
    from ..tool.definition.context import ToolContext
    from ..tool.definition.tool_result import ToolResult
    from ..observability.audit import AuditLog

logger = logging.getLogger("pandaren.skill.registry")

# description 截断上限（SK4）
_DEFAULT_MAX_DESCRIPTION_CHARS: int = 250

# Skill.name 格式校验正则（SK5-N）
# 规则：字母或数字开头，后续允许字母、数字、连字符(-)、下划线(_)
# 不允许：空格、特殊字符、以 -/_ 开头
_SKILL_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9\u4e00-\u9fff][a-zA-Z0-9_\-\u4e00-\u9fff]*$")


class SkillRegistry:
    """Skill 系统核心管理器。

    生命周期：
      - Agent 构建时创建，注册 Skill
      - 每轮 Phase 1 调用 build_skill_summaries() 注入 system prompt
      - search_skills 作为 ALWAYS 级 Tool 注册到 ToolRegistry
      - 每轮结束时 clear_active_skill() 清除激活状态
    """

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry | None = None,
        audit_log: AuditLog | None = None,
        max_description_chars: int = _DEFAULT_MAX_DESCRIPTION_CHARS,
    ) -> None:
        # ── A 类：Skill 定义存储 ──
        self._skills: dict[str, Skill] = {}

        # ── 配置（构造后只读）──
        self._max_description_chars = max_description_chars
        self._tool_registry = tool_registry
        self._audit_log = audit_log

        # ── B 类：运行时状态 ──
        # Turn 级 Skill 激活状态（SK2 执行期约束）
        # None = 不限制（无 Skill 激活或 allowed_tools=None）
        self._active_skill_tools: tuple[str, ...] | None = None

        # 当前激活的 Skill 名称（供 AgentHooks / 观测层查询）
        # search_skills 成功时写入，clear_active_skill() 时清空
        self._active_skill_name: str | None = None

        # 用户手动请求的 Skill 名称集合（Phase 0.5 写入，search_skills 读取后跳过门禁）
        # clear_active_skill() 时一并清空
        self._manually_requested: set[str] = set()

        # ── C 类：Action Skill 桥接 ──
        from .bridge import SkillToolBridge
        self._bridge = SkillToolBridge()
        # 预构建的 Tool 缓存（未注册到 ToolRegistry，search_skills 时才注册）
        self._action_tools_cache: dict[str, "Tool"] = {}  # skill_name → Tool object
        self._action_skill_tools: dict[str, str] = {}  # skill_name → tool.full_name
        self._version: int = 0  # register/unregister 时递增，供脏检查

    # ════════════════════════════════════════════════
    #  配置只读属性
    # ════════════════════════════════════════════════

    @property
    def max_description_chars(self) -> int:
        return self._max_description_chars

    @property
    def tool_registry(self) -> ToolRegistry | None:
        return self._tool_registry

    @property
    def version(self) -> int:
        """注册表版本号，每次 register_skill/unregister_skill 递增。供脏检查用。"""
        return self._version

    # ════════════════════════════════════════════════
    #  注册
    # ════════════════════════════════════════════════

    def register_skill(self, skill: Skill) -> None:
        """注册单个 Skill。

        校验规则（SK5 显式声明）：
          - name 非空
          - content 非空
          - description 超 250 字符自动截断 + WARNING

        同名覆盖规则（场景 5）：
          - 新 Skill.source 优先级 >= 已有 → 覆盖
          - 新 Skill.source 优先级 < 已有 → 跳过 + WARNING
          - 覆盖时写审计事件 SKILL_OVERRIDDEN
        """
        # ── 校验 ──
        if not skill.name or not skill.name.strip():
            raise SkillRegistrationError("Skill.name 不能为空")

        # 【SK5-N】name 格式校验：必须是合法的单 token 标识符
        # 不合规 → 警告 + 拒绝注册（不抛异常，避免一个坏 Skill 导致整个 Agent 启动失败）
        if not _SKILL_NAME_PATTERN.match(skill.name.strip()):
            logger.warning(
                "Skill '%s' 注册被拒绝：name 格式非法。"
                "只允许字母、数字、中文、连字符(-)、下划线(_)，"
                "且不能以 -/_ 开头，不能包含空格。"
                "示例：'arch-design'、'code_review'、'translate'。",
                skill.name,
            )
            return

        if not skill.content or not skill.content.strip():
            raise SkillRegistrationError(
                f"Skill '{skill.name}' 的 content 不能为空"
            )

        if not skill.description:
            raise SkillRegistrationError(
                f"Skill '{skill.name}' 的 description 不能为空"
            )

        if not skill.when_to_use or not skill.when_to_use.strip():
            raise SkillRegistrationError(
                f"Skill '{skill.name}' 的 when_to_use 不能为空"
            )

        # description 超长自动截断（SK4）
        if len(skill.description) > self._max_description_chars:
            truncated_desc = skill.description[:self._max_description_chars]
            logger.warning(
                "Skill '%s' description 超过 %d 字符，已自动截断",
                skill.name, self._max_description_chars,
            )
            # frozen dataclass 不可修改，创建新实例替换（SK1：注册后不可变，此处是注册前修正）
            # 注意：重建时必须透传全部字段（含 Action Skill 的 script/entry_function），
            # 否则 Action Skill 会静默退化为 Knowledge（is_action 变 False）。
            skill = Skill(
                name=skill.name,
                description=truncated_desc,
                when_to_use=skill.when_to_use,
                content=skill.content,
                source=skill.source,
                allowed_tools=skill.allowed_tools,
                allow_auto_trigger=skill.allow_auto_trigger,
                argument_hint=skill.argument_hint,
                tags=skill.tags,
                base_path=skill.base_path,
                script=skill.script,
                entry_function=skill.entry_function,
            )

        # ── 同名覆盖检查 ──
        existing = self._skills.get(skill.name)
        if existing is not None:
            if skill.source < existing.source:
                logger.warning(
                    "Skill '%s' 覆盖被跳过：新来源 %s（优先级 %d）< 已有来源 %s（优先级 %d）",
                    skill.name, skill.source.name, skill.source.value,
                    existing.source.name, existing.source.value,
                )
                return
            # 覆盖：先清理旧 Action Tool（缓存 + 已注册到 ToolRegistry 的定义），
            # 防止 ToolRegistry 残留旧 Tool，导致 _lazy_register_action_tool
            # 的幂等检查永远跳过新 Tool 的注册。
            if existing.is_action:
                self._cleanup_action_tool(skill.name)
            # 覆盖：写审计事件
            self._write_audit_skill_overridden(skill, existing)
            logger.info(
                "Skill '%s' 被覆盖：%s → %s",
                skill.name, existing.source.name, skill.source.name,
            )

        self._skills[skill.name] = skill
        logger.debug(
            "Skill 已注册: %s [source=%s, auto_trigger=%s, tools=%s, action=%s]",
            skill.name, skill.source.name, skill.allow_auto_trigger,
            skill.allowed_tools, skill.is_action,
        )

        # ★ Action Skill 自动桥接：生成 Tool 并注册到 ToolRegistry
        if skill.is_action:
            self._register_action_tool(skill)

        self._version += 1

    def register_skills(self, skills: list[Skill]) -> None:
        """批量注册 Skill。"""
        for skill in skills:
            self.register_skill(skill)

    def unregister_skill(self, name: str) -> bool:
        """注销单个 Skill。

        清理：
          - _skills 注册
          - _action_tools_cache 缓存
          - _action_skill_tools 映射
          - 已延迟注册到 ToolRegistry 的 Action Tool（如有）
          - 当前激活状态（如该 Skill 正激活则清除）

        Args:
            name: Skill 名称。

        Returns:
            True 表示成功注销，False 表示 Skill 不存在。
        """
        if name not in self._skills:
            return False

        skill = self._skills[name]

        # 1. 清理 Action Tool（注销已注册到 ToolRegistry 的 + 清缓存）
        self._cleanup_action_tool(name)

        # 2. 清理激活状态（如当前正是该 Skill）
        if self._active_skill_name == name:
            self._active_skill_name = None
            self._active_skill_tools = None

        # 3. 从注册表中移除
        del self._skills[name]

        logger.info(
            "Skill 已注销: %s [source=%s, action=%s]",
            name, skill.source.name, skill.is_action,
        )
        self._version += 1
        return True

    def _register_action_tool(self, skill: Skill) -> None:
        """预构建 Action Skill 的 Tool 对象并缓存（不注册到 ToolRegistry）。

        Tool 在 search_skills 被调用时才真正注册，确保 LLM 必须先加载指令。
        SK7 Fail-Safe：构建失败时仅记录 WARNING，Skill 退化为 Knowledge 类型使用。
        """
        try:
            tool = self._bridge.create_tool(skill)
            self._action_tools_cache[skill.name] = tool
            self._action_skill_tools[skill.name] = tool.full_name
            logger.debug(
                "Action Skill '%s' → Tool '%s' 已缓存（等待 search_skills 触发注册）",
                skill.name, tool.full_name,
            )
        except Exception as e:
            logger.warning(
                "Action Skill '%s' Tool 构建失败（退化为 Knowledge Skill）: %s",
                skill.name, e,
            )

    def get_action_tool_name(self, skill_name: str) -> str | None:
        """查询 Action Skill 对应的 Tool full_name。"""
        return self._action_skill_tools.get(skill_name)

    # ════════════════════════════════════════════════
    #  查询
    # ════════════════════════════════════════════════

    def get_skill(self, name: str) -> Skill | None:
        """精确查找 Skill。

        安全边界说明：返回完整 Skill 对象（含 content），不经过
        allow_auto_trigger 门禁检查。这是有意设计——get_skill 面向
        应用层开发者（受信任），门禁仅约束 LLM 自动触发路径（search_skills）。
        """
        return self._skills.get(name)

    def list_skills(self) -> tuple[Skill, ...]:
        """枚举所有已注册 Skill。

        返回 tuple 副本（非内部 dict.values() 视图），
        防止外部通过引用修改内部状态。
        """
        return tuple(self._skills.values())

    def skill_count(self) -> int:
        """已注册 Skill 数量。"""
        return len(self._skills)

    # ════════════════════════════════════════════════
    #  手动触发（Phase 0.5 专用）
    # ════════════════════════════════════════════════

    def invoke_skill_manually(
        self, name: str,
    ) -> SkillResult:
        """用户手动触发 Skill 的预验证（Phase 0.5 专用）。

        **不执行 Skill 加载**，只做两件事：
          1. 验证 Skill 是否存在
          2. 标记 _manually_requested（让后续 search_skills 跳过门禁）

        实际的 content 渲染、Tool 提升、激活白名单、审计——全部由
        LLM 在 step 循环中调用 search_skills 完成，复用同一条路径。

        Args:
            name: Skill 名称（精确匹配，支持大小写容错）

        Returns:
            SkillResult(success=True, content=hint_text) — Skill 存在，hint 引导 LLM 加载
            SkillResult(success=False, error="...") — Skill 不存在
        """
        # 1. 精确匹配（复用 _match_skills 逻辑，含大小写容错）
        skill = self.get_skill(name)
        if skill is None:
            matches = self._match_skills(name)
            if not matches:
                return SkillResult(
                    success=False,
                    error=f"Skill '{name}' 不存在",
                    skill_name=name,
                )
            skill = matches[0]

        # 2. 标记为"用户手动请求"——search_skills 检测到此标记后跳过门禁
        self._manually_requested.add(skill.name)

        logger.info(
            "Phase 0.5: Skill '%s' 验证通过，已标记为用户手动请求，"
            "等待 LLM 调用 search_skills 完成加载。",
            skill.name,
        )

        # 3. 返回 hint 文案（注入 STM，引导 LLM 调 search_skills）
        hint = (
            f"用户明确要求使用技能「{skill.name}」，"
            f"请立即调用 search_skills(skill_name=\"{skill.name}\") 加载该技能，"
            f"然后严格按照技能指引完成任务。"
        )

        return SkillResult(
            success=True,
            content=hint,
            skill_name=skill.name,
        )

    # ════════════════════════════════════════════════
    #  摘要列表（注入 system prompt）
    # ════════════════════════════════════════════════

    def build_skill_summaries(
        self, context_window: int = 128_000,
    ) -> list[SkillSummary]:
        """构建 Skill 摘要列表，受 1% 上下文预算约束（SK4）。

        Args:
            context_window: 上下文窗口大小（token 数），默认 128K。

        Returns:
            按优先级排序的摘要列表（高优先级在前）。
            超出 1% 预算时，从低优先级开始裁剪。
        """
        if not self._skills:
            return []

        # 按 source 优先级排序（高优先级在前，先保留）
        sorted_skills = sorted(
            self._skills.values(),
            key=lambda s: s.source.value,
            reverse=True,
        )

        budget_tokens = int(context_window * 0.01)
        summaries: list[SkillSummary] = []
        used_tokens = 0

        for skill in sorted_skills:
            desc = self._truncate_description(
                skill.when_to_use, self._max_description_chars,
            )
            # 粗略估算：name + when_to_use 的 token 数
            entry_tokens = (len(skill.name) + len(desc)) // _CHARS_PER_TOKEN + 5
            if used_tokens + entry_tokens > budget_tokens:
                logger.debug(
                    "Skill 摘要预算已满（%d/%d tokens），跳过 '%s'",
                    used_tokens, budget_tokens, skill.name,
                )
                continue
            summaries.append(SkillSummary(name=skill.name, when_to_use=desc))
            used_tokens += entry_tokens

        return summaries

    # ════════════════════════════════════════════════
    #  search_skills — 一步到位搜索 + 加载
    # ════════════════════════════════════════════════

    def search_skills(self, skill_name: str, context: ToolContext) -> ToolResult:
        """按 skill_name 精准搜索并直接加载 Skill（一步到位）。

        流程：
          1. 按 skill_name 精准匹配 → 找到对应 Skill
          2. 门禁检查（allow_auto_trigger）
          3. 渲染 content（替换 $ARGUMENTS）
          4. 主动提升 DEFERRED Tool（KD3）
          5. 设置 _active_skill_tools（SK2）
          6. 写审计事件 SKILL_INVOKED
          7. 返回 ToolResult

        Args:
            skill_name: LLM 传入的技能名称（必须与 Skill.name 精准匹配）。
            context: ToolContext（复用 Tool 层类型，不新建）。

        Returns:
            ToolResult（复用 Tool 层类型）。
        """
        from ..tool.definition.tool_result import ToolResult

        if not self._skills:
            return ToolResult(
                success=True,
                data="当前没有注册任何技能。",
                tool_name="search_skills",
            )

        # 1. 精准匹配
        matches = self._match_skills(skill_name)

        if not matches:
            return ToolResult(
                success=True,
                data=f"未找到名称为 '{skill_name}' 的技能。",
                tool_name="search_skills",
            )

        skill = matches[0]

        # 2. 门禁检查（SK3）
        #    如果该 Skill 在 _manually_requested 中（用户手动触发），则跳过门禁
        is_manual = skill.name in self._manually_requested
        if is_manual:
            self._manually_requested.discard(skill.name)  # 一次性消费，防止后续滥用
            logger.debug(
                "search_skills: Skill '%s' 由用户手动请求，跳过 auto_trigger 门禁",
                skill.name,
            )
        elif not self._check_auto_trigger(skill, is_auto=True):
            self._write_audit_auto_trigger_denied(skill, context)
            return ToolResult(
                success=False,
                error=f"技能 '{skill.name}' 需要手动触发，不可自动使用。"
                      f"请让用户明确指定使用此技能。",
                tool_name="search_skills",
            )

        # 3. 渲染 content
        content = self._render_content(skill, skill_name)

        # 4. 主动提升 DEFERRED Tool（KD3）
        #    对 Action Skill：延迟注册 Tool 到 ToolRegistry + promote
        tools_to_promote: list[str] = list(skill.allowed_tools or [])
        action_tool_name = self._action_skill_tools.get(skill.name)
        if action_tool_name:
            # 延迟注册：此时才把缓存的 Tool 注册到 ToolRegistry
            self._lazy_register_action_tool(skill.name)
            tools_to_promote.append(action_tool_name)

        if tools_to_promote and self._tool_registry:
            self._promote_deferred_tools(tuple(tools_to_promote), context)

        # 5. 设置 _active_skill_tools（SK2 Turn 级激活）
        #    对 Action Skill：将生成的工具也加入激活白名单
        active_tools = list(skill.allowed_tools or [])
        if action_tool_name:
            active_tools.append(action_tool_name)
        self._activate_skill_tools(tuple(active_tools) if active_tools else skill.allowed_tools)

        # 记录当前激活的 Skill 名称（供 AgentHooks 触发 on_skill_activated）
        self._active_skill_name = skill.name

        # 6. 写审计事件
        content_tokens = len(content) // _CHARS_PER_TOKEN
        self._write_audit_skill_invoked(skill, content_tokens, context)

        # 7. 构建返回内容
        action_tool = self._action_skill_tools.get(skill.name)
        if action_tool:
            result_text = (
                f"🔧 已加载技能 [{skill.name}]\n\n{content}\n\n"
                f"---\n⚡ 执行方式：请直接调用工具 `{action_tool}` "
                f"（参数 schema 已就绪），按上述指令处理返回结果。"
            )
        else:
            result_text = f"🔧 已加载技能 [{skill.name}]\n\n{content}"

        return ToolResult(
            success=True,
            data=result_text,
            tool_name="search_skills",
        )

    # ════════════════════════════════════════════════
    #  Turn 级 Skill 激活管理（SK2）
    # ════════════════════════════════════════════════

    def get_active_skill_tools(self) -> tuple[str, ...] | None:
        """获取当前激活的工具白名单。

        供 ToolRegistry 在 build_tool_schemas() 时读取，做工具过滤。
        None = 不限制。
        """
        return self._active_skill_tools

    def get_active_skill_name(self) -> str | None:
        """获取当前激活的 Skill 名称。

        供 AgentLoop 在触发 on_skill_activated / on_skill_cleared hook 前查询。
        None = 无 Skill 激活。
        """
        return self._active_skill_name

    def clear_active_skill(self) -> None:
        """清除 Skill 激活状态。

        由 AgentLoop 在每轮结束时调用（与 rate_limiter.reset_turn() 同时机）。
        同时清空 _manually_requested 标记（Turn 级生命周期）。
        """
        self._active_skill_tools = None
        self._manually_requested.clear()
        self._active_skill_name = None

    def _lazy_register_action_tool(self, skill_name: str) -> None:
        """延迟注册：将缓存的 Action Skill Tool 注册到 ToolRegistry。

        幂等：已注册则跳过。仅在 search_skills 被调用时触发。
        """
        if self._tool_registry is None:
            return
        cached_tool = self._action_tools_cache.get(skill_name)
        if cached_tool is None:
            return
        # 幂等：检查是否已注册
        if self._tool_registry.get_tool(cached_tool.full_name) is not None:
            return
        self._tool_registry.register_tool(cached_tool)
        # logger.info(
        #     "Action Skill Tool 延迟注册: '%s' → ToolRegistry",
        #     cached_tool.full_name,
        # )

    def _cleanup_action_tool(self, skill_name: str) -> None:
        """清理指定 Skill 的 Action Tool（缓存 + 已注册到 ToolRegistry 的定义）。

        覆盖 / 注销场景复用：
        - 同名 Skill 被更高优先级覆盖时，必须先注销旧 Tool，否则 ToolRegistry
          残留旧定义，_lazy_register_action_tool 的幂等检查会跳过新 Tool 的注册；
        - unregister_skill 注销 Skill 时同步清理。
        """
        # 1. 注销已注册到 ToolRegistry 的旧 Tool（如有）
        action_tool_name = self._action_skill_tools.get(skill_name)
        if action_tool_name and self._tool_registry is not None:
            try:
                self._tool_registry.unregister_tool(action_tool_name)
            except Exception as e:
                # E4 Fail-Safe：注销失败不阻断 Skill 覆盖/注销主流程
                logger.debug(
                    "注销 Action Tool '%s' 失败: %s", action_tool_name, e,
                )

        # 2. 清理缓存
        self._action_tools_cache.pop(skill_name, None)
        self._action_skill_tools.pop(skill_name, None)

    def _activate_skill_tools(self, allowed_tools: tuple[str, ...] | None) -> None:
        """设置或合并 Skill 激活期间的工具白名单。

        多 Skill 同时激活（同一 turn 内多次 search_skills）时取并集。
        任一 Skill 的 allowed_tools=None → 整体不限制（继承 Agent 默认工具集）。
        """
        if allowed_tools is None:
            # None = 不限制（继承 Agent 默认），一旦出现就解除所有限制
            self._active_skill_tools = None
            return

        if self._active_skill_tools is None:
            # 首次激活：直接设置白名单
            self._active_skill_tools = allowed_tools
            return

        # 已有激活的白名单，取并集
        merged = set(self._active_skill_tools) | set(allowed_tools)
        self._active_skill_tools = tuple(sorted(merged))

    # ════════════════════════════════════════════════
    #  内部方法
    # ════════════════════════════════════════════════

    def _match_skills(self, query: str) -> list[Skill]:
        """按 name 精准匹配 Skill。

        LLM 通过 SkillSummary 只能看到 name 和 when_to_use，
        因此应按 name 精准选取，避免模糊匹配导致误加载。

        Returns:
            匹配到的 Skill 列表（0 或 1 个元素）。
        """
        query_stripped = query.strip()
        if not query_stripped:
            return []

        skill = self._skills.get(query_stripped)
        if skill is not None:
            return [skill]

        # 大小写容错：尝试忽略大小写匹配
        query_lower = query_stripped.lower()
        for skill in self._skills.values():
            if skill.name.lower() == query_lower:
                return [skill]

        return []

    def _check_auto_trigger(self, skill: Skill, is_auto: bool) -> bool:
        """门禁检查（SK3）。

        Returns:
            True = 允许调用，False = 拒绝。
        """
        if not is_auto:
            return True
        if not skill.allow_auto_trigger:
            logger.warning(
                "Skill 自动触发被拒绝: %s (allow_auto_trigger=False)",
                skill.name,
            )
            return False
        return True

    def _render_content(self, skill: Skill, arguments: str) -> str:
        """渲染 Skill 正文（替换 $ARGUMENTS 等变量）。

        SK7 Fail-Safe：变量替换失败时返回原始 content，不注入损坏内容。
        """
        try:
            content = skill.content
            if "$ARGUMENTS" in content:
                content = content.replace("$ARGUMENTS", arguments or "")
            return content
        except Exception as e:
            logger.warning(
                "Skill '%s' 渲染失败: %s，返回原始 content",
                skill.name, e,
            )
            return skill.content

    def _truncate_description(self, text: str, max_chars: int) -> str:
        """描述截断（SK4 Token 预算）。"""
        if len(text) <= max_chars:
            return text
        return text[:max_chars - 3] + "..."

    def _promote_deferred_tools(
        self,
        tool_names: tuple[str, ...],
        context: ToolContext,
    ) -> None:
        """将 allowed_tools 中的 DEFERRED Tool 提升到 discovered_set（KD3）。

        通过 ToolRegistry 的公开接口操作，不直接访问内部状态。
        ALWAYS Tool 或不存在的 Tool 名静默跳过（E4 Fail-Safe）。
        """
        if self._tool_registry is None:
            return

        step_n = getattr(context, "step_n", 0)
        for name in tool_names:
            try:
                self._tool_registry.promote_to_discovered(name, step_n)
            except Exception as e:
                logger.debug(
                    "promote_to_discovered('%s') 失败: %s（静默跳过）",
                    name, e,
                )

    # ════════════════════════════════════════════════
    #  审计事件写入
    # ════════════════════════════════════════════════

    def _write_audit_skill_invoked(
        self, skill: Skill, content_tokens: int, context: ToolContext,
    ) -> None:
        """写入 SKILL_INVOKED 审计事件。"""
        if self._audit_log is None:
            return
        try:
            from ..observability.types import AuditEventType
            self._audit_log.write_sync(
                AuditEventType.SKILL_INVOKED,
                agent_id=getattr(context, "agent_id", ""),
                run_id=getattr(context, "run_id", ""),
                detail=f"Skill invoked: {skill.name}, content_tokens={content_tokens}",
                session_id=getattr(context, "session_id", ""),
                step_n=getattr(context, "step_n", None),
                tool_name=f"skill:{skill.name}",
            )
        except Exception as e:
            logger.warning("Skill 审计写入失败: %s", e)

    def _write_audit_auto_trigger_denied(
        self, skill: Skill, context: ToolContext,
    ) -> None:
        """写入 SKILL_AUTO_TRIGGER_DENIED 审计事件。"""
        if self._audit_log is None:
            return
        try:
            from ..observability.types import AuditEventType
            self._audit_log.write_sync(
                AuditEventType.SKILL_AUTO_TRIGGER_DENIED,
                agent_id=getattr(context, "agent_id", ""),
                run_id=getattr(context, "run_id", ""),
                detail=f"Skill auto-trigger denied: {skill.name}",
                session_id=getattr(context, "session_id", ""),
                step_n=getattr(context, "step_n", None),
                tool_name=f"skill:{skill.name}",
            )
        except Exception as e:
            logger.warning("Skill 审计写入失败: %s", e)

    def _write_audit_skill_overridden(
        self, new_skill: Skill, old_skill: Skill,
    ) -> None:
        """写入 SKILL_OVERRIDDEN 审计事件。"""
        if self._audit_log is None:
            return
        try:
            from ..observability.types import AuditEventType
            self._audit_log.write_sync(
                AuditEventType.SKILL_OVERRIDDEN,
                agent_id="",
                run_id="",
                detail=(
                    f"Skill overridden: {new_skill.name}, "
                    f"{old_skill.source.name} → {new_skill.source.name}"
                ),
            )
        except Exception as e:
            logger.warning("Skill 审计写入失败: %s", e)

    def __repr__(self) -> str:
        return (
            f"SkillRegistry(skills={len(self._skills)}, "
            f"active_tools={self._active_skill_tools})"
        )
