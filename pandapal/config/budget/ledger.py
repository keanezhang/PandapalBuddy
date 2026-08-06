"""pandapal/config/budget/ledger.py — 按 (user_id, provider) 分账的预算账本（唯一已花费真相源）。

设计文档：docs/design/budget-ledger-代码设计.md
职责：按 (user_id, provider) 累计净费用（唯一已花费量 spent_usd）、超额裁决、
      额度设置+币种换算+持久化、前置拦截判据、每 provider 额度视图组装。
不做：计价（调 cost_of_call，见 pricing.py）、provider 采集（消费 usage.provider）、
      user_id 产生（只登记 executor 给的权威值）、IPC、前端、聚合。

与展示/停机/Dashboard 同源：`spent_usd` 是唯一「已花费」量，既判停机（record_step）、
又展示（get_status）、又对齐 Dashboard 该 provider 聚合。

并发（见设计 5e）：单 event loop；`record_step`/`is_exhausted`/`register_run` 为
**纯同步无 await** 方法，单 loop 内原子执行、键控隔离 → 无锁。持久化 `set_budget`/
`seed_from_store`/`flush` 为 async（走 repo I/O），与同步累加不交错破坏不变量。
**红线：record_step 内绝不 await / 做 I-O。**

Fail-Safe（O3/E4）：账本自身任何异常一律降级为「不拦截」+ 留痕，绝不炸断正常 run。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import NamedTuple

from pandapal.config.budget.pricing import _COST_DECIMAL_PLACES
from pandapal.config.budget.repo import BudgetRepository
from pandapal.config.llm.model_prices import EXCHANGE_RATE_USD
from pandapal.config.llm.provider_catalog import BUILTIN_PROVIDERS
from pandapal.degradation import DegradationEvent, report_degradation

logger = logging.getLogger("pandapal.config.budget.ledger")

# ── 预算额度常量（B5 单一真相源）──────────────────────────────────────────────
# 临近告警阈值：spent/limit ≥ 该比例 → 额度条转「临近(黄)」（PRD §3.4.8）。
BUDGET_WARN_THRESHOLD: float = 0.8
# 币种→USD 汇率（内部记账恒 USD；用户按 provider 设展示/输入币种，默认 CNY）。
#
# ⚠️ 汇率的**唯一真相源**是 model_prices.toml 的 exchange_rate_usd，此处按其派生，
#    不再独立维护一份数值。历史上这里硬编码 CNY=0.14（隐含汇率 7.14），与定价表
#    的汇率并存且不一致——两份汇率会让「同一笔消费在计费与预算两处算出不同金额」。
#    调整汇率只改 model_prices.toml 一处。
DEFAULT_FX_TO_USD: dict[str, float] = {
    "USD": 1.0,
    "CNY": 1.0 / EXCHANGE_RATE_USD,
}


class BudgetError(Exception):
    """预算业务异常（BL5）。与技术异常（存储超时等）区分——后者在账本内 Fail-Safe 吞掉。

    `code` 供 IPC 层转成机器可读 error_code（A5）。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class HaltVerdict(NamedTuple):
    """账本对「本步之后是否因预算停机」的裁决（供 CostBudgetGuard 转 GuardDecision）。"""
    halt: bool
    reason: str = ""


@dataclass(frozen=True)
class BudgetView:
    """某 (user,provider) 额度的展示视图（供 BUDGET_STATUS 出站 / 前端额度条）。

    内部记账恒 USD；native 为该额度币种下的展示值。`state` 见 PRD §3.4.8 状态机。
    """
    provider: str
    currency: str
    limit_native: float | None      # None = 未设额度
    spent_native: float
    remaining_native: float | None  # None = 未设额度
    usage_ratio: float              # spent_usd / limit_usd（未设→0）
    state: str                      # unset | normal | near | exhausted
    spent_usd: float
    limit_usd: float | None

    def to_dict(self) -> dict[str, object]:
        """JSON 可序列化 dict（供 IPC 出站 / 前端消费）。"""
        return {
            "provider": self.provider,
            "currency": self.currency,
            "limit_native": self.limit_native,
            "spent_native": round(self.spent_native, 4),
            "remaining_native": (
                round(self.remaining_native, 4) if self.remaining_native is not None else None
            ),
            "usage_ratio": round(self.usage_ratio, 4),
            "state": self.state,
            "spent_usd": round(self.spent_usd, 8),
            "limit_usd": self.limit_usd,
        }


