"""pandaren/observability/logger.py — 结构化日志 Facade

Logger 子系统：结构化日志记录。
非 HC4 子系统——故障时优雅降级，不传播异常到 Loop。

后端实现在 backend/ 子目录中。

设计文档对齐：
  log(level, message, **context)           → 结构化日志输出
  debug/info/warn/error(message, **context) → log() 的快捷方法
  _format_record(level, message, context)   → 格式化为结构化 JSON 记录
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .types import LogLevel, generate_id
from .protocols import LoggerBackend
from .backend.console import ConsoleLoggerBackend


class Logger:
    """结构化日志系统。"""

    def __init__(self, *, backend: LoggerBackend | None = None,
                 min_level: LogLevel = LogLevel.DEBUG, agent_id: str = "") -> None:
        self._backend: LoggerBackend = backend or ConsoleLoggerBackend()
        self._min_level = min_level
        self._agent_id = agent_id

    def _should_log(self, level: LogLevel) -> bool:
        return level >= self._min_level

    def _format_record(self, level: LogLevel, message: str, context: dict[str, Any]) -> dict[str, Any]:
        """格式化为结构化记录。"""
        return {
            "log_id": generate_id(),
            "timestamp": datetime.now(timezone.utc),
            "level": level.name,
            "module": context.get("module", ""),
            "message": message,
            "agent_id": context.get("agent_id", self._agent_id),
            "run_id": context.get("run_id", ""),
            # session_id 由调用方（hooks_adapter，源自 run 的 session）显式传入，
            # 供 MarkdownLoggerBackend 按会话分片；空串 → _no_session（全局级日志）。
            "session_id": context.get("session_id", ""),
            "step_n": context.get("step_n"),
            **{k: v for k, v in context.items()
               if k not in ("module", "agent_id", "run_id", "session_id", "step_n")},
        }

    def _write(self, level: LogLevel, message: str, **context: Any) -> None:
        if not self._should_log(level):
            return
        try:
            record = self._format_record(level, message, context)
            self._backend.write_log(record)
        except Exception:
            pass

    def log(self, level: LogLevel, message: str, **context: Any) -> None:
        """通用日志方法——按指定级别输出结构化日志。

        context 可包含：module, agent_id, run_id, step_n 及其他自定义字段。
        """
        self._write(level, message, **context)

    def debug(self, message: str, **context: Any) -> None:
        self._write(LogLevel.DEBUG, message, **context)

    def info(self, message: str, **context: Any) -> None:
        self._write(LogLevel.INFO, message, **context)

    def warn(self, message: str, **context: Any) -> None:
        self._write(LogLevel.WARN, message, **context)

    def error(self, message: str, **context: Any) -> None:
        self._write(LogLevel.ERROR, message, **context)

    def set_agent_id(self, agent_id: str) -> None:
        self._agent_id = agent_id

    def __repr__(self) -> str:
        return f"Logger(min_level={self._min_level.name}, agent_id='{self._agent_id}')"
