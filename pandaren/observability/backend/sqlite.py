"""pandaren/observability/backend/sqlite.py — SQLite 可观测后端

为 AuditLog / Tracer / Metrics / Logger 提供 SQLite 持久化后端，
零外部依赖（stdlib ``sqlite3``），面向正式生产。

与 markdown.py 的取舍差异：
  - markdown 按 session_id **物理分目录**分片；SQLite 不需要——session_id 退化为
    一个带索引的列，隔离靠 ``WHERE session_id = ?``，更干净、可跨 session 聚合。
  - markdown 的 metrics 是"聚合快照覆盖写"（丢时间维度）；SQLite 采用**事件流**：
    每个数据点 append 一行，``get_summary()`` 用 ``GROUP BY`` 实时聚合，
    使生产看板能按时间/会话切片。
  - span.attributes **全量存 JSON**（不做白名单渲染）——历史上 markdown 白名单
    渲染漏了 model/cost 字段，逼看板去解析自由文本；JSON 全存杜绝此类口径丢失。

范式对齐（与 memory/backends/sqlite_raw_log.py 严格一致）：
  - ``db_path`` 与 ``connection`` 严格互斥（二选一）
  - 拒绝 ``db_path=":memory:"``——无持久化价值，单测用 pytest ``tmp_path``
  - 默认 WAL 模式；``check_same_thread=False`` + ``isolation_level="DEFERRED"``
  - ``row_factory = sqlite3.Row``；schema 用 ``IF NOT EXISTS`` 幂等初始化
  - ``close()`` 只关闭自持有的连接；支持 ``with`` 上下文

共库（single-DB）用法——让 span 能与 raw_message 按 (run_id, step) join::

    conn = sqlite3.connect("observability.db", check_same_thread=False)
    audit  = SQLiteAuditBackend(connection=conn)
    tracer = SQLiteTracerBackend(connection=conn)
    metrics= SQLiteMetricsBackend(connection=conn)
    logs   = SQLiteLoggerBackend(connection=conn)
    # 把同一个 conn 也传给 SQLiteRawLogBackend，即可跨表 join

装配（Provider 四态里 SQLite 需要路径 → 作为实例注入，不走 "mem" 魔法字符串）::

    AgentBuilder().observability(
        audit=SQLiteAuditBackend(db_path="./observability.db"),
    ).build()
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..types import (
    AuditEventType,
    AuditRecord,
    AuditSeverity,
    Span,
    SpanStatus,
    SpanType,
)

logger = logging.getLogger("pandaren.observability.backend.sqlite")


# ════════════════════════════════════════════════════════════════════════
#  Schema（每张表独立，backend 各自 init 自己的表——共库时互不干扰）
# ════════════════════════════════════════════════════════════════════════

_SCHEMA_AUDIT = """
CREATE TABLE IF NOT EXISTS audit_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       TEXT    NOT NULL,
    ts              TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    severity        TEXT    NOT NULL,
    agent_id        TEXT,
    run_id          TEXT,
    session_id      TEXT,
    step_n          INTEGER,
    tool_name       TEXT,
    terminal_reason TEXT,
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_session_ts ON audit_records(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_run        ON audit_records(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_event_ts   ON audit_records(event_type, ts);
CREATE INDEX IF NOT EXISTS idx_audit_agent      ON audit_records(agent_id);
"""

_SCHEMA_SPANS = """
CREATE TABLE IF NOT EXISTS spans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    span_id         TEXT    NOT NULL,
    trace_id        TEXT,
    parent_span_id  TEXT,
    span_type       TEXT    NOT NULL,
    name            TEXT,
    status          TEXT,
    agent_id        TEXT,
    run_id          TEXT,
    session_id      TEXT,
    step_n          INTEGER,
    start_time      TEXT,
    end_time        TEXT,
    duration_ms     REAL,
    attributes_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_run           ON spans(run_id);
CREATE INDEX IF NOT EXISTS idx_spans_session_start ON spans(session_id, start_time);
CREATE INDEX IF NOT EXISTS idx_spans_trace         ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_run_step      ON spans(run_id, step_n);
CREATE INDEX IF NOT EXISTS idx_spans_type_start    ON spans(span_type, start_time);
"""

_SCHEMA_METRICS = """
CREATE TABLE IF NOT EXISTS metrics_points (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    kind        TEXT    NOT NULL,   -- counter / histogram / gauge
    value       REAL    NOT NULL,
    labels_json TEXT,
    session_id  TEXT,               -- 从 labels 提取（若有），便于按会话切片
    run_id      TEXT,
    ts          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_name_ts    ON metrics_points(name, ts);
CREATE INDEX IF NOT EXISTS idx_metrics_session    ON metrics_points(session_id);
"""

_SCHEMA_LOGS = """
CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    level       TEXT,
    module      TEXT,
    message     TEXT,
    agent_id    TEXT,
    run_id      TEXT,
    session_id  TEXT,
    step_n      INTEGER,
    log_id      TEXT,
    extra_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_session_ts ON logs(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_logs_run        ON logs(run_id);
CREATE INDEX IF NOT EXISTS idx_logs_level       ON logs(level);
"""


# ════════════════════════════════════════════════════════════════════════
#  工具函数
# ════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_enum(enum_cls: type[Enum], value: Any, fallback: Any = None, *, label: str = "") -> Any:
    """枚举转换防御：未知值（版本演进/坏数据）记 warning 并回落，不让整个查询崩溃。

    - ``fallback=None``：该字段无合理回落（如 span_type/event_type 无法归类）→
      返回 None，由调用方**跳过该行**（数据仍在库中，仅读侧不返回）。
    - 有回落值：降级使用（severity→MEDIUM、status→OK），warning 留痕不丢行。
    """
    if not value:
        return fallback
    try:
        return enum_cls(value)
    except ValueError:
        logger.warning(
            "SQLite read: unknown %s value %r — 该行按坏数据处理（label=%s）",
            enum_cls.__name__, value, label or enum_cls.__name__,
        )
        return fallback


def _dt_to_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _normalize_time_bound(value: str | datetime | None) -> str | None:
    """把 query 的 start_time/end_time 归一为 ISO 文本，用于字符串比较。

    ISO8601（UTC，同精度）按字典序可比较——存写两侧都用 ``datetime.isoformat()``，
    量纲一致，故 ``ts >= ?`` / ``ts <= ?`` 的字符串比较等价于时间比较。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ════════════════════════════════════════════════════════════════════════
#  连接生命周期基类
# ════════════════════════════════════════════════════════════════════════

class _SQLiteBackendBase:
    """SQLite 后端共享的连接管理（对齐 sqlite_raw_log 的范式）。

    子类只需提供 ``_SCHEMA`` 类属性并在 ``__init__`` 末尾调用 ``self._init_schema()``。
    """

    _SCHEMA: str = ""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        wal_mode: bool = True,
    ) -> None:
        if (db_path is None) == (connection is None):
            raise ValueError(
                f"{type(self).__name__}: provide exactly one of db_path or connection "
                "(got both or neither)."
            )
        if db_path is not None and str(db_path) == ":memory:":
            raise ValueError(
                f"{type(self).__name__}: db_path=':memory:' is not supported. "
                "Use a real file path (use pytest tmp_path fixture for ephemeral storage)."
            )

        self._db_path: Path | None = Path(db_path) if db_path is not None else None
        self._owns_connection: bool = connection is None
        # 每实例一把锁：保护多语句读-聚合的一致性；单语句 INSERT 也串行化，
        # 避免与共享 connection 的其他后端在 CPython sqlite 上交错（虽默认 serialized，
        # 但显式锁让 get_summary 这类"读多行再聚合"不被并发写穿插）。
        self._lock = threading.Lock()

        if connection is not None:
            self._conn = connection
        else:
            assert self._db_path is not None
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level="DEFERRED",
            )
            if wal_mode:
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.Error as exc:
                    logger.warning(
                        "%s: failed to enable WAL mode: %s", type(self).__name__, exc,
                    )

        self._conn.row_factory = sqlite3.Row

    def _init_schema(self) -> None:
        """初始化本后端的表（IF NOT EXISTS，幂等；共库时与其他后端互不干扰）。"""
        with self._lock, self._conn:
            self._conn.executescript(self._SCHEMA)

    # ── 资源管理 ──

    def close(self) -> None:
        """关闭内部持有的连接。

        - 自构造的 connection（``db_path`` 模式）：会被关闭
        - 外部传入的 connection：**不**关闭（生命周期由调用方掌控，共库场景常见）
        """
        if self._owns_connection:
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                logger.warning(
                    "%s.close: failed to close connection: %s", type(self).__name__, exc,
                )

    def __enter__(self):
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


# ════════════════════════════════════════════════════════════════════════
#  Audit
# ════════════════════════════════════════════════════════════════════════

class SQLiteAuditBackend(_SQLiteBackendBase):
    """SQLite 审计后端（实现 AuditBackend Protocol）。

    append-only（HC4）：审计记录只增不改。``query`` 支持按 agent/事件类型/时间窗过滤。
    """

    _SCHEMA = _SCHEMA_AUDIT

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        wal_mode: bool = True,
    ) -> None:
        super().__init__(db_path, connection=connection, wal_mode=wal_mode)
        self._init_schema()

    def write(self, record: AuditRecord) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit_records "
                "(record_id, ts, event_type, severity, agent_id, run_id, session_id, "
                " step_n, tool_name, terminal_reason, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.record_id,
                    record.timestamp.isoformat(),
                    record.event_type.value,
                    record.severity.value,
                    record.agent_id or None,
                    record.run_id or None,
                    record.session_id or "",
                    record.step_n,
                    record.tool_name,
                    record.terminal_reason,
                    record.detail,
                ),
            )

    def flush(self) -> None:
        # 每次 write 都在自身事务内 commit，无缓冲需刷；WAL checkpoint 交给 sqlite。
        pass

    def query(
        self,
        agent_id: str | None = None,
        event_type: str | None = None,
        start_time: str | datetime | None = None,
        end_time: str | datetime | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        start_iso = _normalize_time_bound(start_time)
        if start_iso:
            clauses.append("ts >= ?")
            params.append(start_iso)
        end_iso = _normalize_time_bound(end_time)
        if end_iso:
            clauses.append("ts <= ?")
            params.append(end_iso)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM audit_records {where} "
            "ORDER BY ts DESC, id DESC LIMIT ?"
        )
        params.append(int(limit))

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [rec for r in rows if (rec := self._row_to_record(r)) is not None]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AuditRecord | None:
        event_type = _safe_enum(AuditEventType, row["event_type"], fallback=None, label="AuditEventType")
        # severity 无 UNKNOWN 值：审计行是 HC4 强制保留的，未知 severity 降级 WARN
        # （warning 留痕，WARN 为"需关注"中性档），不丢行——比跳过更符合审计不可绕过。
        severity = _safe_enum(AuditSeverity, row["severity"], fallback=AuditSeverity.WARN, label="AuditSeverity")
        if event_type is None:
            return None  # 未知事件类型无法语义化，跳过该行（数据仍在库中）
        return AuditRecord(
            timestamp=_iso_to_dt(row["ts"]) or datetime.now(timezone.utc),
            record_id=row["record_id"],
            event_type=event_type,
            severity=severity,
            agent_id=row["agent_id"] or "",
            run_id=row["run_id"] or "",
            detail=row["detail"] or "",
            session_id=row["session_id"] or "",
            step_n=row["step_n"],
            tool_name=row["tool_name"],
            terminal_reason=row["terminal_reason"],
        )

    def __repr__(self) -> str:
        target = self._db_path if self._db_path else "shared-connection"
        return f"SQLiteAuditBackend(db='{target}')"


