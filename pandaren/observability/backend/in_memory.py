"""pandaren/observability/backend/in_memory.py — InMemory 后端（测试 / 零配置启动用）"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from ..types import AuditRecord, Span


class InMemoryAuditBackend:
    """内存审计后端（默认实现，线程安全）。"""

    def __init__(self, max_records: int = 10000) -> None:
        self._records: list[AuditRecord] = []
        self._max_records = max_records
        self._lock = threading.Lock()

    def write(self, record: AuditRecord) -> None:
        with self._lock:
            self._records.append(record)
            if len(self._records) > self._max_records:
                self._records = self._records[-self._max_records:]

    def flush(self) -> None:
        pass

    def query(self, agent_id=None, event_type=None,
              start_time=None, end_time=None, limit=100) -> list[AuditRecord]:
        with self._lock:
            results = list(self._records)
        if agent_id:
            results = [r for r in results if r.agent_id == agent_id]
        if event_type:
            results = [r for r in results if r.event_type.value == event_type]
        if start_time:
            from datetime import datetime as _dt
            start_dt = _dt.fromisoformat(start_time) if isinstance(start_time, str) else start_time
            results = [r for r in results if r.timestamp >= start_dt]
        if end_time:
            from datetime import datetime as _dt
            end_dt = _dt.fromisoformat(end_time) if isinstance(end_time, str) else end_time
            results = [r for r in results if r.timestamp <= end_dt]
        results.sort(key=lambda r: r.timestamp, reverse=True)
        return results[:limit]


class InMemoryTracerBackend:
    """内存 Tracer 后端：存储 span 列表，供查询。"""

    def __init__(self, max_spans: int = 10000) -> None:
        self._spans: list[Span] = []
        self._max_spans = max_spans

    def export_span(self, span: Span) -> None:
        self._spans.append(span)
        if len(self._spans) > self._max_spans:
            self._spans = self._spans[-self._max_spans:]

    def get_spans(self, run_id: str | None = None) -> list[Span]:
        if run_id:
            return [s for s in self._spans if s.run_id == run_id]
        return list(self._spans)

    def query_spans(self, run_id: str) -> list[Span]:
        """TracerBackend Protocol 要求的查询接口。"""
        return self.get_spans(run_id)

    def clear(self) -> None:
        self._spans.clear()


class InMemoryLoggerBackend:
    """内存日志后端：存储结构化日志列表，供测试查询。"""

    def __init__(self, max_records: int = 10000) -> None:
        self._records: list[dict] = []
        self._max_records = max_records
        self._lock = threading.Lock()

    def write_log(self, record: dict) -> None:
        with self._lock:
            self._records.append(record)
            if len(self._records) > self._max_records:
                self._records = self._records[-self._max_records:]

    def get_records(self) -> list[dict]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


class InMemoryMetricsBackend:
    """内存 Metrics 后端：存储指标数据，线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._gauges: dict[str, float] = {}

    @staticmethod
    def _make_key(name: str, labels: dict[str, str]) -> str:
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    def record_counter(self, name: str, value: int, labels: dict[str, str]) -> None:
        with self._lock:
            self._counters[self._make_key(name, labels)] += value

    def record_histogram(self, name: str, value: float, labels: dict[str, str]) -> None:
        with self._lock:
            self._histograms[self._make_key(name, labels)].append(value)

    def record_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        with self._lock:
            self._gauges[self._make_key(name, labels)] = value

    def get_counter(self, name: str, labels: dict[str, str] | None = None) -> int:
        with self._lock:
            return self._counters.get(self._make_key(name, labels or {}), 0)

    def get_histogram(self, name: str, labels: dict[str, str] | None = None) -> list[float]:
        with self._lock:
            return list(self._histograms.get(self._make_key(name, labels or {}), []))

    def get_gauge(self, name: str, labels: dict[str, str] | None = None) -> float:
        with self._lock:
            return self._gauges.get(self._make_key(name, labels or {}), 0.0)

    def get_summary(self) -> dict[str, Any]:
        with self._lock:
            summary: dict[str, Any] = {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {},
            }
            for key, values in self._histograms.items():
                if values:
                    summary["histograms"][key] = {
                        "count": len(values), "min": min(values),
                        "max": max(values), "avg": sum(values) / len(values),
                        "sum": sum(values),
                    }
            return summary
