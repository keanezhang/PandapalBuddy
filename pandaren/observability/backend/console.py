"""pandaren/observability/backend/console.py — Console 后端（实时输出到 stderr）"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from ..types import AuditRecord, Span, SpanType, SpanStatus, LogLevel


# ANSI 颜色
_MAGENTA = "\033[35m"
_YELLOW  = "\033[33m"
_GREEN   = "\033[32m"
_RESET   = "\033[0m"
_DIM     = "\033[2m"          # 暗化：用于时间戳、context、extra 等次要信息
_GRAY    = "\033[90m"         # 亮黑/灰：DEBUG 级别，低视觉噪音
_RED     = "\033[1;31m"       # 粗体红：ERROR 级别，强制视觉焦点

# 每个 LogLevel 对应的主色调
_LEVEL_COLOR: dict[LogLevel, str] = {
    LogLevel.DEBUG: _GRAY,
    LogLevel.INFO:  _GREEN,
    LogLevel.WARN:  _YELLOW,
    LogLevel.ERROR: _RED,
}

_LEVEL_NAME: dict[LogLevel, str] = {
    LogLevel.DEBUG: "DEBUG",
    LogLevel.INFO:  "INFO ",
    LogLevel.WARN:  "WARN ",
    LogLevel.ERROR: "ERROR",
}


class ConsoleAuditBackend:
    """控制台审计后端：将审计记录实时打印到 stderr。"""

    def write(self, record: AuditRecord) -> None:
        try:
            ts = record.timestamp.strftime("%H:%M:%S")
            severity = record.severity.value.upper()
            event = record.event_type.value
            detail = record.detail
            run_short = record.run_id[:8] if record.run_id else ""
            step_str = f", step={record.step_n}" if record.step_n is not None else ""
            tool_str = f", tool={record.tool_name}" if record.tool_name else ""
            line = (
                f"  {_MAGENTA}[AUDIT]{_RESET}  [{ts}] {severity} {event}: "
                f"{detail} (run={run_short}{step_str}{tool_str})"
            )
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass

    def flush(self) -> None:
        pass

    def query(self, agent_id=None, event_type=None,
              start_time=None, end_time=None, limit=100) -> list[AuditRecord]:
        _ = agent_id, event_type, start_time, end_time, limit
        return []


class ConsoleTracerBackend:
    """控制台 Tracer 后端：格式化输出 span 信息。"""

    def export_span(self, span: Span) -> None:
        try:
            status_str = "OK" if span.status == SpanStatus.OK else "ERR"
            duration = f"{span.duration_ms:.1f}ms" if span.duration_ms is not None else "N/A"
            if span.span_type == SpanType.RUN:
                indent = ""
            elif span.span_type == SpanType.STEP:
                indent = "  "
            else:
                indent = "    "
            step_str = f" step={span.step_n}" if span.step_n is not None else ""
            attrs_str = ""
            if span.attributes:
                attrs_str = " | " + ", ".join(f"{k}={v}" for k, v in span.attributes.items())
            line = (
                f"  {_YELLOW}[TRACE]{_RESET}  {indent}[{span.span_type.name.lower()}] {span.name} "
                f"{status_str} {duration}{step_str}{attrs_str}"
            )
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass


class ConsoleMetricsBackend:
    """控制台 Metrics 后端：实时打印关键指标变化。"""

    def record_counter(self, name: str, value: int, labels: dict[str, str]) -> None:
        try:
            show_labels = {k: v for k, v in labels.items() if k != "agent_id"}
            label_part = f" ({', '.join(f'{k}={v}' for k, v in show_labels.items())})" if show_labels else ""
            print(f"  {_GREEN}[METRIC]{_RESET} counter   {name}{label_part}: +{value}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def record_histogram(self, name: str, value: float, labels: dict[str, str]) -> None:
        try:
            show_labels = {k: v for k, v in labels.items() if k != "agent_id"}
            label_part = f" ({', '.join(f'{k}={v}' for k, v in show_labels.items())})" if show_labels else ""
            print(f"  {_GREEN}[METRIC]{_RESET} histogram {name}{label_part}: {value:.1f}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def record_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        try:
            show_labels = {k: v for k, v in labels.items() if k != "agent_id"}
            label_part = f" ({', '.join(f'{k}={v}' for k, v in show_labels.items())})" if show_labels else ""
            print(f"  {_GREEN}[METRIC]{_RESET} gauge     {name}{label_part}: {value:.2f}", file=sys.stderr, flush=True)
        except Exception:
            pass


class ConsoleLoggerBackend:
    """控制台日志后端：格式化输出到 stderr。"""

    def write_log(self, record: dict) -> None:
        try:
            level = record.get("level", "INFO")
            level_enum = LogLevel[level] if isinstance(level, str) else level
            color     = _LEVEL_COLOR.get(level_enum, _RESET)
            level_str = _LEVEL_NAME.get(level_enum, str(level))

            ts = record.get("timestamp", "")
            if isinstance(ts, datetime):
                ts = ts.strftime("%H:%M:%S")
            elif isinstance(ts, str) and len(ts) > 19:
                ts = ts[11:19]

            module  = record.get("module", "")
            message = record.get("message", "")
            run_id  = record.get("run_id", "")
            step_n  = record.get("step_n")

            ctx_parts = []
            if run_id:
                ctx_parts.append(f"run={run_id[:8]}")
            if step_n is not None:
                ctx_parts.append(f"step={step_n}")
            ctx_str = f" {_DIM}({', '.join(ctx_parts)}){_RESET}" if ctx_parts else ""

            extra = {k: v for k, v in record.items()
                     if k not in ("level", "timestamp", "module", "message",
                                  "run_id", "step_n", "agent_id", "log_id")}
            extra_str = (
                f" {_DIM}| {json.dumps(extra, default=str, ensure_ascii=False)}{_RESET}"
                if extra else ""
            )

            # DEBUG：整行灰色低调；WARN/ERROR：消息本体也染色；INFO：仅 tag+level 着色
            if level_enum == LogLevel.DEBUG:
                line = (
                    f"  {color}[LOG]    [{ts}] {level_str} {module}: {message}"
                    f"{ctx_str}{extra_str}{_RESET}"
                )
            elif level_enum == LogLevel.INFO:
                line = (
                    f"  {color}[LOG]{_RESET}    "
                    f"{_DIM}[{ts}]{_RESET} "
                    f"{color}{level_str}{_RESET} "
                    f"{module}: {message}{ctx_str}{extra_str}"
                )
            else:  # WARN / ERROR：整行主色，时间戳暗化保持层次
                line = (
                    f"  {color}[LOG]{_RESET}    "
                    f"{_DIM}[{ts}]{_RESET} "
                    f"{color}{level_str} {module}: {message}"
                    f"{ctx_str}{extra_str}{_RESET}"
                )

            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass
