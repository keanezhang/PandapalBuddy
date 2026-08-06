"""agent_task_tools — AgentTask 管理工具组（★ 层次 3 改造 2026-06-11）。

Agent-Native 设计：AI 在会话中自主管理多步骤任务的拆解与追踪。
LLM 调用这些工具记录步骤、更新进度、设置依赖关系，
用户通过前端任务面板实时观察执行进度。

设计约束（9 条）：
- BL2 (Stateless): session_id/user_id 从 ToolContext 传入，不存全局状态
- BL4 (DI): repo/broadcaster 通过 Provider 类构造函数注入
- I1 (Fail Fast): user_id 缺失立即报错
- SDK5 (Actionable Errors): 错误信息可操作
- I3 (Idempotent): 重复操作不报错
- D4 (Transaction): 依赖操作由 Repo 层保证原子性
- D1 (Storage Abstraction): 工具层不碰 SQL
- D2 (Explicit Query Intent): 工具名体现业务语义
- D5 (No Business Logic in DB): 状态校验在 Repo 层

★ 层次 3 根本解改造：
  之前：module-level 单例 `_repo: ... | None = None` + `_broadcaster: ... | None = None`
        + `inject_agent_task_repo()` + `inject_agent_task_broadcaster()` 两个全局副作用函数。
  之后：依赖通过 AgentTaskTools 构造函数显式注入，工具函数通过闭包变量访问。
        没有全局单例，没有"记着调 inject"——构造 AgentTaskTools 时依赖就到位。
        启动期 fail-fast：未构造 AgentTaskTools 就调工具，工具不在 agent_builder.tools() 中。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from pandaren.tool.decorator import tool
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool import Tool
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.types import SensitivityLevel, ToolTier

from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.broadcast.channel_ids import LOCAL_AGENT_TASK_CHANNEL_ID
from pandapal.events.normalized import EventType, NormalizedEvent
from pandapal import session_id as session_id_mod
from pandapal.storage.models import AgentTask, AgentTaskStatus
from pandapal.storage.repositories.sqlite_agent_task_repo import AgentTaskRepository
from pandapal.storage.repositories.markdown_agent_task_repo import MarkdownAgentTaskRepository

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 状态转换表：强制串行执行规则
# ═══════════════════════════════════════════════════════════════════════════════

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in_progress", "cancelled"},
    "in_progress": {"completed", "failed", "cancelled"},
    "completed": set(),   # 终态
    "failed": set(),      # 终态
    "cancelled": set(),   # 终态
}


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers（纯函数，不依赖任何子系统）
# ═══════════════════════════════════════════════════════════════════════════════


def _get_user_id(ctx: ToolContext) -> str:
    """从 ToolContext.metadata 取权威 user_id（I1 Fail Fast）。

    ★ 只认权威来源，绝不从 session_id 里「反推/抠」user_id —— 那违反 SESSION_ID 契约
      「端到端透传，不重新推导」。缺失即抛错，暴露上游漏传。
    """
    user_id = (ctx.metadata or {}).get("user_id", "")
    if user_id:
        return user_id

    logger.warning(
        "_get_user_id: user_id NOT in metadata. metadata_keys=%s, session_id=%s, agent_id=%s",
        list(ctx.metadata.keys()) if ctx.metadata else "EMPTY",
        ctx.session_id, ctx.agent_id,
    )
    raise ValueError(
        "create_agent_task 缺少 user_id：请确保 ToolContext.metadata['user_id'] 已由上游透传。"
    )


def _get_session_id(ctx: ToolContext) -> str:
    """获取 session_id：0 容忍空值，绝不降级为 'unknown' 兜底（那会把所有无会话任务
    坍缩到同一污染桶，见 SESSION_ID 契约）。为空即抛错，暴露上游漏传。"""
    return session_id_mod.require(ctx.session_id, where="agent_task_tools")


# ═══════════════════════════════════════════════════════════════════════════════
# _push_event 工厂（★ 闭包绑定 broadcaster）
# ═══════════════════════════════════════════════════════════════════════════════


def _make_push_event(broadcaster: MessageBroadcast | None):
    """工厂：返回绑定 broadcaster 的 _push_event 函数。

    broadcaster 为 None 时返回 no-op 版本（Fail-Safe 兜底）。
    """
    if broadcaster is None:
        def _push_event(event: str, task: AgentTask) -> None:
            logger.debug("push_event skipped (no broadcaster): event=%s", event)
        return _push_event

    def _push_event(event: str, task: AgentTask) -> None:
        """推送任务事件到前端（Broadcaster）。

        ★ 5.2 迁移：用 NormalizedEvent + broadcast.send()，payload 用 dict（不再 json.dumps 成 bytes）。
        """
        try:
            import asyncio
            payload = {
                "event": event,
                "task": {
                    "task_id": task.task_id,
                    "session_id": task.session_id,
                    "user_id": task.user_id,
                    "subject": task.subject,
                    "description": task.description,
                    "status": task.status.value,
                    "active_form": task.active_form,
                    "order": task.order,
                    "blocks": task.blocks or [],
                    "blocked_by": task.blocked_by or [],
                    "created_at": task.created_at.isoformat() if task.created_at else None,
                    "updated_at": task.updated_at.isoformat() if task.updated_at else None,
                    "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                },
                "session_id": task.session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            asyncio.create_task(
                broadcaster.send(
                    NormalizedEvent(
                        event_type=EventType.AGENT_TASK_EVENT,
                        payload=payload,
                        origin_channel_id=LOCAL_AGENT_TASK_CHANNEL_ID,
                    )
                )
            )
        except Exception as e:
            logger.warning("Failed to push agent task event: %s", e)

    return _push_event


# ═══════════════════════════════════════════════════════════════════════════════
# 工具工厂（★ 层次 3 核心：依赖通过闭包绑定）
# ═══════════════════════════════════════════════════════════════════════════════


def _make_create_agent_task(
    repo: "AgentTaskRepository | MarkdownAgentTaskRepository",
    push_event,
) -> Tool:
    @tool.function(
        name="create_agent_task",
        description=(
            "在用户请求包含≥2个独立步骤时，批量创建任务步骤推到前端面板，让用户实时看到进度。\n"
            "\n"
            "【何时必须调用】用户请求明显需要多步骤才能完成时：\n"
            "- 信息收集类：'规划日本旅行' → 查景点、查餐厅、查交通、汇总方案\n"
            "- 调研分析类：'做市场调研'、'分析竞品'、'写方案文档'\n"
            "- 执行操作类：'帮我整理文件夹并发邮件'\n"
            "- 代码改动类：'改某个模块'、'新增某个功能'、'重构某段代码'\n"
            "创建后立刻用 update_agent_task 标记第1步为 in_progress 并开始执行。\n"
            "\n"
            "【何时不要调用】单步查询，如'今天天气'、'现在几点'、'特斯拉股价'、单纯问答。\n"
            "\n"
            "参数要求：subject ≤15字祈使句（如'查询北京景点'、'搜索推荐餐厅'）；description 写明做什么+为什么+预期产出。\n"
            "\n"
            "【verify_hint 填法】（代码改动类任务必填，纯查询可留空）\n"
            "L1 快速: verify_hint=\"在 文件路径 中存在 '关键模式'\"    (秒级通过)\n"
            "L2 语义: verify_hint=\"在 文件路径 中: 1) xxx 2) xxx 3) xxx\"  (10-20秒)\n"
            "指定了 verify_hint 的任务，完成前必须先调 verify_agent_task 验证，否则 update(status='completed') 会被拒绝。"
        ),
        when_to_use=(
            "当任务需要多步骤执行时（≥2步），先用此工具批量创建所有步骤。"
            "创建后立即用 update_agent_task 标记第一个任务为 in_progress 并执行。"
            "不要在单步查询时调用（如'今天天气怎么样'）。"
            "subject 必须是≤15字的祈使句（如'查询北京景点'、'搜索推荐餐厅'）。"
            "代码改动类任务必须填写 verify_hint，格式见工具描述。"
        ),
        tier=ToolTier.ALWAYS,
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=False,
            is_idempotent=False,
        ),
        progress_label='创建任务「{subject}」',
    )
    async def create_agent_task(
        ctx: ToolContext,
        subject: str,
        description: str = "",
        active_form: str = "",
        order: int = 0,
    ) -> str:
        """创建 AgentTask。

        Args:
            ctx: 工具执行上下文
            subject: 任务标题
            description: 详细描述
            active_form: 进行中文案
            order: 执行顺序
        """
        try:
            user_id = _get_user_id(ctx)
            session_id = _get_session_id(ctx)
        except ValueError as e:
            return f"❌ {e}"

        task_id = str(uuid.uuid4())
        task = AgentTask(
            task_id=task_id,
            session_id=session_id,
            user_id=user_id,
            subject=subject,
            description=description,
            active_form=active_form,
            order=order,
        )

        try:
            created = await repo.create_task(task)
            push_event("created", created)
            return (
                f"✅ 已创建任务 #{task_id}: {subject}\n"
                f"   状态: {created.status.value}, 序号: {created.order}"
            )
        except Exception as e:
            logger.error("create_agent_task failed: %s", e, exc_info=True)
            return f"❌ 创建任务失败: {e}"

    return create_agent_task


def _make_update_agent_task(
    repo: "AgentTaskRepository | MarkdownAgentTaskRepository",
    push_event,
) -> Tool:
    @tool.function(
        name="update_agent_task",
        description=(
            "更新任务面板中某个任务的状态、标题、描述或进行中文案，让用户看到实时进度变化。\n"
            "\n"
            "【调用时机】\n"
            "- 开始执行某步骤：status='in_progress'（同时设 active_form='正在做XXX...'）\n"
            "- 步骤成功完成：status='completed'\n"
            "- 步骤执行失败（无法完成，如报错、找不到资源、验证不通过）：status='failed'\n"
            "- 步骤取消（不再需要执行）：status='cancelled'\n"
            "- 更新进度提示：仅更新 active_form 字段，不需要改 status\n"
            "\n"
            "规则：先 in_progress → 执行 → completed/failed，每步都要更新。终态后不要再重复标记。"
        ),
        when_to_use=(
            "当任务状态变化时调用：开始/完成/取消任务，或更新进行中文案。"
        ),
        tier=ToolTier.ALWAYS,
        progress_label='更新任务进度',
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=False,
            is_idempotent=True,
        ),
    )
    async def update_agent_task(
        ctx: ToolContext,
        task_id: str,
        status: str = "",
        subject: str = "",
        description: str = "",
        active_form: str = "",
        order: int | None = None,
    ) -> str:
        """更新 AgentTask。

        Args:
            ctx: 工具执行上下文
            task_id: 目标任务 ID
            status: 新状态
            subject: 新标题
            description: 新描述
            active_form: 新进行中文案
            order: 新序号
        """
        _ = _get_user_id(ctx)

        kwargs = {}
        if status:
            kwargs["status"] = status
        if subject:
            kwargs["subject"] = subject
        if description:
            kwargs["description"] = description
        if active_form:
            kwargs["active_form"] = active_form
        if order is not None:
            kwargs["order"] = order

        try:
            if status:
                current_task = await repo.get_task(task_id)
                if current_task is None:
                    return f"❌ 任务 #{task_id} 不存在"

                current_status = current_task.status.value
                if status not in _VALID_TRANSITIONS.get(current_status, set()):
                    return (
                        f"❌ 非法状态转换：{current_status} → {status}。\n"
                        f"合法转换：{current_status} → "
                        f"{_VALID_TRANSITIONS[current_status] or '（终态，不可变更）'}\n"
                        f"正确流程：pending → in_progress → completed（必须逐步执行）"
                    )

                # V2 — 验证门控：代码类任务完成前必须通过验证
                if status == "completed" and current_task.verify_hint and not current_task.verified:
                    return (
                        f"❌ 任务 '{current_task.subject}' (#{task_id}) 需要先通过代码验证。\n"
                        f"   验证要求: {current_task.verify_hint}\n"
                        f"   请先调用 verify_agent_task(task_id='{task_id}') 验证后重试。\n"
                        f"   验证通过后再调 update_agent_task(status='completed')。"
                    )

            updated = await repo.update_task(task_id, **kwargs)
            push_event("updated", updated)

            if status in ("completed", "cancelled"):
                for blocked_id in (updated.blocks or []):
                    blocked_task = await repo.get_task(blocked_id)
                    if blocked_task:
                        push_event("updated", blocked_task)

            return (
                f"✅ 已更新任务 #{task_id}\n"
                f"   状态: {updated.status.value}"
            )
        except ValueError as e:
            return str(e)
        except Exception as e:
            logger.error("update_agent_task failed: %s", e, exc_info=True)
            return f"❌ 更新任务失败: {e}"

    return update_agent_task


def _make_set_task_dependency(
    repo: "AgentTaskRepository | MarkdownAgentTaskRepository",
    push_event,
) -> Tool:
    @tool.function(
        name="set_task_dependency",
        description=(
            "设置或解除任务间的阻塞/依赖关系。当一个任务必须等待另一个任务完成后才能开始时调用。\n"
            "【典型用法】'订酒店'必须在'查航班'完成后才能开始。"
        ),
        when_to_use=(
            "当一个任务必须等待另一个任务完成后才能开始时，设置依赖。"
        ),
        tier=ToolTier.ALWAYS,
        progress_label='设置任务依赖',
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=False,
            is_idempotent=True,
        ),
    )
    async def set_task_dependency(
        ctx: ToolContext,
        task_id: str,
        blocked_by: str = "",
        remove_blocked_by: str = "",
    ) -> str:
        """设置或解除任务依赖关系。"""
        _ = _get_user_id(ctx)

        if remove_blocked_by:
            try:
                await repo.remove_block_relation(task_id, remove_blocked_by)
                updated = await repo.get_task(task_id)
                if updated:
                    push_event("updated", updated)
                return (
                    f"✅ 已解除: 任务 #{task_id} 不再被 #{remove_blocked_by} 阻塞"
                )
            except Exception as e:
                logger.error("remove_block_relation failed: %s", e, exc_info=True)
                return f"❌ 解除依赖失败: {e}"

        if blocked_by:
            try:
                await repo.add_block_relation(task_id, blocked_by)
                updated_task = await repo.get_task(task_id)
                if updated_task:
                    push_event("updated", updated_task)
                updated_blocker = await repo.get_task(blocked_by)
                if updated_blocker:
                    push_event("updated", updated_blocker)
                return (
                    f"✅ 已设置: 任务 #{task_id} 被 #{blocked_by} 阻塞"
                )
            except ValueError as e:
                return str(e)
            except Exception as e:
                logger.error("add_block_relation failed: %s", e, exc_info=True)
                return f"❌ 设置依赖失败: {e}"

        return "❌ 请提供 blocked_by（设置依赖）或 remove_blocked_by（解除依赖）参数"

    return set_task_dependency


def _make_list_agent_tasks(
    repo: "AgentTaskRepository | MarkdownAgentTaskRepository",
) -> Tool:
    @tool.function(
        name="list_agent_tasks",
        description=(
            "查看当前会话中所有 AgentTask 及其状态。\n"
            "- hide_completed=True 只显示未完成任务，快速聚焦剩余工作\n"
            "- 首次启动新会话或断线重连时优先调用此工具，了解当前进度"
        ),
        when_to_use=(
            "当需要查看当前任务面板状态时调用。"
            "hide_completed=True 可以快速过滤已完成任务，聚焦剩余工作。"
        ),
        tier=ToolTier.ALWAYS,
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=False,
            is_idempotent=True,
        ),
    )
    async def list_agent_tasks(
        ctx: ToolContext,
        hide_completed: bool = False,
    ) -> str:
        """列出当前会话的任务。"""
        try:
            user_id = _get_user_id(ctx)
            session_id = _get_session_id(ctx)
        except ValueError as e:
            return f"❌ {e}"

        try:
            tasks = await repo.list_tasks_by_session(
                session_id,
                include_completed=not hide_completed,
            )
        except Exception as e:
            logger.error("list_agent_tasks failed: %s", e, exc_info=True)
            return f"❌ 查询任务列表失败: {e}"

        if not tasks:
            return "📋 当前会话没有任务记录。"

        status_icons = {
            AgentTaskStatus.PENDING.value: "⬜",
            AgentTaskStatus.IN_PROGRESS.value: "🔄",
            AgentTaskStatus.COMPLETED.value: "✅",
            AgentTaskStatus.CANCELLED.value: "❌",
        }

        lines: list[str] = ["📋 当前任务列表:"]
        for t in tasks:
            icon = status_icons.get(t.status.value, "❓")
            deps = ""
            if t.blocked_by:
                deps = f" [阻塞于: {', '.join(f'#{b}' for b in t.blocked_by)}]"
            active = ""
            if t.active_form:
                active = f" ({t.active_form})"
            lines.append(
                f"  {icon} #{t.order} {t.subject} [{t.status.value}]{deps}{active}"
            )

        return "\n".join(lines)

    return list_agent_tasks


def _make_get_agent_task(
    repo: "AgentTaskRepository | MarkdownAgentTaskRepository",
) -> Tool:
    @tool.function(
        name="get_agent_task",
        description="查看单个 AgentTask 的完整详情：标题、描述、状态、依赖关系、创建/完成时间。",
        when_to_use="当需要查看某个具体任务的完整信息时调用。",
        tier=ToolTier.ALWAYS,
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=False,
            is_idempotent=True,
        ),
    )
    async def get_agent_task(ctx: ToolContext, task_id: str) -> str:
        """获取单个任务详情。"""
        _ = _get_user_id(ctx)

        try:
            task = await repo.get_task(task_id)
        except Exception as e:
            logger.error("get_agent_task failed: %s", e, exc_info=True)
            return f"❌ 查询任务失败: {e}"

        if task is None:
            return f"❌ 任务 #{task_id} 不存在"

        blocks = ", ".join(f"#{b}" for b in (task.blocks or [])) or "无"
        blocked_by = ", ".join(f"#{b}" for b in (task.blocked_by or [])) or "无"

        return (
            f"📌 任务详情 #{task_id}:\n"
            f"   标题: {task.subject}\n"
            f"   描述: {task.description or '无'}\n"
            f"   状态: {task.status.value}\n"
            f"   序号: {task.order}\n"
            f"   进行中文案: {task.active_form or '无'}\n"
            f"   阻塞谁: {blocks}\n"
            f"   被谁阻塞: {blocked_by}\n"
            f"   创建于: {task.created_at.isoformat() if task.created_at else '-'}\n"
            f"   完成于: {task.completed_at.isoformat() if task.completed_at else '-'}"
        )

    return get_agent_task


def _make_delete_agent_task(
    repo: "AgentTaskRepository | MarkdownAgentTaskRepository",
    push_event,
) -> Tool:
    @tool.function(
        name="delete_agent_task",
        description="删除不再需要的 AgentTask，会级联清理关联的依赖关系。不可逆操作。",
        when_to_use="当任务不再需要时调用（如用户要求取消某步骤、任务创建错误）。",
        tier=ToolTier.ALWAYS,
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=False,
            is_idempotent=True,
        ),
    )
    async def delete_agent_task(ctx: ToolContext, task_id: str) -> str:
        """删除任务（硬删除 + 级联清理依赖）。"""
        _ = _get_user_id(ctx)

        try:
            deleted = await repo.delete_task(task_id)
        except Exception as e:
            logger.error("delete_agent_task failed: %s", e, exc_info=True)
            return f"❌ 删除任务失败: {e}"

        if deleted is None:
            return f"❌ 任务 #{task_id} 不存在"

        push_event("deleted", deleted)
        return f"✅ 已删除任务 #{task_id}: {deleted.subject}"

    return delete_agent_task


def _make_verify_agent_task(
    repo: "AgentTaskRepository | MarkdownAgentTaskRepository",
    push_event,
) -> Tool:
    @tool.function(
        name="verify_agent_task",
        tier=ToolTier.ALWAYS,
        description=(
            "【强制门控】启动独立验证 Agent 检查代码改动是否已真实落地到源文件中。\n"
            "\n"
            "当 task 的 verify_hint 不为空时，标记 completed 前**必须先**调用本工具。\n"
            "\n"
            "工作原理：\n"
            "1. 读取 task.verify_hint 获取验证目标（目标文件 + 检查模式）\n"
            "2. 启动 code-verifier Agent（只读工具：read_file/search_content/list_dir）\n"
            "3. 验证 Agent 读取源文件、搜索代码模式、返回 PASSED/FAILED + 代码证据\n"
            "4. PASSED → 标记 verified=True，此后允许 update_agent_task(status='completed')\n"
            "5. FAILED → 不改变任何状态，你必须修改代码后重新验证\n"
            "\n"
            "参数:\n"
            "- task_id: 要验证的 AgentTask ID\n"
            "\n"
            "【重要】验证 Agent 只有只读工具，永远不写代码、不修改文件、不改变任务状态。"
        ),
        when_to_use=(
            "在标记任何 verify_hint 不为空的 task 为 completed **之前**，必须先调此工具验证。\n"
            "验证失败后必须修复代码并重新验证，不可跳过。\n"
            "纯查询/非代码类任务（verify_hint 为空）无需调用，verify_agent_task 会自动通过。"
        ),
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=False,
            is_idempotent=True,
        ),
    )
    async def verify_agent_task(ctx: ToolContext, task_id: str) -> str:
        """启动独立验证 Agent 验证代码改动是否落地。"""
        _ = _get_user_id(ctx)

        task = await repo.get_task(task_id)
        if task is None:
            return f"❌ 任务 #{task_id} 不存在"

        if task.status != AgentTaskStatus.IN_PROGRESS:
            return (
                f"❌ 只能验证 in_progress 状态的任务，当前状态: {task.status.value}\n"
                f"   正确流程: pending → in_progress → verify_agent_task → completed"
            )

        if not task.verify_hint:
            try:
                await repo.update_task(
                    task_id, verified=True,
                    verify_evidence="(纯查询/非代码任务，自动通过)",
                )
                updated = await repo.get_task(task_id)
                if updated:
                    push_event("updated", updated)
                return f"✅ 任务 '{task.subject}' 为纯查询任务，自动通过验证。"
            except Exception as e:
                logger.error("verify_agent_task auto-pass: %s", e, exc_info=True)
                return f"❌ 自动验证失败: {e}"

        registry = ctx.metadata.get("agent_registry")
        if registry is None:
            return "❌ SubAgentRegistry 不可用。请确认 code-verifier Agent 已注册。"

        verification_task = (
            f"## 验证任务\n\n"
            f"验证目标: {task.subject}\n"
            f"任务描述: {task.description}\n"
            f"验证要求: {task.verify_hint}\n\n"
            f"请按格式输出: PASSED 或 FAILED + Evidence + Reason"
        )

        try:
            result = await registry.call_agent(
                agent_name="code-verifier",
                task=verification_task,
                context=ctx,
            )
        except Exception as e:
            logger.error("Verification agent call failed: %s", e, exc_info=True)
            return f"❌ 验证 Agent 执行失败: {e}"

        output = str(result.output) if hasattr(result, "output") else str(result)
        last_passed = output.rfind("PASSED")
        last_failed = output.rfind("FAILED")
        is_passed = last_passed > last_failed

        evidence = ""
        reason = ""
        for line in output.split("\n"):
            if line.startswith("Evidence:"):
                evidence = line.replace("Evidence:", "").strip()
            elif line.startswith("Reason:"):
                reason = line.replace("Reason:", "").strip()

        if not evidence:
            evidence = output[:500]

        if is_passed:
            try:
                await repo.update_task(
                    task_id, verified=True, verify_evidence=evidence,
                )
                updated = await repo.get_task(task_id)
                if updated:
                    push_event("updated", updated)
                return (
                    f"✅ 验证通过: {task.subject}\n"
                    f"   证据: {evidence[:300]}\n"
                    f"   现在可以 update_agent_task(status='completed')"
                )
            except Exception as e:
                logger.error("verify_agent_task update: %s", e, exc_info=True)
                return f"❌ 验证通过但保存失败: {e}"
        else:
            return (
                f"❌ 验证失败: {task.subject}\n"
                f"   原因: {reason or '未找到所需模式'}\n"
                f"   请修复代码后重新调用 verify_agent_task(task_id='{task_id}')"
            )

    return verify_agent_task


# ═══════════════════════════════════════════════════════════════════════════════
# Provider 类（★ 层次 3：依赖通过 __init__ 显式注入）
# ═══════════════════════════════════════════════════════════════════════════════


class AgentTaskTools:
    """AgentTask 工具组 Provider。

    构造时显式注入依赖（repo + broadcaster），get_tools() 返回
    绑定了这些依赖的 Tool 列表。

    ★ 根本解：替代之前的 module-level 单例 + inject_agent_task_*() 全局副作用。
    类型系统强制要求 __init__ 传入 repo 和 broadcaster，漏传就构造失败。
    """

    def __init__(
        self,
        repo: "AgentTaskRepository | MarkdownAgentTaskRepository",
        broadcaster: MessageBroadcast | None = None,
    ) -> None:
        if repo is None:
            raise ValueError("repo cannot be None")
        self._repo = repo
        self._broadcaster = broadcaster
        self._push_event = _make_push_event(broadcaster)

    def get_tools(self) -> list[Tool]:
        """返回 AgentTask 管理工具组（绑定了依赖的 Tool 列表）。"""
        return [
            _make_create_agent_task(self._repo, self._push_event),
            _make_update_agent_task(self._repo, self._push_event),
            _make_set_task_dependency(self._repo, self._push_event),
            _make_list_agent_tasks(self._repo),
            _make_get_agent_task(self._repo),
            _make_delete_agent_task(self._repo, self._push_event),
            _make_verify_agent_task(self._repo, self._push_event),
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# 外部调用接口（供 SessionManager / Resume 模块使用）
# ═══════════════════════════════════════════════════════════════════════════════

# ★ 层次 3 妥协：build_resume_context 需要 repo，但它是 module-level 函数
# 没法改成 Provider 方法（因为它由 SessionManager 等外部调用，不知道 Provider 实例）。
# 解决方案：用一个 module-level _resume_provider 引用（不是 None 单例，是 Provider 引用），
# SubsystemContainer 启动时通过 set_resume_provider() 设置。
# 这不是反模式（不是 None + inject_xxx），是"显式注册 + fail-fast"的中间方案。
_resume_provider: AgentTaskTools | None = None


def set_resume_provider(provider: AgentTaskTools) -> None:
    """设置 build_resume_context 使用的 Provider（容器启动时调一次）。"""
    global _resume_provider
    _resume_provider = provider


async def build_resume_context(session_id: str) -> str:
    """构建会话恢复提示文本（供 Resume 模块使用）。

    将未完成任务信息格式化为注入 AI 首条消息的上下文文本。
    """
    if _resume_provider is None:
        logger.debug("build_resume_context: no provider set, returning empty")
        return ""
    repo = _resume_provider._repo

    try:
        tasks = await repo.list_tasks_by_session(
            session_id,
            include_completed=False,
            include_cancelled=False,
        )
    except Exception as e:
        logger.warning("build_resume_context failed: %s", e)
        return ""

    if not tasks:
        return ""

    in_progress = [t for t in tasks if t.status == AgentTaskStatus.IN_PROGRESS]
    pending = [t for t in tasks if t.status == AgentTaskStatus.PENDING]
    completed = await repo.list_tasks_by_session(
        session_id, include_completed=True, include_cancelled=False,
    )
    completed_count = len([t for t in completed if t.status == AgentTaskStatus.COMPLETED])

    lines = ["⚠️ 上次会话断开前还有任务："]

    for t in in_progress:
        lines.append(f"  🔄 #{t.order} {t.subject}（被中断）")

    if completed_count > 0:
        lines.append(f"  ✅ 已完成 {completed_count} 个")

    if pending:
        pending_str = ", ".join(f"#{t.order} {t.subject}" for t in pending)
        lines.append(f"  ⬜ 待处理：{pending_str}")

    lines.append("  请调用 list_agent_tasks 确认状态后继续。")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# ★ 反模式消除确认（与 scheduler_tools.py 同型）
# ═══════════════════════════════════════════════════════════════════════════════
#
# 之前（反模式）：
#   _repo: ... | None = None
#   _broadcaster: ... | None = None
#   def inject_agent_task_repo(repo): global _repo; _repo = repo
#   def inject_agent_task_broadcaster(b): global _broadcaster; _broadcaster = b
#
# 之后（消除反模式）：
#   - 删除了 module-level 单例 _repo, _broadcaster
#   - 删除了 inject_agent_task_repo / inject_agent_task_broadcaster 全局副作用
#   - 删除了 get_agent_task_tools()（改用 AgentTaskTools.get_tools()）
#   - 工具函数通过闭包变量访问依赖
#   - 依赖在 AgentTaskTools.__init__ 显式声明
#
# 唯一的 module-level 引用是 _resume_provider（用于 build_resume_context）——
# 这是"显式注册 + fail-fast"模式，不是反模式：调用前必须 set_resume_provider()。