# ════════════════════════════════════════════════════════════════════════
#  Tracer
# ════════════════════════════════════════════════════════════════════════

class SQLiteTracerBackend(_SQLiteBackendBase):
    """SQLite Tracer 后端（实现 TracerBackend Protocol）。

    span.attributes 全量存 JSON；``query_spans(run_id)`` 按 run 关联，
    ``run_id/step_n`` 落独立列，可与 raw_messages 按 (run_id, step) join（共库时）。
    """

    _SCHEMA = _SCHEMA_SPANS

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        wal_mode: bool = True,
    ) -> None:
        super().__init__(db_path, connection=connection, wal_mode=wal_mode)
        self._init_schema()

    def export_span(self, span: Span) -> None:
        attrs_json = (
            json.dumps(span.attributes, ensure_ascii=False, default=str)
            if span.attributes else None
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO spans "
                "(span_id, trace_id, parent_span_id, span_type, name, status, "
                " agent_id, run_id, session_id, step_n, start_time, end_time, "
                " duration_ms, attributes_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    span.span_id,
                    span.trace_id,
                    span.parent_span_id,
                    span.span_type.value,
                    span.name,
                    span.status.value,
                    span.agent_id or None,
                    span.run_id or None,
                    span.session_id or "",
                    span.step_n,
                    _dt_to_iso(span.start_time),
                    _dt_to_iso(span.end_time),
                    span.duration_ms,
                    attrs_json,
                ),
            )

    def get_spans(self, run_id: str | None = None) -> list[Span]:
        with self._lock:
            if run_id:
                rows = self._conn.execute(
                    "SELECT * FROM spans WHERE run_id = ? ORDER BY start_time ASC, id ASC",
                    (run_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM spans ORDER BY start_time ASC, id ASC"
                ).fetchall()
        return [s for r in rows if (s := self._row_to_span(r)) is not None]

    def query_spans(self, run_id: str) -> list[Span]:
        """TracerBackend Protocol 要求的查询接口。"""
        return self.get_spans(run_id)

    @staticmethod
    def _row_to_span(row: sqlite3.Row) -> Span | None:
        span_type = _safe_enum(SpanType, row["span_type"], fallback=None, label="SpanType")
        if span_type is None:
            return None  # 未知 span 类型无法归类，跳过该行（数据仍在库中）
        attrs: dict[str, Any] = {}
        if row["attributes_json"]:
            try:
                attrs = json.loads(row["attributes_json"])
            except (TypeError, ValueError):
                attrs = {}
        status = _safe_enum(SpanStatus, row["status"], fallback=SpanStatus.OK, label="SpanStatus") or SpanStatus.OK
        return Span(
            span_id=row["span_id"],
            trace_id=row["trace_id"] or "",
            parent_span_id=row["parent_span_id"],
            span_type=span_type,
            name=row["name"] or "",
            agent_id=row["agent_id"] or "",
            run_id=row["run_id"] or "",
            session_id=row["session_id"] or "",
            step_n=row["step_n"],
            start_time=_iso_to_dt(row["start_time"]) or datetime.now(timezone.utc),
            end_time=_iso_to_dt(row["end_time"]),
            duration_ms=row["duration_ms"],
            status=status,
            attributes=attrs,
        )

    def __repr__(self) -> str:
        target = self._db_path if self._db_path else "shared-connection"
        return f"SQLiteTracerBackend(db='{target}')"


# ════════════════════════════════════════════════════════════════════════
#  Metrics（事件流：每个数据点 append 一行，get_summary 实时聚合）
# ════════════════════════════════════════════════════════════════════════

class SQLiteMetricsBackend(_SQLiteBackendBase):
    """SQLite Metrics 后端（实现 MetricsBackend Protocol）。

    事件流模型：counter/histogram/gauge 每次记录都 append 一行，保留时间维度。
    ``get_summary()`` 用 SQL 聚合，输出与 InMemory/Markdown 后端一致的 dict 结构：
      counters → SUM(value)；histograms → count/min/max/avg/sum；gauges → 最新值。
    key 采用 ``name{k=v,...}`` 格式（与 _make_key 一致），供看板/断言复用。
    """

    _SCHEMA = _SCHEMA_METRICS

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        wal_mode: bool = True,
    ) -> None:
        super().__init__(db_path, connection=connection, wal_mode=wal_mode)
        self._init_schema()

    @staticmethod
    def _make_key(name: str, labels: dict[str, str]) -> str:
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    def _record(self, name: str, kind: str, value: float, labels: dict[str, str]) -> None:
        labels = labels or {}
        labels_json = json.dumps(labels, ensure_ascii=False, sort_keys=True) if labels else None
        session_id = labels.get("session_id") or None
        run_id = labels.get("run_id") or None
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO metrics_points "
                "(name, kind, value, labels_json, session_id, run_id, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, kind, float(value), labels_json, session_id, run_id, _now_iso()),
            )

    def record_counter(self, name: str, value: int, labels: dict[str, str]) -> None:
        self._record(name, "counter", value, labels)

    def record_histogram(self, name: str, value: float, labels: dict[str, str]) -> None:
        self._record(name, "histogram", value, labels)

    def record_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        self._record(name, "gauge", value, labels)

    # ── 聚合读 ──

    @staticmethod
    def _key_from(name: str, labels_json: str | None) -> str:
        labels: dict[str, str] = {}
        if labels_json:
            try:
                labels = json.loads(labels_json)
            except (TypeError, ValueError):
                labels = {}
        return SQLiteMetricsBackend._make_key(name, labels)

    def get_counter(self, name: str, labels: dict[str, str] | None = None) -> int:
        labels_json = (
            json.dumps(labels, ensure_ascii=False, sort_keys=True) if labels else None
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(value), 0) AS s FROM metrics_points "
                "WHERE name = ? AND kind = 'counter' "
                "AND ((labels_json IS NULL AND ? IS NULL) OR labels_json = ?)",
                (name, labels_json, labels_json),
            ).fetchone()
        return int(row["s"])

    def get_gauge(self, name: str, labels: dict[str, str] | None = None) -> float:
        labels_json = (
            json.dumps(labels, ensure_ascii=False, sort_keys=True) if labels else None
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM metrics_points "
                "WHERE name = ? AND kind = 'gauge' "
                "AND ((labels_json IS NULL AND ? IS NULL) OR labels_json = ?) "
                "ORDER BY id DESC LIMIT 1",
                (name, labels_json, labels_json),
            ).fetchone()
        return float(row["value"]) if row else 0.0

    def get_histogram(self, name: str, labels: dict[str, str] | None = None) -> list[float]:
        labels_json = (
            json.dumps(labels, ensure_ascii=False, sort_keys=True) if labels else None
        )
        with self._lock:
            rows = self._conn.execute(
                "SELECT value FROM metrics_points "
                "WHERE name = ? AND kind = 'histogram' "
                "AND ((labels_json IS NULL AND ? IS NULL) OR labels_json = ?) "
                "ORDER BY id ASC",
                (name, labels_json, labels_json),
            ).fetchall()
        return [float(r["value"]) for r in rows]

    def get_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"counters": {}, "gauges": {}, "histograms": {}}
        with self._lock:
            # counters：按 (name, labels) SUM
            for r in self._conn.execute(
                "SELECT name, labels_json, SUM(value) AS s FROM metrics_points "
                "WHERE kind = 'counter' GROUP BY name, labels_json"
            ).fetchall():
                summary["counters"][self._key_from(r["name"], r["labels_json"])] = int(r["s"])

            # gauges：取每 (name, labels) 的最新值（MAX(id)）
            for r in self._conn.execute(
                "SELECT m.name, m.labels_json, m.value FROM metrics_points m "
                "JOIN (SELECT name, labels_json, MAX(id) AS mid FROM metrics_points "
                "      WHERE kind = 'gauge' GROUP BY name, labels_json) g "
                "  ON m.id = g.mid"
            ).fetchall():
                summary["gauges"][self._key_from(r["name"], r["labels_json"])] = float(r["value"])

            # histograms：聚合统计
            for r in self._conn.execute(
                "SELECT name, labels_json, COUNT(*) AS c, MIN(value) AS mn, "
                "       MAX(value) AS mx, AVG(value) AS av, SUM(value) AS sm "
                "FROM metrics_points WHERE kind = 'histogram' "
                "GROUP BY name, labels_json"
            ).fetchall():
                summary["histograms"][self._key_from(r["name"], r["labels_json"])] = {
                    "count": int(r["c"]),
                    "min": float(r["mn"]),
                    "max": float(r["mx"]),
                    "avg": float(r["av"]),
                    "sum": float(r["sm"]),
                }
        return summary

    def flush(self) -> None:
        pass

    def __repr__(self) -> str:
        target = self._db_path if self._db_path else "shared-connection"
        return f"SQLiteMetricsBackend(db='{target}', mode='event-stream')"


