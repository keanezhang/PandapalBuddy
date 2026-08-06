"""pandaren/plan/tools.py — Plan Mode 内置工具定义

enter_plan_mode: LLM 主动请求进入规划模式
write_plan:      将计划内容写入计划文件（cap-style）
exit_plan_mode:  提交计划，结束规划阶段

设计原则：
  B2  — 工具只读 ToolContext，不直接写 Memory / 发 StreamEvent / 调 PlanManager
  信号传递 — 工具返回 ToolResult(success=True) → run_core 统一处理副作用
  文件路径 — enter_plan_mode 生成路径 → LLM 原样传回给 write/exit
  文件 IO — 全部委托 files.py
"""

from __future__ import annotations

import logging
from typing import Any

from ..tool.definition.tool import Tool
from ..tool.definition.context import ToolContext
from ..tool.definition.tool_result import ToolResult
from ..tool.definition.tool_policy import ToolPolicy
from ..tool.types import ToolTier, SensitivityLevel

from .files import (
    generate_plan_file_path,
    validate_plan_file_path,
    write_plan_content,
    read_plan,
    plan_exists,
)

logger = logging.getLogger("pandaren.plan.tools")


# ── 工具名称常量 ──
ENTER_PLAN_MODE_NAME = "enter_plan_mode"
WRITE_PLAN_NAME = "write_plan"
EXIT_PLAN_MODE_NAME = "exit_plan_mode"
PLAN_MODE_BUILTIN_TOOLS = frozenset({
    ENTER_PLAN_MODE_NAME, WRITE_PLAN_NAME, EXIT_PLAN_MODE_NAME,
})
PLAN_MODE_ALLOWED_BUILTIN = frozenset({
    ENTER_PLAN_MODE_NAME,
    WRITE_PLAN_NAME,
    EXIT_PLAN_MODE_NAME,
    "ask_user",
})


# ═══════════════════════════════════════════════════
# enter_plan_mode executor
# ═══════════════════════════════════════════════════

async def _enter_plan_mode_executor(
    ctx: ToolContext,
    plan_name: str = "",
    plan_file_path: str = "",
    plan_dir: str | None = None,
    **kwargs: Any,
) -> ToolResult:
    """进入规划模式。引导 LLM 遵循规划工作流。

    路径优先级: plan_file_path > plan_name > 自动生成
    """
    # ── 1. 确定最终路径 ──
    if plan_file_path:
        # 用户在对话中指定了完整路径 → LLM 提取后填入
        final_path = validate_plan_file_path(plan_file_path)
        if final_path is None:
            return ToolResult(
                success=False,
                error=f"指定的计划文件路径不合法: {plan_file_path}",
            )
        logger.info("[enter-plan-mode] using user-specified path: %s", final_path)

    elif plan_name:
        # LLM 指定了 plan 名称，如果用户指定了 plan_dir，则使用用户指定的 plan_dir，否则用默认的dir
        final_path = generate_plan_file_path(plan_name, plan_dir=plan_dir)
        logger.info("[enter-plan-mode] generated path from name: %s", final_path)

    else:
        # 自动生成兜底名称
        sid = ctx.session_id[:12].replace("/", "_").replace("\\", "_")
        rid = ctx.run_id[:8].replace("/", "_").replace("\\", "_")
        auto_name = f"{sid}-{rid}"
        final_path = generate_plan_file_path(auto_name, plan_dir=plan_dir)
        logger.info("[enter-plan-mode] auto-generated path: %s", final_path)

    # ── 2. 构造引导文本 ──
    # 注: guidance 仅提供通用信息和文件路径，不包含 Phase 特定描述。
    #     详细的 Phase 方法论由 FULL_PLANNING_REMINDER 在下一轮作为 dynamic_reminder 注入。
    guidance = (
        "✅ 已进入规划模式。\n\n"
        "你现在处于只读规划阶段，计划文件是唯一可以写入的文件。\n"
        "详细的规划方法论将在下一轮提醒中提供。\n\n"
        f"计划文件路径: {final_path}\n\n"
        "---\n\n"
        "## 关键工具\n\n"
        "- `write_plan` — 将最终计划写入计划文件（唯一可写工具）\n"
        "- `exit_plan_mode` — 提交计划等待用户审批\n"
        "- `ask_user` — 需要澄清时向用户提问\n\n"
        "---\n\n"
        f"⚠️ 重要: 调用 write_plan 和 exit_plan_mode 时，"
        f"请将 plan_file_path 参数设为 \"{final_path}\"。"
    )

    return ToolResult(
        success=True,
        data=guidance,
        plan_path=final_path,
    )


