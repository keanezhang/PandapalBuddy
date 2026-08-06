"""数据隔离端到端测试（T9-T13）。

覆盖：
  T9  — 用户隔离：Alice / Bob 数据不互串
  T10 — Session 隔离：同用户下 S1 / S2 的观测/日志/记忆各自独立
  T11 — 观测溯源：从 sessions/{sid}/audit.md 能读到该 session 的完整生命周期
  T12 — 路径新旧共存：新 users/{uid}/ 布局不碰旧目录
  T13 — 并发写不冲突：5 session 并发触发大量 audit 写入，无错乱

不测跨用户越权（那是 OS 层文件权限的事情），只测"数据是否被物理分开"。
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pandaren.observability.audit import AuditLog
from pandaren.observability.backend.markdown import (
    MarkdownAuditBackend,
    MarkdownLoggerBackend,
    MarkdownTracerBackend,
)
from pandaren.observability.tracer import Tracer
from pandaren.observability.types import (
    AuditEventType, AuditSeverity, Span, SpanType, SpanStatus,
)


# ─── T9: 用户隔离 ─────────────────────────────────────────────────────


def test_t9_user_isolation_via_storage_manager():
    """Alice 与 Bob 使用同一 StorageManager 类型 + 相同 session_id，
    但落盘路径完全物理分离。"""
    from pandapal.storage.manager import StorageManager

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "pandapal_md"
            sm_a = StorageManager(storage_path=str(base), storage_mode="markdown", user_id="alice")
            sm_b = StorageManager(storage_path=str(base), storage_mode="markdown", user_id="bob")
            await sm_a.initialize_storage()
            await sm_b.initialize_storage()

            raw_a = sm_a.get_raw_log_backend()
            raw_b = sm_b.get_raw_log_backend()
            raw_a.append_raw_message({"role": "user", "content": "alice-1"}, session_id="s1")
            raw_b.append_raw_message({"role": "user", "content": "bob-1"}, session_id="s1")

            # 物理路径隔离
            alice_files = sorted((base / "users" / "alice").rglob("*.md"))
            bob_files = sorted((base / "users" / "bob").rglob("*.md"))
            assert len(alice_files) >= 1 and len(bob_files) >= 1

            alice_content = alice_files[0].read_text(encoding="utf-8")
            bob_content = bob_files[0].read_text(encoding="utf-8")
            assert "alice-1" in alice_content
            assert "bob-1" not in alice_content
            assert "alice-1" not in bob_content
            assert "bob-1" in bob_content

            await sm_a.shutdown_storage()
            await sm_b.shutdown_storage()

    asyncio.run(run())


# ─── T10: Session 隔离 ────────────────────────────────────────────────


def test_t10_session_isolation_audit_and_logs():
    """同用户下两个 session 的 audit / logs 各自独立文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        audit = MarkdownAuditBackend(tmp)
        logger_b = MarkdownLoggerBackend(tmp)

        audit.write(_mk_audit(session_id="s1", detail="run started for s1"))
        audit.write(_mk_audit(session_id="s2", detail="run started for s2"))
        logger_b.write_log({"timestamp": datetime.now(timezone.utc), "level": "INFO",
                            "message": "s1 log", "session_id": "s1"})
        logger_b.write_log({"timestamp": datetime.now(timezone.utc), "level": "INFO",
                            "message": "s2 log", "session_id": "s2"})

        s1_audit = (Path(tmp) / "sessions" / "s1" / "audit.md").read_text(encoding="utf-8")
        s2_audit = (Path(tmp) / "sessions" / "s2" / "audit.md").read_text(encoding="utf-8")
        s1_log = (Path(tmp) / "sessions" / "s1" / "logs.md").read_text(encoding="utf-8")
        s2_log = (Path(tmp) / "sessions" / "s2" / "logs.md").read_text(encoding="utf-8")

        assert "run started for s1" in s1_audit
        assert "run started for s2" not in s1_audit
        assert "run started for s2" in s2_audit
        assert "s1 log" in s1_log and "s2 log" not in s1_log
        assert "s2 log" in s2_log and "s1 log" not in s2_log


