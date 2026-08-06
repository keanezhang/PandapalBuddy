"""
Pandaren Agent SDK · Observability 模块 Mock 测试

覆盖约束
--------
  HC4  AuditLog 不可关闭：
    - backend=None → ValueError
    - write_sync 后端抛异常 → AuditWriteError 传播
    - 写入失败时 fallback 输出到 stderr
  E4   Logger / Tracer / Metrics 故障时静默降级（不传播异常）
  三态 ObservabilityConfig: None / False / 实例
  不可变数据结构: AuditRecord / Span / ObservabilityContext 均 frozen=True
  DualAuditBackend: 双写行为
  InMemoryAuditBackend: 线程安全存储 + query 过滤
  InMemoryTracerBackend: span 存储 + 按 run_id 查询
  InMemoryMetricsBackend: counter / histogram / gauge + get_summary
  Tracer MINIMAL 模式: 非 RUN span 返回 noop (span_id="")
  Tracer._sanitize_attributes: sanitizer 异常 → {"_sanitize_error": True}
  ObservabilityProvider: 三态后端解析
  ObservabilityHooksAdapter: 11 个 hook 各自调用正确的 facade 方法

运行方式
--------
  cd /path/to/py_pandaren_agent_sdk
  python -m pytest pandaren/observability/tests/test_observability_mock.py -v
  # 或直接运行
  python pandaren/observability/tests/test_observability_mock.py
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import threading
import time
from dataclasses import dataclass, FrozenInstanceError
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from pandaren.observability.types import (
    LogLevel, TraceLevel, SpanType, SpanStatus,
    AuditEventType, AuditSeverity,
    ObservabilityContext, AuditRecord, Span,
    generate_id,
)
from pandaren.observability.exceptions import AuditWriteError
from pandaren.observability.audit import AuditLog, DualAuditBackend
from pandaren.observability.logger import Logger
from pandaren.observability.tracer import Tracer
from pandaren.observability.metrics import Metrics
from pandaren.observability.config import ObservabilityConfig
from pandaren.observability.provider import ObservabilityProvider
from pandaren.observability.hooks_adapter import ObservabilityHooksAdapter
from pandaren.observability.backend.in_memory import (
    InMemoryAuditBackend,
    InMemoryTracerBackend,
    InMemoryMetricsBackend,
)


# ════════════════════════════════════════════════════
#  测试框架（与项目其他测试文件保持一致）
# ════════════════════════════════════════════════════

class TestResult:
    """轻量测试结果收集器。"""
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def ok(self, name: str):
        self.passed += 1
        print(f"   ✅ {name}")

    def fail(self, name: str, detail: str = ""):
        self.failed += 1
        msg = f"   ❌ {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        self.errors.append(msg)

    def summary(self, section: str = ""):
        total = self.passed + self.failed
        label = f" [{section}]" if section else ""
        print(f"\n📊{label} 通过={self.passed} / 失败={self.failed} / 总计={total}")
        if self.errors:
            print("   失败列表:")
            for e in self.errors:
                print(f"     {e}")
        return self.failed == 0


result = TestResult()


def assert_true(condition: bool, name: str, detail: str = ""):
    if condition:
        result.ok(name)
    else:
        result.fail(name, detail or "条件为 False")


def assert_raises(exc_type, name: str, detail: str = ""):
    """装饰器：断言被装饰的函数会抛出指定异常。"""
    def decorator(fn):
        try:
            fn()
            result.fail(name, f"未抛出 {exc_type.__name__}" + (f": {detail}" if detail else ""))
        except exc_type:
            result.ok(name)
        except Exception as e:
            result.fail(name, f"抛出了 {type(e).__name__}({e}) 而非 {exc_type.__name__}")
    return decorator


def assert_no_raises(name: str, detail: str = ""):
    """装饰器：断言被装饰的函数不会抛出异常。"""
    def decorator(fn):
        try:
            fn()
            result.ok(name)
        except Exception as e:
            result.fail(name, f"意外抛出 {type(e).__name__}({e})" + (f": {detail}" if detail else ""))
    return decorator


# ════════════════════════════════════════════════════
#  辅助：快速构建测试用 AuditRecord 和 Span
# ════════════════════════════════════════════════════

def _make_audit_record(**kwargs) -> AuditRecord:
    defaults = dict(
        timestamp=datetime.now(timezone.utc),
        record_id=generate_id(),
        event_type=AuditEventType.RUN_STARTED,
        severity=AuditSeverity.INFO,
        agent_id="agent_test",
        run_id=generate_id(),
        detail="test detail",
    )
    defaults.update(kwargs)
    return AuditRecord(**defaults)


def _make_span(**kwargs) -> Span:
    defaults = dict(
        span_id=generate_id(),
        trace_id=generate_id(),
        parent_span_id=None,
        span_type=SpanType.RUN,
        name="test.span",
        agent_id="agent_test",
        run_id=generate_id(),
    )
    defaults.update(kwargs)
    return Span(**defaults)


# ════════════════════════════════════════════════════
#  1. types.py — 枚举与数据结构
# ════════════════════════════════════════════════════

def test_types():
    print("\n" + "═" * 60)
    print("1️⃣  types.py — 枚举与不可变数据结构")

    # LogLevel IntEnum 比较
    assert_true(LogLevel.DEBUG < LogLevel.INFO, "LogLevel: DEBUG < INFO")
    assert_true(LogLevel.WARN >= LogLevel.INFO, "LogLevel: WARN >= INFO")
    assert_true(LogLevel.ERROR > LogLevel.WARN, "LogLevel: ERROR > WARN")
    assert_true(LogLevel.DEBUG == 10, "LogLevel.DEBUG == 10")

    # generate_id 唯一性
    ids = {generate_id() for _ in range(100)}
    assert_true(len(ids) == 100, "generate_id 唯一性（100次无碰撞）")

    # SpanType 枚举完整性（8种）
    expected_span_types = {
        "run", "step", "llm_call", "tool_call",
        "guard_check", "hitl_check", "message_build", "skill_invoke"
    }
    actual_span_types = {s.value for s in SpanType}
    assert_true(actual_span_types == expected_span_types, "SpanType 8 种枚举值齐全")

    # AuditEventType 20 种
    assert_true(len(AuditEventType) == 21, f"AuditEventType 共 21 种（实际 {len(AuditEventType)}）")

    # ObservabilityContext frozen=True → 不可变
    ctx = ObservabilityContext(run_id="r1", agent_id="a1", trace_id="t1")
    @assert_raises(FrozenInstanceError, "ObservabilityContext frozen 不可变")
    def _():
        ctx.run_id = "mutated"  # type: ignore

    # AuditRecord frozen=True → 不可变
    rec = _make_audit_record()
    @assert_raises(FrozenInstanceError, "AuditRecord frozen 不可变")
    def _():
        rec.detail = "mutated"  # type: ignore

    # Span frozen=True → 不可变
    span = _make_span()
    @assert_raises(FrozenInstanceError, "Span frozen 不可变")
    def _():
        span.name = "mutated"  # type: ignore

    # AuditRecord 可选字段默认为 None
    rec2 = _make_audit_record()
    assert_true(rec2.step_n is None, "AuditRecord.step_n 默认 None")

    # Span 可选字段默认值
    sp = _make_span()
    assert_true(sp.end_time is None, "Span.end_time 默认 None")
    assert_true(sp.duration_ms is None, "Span.duration_ms 默认 None")
    assert_true(sp.status == SpanStatus.OK, "Span.status 默认 OK")


# ════════════════════════════════════════════════════
#  2. AuditLog — HC4 核心
# ════════════════════════════════════════════════════

def test_audit_log():
    print("\n" + "═" * 60)
    print("2️⃣  AuditLog — HC4 核心")

    # HC4: backend=None → ValueError
    @assert_raises(ValueError, "AuditLog(None) → ValueError（HC4）")
    def _():
        AuditLog(None)  # type: ignore

    # 正常写入
    backend = InMemoryAuditBackend()
    audit = AuditLog(backend=backend)

    @assert_no_raises("AuditLog.write_sync 正常写入不抛异常")
    def _():
        audit.write_sync(
            AuditEventType.RUN_STARTED,
            agent_id="agent_a",
            run_id="run_001",
            detail="run started",
        )

    # 写入后可查询
    records = audit.query_records(agent_id="agent_a")
    assert_true(len(records) == 1, "write_sync 后 query_records 可查到记录")
    assert_true(records[0].event_type == AuditEventType.RUN_STARTED, "查到的记录类型正确")
    assert_true(records[0].agent_id == "agent_a", "查到的 agent_id 正确")

    # HC4: 后端 write 抛异常 → AuditWriteError 传播
    class _FailBackend:
        def write(self, record): raise RuntimeError("disk full")
        def flush(self): pass
        def query(self, **kwargs): return []

    fail_audit = AuditLog(backend=_FailBackend())

    @assert_raises(AuditWriteError, "后端写入失败 → AuditWriteError 传播（HC4）")
    def _():
        fail_audit.write_sync(
            AuditEventType.AGENT_TERMINATED,
            agent_id="agent_b",
            run_id="run_002",
            detail="terminated",
        )

    # HC4: AuditWriteError 不是 ObservabilityError（独立基类，防止被意外吞掉）
    from pandaren.observability.exceptions import ObservabilityError
    assert_true(
        not issubclass(AuditWriteError, ObservabilityError),
        "AuditWriteError 不继承 ObservabilityError（防止被意外吞掉）"
    )

    # 写入失败时 fallback 输出到 stderr
    captured = io.StringIO()
    with patch("sys.stderr", captured):
        try:
            fail_audit.write_sync(
                AuditEventType.RUN_FINISHED,
                agent_id="agent_c",
                run_id="run_003",
                detail="fallback test",
            )
        except AuditWriteError:
            pass
    stderr_output = captured.getvalue()
    assert_true("AUDIT_FALLBACK" in stderr_output, "写入失败时 fallback 输出到 stderr")

    # 显式设置 severity
    backend2 = InMemoryAuditBackend()
    audit2 = AuditLog(backend=backend2)
    audit2.write_sync(
        AuditEventType.HITL_REQUESTED,
        agent_id="agent_d",
        run_id="run_004",
        detail="high risk",
        severity=AuditSeverity.CRITICAL,
    )
    recs = audit2.query_records(agent_id="agent_d")
    assert_true(recs[0].severity == AuditSeverity.CRITICAL, "显式 severity=CRITICAL 写入正确")

    # 默认 severity 映射（AGENT_TERMINATED → CRITICAL）
    backend3 = InMemoryAuditBackend()
    audit3 = AuditLog(backend=backend3)
    audit3.write_sync(
        AuditEventType.AGENT_TERMINATED,
        agent_id="agent_e",
        run_id="run_005",
        detail="terminated",
    )
    recs3 = audit3.query_records(agent_id="agent_e")
    assert_true(recs3[0].severity == AuditSeverity.CRITICAL, "AGENT_TERMINATED 默认 severity=CRITICAL")

    # write_sync 可选字段透传
    backend4 = InMemoryAuditBackend()
    audit4 = AuditLog(backend=backend4)
    audit4.write_sync(
        AuditEventType.TOOL_CALL if hasattr(AuditEventType, "TOOL_CALL") else AuditEventType.SKILL_INVOKED,
        agent_id="agent_f",
        run_id="run_006",
        detail="with optionals",
        step_n=3,
        tool_name="my_tool",
    )
    recs4 = audit4.query_records(agent_id="agent_f")
    assert_true(recs4[0].step_n == 3, "write_sync step_n 透传正确")
    assert_true(recs4[0].tool_name == "my_tool", "write_sync tool_name 透传正确")


# ════════════════════════════════════════════════════
#  3. DualAuditBackend — 双写
# ════════════════════════════════════════════════════

def test_dual_audit_backend():
    print("\n" + "═" * 60)
    print("3️⃣  DualAuditBackend — 双写行为")

    primary = InMemoryAuditBackend()
    secondary = InMemoryAuditBackend()
    dual = DualAuditBackend(primary=primary, secondary=secondary)
    audit = AuditLog(backend=dual)

    audit.write_sync(
        AuditEventType.RUN_STARTED,
        agent_id="a1",
        run_id="r1",
        detail="dual write test",
    )

    # 两个后端都写入了
    assert_true(len(primary.query()) == 1, "DualAuditBackend: primary 收到写入")
    assert_true(len(secondary.query()) == 1, "DualAuditBackend: secondary 收到写入")

    # query 委托给 primary
    recs = audit.query_records(agent_id="a1")
    assert_true(len(recs) == 1, "DualAuditBackend.query 委托给 primary")

    # primary 写入失败 → AuditWriteError（不会继续写 secondary 也没问题，仍然传播）
    class _FailPrimary:
        def write(self, record): raise IOError("primary fail")
        def flush(self): pass
        def query(self, **kwargs): return []

    dual_fail = DualAuditBackend(primary=_FailPrimary(), secondary=InMemoryAuditBackend())
    fail_audit = AuditLog(backend=dual_fail)

    @assert_raises(AuditWriteError, "DualAuditBackend primary 失败 → AuditWriteError 传播")
    def _():
        fail_audit.write_sync(
            AuditEventType.RUN_FINISHED,
            agent_id="a2",
            run_id="r2",
            detail="primary fail test",
        )

    # flush 双写
    primary2 = MagicMock()
    secondary2 = MagicMock()
    dual2 = DualAuditBackend(primary=primary2, secondary=secondary2)
    dual2.flush()
    assert_true(primary2.flush.called, "DualAuditBackend.flush 调用 primary.flush")
    assert_true(secondary2.flush.called, "DualAuditBackend.flush 调用 secondary.flush")


# ════════════════════════════════════════════════════
#  4. InMemoryAuditBackend — 线程安全存储
# ════════════════════════════════════════════════════

def test_in_memory_audit_backend():
    print("\n" + "═" * 60)
    print("4️⃣  InMemoryAuditBackend — 线程安全存储与查询")

    backend = InMemoryAuditBackend()

    # 写入多条记录
    run_id = generate_id()
    for i in range(5):
        backend.write(_make_audit_record(
            agent_id="agent_test",
            run_id=run_id,
            event_type=AuditEventType.RUN_STARTED if i % 2 == 0 else AuditEventType.RUN_FINISHED,
            detail=f"record {i}",
        ))
    backend.flush()  # 无操作，不应抛异常

    # 查全部
    all_recs = backend.query(limit=100)
    assert_true(len(all_recs) == 5, "InMemoryAuditBackend: 5条记录全部存入")

    # 按 agent_id 过滤
    recs = backend.query(agent_id="agent_test")
    assert_true(len(recs) == 5, "query 按 agent_id 过滤正确")

    recs_other = backend.query(agent_id="non_existent")
    assert_true(len(recs_other) == 0, "query agent_id 不匹配返回空")

    # 按 event_type 过滤
    recs_started = backend.query(event_type="run_started")
    assert_true(len(recs_started) == 3, "query 按 event_type 过滤正确（3条 RUN_STARTED）")

    # limit 参数
    recs_limited = backend.query(limit=2)
    assert_true(len(recs_limited) == 2, "query limit=2 返回 2 条")

    # max_records 限制（旧记录被裁剪）
    small_backend = InMemoryAuditBackend(max_records=3)
    for i in range(5):
        small_backend.write(_make_audit_record(detail=f"rec {i}"))
    all_recs2 = small_backend.query(limit=10)
    assert_true(len(all_recs2) == 3, "max_records=3 时超出部分被裁剪（保留最新 3 条）")

    # 线程安全：并发写入
    concurrent_backend = InMemoryAuditBackend()
    errors = []

    def _writer():
        try:
            for _ in range(50):
                concurrent_backend.write(_make_audit_record())
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=_writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert_true(len(errors) == 0, "并发写入无异常（线程安全）")
    concurrent_recs = concurrent_backend.query(limit=10000)
    assert_true(len(concurrent_recs) == 200, "并发写入 200 条全部保存（4线程×50次）")


# ════════════════════════════════════════════════════
#  5. Logger — E4 Fail-Safe
# ════════════════════════════════════════════════════

def test_logger():
    print("\n" + "═" * 60)
    print("5️⃣  Logger — E4 静默降级")

    # 正常 backend 写入
    class _RecordingBackend:
        def __init__(self): self.records = []
        def write_log(self, record): self.records.append(record)

    recording = _RecordingBackend()
    logger = Logger(backend=recording, min_level=LogLevel.DEBUG, agent_id="agent_x")

    logger.debug("debug msg", module="test")
    logger.info("info msg", run_id="r1")
    logger.warn("warn msg", step_n=2)
    logger.error("error msg")

    assert_true(len(recording.records) == 4, "Logger: 4 条日志全部写入")
    assert_true(recording.records[0]["level"] == "DEBUG", "Logger.debug 级别正确")
    assert_true(recording.records[1]["message"] == "info msg", "Logger.info 消息正确")
    assert_true(recording.records[2]["step_n"] == 2, "Logger context step_n 透传")
    assert_true(recording.records[1]["run_id"] == "r1", "Logger context run_id 透传")

    # _format_record 结构
    record = recording.records[0]
    assert_true("log_id" in record, "_format_record 包含 log_id")
    assert_true("timestamp" in record, "_format_record 包含 timestamp")
    assert_true("level" in record, "_format_record 包含 level")
    assert_true("module" in record, "_format_record 包含 module")
    assert_true("agent_id" in record, "_format_record 包含 agent_id")

    # agent_id 从构造时传入
    assert_true(record.get("agent_id") == "agent_x", "Logger agent_id 从构造时透传")

    # context 中显式 agent_id 覆盖构造时的
    logger2 = Logger(backend=recording, agent_id="agent_default")
    logger2.info("override agent", agent_id="agent_override")
    last = recording.records[-1]
    assert_true(last.get("agent_id") == "agent_override", "context agent_id 覆盖默认 agent_id")

    # min_level 过滤
    recording2 = _RecordingBackend()
    logger3 = Logger(backend=recording2, min_level=LogLevel.WARN)
    logger3.debug("should be filtered")
    logger3.info("should be filtered too")
    logger3.warn("should pass")
    logger3.error("should pass")
    assert_true(len(recording2.records) == 2, "min_level=WARN 过滤掉 DEBUG/INFO，保留 WARN/ERROR")

    # E4: backend.write_log 抛异常 → 静默降级，不传播
    class _FailBackend:
        def write_log(self, record): raise RuntimeError("backend crash")

    fail_logger = Logger(backend=_FailBackend(), min_level=LogLevel.DEBUG)

    @assert_no_raises("Logger E4: 后端异常 → 静默降级，不传播")
    def _():
        fail_logger.info("this should not raise")

    # log() 通用方法
    recording3 = _RecordingBackend()
    logger4 = Logger(backend=recording3)
    logger4.log(LogLevel.ERROR, "via log()", module="m1")
    assert_true(len(recording3.records) == 1, "Logger.log() 通用方法有效")
    assert_true(recording3.records[0]["level"] == "ERROR", "Logger.log() 级别正确")

    # set_agent_id
    logger5 = Logger(backend=recording3, agent_id="old_id")
    logger5.set_agent_id("new_id")
    logger5.info("after set_agent_id")
    assert_true(recording3.records[-1].get("agent_id") == "new_id", "set_agent_id 更新有效")


# ════════════════════════════════════════════════════
#  6. Tracer — E4 + MINIMAL 采样 + 脱敏
# ════════════════════════════════════════════════════

def test_tracer():
    print("\n" + "═" * 60)
    print("6️⃣  Tracer — E4 降级 / MINIMAL 采样 / 脱敏")

    tracer_backend = InMemoryTracerBackend()

    # ── FULL 模式：所有 span 都被记录 ──
    tracer = Tracer(backend=tracer_backend, trace_level=TraceLevel.FULL, agent_id="a1")
    run_id = generate_id()
    run_span = tracer.start_span("agent.run", SpanType.RUN, run_id=run_id)
    step_span = tracer.start_span("step.1", SpanType.STEP, run_id=run_id)
    llm_span = tracer.start_span("llm.call", SpanType.LLM_CALL, run_id=run_id)

    assert_true(run_span.span_id != "", "FULL 模式: RUN span 非 noop")
    assert_true(step_span.span_id != "", "FULL 模式: STEP span 非 noop")
    assert_true(llm_span.span_id != "", "FULL 模式: LLM_CALL span 非 noop")

    tracer.end_span(llm_span)
    tracer.end_span(step_span)
    tracer.end_span(run_span)

    spans = tracer_backend.get_spans(run_id)
    assert_true(len(spans) == 3, f"FULL 模式: 3 个 span 被记录（实际 {len(spans)}）")
    tracer_backend.clear()

    # ── MINIMAL 模式：只记录 RUN span，其他返回 noop ──
    tracer_min = Tracer(backend=tracer_backend, trace_level=TraceLevel.MINIMAL, agent_id="a2")
    run_id2 = generate_id()
    run_span2 = tracer_min.start_span("agent.run", SpanType.RUN, run_id=run_id2)
    step_span2 = tracer_min.start_span("step.1", SpanType.STEP, run_id=run_id2)
    llm_span2 = tracer_min.start_span("llm.call", SpanType.LLM_CALL, run_id=run_id2)

    assert_true(run_span2.span_id != "", "MINIMAL 模式: RUN span 非 noop")
    assert_true(step_span2.span_id == "", "MINIMAL 模式: STEP span 为 noop (span_id='')")
    assert_true(llm_span2.span_id == "", "MINIMAL 模式: LLM_CALL span 为 noop (span_id='')")

    # noop span 的 end_span 不写入后端（span_id 为空时直接返回）
    tracer_min.end_span(step_span2)
    tracer_min.end_span(llm_span2)
    tracer_min.end_span(run_span2)

    spans_min = tracer_backend.get_spans(run_id2)
    assert_true(len(spans_min) == 1, f"MINIMAL 模式: 只有 RUN span 写入后端（实际 {len(spans_min)}）")
    tracer_backend.clear()

    # ── SUMMARY 模式：所有 span 记录，但长属性被截断 ──
    tracer_sum = Tracer(backend=tracer_backend, trace_level=TraceLevel.SUMMARY)
    run_id3 = generate_id()
    span_sum = tracer_sum.start_span("agent.run", SpanType.RUN, run_id=run_id3,
                                      attributes={"long_attr": "x" * 300})
    tracer_sum.end_span(span_sum)
    spans_sum = tracer_backend.get_spans(run_id3)
    assert_true(len(spans_sum) == 1, "SUMMARY 模式: RUN span 被记录")
    # 长属性被截断到 200 字符 + "..."
    long_val = spans_sum[0].attributes.get("long_attr", "")
    assert_true(len(long_val) <= 203, f"SUMMARY 模式: 长属性被截断（实际长度 {len(long_val)}）")
    assert_true(long_val.endswith("..."), "SUMMARY 模式: 截断后以 '...' 结尾")
    tracer_backend.clear()

    # ── start_span 返回的 Span 包含正确字段 ──
    tracer2 = Tracer(backend=tracer_backend, trace_level=TraceLevel.FULL, agent_id="a3")
    run_id4 = generate_id()
    sp = tracer2.start_span("llm.call", SpanType.LLM_CALL, run_id=run_id4, step_n=2,
                              attributes={"model": "gpt-4"})
    assert_true(sp.span_id != "", "start_span 返回非 noop span")
    assert_true(sp.run_id == run_id4, "start_span run_id 正确")
    assert_true(sp.step_n == 2, "start_span step_n 正确")
    assert_true(sp.span_type == SpanType.LLM_CALL, "start_span span_type 正确")
    assert_true(sp.end_time is None, "start_span end_time 未填充")
    assert_true(sp.duration_ms is None, "start_span duration_ms 未填充")
    assert_true(sp.attributes.get("model") == "gpt-4", "start_span attributes 透传")
    tracer_backend.clear()

    # ── end_span 填充 end_time 和 duration_ms ──
    tracer3 = Tracer(backend=tracer_backend, trace_level=TraceLevel.FULL)
    run_id5 = generate_id()
    sp3 = tracer3.start_span("step.1", SpanType.STEP, run_id=run_id5)
    time.sleep(0.01)
    tracer3.end_span(sp3, status=SpanStatus.OK)
    recorded = tracer_backend.get_spans(run_id5)
    assert_true(len(recorded) == 1, "end_span 写入后端")
    assert_true(recorded[0].end_time is not None, "end_span 填充 end_time")
    assert_true(recorded[0].duration_ms is not None and recorded[0].duration_ms > 0,
                "end_span 填充 duration_ms > 0")
    assert_true(recorded[0].status == SpanStatus.OK, "end_span status=OK 正确")
    tracer_backend.clear()

    # ── end_span 可附加额外 attributes ──
    tracer4 = Tracer(backend=tracer_backend, trace_level=TraceLevel.FULL)
    run_id6 = generate_id()
    sp4 = tracer4.start_span("llm.call", SpanType.LLM_CALL, run_id=run_id6,
                               attributes={"model": "gpt-4"})
    tracer4.end_span(sp4, attributes={"input_tokens": 100, "output_tokens": 50})
    recorded4 = tracer_backend.get_spans(run_id6)
    assert_true(recorded4[0].attributes.get("input_tokens") == 100, "end_span 合并 attributes")
    assert_true(recorded4[0].attributes.get("model") == "gpt-4", "end_span 保留原始 attributes")
    tracer_backend.clear()

    # ── mark_span_error ──
    tracer5 = Tracer(backend=tracer_backend, trace_level=TraceLevel.MINIMAL)
    run_id7 = generate_id()
    step_sp5 = tracer5.start_span("step.1", SpanType.STEP, run_id=run_id7)
    # MINIMAL 下 STEP span 为 noop，mark_span_error 不应崩溃
    @assert_no_raises("mark_span_error on noop span 不崩溃")
    def _():
        tracer5.mark_span_error(step_sp5)
    tracer_backend.clear()

    # ── E4: backend.export_span 抛异常 → 静默降级 ──
    class _FailTracerBackend:
        def export_span(self, span): raise RuntimeError("tracer fail")
        def query_spans(self, run_id): return []

    fail_tracer = Tracer(backend=_FailTracerBackend(), trace_level=TraceLevel.FULL)
    run_id8 = generate_id()
    sp8 = fail_tracer.start_span("test", SpanType.RUN, run_id=run_id8)

    @assert_no_raises("Tracer E4: backend 异常 → 静默降级")
    def _():
        fail_tracer.end_span(sp8)

    # ── None backend → _should_record 返回 False ──
    noop_tracer = Tracer(backend=None, trace_level=TraceLevel.FULL)
    noop_span = noop_tracer.start_span("test", SpanType.RUN, run_id="r1")
    assert_true(noop_span.span_id == "", "backend=None → start_span 返回 noop")

    # ── _sanitize_attributes: sanitizer 正常运行 ──
    class _UpperSanitizer:
        def sanitize(self, value: str, *, field_name: str) -> str:
            return value.upper()

    tracer_san = Tracer(backend=tracer_backend, trace_level=TraceLevel.FULL,
                        sanitizer=_UpperSanitizer())
    run_id9 = generate_id()
    sp9 = tracer_san.start_span("test", SpanType.RUN, run_id=run_id9,
                                  attributes={"msg": "hello"})
    assert_true(sp9.attributes.get("msg") == "HELLO", "_sanitize_attributes: 字符串脱敏正确")
    tracer_san.end_span(sp9)
    tracer_backend.clear()

    # ── _sanitize_attributes: sanitizer 抛异常 → {"_sanitize_error": True} ──
    class _ErrorSanitizer:
        def sanitize(self, value: str, *, field_name: str) -> str:
            raise ValueError("sanitize failed")

    tracer_err_san = Tracer(backend=tracer_backend, trace_level=TraceLevel.FULL,
                             sanitizer=_ErrorSanitizer())
    run_id10 = generate_id()
    sp10 = tracer_err_san.start_span("test", SpanType.RUN, run_id=run_id10,
                                      attributes={"msg": "secret"})
    assert_true(sp10.attributes.get("_sanitize_error") is True,
                "_sanitize_attributes: sanitizer 异常 → {'_sanitize_error': True}")
    tracer_backend.clear()

    # ── build_trace_context ──
    tracer_ctx = Tracer(backend=tracer_backend, trace_level=TraceLevel.FULL, agent_id="parent_agent")
    parent_span = tracer_ctx.start_span("agent.run", SpanType.RUN, run_id="run_parent")
    ctx = tracer_ctx.build_trace_context(parent_span)
    assert_true(ctx.trace_id == parent_span.trace_id, "build_trace_context trace_id 从 parent_span 取")
    assert_true(ctx.parent_span_id == parent_span.span_id, "build_trace_context parent_span_id 正确")
    assert_true(ctx.agent_id == "parent_agent", "build_trace_context agent_id 正确")
    assert_true(ctx.run_id == "", "build_trace_context run_id 为空（子 Agent 自己生成）")
    tracer_backend.clear()

    # ── query_trace ──
    tracer_q = Tracer(backend=tracer_backend, trace_level=TraceLevel.FULL)
    run_id_q = generate_id()
    sp_q = tracer_q.start_span("agent.run", SpanType.RUN, run_id=run_id_q)
    tracer_q.end_span(sp_q)
    spans_q = tracer_q.query_trace(run_id_q)
    assert_true(len(spans_q) == 1, "query_trace 返回正确 span 数量")


# ════════════════════════════════════════════════════
#  7. InMemoryTracerBackend
# ════════════════════════════════════════════════════

def test_in_memory_tracer_backend():
    print("\n" + "═" * 60)
    print("7️⃣  InMemoryTracerBackend — 存储与查询")

    backend = InMemoryTracerBackend()

    run_a = generate_id()
    run_b = generate_id()

    for i in range(3):
        backend.export_span(_make_span(run_id=run_a, name=f"span_a_{i}"))
    for i in range(2):
        backend.export_span(_make_span(run_id=run_b, name=f"span_b_{i}"))

    assert_true(len(backend.get_spans(run_a)) == 3, "get_spans 按 run_id 过滤（run_a: 3 条）")
    assert_true(len(backend.get_spans(run_b)) == 2, "get_spans 按 run_id 过滤（run_b: 2 条）")
    assert_true(len(backend.get_spans()) == 5, "get_spans() 无参数返回全部")
    assert_true(len(backend.query_spans(run_a)) == 3, "query_spans 与 get_spans 行为一致")

    # max_spans 限制
    small = InMemoryTracerBackend(max_spans=3)
    for i in range(5):
        small.export_span(_make_span(name=f"s{i}"))
    assert_true(len(small.get_spans()) == 3, "max_spans=3: 只保留最新 3 条")

    # clear
    backend.clear()
    assert_true(len(backend.get_spans()) == 0, "clear() 后 get_spans() 返回空")


# ════════════════════════════════════════════════════
#  8. Metrics — E4 Fail-Safe
# ════════════════════════════════════════════════════

def test_metrics():
    print("\n" + "═" * 60)
    print("8️⃣  Metrics — E4 降级 / 命名 API")

    m_backend = InMemoryMetricsBackend()
    metrics = Metrics(backend=m_backend, agent_id="agent_m")

    # ── 命名 Counter API ──
    metrics.inc_run_total("started")
    metrics.inc_run_total("success")
    metrics.inc_step_total()
    metrics.inc_step_total()
    metrics.inc_llm_call_total("gpt-4", "success")
    metrics.inc_tool_execute_total("search", "success")
    metrics.inc_tool_execute_total("search", "error")
    metrics.inc_error_total("TimeoutError")
    metrics.inc_permission_check_total("allow")
    metrics.inc_hitl_approval_total("need_approval")

    assert_true(m_backend.get_counter("run_total", {"status": "started", "agent_id": "agent_m"}) == 1,
                "inc_run_total('started') 记录正确")
    assert_true(m_backend.get_counter("step_total", {"agent_id": "agent_m"}) == 2,
                "inc_step_total 计数正确（2次）")
    assert_true(m_backend.get_counter("llm_call_total", {"model": "gpt-4", "status": "success", "agent_id": "agent_m"}) == 1,
                "inc_llm_call_total 记录正确")
    assert_true(m_backend.get_counter("tool_execute_total", {"tool_name": "search", "status": "success", "agent_id": "agent_m"}) == 1,
                "inc_tool_execute_total success 记录正确")
    assert_true(m_backend.get_counter("error_total", {"error_type": "TimeoutError", "agent_id": "agent_m"}) == 1,
                "inc_error_total 记录正确")

    # ── 直方图 API ──
    metrics.observe_run_duration_ms(1500.0)
    metrics.observe_step_duration_ms(300.0)
    metrics.observe_llm_call_duration_ms(800.0, model="gpt-4")
    metrics.observe_tool_execute_duration_ms(50.0, tool_name="search")

    hist_run = m_backend.get_histogram("run_duration_ms", {"agent_id": "agent_m"})
    assert_true(len(hist_run) == 1 and hist_run[0] == 1500.0,
                "observe_run_duration_ms 记录正确")

    # ── Gauge API ──（token_cost_total_usd 已移除：SDK 不再计价/记录花费）
    metrics.set_active_runs(3)

    assert_true(m_backend.get_gauge("active_runs", {"agent_id": "agent_m"}) == 3.0,
                "set_active_runs 记录正确")

    # ── record_tokens ──
    metrics.record_tokens(1000, 200, model_name="gpt-4")
    assert_true(
        m_backend.get_counter("llm_input_tokens_total", {"model": "gpt-4", "agent_id": "agent_m"}) == 1000,
        "record_tokens input_tokens 记录正确"
    )
    assert_true(
        m_backend.get_counter("llm_output_tokens_total", {"model": "gpt-4", "agent_id": "agent_m"}) == 200,
        "record_tokens output_tokens 记录正确"
    )

    # ── get_summary ──
    summary = m_backend.get_summary()
    assert_true("counters" in summary, "get_summary 包含 counters")
    assert_true("histograms" in summary, "get_summary 包含 histograms")
    assert_true("gauges" in summary, "get_summary 包含 gauges")

    # ── E4: backend=None → 所有方法无操作，不崩溃 ──
    noop_metrics = Metrics(backend=None, agent_id="noop")

    @assert_no_raises("Metrics backend=None: 所有命名 API 无操作，不崩溃")
    def _():
        noop_metrics.inc_run_total()
        noop_metrics.inc_step_total()
        noop_metrics.inc_llm_call_total()
        noop_metrics.inc_tool_execute_total("tool")
        noop_metrics.inc_error_total()
        noop_metrics.inc_permission_check_total()
        noop_metrics.inc_hitl_approval_total()
        noop_metrics.observe_run_duration_ms(100.0)
        noop_metrics.observe_step_duration_ms(50.0)
        noop_metrics.observe_llm_call_duration_ms(200.0)
        noop_metrics.observe_tool_execute_duration_ms(30.0)
        noop_metrics.set_active_runs(1)
        noop_metrics.inc_active_runs()
        noop_metrics.record_tokens(100, 50)

    # ── E4: backend.record_counter 抛异常 → 静默降级 ──
    class _FailMetricsBackend:
        def record_counter(self, *a, **kw): raise RuntimeError("metrics fail")
        def record_histogram(self, *a, **kw): raise RuntimeError("metrics fail")
        def record_gauge(self, *a, **kw): raise RuntimeError("metrics fail")

    fail_metrics = Metrics(backend=_FailMetricsBackend(), agent_id="fail")

    @assert_no_raises("Metrics E4: backend 异常 → 静默降级")
    def _():
        fail_metrics.inc_run_total()
        fail_metrics.observe_run_duration_ms(100.0)
        fail_metrics.set_active_runs(2)

    # ── 通用 API ──
    m2 = Metrics(backend=m_backend, agent_id="gen")
    m2.record_duration("custom_duration", 123.4)
    m2.increment_counter("custom_counter")
    m2.set_gauge("custom_gauge", 42.0)

    @assert_no_raises("Metrics 通用 API 不崩溃")
    def _():
        m2.record_duration("custom_duration", 50.0)
        m2.increment_counter("custom_counter")
        m2.set_gauge("custom_gauge", 10.0)


# ════════════════════════════════════════════════════
#  9. InMemoryMetricsBackend — 存储与查询
# ════════════════════════════════════════════════════

def test_in_memory_metrics_backend():
    print("\n" + "═" * 60)
    print("9️⃣  InMemoryMetricsBackend — 存储与查询")

    backend = InMemoryMetricsBackend()

    # counter 累计
    backend.record_counter("req_total", 1, {"env": "test"})
    backend.record_counter("req_total", 2, {"env": "test"})
    backend.record_counter("req_total", 1, {"env": "prod"})
    assert_true(backend.get_counter("req_total", {"env": "test"}) == 3, "counter 累计: test = 3")
    assert_true(backend.get_counter("req_total", {"env": "prod"}) == 1, "counter 累计: prod = 1")
    assert_true(backend.get_counter("nonexistent") == 0, "不存在的 counter 返回 0")

    # histogram
    backend.record_histogram("latency_ms", 100.0, {})
    backend.record_histogram("latency_ms", 200.0, {})
    backend.record_histogram("latency_ms", 300.0, {})
    hist = backend.get_histogram("latency_ms")
    assert_true(hist == [100.0, 200.0, 300.0], "histogram 存储有序（[100, 200, 300]）")
    assert_true(backend.get_histogram("nonexistent") == [], "不存在的 histogram 返回空列表")

    # gauge 覆盖
    backend.record_gauge("active_conns", 5.0, {})
    backend.record_gauge("active_conns", 10.0, {})
    assert_true(backend.get_gauge("active_conns") == 10.0, "gauge 覆盖（最新值）")
    assert_true(backend.get_gauge("nonexistent") == 0.0, "不存在的 gauge 返回 0.0")

    # get_summary 结构完整
    summary = backend.get_summary()
    assert_true("req_total{env=prod}" in summary["counters"] or
                any("req_total" in k for k in summary["counters"]), "get_summary counters 包含 req_total")
    assert_true("latency_ms" in summary["histograms"], "get_summary histograms 包含 latency_ms")
    hist_summary = summary["histograms"]["latency_ms"]
    assert_true(hist_summary["count"] == 3, "histogram summary count=3")
    assert_true(hist_summary["min"] == 100.0, "histogram summary min=100.0")
    assert_true(hist_summary["max"] == 300.0, "histogram summary max=300.0")
    assert_true(abs(hist_summary["avg"] - 200.0) < 0.01, "histogram summary avg=200.0")
    assert_true(hist_summary["sum"] == 600.0, "histogram summary sum=600.0")

    # label key 生成
    assert_true(backend.get_counter("req_total", None) == 0, "labels=None 等价于 labels={}")


# ════════════════════════════════════════════════════
#  10. ObservabilityConfig — 三态值对象
# ════════════════════════════════════════════════════

def test_observability_config():
    print("\n" + "═" * 60)
    print("🔟  ObservabilityConfig — 三态值对象")

    # 全部默认（零配置）
    cfg = ObservabilityConfig()
    assert_true(cfg.log_backend is None, "ObservabilityConfig 默认: log_backend=None")
    assert_true(cfg.tracer_backend is None, "ObservabilityConfig 默认: tracer_backend=None")
    assert_true(cfg.metrics_backend is None, "ObservabilityConfig 默认: metrics_backend=None")
    assert_true(cfg.audit_backend is None, "ObservabilityConfig 默认: audit_backend=None")
    assert_true(cfg.log_level == LogLevel.INFO, "ObservabilityConfig 默认 log_level=INFO")
    assert_true(cfg.trace_level == TraceLevel.SUMMARY, "ObservabilityConfig 默认 trace_level=SUMMARY")
    assert_true(cfg.sanitizer is None, "ObservabilityConfig 默认 sanitizer=None")

    # 显式 False（关闭各子系统）
    cfg2 = ObservabilityConfig(
        log_backend=False,
        tracer_backend=False,
        metrics_backend=False,
        audit_backend=False,
    )
    assert_true(cfg2.log_backend is False, "ObservabilityConfig: log_backend=False")
    assert_true(cfg2.tracer_backend is False, "ObservabilityConfig: tracer_backend=False")
    assert_true(cfg2.metrics_backend is False, "ObservabilityConfig: metrics_backend=False")
    assert_true(cfg2.audit_backend is False, "ObservabilityConfig: audit_backend=False（HC4 由 Provider 降级）")

    # 自定义 Backend 实例
    custom_audit = InMemoryAuditBackend()
    custom_tracer = InMemoryTracerBackend()
    cfg3 = ObservabilityConfig(
        audit_backend=custom_audit,
        tracer_backend=custom_tracer,
        log_level=LogLevel.DEBUG,
        trace_level=TraceLevel.FULL,
    )
    assert_true(cfg3.audit_backend is custom_audit, "ObservabilityConfig: audit_backend=自定义实例")
    assert_true(cfg3.tracer_backend is custom_tracer, "ObservabilityConfig: tracer_backend=自定义实例")
    assert_true(cfg3.log_level == LogLevel.DEBUG, "ObservabilityConfig: log_level=DEBUG")
    assert_true(cfg3.trace_level == TraceLevel.FULL, "ObservabilityConfig: trace_level=FULL")

    # frozen=True → 不可变
    @assert_raises(FrozenInstanceError, "ObservabilityConfig frozen 不可变")
    def _():
        cfg.log_level = LogLevel.ERROR  # type: ignore


# ════════════════════════════════════════════════════
#  11. ObservabilityProvider — 三态后端解析
# ════════════════════════════════════════════════════

def test_observability_provider():
    print("\n" + "═" * 60)
    print("1️⃣1️⃣  ObservabilityProvider — 三态后端解析")

    # ── None → SDK 默认 ──
    # audit: None → InMemoryAuditBackend（带 WARN 日志）
    # tracer: None → NoopTracer（带 WARN 日志）
    # metrics: None → NoopMetrics（带 WARN 日志）
    # logger: None → ConsoleLoggerBackend
    provider_default = ObservabilityProvider(agent_id="test_agent")
    assert_true(provider_default.logger is not None, "Provider 默认: logger 已创建")
    assert_true(provider_default.tracer is not None, "Provider 默认: tracer 已创建")
    assert_true(provider_default.metrics is not None, "Provider 默认: metrics 已创建")
    assert_true(provider_default.audit_log is not None, "Provider 默认: audit_log 已创建")
    assert_true(provider_default.hooks_adapter is not None, "Provider 默认: hooks_adapter 已创建")

    # audit_log 可以写入（不应抛出）
    @assert_no_raises("Provider 默认 audit_log.write_sync 不崩溃")
    def _():
        provider_default.audit_log.write_sync(
            AuditEventType.RUN_STARTED,
            agent_id="test_agent",
            run_id="run_test",
            detail="test",
        )

    # ── False → Null/InMemory（HC4 降级） ──
    cfg_off = ObservabilityConfig(
        log_backend=False,
        tracer_backend=False,
        metrics_backend=False,
        audit_backend=False,  # HC4: 强制降级为 InMemory
    )
    provider_off = ObservabilityProvider(cfg_off, agent_id="off_agent")

    # logger=False → NullLogger（写入无操作，不崩溃）
    @assert_no_raises("Provider log=False: logger.info 不崩溃")
    def _():
        provider_off.logger.info("should be swallowed")

    # tracer=False → NoopTracer
    noop_sp = provider_off.tracer.start_span("test", SpanType.RUN, run_id="r1")
    assert_true(noop_sp.span_id == "", "Provider tracer=False: start_span 返回 noop")

    # audit=False → HC4 降级为 InMemory，write_sync 正常工作
    @assert_no_raises("Provider audit=False: 降级为 InMemory，write_sync 不崩溃")
    def _():
        provider_off.audit_log.write_sync(
            AuditEventType.RUN_STARTED,
            agent_id="off_agent",
            run_id="run_off",
            detail="degraded to InMemory",
        )

    # ── 自定义 Backend 实例 ──
    custom_audit_b = InMemoryAuditBackend()
    custom_tracer_b = InMemoryTracerBackend()
    custom_metrics_b = InMemoryMetricsBackend()

    cfg_custom = ObservabilityConfig(
        audit_backend=custom_audit_b,
        tracer_backend=custom_tracer_b,
        metrics_backend=custom_metrics_b,
        log_level=LogLevel.DEBUG,
        trace_level=TraceLevel.FULL,
    )
    provider_custom = ObservabilityProvider(cfg_custom, agent_id="custom_agent")

    # audit 写入后自定义后端有记录
    provider_custom.audit_log.write_sync(
        AuditEventType.RUN_FINISHED,
        agent_id="custom_agent",
        run_id="run_custom",
        detail="custom backend test",
    )
    recs = custom_audit_b.query(agent_id="custom_agent")
    assert_true(len(recs) == 1, "Provider 自定义 audit_backend: write_sync 写入自定义后端")

    # tracer 写入后自定义后端有 span
    run_sp = provider_custom.tracer.start_span("agent.run", SpanType.RUN, run_id="run_custom")
    provider_custom.tracer.end_span(run_sp)
    spans = custom_tracer_b.get_spans("run_custom")
    assert_true(len(spans) == 1, "Provider 自定义 tracer_backend: span 写入自定义后端")

    # metrics 写入后自定义后端有数据
    provider_custom.metrics.inc_run_total("success")
    cnt = custom_metrics_b.get_counter("run_total", {"status": "success", "agent_id": "custom_agent"})
    assert_true(cnt == 1, "Provider 自定义 metrics_backend: counter 写入自定义后端")

    # ── build_observability_context ──
    ctx = ObservabilityProvider.build_observability_context(
        agent_id="ctx_agent",
        run_id="run_ctx",
    )
    assert_true(ctx.agent_id == "ctx_agent", "build_observability_context: agent_id 正确")
    assert_true(ctx.run_id == "run_ctx", "build_observability_context: run_id 正确")
    assert_true(ctx.trace_id == "run_ctx", "build_observability_context: trace_id=run_id（单 Agent）")

    # 多 Agent 场景：显式传入 trace_id
    ctx2 = ObservabilityProvider.build_observability_context(
        agent_id="sub_agent",
        run_id="run_sub",
        trace_id="trace_root",
        parent_span_id="span_parent",
    )
    assert_true(ctx2.trace_id == "trace_root", "build_observability_context: 显式 trace_id 优先")
    assert_true(ctx2.parent_span_id == "span_parent", "build_observability_context: parent_span_id 正确")


# ════════════════════════════════════════════════════
#  12. ObservabilityHooksAdapter — 11 个 Hook
# ════════════════════════════════════════════════════

def test_hooks_adapter():
    print("\n" + "═" * 60)
    print("1️⃣2️⃣  ObservabilityHooksAdapter — 11 个 Hook")

    # 准备可检查的 mock backends
    tracer_backend = InMemoryTracerBackend()
    metrics_backend = InMemoryMetricsBackend()
    log_backend_records: list[dict] = []

    class _LogBackend:
        def write_log(self, record): log_backend_records.append(record)

    logger = Logger(backend=_LogBackend(), min_level=LogLevel.DEBUG, agent_id="ha_agent")
    tracer = Tracer(backend=tracer_backend, trace_level=TraceLevel.FULL, agent_id="ha_agent")
    metrics = Metrics(backend=metrics_backend, agent_id="ha_agent")
    adapter = ObservabilityHooksAdapter(logger=logger, tracer=tracer, metrics=metrics)

    run_id = generate_id()

    # ── Hook 1: on_run_start ──
    adapter.on_run_start("用户任务描述", run_id)
    assert_true(len(log_backend_records) > 0, "on_run_start: 产生日志")
    assert_true(
        metrics_backend.get_counter("run_total", {"status": "started", "agent_id": "ha_agent"}) == 1,
        "on_run_start: inc_run_total('started') 调用"
    )
    assert_true(
        metrics_backend.get_gauge("active_runs", {"agent_id": "ha_agent"}) == 1.0,
        "on_run_start: set_active_runs(1) 调用"
    )
    # run span 已创建
    run_spans_before_end = [s for s in tracer_backend.get_spans() if s.span_type == SpanType.RUN]
    # run span 还未结束，应在 active_spans 中（此时 backend 中可能还没有）
    assert_true(adapter._run_span is not None and adapter._run_span.span_id != "",
                "on_run_start: _run_span 已创建（非 noop）")

    # ── Hook 3: on_step_start ──
    log_count_before = len(log_backend_records)
    adapter.on_step_start(1, run_id)
    assert_true(len(log_backend_records) > log_count_before, "on_step_start: 产生日志")
    assert_true(
        metrics_backend.get_counter("step_total", {"agent_id": "ha_agent"}) == 1,
        "on_step_start: inc_step_total() 调用"
    )
    assert_true(adapter._step_span is not None and adapter._step_span.span_id != "",
                "on_step_start: _step_span 已创建")

    # ── Hook 5: on_before_llm_call ──
    messages = [
        {"role": "system", "content": "you are a helpful assistant"},
        {"role": "user", "content": "Hello"},
    ]
    adapter.on_before_llm_call(messages, run_id, model="gpt-4o")
    assert_true(adapter._llm_span is not None and adapter._llm_span.span_id != "",
                "on_before_llm_call: _llm_span 已创建")
    assert_true(adapter._llm_call_model == "gpt-4o", "on_before_llm_call: _llm_call_model 暂存")

    # ── Hook 6: on_after_llm_call ──
    response = {
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "tool_calls": None,
    }
    adapter.on_after_llm_call(response, run_id, model="gpt-4o")
    assert_true(adapter._llm_span is None, "on_after_llm_call: _llm_span 已清空")
    assert_true(
        metrics_backend.get_counter("llm_call_total", {"model": "gpt-4o", "status": "success", "agent_id": "ha_agent"}) == 1,
        "on_after_llm_call: inc_llm_call_total 调用"
    )
    llm_tokens_in = metrics_backend.get_counter("llm_input_tokens_total", {"model": "gpt-4o", "agent_id": "ha_agent"})
    assert_true(llm_tokens_in == 100, "on_after_llm_call: record_tokens input=100 正确")

    # on_after_llm_call 幂等保护（重复调用不重复记录）
    adapter.on_after_llm_call(response, run_id, model="gpt-4o")  # 第二次
    llm_call_cnt = metrics_backend.get_counter("llm_call_total", {"model": "gpt-4o", "status": "success", "agent_id": "ha_agent"})
    assert_true(llm_call_cnt == 1, "on_after_llm_call 幂等保护：重复调用不重复计数")

    # ── Hook 7: on_before_tool_call ──
    adapter.on_before_tool_call("search_web", {"query": "test"}, run_id)
    assert_true("search_web" in adapter._tool_spans, "on_before_tool_call: _tool_spans 已记录")
    assert_true(len(adapter._tool_spans["search_web"]) == 1, "on_before_tool_call: FIFO 队列 1 个 span")

    # ── Hook 8: on_after_tool_call ──
    class _ToolResult:
        def __init__(self, success, data=None, error=None):
            self.success = success
            self.data = data
            self.error = error

    adapter.on_after_tool_call("search_web", _ToolResult(True, "result data"), run_id)
    assert_true("search_web" not in adapter._tool_spans, "on_after_tool_call: _tool_spans 清空")
    assert_true(
        metrics_backend.get_counter("tool_execute_total", {"tool_name": "search_web", "status": "success", "agent_id": "ha_agent"}) == 1,
        "on_after_tool_call: inc_tool_execute_total('success') 调用"
    )

    # ── Hook 7+8 并发同名工具（Bug 1 Fix 验证） ──
    adapter2_tracer_b = InMemoryTracerBackend()
    adapter2_metrics_b = InMemoryMetricsBackend()
    adapter2 = ObservabilityHooksAdapter(
        logger=Logger(backend=_LogBackend(), agent_id="ha2"),
        tracer=Tracer(backend=adapter2_tracer_b, trace_level=TraceLevel.FULL, agent_id="ha2"),
        metrics=Metrics(backend=adapter2_metrics_b, agent_id="ha2"),
    )
    run_id2 = generate_id()
    adapter2.on_run_start("task", run_id2)
    adapter2.on_step_start(1, run_id2)
    adapter2.on_before_tool_call("fetch", {"url": "a"}, run_id2)
    adapter2.on_before_tool_call("fetch", {"url": "b"}, run_id2)
    assert_true(len(adapter2._tool_spans.get("fetch", [])) == 2, "并发同名工具: FIFO 队列有 2 个 span")
    adapter2.on_after_tool_call("fetch", _ToolResult(True, "ok"), run_id2)
    assert_true(len(adapter2._tool_spans.get("fetch", [])) == 1, "并发同名工具: on_after_tool_call FIFO pop 正确")
    adapter2.on_after_tool_call("fetch", _ToolResult(False, error="failed"), run_id2)
    assert_true("fetch" not in adapter2._tool_spans, "并发同名工具: 第二个 on_after_tool_call 后队列清空")

    # ── Hook 9: on_hitl_requested ──
    adapter.on_hitl_requested("risky_tool", run_id)
    assert_true(adapter._step_span is None, "on_hitl_requested: step_span 已关闭（Bug 2 Fix）")
    assert_true(adapter._hitl_span is not None and adapter._hitl_span.span_id != "",
                "on_hitl_requested: _hitl_span 已独立创建（Bug 2 Fix）")
    assert_true(
        metrics_backend.get_counter("hitl_approval_total", {"result": "need_approval", "agent_id": "ha_agent"}) == 1,
        "on_hitl_requested: inc_hitl_approval_total('need_approval') 调用"
    )

    # ── Hook 9b: on_hitl_resolved ── 审批「结果」计入指标（补齐 need_approval 观测缺口）
    adapter.on_hitl_resolved("risky_tool", "approved", run_id)
    adapter.on_hitl_resolved("risky_tool2", "rejected", run_id)
    assert_true(
        metrics_backend.get_counter("hitl_approval_total", {"result": "approved", "agent_id": "ha_agent"}) == 1,
        "on_hitl_resolved('approved'): hitl_approval_total{result=approved} 计入"
    )
    assert_true(
        metrics_backend.get_counter("hitl_approval_total", {"result": "rejected", "agent_id": "ha_agent"}) == 1,
        "on_hitl_resolved('rejected'): hitl_approval_total{result=rejected} 计入"
    )

    # ── Hook 10: on_error ──
    log_count_before_err = len(log_backend_records)
    adapter.on_error(ValueError("test error"), run_id)
    assert_true(len(log_backend_records) > log_count_before_err, "on_error: 产生 error 日志")
    assert_true(
        metrics_backend.get_counter("error_total", {"error_type": "ValueError", "agent_id": "ha_agent"}) == 1,
        "on_error: inc_error_total('ValueError') 调用"
    )

    # ── Hook 11: on_halt ──
    log_count_before_halt = len(log_backend_records)
    adapter.on_halt("max_steps_exceeded", run_id)
    assert_true(len(log_backend_records) > log_count_before_halt, "on_halt: 产生 warn 日志")

    # ── Hook 4: on_step_end ──
    # 先重新创建 step span（因为 on_hitl_requested 已关闭了 _step_span）
    adapter.on_step_start(2, run_id)
    adapter.on_step_end(2, run_id)
    assert_true(adapter._step_span is None, "on_step_end: _step_span 已清空")
    step_hist = metrics_backend.get_histogram("step_duration_ms", {"agent_id": "ha_agent"})
    assert_true(len(step_hist) == 1, "on_step_end: observe_step_duration_ms 调用了 1 次（HITL 中断的 step1 不经过 on_step_end，只有正常结束的 step2 记录 duration）")

    # ── Hook 2: on_run_end ──
    adapter.on_run_end(run_id, success=True)
    assert_true(adapter._run_span is None, "on_run_end: _run_span 已清空")
    assert_true(
        metrics_backend.get_counter("run_total", {"status": "success", "agent_id": "ha_agent"}) == 1,
        "on_run_end success: inc_run_total('success') 调用"
    )
    assert_true(
        metrics_backend.get_gauge("active_runs", {"agent_id": "ha_agent"}) == 0.0,
        "on_run_end: set_active_runs(0) 调用（active_run_count 减 1）"
    )
    run_hist = metrics_backend.get_histogram("run_duration_ms", {"agent_id": "ha_agent"})
    assert_true(len(run_hist) == 1, "on_run_end: observe_run_duration_ms 调用一次")

    # on_run_end 检查 run span 被写入 tracer backend
    run_spans = [s for s in tracer_backend.get_spans() if s.span_type == SpanType.RUN]
    assert_true(len(run_spans) >= 1, "on_run_end: run span 已写入 tracer backend")

    # ── on_run_end: hitl_paused 不减少 active_runs ──
    adapter3_metrics_b = InMemoryMetricsBackend()
    adapter3 = ObservabilityHooksAdapter(
        logger=Logger(backend=_LogBackend(), agent_id="ha3"),
        tracer=Tracer(backend=InMemoryTracerBackend(), trace_level=TraceLevel.FULL, agent_id="ha3"),
        metrics=Metrics(backend=adapter3_metrics_b, agent_id="ha3"),
    )
    run_id3 = generate_id()
    adapter3.on_run_start("task", run_id3)
    gauge_before = adapter3_metrics_b.get_gauge("active_runs", {"agent_id": "ha3"})
    adapter3.on_run_end(run_id3, success=False, terminal_reason="hitl_paused")
    gauge_after = adapter3_metrics_b.get_gauge("active_runs", {"agent_id": "ha3"})
    assert_true(gauge_before == gauge_after == 1.0,
                "on_run_end hitl_paused: active_runs 不减少（仍为 1）")

    # ── on_run_end: 交互/Plan 暂停不被误记为 error（run span=CANCELLED/OK）──
    # 回归：历史上只特判了 hitl_paused，interaction_paused / plan_complete 落到 else 被记 ERROR
    # 且结束原因列为空（run_terminal_reason 在 yield 之后赋值、被 GeneratorExit 跳过）。
    # active_runs 语义：真正挂起等待（success=False）仍活跃；plan_complete 是成功终止
    # （success=True）→ 必须释放 active_runs（否则每个 plan 模式 run 永久泄漏 +1）。
    for _reason, _success, _want_status, _want_gauge in (
        ("interaction_paused", False, SpanStatus.CANCELLED, 1.0),  # 挂起等待用户 → 仍活跃
        ("plan_complete", True, SpanStatus.OK, 0.0),               # 规划成功终止 → 释放
    ):
        _tb = InMemoryTracerBackend()
        _mb = InMemoryMetricsBackend()
        _ad = ObservabilityHooksAdapter(
            logger=Logger(backend=_LogBackend(), agent_id="hap"),
            tracer=Tracer(backend=_tb, trace_level=TraceLevel.FULL, agent_id="hap"),
            metrics=Metrics(backend=_mb, agent_id="hap"),
        )
        _rid = generate_id()
        _ad.on_run_start("task", _rid)
        _ad.on_run_end(_rid, success=_success, terminal_reason=_reason)
        _after = _mb.get_gauge("active_runs", {"agent_id": "hap"})
        assert_true(_after == _want_gauge,
                    f"on_run_end {_reason}: active_runs={_want_gauge}"
                    f"（{'仍活跃' if _want_gauge else '成功终止已释放'}）")
        _rspan = [s for s in _tb.get_spans() if s.span_type == SpanType.RUN][-1]
        assert_true(_rspan.status == _want_status,
                    f"on_run_end {_reason}: run span={_want_status.value}（不被误记为 error）")
        assert_true((_rspan.attributes or {}).get("terminal_reason") == _reason,
                    f"on_run_end {_reason}: 结束原因写入 span 属性（非空）")

    # ── on_run_start 同 run_id resume 不重复计数 ──
    adapter4_metrics_b = InMemoryMetricsBackend()
    adapter4 = ObservabilityHooksAdapter(
        logger=Logger(backend=_LogBackend(), agent_id="ha4"),
        tracer=Tracer(backend=InMemoryTracerBackend(), trace_level=TraceLevel.FULL, agent_id="ha4"),
        metrics=Metrics(backend=adapter4_metrics_b, agent_id="ha4"),
    )
    run_id4 = generate_id()
    adapter4.on_run_start("task", run_id4)
    adapter4.on_run_start("task", run_id4)  # resume，同一 run_id
    gauge_resume = adapter4_metrics_b.get_gauge("active_runs", {"agent_id": "ha4"})
    assert_true(gauge_resume == 1.0, "on_run_start resume 同 run_id 不重复计数（active_runs 仍为 1）")

    # ── on_run_end: 异常终止时兜底关闭未关闭子 span（Bug 3 Fix） ──
    adapter5_tracer_b = InMemoryTracerBackend()
    adapter5 = ObservabilityHooksAdapter(
        logger=Logger(backend=_LogBackend(), agent_id="ha5"),
        tracer=Tracer(backend=adapter5_tracer_b, trace_level=TraceLevel.FULL, agent_id="ha5"),
        metrics=Metrics(backend=InMemoryMetricsBackend(), agent_id="ha5"),
    )
    run_id5 = generate_id()
    adapter5.on_run_start("task", run_id5)
    adapter5.on_step_start(1, run_id5)
    adapter5.on_before_llm_call([{"role": "user", "content": "hi"}], run_id5, model="m1")
    # 模拟异常终止（不先调用 on_after_llm_call）
    adapter5.on_run_end(run_id5, success=False, terminal_reason="error")

    # 所有 span 都应被写入（包括异常关闭的 span）
    all_spans5 = adapter5_tracer_b.get_spans()
    assert_true(len(all_spans5) >= 1, "on_run_end 异常终止: 兜底关闭未完成的子 span")
    run_span5_list = [s for s in all_spans5 if s.span_type == SpanType.RUN]
    assert_true(len(run_span5_list) == 1, "on_run_end 异常终止: run span 最终被写入")


# ════════════════════════════════════════════════════
#  main
# ════════════════════════════════════════════════════

SECTIONS = {
    "types": test_types,
    "audit": test_audit_log,
    "dual_audit": test_dual_audit_backend,
    "in_memory_audit": test_in_memory_audit_backend,
    "logger": test_logger,
    "tracer": test_tracer,
    "in_memory_tracer": test_in_memory_tracer_backend,
    "metrics": test_metrics,
    "in_memory_metrics": test_in_memory_metrics_backend,
    "config": test_observability_config,
    "provider": test_observability_provider,
    "hooks": test_hooks_adapter,
}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Observability Mock Tests")
    parser.add_argument("--section", help=f"运行指定测试节（{', '.join(SECTIONS.keys())}）")
    args = parser.parse_args()

    print("=" * 60)
    print("🐼  Pandaren SDK — Observability 模块 Mock 测试")
    print("=" * 60)

    if args.section:
        if args.section not in SECTIONS:
            print(f"未知 section: {args.section}，可选: {', '.join(SECTIONS.keys())}")
            sys.exit(1)
        SECTIONS[args.section]()
        ok = result.summary(args.section)
    else:
        for name, fn in SECTIONS.items():
            fn()
        ok = result.summary("全部")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
