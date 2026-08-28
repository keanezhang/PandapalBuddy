"""pandaren/tests/test_observability_fixes.py — observability 近期 5 项修复的回归测试。

设计文档来源：docs/代码总结/pandaren-modules/09-observability-tests.md（已定稿，20 用例，Group 1-5）
  - Group 1 顶层导出面（修复 1）：E1 / E2
  - Group 2 InMemoryTracerBackend 线程安全（修复 2）：I1 / I2 / I3
  - Group 3 Logger 降级留痕（修复 3）：L1 / L2 / L3
  - Group 4 Markdown 后端 surrogate 防御（修复 4）：M1 / M2 / M3 / M4
  - Group 5 AuditLog HC4 传播语义（修复 5）：A1 / A2 / A3 / A4 / A5 / A6 / A7 / A8

已知差距 KG-1：markdown.py 单行 surrogateescape 防御仅覆盖 U+DC80-DCFF，
EC-S2/S3/S4 用例标 xfail(strict=True)——修复后「意外通过」即报警。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

import pytest

from pandaren.observability import (
    AuditEventType, AuditLog, AuditRecord, AuditSeverity, AuditWriteError,
    DualAuditBackend, InMemoryAuditBackend, InMemoryTracerBackend, LogLevel,
    Logger, MarkdownAuditBackend, MarkdownLoggerBackend, MarkdownTracerBackend,
    ObservabilityError, Span, SpanType,
)

# ════════════════════════════════════════════════════════════════
# Group 1 — 顶层导出面（修复 1）
# ════════════════════════════════════════════════════════════════

# 权威清单（backend/__init__.py:35-44）：四组 Console/InMemory/Markdown/SQLite × 四子系统
BACKENDS = [
    # Console
    "ConsoleAuditBackend", "ConsoleTracerBackend", "ConsoleMetricsBackend", "ConsoleLoggerBackend",
    # InMemory
    "InMemoryAuditBackend", "InMemoryTracerBackend", "InMemoryMetricsBackend", "InMemoryLoggerBackend",
    # Markdown
    "MarkdownAuditBackend", "MarkdownTracerBackend", "MarkdownMetricsBackend", "MarkdownLoggerBackend",
    # SQLite
    "SQLiteAuditBackend", "SQLiteTracerBackend", "SQLiteMetricsBackend", "SQLiteLoggerBackend",
]


def test_e1_exported_backend_set_matches_backend_module():
    """E1: 顶层与 backend 模块的 __all__ 后端子集完全一致（inv-1 + R1 ImportError 回归）"""
    import pandaren.observability as obs
    import pandaren.observability.backend as bk

    obs_backends = [n for n in obs.__all__ if n in BACKENDS]
    bk_backends = [n for n in bk.__all__ if n in BACKENDS]

    assert set(obs_backends) == set(bk_backends) == set(BACKENDS)
    assert len(obs_backends) == 16


def test_e2_top_level_imports_and_class_identity():
    """E2: 16 后端名可顶层 import 且类身份一致（inv-2 + R1）"""
    import pandaren.observability as obs
    import pandaren.observability.backend as bk
    # 曾真实缺失的两个，重点直接 import 验证
    from pandaren.observability import InMemoryLoggerBackend, MarkdownLoggerBackend  # noqa: F401

    for name in BACKENDS:
        assert hasattr(obs, name)
        assert getattr(obs, name) is getattr(bk, name)


# ════════════════════════════════════════════════════════════════
# Group 2 — InMemoryTracerBackend 线程安全（修复 2）
# ════════════════════════════════════════════════════════════════

def _make_span(span_id: str, run_id: str = "r") -> Span:
    return Span(
        span_id=span_id, trace_id="t", parent_span_id=None, span_type=SpanType.RUN,
        name=f"n{span_id}", agent_id="a", run_id=run_id,
    )


def test_i1_inmemory_tracer_sequential_semantics():
    """I1: export/get/get(run_id)/clear 顺序语义 golden（inv-4 + 防回归基座）"""
    backend = InMemoryTracerBackend()
    s1 = _make_span("s1", run_id="rA")
    s2 = _make_span("s2", run_id="rB")
    s3 = _make_span("s3", run_id="rA")

    backend.export_span(s1)
    backend.export_span(s2)
    backend.export_span(s3)

    assert backend.get_spans() == [s1, s2, s3]                      # 插入序
    assert [s.span_id for s in backend.get_spans("rA")] == ["s1", "s3"]  # run_id 精确、保序
    assert backend.get_spans("rX") == []                            # 不匹配返回空
    backend.clear()
    assert backend.get_spans() == []                                # clear 后为空


def test_i2_inmemory_tracer_max_spans_trimming():
    """I2: max_spans 超限裁剪，丢弃最旧保留最新（inv-5 + R2 切片赋值丢数据回归）"""
    backend = InMemoryTracerBackend(max_spans=5)
    for i in range(1, 9):
        backend.export_span(_make_span(f"s{i}"))

    got = backend.get_spans()
    assert len(got) == 5
    assert [s.span_id for s in got] == ["s4", "s5", "s6", "s7", "s8"]


def test_i3_concurrent_export_clear_get_no_error_no_foreign():
    """I3: 并发 export+clear+get 无异常、无串数据、终态确定（inv-3 + inv-4 + R2）"""
    backend = InMemoryTracerBackend()
    errors: list[BaseException] = []
    exported: set[str] = set()
    foreign: list[tuple[str, str]] = []
    exported_lock = threading.Lock()
    collect_lock = threading.Lock()
    start = threading.Barrier(11)  # 8 写 + 1 清 + 2 读

    def writer(w: int) -> None:
        try:
            start.wait()
            for i in range(200):
                span = _make_span(f"w{w}-{i}", run_id=f"r{w}")
                with exported_lock:
                    exported.add(span.span_id)
                backend.export_span(span)
        except BaseException as e:  # 任何异常都是回归信号
            with collect_lock:
                errors.append(e)

    def clearer() -> None:
        try:
            start.wait()
            for _ in range(100):
                backend.clear()
        except BaseException as e:
            with collect_lock:
                errors.append(e)

    def reader() -> None:
        try:
            start.wait()
            for _ in range(50):
                spans = backend.get_spans()
                with exported_lock:
                    snapshot = set(exported)
                for s in spans:
                    if s.span_id not in snapshot:
                        with collect_lock:
                            foreign.append(("foreign", s.span_id))
        except BaseException as e:
            with collect_lock:
                errors.append(e)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(8)]
    threads += [threading.Thread(target=clearer)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "线程未在 join(timeout=30) 内退出"

    assert errors == []   # 修复前此负载高概率抛 RuntimeError: list changed size during iteration
    assert foreign == []  # 无串数据：get_spans 返回集 ⊆ 已 export 集

    # 终态确定性收尾：clear 后 export 3 条 → 恰 3 条，结构未被并发破坏
    backend.clear()
    for i in range(3):
        backend.export_span(_make_span(f"final{i}", run_id="rf"))
    assert [s.span_id for s in backend.get_spans()] == ["final0", "final1", "final2"]


# ════════════════════════════════════════════════════════════════
# Group 3 — Logger 降级留痕（修复 3）
# ════════════════════════════════════════════════════════════════

class BoomBackend:
    """write_log 必抛的 Fake——注入写失败。"""

    def write_log(self, record: dict) -> None:
        raise RuntimeError("boom write")


class RecordBackend:
    """记录收到内容的 Fake——副作用可审计。"""

    def __init__(self, records: list) -> None:
        self._records = records

    def write_log(self, record: dict) -> None:
        self._records.append(record)


class CountingBackend:
    """计数 Fake——验证后端零调用。"""

    def __init__(self) -> None:
        self.calls = 0

    def write_log(self, record: dict) -> None:
        self.calls += 1


def test_l1_write_failure_does_not_propagate_and_logs(caplog):
    """L1: 写失败不传播 + caplog debug 留痕 exc_info（inv-6 + inv-7 + R3）"""
    caplog.set_level(logging.DEBUG, logger="pandaren.observability.logger")
    logger = Logger(backend=BoomBackend())

    logger.info("hello")  # 不捕获异常——若传播则用例失败

    debug_records = [
        r for r in caplog.records
        if r.levelname == "DEBUG" and r.name == "pandaren.observability.logger"
    ]
    assert len(debug_records) == 1
    assert debug_records[0].getMessage() == "observability logger write failed"
    assert debug_records[0].exc_info is not None


def test_l2_happy_path_structured_record_fields():
    """L2: happy path 结构化记录字段完整 + 写入后端（inv-6 正面 + R3）"""
    records: list[dict] = []
    logger = Logger(backend=RecordBackend(records), agent_id="ag9")

    logger.info("hi", module="m", run_id="run1", session_id="s1", step_n=3, extra_key="v")

    assert len(records) == 1
    r = records[0]
    assert r["level"] == "INFO"
    assert r["message"] == "hi"
    assert r["module"] == "m"
    assert r["agent_id"] == "ag9"      # 默认取自构造参数
    assert r["run_id"] == "run1"
    assert r["session_id"] == "s1"
    assert r["step_n"] == 3
    assert r["extra_key"] == "v"       # 自定义 extra 透传
    assert isinstance(r["timestamp"], datetime)
    assert r["timestamp"].tzinfo == timezone.utc
    assert isinstance(r["log_id"], str) and r["log_id"]


def test_l3_min_level_filters_below_threshold():
    """L3: min_level 之下的级别早退，后端零调用（_should_log B1 分支 + R3 邻接）"""
    cb = CountingBackend()
    logger = Logger(backend=cb, min_level=LogLevel.INFO)

    logger.debug("skipped")   # DEBUG=10 < INFO=20 → 早退
    logger.info("written")    # INFO=20 ≥ 20 → 写入

    assert cb.calls == 1      # 恰好 1 次：非 0 也非 2


# ════════════════════════════════════════════════════════════════
# Group 4 — Markdown 后端 surrogate 防御（修复 4）
# ════════════════════════════════════════════════════════════════

# surrogate 一律用 chr()/转义构造，禁止源码非法字面量。
# EC-S2/S3/S4 标 [known-gap]：markdown.py 单行 surrogateescape 仅覆盖 U+DC80-DCFF，
# 高代理/低代理其余/surrogate pair 仍抛 UnicodeEncodeError → xfail(strict=True)。
_XFAIL_KG1 = pytest.mark.xfail(
    reason="KG-1: surrogateescape 仅覆盖 U+DC80-DCFF（S2/S3/S4 后端防御缺失，抛 UnicodeEncodeError）",
    strict=True,
)

SURROGATE_CASES = [
    pytest.param(chr(0xDC80), "\ufffd", id="EC-S1"),    # surrogateescape 解码产物（PEP 383 范围）→ pass
    pytest.param(chr(0xD800), None, marks=_XFAIL_KG1, id="EC-S2"),    # 高代理 → xfail
    pytest.param(chr(0xDFFF), None, marks=_XFAIL_KG1, id="EC-S3"),    # 低代理其余 → xfail
    pytest.param("\ud83d\ude00", None, marks=_XFAIL_KG1, id="EC-S4"),  # surrogate pair → xfail
    pytest.param("ok", "ok", id="EC-S5"),               # 正常 BMP 文本 → 防御不误伤、pass
]


@pytest.mark.parametrize("ch, file_fragment", SURROGATE_CASES)
def test_m1_tracer_surrogate_persists_and_memory_keeps_raw(tmp_path, ch, file_fragment):
    """M1: tracer surrogate 成功落盘 + 内存镜像保留原值（inv-8 + inv-9 + R4 + KG-1）"""
    backend = MarkdownTracerBackend(tmp_path)
    span = Span(
        span_id="s1", trace_id="t1", parent_span_id=None, span_type=SpanType.LLM_CALL,
        name=f"llm_{ch}call", agent_id="ag1", run_id="r1",
        end_time=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        attributes={"model": f"gpt-{ch}", "input_tokens": 10},
    )
    span_empty = Span(
        span_id="s2", trace_id="t1", parent_span_id=None, span_type=SpanType.LLM_CALL,
        name="empty_attrs", agent_id="ag1", run_id="r1",
        end_time=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        attributes={},   # B2 分支：空 attributes 正常渲染
    )
    backend.export_span(span)
    backend.export_span(span_empty)

    text = (tmp_path / "_no_session" / "traces.md").read_text(encoding="utf-8")
    assert f"gpt-{file_fragment}" in text          # attributes 值替换为 U+FFFD（或原文）
    assert f"llm_{file_fragment}call" in text      # name 同样替换
    assert "\ud800" not in text                    # 无任何残留 surrogate
    assert "\udc80" not in text
    assert "empty_attrs" in text                   # 空 attributes 不误伤
    assert "| 时间 | 类型 |" in text               # 整条记录行未丢，仅字符被替换

    mem = backend.get_spans()
    assert mem[0].attributes["model"] == f"gpt-{ch}"  # inv-9: 内存保留原始 surrogate
    assert len(mem) == 2


@pytest.mark.parametrize("ch, file_fragment", SURROGATE_CASES)
def test_m2_logger_surrogate_persists_with_escaping(tmp_path, ch, file_fragment):
    """M2: logger surrogate 成功落盘，`|`/换行转义不误伤（inv-8 + R4 + KG-1）"""
    backend = MarkdownLoggerBackend(tmp_path)
    record = {
        "timestamp": datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        "level": "INFO",
        "module": "m",
        "message": f"line1\nwith|pipe {ch}",
        "agent_id": "ag1",
        "run_id": "run1",
        "session_id": "",
        "step_n": 1,
        "extra": {"model": f"x{ch}"},
    }
    backend.write_log(record)

    text = (tmp_path / "_no_session" / "logs.md").read_text(encoding="utf-8")
    assert f"with\\|pipe {file_fragment}" in text   # message 的 surrogate → U+FFFD（`|` 已转义）
    assert "line1" in text                          # 换行被压平
    assert "with\\|pipe" in text                    # `|` 转义形态保留
    assert f"x{file_fragment}" in text              # extra JSON 值同样被替换
    assert "\ud800" not in text                     # 无残留 surrogate


@pytest.mark.parametrize("ch, file_fragment", SURROGATE_CASES)
def test_m3_audit_backend_surrogate_persists(tmp_path, ch, file_fragment):
    """M3: audit 后端 detail surrogate 直接落盘（inv-8 三后端一致 + R4 + KG-1）"""
    backend = MarkdownAuditBackend(tmp_path)
    record = AuditRecord(
        timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        record_id="rid1",
        event_type=AuditEventType.RUN_STARTED,
        severity=AuditSeverity.INFO,
        agent_id="ag1",
        run_id="run1",
        detail=f"detail {ch}",
    )
    backend.write(record)

    text = (tmp_path / "_no_session" / "audit.md").read_text(encoding="utf-8")
    assert f"detail {file_fragment}" in text        # detail 的 surrogate → U+FFFD（或原文）
    assert "\ud800" not in text                     # 无残留 surrogate
    assert "\udc80" not in text
    assert "| 时间 | 级别 |" in text                # 整行落盘


def test_m4_logger_facade_full_chain_success(tmp_path, caplog):
    """M4-EC-S1: 全链路成功路径——无留痕误报 + 记录落盘（inv-6 + inv-7 + inv-8 + R3 + R4）"""
    caplog.set_level(logging.DEBUG, logger="pandaren.observability.logger")
    logger = Logger(backend=MarkdownLoggerBackend(tmp_path))

    logger.info(f"msg {chr(0xDC80)}")   # 不传播（inv-6）

    assert not [r for r in caplog.records if "write failed" in r.getMessage()]  # 成功不误报
    text = (tmp_path / "_no_session" / "logs.md").read_text(encoding="utf-8")
    assert "msg \ufffd" in text          # 记录成功落盘


def test_m4_logger_facade_full_chain_failure(tmp_path, caplog):
    """M4-EC-S2: 失败路径——不传播 + caplog 留痕（pass）；文件落盘 [known-gap] xfail（KG-1）"""
    caplog.set_level(logging.DEBUG, logger="pandaren.observability.logger")
    logger = Logger(backend=MarkdownLoggerBackend(tmp_path))

    logger.info(f"msg {chr(0xD800)}")   # 不传播（inv-6），传播即失败

    failed = [r for r in caplog.records
              if r.getMessage() == "observability logger write failed"]
    assert len(failed) == 1             # inv-7 留痕机制正常（Fail-Safe 把后端抛错转 debug 留痕）
    assert failed[0].exc_info is not None

    # 记录已丢（后端防御缺失）——修复后此处应转为落盘成功，测试需更新
    pytest.xfail("KG-1: surrogateescape 仅覆盖 U+DC80-DCFF——S2 记录落盘为已知差距")
    text = (tmp_path / "_no_session" / "logs.md").read_text(encoding="utf-8")
    assert "msg \ufffd" in text


# ════════════════════════════════════════════════════════════════
# Group 5 — AuditLog HC4 传播语义（修复 5）
# ════════════════════════════════════════════════════════════════

class BoomWrite:
    """write 必抛的 Fake——注入写入失败。"""

    def write(self, record: AuditRecord) -> None:
        raise RuntimeError("simulated disk failure")

    def flush(self) -> None:
        pass

    def query(self, **kwargs) -> list:
        return []


class BoomFlush:
    """flush 必抛的 Fake——write 成功但 flush 失败。"""

    def write(self, record: AuditRecord) -> None:
        pass

    def flush(self) -> None:
        raise IOError("flush boom")

    def query(self, **kwargs) -> list:
        return []


class FlushSpy:
    """包装真实后端并计数 flush 调用的 Fake。"""

    def __init__(self, inner: InMemoryAuditBackend) -> None:
        self._inner = inner
        self.flush_calls = 0

    def write(self, record: AuditRecord) -> None:
        self._inner.write(record)

    def flush(self) -> None:
        self.flush_calls += 1
        self._inner.flush()

    def query(self, **kwargs) -> list:
        return self._inner.query(**kwargs)


def test_a1_write_failure_fallback_and_raise(capsys):
    """A1: write 失败 → stderr AUDIT_FALLBACK + raise AuditWriteError（cause 链）（inv-11 + R5 + R6）"""
    al = AuditLog(backend=BoomWrite())

    with pytest.raises(AuditWriteError) as ei:
        al.write_sync(AuditEventType.RUN_STARTED, agent_id="ag1", run_id="r1", detail="d")

    assert isinstance(ei.value.__cause__, RuntimeError)   # raise ... from e 链存在
    assert "run_started" in str(ei.value)                 # 错误消息含定位信息
    assert "ag1" in str(ei.value)

    err = capsys.readouterr().err
    fallback_lines = [line for line in err.splitlines() if "AUDIT_FALLBACK" in line]
    assert len(fallback_lines) == 1                       # 恰 1 行
    data = json.loads(fallback_lines[0])
    assert data["AUDIT_FALLBACK"] is True
    assert data["event_type"] == "run_started"
    assert data["agent_id"] == "ag1"
    assert data["run_id"] == "r1"
    assert "simulated disk failure" in data["original_error"]


def test_a2_flush_failure_fallback_and_raise(capsys):
    """A2: flush 失败（write 成功）→ 同样 fallback + raise（inv-10 + inv-11 + R5 + R6）"""
    al = AuditLog(backend=BoomFlush())

    with pytest.raises(AuditWriteError) as ei:
        al.write_sync(AuditEventType.RUN_STARTED, agent_id="a", run_id="r", detail="d")

    assert isinstance(ei.value.__cause__, IOError)        # write 成功 ≠ 提交成功

    err = capsys.readouterr().err
    fallback_lines = [line for line in err.splitlines() if "AUDIT_FALLBACK" in line]
    assert len(fallback_lines) == 1
    data = json.loads(fallback_lines[0])
    assert data["AUDIT_FALLBACK"] is True
    assert "flush boom" in data["original_error"]


def test_a3_audit_write_error_independent_base():
    """A3: AuditWriteError 独立基类，防上层 except ObservabilityError 误吞（inv-12 + R7）"""
    assert issubclass(AuditWriteError, ObservabilityError) is False  # 核心断言
    assert issubclass(AuditWriteError, Exception) is True


def test_a4_success_path_atomic_and_facade_sanitize():
    """A4: 成功路径 write+flush 原子 + facade 全范围 surrogate 清洗（inv-10 + inv-14 + R5 正面）"""
    inner = InMemoryAuditBackend()
    spy = FlushSpy(inner)
    al = AuditLog(backend=spy)

    al.write_sync(AuditEventType.RUN_STARTED, agent_id="ag1", run_id="r1", detail="bad\ud800detail")

    assert spy.flush_calls == 1                            # flush 被原子调用
    recs = inner.query()
    assert len(recs) == 1
    rec = recs[0]
    assert rec.event_type == AuditEventType.RUN_STARTED
    assert rec.agent_id == "ag1"
    assert rec.run_id == "r1"
    assert rec.detail == "bad?detail"                      # facade 清洗全范围：\ud800 → '?'
    assert rec.severity == AuditSeverity.INFO              # _DEFAULT_SEVERITY 表查得
    assert isinstance(rec.timestamp, datetime)
    assert rec.timestamp.tzinfo == timezone.utc


def test_a5_dual_audit_backend_dual_write_and_delegation():
    """A5: DualAuditBackend 双写 + query/flush 委托 primary/secondary（inv-13 + R8）"""
    p = InMemoryAuditBackend()
    s = InMemoryAuditBackend()
    dual = DualAuditBackend(p, s)

    record = AuditRecord(
        timestamp=datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc),   # 较新 → 倒序在前
        record_id="rid-dup", event_type=AuditEventType.RUN_STARTED,
        severity=AuditSeverity.INFO, agent_id="ag1", run_id="r1", detail="d",
    )
    other = AuditRecord(
        timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        record_id="rid-other", event_type=AuditEventType.RUN_FINISHED,
        severity=AuditSeverity.INFO, agent_id="ag2", run_id="r2", detail="d",
    )
    dual.write(record)
    p.write(other)                                          # 仅 primary 有第二条

    assert len(p.query()) == 2
    assert len(s.query()) == 1
    assert p.query()[0].record_id == s.query()[0].record_id == "rid-dup"
    from_dual = dual.query()
    assert {r.record_id for r in from_dual} == {"rid-dup", "rid-other"}  # 委托 primary

    p2 = FlushSpy(InMemoryAuditBackend())
    s2 = FlushSpy(InMemoryAuditBackend())
    dual2 = DualAuditBackend(p2, s2)
    dual2.flush()
    assert p2.flush_calls == 1
    assert s2.flush_calls == 1


def _seed_audit_records(backend: InMemoryAuditBackend) -> None:
    """直接 backend.write 注入 5 条固定 tz-aware 时间戳记录（不经 write_sync，钉死时间）。"""
    specs = [
        (0, AuditEventType.RUN_STARTED, "alice"),
        (10, AuditEventType.RUN_FINISHED, "alice"),
        (20, AuditEventType.PERMISSION_DENIED, "bob"),
        (30, AuditEventType.RUN_STARTED, "bob"),
        (40, AuditEventType.RUN_STARTED, "alice"),
    ]
    for sec, event_type, agent_id in specs:
        backend.write(AuditRecord(
            timestamp=datetime(2026, 1, 1, 0, 0, sec, tzinfo=timezone.utc),
            record_id=f"rid-{sec}", event_type=event_type,
            severity=AuditSeverity.INFO, agent_id=agent_id, run_id="r", detail="d",
        ))


def test_a6_query_records_single_dimension():
    """A6: query_records 单维过滤——agent 精确 / event value 匹配 / 时间窗闭区间（inv-14 + R9）"""
    backend = InMemoryAuditBackend()
    log = AuditLog(backend=backend)
    _seed_audit_records(backend)

    by_agent = log.query_records(agent_id="alice")
    assert [(r.timestamp.second, r.event_type.value) for r in by_agent] == [
        (40, "run_started"), (10, "run_finished"), (0, "run_started"),
    ]

    by_event = log.query_records(event_type="run_started")
    assert [r.agent_id for r in by_event] == ["alice", "bob", "alice"]

    by_window = log.query_records(
        start_time="2026-01-01T00:00:10+00:00",
        end_time="2026-01-01T00:00:30+00:00",
    )
    assert [(r.timestamp.second, r.event_type.value) for r in by_window] == [
        (30, "run_started"), (20, "permission_denied"), (10, "run_finished"),
    ]                                                       # 闭区间：端点 10 与 30 都被包含

    assert log.query_records(agent_id="nobody") == []       # 无匹配返回空


def test_a7_query_records_combined_sort_limit_empty():
    """A7: query_records 组合 AND + 倒序 + limit 截断 + 全量（inv-14 + R9）"""
    backend = InMemoryAuditBackend()
    log = AuditLog(backend=backend)
    _seed_audit_records(backend)

    combined = log.query_records(
        agent_id="bob",
        start_time="2026-01-01T00:00:10+00:00",
        end_time="2026-01-01T00:00:30+00:00",
    )
    assert [(r.timestamp.second, r.event_type.value) for r in combined] == [
        (30, "run_started"), (20, "permission_denied"),
    ]

    limited = log.query_records(limit=2)
    assert [r.timestamp.second for r in limited] == [40, 30]   # 全量倒序后截断前 2

    all_records = log.query_records()
    assert [r.timestamp.second for r in all_records] == [40, 30, 20, 10, 0]


def test_a8_auditlog_none_backend_raises_value_error():
    """A8: AuditLog(backend=None) → ValueError（HC4 不可关闭）（inv-15）"""
    with pytest.raises(ValueError) as ei:
        AuditLog(backend=None)

    assert "不可为 None" in str(ei.value)
    assert "HC4" in str(ei.value)
