"""SQLite 可观测后端测试。

覆盖：
  - 构造契约：db_path XOR connection / 拒绝 :memory:
  - Audit：write→query roundtrip（frozen 对象 + 枚举反解一致）、过滤、时间窗、limit、持久化
  - Tracer：export→query_spans roundtrip、attributes 全量往返、按 run 关联
  - Metrics：事件流聚合（counter SUM / gauge 最新 / histogram 统计）与 InMemory 一致
  - Logger：write_log→get_records roundtrip、extra 字段进 extra_json
  - Session 隔离：同库不同 session 用 WHERE 过滤互不串
  - 共库 join：span 与 raw_message 共用一个 connection，可按 (run_id, step) join
  - 并发：多线程写同一后端无丢失
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from pandaren.observability.backend.in_memory import InMemoryMetricsBackend
from pandaren.observability.backend.sqlite import (
    SQLiteAuditBackend,
    SQLiteLoggerBackend,
    SQLiteMetricsBackend,
    SQLiteTracerBackend,
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


# ─── helpers ──────────────────────────────────────────────────────────

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
             status=SpanStatus.OK):
    now = datetime.now(timezone.utc)
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
        start_time=now,
        end_time=now,
        duration_ms=1.5,
        status=status,
        attributes=attributes or {},
    )


# ─── 构造契约 ─────────────────────────────────────────────────────────

def test_requires_exactly_one_of_path_or_connection(tmp_path):
    with pytest.raises(ValueError):
        SQLiteAuditBackend()  # 都不传
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    with pytest.raises(ValueError):
        SQLiteAuditBackend(db_path=str(tmp_path / "y.db"), connection=conn)  # 都传
    conn.close()


def test_rejects_memory_db(tmp_path):
    with pytest.raises(ValueError):
        SQLiteTracerBackend(db_path=":memory:")


# ─── Audit roundtrip ──────────────────────────────────────────────────

def test_audit_write_query_roundtrip(tmp_path):
    be = SQLiteAuditBackend(db_path=str(tmp_path / "obs.db"))
    rec = _mk_audit(session_id="s1", detail="hello 你好",
                    event_type=AuditEventType.TOOL_EXECUTED,
                    severity=AuditSeverity.WARN, step_n=3,
                    tool_name="calc", terminal_reason="none")
    be.write(rec)

    out = be.query()
    assert len(out) == 1
    got = out[0]
    # frozen 对象 + 枚举反解一致
    assert got.event_type is AuditEventType.TOOL_EXECUTED
    assert got.severity is AuditSeverity.WARN
    assert got.detail == "hello 你好"
    assert got.session_id == "s1"
    assert got.step_n == 3
    assert got.tool_name == "calc"
    assert got.terminal_reason == "none"
    assert got.record_id == rec.record_id
    be.close()


def test_audit_query_filters_and_limit(tmp_path):
    be = SQLiteAuditBackend(db_path=str(tmp_path / "obs.db"))
    be.write(_mk_audit(agent_id="a1", event_type=AuditEventType.RUN_STARTED, detail="d1"))
    be.write(_mk_audit(agent_id="a2", event_type=AuditEventType.RUN_FINISHED, detail="d2"))
    be.write(_mk_audit(agent_id="a1", event_type=AuditEventType.RUN_FINISHED, detail="d3"))

    assert {r.detail for r in be.query(agent_id="a1")} == {"d1", "d3"}
    assert {r.detail for r in be.query(event_type="run_finished")} == {"d2", "d3"}
    assert len(be.query(limit=2)) == 2
    be.close()


def test_audit_time_window(tmp_path):
    be = SQLiteAuditBackend(db_path=str(tmp_path / "obs.db"))
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    be.write(_mk_audit(detail="old", ts=base))
    be.write(_mk_audit(detail="mid", ts=base + timedelta(hours=1)))
    be.write(_mk_audit(detail="new", ts=base + timedelta(hours=2)))

    got = be.query(start_time=(base + timedelta(minutes=30)),
                   end_time=(base + timedelta(hours=1, minutes=30)))
    assert {r.detail for r in got} == {"mid"}
    be.close()


def test_audit_persists_across_reopen(tmp_path):
    db = str(tmp_path / "obs.db")
    be = SQLiteAuditBackend(db_path=db)
    be.write(_mk_audit(detail="persisted"))
    be.close()

    be2 = SQLiteAuditBackend(db_path=db)
    assert any(r.detail == "persisted" for r in be2.query())
    be2.close()


# ─── Tracer roundtrip ─────────────────────────────────────────────────

def test_tracer_roundtrip_with_full_attributes(tmp_path):
    be = SQLiteTracerBackend(db_path=str(tmp_path / "obs.db"))
    attrs = {"model": "claude-opus-4-8", "provider": "openai",
             "input_tokens": 100, "cache_hit_ratio": 0.42, "flag": True}
    be.export_span(_mk_span(run_id="r1", session_id="s1", name="llm",
                            span_type=SpanType.LLM_CALL, step_n=2, attributes=attrs))

    spans = be.query_spans("r1")
    assert len(spans) == 1
    sp = spans[0]
    assert sp.span_type is SpanType.LLM_CALL
    assert sp.status is SpanStatus.OK
    assert sp.step_n == 2
    # attributes 全量往返（不做白名单裁剪）
    assert sp.attributes["model"] == "claude-opus-4-8"
    assert sp.attributes["input_tokens"] == 100
    assert sp.attributes["cache_hit_ratio"] == 0.42
    assert sp.attributes["flag"] is True
    be.close()


def test_tracer_query_by_run(tmp_path):
    be = SQLiteTracerBackend(db_path=str(tmp_path / "obs.db"))
    be.export_span(_mk_span(run_id="r1", name="a"))
    be.export_span(_mk_span(run_id="r1", name="b"))
    be.export_span(_mk_span(run_id="r2", name="c"))
    assert len(be.query_spans("r1")) == 2
    assert len(be.query_spans("r2")) == 1
    assert len(be.get_spans()) == 3
    be.close()


# ─── Metrics 事件流聚合 ───────────────────────────────────────────────

def test_metrics_summary_matches_in_memory(tmp_path):
    sq = SQLiteMetricsBackend(db_path=str(tmp_path / "obs.db"))
    mem = InMemoryMetricsBackend()
    for be in (sq, mem):
        be.record_counter("calls", 1, {"tool": "calc"})
        be.record_counter("calls", 2, {"tool": "calc"})
        be.record_gauge("temp", 0.5, {})
        be.record_gauge("temp", 0.9, {})  # 最新值胜出
        be.record_histogram("latency", 10.0, {"m": "gpt"})
        be.record_histogram("latency", 30.0, {"m": "gpt"})

    s_sq = sq.get_summary()
    s_mem = mem.get_summary()
    assert s_sq["counters"] == s_mem["counters"]       # calls{tool=calc}=3
    assert s_sq["counters"]["calls{tool=calc}"] == 3
    assert s_sq["gauges"] == s_mem["gauges"]           # temp=0.9（最新）
    assert s_sq["gauges"]["temp"] == 0.9
    assert s_sq["histograms"] == s_mem["histograms"]   # count=2/min=10/max=30/avg=20/sum=40
    assert s_sq["histograms"]["latency{m=gpt}"]["sum"] == 40.0
    sq.close()


def test_metrics_getters(tmp_path):
    be = SQLiteMetricsBackend(db_path=str(tmp_path / "obs.db"))
    be.record_counter("c", 5, {})
    be.record_counter("c", 7, {})
    be.record_gauge("g", 1.0, {"k": "v"})
    be.record_histogram("h", 3.0, {})
    assert be.get_counter("c") == 12
    assert be.get_gauge("g", {"k": "v"}) == 1.0
    assert be.get_histogram("h") == [3.0]
    be.close()


def test_metrics_event_stream_keeps_rows(tmp_path):
    """事件流：每次 record 都落一行（不覆盖），可按时间维度回放。"""
    db = str(tmp_path / "obs.db")
    be = SQLiteMetricsBackend(db_path=db)
    for _ in range(5):
        be.record_histogram("lat", 1.0, {})
    be.close()
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM metrics_points WHERE name='lat'").fetchone()[0]
    conn.close()
    assert n == 5


# ─── Logger roundtrip ─────────────────────────────────────────────────

def test_logger_roundtrip_and_extra(tmp_path):
    be = SQLiteLoggerBackend(db_path=str(tmp_path / "obs.db"))
    be.write_log({
        "timestamp": datetime.now(timezone.utc),
        "level": "ERROR", "module": "engine", "message": "boom",
        "session_id": "s1", "run_id": "r1", "step_n": 4,
        "custom_field": "extra-value",  # 非已知字段 → extra_json
    })
    recs = be.get_records(session_id="s1")
    assert len(recs) == 1
    r = recs[0]
    assert r["level"] == "ERROR"
    assert r["message"] == "boom"
    assert r["step_n"] == 4
    assert r["custom_field"] == "extra-value"
    be.close()


# ─── Session 隔离 ─────────────────────────────────────────────────────

def test_session_isolation_via_where(tmp_path):
    db = str(tmp_path / "obs.db")
    audit = SQLiteAuditBackend(db_path=db)
    audit.write(_mk_audit(session_id="s1", detail="in-s1"))
    audit.write(_mk_audit(session_id="s2", detail="in-s2"))
    # query 无 session 过滤参数，但可用底层验证物理隔离
    conn = sqlite3.connect(db)
    s1 = conn.execute("SELECT detail FROM audit_records WHERE session_id='s1'").fetchall()
    s2 = conn.execute("SELECT detail FROM audit_records WHERE session_id='s2'").fetchall()
    conn.close()
    assert [r[0] for r in s1] == ["in-s1"]
    assert [r[0] for r in s2] == ["in-s2"]
    audit.close()


# ─── 共库 join：span ↔ raw_message ────────────────────────────────────

def test_shared_connection_join_span_and_raw_message(tmp_path):
    """trace 与 raw_log 共用一个 connection → 可按 (run_id, step) join。"""
    from pandaren.memory.backends.sqlite_raw_log import SQLiteRawLogBackend

    conn = sqlite3.connect(str(tmp_path / "obs.db"), check_same_thread=False)
    tracer = SQLiteTracerBackend(connection=conn)
    raw = SQLiteRawLogBackend(connection=conn)

    tracer.export_span(_mk_span(run_id="r1", session_id="s1",
                                name="tool_call", span_type=SpanType.TOOL_CALL, step_n=1))
    raw.append_raw_message({"role": "assistant", "content": "using tool"},
                           session_id="s1", run_id="r1", step=1)

    rows = conn.execute(
        "SELECT s.name, m.content FROM spans s "
        "JOIN raw_messages m ON s.run_id = m.run_id AND s.step_n = m.step "
        "WHERE s.run_id = 'r1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "tool_call"
    assert rows[0][1] == "using tool"

    # 外部传入的 connection 不被 backend.close() 关闭
    tracer.close()
    conn.execute("SELECT 1")  # 仍可用
    conn.close()


# ─── 并发写 ───────────────────────────────────────────────────────────

def test_concurrent_writes_no_loss(tmp_path):
    be = SQLiteAuditBackend(db_path=str(tmp_path / "obs.db"))

    def worker(sid: str) -> None:
        for i in range(20):
            be.write(_mk_audit(session_id=sid, detail=f"{sid}-{i}"))

    threads = [threading.Thread(target=worker, args=(f"s{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    all_recs = be.query(limit=1000)
    assert len(all_recs) == 100
    be.close()