# ─── T11: 观测溯源 ────────────────────────────────────────────────────


def test_t11_audit_tracing_completes_a_session_lifecycle():
    """写入完整生命周期事件后，session 目录里能读到全部记录。"""
    with tempfile.TemporaryDirectory() as tmp:
        audit = MarkdownAuditBackend(tmp)
        # 一次典型的 run 生命周期
        events = [
            (AuditEventType.RUN_STARTED, "task: 写一首诗"),
            (AuditEventType.HITL_REQUESTED, "需审批工具: write_file"),
            (AuditEventType.HITL_APPROVED, "用户批准: write_file"),
            (AuditEventType.TOOL_EXECUTED, "write_file success"),
            (AuditEventType.RUN_FINISHED, "success"),
        ]
        for ev, detail in events:
            audit.write(_mk_audit(session_id="s-trace", detail=detail, event_type=ev))

        content = (Path(tmp) / "sessions" / "s-trace" / "audit.md").read_text(encoding="utf-8")
        for _, detail in events:
            assert detail in content, f"missing: {detail}"


# ─── T12: 无归属事件走 _no_session ─────────────────────────────────────


def test_t12_no_session_events_fall_to_global():
    """没有 session_id 的启动/关闭事件落到 _no_session 目录。"""
    with tempfile.TemporaryDirectory() as tmp:
        audit = MarkdownAuditBackend(tmp)
        audit.write(_mk_audit(session_id="", detail="startup event", event_type=AuditEventType.AGENT_REGISTERED))
        audit.write(_mk_audit(session_id="s1", detail="in-session event"))

        global_path = Path(tmp) / "_no_session" / "audit.md"
        s1_path = Path(tmp) / "sessions" / "s1" / "audit.md"
        assert global_path.exists()
        assert s1_path.exists()
        assert "startup event" in global_path.read_text(encoding="utf-8")
        assert "startup event" not in s1_path.read_text(encoding="utf-8")
        assert "in-session event" in s1_path.read_text(encoding="utf-8")
        assert "in-session event" not in global_path.read_text(encoding="utf-8")


# ─── T13: 并发写不冲突 ────────────────────────────────────────────────


