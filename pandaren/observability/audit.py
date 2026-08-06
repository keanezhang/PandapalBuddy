"""pandaren/observability/audit.py — 审计日志 Facade（HC4 核心）

HC4：write_sync() 同步写入 + 不可关闭 + 不可采样。
     写入失败 → AuditWriteError → 传播到 Loop。

后端实现在 backend/ 子目录中。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone

from .types import AuditEventType, AuditRecord, AuditSeverity, generate_id
from .protocols import AuditBackend
from .exceptions import AuditWriteError

logger = logging.getLogger("pandaren.observability.audit")


def _sanitize_surrogates(text: str) -> str:
    """移除字符串中的孤立的 surrogate 字符（U+D800-U+DFFF）。

    这些字符在合法的 UTF-8 中不应出现，但如果上游编码/解码出错
    （例如 UTF-8 bytes 被按 latin-1 解码），就可能混入此类字符，
    导致 json.dumps / open(encoding='utf-8') 抛出 UnicodeEncodeError。
    """
    if not text:
        return text
    try:
        # 尝试编码为 UTF-8 并忽略 surrogate 字符
        return text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
    except Exception:
        return text.encode("utf-8", errors="replace").decode("utf-8")

_DEFAULT_SEVERITY: dict[AuditEventType, AuditSeverity] = {
    AuditEventType.RUN_STARTED: AuditSeverity.INFO,
    AuditEventType.RUN_FINISHED: AuditSeverity.INFO,
    AuditEventType.PERMISSION_DENIED: AuditSeverity.WARN,
    AuditEventType.PERMISSION_GRANTED_HIGH_RISK: AuditSeverity.WARN,
    AuditEventType.HITL_REQUESTED: AuditSeverity.WARN,
    AuditEventType.HITL_APPROVED: AuditSeverity.INFO,
    AuditEventType.HITL_REJECTED: AuditSeverity.WARN,
    AuditEventType.AGENT_TERMINATED: AuditSeverity.CRITICAL,
    AuditEventType.INPUT_BLOCKED: AuditSeverity.WARN,
    AuditEventType.AUDIT_WRITE_FAILED: AuditSeverity.CRITICAL,
    # ── Tool 层审计事件 ──
    AuditEventType.TOOL_EXECUTED: AuditSeverity.INFO,
    # ── Skill 层审计事件 ──
    AuditEventType.SKILL_INVOKED: AuditSeverity.INFO,
    AuditEventType.SKILL_AUTO_TRIGGER_DENIED: AuditSeverity.WARN,
    AuditEventType.SKILL_OVERRIDDEN: AuditSeverity.WARN,
    # ── Agent Registry 层审计事件 ──
    AuditEventType.AGENT_REGISTERED: AuditSeverity.INFO,
    AuditEventType.AGENT_UNREGISTERED: AuditSeverity.INFO,
    AuditEventType.AGENT_STATUS_CHANGED: AuditSeverity.INFO,
    AuditEventType.AGENT_DELEGATED: AuditSeverity.INFO,
    AuditEventType.AGENT_DELEGATE_COMPLETED: AuditSeverity.INFO,
    AuditEventType.AGENT_DELEGATE_DENIED: AuditSeverity.WARN,
    AuditEventType.AGENT_DELEGATE_CYCLE: AuditSeverity.CRITICAL,
}


class DualAuditBackend:
    """双写审计后端：同时写入两个后端（通常是 Markdown + Console）。"""

    def __init__(self, primary: AuditBackend, secondary: AuditBackend) -> None:
        self._primary = primary
        self._secondary = secondary

    def write(self, record: AuditRecord) -> None:
        self._primary.write(record)
        self._secondary.write(record)

    def flush(self) -> None:
        self._primary.flush()
        self._secondary.flush()

    def query(self, agent_id=None, event_type=None,
              start_time=None, end_time=None, limit=100) -> list[AuditRecord]:
        return self._primary.query(
            agent_id=agent_id, event_type=event_type,
            start_time=start_time,
            end_time=end_time, limit=limit,
        )


class AuditLog:
    """审计日志系统（HC4 核心）。

    不提供 disable() / set_enabled(false) 方法。
    backend 不可为 None。
    """

    def __init__(self, backend: AuditBackend) -> None:
        if backend is None:
            raise ValueError(
                "AuditLog backend 不可为 None（HC4：审计不可关闭）。"
                "如需默认实现，使用 InMemoryAuditBackend()。"
            )
        self._backend = backend
        # ★ v2 并发安全：多 session 共享同一 AuditLog，
        #   write() + flush() 两步须原子化。
        self._lock = threading.Lock()

    def write_sync(
        self,
        event_type: AuditEventType,
        *,
        agent_id: str,
        run_id: str,
        detail: str,
        session_id: str = "",
        step_n: int | None = None,
        tool_name: str | None = None,
        terminal_reason: str | None = None,
        severity: AuditSeverity | None = None,
    ) -> None:
        """同步写入审计记录。写入失败 → AuditWriteError。

        session_id 用于后端按会话分片落盘；空字符串表示无会话归属（回落 global）。
        """
        # 清理 surrogate 字符，防止因上游编码错误导致 write/flush/fallback 全链路崩溃
        detail = _sanitize_surrogates(detail)
        if tool_name:
            tool_name = _sanitize_surrogates(tool_name)
        if terminal_reason:
            terminal_reason = _sanitize_surrogates(terminal_reason)

        record = AuditRecord(
            timestamp=datetime.now(timezone.utc),
            record_id=generate_id(),
            event_type=event_type,
            severity=severity or _DEFAULT_SEVERITY.get(event_type, AuditSeverity.INFO),
            agent_id=agent_id,
            run_id=run_id,
            session_id=session_id,
            detail=detail,
            step_n=step_n,
            tool_name=tool_name,
            terminal_reason=terminal_reason,
        )
        try:
            with self._lock:
                self._backend.write(record)
                self._backend.flush()
        except Exception as e:
            self._write_fallback(record, original_error=e)
            raise AuditWriteError(
                f"审计日志写入失败: event_type={event_type.value}, "
                f"agent_id={agent_id}, run_id={run_id}, error={e!r}"
            ) from e

    def query_records(
        self,
        *,
        agent_id: str | None = None,
        event_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        """查询审计记录，支持按时间范围过滤。"""
        return self._backend.query(
            agent_id=agent_id, event_type=event_type,
            start_time=start_time,
            end_time=end_time, limit=limit,
        )

    def _write_fallback(self, record: AuditRecord, original_error: Exception) -> None:
        try:
            # 二次清理，防止 record 中仍有残留的 surrogate 字符
            detail_clean = _sanitize_surrogates(record.detail)
            error_clean = _sanitize_surrogates(str(original_error))
            fallback_data = {
                "AUDIT_FALLBACK": True,
                "timestamp": record.timestamp.isoformat(),
                "event_type": record.event_type.value,
                "agent_id": record.agent_id,
                "run_id": record.run_id,
                "detail": detail_clean,
                "original_error": error_clean,
            }
            print(json.dumps(fallback_data, ensure_ascii=False), file=sys.stderr, flush=True)
        except Exception:
            pass
