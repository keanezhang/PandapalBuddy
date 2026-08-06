"""scheduler_tools — TaskScheduler 定时任务管理工具组（★ 层次 3 改造 2026-06-11）。

Agent-Native 设计：用户通过自然语言对话让 LLM 创建/管理定时任务。
LLM 调用这些工具，工具内部操作 TaskScheduler。

时区约定（重要）：
    所有 cron 表达式以"本地时间"解释（与 croniter 默认行为一致）。
    `after_minutes` 由服务端用 datetime.now()（本地 naive）计算，
    避免 LLM 时间幻觉与 UTC/本地混用导致的调度漂移。

错误传递：
    工具直接 await TaskScheduler.register_task_definition；
    cron 语法非法等错误会以 ❌ 文本回到 LLM，不再悄悄静默。
    cron 语法校验由 TaskScheduler 内部完成（避免双重校验）。

★ 层次 3 根本解改造：
    之前：module-level 单例 `_task_scheduler: Any = None` + `inject_task_scheduler()` 全局副作用。
    之后：依赖通过 SchedulerTools 构造函数显式注入，工具函数通过闭包变量访问。
    没有全局单例，没有"记着调 inject"——构造 SchedulerTools 时依赖就到位。
    启动期 fail-fast：未构造 SchedulerTools 就调工具，工具不在 agent_builder.tools() 中。

Cron 表达式速查（5 字段: 分 时 日 月 星期）：
    "0 5 * * *"        每天 5:00
    "*/30 * * * *"     每 30 分钟
    "0 8 * * 1-5"      工作日早上 8:00
    "0 9 1 * *"        每月 1 号 9:00
    "0 0 * * 0"        每周日 0:00
    "23 14 5 6 *"      6 月 5 日 14:23（一次性）
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from pandaren.tool.decorator import tool
from pandaren.tool.definition.context import ToolContext
from pandaren.tool.definition.tool import Tool
from pandaren.tool.definition.tool_policy import ToolPolicy
from pandaren.tool.types import SensitivityLevel, ToolTier

from pandapal.task_scheduler.task_scheduler import TaskScheduler
from pandapal.broadcast.broadcaster import MessageBroadcast
from pandapal.storage.models import TaskDefinition

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers（纯函数，不依赖任何子系统）
# ═══════════════════════════════════════════════════════════════════════════════


def _local_now() -> datetime:
    """本地 naive 时间。

    与 croniter 默认行为一致 —— croniter 不传 start_time 时也走本地 naive。
    若两端时区语义不一致（曾经的 UTC bug），会导致下次触发漂到一年后。
    """
    return datetime.now()


def _build_oneshot_cron(dt: datetime) -> str:
    """一次性触发 cron：'分 时 日 月 *'（本地时间字段）。"""
    return f"{dt.minute} {dt.hour} {dt.day} {dt.month} *"


def _resolve_cron(cron_expression: str, after_minutes: int) -> str:
    """根据输入决定最终 cron 字符串（不做语法校验，由 TaskScheduler 兜底）。

    分两条路径，互不混淆：
      1. after_minutes > 0 —— 服务端权威路径
         由真实时钟计算 now+N，覆盖 LLM 输入。用于"N 分钟/小时后"。
      2. after_minutes == 0 —— LLM 权威路径
         直接采用 LLM 给的 cron（绝对时间或周期任务）。

    Raises:
        ValueError: 路径 B 下 cron_expression 为空。
    """
    if after_minutes > 0:
        future = _local_now() + timedelta(minutes=after_minutes)
        cron = _build_oneshot_cron(future)
        if cron_expression and cron_expression != cron:
            logger.info(
                "Cron overridden by after_minutes=%d: LLM=%r → server=%r",
                after_minutes, cron_expression, cron,
            )
        return cron

    cron = (cron_expression or "").strip()
    if not cron:
        raise ValueError("cron_expression 不能为空（除非提供 after_minutes>0）")
    return cron


def _user_id_from_ctx(ctx: ToolContext) -> str:
    """从 ToolContext 的 metadata 中提取 user_id。

    user_id 是严肃字段，必须由上游（Scheduler）通过 metadata 注入，
    不允许使用占位值。若无 metadata 或无 user_id，返回空字符串，
    下游创建任务时会校验并拒绝。
    """
    if not ctx.metadata:
        return ""
    return ctx.metadata.get("user_id", "")


# ═══════════════════════════════════════════════════════════════════════════════
# 工具工厂（★ 层次 3 核心：依赖通过闭包绑定，避免 module-level 单例）
# ═══════════════════════════════════════════════════════════════════════════════


def _make_create_scheduled_task(
    task_scheduler: TaskScheduler,
    broadcast: MessageBroadcast,
) -> Tool:
    """创建 create_scheduled_task 工具（闭包绑定 task_scheduler + broadcast）。

    闭包变量不进入工具 signature（LLM 看到的 schema 不含它们）；
    ctx: ToolContext 作为第一参数，schema 推断自动跳过。
    """
    @tool.function(
        tier=ToolTier.ALWAYS,
        name="create_scheduled_task",
        description=(
            "创建定时任务，在指定时间自动执行 AI 指令并推送结果。"
            "schedule_type 必须为 \"oneshot\"（一次性触发）或 \"recurring\"（周期性触发）。"
            "时间指定方式：\"多久后\"用 after_minutes，\"几点几分\"用 cron_expression。"
            "cron_expression 示例: \"0 7 * * *\"（每天7:00）、\"0 8 * * 1\"（每周一8:00）。"
        ),
        when_to_use=(
            "当用户要求设置提醒、闹钟、定时任务、每日/每周/每月通知时调用。"
            "例如：「1分钟后提醒我开会」「每天早上8点提醒我背单词」「每30分钟检查一下天气」"
            "「明天下午3点叫我开会」「每周一早上总结上周工作」「每月1号提醒我还信用卡」"
        ),
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.MEDIUM,
            is_reversible=True,
            audit_required=True,
            is_idempotent=False,
        ),
        progress_label='创建定时任务「{name}」',
    )
    async def create_scheduled_task(
        ctx: ToolContext,
        task_id: str,
        name: str,
        task_prompt: str,
        cron_expression: str = "",
        after_minutes: int = 0,
        schedule_type: str = "recurring",
    ) -> str:
        """创建定时任务（使用规则见 description）。

        Args:
            ctx: 工具上下文（自动注入）
            task_id: 任务唯一 ID
            name: 任务显示名称
            cron_expression: cron 表达式
            task_prompt: 触发时 AI 执行的完整指令
            after_minutes: "多久后"方式
            schedule_type: 调度类型，必须是 "oneshot"（一次性）或 "recurring"（周期性）
        """
        try:
            cron = _resolve_cron(cron_expression, after_minutes)
        except ValueError as e:
            logger.warning("create_scheduled_task: invalid input task_id=%s: %s", task_id, e)
            return f"❌ {e}"

        if schedule_type not in ("oneshot", "recurring"):
            return f"❌ schedule_type 必须是 'oneshot' 或 'recurring'，收到：{schedule_type!r}"

        user_id = _user_id_from_ctx(ctx)
        if not user_id:
            return "❌ 任务创建失败：未能获取用户身份（user_id 为空），请重试"
        session_id = ctx.session_id or ""
        if not session_id:
            return "❌ 任务创建失败：未能获取 session_id，请重试"

        trigger_type = "oneshot" if schedule_type == "oneshot" else "recurring"

        definition = TaskDefinition(
            task_id=task_id,
            user_id=user_id,
            name=name,
            trigger_rule_json=json.dumps(
                {"trigger_type": trigger_type, "cron_expression": cron},
                ensure_ascii=False,
            ),
            task_prompt=task_prompt,
            session_id=session_id,
        )

        try:
            await task_scheduler.register_task_definition(definition)
        except ValueError as e:
            logger.warning("create_scheduled_task: registration rejected task_id=%s: %s", task_id, e)
            return f"❌ 创建失败：{e}"
        except Exception as e:
            logger.error("create_scheduled_task: unexpected error task_id=%s: %s", task_id, e)
            return f"❌ 创建失败（内部错误）：{e}"

        type_label = "一次性" if schedule_type == "oneshot" else "周期性"
        type_note = "（触发后自动注销）" if schedule_type == "oneshot" else ""

        return (
            f"✅ 已创建{type_label}定时任务「{name}」{type_note}\n"
            f"   🆔 ID: {task_id}\n"
            f"   ⏰ 规则: {cron}\n"
            f"   📌 类型: {type_label}\n"
            f"   📋 任务: {task_prompt}"
        )

    return create_scheduled_task


def _make_delete_scheduled_task(
    task_scheduler: TaskScheduler,
) -> Tool:
    """创建 delete_scheduled_task 工具（闭包绑定 task_scheduler）。"""
    @tool.function(
        tier=ToolTier.ALWAYS,
        name="delete_scheduled_task",
        description="删除一个已创建的定时任务",
        when_to_use="当用户要求取消、删除或停止某个定时任务/提醒/闹钟时调用",
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.HIGH,
            is_reversible=False,
            audit_required=True,
            is_idempotent=True,
        ),
    )
    async def delete_scheduled_task(ctx: ToolContext, task_id: str) -> str:
        """删除定时任务。

        Args:
            ctx: 工具上下文（自动注入）
            task_id: 要删除的任务 ID
        """
        try:
            await task_scheduler.unregister_task_definition(task_id)
        except Exception as e:
            logger.error("delete_scheduled_task: unexpected error task_id=%s: %s", task_id, e)
            return f"❌ 删除失败：{e}"

        return f"✅ 已删除定时任务（ID: {task_id}）"

    return delete_scheduled_task


# ═══════════════════════════════════════════════════════════════════════════════
# Provider 类（★ 层次 3：依赖通过 __init__ 显式注入）
# ═══════════════════════════════════════════════════════════════════════════════


class SchedulerTools:
    """TaskScheduler 工具组 Provider。

    构造时显式注入依赖（task_scheduler + broadcast），get_tools() 返回
    绑定了这些依赖的 Tool 列表。SubsystemContainer 通过 inject_into
    把 TaskScheduler 实例注入到这里（自动）。

    ★ 根本解：替代之前的 module-level 单例 + inject_task_scheduler() 全局副作用。
    类型系统强制要求 __init__ 传入 TaskScheduler，漏传就构造失败。
    """

    def __init__(
        self,
        task_scheduler: TaskScheduler,
        broadcast: MessageBroadcast,
    ) -> None:
        if task_scheduler is None:
            raise ValueError("task_scheduler cannot be None")
        if broadcast is None:
            raise ValueError("broadcast cannot be None")
        self._task_scheduler = task_scheduler
        self._broadcast = broadcast

    def get_tools(self) -> list[Tool]:
        """返回任务管理工具组（绑定了依赖的 Tool 列表）。"""
        return [
            _make_create_scheduled_task(self._task_scheduler, self._broadcast),
            _make_delete_scheduled_task(self._task_scheduler),
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# ★ 反模式消除确认
# ═══════════════════════════════════════════════════════════════════════════════
#
# 之前（反模式）：
#   _task_scheduler: Any = None
#   def inject_task_scheduler(ts): global _task_scheduler; _task_scheduler = ts
#   async def create_scheduled_task(ctx, ...):
#       if _task_scheduler is None: return "❌ 未初始化"
#       await _task_scheduler.register_task_definition(...)
#
#   def get_task_tools() -> list[Tool]:
#       return [create_scheduled_task, delete_scheduled_task]
#
# 之后（消除反模式）：
#   - 删除了 module-level 单例 _task_scheduler
#   - 删除了 inject_task_scheduler() 全局副作用函数
#   - 删除了 get_task_tools()（改用 SchedulerTools.get_tools()）
#   - 工具函数通过闭包变量访问依赖（compile-time 绑定，不可运行时换）
#   - 依赖在 SchedulerTools.__init__ 显式声明（类型系统强制）
#
# 收益：
#   - 不存在"忘调 inject_task_scheduler"的反模式（根本无此函数可调）
#   - 不存在"工具运行时发现依赖空"的反模式（依赖在构造时就绑定）
#   - 单元测试容易（构造 SchedulerTools(mock_ts, mock_b) 即可）
#   - SubsystemContainer 启动期校验：若没注册 SchedulerTools spec，工具根本不在 agent 里
#
# get_task_tools() 已被删除：
#   改由 run_local.py 显式 import SchedulerTools + 构造 + get_tools() + 注册到 agent_builder。
#   tools/__init__.py 的 get_all_tools() 自动跳过此模块（因为无 get_*_tools() 函数）。