def test_t13_concurrent_writes_no_corruption():
    """5 session × 20 条 audit 并发写入，per-file 锁保证无行错乱、无内容丢失。"""
    with tempfile.TemporaryDirectory() as tmp:
        audit = MarkdownAuditBackend(tmp)

        def worker(sid: str) -> None:
            for i in range(20):
                audit.write(_mk_audit(session_id=sid, detail=f"{sid}-msg-{i}"))

        threads = [threading.Thread(target=worker, args=(f"s{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 每个 session 应有 20 条数据行
        for i in range(5):
            content = (Path(tmp) / "sessions" / f"s{i}" / "audit.md").read_text(encoding="utf-8")
            # 数据行判定：table 行且不是 header/separator
            data_rows = [
                ln for ln in content.splitlines()
                if "|" in ln and not ln.startswith("|--") and not ln.startswith("| 时间")
            ]
            assert len(data_rows) == 20, f"s{i}: expected 20, got {len(data_rows)}"
            # 每一行都包含正确的 session prefix，且序号 0..19 全都在
            msgs = {f"s{i}-msg-{j}" for j in range(20)}
            for msg in msgs:
                assert msg in content, f"s{i} missing {msg}"


# ─── T14: session 路径清洗 ────────────────────────────────────────────


def test_t14_sanitize_prevents_path_escape():
    """恶意 session_id 无法逃出 base_dir。"""
    with tempfile.TemporaryDirectory() as tmp:
        audit = MarkdownAuditBackend(tmp)
        # 尝试路径逃逸：../../evil
        audit.write(_mk_audit(session_id="../../evil", detail="escape attempt"))

        # 断言 evil 目录没有在 tmp 之外被创建
        parent_evil = Path(tmp).parent / "evil"
        # tmp 是 tempdir，其 parent 是系统 tmp；确认那里没有 evil
        assert not parent_evil.exists() or not any(parent_evil.rglob("audit.md"))
        # 且 tmp 内应该有一个 sanitized 后的目录
        all_md = list(Path(tmp).rglob("audit.md"))
        assert len(all_md) == 1


# ─── T15: Tracer 后端按 session 分片 ──────────────────────────────────


def test_t15_tracer_backend_shards_by_session():
    """span.session_id 决定落盘分片：有归属 → sessions/{sid}/traces.md，
    无归属 → _no_session/traces.md。内存镜像仍可跨 session 按 run 查询。"""
    with tempfile.TemporaryDirectory() as tmp:
        tracer_b = MarkdownTracerBackend(tmp)
        tracer_b.export_span(_mk_span(run_id="r1", session_id="s1", name="run.s1"))
        tracer_b.export_span(_mk_span(run_id="r2", session_id="s2", name="run.s2"))
        tracer_b.export_span(_mk_span(run_id="r0", session_id="", name="run.global"))

        s1 = (Path(tmp) / "sessions" / "s1" / "traces.md").read_text(encoding="utf-8")
        s2 = (Path(tmp) / "sessions" / "s2" / "traces.md").read_text(encoding="utf-8")
        glob = (Path(tmp) / "_no_session" / "traces.md").read_text(encoding="utf-8")

        assert "run.s1" in s1 and "run.s2" not in s1
        assert "run.s2" in s2 and "run.s1" not in s2
        assert "run.global" in glob and "run.s1" not in glob
        # 根目录不再有平铺的 traces.md
        assert not (Path(tmp) / "traces.md").exists()
        # 内存镜像跨 session 保留，按 run_id 查询仍可用
        assert len(tracer_b.query_spans("r1")) == 1
        assert tracer_b.query_spans("r1")[0].session_id == "s1"


# ─── T16: Tracer facade 显式 session_id 端到端落到分片 ────────────────


def test_t16_tracer_facade_threads_session_id_to_shard():
    """start_span(session_id=...) → Span 携带 → end_span 导出到对应 session 分片。"""
    with tempfile.TemporaryDirectory() as tmp:
        tracer = Tracer(backend=MarkdownTracerBackend(tmp))
        span = tracer.start_span("agent.run", SpanType.RUN, run_id="r1", session_id="sess-A")
        assert span.session_id == "sess-A"
        tracer.end_span(span, status=SpanStatus.OK)

        shard = Path(tmp) / "sessions" / "sess-A" / "traces.md"
        assert shard.exists()
        assert "agent.run" in shard.read_text(encoding="utf-8")


# ─── T17: 无归属日志走 _no_session ────────────────────────────────────


def test_t17_session_less_logs_fall_to_no_session():
    """不带 session_id 的框架级日志落到 _no_session/logs.md，不污染任何 session。"""
    with tempfile.TemporaryDirectory() as tmp:
        logger_b = MarkdownLoggerBackend(tmp)
        logger_b.write_log({"timestamp": datetime.now(timezone.utc), "level": "INFO",
                            "message": "framework boot", "session_id": ""})
        logger_b.write_log({"timestamp": datetime.now(timezone.utc), "level": "INFO",
                            "message": "in session", "session_id": "s1"})

        glob = (Path(tmp) / "_no_session" / "logs.md").read_text(encoding="utf-8")
        s1 = (Path(tmp) / "sessions" / "s1" / "logs.md").read_text(encoding="utf-8")
        assert "framework boot" in glob and "in session" not in glob
        assert "in session" in s1 and "framework boot" not in s1


# ─── T18: 适配器端到端把 session_id 透传给 logger + tracer ─────────────


def test_t18_adapter_threads_session_id_to_logs_and_traces():
    """ObservabilityHooksAdapter 收到 session_id 后，logs.md 与 traces.md
    都按会话分片——验证 adapter→logger 与 adapter→tracer 两条链路都通。"""
    from pandaren.observability.hooks_adapter import ObservabilityHooksAdapter
    from pandaren.observability.logger import Logger
    from pandaren.observability.metrics import Metrics
    from pandaren.observability.backend.in_memory import InMemoryMetricsBackend

    with tempfile.TemporaryDirectory() as tmp:
        adapter = ObservabilityHooksAdapter(
            logger=Logger(backend=MarkdownLoggerBackend(tmp)),
            tracer=Tracer(backend=MarkdownTracerBackend(tmp)),
            metrics=Metrics(backend=InMemoryMetricsBackend()),
        )
        # 模拟引擎/执行器 fire（session_id 由 _safe_hook / context 注入）
        adapter.on_run_start("task-A", "r1", session_id="sess-A")
        adapter.on_step_start(1, "r1", session_id="sess-A")
        adapter.on_before_tool_call("calc", {"x": 1}, "r1", step_n=1, session_id="sess-A")
        adapter.on_after_tool_call("calc", "ok", "r1", step_n=1, session_id="sess-A")
        adapter.on_step_end(1, "r1", session_id="sess-A")
        adapter.on_run_end("r1", True, session_id="sess-A")

        logs = Path(tmp) / "sessions" / "sess-A" / "logs.md"
        traces = Path(tmp) / "sessions" / "sess-A" / "traces.md"
        assert logs.exists() and traces.exists()
        assert "Run 开始" in logs.read_text(encoding="utf-8")
        assert "agent.run" in traces.read_text(encoding="utf-8")
        # 没有任何东西掉进 _no_session
        assert not (Path(tmp) / "_no_session").exists()


# ─── T19: 引擎 _safe_hook 从 _current_session_id 注入 session_id ──────


def test_t19_safe_hook_injects_current_session_id():
    """run_core._safe_hook 把 self._current_session_id 作为 session_id 注入 hook；
    调用方已显式传 session_id 时不覆盖（setdefault 语义）。"""
    from pandaren.engine.run_core import RunCoreMixin

    calls: list[dict] = []

    class _RecordingHooks:
        def on_step_start(self, step_n, run_id, *, session_id=""):
            calls.append({"step_n": step_n, "run_id": run_id, "session_id": session_id})

    class _FakeLoop:
        _safe_hook = RunCoreMixin._safe_hook

        def __init__(self, hooks, sid):
            self._hooks = hooks
            self._current_session_id = sid

    loop = _FakeLoop(_RecordingHooks(), "sess-Z")
    loop._safe_hook("on_step_start", 3, "r1")
    assert calls[-1] == {"step_n": 3, "run_id": "r1", "session_id": "sess-Z"}
    # 显式传入优先，不被实例字段覆盖
    loop._safe_hook("on_step_start", 4, "r1", session_id="explicit")
    assert calls[-1]["session_id"] == "explicit"


# ─── helpers ──────────────────────────────────────────────────────────


def _mk_span(
    *,
    run_id: str = "run-test",
    session_id: str = "",
    name: str = "span",
    span_type: SpanType = SpanType.RUN,
):
    from pandaren.observability.types import generate_id
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
        step_n=None,
        start_time=now,
        end_time=now,
        duration_ms=1.0,
        status=SpanStatus.OK,
        attributes={},
    )


def _mk_audit(
    *,
    session_id: str = "",
    detail: str = "",
    event_type: AuditEventType = AuditEventType.RUN_STARTED,
):
    from pandaren.observability.types import AuditRecord, generate_id
    return AuditRecord(
        timestamp=datetime.now(timezone.utc),
        record_id=generate_id(),
        event_type=event_type,
        severity=AuditSeverity.INFO,
        agent_id="pandapal",
        run_id="run-test",
        session_id=session_id,
        detail=detail,
    )
