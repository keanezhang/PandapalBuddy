"""pandapal/dashboard/tests/test_sqlite_aggregator.py — SQLiteDashboardAggregator 单测。

核心立场：**写↔读对称**。用真实的 SQLite 观测后端（AuditLog/Tracer/Metrics/Logger）
和 raw_log 写入，再经聚合器读回，验证 storage_mode=sqlite 下看板能拿到完整快照——
正是「切 sqlite 后 dashboard 空白」那个 bug 的回归护栏。

同时校验 sqlite 源与 markdown 源装配口径一致：(run_id, step) join、费用精算、
tool_spans per-call 真时长、run finish_reason、system_prompt。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
)

from pandapal.dashboard.sqlite_aggregator import SQLiteDashboardAggregator

_RUN = "0ab7ca19aaaa4b0c8d1e2f3a4b5c6d7e"  # 完整 run_id（聚合内部按 [:8] 归一）
_RID = _RUN[:8]
_SID = "sess-0ff4d198c53f42019bd28d261b57e63f"
_T0 = datetime(2026, 7, 16, 22, 0, 0, tzinfo=timezone.utc)


def _ts(sec: int) -> datetime:
    return _T0 + timedelta(seconds=sec)


def _span(span_type, name, step, status=SpanStatus.OK, dur=100.0, attrs=None):
    return Span(
        span_id=f"{span_type.value}-{step}-{name}",
        trace_id="trace-1",
        parent_span_id=None,
        span_type=span_type,
        name=name,
        agent_id="pandapal",
        run_id=_RUN,
        session_id=_SID,
        step_n=step,
        start_time=_ts(step or 0),
        end_time=_ts((step or 0) + 1),
        duration_ms=dur,
        status=status,
        attributes=attrs or {},
    )


def _build_observability_db(path: Path) -> None:
    """用真实观测后端（共享连接）写 spans/metrics/audit/logs——真实写路径。"""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    tracer = SQLiteTracerBackend(connection=conn)
    metrics = SQLiteMetricsBackend(connection=conn)
    audit = SQLiteAuditBackend(connection=conn)
    logger = SQLiteLoggerBackend(connection=conn)

    # run span（结束原因在 attributes.terminal_reason，与 markdown 口径一致）
    tracer.export_span(_span(SpanType.RUN, "agent.run", None, dur=13538.0,
                             attrs={"terminal_reason": "completed"}))
    # 两个 step + 两个 llm_call + 两个 tool_call（step 1 与 2）
    for step, (i, o, cached, ratio, tcc, dur) in enumerate(
        [(19624, 51, 512, 2.6, 1, 4000.0), (20197, 71, 1024, 5.1, 1, 5000.0)], start=1
    ):
        tracer.export_span(_span(SpanType.STEP, "agent.step", step))
        tracer.export_span(_span(SpanType.LLM_CALL, "llm.call", step, dur=dur, attrs={
            "model": "qwen3.7-max", "provider": "dashscope",
            "input_tokens": i, "output_tokens": o,
            "cached_tokens": cached, "cache_hit_ratio": ratio, "tool_calls_count": tcc,
        }))
        tool = "search_skills" if step == 1 else "skill_weather"
        tracer.export_span(_span(SpanType.TOOL_CALL, f"tool.{tool}", step, dur=200.0 * step,
                                 attrs={"tool_name": tool}))

    # metrics（事件流）
    metrics.record_counter("llm_call_total", 2, {"agent_id": "pandapal"})
    metrics.record_counter("llm_input_tokens_total", 39821, {"agent_id": "pandapal"})
    metrics.record_counter("llm_output_tokens_total", 122, {"agent_id": "pandapal"})
    metrics.record_counter("step_total", 2, {"agent_id": "pandapal"})
    metrics.record_counter("run_total", 1, {"agent_id": "pandapal", "status": "started"})
    metrics.record_counter("run_total", 1, {"agent_id": "pandapal", "status": "success"})
    metrics.record_gauge("active_runs", 0.0, {"agent_id": "pandapal"})

    # audit run_finished（权威结束原因）
    audit.write(AuditRecord(
        timestamp=_ts(20), record_id="rec-1",
        event_type=AuditEventType.RUN_FINISHED, severity=AuditSeverity.INFO,
        agent_id="pandapal", run_id=_RUN, session_id=_SID,
        detail="Completed normally", step_n=2,
    ))
    # 一条含 messages 的 llm 日志（供 system_prompt 提取）
    logger.write_log({
        "level": "INFO", "module": "llm", "message": "llm request",
        "agent_id": "pandapal", "run_id": _RUN, "session_id": _SID, "step_n": 1,
        "messages": [{"role": "system", "content": "你是一位得力的办公助手"},
                     {"role": "user", "content": "深圳今天的天气怎么样"}],
    })
    conn.commit()
    conn.close()


def _build_pandapal_db(path: Path) -> None:
    """建 sessions/session_groups/raw_log 表并插入一个会话 + 6 轮 raw_log。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, user_id TEXT, device_id TEXT,
            last_active TEXT, created_at TEXT, title TEXT DEFAULT '',
            preview TEXT DEFAULT '', message_count INTEGER DEFAULT 0,
            is_empty INTEGER DEFAULT 1, is_favorite INTEGER DEFAULT 0,
            is_deleted INTEGER DEFAULT 0, updated_at TEXT DEFAULT '', group_id TEXT
        );
        CREATE TABLE session_groups (id TEXT PRIMARY KEY, user_id TEXT, name TEXT, created_at TEXT);
        CREATE TABLE raw_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, session_id TEXT,
            entry_type TEXT DEFAULT 'message', content_json TEXT, turn_index INTEGER,
            created_at TEXT, run_id TEXT, step INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (session_id, user_id, device_id, last_active, created_at, "
        "title, preview, message_count, is_empty, is_deleted, group_id) "
        "VALUES (?, 'u1', 'd1', ?, ?, ?, ?, 6, 0, 0, 'grp-1')",
        (_SID, _ts(20).isoformat(), _T0.isoformat(), "深圳今天的天气怎么样", "深圳今天的天气怎么样"),
    )
    conn.execute("INSERT INTO session_groups VALUES ('grp-1', 'u1', '生活', ?)", (_T0.isoformat(),))

    def _msg(role, content, tool_calls=None):
        m = {"role": role, "content": content}
        if tool_calls:
            m["tool_calls"] = tool_calls
        return json.dumps(m, ensure_ascii=False)

    tc = [{"function": {"name": "search_skills", "arguments": '{"q":"weather"}'}}]
    rows = [
        ("message", _msg("user", "深圳今天的天气怎么样"), 0, "", None),
        ("message", _msg("assistant", "", tc), 1, _RUN, 1),
        ("message", _msg("tool", "已加载技能 [weather]"), 2, _RUN, 1),
        ("message", _msg("assistant", "", tc), 3, _RUN, 2),
        ("message", _msg("tool", "查询时间: 2026-07-16"), 4, _RUN, 2),
        ("message", _msg("assistant", "深圳今天天气概况"), 5, _RUN, 2),
    ]
    for entry_type, cj, idx, run, step in rows:
        conn.execute(
            "INSERT INTO raw_log (user_id, session_id, entry_type, content_json, turn_index, "
            "created_at, run_id, step) VALUES ('u1', ?, ?, ?, ?, ?, ?, ?)",
            (_SID, entry_type, cj, idx, _ts(idx).isoformat(), run or None, step),
        )
    conn.commit()
    conn.close()


