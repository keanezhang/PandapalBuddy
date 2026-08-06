"""pandaren/memory/compaction — 切分与轻量清理子包

公共导出：
  - WindowedKeepPolicy          （SDK 默认 CompactionPolicy，三维度窗口保留）
  - MicroCompactor              （旧工具结果清理，与切分策略正交）
  - ensure_tool_pair_integrity  （API 硬约束守卫，所有切分路径必经）
"""

from .windowed import WindowedKeepPolicy
from .micro_compact import MicroCompactor
from .tool_pair_integrity import ensure_tool_pair_integrity

__all__ = [
    "WindowedKeepPolicy",
    "MicroCompactor",
    "ensure_tool_pair_integrity",
]
