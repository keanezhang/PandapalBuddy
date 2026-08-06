"""pandapal/budget/handler.py — 预算额度 IPC 直通 handler（按 provider 分账）。

镜像 DashboardHandler：不走 Router，复用 session_list 分派通道（app.py）。职责：
    - 入站 SET_BUDGET：设/改某 provider 额度 → BudgetLedger.set_budget → 回推 BUDGET_STATUS
    - 入站 BUDGET_QUERY：查询 → 回推 BUDGET_STATUS
    - bootstrap()：连接后首屏推一次 BUDGET_STATUS（额度条初始态）

事件出口约定（直通路径集中式转发改造）：
- handle_set_budget / handle_budget_query（请求-响应）：只构建并返回事件，
  由 InboundDispatcher 统一 broadcast.send() 并注入 origin_channel_id；
- bootstrap（连接后自主首推，非请求触发）：豁免路径，仍自广播。

分层：本 handler 只做「IPC ↔ 账本」适配 + 事件构建，不做记账/换算/停机（全在 BudgetLedger）。
user_id 权威取自进程（单用户 sidecar，构造时注入），**不信任入站 payload 的身份**（R8）。
O3：任何异常吞掉并留痕，绝不向 IPC 层抛。
"""

from __future__ import annotations

import logging
from typing import Any

from pandapal.events.normalized import NormalizedEvent

logger = logging.getLogger("pandapal.budget.handler")


class BudgetHandler:
    """预算额度直通 handler。ledger 为 None（未注入守卫账本）时全部 no-op。"""

    def __init__(self, ledger: Any, broadcast: Any, user_id: str) -> None:
        self._ledger = ledger
        self._broadcast = broadcast  # 仅豁免路径 bootstrap 使用
        self._user_id = user_id

    async def handle_set_budget(
        self, data: dict[str, Any]
    ) -> NormalizedEvent | list[NormalizedEvent] | None:
        """设/改某 provider 额度。data = {provider, currency, limit_native}。"""
        if self._ledger is None:
            return None
        provider = str(data.get("provider", "")).strip()
        currency = str(data.get("currency", "USD")).strip() or "USD"
        try:
            limit_native = float(data.get("limit_native"))
        except (TypeError, ValueError):
            return self._build_error_event("bad_limit", "额度必须为数字")
        # 延迟导入避免与 config 顶层循环；BudgetError 用于区分业务错误
        from pandapal.config.budget.ledger import BudgetError
        try:
            await self._ledger.seed_from_store()
            await self._ledger.set_budget(self._user_id, provider, currency, limit_native)
        except BudgetError as be:
            logger.warning("set_budget 业务错误 code=%s: %s", be.code, be.message)
            # 业务错误 + 最新额度态一并回推，保证额度条与后端一致
            return [
                self._build_error_event(be.code, be.message),
                *await self._build_status_events(),
            ]
        except Exception as exc:  # noqa: BLE001 — O3：不向 IPC 抛
            logger.exception("handle_set_budget 失败: %s", exc)
        # 无论成功/失败都回推一次最新额度态，保证额度条与后端一致
        status_events = await self._build_status_events()
        return status_events[0] if status_events else None

    async def handle_budget_query(
        self, data: dict[str, Any]
    ) -> NormalizedEvent | None:
        """查询并回推全部 provider 额度态。"""
        if self._ledger is None:
            return None
        try:
            await self._ledger.seed_from_store()
        except Exception:  # noqa: BLE001
            logger.exception("budget_query seed 失败")
        status_events = await self._build_status_events()
        return status_events[0] if status_events else None

    async def bootstrap(self) -> None:
        """连接后首屏推一次额度态（豁免路径：非请求触发，自广播）。"""
        if self._ledger is None:
            return
        try:
            await self._ledger.seed_from_store()
        except Exception:  # noqa: BLE001
            logger.exception("budget bootstrap seed 失败")
        for ev in await self._build_status_events():
            try:
                await self._broadcast.send(ev)
            except Exception:  # noqa: BLE001 — O3：广播失败不抛
                logger.exception("bootstrap 广播 BUDGET_STATUS 失败")

    # ── 内部 ──────────────────────────────────────────────────────────────
    async def _build_status_events(self) -> list[NormalizedEvent]:
        """构建最新额度态事件；失败时留痕并返回空列表（O3）。"""
        try:
            views = await self._ledger.get_status(self._user_id)
            budgets = [v.to_dict() for v in views]
            return [NormalizedEvent.budget_status(budgets=budgets)]
        except Exception:  # noqa: BLE001 — O3：构建失败不抛
            logger.exception("构建 BUDGET_STATUS 失败")
            return []

    def _build_error_event(self, code: str, message: str) -> NormalizedEvent:
        """构建预算错误事件（由 Dispatcher 统一转发）。"""
        return NormalizedEvent.global_error(
            error_code=f"budget_{code}", error_message=message
        )