def _build(tmp_path: Path):
    _build_observability_db(tmp_path / "observability.db")
    _build_pandapal_db(tmp_path / "pandapal.db")
    return SQLiteDashboardAggregator(tmp_path / "pandapal.db").build()


# ── tests ────────────────────────────────────────────────────────────
def test_global_metrics(tmp_path: Path):
    g = _build(tmp_path).global_
    assert g.agent_id == "pandapal"
    assert g.llm_call_total == 2
    assert g.llm_input_tokens_total == 39821
    assert g.llm_output_tokens_total == 122
    assert g.step_total == 2
    assert g.run_total_started == 1
    assert g.run_total_success == 1
    assert g.active_runs == 0.0
    assert g.last_updated  # MAX(ts) 非空


def test_session_shape_and_join(tmp_path: Path):
    snap = _build(tmp_path)
    assert len(snap.sessions) == 1
    s = snap.sessions[0]
    assert s.id == _SID
    assert s.title == "深圳今天的天气怎么样"
    assert s.model == "qwen3.7-max"        # 单一 model 去重
    assert s.group_name == "生活"           # session_groups join
    assert s.llm_calls == 2
    assert s.step_count == 2
    assert len(s.turns) == 6
    # token 只统计已 join 的 assistant 轮（2 个 llm_call）
    assert s.input_tokens == 19624 + 20197
    assert s.output_tokens == 51 + 71
    assert s.cost > 0.0                     # cost_of_call 精算


def test_turn_llm_and_tool_join(tmp_path: Path):
    s = _build(tmp_path).sessions[0]
    by_turn = {t.turn: t for t in s.turns}
    # assistant 轮按 (run_id, step) 命中对应 llm_call
    assert by_turn[1].llm is not None and by_turn[1].llm.input_tokens == 19624
    assert by_turn[3].llm is not None and by_turn[3].llm.input_tokens == 20197
    assert by_turn[1].llm.cached_tokens == 512
    assert by_turn[1].llm.provider == "dashscope"
    # tool_spans 按 (run_id, step) 挂到 assistant 轮（真时长）
    assert [ts.name for ts in by_turn[1].tool_spans] == ["search_skills"]
    assert [ts.name for ts in by_turn[3].tool_spans] == ["skill_weather"]
    # user 轮无 llm
    assert by_turn[0].llm is None


def test_run_finish_reason(tmp_path: Path):
    s = _build(tmp_path).sessions[0]
    assert len(s.runs) == 1
    r = s.runs[0]
    assert r.id == _RID
    assert r.status == "ok"
    assert r.finish_reason == "正常完成"     # terminal_reason=completed → 友好中文标签
    assert r.duration_ms == 13538.0


def test_tool_stats(tmp_path: Path):
    s = _build(tmp_path).sessions[0]
    names = {t.name for t in s.tools}
    assert names == {"search_skills", "skill_weather"}


def test_system_prompt_from_logs(tmp_path: Path):
    s = _build(tmp_path).sessions[0]
    assert s.system_prompt == "你是一位得力的办公助手"


def test_missing_dbs_returns_empty(tmp_path: Path):
    # 两个库都不存在 → 空快照，不抛（O3）
    snap = SQLiteDashboardAggregator(tmp_path / "nope.db").build()
    assert snap.sessions == []
    assert snap.global_.agent_id == ""


def test_deleted_session_excluded(tmp_path: Path):
    _build_observability_db(tmp_path / "observability.db")
    _build_pandapal_db(tmp_path / "pandapal.db")
    conn = sqlite3.connect(str(tmp_path / "pandapal.db"))
    conn.execute("UPDATE sessions SET is_deleted = 1 WHERE session_id = ?", (_SID,))
    conn.commit()
    conn.close()
    snap = SQLiteDashboardAggregator(tmp_path / "pandapal.db").build()
    assert snap.sessions == []
