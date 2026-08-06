"""pandapal.dashboard — 只读看板聚合层。

按 storage_mode 二分两条数据源，产出同一强类型 DashboardSnapshot（禁止裸 dict 跨层）：
  - markdown：扫描 .pandapal/pandapal_md/users/{uid}/（metrics/traces/session/raw_log/audit/groups .md）
  - sqlite  ：查询 users/{uid}/{pandapal.db + observability.db}

两条源共用 BaseDashboardAggregator 的装配核心（join/费用/聚合），口径严格一致。
数据出处与口径见 docs/prd/dashboard/dashboard-需求设计.md §3。
"""

from pandapal.dashboard.base import BaseDashboardAggregator, DashboardSource
from pandapal.dashboard.models import (
    DashboardSnapshot,
    GlobalMetrics,
    SessionData,
    RunInfo,
    Turn,
    TurnLLM,
    ToolCall,
    ToolStat,
)
from pandapal.dashboard.aggregator import DashboardAggregator
from pandapal.dashboard.sqlite_aggregator import SQLiteDashboardAggregator


def build_dashboard_source(storage_mode: str, storage_path: str) -> DashboardSource:
    """按存储模式构造看板数据源。

    - storage_mode="sqlite" ：storage_path = StorageManager._storage_path（.../users/{uid}/pandapal.db）
                              observability.db 由聚合器从同目录推导。
    - 其余（markdown）      ：storage_path = markdown 存储根（.../pandapal_md/users/{uid}）
    """
    if storage_mode == "sqlite":
        return SQLiteDashboardAggregator(storage_path)
    return DashboardAggregator(storage_path)


__all__ = [
    "DashboardAggregator",
    "SQLiteDashboardAggregator",
    "BaseDashboardAggregator",
    "DashboardSource",
    "build_dashboard_source",
    "DashboardSnapshot",
    "GlobalMetrics",
    "SessionData",
    "RunInfo",
    "Turn",
    "TurnLLM",
    "ToolCall",
    "ToolStat",
]
