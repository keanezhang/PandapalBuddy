"""Observability 层：让 Agent 的每一步都看得见。

结构：
  protocols.py     — Backend Protocol 定义（含 Sanitizer Protocol）
  config.py        — ObservabilityConfig 统一配置入口
  audit.py         — AuditLog Facade（HC4 核心）
  tracer.py        — Tracer Facade（含 build_trace_context / _sanitize_attributes）
  metrics.py       — Metrics Facade（通用 API + 命名便捷 API）
  logger.py        — Logger Facade（含通用 log() 方法）
  sanitizer.py     — DefaultSanitizer 数据脱敏
  backend/         — 所有后端实现（Console / InMemory / Markdown / SQLite）
  hooks_adapter.py — Loop Hooks 桥接器（11 个 hook → trace + metrics + logs）
  provider.py      — ObservabilityProvider 工厂（含 build_observability_context）
"""

from .types import (
    AuditEventType, AuditSeverity, AuditRecord,
    ObservabilityContext, LogLevel, TraceLevel, SpanType, SpanStatus, Span,
    generate_id,
)
from .protocols import LoggerBackend, TracerBackend, MetricsBackend, AuditBackend, Sanitizer
from .config import ObservabilityConfig
from .sanitizer import DefaultSanitizer
from .audit import AuditLog, DualAuditBackend
from .tracer import Tracer
from .metrics import Metrics
from .logger import Logger
from .backend import (
    # Console
    ConsoleAuditBackend, ConsoleTracerBackend, ConsoleMetricsBackend, ConsoleLoggerBackend,
    # InMemory
    InMemoryAuditBackend, InMemoryTracerBackend, InMemoryMetricsBackend,
    # Markdown
    MarkdownAuditBackend, MarkdownTracerBackend, MarkdownMetricsBackend,
    # SQLite
    SQLiteAuditBackend, SQLiteTracerBackend, SQLiteMetricsBackend, SQLiteLoggerBackend,
)
from .provider import ObservabilityProvider
from .hooks_adapter import ObservabilityHooksAdapter
from .exceptions import ObservabilityError, AuditWriteError, SanitizeError

__all__ = [
    # 类型
    "AuditEventType", "AuditSeverity", "AuditRecord",
    "ObservabilityContext", "LogLevel", "TraceLevel", "SpanType", "SpanStatus", "Span",
    "generate_id",
    # Protocols
    "LoggerBackend", "TracerBackend", "MetricsBackend", "AuditBackend", "Sanitizer",
    # 配置
    "ObservabilityConfig",
    # 脱敏
    "DefaultSanitizer",
    # Facade
    "AuditLog", "DualAuditBackend", "Tracer", "Metrics", "Logger",
    # Backend — Console
    "ConsoleAuditBackend", "ConsoleTracerBackend", "ConsoleMetricsBackend", "ConsoleLoggerBackend",
    # Backend — InMemory
    "InMemoryAuditBackend", "InMemoryTracerBackend", "InMemoryMetricsBackend",
    # Backend — Markdown
    "MarkdownAuditBackend", "MarkdownTracerBackend", "MarkdownMetricsBackend",
    # Backend — SQLite
    "SQLiteAuditBackend", "SQLiteTracerBackend", "SQLiteMetricsBackend", "SQLiteLoggerBackend",
    # 工厂 + 适配器
    "ObservabilityProvider", "ObservabilityHooksAdapter",
    # 异常
    "ObservabilityError", "AuditWriteError", "SanitizeError",
]