# ════════════════════════════════════════════════════════════════════════
#  Logger
# ════════════════════════════════════════════════════════════════════════

class SQLiteLoggerBackend(_SQLiteBackendBase):
    """SQLite Logger 后端（实现 LoggerBackend Protocol）。

    结构化日志：已知字段落列，其余键塞进 extra_json。
    """

    _SCHEMA = _SCHEMA_LOGS

    _KNOWN_FIELDS = frozenset(
        {"level", "timestamp", "module", "message", "run_id", "step_n",
         "agent_id", "log_id", "session_id"}
    )

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        wal_mode: bool = True,
    ) -> None:
        super().__init__(db_path, connection=connection, wal_mode=wal_mode)
        self._init_schema()
        self._migrate_logs_table()

    def _migrate_logs_table(self) -> None:
        """轻量 schema 迁移：老库补 log_id 列（幂等，ALTER TABLE ADD COLUMN）。

        log_id 由 Logger 生成（日志唯一标识），此前被静默丢弃——排除列表把它滤进
        extra_json，读侧 _row_to_record 也从 extra 里取回，但 write 从未落列。
        """
        with self._lock, self._conn:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(logs)")}
            if "log_id" not in cols:
                self._conn.execute("ALTER TABLE logs ADD COLUMN log_id TEXT")

    def write_log(self, record: dict) -> None:
        ts = record.get("timestamp", "")
        if isinstance(ts, datetime):
            ts = ts.isoformat()
        elif not ts:
            ts = _now_iso()
        else:
            ts = str(ts)

        extra = {k: v for k, v in record.items() if k not in self._KNOWN_FIELDS}
        extra_json = (
            json.dumps(extra, ensure_ascii=False, default=str) if extra else None
        )
        step_n = record.get("step_n")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO logs "
                "(ts, level, module, message, agent_id, run_id, session_id, step_n, log_id, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    record.get("level", "INFO"),
                    record.get("module") or None,
                    record.get("message", ""),
                    record.get("agent_id") or None,
                    record.get("run_id") or None,
                    record.get("session_id") or "",
                    int(step_n) if step_n is not None else None,
                    record.get("log_id") or None,
                    extra_json,
                ),
            )

    def get_records(self, session_id: str | None = None, limit: int = 1000) -> list[dict]:
        """离线读：返回结构化日志（时间升序），供看板/分析。"""
        with self._lock:
            if session_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM logs WHERE session_id = ? ORDER BY ts ASC, id ASC LIMIT ?",
                    (session_id, int(limit)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM logs ORDER BY ts ASC, id ASC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> dict:
        record: dict[str, Any] = {
            "timestamp": row["ts"],
            "level": row["level"],
            "module": row["module"],
            "message": row["message"],
            "agent_id": row["agent_id"],
            "run_id": row["run_id"],
            "session_id": row["session_id"],
            "step_n": row["step_n"],
            "log_id": row["log_id"],
        }
        if row["extra_json"]:
            try:
                record.update(json.loads(row["extra_json"]))
            except (TypeError, ValueError):
                pass
        return record

    def __repr__(self) -> str:
        target = self._db_path if self._db_path else "shared-connection"
        return f"SQLiteLoggerBackend(db='{target}')"
