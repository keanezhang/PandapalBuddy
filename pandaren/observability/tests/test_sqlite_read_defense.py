"""SQLite 读侧防御改动测试（sqlite_read_defense 设计文档落地）。

对应设计文档：tests/design/sqlite_read_defense.design.md
覆盖：
  - `_safe_enum` 三分支（B1 空值静默 / B2 已知值 / B3 未知值降级留痕）
  - `_row_to_record` / `_row_to_span` 防御转换（跳过、降级、坏 JSON / 坏 ts 兜底）
  - `query` / `get_spans` 坏数据跳过与降级留痕、排序与过滤共存、数据仍在库
  - audit / spans 复合索引存在性
  - console / markdown 渲染格式回归（KG-1/KG-2 已修复，全部正常断言）

已知差距（KG）处理（源码已修复，不落 xfail）：
  - KG-1：severity 降级目标为 `AuditSeverity.WARN`（修复方向②确认；
    设计文档 §9 的 MEDIUM 是无效值——AuditSeverity 无该成员），用例 5-10 正常断言。
  - KG-2：spans 已补 `idx_spans_type_start ON spans(span_type, start_time)`，
    用例 16 正常断言（按文档要求以列组合为主）。
  - KG-3：audit 索引无 DESC 声明，SQLite 反向扫描无行为差异，用例 15 正常断言。

零 mock；坏数据统一手工 INSERT（write/export_span 只写合法枚举）；
warning 用 caplog（logger="pandaren.observability.backend.sqlite"）；
console 用 capsys（stderr 子串断言）；markdown 用 tmp_path 真文件。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

import pytest

from pandaren.observability.backend.console import ConsoleTracerBackend
from pandaren.observability.backend.markdown import MarkdownTracerBackend
from pandaren.observability.backend.sqlite import (
    SQLiteAuditBackend,
    SQLiteTracerBackend,
    _safe_enum,
)
from pandaren.observability.types import (
    AuditEventType,
    AuditRecord,
    AuditSeverity,
    Span,
    SpanStatus,
    SpanType,
    generate_id,
)

SQLITE_LOGGER = "pandaren.observability.backend.sqlite"


# ─── 基座 helpers（对齐 tests/test_sqlite_backend.py 的 _mk_audit/_mk_span）───

def _mk_audit(*, session_id="", detail="", event_type=AuditEventType.RUN_STARTED,
              agent_id="pandapal", severity=AuditSeverity.INFO, run_id="run-test",
              ts=None, step_n=None, tool_name=None, terminal_reason=None):
    return AuditRecord(
        timestamp=ts or datetime.now(timezone.utc),
        record_id=generate_id(),
        event_type=event_type,
        severity=severity,
        agent_id=agent_id,
        run_id=run_id,
        session_id=session_id,
        detail=detail,
        step_n=step_n,
        tool_name=tool_name,
        terminal_reason=terminal_reason,
    )


def _mk_span(*, run_id="run-test", session_id="", name="span",
             span_type=SpanType.RUN, step_n=None, attributes=None,
             status=SpanStatus.OK, start_time=None, end_time=None,
             duration_ms=1.5):
    start = start_time or datetime.now(timezone.utc)
    return Span(
        span_id=generate_id(),
        trace_id=run_id,
        parent_span_id=None,
        span_type=span_type,
        name=name,
        agent_id="pandapal",
        run_id=run_id,
        session_id=session_id,
        step_n=step_n,
        start_time=start,
        end_time=end_time if end_time is not None else start,
        duration_ms=duration_ms,
        status=status,
        attributes=attributes or {},
    )


# ─── Row 夹具（内存连接构造 sqlite3.Row，镜像后端 schema）──────────────────

def _make_audit_row(**fields) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE audit_records ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " record_id TEXT NOT NULL, ts TEXT NOT NULL, event_type TEXT NOT NULL,"
        " severity TEXT NOT NULL, agent_id TEXT, run_id TEXT, session_id TEXT,"
        " step_n INTEGER, tool_name TEXT, terminal_reason TEXT, detail TEXT)"
    )
    defaults = dict(
        record_id="rec-x", ts="2026-01-01T12:00:00+00:00",
        event_type="run_started", severity="info", agent_id="pandapal",
        run_id="run-1", session_id="s1", step_n=None, tool_name=None,
        terminal_reason=None, detail="",
    )
    defaults.update(fields)
    cols = ", ".join(defaults)
    placeholders = ", ".join("?" for _ in defaults)
    conn.execute(
        f"INSERT INTO audit_records ({cols}) VALUES ({placeholders})",
        tuple(defaults.values()),
    )
    return conn.execute("SELECT * FROM audit_records").fetchone()


def _make_span_row(**fields) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE spans ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " span_id TEXT NOT NULL, trace_id TEXT, parent_span_id TEXT,"
        " span_type TEXT NOT NULL, name TEXT, status TEXT, agent_id TEXT,"
        " run_id TEXT, session_id TEXT, step_n INTEGER, start_time TEXT,"
        " end_time TEXT, duration_ms REAL, attributes_json TEXT)"
    )
    defaults = dict(
        span_id="sp-x", trace_id="t", parent_span_id=None, span_type="run",
        name="span", status="ok", agent_id="pandapal", run_id="run-1",
        session_id="s1", step_n=None, start_time="2026-01-01T12:00:00+00:00",
        end_time="2026-01-01T12:00:00+00:00", duration_ms=1.5,
        attributes_json=None,
    )
    defaults.update(fields)
    cols = ", ".join(defaults)
    placeholders = ", ".join("?" for _ in defaults)
    conn.execute(
        f"INSERT INTO spans ({cols}) VALUES ({placeholders})",
        tuple(defaults.values()),
    )
    return conn.execute("SELECT * FROM spans").fetchone()


# ─── 坏数据手工 INSERT（绕过 write/export_span，write 只能写合法枚举）────────

def _insert_audit_row(be, *, record_id, ts, event_type, severity,
                      agent_id="pandapal", run_id="run-1", session_id="s1",
                      step_n=1, tool_name=None, terminal_reason=None, detail=""):
    with be._conn:
        be._conn.execute(
            "INSERT INTO audit_records "
            "(record_id, ts, event_type, severity, agent_id, run_id, session_id, "
            " step_n, tool_name, terminal_reason, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, ts, event_type, severity, agent_id, run_id, session_id,
             step_n, tool_name, terminal_reason, detail),
        )


def _insert_span_row(be, *, span_id, span_type, run_id="run1", session_id="",
                     status="ok", start_time=None, end_time=None,
                     attributes_json=None, name="span", step_n=None):
    with be._conn:
        be._conn.execute(
            "INSERT INTO spans "
            "(span_id, trace_id, parent_span_id, span_type, name, status, "
            " agent_id, run_id, session_id, step_n, start_time, end_time, "
            " duration_ms, attributes_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (span_id, run_id, None, span_type, name, status,
             "pandapal", run_id, session_id, step_n, start_time, end_time,
             1.5, attributes_json),
        )


def _warning_messages(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == SQLITE_LOGGER and r.levelno >= logging.WARNING
    ]


# ─── 用例 1-4：`_safe_enum` ──────────────────────────────────────────────

# inv-5 确定性 + R1 已知路径不误伤（branch B2）
@pytest.mark.parametrize("enum_cls, value, expected", [
    (AuditSeverity, "warn", AuditSeverity.WARN),
    (SpanType, "llm_call", SpanType.LLM_CALL),
])
def test_safe_enum_known_value_returns_member(enum_cls, value, expected, caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    got = _safe_enum(enum_cls, value, fallback=None, label=enum_cls.__name__)
    assert got is expected
    assert _warning_messages(caplog) == []


# inv-5 + R1 未知值不抛 + R7 warning 可观测（branch B3，含 label 回落）
def test_safe_enum_unknown_value_fallback_and_warning(caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    got1 = _safe_enum(SpanStatus, "v99_alien", fallback=SpanStatus.OK, label="SpanStatus")
    got2 = _safe_enum(SpanStatus, "v99_alien", fallback=SpanStatus.OK)  # label 缺省
    assert got1 is SpanStatus.OK
    assert got2 is SpanStatus.OK

    msgs = _warning_messages(caplog)
    assert len(msgs) == 2
    for m in msgs:
        assert "unknown SpanStatus value 'v99_alien'" in m
        # 显式 label 与缺省回落（label or enum_cls.__name__）一致
        assert "label=SpanStatus" in m


# inv-1 不抛 + inv-5 + R5 空值路径静默回落（branch B1；"" / None 为代表值，"ok" 为边界对照）
@pytest.mark.parametrize("value", ["", None, "ok"])
def test_safe_enum_empty_or_none_silent_fallback(value, caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    assert _safe_enum(SpanStatus, value, fallback=SpanStatus.OK, label="SpanStatus") is SpanStatus.OK
    # B1 静默回落是设计意图（空值常见，不刷日志），不是漏测
    assert not [m for m in _warning_messages(caplog) if "unknown SpanStatus" in m]


# inv-5 + R1：fallback=None 的 skip 语义（branch B3 + fallback=None 组合）
def test_safe_enum_fallback_none_returns_none(caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    assert _safe_enum(SpanType, "v99_alien", fallback=None, label="SpanType") is None
    msgs = _warning_messages(caplog)
    assert any("unknown SpanType value 'v99_alien'" in m for m in msgs)


# ─── 用例 5-10：`_row_to_record` / `query` ───────────────────────────────

# inv-4 往返一致 + R4 不误伤（branch D1+D2 已知值、D4 正常 ts）
def test_row_to_record_known_row_fields(caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    row = _make_audit_row(
        event_type="run_started", severity="warn", ts="2026-01-01T12:00:00+00:00",
        record_id="rec-1", agent_id="pandapal", run_id="run-1", session_id="s1",
        step_n=2, tool_name="calc", terminal_reason="none", detail="hello 你好",
    )
    rec = SQLiteAuditBackend._row_to_record(row)
    assert rec is not None
    assert rec.event_type is AuditEventType.RUN_STARTED
    assert rec.severity is AuditSeverity.WARN
    assert rec.timestamp == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert rec.record_id == "rec-1"
    assert rec.agent_id == "pandapal"
    assert rec.run_id == "run-1"
    assert rec.session_id == "s1"
    assert rec.step_n == 2
    assert rec.tool_name == "calc"
    assert rec.terminal_reason == "none"
    assert rec.detail == "hello 你好"
    assert _warning_messages(caplog) == []


# inv-1 + inv-2 跳过语义 + inv-6 + R1 + R7（branch D1→D3）
def test_row_to_record_unknown_event_type_skipped(caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    row = _make_audit_row(event_type="v99_event", severity="info")
    assert SQLiteAuditBackend._row_to_record(row) is None
    msgs = _warning_messages(caplog)
    assert any("unknown AuditEventType value 'v99_event'" in m for m in msgs)
    assert any("label=AuditEventType" in m for m in msgs)
    # severity 合法（"info"），不产生降级 warning——锁定"跳过优先级高于降级"
    assert not [m for m in msgs if "unknown AuditSeverity" in m]


# inv-1 + inv-3 降级留痕 + R2 + R7 + 坏 ts 蜕变（branch D2 未知值、D4 坏 ts）
def test_row_to_record_unknown_severity_degrades_and_bad_ts_fallback(caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    row = _make_audit_row(event_type="run_started", severity="bogus", ts="not-a-date")
    rec = SQLiteAuditBackend._row_to_record(row)
    assert rec is not None  # 审计行 HC4 强制保留，不丢行
    assert rec.severity is AuditSeverity.WARN  # 降级目标（KG-1 修复方向②，非 MEDIUM）
    assert rec.event_type is AuditEventType.RUN_STARTED  # 未知 severity 不影响已知字段
    assert isinstance(rec.timestamp, datetime)  # 蜕变断言：兜底 now() 值不确定，禁硬编码
    msgs = _warning_messages(caplog)
    assert any("unknown AuditSeverity value 'bogus'" in m for m in msgs)


# inv-1 + inv-2 + inv-6 + R1 + R3 + R6（branch D3 全行跳过 + query 过滤 None）
def test_query_all_bad_rows_returns_empty_but_keeps_rows(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    be = SQLiteAuditBackend(db_path=str(tmp_path / "obs.db"))
    _insert_audit_row(be, record_id="bad-1", ts="2026-01-01T12:00:00+00:00",
                      event_type="v99", severity="info", detail="bad 1")
    _insert_audit_row(be, record_id="bad-2", ts="2026-01-01T12:00:01+00:00",
                      event_type="v99", severity="info", detail="bad 2")

    assert be.query() == []

    n = be._conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0]
    assert n == 2  # inv-2：读侧跳过 ≠ 删除，append-only 不被破坏

    msgs = _warning_messages(caplog)
    assert len([m for m in msgs if "unknown AuditEventType value 'v99'" in m]) == 2
    be.close()


# inv-2 + inv-3 + inv-6 + R2 + R6 + R7（branch D3 跳过 + D2 降级 + 排序不变）
def test_query_mixed_rows_filter_degrade_order(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    be = SQLiteAuditBackend(db_path=str(tmp_path / "obs.db"))
    _insert_audit_row(be, record_id="good", ts="2026-01-01T12:00:00+00:00",
                      event_type="run_started", severity="warn", detail="good")
    _insert_audit_row(be, record_id="bad-event", ts="2026-01-01T12:00:02+00:00",
                      event_type="v99", severity="info", detail="bad event")
    _insert_audit_row(be, record_id="degrade", ts="2026-01-01T12:00:01+00:00",
                      event_type="run_finished", severity="bogus", detail="degrade")

    got = be.query()  # 默认 ORDER BY ts DESC, id DESC
    assert len(got) == 2
    assert all(r is not None for r in got)
    # 坏行（t=12:00:02, v99）已跳过；剩余按 ts DESC：run_finished(12:00:01) → run_started(12:00:00)
    assert [r.event_type for r in got] == [AuditEventType.RUN_FINISHED, AuditEventType.RUN_STARTED]
    assert got[0].timestamp == datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc)
    assert got[1].timestamp == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert got[1].severity is AuditSeverity.WARN  # 好数据不受降级污染
    assert got[0].severity is AuditSeverity.WARN  # 坏 severity 降级（KG-1 修复方向②，非 MEDIUM）

    msgs = _warning_messages(caplog)
    assert len([m for m in msgs if "unknown AuditEventType value 'v99'" in m]) == 1
    assert len([m for m in msgs if "unknown AuditSeverity value 'bogus'" in m]) == 1
    be.close()


# inv-4 往返一致 + R4（3 组 (event_type × severity) 代表组合，roundtrip 对拍）
def test_query_roundtrip_preserves_fields(tmp_path):
    be = SQLiteAuditBackend(db_path=str(tmp_path / "obs.db"))
    specs = [
        (AuditEventType.RUN_STARTED, AuditSeverity.INFO),
        (AuditEventType.TOOL_EXECUTED, AuditSeverity.WARN),
        (AuditEventType.PERMISSION_DENIED, AuditSeverity.CRITICAL),
    ]
    written = []
    for i, (event_type, severity) in enumerate(specs):
        rec = _mk_audit(event_type=event_type, severity=severity, detail=f"d{i}",
                        session_id=f"s{i}", step_n=i, tool_name="calc",
                        terminal_reason="none")
        written.append(rec)
        be.write(rec)

    got = be.query()
    assert len(got) == 3
    assert all(r is not None for r in got)
    by_id = {r.record_id: r for r in got}
    for rec in written:
        out = by_id[rec.record_id]
        assert out.record_id == rec.record_id
        assert out.event_type is rec.event_type  # 枚举成员身份断言
        assert out.severity is rec.severity
        assert out.detail == rec.detail
        assert out.session_id == rec.session_id
        assert out.step_n == rec.step_n
        assert out.tool_name == rec.tool_name
        assert out.terminal_reason == rec.terminal_reason
    be.close()


# ─── 用例 11-14：`_row_to_span` / `get_spans` ────────────────────────────

# inv-1 + inv-2 + inv-6 + R1 + R7（branch S1→S2）
def test_row_to_span_unknown_span_type_skipped(caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    row = _make_span_row(span_type="v99_span", status="ok", attributes_json='{"a": 1}')
    assert SQLiteTracerBackend._row_to_span(row) is None
    msgs = _warning_messages(caplog)
    assert any("unknown SpanType value 'v99_span'" in m for m in msgs)
    assert any("label=SpanType" in m for m in msgs)


# inv-1 + inv-3 + R2 + R7 + S3 坏 JSON 兜底（branch S1 已知 + S3 + S4 未知 status）
def test_row_to_span_unknown_status_ok_fallback_and_bad_json(caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    row = _make_span_row(
        span_type="llm_call", status="v99_status", attributes_json="{broken json",
        start_time="2026-01-01T12:00:00+00:00", end_time="2026-01-01T12:00:00+00:00",
        duration_ms=1.5,
    )
    sp = SQLiteTracerBackend._row_to_span(row)
    assert sp is not None
    assert sp.span_type is SpanType.LLM_CALL
    assert sp.status is SpanStatus.OK  # 降级
    assert sp.attributes == {}  # 坏 JSON 兜底，不抛
    assert sp.start_time == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # 坏 JSON 不产生 warning（实现如此，非缺陷）——只应有 status 降级这一条
    msgs = _warning_messages(caplog)
    assert len(msgs) == 1
    assert "unknown SpanStatus value 'v99_status'" in msgs[0]


# inv-2 + inv-3 + inv-6 + R2 + R6（branch S2 跳过 + S4 降级 + ORDER BY start_time ASC）
def test_get_spans_mixed_filter_and_order(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    be = SQLiteTracerBackend(db_path=str(tmp_path / "obs.db"))
    _insert_span_row(be, span_id="llm-1", span_type="llm_call", run_id="run1",
                     status="error", start_time="2026-01-01T12:00:01+00:00")
    _insert_span_row(be, span_id="tool-1", span_type="tool_call", run_id="run1",
                     status="v99_status", start_time="2026-01-01T12:00:00+00:00")
    _insert_span_row(be, span_id="bad-1", span_type="v99_span", run_id="run1",
                     status="ok", start_time="2026-01-01T12:00:02+00:00")
    _insert_span_row(be, span_id="run-2", span_type="run", run_id="run2",
                     status="ok", start_time="2026-01-01T12:00:00+00:00")

    run1 = be.get_spans("run1")
    assert len(run1) == 2
    assert all(s is not None for s in run1)
    assert [s.span_type for s in run1] == [SpanType.TOOL_CALL, SpanType.LLM_CALL]  # start_time ASC
    assert run1[0].status is SpanStatus.OK  # v99_status 降级
    assert run1[1].status is SpanStatus.ERROR

    all_spans = be.get_spans()
    assert len(all_spans) == 3  # 坏 type 行不可见，run2 对照组保留
    assert all(s is not None for s in all_spans)

    msgs = _warning_messages(caplog)
    assert any("unknown SpanType value 'v99_span'" in m for m in msgs)
    assert any("unknown SpanStatus value 'v99_status'" in m for m in msgs)
    be.close()


# inv-1 + inv-2 + inv-3 + R3 + R5（branch S2 全跳过 + S4 空值路径：status 列可 NULL）
def test_get_spans_all_bad_and_null_status(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=SQLITE_LOGGER)
    be = SQLiteTracerBackend(db_path=str(tmp_path / "obs.db"))
    _insert_span_row(be, span_id="bad-1", span_type="v99_span", run_id="r",
                     status="ok", start_time="2026-01-01T12:00:00+00:00")
    _insert_span_row(be, span_id="bad-2", span_type="v99_span", run_id="r",
                     status="ok", start_time="2026-01-01T12:00:01+00:00")
    _insert_span_row(be, span_id="null-1", span_type="run", run_id="r",
                     status=None, start_time=None)

    got = be.get_spans()
    assert len(got) == 1  # 2 行坏 type 被跳过
    n = be._conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    assert n == 3  # inv-2：数据仍在库
    sp = got[0]
    assert sp.status is SpanStatus.OK  # NULL → B1 空值路径静默回落 OK
    assert isinstance(sp.start_time, datetime)  # 蜕变断言：S5 兜底 now()，禁硬编码

    msgs = _warning_messages(caplog)
    assert len([m for m in msgs if "unknown SpanType value 'v99_span'" in m]) == 2
    assert not [m for m in msgs if "unknown SpanStatus" in m]  # NULL 走 B1，非 B3
    be.close()


# ─── 用例 15-16：复合索引存在 ─────────────────────────────────────────────

# R8 索引存在（KG-3：无 DESC 声明，SQLite 反向扫描无行为差异，正常断言）
def test_audit_composite_index_event_type_ts(tmp_path):
    db = str(tmp_path / "obs.db")
    be = SQLiteAuditBackend(db_path=db)
    rows = be._conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='audit_records'"
    ).fetchall()
    by_name = {r["name"]: r["sql"] for r in rows}
    assert "idx_audit_event_ts" in by_name
    assert "event_type" in by_name["idx_audit_event_ts"]
    assert "ts" in by_name["idx_audit_event_ts"]

    # 附：同路径幂等重建（IF NOT EXISTS）不抛异常
    be2 = SQLiteAuditBackend(db_path=db)
    be2.close()
    be.close()


# R8 索引存在（KG-2 已修复：实际索引名 idx_spans_type_start；列组合为主）
def test_spans_composite_index_span_type_start_time(tmp_path):
    be = SQLiteTracerBackend(db_path=str(tmp_path / "obs.db"))
    rows = be._conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='spans'"
    ).fetchall()
    matching = [
        r for r in rows
        if r["sql"]
        and "span_type" in r["sql"]
        and "start_time" in r["sql"]
        and r["sql"].find("span_type") < r["sql"].find("start_time")
    ]
    assert matching, "spans 表缺少 (span_type, start_time) 复合索引"
    assert matching[0]["name"] == "idx_spans_type_start"  # 现状确认（文档命名推断已放宽）
    be.close()


# ─── 用例 17-18：console export_span 渲染 ────────────────────────────────

# R9 渲染格式：`[span_type.name.lower()]`（ANSI 前缀不参与精确匹配，用子串断言）
def test_console_export_span_renders_llm_call(capsys):
    span = Span(
        span_id="s1", trace_id="t", parent_span_id=None,
        span_type=SpanType.LLM_CALL, name="llm:gpt-4o",
        agent_id="a", run_id="run-1", session_id="", step_n=2,
        start_time=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc),
        duration_ms=1.5, status=SpanStatus.OK,
        attributes={"model": "gpt-4o"},
    )
    ConsoleTracerBackend().export_span(span)
    err = capsys.readouterr().err
    assert "[llm_call]" in err  # 改动点：LLM_CALL.name.lower()
    assert "llm:gpt-4o" in err
    assert "OK" in err
    assert "1.5ms" in err
    assert "step=2" in err
    assert "model=gpt-4o" in err


# R9：全 SpanType 渲染 name.lower() + name/value 口径守护（参数化 8 成员）
@pytest.mark.parametrize("member", list(SpanType))
def test_console_all_span_types_render_name_lower(capsys, member):
    span = Span(
        span_id="s", trace_id="t", parent_span_id=None,
        span_type=member, name="n", agent_id="a", run_id="r", session_id="",
        start_time=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc),
        duration_ms=1.5, status=SpanStatus.OK,
    )
    ConsoleTracerBackend().export_span(span)
    err = capsys.readouterr().err
    assert f"[{member.name.lower()}]" in err
    # 口径守护：console 用 name.lower()、markdown 按 value 渲染同一文本；
    # 若未来 name/value 分叉，此断言报警，阻止口径静默分裂
    assert member.name.lower() == member.value


# ─── 用例 19-20：markdown 渲染回归 ───────────────────────────────────────

# R10：表格格式回归（真实文件，tmp_path；固定时钟防 flaky；_headered 幂等）
def test_markdown_table_format_regression(tmp_path):
    be = MarkdownTracerBackend(tmp_path)
    end_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # 注：文档 Given 的 run_id="run1234abcd" 与 golden 单元格 `run1234` 自相矛盾
    # （实现取 run_id[:8]）；golden 数据行是权威，故 run_id 取 "run1234" 使 [:8] 恰为该单元格。
    span = _mk_span(session_id="s1", name="agent run", span_type=SpanType.RUN,
                    run_id="run1234", status=SpanStatus.OK, duration_ms=500,
                    start_time=end_time, end_time=end_time)
    be.export_span(span)

    text = (tmp_path / "sessions" / "s1" / "traces.md").read_text(encoding="utf-8")
    assert "| 时间 | 类型 | 名称 | 状态 | 结束原因 | 耗时(ms) | Step | Run | 属性 |" in text
    assert "| 12:00:00 | 🚀 run | `agent run` | ✅ ok |  | **500** |  | `run1234` |  |" in text

    # _headered 幂等：再 export 一次仍只有一行表头
    be.export_span(span)
    text2 = (tmp_path / "sessions" / "s1" / "traces.md").read_text(encoding="utf-8")
    assert text2.count("| 时间 | 类型 | 名称 | 状态 | 结束原因 | 耗时(ms) | Step | Run | 属性 |") == 1


# R10：status 三态渲染（参数化 3 组；session_id="" 走 _no_session/traces.md）
@pytest.mark.parametrize("status, expected_status_cell", [
    (SpanStatus.OK, "✅ ok"),
    (SpanStatus.CANCELLED, "⏸️ cancelled"),
    (SpanStatus.ERROR, "❌ error"),
])
def test_markdown_status_three_states(tmp_path, status, expected_status_cell):
    be = MarkdownTracerBackend(tmp_path)
    end_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    span = _mk_span(session_id="", span_type=SpanType.RUN, status=status,
                    start_time=end_time, end_time=end_time)
    be.export_span(span)

    text = (tmp_path / "_no_session" / "traces.md").read_text(encoding="utf-8")
    assert f"| {expected_status_cell} |" in text


# R10：span_type icon 渲染抽样（_SPAN_TYPE_ICON 按 value 查表）
@pytest.mark.parametrize("span_type, expected_icon_cell", [
    (SpanType.STEP, "📍 step"),
    (SpanType.LLM_CALL, "🤖 llm_call"),
])
def test_markdown_span_type_icon(tmp_path, span_type, expected_icon_cell):
    be = MarkdownTracerBackend(tmp_path)
    end_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    span = _mk_span(session_id="", span_type=span_type, status=SpanStatus.OK,
                    start_time=end_time, end_time=end_time)
    be.export_span(span)

    text = (tmp_path / "_no_session" / "traces.md").read_text(encoding="utf-8")
    assert f"| {expected_icon_cell} |" in text
