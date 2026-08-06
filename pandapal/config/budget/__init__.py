"""pandapal.config.budget — 计费 / 预算。

子模块（从原 llm_pricing.py 拆分而来）：
    - pricing: 单价三级回落 + 价格账本 + 唯一计费函数 cost_of_call（费用真相源）
    - guard:   运行级费用停机守卫 CostBudgetGuard + 用量记账 RunUsageSummary
    - ledger:  按 (user,provider) 分账的预算账本 BudgetLedger（已花费真相源）
    - repo:    预算持久化（JSON 文件 / 内存，BudgetRepository Protocol）

依赖方向（无循环）：
    pricing ← degradation（外部）
    guard   ← pricing（cost_of_call）; ledger 仅类型注解（TYPE_CHECKING）
    ledger  ← pricing（_COST_DECIMAL_PLACES）; repo; provider_catalog
    repo    ← 无内部依赖（纯存储层，刻意不 import pricing/ledger 避免循环）
"""
from pandapal.config.budget.guard import CostBudgetGuard, RunUsageSummary
from pandapal.config.budget.ledger import (
    BUDGET_WARN_THRESHOLD,
    DEFAULT_FX_TO_USD,
    BudgetAccount,
    BudgetError,
    BudgetLedger,
    BudgetView,
    HaltVerdict,
)
from pandapal.config.budget.pricing import (
    EXCHANGE_RATE_USD,
    CallCost,
    ModelPrice,
    cost_of_call,
    install_price_book,
    price_book_size,
    resolve_effective_price,
)
from pandapal.config.budget.repo import (
    BudgetRepository,
    BudgetRow,
    InMemoryBudgetRepo,
    JsonFileBudgetRepo,
)

__all__ = [
    # pricing
    "CallCost",
    "EXCHANGE_RATE_USD",
    "ModelPrice",
    "cost_of_call",
    "install_price_book",
    "price_book_size",
    "resolve_effective_price",
    # guard
    "CostBudgetGuard",
    "RunUsageSummary",
    # ledger
    "BudgetAccount",
    "BudgetError",
    "BudgetLedger",
    "BudgetView",
    "HaltVerdict",
    "BUDGET_WARN_THRESHOLD",
    "DEFAULT_FX_TO_USD",
    # repo
    "BudgetRepository",
    "BudgetRow",
    "InMemoryBudgetRepo",
    "JsonFileBudgetRepo",
]
