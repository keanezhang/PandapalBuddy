"""pandaren/behavior/context_window_budget.py — 上下文窗口 Token 预算分配

作为 context window token 的单一真相源，为所有消费方提供明确的 slot 配额，
替代各模块各自为政、互不通信的碎片化 token 预算状态。

核心设计原则：
  S1 · 不可变性：创建后所有字段只读，运行时不可修改
  E4/E5 · 失败安全默认值：参数未传入时使用保守默认值 + WARNING，不拒绝启动
  O3 · 错误必须显式处理：ratio_sum > 1.0、参数 ≤ 0 立即抛 BehaviorConfigError

边界：
  管 → context_window 持有、ratio 校验、绝对配额计算、只读查询
  不管 → model_id 映射、实际 token 计数、超限触发行为、USD 花费、步数超时
        max_output_tokens（属于 LLM 调用参数，由 llm_settings 管理）
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import ClassVar

from .exceptions import BehaviorConfigError
from ..constants import DEFAULT_CONTEXT_WINDOW, DEFAULT_TOOL_SCHEMA_RATIO, DEFAULT_CONVERSATION_RATIO

logger = logging.getLogger("pandaren.behavior.context_window_budget")

# ── 默认值常量 ─────────────────────────────────────────────────────────────────
# 各 input slot 的默认配额比例
DEFAULT_SYSTEM_PROMPT_RATIO: float = 0.15

# recall slot 配额比例。
# v1.4 重构后「跨 session 召回」已整体废弃，项目内**无任何消费方**，
# 原默认 0.10 在 CW=100,000 时白占 10,000 token（实测 100% 空置）。
# 故默认置 0 回收配额；见 COMPACT_BUDGET_AUDIT.md G4。
DEFAULT_RECALL_RATIO: float = 0.00

# 有效 slot 名称集合
_VALID_SLOT_NAMES: frozenset[str] = frozenset({
    "system_prompt", "tool_schema", "conversation", "recall",
})


@dataclass(frozen=True)
class SlotSnapshot:
    """全量 slot 配额快照（不可变）。

    供 MessageBuilder 一次性读取所有 slot 的绝对 token 配额。
    """

    system_prompt_tokens: int
    tool_schema_tokens: int
    conversation_tokens: int
    recall_tokens: int


class ContextWindowBudget:
    """上下文窗口 Token 预算分配对象。创建后完全不可变（S1）。

    作为 context window token 的单一真相源，为所有消费方（ToolBudget、Memory、
    MessageBuilder、AgentLoop）提供明确的 slot 配额。

    注意：max_output_tokens（LLM 最大输出 token 数）不属于本模块职责，
    应通过 AgentBuilder.llm_settings(max_tokens=...) 配置。

    用法::

        budget = ContextWindowBudget(
            context_window=128000,
            system_prompt_ratio=0.15,
            tool_schema_ratio=0.10,
            conversation_ratio=0.50,
            recall_ratio=0.10,
        )

        # 按 slot 名查询
        tool_budget_tokens = budget.get_slot_tokens("tool_schema")

        # 全量快照
        snapshot = budget.build_slot_snapshot()
    """

    __slots__ = (
        "_context_window",
        "_system_prompt_ratio",
        "_tool_schema_ratio",
        "_conversation_ratio",
        "_recall_ratio",
        "_system_prompt_tokens",
        "_tool_schema_tokens",
        "_conversation_tokens",
        "_recall_tokens",
        "_system_prompt_tokens_abs",
        "_tool_schema_tokens_abs",
    )

    # 类变量：slot 名到内部属性名的映射
    _SLOT_ATTR_MAP: ClassVar[dict[str, str]] = {
        "system_prompt": "_system_prompt_tokens",
        "tool_schema": "_tool_schema_tokens",
        "conversation": "_conversation_tokens",
        "recall": "_recall_tokens",
    }

    def __init__(
        self,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
        system_prompt_ratio: float = DEFAULT_SYSTEM_PROMPT_RATIO,
        tool_schema_ratio: float = DEFAULT_TOOL_SCHEMA_RATIO,
        conversation_ratio: float = DEFAULT_CONVERSATION_RATIO,
        recall_ratio: float = DEFAULT_RECALL_RATIO,
        system_prompt_tokens_abs: int | None = None,
        tool_schema_tokens_abs: int | None = None,
    ) -> None:
        # ── 默认值 WARNING ──
        if context_window == DEFAULT_CONTEXT_WINDOW:
            logger.warning(
                "context_window_budget: context_window 未显式传入，使用保守默认值 %d。"
                "建议查阅模型文档后显式配置。",
                DEFAULT_CONTEXT_WINDOW,
            )

        # ── 参数合法性校验 ──
        if not isinstance(context_window, int) or context_window <= 0:
            raise BehaviorConfigError(
                f"context_window_budget.context_window 必须是正整数，"
                f"当前值: {context_window!r}"
            )

        # ── 绝对槽位合法性校验 ──
        for _name, _value in (
            ("system_prompt_tokens_abs", system_prompt_tokens_abs),
            ("tool_schema_tokens_abs", tool_schema_tokens_abs),
        ):
            if _value is not None and (not isinstance(_value, int) or _value <= 0):
                raise BehaviorConfigError(
                    f"context_window_budget.{_name} 必须是正整数或 None，"
                    f"当前值: {_value!r}"
                )

        # ── 各 ratio 合法性校验 ──
        ratios = {
            "system_prompt_ratio": system_prompt_ratio,
            "tool_schema_ratio": tool_schema_ratio,
            "conversation_ratio": conversation_ratio,
            "recall_ratio": recall_ratio,
        }
        for name, value in ratios.items():
            if not isinstance(value, (int, float)) or value < 0 or value > 1.0:
                raise BehaviorConfigError(
                    f"context_window_budget.{name} 必须在 [0.0, 1.0] 范围内，"
                    f"当前值: {value!r}"
                )

        # ── ratio 总和校验 ──
        # 被绝对槽位接管的 slot：其 ratio 不再参与"总和 ≤ 1.0"校验，
        # 因为它的实际配额由绝对值决定，且 conversation 会自动吸收剩余（见下）。
        _abs_mode = (
            system_prompt_tokens_abs is not None or tool_schema_tokens_abs is not None
        )
        effective_ratios = {
            "system_prompt_ratio": (
                0.0 if system_prompt_tokens_abs is not None else system_prompt_ratio
            ),
            "tool_schema_ratio": (
                0.0 if tool_schema_tokens_abs is not None else tool_schema_ratio
            ),
            "conversation_ratio": (0.0 if _abs_mode else conversation_ratio),
            "recall_ratio": recall_ratio,
        }
        self._validate_ratios(effective_ratios)

        # ── 冻结字段赋值 ──
        object.__setattr__(self, "_context_window", context_window)
        object.__setattr__(self, "_system_prompt_ratio", system_prompt_ratio)
        object.__setattr__(self, "_tool_schema_ratio", tool_schema_ratio)
        object.__setattr__(self, "_conversation_ratio", conversation_ratio)
        object.__setattr__(self, "_recall_ratio", recall_ratio)
        object.__setattr__(self, "_system_prompt_tokens_abs", system_prompt_tokens_abs)
        object.__setattr__(self, "_tool_schema_tokens_abs", tool_schema_tokens_abs)

        # ── 计算各 slot 绝对 token 配额 ──
        # 为什么需要绝对槽位？
        #   实测显示 system_prompt / tool_schema 是"固定用途"的槽位，其真实占用
        #   与窗口大小无关（实测 coding system_prompt 17,590 / office 2,236，差 9.3 倍；
        #   13 个工具 schema 合计仅 3,374）。按比例给配额必然一处不够、一处浪费。
        #   见 COMPACT_BUDGET_AUDIT.md 结论 3。
        _sys_tokens = (
            system_prompt_tokens_abs
            if system_prompt_tokens_abs is not None
            else self._calculate_slot_tokens(context_window, system_prompt_ratio)
        )
        _tool_tokens = (
            tool_schema_tokens_abs
            if tool_schema_tokens_abs is not None
            else self._calculate_slot_tokens(context_window, tool_schema_ratio)
        )
        _recall_tokens = self._calculate_slot_tokens(context_window, recall_ratio)
        _conv_tokens = (
            context_window - _sys_tokens - _tool_tokens - _recall_tokens
            if _abs_mode
            else self._calculate_slot_tokens(context_window, conversation_ratio)
        )
        if _conv_tokens <= 0:
            raise BehaviorConfigError(
                f"context_window_budget: 绝对槽位配置下 conversation 剩余配额为 "
                f"{_conv_tokens} ≤ 0 "
                f"(context_window={context_window}, system={_sys_tokens}, "
                f"tool_schema={_tool_tokens}, recall={_recall_tokens})"
            )

        object.__setattr__(self, "_system_prompt_tokens", _sys_tokens)
        object.__setattr__(self, "_tool_schema_tokens", _tool_tokens)
        object.__setattr__(self, "_conversation_tokens", _conv_tokens)
        object.__setattr__(self, "_recall_tokens", _recall_tokens)

        logger.info(
            "context_window_budget: created context_window=%d, "
            "slots={system_prompt=%d%s, tool_schema=%d%s, conversation=%d, recall=%d}, "
            "abs_mode=%s",
            context_window,
            self._system_prompt_tokens,
            "(abs)" if system_prompt_tokens_abs is not None else "",
            self._tool_schema_tokens,
            "(abs)" if tool_schema_tokens_abs is not None else "",
            self._conversation_tokens, self._recall_tokens, _abs_mode,
        )

    # ── 不可变性保护 ─────────────────────────────────────────────────────────

    def __setattr__(self, name: str, value: object) -> None:
        logger.warning(
            "context_window_budget: 尝试修改冻结字段 '%s'，已拒绝。", name
        )
        raise PermissionError(
            f"ContextWindowBudget 是不可变对象，禁止修改字段 '{name}'。"
        )

    def __delattr__(self, name: str) -> None:
        raise PermissionError(
            f"ContextWindowBudget 是不可变对象，禁止删除字段 '{name}'。"
        )

    # ── 只读 property ────────────────────────────────────────────────────────

    @property
    def context_window(self) -> int:
        """模型输入上下文窗口大小（token）。"""
        return object.__getattribute__(self, "_context_window")

    @property
    def system_prompt_ratio(self) -> float:
        """system prompt slot 的配额比例。"""
        return object.__getattribute__(self, "_system_prompt_ratio")

    @property
    def tool_schema_ratio(self) -> float:
        """tool schema slot 的配额比例。"""
        return object.__getattribute__(self, "_tool_schema_ratio")

    @property
    def conversation_ratio(self) -> float:
        """conversation slot 的配额比例。"""
        return object.__getattribute__(self, "_conversation_ratio")

    @property
    def recall_ratio(self) -> float:
        """recall slot 的配额比例。"""
        return object.__getattribute__(self, "_recall_ratio")

    @property
    def system_prompt_tokens_abs(self) -> int | None:
        """system_prompt 的绝对配额覆盖。None = 由 ratio 推导。"""
        return object.__getattribute__(self, "_system_prompt_tokens_abs")

    @property
    def tool_schema_tokens_abs(self) -> int | None:
        """tool_schema 的绝对配额覆盖。None = 由 ratio 推导。"""
        return object.__getattribute__(self, "_tool_schema_tokens_abs")

    @property
    def is_abs_mode(self) -> bool:
        """是否启用了绝对槽位模式（此时 conversation = 剩余量，不受 ratio 控制）。"""
        return (
            self.system_prompt_tokens_abs is not None
            or self.tool_schema_tokens_abs is not None
        )

    @property
    def system_prompt_tokens(self) -> int:
        """system prompt slot 的绝对 token 配额。"""
        return object.__getattribute__(self, "_system_prompt_tokens")

    @property
    def tool_schema_tokens(self) -> int:
        """tool schema slot 的绝对 token 配额。"""
        return object.__getattribute__(self, "_tool_schema_tokens")

    @property
    def conversation_tokens(self) -> int:
        """conversation slot 的绝对 token 配额。"""
        return object.__getattribute__(self, "_conversation_tokens")

    @property
    def recall_tokens(self) -> int:
        """recall slot 的绝对 token 配额。"""
        return object.__getattribute__(self, "_recall_tokens")

    # ── 对外查询接口 ─────────────────────────────────────────────────────────

    def get_slot_tokens(self, slot_name: str) -> int:
        """按 slot 名查询该 slot 的绝对 token 配额。

        参数:
            slot_name: slot 名称，有效值为
                       "system_prompt", "tool_schema", "conversation", "recall"

        返回:
            该 slot 的绝对 token 配额（int）

        异常:
            ValueError: slot_name 不在有效列表中
        """
        attr_name = self._SLOT_ATTR_MAP.get(slot_name)
        if attr_name is None:
            raise ValueError(
                f"未知的 slot 名称: {slot_name!r}，"
                f"有效值为: {sorted(_VALID_SLOT_NAMES)}"
            )
        return object.__getattribute__(self, attr_name)

    def build_slot_snapshot(self) -> SlotSnapshot:
        """构建全量 slot 配额快照。

        供 MessageBuilder 一次性读取所有 input slot 的绝对 token 配额。

        返回:
            SlotSnapshot 不可变 dataclass，含所有 slot 的绝对 token 数
        """
        return SlotSnapshot(
            system_prompt_tokens=self.system_prompt_tokens,
            tool_schema_tokens=self.tool_schema_tokens,
            conversation_tokens=self.conversation_tokens,
            recall_tokens=self.recall_tokens,
        )

    # ── 内部方法 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_ratios(ratios: dict[str, float]) -> None:
        """校验所有 ratio 之和 ≤ 1.0。

        失败时抛 BehaviorConfigError，错误信息包含每个 ratio 的值和 sum 值。
        """
        ratio_sum = sum(ratios.values())
        if ratio_sum > 1.0:
            details = ", ".join(f"{k}={v}" for k, v in ratios.items())
            logger.error(
                "context_window_budget: ratio 之和 (%.4f) 超过 1.0: %s",
                ratio_sum, details,
            )
            raise BehaviorConfigError(
                f"context_window_budget: 所有 input slot ratio 之和必须 ≤ 1.0，"
                f"当前 sum={ratio_sum:.4f} ({details})"
            )

    @staticmethod
    def _calculate_slot_tokens(context_window: int, ratio: float) -> int:
        """计算单个 slot 的绝对 token 配额。

        使用 math.floor 取整，不允许浮点结果。
        """
        return math.floor(context_window * ratio)

    # ── repr ─────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        _sys_src = (
            "abs" if self.system_prompt_tokens_abs is not None
            else f"{self.system_prompt_ratio:.0%}"
        )
        _tool_src = (
            "abs" if self.tool_schema_tokens_abs is not None
            else f"{self.tool_schema_ratio:.0%}"
        )
        _conv_src = "剩余" if self.is_abs_mode else f"{self.conversation_ratio:.0%}"
        return (
            f"ContextWindowBudget("
            f"context_window={self.context_window}, "
            f"slots={{"
            f"system_prompt={_sys_src}→{self.system_prompt_tokens}, "
            f"tool_schema={_tool_src}→{self.tool_schema_tokens}, "
            f"conversation={_conv_src}→{self.conversation_tokens}, "
            f"recall={self.recall_ratio:.0%}→{self.recall_tokens}"
            f"}})"
        )