# ═══════════════════════════════════════════════════
# write_plan executor
# ═══════════════════════════════════════════════════

async def _write_plan_executor(
    ctx: ToolContext,
    content: str = "",
    plan_file_path: str = "",
    **kwargs: Any,
) -> ToolResult:
    """将计划内容写入计划文件（全量覆盖）。

    B2: 只做纯校验 + 文件 IO，不碰 PlanManager 状态。
    """
    # ── 1. 输入校验 ──
    if not plan_file_path:
        return ToolResult(
            success=False,
            error=(
                "plan_file_path 未设置。"
                "请传入 enter_plan_mode 返回的计划文件路径作为 plan_file_path 参数。"
            ),
        )
    if not content or not content.strip():
        return ToolResult(
            success=False,
            error="content 不能为空，请提供计划内容。",
        )

    # ── 2. 写入磁盘（委托 files.py）──
    try:
        write_plan_content(plan_file_path, content)
    except OSError as e:
        return ToolResult(success=False, error=f"写入计划文件失败: {e}")

    lines = content.split("\n")
    exceeds_limit = len(lines) > 3000

    logger.debug(
        "[write_plan] wrote %d chars (%d lines) to %s",
        len(content), len(lines), plan_file_path,
    )

    # ── 3. 返回纯信号 ──
    return ToolResult(
        success=True,
        plan_path=plan_file_path,
        data={
            "line_count": len(lines),
            "exceeds_limit": exceeds_limit,
            "message": (
                "⚠️ 超过 3000 行硬限制。请检查是否混入了冗余散文，精简后重新写入。"
                if exceeds_limit
                else (
                    f"✅ 计划已写入 ({len(lines)} 行)。\n\n"
                    "⚠️ 必须立即调用 exit_plan_mode(plan_file_path="
                    f'"{plan_file_path}") 提交审批！\n'
                    "在用户批准之前，严格禁止执行任何其他操作（包括创建任务、写文件等）。"
                )
            ),
        },
    )


# ═══════════════════════════════════════════════════
# exit_plan_mode executor
# ═══════════════════════════════════════════════════

async def _exit_plan_mode_executor(
    ctx: ToolContext,
    plan_file_path: str = "",
    **kwargs: Any,
) -> ToolResult:
    """提交计划，结束规划阶段。

    B2: 只做纯校验 + 读文件 + 返回信号。
        副作用（写 session_meta / emit 事件 / 终止 run）由 run_core 统一处理。
    """
    # ── 1. 输入校验 ──
    if not plan_file_path:
        return ToolResult(
            success=False,
            error=(
                "计划文件路径未设置。"
                "请传入 enter_plan_mode 返回的计划文件路径作为 plan_file_path 参数。"
            ),
        )

    # ── 2. 前置检查 + 读取（委托 files.py）──
    if not plan_exists(plan_file_path):
        return ToolResult(
            success=False,
            error=f"计划文件不存在: {plan_file_path}。请先用 write_plan 写入。",
        )

    plan_content = read_plan(plan_file_path)
    if not plan_content or not plan_content.strip():
        return ToolResult(success=False, error="计划文件为空。请先写入有效的计划内容。")

    # ── 3. 返回纯信号（不做任何副作用）──
    return ToolResult(
        success=True,
        plan_path=plan_file_path,
        data={
            "plan_path": plan_file_path,
            "plan_content": plan_content,
            "message": (
                "计划已提交，等待用户批准。\n\n"
                "接下来可能发生：\n"
                "1. 用户批准 → 你将退出规划模式，开始实施计划\n"
                "2. 用户需完善 → 你将收到具体修改指令，重新进入规划模式修改\n"
                "3. 用户放弃 → Plan Mode 结束，清理状态\n\n"
                "请等待用户决策。"
            ),
        },
    )


# ═══════════════════════════════════════════════════
# 工具构建函数
# ═══════════════════════════════════════════════════

