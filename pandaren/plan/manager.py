"""pandaren/plan/manager.py — PlanManager 类

Plan Mode 的单一管理入口，封装所有 planning 相关状态和行为。

设计原则：
  - run_core 只通过 PlanManager 的公开接口交互，不再直接操作内部变量
  - 文件相关状态：磁盘 plan_exists() + session_meta["plan_submitted_at"] 为真相
  - 不追踪 _plan_written / _plan_submitted（冗余状态）
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from .files import read_plan

logger = logging.getLogger("pandaren.plan.manager")


class PlanManager:
    """Plan Mode 的单一管理入口。

    run_core 只通过 PlanManager 的公开接口交互，不再直接操作
    _plan_phase / _plan_mode_turns 等内部变量。

    文件相关状态的设计原则：
      - 不追踪 _plan_written / _plan_submitted（冗余）
      - 磁盘 plan_exists() + session_meta["plan_submitted_at"] 就是真相来源
    """

    # ── 常量 ──
    TURNS_BETWEEN_REMINDERS: int = 5  # 每 5 轮注入一次 Sparse Reminder

    # ── 状态字段 ──
    _plan_file_path: str | None = None
    _plan_phase: str = "executing"  # "planning" | "executing"
    _plan_mode_turns: int = 0  # Plan Mode 总轮次计数
    _plan_mode_turns_since_reminder: int = 0  # 距上次 reminder 注入的轮次
    _plan_mode_is_reentry: bool = False  # 是否 re-entry 状态
    _methodology: str | None = None  # 用户自定义规划方法论
    _plan_context_reminder: str | None = None  # 用户批准后注入的实施指引

    # ═══════════════════════════════════════════════════
    # 生命周期方法
    # ═══════════════════════════════════════════════════

    def enter(
        self,
        file_path: str,
        *,
        methodology: str | None = None,
    ) -> None:
        """进入 Plan Mode，重置所有状态。

        调用时机: enter_plan_mode 成功后由 run_core 调用。
        """
        self._plan_file_path = file_path
        self._plan_phase = "planning"
        self._plan_mode_turns = 0
        self._plan_mode_turns_since_reminder = 0
        self._plan_mode_is_reentry = False
        self._methodology = methodology
        self._plan_context_reminder = None
        logger.info(
            "[plan-manager] entered: path=%s", file_path,
        )

    def exit(self, approved: bool = True) -> None:
        """退出 Plan Mode。

        - approved=True:  用户批准计划 → 进入 execution，保留 plan-context
        - approved=False: 用户放弃计划 → 进入 executing，清空 plan-context

        调用时机: 仅在用户决策后由 _handle_plan_action 触发，
                  不在 exit_plan_mode 的 handle_tool_result 中调用。
        """
        self._plan_phase = "executing"
        if not approved:
            self._plan_context_reminder = None
        logger.info("[plan-manager] exit: approved=%s", approved)

    def reenter(self) -> None:
        """Re-entry: 用户要求完善计划后重新进入规划。

        设置 is_reentry=True，下一轮 get_reminder() 会注入 REFINE_REMINDER。
        """
        self._plan_phase = "planning"
        self._plan_mode_is_reentry = True
        self._plan_mode_turns_since_reminder = 0
        logger.info("[plan-manager] re-entry (turns=%d)", self._plan_mode_turns)

    # ═══════════════════════════════════════════════════
    # 状态查询方法
    # ═══════════════════════════════════════════════════

    def is_planning(self) -> bool:
        """当前是否在规划阶段。"""
        return self._plan_phase == "planning"

    def is_executing(self) -> bool:
        """当前是否在执行阶段。"""
        return self._plan_phase == "executing"

    @property
    def phase(self) -> str:
        return self._plan_phase

    @property
    def turns(self) -> int:
        return self._plan_mode_turns

    def get_plan_file_path(self) -> str | None:
        """获取计划文件路径。由 enter() 设置。"""
        return self._plan_file_path

    @property
    def is_reentry(self) -> bool:
        return self._plan_mode_is_reentry

    @property
    def context_reminder(self) -> str | None:
        """获取批准后的 plan-context reminder（供 run_core 注入）。"""
        return self._plan_context_reminder

    def set_context_reminder(self, reminder: str) -> None:
        """设置批准后的 plan-context reminder。"""
        self._plan_context_reminder = reminder

    # ═══════════════════════════════════════════════════
    # 轮次管理
    # ═══════════════════════════════════════════════════

    def increment_turn(self) -> None:
        """每轮末尾调用，推进轮次计数器。"""
        self._plan_mode_turns += 1
        self._plan_mode_turns_since_reminder += 1

    # ═══════════════════════════════════════════════════
    # Tool Result 处理
    # ═══════════════════════════════════════════════════

    def handle_tool_result(
        self,
        tool_name: str,
        result: Any,  # ToolResult
    ) -> tuple[bool, str]:
        """检测 Plan Mode 相关的 tool_result，触发 PlanManager 自身状态变更。

        调用时机: run_core._handle_tool_result() 中，作为第一条分支检查。
        返回: (consumed, message)

        注: enter_plan_mode 的进入不经过本方法——
            run_core 直接调用 plan_manager.enter(file_path=...)。
        """
        # ── 出口: exit_plan_mode 执行完毕 → 申请用户审批 ──
        if tool_name == "exit_plan_mode" and getattr(result, "success", False):
            message = _build_exit_plan_mode_message(
                plan_path=result.data.get("plan_path", "") if isinstance(result.data, dict) else "",
                plan_content=result.data.get("plan_content", "") if isinstance(result.data, dict) else "",
            )
            logger.info("[plan-manager] plan submitted for approval")
            return True, message

        return False, ""

    # ═══════════════════════════════════════════════════
    # 跨 run 恢复（session 级 plan）
    # ═══════════════════════════════════════════════════

    @classmethod
    def restore_from_session_meta(cls, meta: dict) -> "PlanManager":
        """从 session_meta 恢复 PlanManager 状态。

        新 run 启动时调用。
        只恢复路径和阶段，不追踪 _plan_written / _plan_submitted（冗余）。
        """
        pm = cls()
        pm._plan_file_path = meta.get("plan_file_path")
        pm._plan_phase = meta.get("plan_phase", "executing")
        pm._plan_mode_turns = 0  # 新 run 重置轮次
        return pm

    # ═══════════════════════════════════════════════════
    # Reminder 注入
    # ═══════════════════════════════════════════════════

    def get_reminder(self) -> str:
        """获取当前轮次应注入的 Plan Mode Reminder。

        调用时机: run_core 每次构建 LLM 请求前。

        优先级: re-entry > methodology > FULL (turn 0) > SPARSE (每5轮)
        """
        if self._plan_phase != "planning":
            return ""

        from .prompt import (
            FULL_PLANNING_REMINDER,
            SPARSE_PLANNING_REMINDER,
            PLAN_MODE_REFINE_REMINDER,
        )

        # A. Re-entry（优先级最高）
        if self._plan_mode_is_reentry:
            self._plan_mode_is_reentry = False
            self._plan_mode_turns_since_reminder = 0
            logger.info("[plan-reminder] re-entry block injected")
            return PLAN_MODE_REFINE_REMINDER

        # B. 首轮 (turn 0)
        if self._plan_mode_turns == 0:
            self._plan_mode_turns_since_reminder = 0
            if self._methodology is not None:
                return self._methodology
            return FULL_PLANNING_REMINDER

        # C. 每 5 轮 Sparse
        if self._plan_mode_turns_since_reminder >= self.TURNS_BETWEEN_REMINDERS:
            self._plan_mode_turns_since_reminder = 0
            return SPARSE_PLANNING_REMINDER

        # D. 不注入
        return ""

    # ═══════════════════════════════════════════════════
    # 工具过滤
    # ═══════════════════════════════════════════════════

    def filter_tools(self, all_tools: list[Any]) -> list[Any]:
        """Plan Mode 下只暴露只读工具 + Plan Mode 专用工具。

        调用时机: run_core 每次构建 LLM 请求前。
        """
        from .tools import PLAN_MODE_ALLOWED_BUILTIN

        filtered: dict[str, Any] = {}
        for tool in all_tools:
            name = getattr(tool, "name", "")
            if name in filtered:
                continue
            # 规则1: Plan Mode 内置工具 → 保留
            if name in PLAN_MODE_ALLOWED_BUILTIN:
                filtered[name] = tool
                continue
            # 规则2: 纯只读工具 → 保留
            policy = getattr(tool, "policy", None)
            if policy and getattr(policy, "read_only", False):
                filtered[name] = tool
                continue
            # 规则3: 其余工具（write/edit/delete/bash写）→ 过滤掉
            logger.debug("[plan-filter] tool filtered out: %s", name)

        logger.info(
            "[plan-filter] tools filtered: %d → %d available",
            len(all_tools), len(filtered),
        )
        return list(filtered.values())

    # ═══════════════════════════════════════════════════
    # 辅助函数
    # ═══════════════════════════════════════════════════

    @staticmethod
    def compute_plan_hash(content: str) -> str:
        """计算计划内容的 SHA-256 hash。"""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def read_plan_content(self) -> str | None:
        """读取计划文件内容（委托 files.py）。"""
        if not self._plan_file_path:
            return None
        return read_plan(self._plan_file_path)


def _build_exit_plan_mode_message(plan_path: str, plan_content: str) -> str:
    """构建计划提交后的对话历史消息（回写给 LLM 看）。

    调用时机: exit_plan_mode 成功后由 handle_tool_result 调用。
    """
    return (
        f"计划已提交，等待用户批准。\n\n"
        f"计划文件: {plan_path}\n\n"
        f"请等待用户决策（批准 / 完善 / 放弃）。"
    )
