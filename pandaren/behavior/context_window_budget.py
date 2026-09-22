"""pandaren/behavior/context_window_budget.py — 上下文窗口 Token 预算（唯一真相源）

为所有消费方（ToolBudget / Memory / MessageBuilder / AgentLoop）提供明确的
预算数字，替代各模块各自为政、互不通信的碎片化 token 预算状态。

核心设计原则：
  S1    · 不可变性：创建后所有字段只读（frozen dataclass）
  E4/E5 · 失败安全：未配置时由 SDK 兜底值接管，不拒绝启动
  O3    · 错误必须显式处理：参数非法立即抛 BehaviorConfigError

记账口径（见 COMPACT_BUDGET_LAYERING_SPEC §2.3.0）：
  · system prompt 静态 → 用**实占** ``system_prompt_tokens`` 记账
  · tool schema  动态 → 用**熔断线** ``tool_cap_tokens`` 记账（与运行时裁剪同尺）

边界：
  管 → CW、熔断线（SlotBudget）、两个记账值、预算派生 property
  不管 → model_id 映射（app 的 toml / resolver）、实际 token 计数、
        压缩策略（CompactionProfile，见 pandaren/memory/constants.py）
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .exceptions import BehaviorConfigError
from ..memory.constants import COMPACT_BUFFER_TOKENS

logger = logging.getLogger("pandaren.behavior.context_window_budget")

#: SDK 侧 ε 兜底。应用层会注入自己的 ``ESTIMATOR_ERROR_RATIO``；
#: 二者由护栏测试锁死相等（见 SPEC §2.6）。
DEFAULT_ESTIMATOR_ERROR_RATIO: float = 0.15


@dataclass(frozen=True)
class SlotBudget:
    """固定用途槽位的「熔断线」：双尺子 = min(绝对上限, floor(CW × 占比上限))。

    语义：**上限，不是配额**。它不参与分配，只负责越界告警 + 截断
    （见 COMPACT_BUDGET_LAYERING_SPEC §2.3）。

    · 大窗口下绝对值生效（固定用途不随窗口膨胀）；
    · 极小窗口下占比项生效（防固定开销挤死对话区）。
    """

    absolute_cap: int
    cw_share: float

    def resolve(self, context_window: int) -> int:
        """按 CW 解析出实际熔断线值。"""
        return min(self.absolute_cap, math.floor(context_window * self.cw_share))

    def __post_init__(self) -> None:
        if self.absolute_cap <= 0:
            raise BehaviorConfigError(
                f"SlotBudget.absolute_cap 必须 > 0，收到 {self.absolute_cap}"
            )
        if not 0.0 <= self.cw_share <= 1.0:
            raise BehaviorConfigError(
                f"SlotBudget.cw_share 必须 ∈ [0,1]，收到 {self.cw_share}"
            )


@dataclass(frozen=True)
class ContextWindowBudget:
    """SDK 侧唯一预算对象（不可变）。只持「预算」字段与「预算派生」。

    ⚠️ 记账口径：静态槽位用实占、动态槽位用熔断线——
      · ``system_prompt_tokens`` = 实占（静态，参与记账）
      · ``tool_cap_tokens``      = 熔断线（动态，参与记账；运行时 schema 也被裁到它，同尺）
      · ``sys_cap`` / ``tool_cap`` = SlotBudget 双尺子定义（``.resolve(CW)`` 后才是熔断线值）

    ⚠️ 压缩派生（min_keep / max_keep / single_result / target）**不在这里**，
      归 ``CompactionProfile``（memory 模块）—— 那取决于压缩策略，与窗口预算无关。
    """

    context_window: int            # app 传：M × 固定比例（绝对值）
    sys_cap: SlotBudget            # 熔断线（双尺子定义）
    tool_cap: SlotBudget           # 熔断线（双尺子定义）
    system_prompt_tokens: int      # 记账值（实占，静态）
    output_tokens_reserve: int     # 输出预留（三层来源，见 SPEC §2.3.3）
    model_max_context: int         # 模型上限 M（I1 / 误差超支缓冲需要）
    estimator_error_ratio: float = DEFAULT_ESTIMATOR_ERROR_RATIO
    recall_tokens: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.context_window, int) or self.context_window <= 0:
            raise BehaviorConfigError(
                f"ContextWindowBudget.context_window 必须是正整数，"
                f"当前值: {self.context_window!r}"
            )
        if not isinstance(self.model_max_context, int) or self.model_max_context <= 0:
            raise BehaviorConfigError(
                f"ContextWindowBudget.model_max_context 必须是正整数，"
                f"当前值: {self.model_max_context!r}"
            )
        for _name in ("system_prompt_tokens", "output_tokens_reserve", "recall_tokens"):
            _value = getattr(self, _name)
            if not isinstance(_value, int) or _value < 0:
                raise BehaviorConfigError(
                    f"ContextWindowBudget.{_name} 必须是非负整数，当前值: {_value!r}"
                )
        if not 0.0 <= self.estimator_error_ratio <= 1.0:
            raise BehaviorConfigError(
                f"ContextWindowBudget.estimator_error_ratio 必须 ∈ [0,1]，"
                f"当前值: {self.estimator_error_ratio!r}"
            )
        if self.conversation_tokens <= 0:
            raise BehaviorConfigError(
                f"ContextWindowBudget: conversation 剩余 {self.conversation_tokens} ≤ 0 "
                f"(CW={self.context_window}, sys 实占={self.system_prompt_tokens}, "
                f"tool 熔断线={self.tool_cap_tokens}, recall={self.recall_tokens})"
            )

        logger.info(
            "ContextWindowBudget: CW=%d, sys_cap=%d(实占 %d), tool_cap=%d, conv=%d, T=%d",
            self.context_window, self.sys_cap_tokens, self.system_prompt_tokens,
            self.tool_cap_tokens, self.conversation_tokens, self.compact_threshold,
        )

    # ── 预算派生 property（见 SPEC §2.7）────────────────────────────────────

    @property
    def sys_cap_tokens(self) -> int:
        """system prompt 熔断线 = min(绝对上限, floor(CW × share))。"""
        return self.sys_cap.resolve(self.context_window)

    @property
    def tool_cap_tokens(self) -> int:
        """tool schema 熔断线（同尺用于记账与运行时裁剪）。"""
        return self.tool_cap.resolve(self.context_window)

    @property
    def conversation_tokens(self) -> int:
        """对话区可用 token = CW − 实占 sys − 熔断线 tool − recall。"""
        return (
            self.context_window
            - self.system_prompt_tokens
            - self.tool_cap_tokens
            - self.recall_tokens
        )

    @property
    def compact_threshold(self) -> int:
        """压缩触发阈值 T = conversation − COMPACT_BUFFER_TOKENS（全档固定 500）。"""
        return max(1, self.conversation_tokens - COMPACT_BUFFER_TOKENS)

    @property
    def error_cushion_tokens(self) -> int:
        """估算误差垫 = ε × CW（主动预留的比例项，随 CW 放大）。"""
        return round(self.context_window * self.estimator_error_ratio)

    @property
    def error_overspend_buffer_tokens(self) -> int:
        """误差超支缓冲 = I1 松弛量 = M − 输出预留 − CW×(1+ε)。

        真实误差超过假设 ε 时先消耗这里；吃光即撞 API 400。
        派生量，无归属；不是"没分配就浪费了"，而是本档唯一的安全裕度。
        可容忍的真实误差上限 = ``ε + 本值 / CW``。
        """
        return (
            self.model_max_context
            - self.output_tokens_reserve
            - round(self.context_window * (1 + self.estimator_error_ratio))
        )

    def __repr__(self) -> str:
        return (
            f"ContextWindowBudget(CW={self.context_window}, "
            f"sys_cap={self.sys_cap_tokens}(实占 {self.system_prompt_tokens}), "
            f"tool_cap={self.tool_cap_tokens}, conv={self.conversation_tokens}, "
            f"T={self.compact_threshold})"
        )


# ── SDK 兜底（无人配置时使用；只兜「预算」字段）────────────────────────────────
# 与 app 的常量存在护栏测试锁定关系（pandaren/tests/test_budget_layering.py）：
#   context_window == floor(toml [default].max_context × 0.80)
#   sys_cap == app SYSTEM_PROMPT_CAP ; tool_cap == app TOOL_SCHEMA_CAP
SDK_FALLBACK_BUDGET = ContextWindowBudget(
    context_window=102_400,
    sys_cap=SlotBudget(absolute_cap=24_000, cw_share=0.26),
    tool_cap=SlotBudget(absolute_cap=8_000, cw_share=0.10),
    system_prompt_tokens=19_203,
    output_tokens_reserve=32_000,
    model_max_context=128_000,
)