def build_plan_mode_tools(
    *,
    plan_dir: str | None = None,
) -> list[Tool]:
    """构建 Plan Mode 的三个内置工具。

    Args:
        plan_dir: 自定义计划文件存放目录（绝对路径）。
                  不传则默认为 {cwd}/.pandaren/plans/。

    Returns:
        [enter_plan_mode, write_plan, exit_plan_mode] 工具列表
    """
    from .prompt import ENTER_PLAN_MODE_DESCRIPTION, PLAN_TEMPLATE

    # ── 闭包捕获 plan_dir，注入到 executor ──
    _plan_cfg_dir = plan_dir

    async def _enter_exec(  # pragma: no cover
        ctx: ToolContext,
        plan_name: str = "",
        plan_file_path: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        return await _enter_plan_mode_executor(
            ctx,
            plan_name=plan_name,
            plan_file_path=plan_file_path,
            plan_dir=_plan_cfg_dir,
            **kwargs,
        )

    enter_tool = Tool(
        name=ENTER_PLAN_MODE_NAME,
        description=ENTER_PLAN_MODE_DESCRIPTION,
        executor=_enter_exec,
        input_schema={
            "type": "object",
            "properties": {
                "plan_name": {
                    "type": "string",
                    "description": "可选，计划文件名称（如 osaka-travel），不传则自动生成",
                },
                "plan_file_path": {
                    "type": "string",
                    "description": (
                        "可选，用户在对话中指定的计划文件完整路径（绝对路径，.md 后缀）。"
                        "传入后忽略 plan_name。LLM 应先检查用户对话中是否提供了路径。"
                    ),
                },
            },
            "required": [],
        },
        tier=ToolTier.ALWAYS,
        when_to_use="当需要制定复杂实现计划时，先进入规划模式进行只读探索和方案设计",
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=False,
            is_idempotent=True,
            read_only=False,
        ),
    )

    write_tool = Tool(
        name=WRITE_PLAN_NAME,
        description=(
            f"将计划内容写入计划文件（全量覆盖）。只能在规划阶段使用。\n\n"
            f"## 计划文件格式要求\n{PLAN_TEMPLATE}"
        ),
        executor=_write_plan_executor,
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "计划的完整 Markdown 内容（全量覆盖写入计划文件）",
                },
                "plan_file_path": {
                    "type": "string",
                    "description": "enter_plan_mode 返回的计划文件路径（必填）",
                },
            },
            "required": ["content", "plan_file_path"],
        },
        tier=ToolTier.ALWAYS,
        when_to_use="规划阶段编写完计划后，用此工具将计划写入计划文件",
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=False,
            is_idempotent=True,
            read_only=False,
        ),
    )

    exit_tool = Tool(
        name=EXIT_PLAN_MODE_NAME,
        description=(
            "结束规划阶段，将计划提交给用户审批。\n\n"
            "## 调用时机\n"
            "当你满足以下条件时主动调用:\n"
            "- 已用 write_plan 写完计划，内容清晰可执行\n"
            "- 所有关键问题已通过与用户澄清得到解决\n"
            "- 没有更多需要探索或确认的内容\n\n"
            "## 重要规则\n"
            "- **不要等待用户指令**，也不要问'是否要提交'——规划完成就提交\n"
            "- **不要通过文字或 ask_user 询问计划是否 OK**——必须用本工具请求审批\n"
            "- plan_file_path 必须传入 enter_plan_mode 返回的路径\n"
            "- 若计划尚未写入文件，请先调用 write_plan"
        ),
        executor=_exit_plan_mode_executor,
        input_schema={
            "type": "object",
            "properties": {
                "plan_file_path": {
                    "type": "string",
                    "description": "enter_plan_mode 返回的计划文件路径（必填）",
                },
            },
            "required": ["plan_file_path"],
        },
        tier=ToolTier.ALWAYS,
        when_to_use="规划完成后，调用此工具提交计划等待用户批准",
        policy=ToolPolicy(
            sensitivity=SensitivityLevel.LOW,
            is_reversible=True,
            audit_required=True,
            is_idempotent=True,
            read_only=False,
        ),
    )

    return [enter_tool, write_tool, exit_tool]