@dataclass
class BudgetAccount:
    """单个 (user,provider) 账户的可变累加桶（内部用）。

    只存权威量：币种 + 用户设定额度（native）+ 已花费（USD）。**不存 limit_usd**——
    它是 `limit_native × 当前FX` 的读时派生值（决策 E：运营调汇率即时生效）。
    """
    currency: str = "USD"
    limit_native: float | None = None  # None = 未设额度 = 不限该 provider
    spent_usd: float = 0.0


class BudgetLedger:
    """按 (user_id, provider) 分账的预算账本兼停机判据（唯一已花费真相源）。

    与展示/停机/Dashboard 同源：`spent_usd` 是唯一「已花费」量，既判停机（record_step）、
    又展示（get_status）、又对齐 Dashboard 该 provider 聚合。

    并发（见设计 5e）：单 event loop；`record_step`/`is_exhausted`/`register_run` 为
    **纯同步无 await** 方法，单 loop 内原子执行、键控隔离 → 无锁。持久化 `set_budget`/
    `seed_from_store`/`flush` 为 async（走 repo I/O），与同步累加不交错破坏不变量。
    **红线：record_step 内绝不 await / 做 I-O。**

    Fail-Safe（O3/E4）：账本自身任何异常一律降级为「不拦截」+ 留痕，绝不炸断正常 run。
    """

    # provider 白名单统一引用 provider_catalog（单一真相源）
    _KNOWN_PROVIDERS: frozenset[str] = frozenset(BUILTIN_PROVIDERS)

    def __init__(
        self,
        budget_repo: BudgetRepository,
        fx_table: dict[str, float] | None = None,
        *,
        warn_threshold: float = BUDGET_WARN_THRESHOLD,
    ) -> None:
        self._budget_repo = budget_repo
        self._fx_table = dict(fx_table) if fx_table else dict(DEFAULT_FX_TO_USD)
        self._warn = warn_threshold
        # 键控状态（BL2：无标量当前上下文，全按键隔离 → 并发安全）
        self._accounts: dict[tuple[str, str], BudgetAccount] = {}
        self._run_user: dict[str, str] = {}
        self._dirty: set[tuple[str, str]] = set()
        self._seeded: bool = False  # seed_from_store 幂等：只从持久层载入一次
        # 无法归属（缺 user_id 或 provider）时的净费用兜底桶：不能按 provider 分账停机，
        # 但费用不能凭空蒸发（对账可查）。按 provider 累计；provider 亦缺失记 ""。
        self._unattributed_usd: dict[str, float] = {}
        # 归属降级告警去重：一个 run 内多步降级只告警一次，避免刷屏（否则每步一条）。
        self._degraded_warned: set[str] = set()

    # ── 归属登记（executor 调用；user_id 权威、不 mint · R8）──────────────────
    def register_run(self, run_id: str, user_id: str) -> None:
        """登记 run_id → user_id（run 启动前，run_id 须为 SDK 内部 run_id）。"""
        if not run_id or not user_id:
            logger.warning(
                "BudgetLedger.register_run 拒绝空归属 run_id=%r user_id=%r", run_id, user_id
            )
            return
        self._run_user[run_id] = user_id

    def unregister_run(self, run_id: str) -> None:
        """run 结束清理归属映射（幂等）。"""
        self._run_user.pop(run_id, None)
        self._degraded_warned.discard(run_id)

    # ── 每步累加 + 停机裁决（同步原子，红线：不 await）────────────────────────
    def record_step(self, run_id: str, provider: str, net_usd: float) -> HaltVerdict:
        """累加该 (user,provider) 净费用并裁决是否因预算停机。异常→不拦截（O3/E4）。"""
        try:
            user_id = self._run_user.get(run_id)
            if not user_id or not provider:
                # 无法归属到 (user,provider)：不能按 provider 分账停机，但净费用记入兜底桶
                # （不静默丢弃，可对账），并按 run 去重告警（避免多步刷屏）。
                self._unattributed_usd[provider] = round(
                    self._unattributed_usd.get(provider, 0.0) + net_usd, _COST_DECIMAL_PLACES
                )
                if run_id not in self._degraded_warned:
                    self._degraded_warned.add(run_id)
                    cause = "归属缺失" if not user_id else "provider 未知"
                    logger.warning(
                        "budget.degraded %s run_id=%s → 记兜底桶不拦截（本 run 仅告警一次）",
                        cause, run_id,
                    )
                return HaltVerdict(halt=False)
            key = (user_id, provider)
            acc = self._resolve_account(key)
            acc.spent_usd = round(acc.spent_usd + net_usd, _COST_DECIMAL_PLACES)
            self._dirty.add(key)
            limit_usd = self._limit_usd(acc)
            if limit_usd is None:
                return HaltVerdict(halt=False)  # 未设额度 = 不限（E4）
            if acc.spent_usd >= limit_usd:
                logger.warning(
                    "budget.halt user=%s provider=%s spent=%.6f≥limit=%.6f run=%s",
                    user_id, provider, acc.spent_usd, limit_usd, run_id,
                )
                return HaltVerdict(
                    halt=True,
                    reason=(
                        f"预算耗尽：{provider} 累计 ${acc.spent_usd:.4f} ≥ 额度 ${limit_usd:.4f}"
                    ),
                )
            return HaltVerdict(halt=False)
        except Exception:  # noqa: BLE001 — Fail-Safe：账本异常绝不炸断 run（O3/E4）
            logger.exception("BudgetLedger.record_step 异常，按不拦截处理 run=%s", run_id)
            return HaltVerdict(halt=False)

    # ── 前置拦截判据（executor run 启动前调用）──────────────────────────────
    def is_exhausted(self, user_id: str, provider: str) -> bool:
        """某 (user,provider) 是否已达额度。无额度/异常 → False（不拦截）。"""
        try:
            acc = self._accounts.get((user_id, provider))
            if acc is None:
                return False
            limit_usd = self._limit_usd(acc)
            return limit_usd is not None and acc.spent_usd >= limit_usd
        except Exception:  # noqa: BLE001
            logger.exception("BudgetLedger.is_exhausted 异常，按不拦截处理")
            return False

    # ── 额度设置（IPC 调用，async 含持久化）─────────────────────────────────
    async def set_budget(
        self, user_id: str, provider: str, currency: str, limit_native: float
    ) -> BudgetView:
        """设/改某 provider 额度。非法输入抛 BudgetError；持久化失败也抛（告知未保存）。"""
        if not user_id:
            raise BudgetError("missing_user", "额度归属缺失 user_id")
        if provider not in self._KNOWN_PROVIDERS:
            raise BudgetError("unknown_provider", f"未知 provider：{provider}")
        if currency not in self._fx_table:
            raise BudgetError("unknown_currency", f"未配置汇率的币种：{currency}")
        if limit_native is None or limit_native < 0:
            raise BudgetError("bad_limit", "额度必须为非负数")
        key = (user_id, provider)
        acc = self._resolve_account(key)
        # 先记旧值：持久化失败时回滚内存，避免「告知未保存却已按新额度运行时拦截」的认知不符。
        prev_currency, prev_limit = acc.currency, acc.limit_native
        acc.currency = currency
        acc.limit_native = float(limit_native)
        try:
            await self._budget_repo.upsert_budget(user_id, provider, currency, float(limit_native))
        except Exception as exc:  # noqa: BLE001 — 技术异常转业务错误告知用户未保存（BL5）
            acc.currency = prev_currency
            acc.limit_native = prev_limit
            # 落盘失败已回滚内存态并冒泡 BudgetError 给用户（非静默）；再计一笔趋势看运维健康度。
            report_degradation(
                DegradationEvent.BUDGET_PERSIST_FAILED,
                category="capability", severity="ui_bubble", source="budget.ledger.set_budget",
                fallback="rolled_back", exc_info=True,
            )
            raise BudgetError("persist_failed", "额度未能保存，请重试") from exc
        logger.info(
            "budget.set.succeeded user=%s provider=%s currency=%s limit=%s",
            user_id, provider, currency, limit_native,
        )
        return self._to_view(provider, acc)

    async def get_status(self, user_id: str) -> list[BudgetView]:
        """该 user 全部**已设额度**的 provider 视图（供额度条）。"""
        views: list[BudgetView] = []
        for (uid, provider), acc in self._accounts.items():
            if uid == user_id and acc.limit_native is not None:
                views.append(self._to_view(provider, acc))
        return views

    def spent(self, user_id: str, provider: str) -> float:
        """某 (user,provider) 已累计净费用（USD）。"""
        acc = self._accounts.get((user_id, provider))
        return acc.spent_usd if acc else 0.0

    def unattributed_spent(self) -> float:
        """无法归属（缺 user_id 或 provider）而记入兜底桶的净费用合计（USD）。
        非 0 表示存在未分账消费，供对账/告警排查（正常运行应恒为 0）。"""
        return round(sum(self._unattributed_usd.values()), _COST_DECIMAL_PLACES)

    # ── 持久化 seed / flush ────────────────────────────────────────────────
    async def seed_from_store(self) -> None:
        """启动 seed：从 repo 载入 limit+spent 回内存（幂等，只载一次）。

        失败→空账户降级（不阻塞启动/首个 run），并标记已 seed 避免每步重试打爆日志。
        """
        if self._seeded:
            return
        self._seeded = True
        try:
            rows = await self._budget_repo.load_all()
        except Exception:  # noqa: BLE001 — 账本不炸启动（E4/I）
            logger.exception("budget.seed 失败，空账户降级起步")
            return
        for r in rows:
            self._accounts[(r.user_id, r.provider)] = BudgetAccount(
                currency=r.currency, limit_native=r.limit_native, spent_usd=r.spent_usd,
            )
        logger.info("budget.seed.succeeded account_count=%d", len(self._accounts))

    async def flush(self) -> None:
        """把脏账户的 spent 批量落盘（幂等）。失败→保留脏集下次重试。"""
        if not self._dirty:
            return
        entries = [
            (uid, provider, self._accounts[(uid, provider)].spent_usd)
            for (uid, provider) in self._dirty
            if (uid, provider) in self._accounts
        ]
        try:
            await self._budget_repo.bump_spent(entries)
        except Exception:  # noqa: BLE001 — 落盘失败不影响运行时判据，保留脏集重试
            logger.exception("budget.flush 落盘失败，保留脏集下次重试")
            return
        self._dirty.clear()
        logger.info("budget.flush.succeeded flushed_count=%d", len(entries))

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _resolve_account(self, key: tuple[str, str]) -> BudgetAccount:
        acc = self._accounts.get(key)
        if acc is None:
            acc = BudgetAccount()
            self._accounts[key] = acc
        return acc

    def _limit_usd(self, acc: BudgetAccount) -> float | None:
        """读时派生 limit_usd = limit_native × 当前FX（决策 E）。未设额度/FX 缺失→None。"""
        if acc.limit_native is None:
            return None
        fx = self._fx_table.get(acc.currency)
        if fx is None:
            # 金额类降级：汇率缺失 → 预算上限无法折算 → 该账户「不限额」（安全阀被静默关闭）。
            # 走统一通道（cost），按 currency 每小时去重。
            report_degradation(
                DegradationEvent.BUDGET_FX_MISSING,
                category="cost", source="budget.ledger._limit_usd",
                expected=f"FX rate for {acc.currency}", fallback="unlimited",
                dedup_key=f"fx_missing:{acc.currency}",
            )
            return None
        return round(acc.limit_native * fx, _COST_DECIMAL_PLACES)

    def _to_view(self, provider: str, acc: BudgetAccount) -> BudgetView:
        limit_usd = self._limit_usd(acc)
        fx = self._fx_table.get(acc.currency, 1.0) or 1.0
        spent_native = round(acc.spent_usd / fx, _COST_DECIMAL_PLACES)
        remaining_native = (
            max(0.0, acc.limit_native - spent_native) if acc.limit_native is not None else None
        )
        # limit_usd 三态区分：None=未设额度；0=额度为 0（首步即被 record_step 停机，须显示
        # 耗尽而非绿色正常，否则「显示正常却被停」认知不符）；>0 才按比例判定。
        if limit_usd is None:
            usage_ratio = 0.0
            state = "unset"
        elif limit_usd <= 0.0:
            usage_ratio = 1.0
            state = "exhausted"
        else:
            usage_ratio = acc.spent_usd / limit_usd
            if usage_ratio >= 1.0:
                state = "exhausted"
            elif usage_ratio >= self._warn:
                state = "near"
            else:
                state = "normal"
        return BudgetView(
            provider=provider,
            currency=acc.currency,
            limit_native=acc.limit_native,
            spent_native=spent_native,
            remaining_native=remaining_native,
            usage_ratio=usage_ratio,
            state=state,
            spent_usd=acc.spent_usd,
            limit_usd=limit_usd,
        )
