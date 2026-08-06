"""pandaren/behavior/hitl_controller.py — HITL 审批决策与状态管理

HC6：sensitivity 是 HITL 审批的唯一判断依据。
     CRITICAL → 强制 HITL，无论任何配置，不可绕过。

职责：
  1. check_approval()  — 判断工具调用是否需要人工审批
  2. resolve_resume()  — 处理 resume 路径的决策分发

注：RunState 快照的构建由 engine/run_core.py 负责（执行层职责）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from ..tool.types import SensitivityLevel

logger = logging.getLogger("pandaren.behavior.hitl_controller")

# ── 公共类型别名 ──────────────────────────────────────────────────────────────
ApprovalResult = Literal["approved", "rejected"]
CheckResult = Literal["pass", "need_approval"]
ResumeAction = Literal["execute_pending", "reject_and_halt"]


@dataclass(frozen=True)
class PendingApproval:
    """待审批的工具调用信息（从 HITL_REQUESTED 到 resume 之间传递的上下文）。

    frozen=True 保证一旦创建不可篡改。
    """
    tool_call: dict                # 原始 tool_call dict（含 id, function.name, function.arguments）
    tool_name: str                 # 提取出的工具名（便于日志/审计）
    tool_args: dict                # 解析后的工具参数
    sensitivity: int               # 触发 HITL 的敏感度等级
    step_n: int                    # 触发时的 step 编号
    # 批次中已通过预检的 tool_calls（HITL 触发前已 approved 的那些）
    approved_calls_before: tuple[dict, ...] = ()
    # 批次中尚未检查的 tool_calls（HITL 触发后排在后面的那些）
    unchecked_calls_after: tuple[dict, ...] = ()


@dataclass(frozen=True)
class PendingInteraction:
    """待用户交互的工具调用信息（INTERACTION_REQUESTED → resume 之间的上下文）。

    与 PendingApproval 的区别：
      - PendingApproval → 二选一审批（批准/拒绝），由 HITLController 决策
      - PendingInteraction → 自由文本回复，用户答案直接作为工具执行结果

    frozen=True 保证一旦创建不可篡改。
    """
    tool_call: dict    # 原始 tool_call dict（含 id, function.name, function.arguments）
    tool_name: str     # 提取出的工具名（便于日志/审计）
    tool_args: dict    # 解析后的工具参数
    step_n: int        # 触发时的 step 编号
    # 交互工具之前已通过预检的 tool_calls（需在 resume 时一起执行并写入结果）
    approved_calls_before: tuple[dict, ...] = ()


@dataclass(frozen=True)
class ResumeDecision:
    """resume 路径的决策结果。

    action:
      - "execute_pending": 批准 → 直接执行 pending_approval 中保存的工具调用
      - "reject_and_halt": 拒绝 → 终止 run
    """
    action: ResumeAction
    pending: PendingApproval | None = None


class HITLController:
    """HITL 审批决策器（创建后冻结）。

    HC6：CRITICAL → 强制 HITL，不可绕过。
    HC1/HC2：auto_confirm_high 创建后冻结。

    设计原则：
      - 纯决策器：只负责"是否需要审批"和"恢复时做什么"的判断
      - 无状态：不存储审批结果，审批结果通过 hitl_decision 参数显式传入
      - 不可篡改：创建后所有字段冻结
    """

    __slots__ = ("_auto_confirm_high",)

    def __init__(self, auto_confirm_high: bool = False) -> None:
        object.__setattr__(self, "_auto_confirm_high", auto_confirm_high)

    def __setattr__(self, name: str, value: object) -> None:
        raise PermissionError(
            f"HITLController 字段 '{name}' 不可直接修改。"
        )

    def __delattr__(self, name: str) -> None:
        raise PermissionError(
            f"HITLController 字段 '{name}' 不可删除。"
        )

    @property
    def auto_confirm_high(self) -> bool:
        return object.__getattribute__(self, "_auto_confirm_high")

    # ── 核心方法 1：审批决策 ──────────────────────────────────────────────────

    def check_approval(
        self,
        sensitivity_value: int,
        tool_name: str = "",
    ) -> CheckResult:
        """判断是否需要人工审批。

        CRITICAL (4) → 强制 "need_approval"（HC6，不可绕过）
        HIGH     (3) → 按 auto_confirm_high 决定
        MEDIUM/LOW   → "pass"
        """
        if sensitivity_value >= SensitivityLevel.CRITICAL:
            logger.info("hitl: need_approval（CRITICAL），tool='%s'", tool_name)
            return "need_approval"

        if sensitivity_value == SensitivityLevel.HIGH:
            if self.auto_confirm_high:
                logger.info("hitl: pass（HIGH + auto_confirm=True），tool='%s'", tool_name)
                return "pass"
            else:
                logger.info("hitl: need_approval（HIGH），tool='%s'", tool_name)
                return "need_approval"

        return "pass"

    # ── 核心方法 2：resume 决策 ───────────────────────────────────────────────

    def resolve_resume(
        self,
        hitl_decision: str,
        pending: PendingApproval,
    ) -> ResumeDecision:
        """根据审批结果决定 resume 后的动作。

        Args:
            hitl_decision: "approved" | "rejected"
            pending: 暂停时保存的待审批信息

        Returns:
            ResumeDecision，指示执行内核应该做什么
        """
        if hitl_decision == "approved":
            logger.info(
                "hitl: resume approved，tool='%s'，将直接执行 pending_tool_call",
                pending.tool_name,
            )
            return ResumeDecision(action="execute_pending", pending=pending)

        # rejected 或其他非法值一律视为拒绝
        logger.info(
            "hitl: resume rejected，tool='%s'，run 将终止",
            pending.tool_name,
        )
        return ResumeDecision(action="reject_and_halt", pending=pending)

    def __repr__(self) -> str:
        return f"HITLController(auto_confirm_high={self.auto_confirm_high})"
