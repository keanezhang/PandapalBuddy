"""pandapal.config — 配置管理模块（顶层 re-export，保向后兼容）。

按场景分子包：
    - system: 系统配置（.env 加载 / SystemConfig / 异常），与 LLM 无关
    - llm:    模型配置 + 模型切换（provider_catalog / credentials / llm_config / model_registry）
    - budget: 计费 / 预算（pricing / guard / ledger / repo）

老的 `from pandapal.config import X` 调用路径保持不变（本文件做 re-export）。
直接 import 子模块的请改用新路径，如 `from pandapal.config.llm.credentials_store import CredentialStore`。
"""

# ── system: 系统配置 ──
from pandapal.config.system.exceptions import (
    ConfigFileError,
    ConfigLoadError,
    ConfigStorageError,
    ConfigValidationError,
)
from pandapal.config.system.manager import ConfigManager
from pandapal.config.system.models import SystemConfig

# ── llm: 模型配置 + 模型切换 ──

# ── budget: 计费 / 预算 ──
from pandapal.config.budget.guard import CostBudgetGuard, RunUsageSummary
from pandapal.config.budget.ledger import (
    BudgetAccount,
    BudgetError,
    BudgetLedger,
    BudgetView,
    HaltVerdict,
)
from pandapal.config.budget.pricing import (
    cost_of_call,
    install_price_book,
    resolve_effective_price,
)
from pandapal.config.budget.repo import (
    BudgetRepository,
    BudgetRow,
    InMemoryBudgetRepo,
    JsonFileBudgetRepo,
)

__all__ = [
    # system
    "ConfigManager",
    "SystemConfig",
    "ConfigValidationError",
    "ConfigFileError",
    "ConfigStorageError",
    "ConfigLoadError",
    # llm
    # budget
    "CostBudgetGuard",
    "RunUsageSummary",
    "cost_of_call",
    "install_price_book",
    "resolve_effective_price",
    # 预算账本（按 provider 分账）
    "BudgetLedger",
    "BudgetView",
    "BudgetAccount",
    "BudgetError",
    "HaltVerdict",
    "BudgetRepository",
    "BudgetRow",
    "InMemoryBudgetRepo",
    "JsonFileBudgetRepo",
]
