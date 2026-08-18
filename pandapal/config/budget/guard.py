"""pandapal/config/budget/guard.py — 应用层费用停机守卫 + 运行级用量记账。

实现 SDK `StepGuard` 协议：SDK 每步 LLM 调用后调用 `should_halt`，本守卫按 **run_id**
累加该步用量与**实际净费用**（`cost_of_call().net_usd`，含缓存命中折扣，应用层价格表精算）。
累计净费用 ≥ `max_usd` → 返回 `GuardDecision(halt=True, reason=...)`，SDK 据此停机。

- 费用 per-step 累加（而非按 run 总量一次算），因此**混合模型**的 run 也精确。
- `max_usd=None` → 永不停机（只累加，供展示；仅作机制占位）。
- Fail-Safe（O3）：内部任何异常吞掉并返回 `GuardDecision(False)`，绝不因计价问题炸断 run。
- 累加器按 run_id 分桶，pause/resume 续用同桶。
- `spent(run_id)` → 本 run 累计净费用；`summary(run_id)` → 完整用量+费用汇总（会话末尾展示）。

可选注入 `BudgetLedger`（ledger.py）：注入后每步把净费用委托给它按 (user,provider) 分账
并取超额裁决；未注入则退化为「按 run 单一 max_usd」行为。ledger 仅作类型注解引用
（TYPE_CHECKING），运行时由外部注入实例，guard 不 import ledger 模块 → 无循环依赖。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

from pandaren.behavior.step_guard import GuardDecision, StepUsage

from pandapal.config.budget.pricing import (
    _COST_DECIMAL_PLACES,
    cost_of_call,
)

if TYPE_CHECKING:
    # 仅类型注解用：CostBudgetGuard.__init__ 的 ledger 参数 / ledger property 返回类型。
    # 运行时由外部注入实例，guard 调其 record_step（鸭子类型），不 import 本模块 → 无循环。
    from pandapal.config.budget.ledger import BudgetLedger

logger = logging.getLogger("pandapal.config.budget.guard")


class RunUsageSummary(NamedTuple):
    """一个 run 的累计用量 + 费用汇总（供会话末尾 REPLY_END 展示；派生量见 property）。

    token 口径：
      input_tokens = cached_tokens(命中) + miss_tokens(未命中)；cache_creation_tokens 为新写入。
      output_tokens = reply_tokens(llm 回复) + reasoning_tokens(推理)。
    费用口径（应用层价格表精算，per-step 累加，混合模型安全）：
      net_cost_usd(实际净费用) + saved_usd(命中节省) == full_cost_usd(全价基线)。
    """
    model: str
    input_tokens: int
    cached_tokens: int          # 命中
    cache_creation_tokens: int  # 新写入
    output_tokens: int          # 输出总量（含推理）
    reasoning_tokens: int       # 其中推理
    net_cost_usd: float
    full_cost_usd: float
    saved_usd: float

    @property
    def miss_tokens(self) -> int:
        """未命中输入 token = 输入 − 命中。"""
        return max(0, self.input_tokens - self.cached_tokens)

    @property
    def reply_tokens(self) -> int:
        """llm 回复 token = 输出 − 推理。"""
        return max(0, self.output_tokens - self.reasoning_tokens)

    @property
    def hit_rate(self) -> float:
        """命中率 = 命中 / 输入（0~1）。"""
        return (self.cached_tokens / self.input_tokens) if self.input_tokens else 0.0

    def to_dict(self) -> dict[str, float | int | str]:
        """JSON 可序列化 dict（供 IPC 出站，键与前端 REPLY_END.usage 对齐）。"""
        return {
            "model": self.model,
            "net_cost_usd": self.net_cost_usd,
            "full_cost_usd": self.full_cost_usd,
            "saved_usd": self.saved_usd,
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "miss_tokens": self.miss_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "output_tokens": self.output_tokens,
            "reply_tokens": self.reply_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "hit_rate": round(self.hit_rate, 4),
        }


@dataclass
class _RunAccount:
    """单个 run 的可变累加桶（内部用）。费用 per-step 累加以支持混合模型。"""
    model: str = ""
    input_tokens: int = 0
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    net_usd: float = 0.0
    full_usd: float = 0.0
    saved_usd: float = 0.0


class CostBudgetGuard:
    """应用层费用停机守卫 + 运行级用量记账（实现 SDK `StepGuard` 协议）。

    SDK 每步 LLM 调用后调用 `should_halt`：本守卫按 **run_id** 累加该步用量与**实际净费用**
    （`cost_of_call().net_usd`，含缓存命中折扣，应用层价格表精算）。累计净费用 ≥ `max_usd`
    → 返回 `GuardDecision(halt=True, reason=...)`，SDK 据此停机（`HALTED_BY_GUARD`）。

    - 费用 per-step 累加（而非按 run 总量一次算），因此**混合模型**的 run 也精确。
    - `max_usd=None` → 永不停机（只累加，供展示；仅作机制占位）。
    - 分时计费：高峰/空闲按本机当前本地时刻判定（`at=datetime.now().astimezone()`），
      与历史看板按调用落库时刻判档口径一致。
    - Fail-Safe（O3）：内部任何异常吞掉并返回 `GuardDecision(False)`，绝不因计价问题炸断 run。
    - 累加器按 run_id 分桶，pause/resume 续用同桶。
    - `spent(run_id)` → 本 run 累计净费用；`summary(run_id)` → 完整用量+费用汇总（会话末尾展示）。
    """

    def __init__(
        self,
        max_usd: float | None = None,
        *,
        ledger: "BudgetLedger | None" = None,
    ) -> None:
        self._max_usd = max_usd
        self._accounts: dict[str, _RunAccount] = {}
        # 可选：按 (user,provider) 分账的预算账本。注入后本守卫每步把净费用委托给它
        # 累加并取超额裁决（PRD 预算分账）；未注入则退化为原「按 run 单一 max_usd」行为。
        self._ledger = ledger

    @property
    def ledger(self) -> "BudgetLedger | None":
        """暴露注入的预算账本（供 executor 做 register_run / 前置拦截 / flush）。未注入→None。"""
        return self._ledger

    def should_halt(self, *, run_id: str, usage: StepUsage) -> GuardDecision:
        try:
            # 分时计费：按调用发生的本机当前时刻判档（高峰/空闲），与历史看板口径一致
            cc = cost_of_call(
                usage.model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens,
                at=datetime.now().astimezone(),
            )
            # per-run 汇总（供 footer summary，口径不变）
            acc = self._accounts.setdefault(run_id, _RunAccount())
            acc.model = usage.model or acc.model
            acc.input_tokens += max(0, usage.input_tokens)
            acc.cached_tokens += max(0, usage.cached_tokens)
            acc.cache_creation_tokens += max(0, usage.cache_creation_tokens)
            acc.output_tokens += max(0, usage.output_tokens)
            acc.reasoning_tokens += max(0, usage.reasoning_tokens)
            acc.net_usd = round(acc.net_usd + cc.net_usd, _COST_DECIMAL_PLACES)
            acc.full_usd = round(acc.full_usd + cc.full_usd, _COST_DECIMAL_PLACES)
            acc.saved_usd = round(acc.saved_usd + cc.saved_usd, _COST_DECIMAL_PLACES)
            # 按 (user,provider) 分账停机（委托账本；账本自身 Fail-Safe 不炸断）
            if self._ledger is not None:
                verdict = self._ledger.record_step(run_id, usage.provider, cc.net_usd)
                if verdict.halt:
                    return GuardDecision(halt=True, reason=verdict.reason)
            # 兼容：全局单一 max_usd（按 run 净费用）——账本未注入时的原行为
            if self._max_usd is not None and acc.net_usd >= self._max_usd:
                return GuardDecision(
                    halt=True,
                    reason=f"花费超限：本 run 累计 ${acc.net_usd:.4f} ≥ 预算 ${self._max_usd:.2f}",
                )
            return GuardDecision(halt=False)
        except Exception:  # noqa: BLE001 — 计价异常绝不炸断 run（O3 Fail-Safe）
            logger.exception("CostBudgetGuard.should_halt 计价异常，按不停机处理")
            return GuardDecision(halt=False)

    def spent(self, run_id: str) -> float:
        """某 run 已累计的净费用（USD）。供实时观测/快速取数，非 SDK 依赖。"""
        acc = self._accounts.get(run_id)
        return acc.net_usd if acc else 0.0

    def summary(self, run_id: str) -> RunUsageSummary | None:
        """某 run 的完整用量+费用汇总；无记录（如未发生 LLM 调用）→ None。"""
        acc = self._accounts.get(run_id)
        if acc is None:
            return None
        s = RunUsageSummary(
            model=acc.model,
            input_tokens=acc.input_tokens,
            cached_tokens=acc.cached_tokens,
            cache_creation_tokens=acc.cache_creation_tokens,
            output_tokens=acc.output_tokens,
            reasoning_tokens=acc.reasoning_tokens,
            net_cost_usd=acc.net_usd,
            full_cost_usd=acc.full_usd,
            saved_usd=acc.saved_usd,
        )
        return s

