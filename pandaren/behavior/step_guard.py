"""pandaren/behavior/step_guard.py — 通用「每步停机」机制契约（SDK 不知道停机理由）

分层原则（管控归 SDK 机制、判定归应用层）：
    SDK 只提供**一个通用机制**——每步 LLM 调用结束后，把本步**用量事实**
    （`StepUsage`：model / input / output / cached_tokens / step）交给应用层注入的
    `StepGuard`，由它返回 `GuardDecision(halt, reason)`。`halt=True` → SDK 立即终止
    run（R3 / `TerminalReason.HALTED_BY_GUARD`），并把 `reason` 透传给终止事件与审计。

    SDK 因此**完全不知道**停机的业务理由——费用、token 总量、自定义策略都行，SDK 只据
    `halt` 这个 bool 决定停不停，据 `reason` 决定终止事件里写什么。未注入守卫 → 永不停机。

    为什么是通用守卫而非「花费守卫」：SDK 层没有「价格/费用」概念（价格与预算全归应用层，
    见 pandapal.config.llm_pricing）。费用超限只是「应该停机的一种理由」，它天然是应用层
    的 `StepGuard` 实现（`CostBudgetGuard`）。把机制通用化后，同一个钩子可承载任意「每步
    之后的业务否决权」，不再让「cost」概念泄漏进 SDK。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class StepUsage:
    """一步 LLM 调用的用量事实（SDK 本就持有，打包交给守卫）。

    SDK 不解读这些数字的「价值」，只如实转交；由应用层守卫按需取用（据此计费/统计）。

    字段口径（与各 provider usage 对齐，缺省 0）：
      - input_tokens          本次 prompt 总输入 token
      - cached_tokens         命中 prefix cache 的输入 token（≤ input_tokens）
      - cache_creation_tokens 本次触发的缓存写入 token（Anthropic 语义；其它 provider 常为 0）
      - output_tokens         本次输出 token 总量（含推理，若 provider 把推理计入 completion）
      - reasoning_tokens      其中的推理 token（思考模型；provider 未提供则 0）
      - provider              发起本次调用的平台/API 厂商名（dashscope/volcengine/openai/
                              deepseek）。SDK 只如实转交，供应用层守卫按 provider 分账/统计
                              （如 CostBudgetGuard/BudgetLedger）。未知/未注入能力时为 ""。
    """
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    step: int
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    provider: str = ""


@dataclass(frozen=True)
class GuardDecision:
    """守卫对「本步之后是否停机」的裁决。

    - `halt`：SDK 唯一读取的字段——True 则立即终止 run。
    - `reason`：停机理由串，SDK 不解读，仅透传给终止事件 / 审计供展示。halt=False 时忽略。
    """
    halt: bool
    reason: str = ""


@runtime_checkable
class StepGuard(Protocol):
    """通用每步停机守卫协议（应用层实现，`.behavior(step_guard=...)` 注入）。"""

    def should_halt(self, *, run_id: str, usage: StepUsage) -> GuardDecision:
        """记账本步用量并裁决是否停机。

        约定：
          - 按 `run_id` 维护自己的累计状态（如净费用），据应用层策略判定是否停机。
          - SDK 只读 `GuardDecision.halt`；`reason` 由 SDK 透传，不做任何解读。
          - **Fail-Safe（O3）**：实现内部任何异常都应吞掉并返回 `GuardDecision(False)`，
            绝不因守卫自身问题把 run 炸断。
        """
        ...
