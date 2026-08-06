"""pandaren/observability/protocols.py — Backend Protocol 定义"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .types import AuditRecord, Span


@runtime_checkable
class Sanitizer(Protocol):
    """数据脱敏 Protocol。

    默认实现：None（不脱敏）。
    可替换为：DefaultSanitizer / RegexSanitizer / 自定义实现。

    E4 Fail-Safe：脱敏失败时应返回 "[SANITIZE_ERROR]"，
    不暴露原始敏感数据。
    """

    def sanitize(self, data: str, field_name: str = "") -> str:
        """对数据进行脱敏处理。

        Args:
            data:       原始数据字符串
            field_name: 字段名称（可选，用于字段级脱敏规则）

        Returns:
            脱敏后的数据字符串。失败时返回 "[SANITIZE_ERROR]"。
        """
        ...


@runtime_checkable
class LoggerBackend(Protocol):
    def write_log(self, record: dict[str, Any]) -> None: ...


@runtime_checkable
class TracerBackend(Protocol):
    def export_span(self, span: Span) -> None: ...
    def query_spans(self, run_id: str) -> list[Span]:
        """根据 run_id 查询完整链路（可选方法，默认返回空列表）。"""
        return []


@runtime_checkable
class MetricsBackend(Protocol):
    def record_counter(self, name: str, value: int, labels: dict[str, str]) -> None: ...
    def record_histogram(self, name: str, value: float, labels: dict[str, str]) -> None: ...
    def record_gauge(self, name: str, value: float, labels: dict[str, str]) -> None: ...


@runtime_checkable
class AuditBackend(Protocol):
    def write(self, record: AuditRecord) -> None: ...
    def flush(self) -> None: ...
    def query(
        self,
        agent_id: str | None = None,
        event_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]: ...
