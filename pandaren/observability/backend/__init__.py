"""pandaren/observability/backend/ — 可观测后端实现

四种存储方式，统一在此管理：
  console.py   — Console 实时输出（stderr）
  in_memory.py — InMemory 存储（测试 / 零配置启动）
  markdown.py  — Markdown 文件持久化（调试 / 轻量生产）
  sqlite.py    — SQLite 持久化（正式生产 / 看板查询）
"""

from .console import (
    ConsoleAuditBackend,
    ConsoleTracerBackend,
    ConsoleMetricsBackend,
    ConsoleLoggerBackend,
)
from .in_memory import (
    InMemoryAuditBackend,
    InMemoryTracerBackend,
    InMemoryMetricsBackend,
    InMemoryLoggerBackend,
)
from .markdown import (
    MarkdownAuditBackend,
    MarkdownTracerBackend,
    MarkdownMetricsBackend,
    MarkdownLoggerBackend,
)
from .sqlite import (
    SQLiteAuditBackend,
    SQLiteTracerBackend,
    SQLiteMetricsBackend,
    SQLiteLoggerBackend,
)

__all__ = [
    # Console
    "ConsoleAuditBackend", "ConsoleTracerBackend", "ConsoleMetricsBackend", "ConsoleLoggerBackend",
    # InMemory
    "InMemoryAuditBackend", "InMemoryTracerBackend", "InMemoryMetricsBackend", "InMemoryLoggerBackend",
    # Markdown
    "MarkdownAuditBackend", "MarkdownTracerBackend", "MarkdownMetricsBackend", "MarkdownLoggerBackend",
    # SQLite
    "SQLiteAuditBackend", "SQLiteTracerBackend", "SQLiteMetricsBackend", "SQLiteLoggerBackend",
]
