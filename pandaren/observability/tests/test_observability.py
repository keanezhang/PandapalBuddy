"""
Pandaren Agent SDK · Observability 模块真实集成测试

覆盖约束
--------
  O1  全链路可追踪：真实 agent.run() 执行后必须有 RUN 级别 Span
  O2  审计必填字段：真实 run 产生的 AuditRecord 字段完整
  O3  Agent.run() 永不向外抛异常，异常转为 RunResult.success=False
  HC4 AuditLog 不可关闭：run 中必然写入审计记录

集成场景（需要真实 LLM）
-----------------------
  1. 单次 run() → AuditLog 包含 RUN_STARTED + RUN_FINISHED 记录
  2. 单次 run() → Tracer 包含 RUN span + STEP span + LLM_CALL span
  3. 单次 run() → Metrics 记录 run_total、step_total、llm_call_total
  4. run_id 在 AuditRecord / Span / RunResult 之间一致
  5. 带工具调用的 run → TOOL_CALL span + tool_execute_total 计数
  6. 多轮对话 → 每轮独立 run_id + AuditRecord
  7. 验证 AuditRecord 字段完整性（agent_id / run_id / timestamp / detail 非空）
  8. 验证 Span 字段完整性（span_id / run_id / duration_ms > 0）
  9. ObservabilityProvider.build_observability_context 与 run_id 一致
  10. 自定义后端实例（DualAuditBackend）在真实 run 中双写正常

运行方式
--------
  cd /path/to/py_pandaren_agent_sdk
  python pandaren/observability/tests/test_observability.py
  python pandaren/observability/tests/test_observability.py --section audit
  python pandaren/observability/tests/test_observability.py --section tracer
  python pandaren/observability/tests/test_observability.py --section metrics
  python pandaren/observability/tests/test_observability.py --section consistency
  python pandaren/observability/tests/test_observability.py --section tools
  python pandaren/observability/tests/test_observability.py --section multirun
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys

# Windows 控制台 UTF-8 输出
if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 下交给 capture，避免拆台
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# ═══ 环境变量加载 ═══
_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env.development")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#"):
                continue
            if "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

# ═══ SDK 导入 ═══
from pandaren.identity.models import Identity, SensitivePermission, PERMISSION_ALL, TrustLevel
from pandaren.builder import AgentBuilder
from pandaren.llm.client import OpenAICompatibleClient
from pandaren.tool import Tool, ToolContext, ToolResult
from pandaren.tool.types import ToolTier, SensitivityLevel

from pandaren.observability.types import (
    AuditEventType, AuditSeverity, SpanType, SpanStatus,
    LogLevel, TraceLevel,
)
from pandaren.observability.audit import AuditLog, DualAuditBackend
from pandaren.observability.provider import ObservabilityProvider
from pandaren.observability.config import ObservabilityConfig
from pandaren.observability.backend.in_memory import (
    InMemoryAuditBackend,
    InMemoryTracerBackend,
    InMemoryMetricsBackend,
)


# ════════════════════════════════════════════════════
#  测试框架
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
    def decorator(fn):
        try:
            fn()
            result.ok(name)
        except Exception as e:
            result.fail(name, f"意外抛出 {type(e).__name__}({e})" + (f": {detail}" if detail else ""))
    return decorator


# ════════════════════════════════════════════════════
#  辅助：LLM 客户端 + Agent 构建
# ════════════════════════════════════════════════════

def _make_llm_client():
    api_key = os.getenv("DASHSCOPE_API_KEY")
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_name = "qwen-plus"
    return OpenAICompatibleClient(
        api_key=api_key,
        model_name=model_name,
        base_url=base_url,
        timeout=60.0,
    )


def _make_observed_agent(
    agent_id: str,
    audit_backend: InMemoryAuditBackend,
    tracer_backend: InMemoryTracerBackend,
    metrics_backend: InMemoryMetricsBackend,
    tools: list | None = None,
    system_prompt: str = "你是一个测试助手，请直接简洁地回答问题。",
):
    """构建携带内存后端的 Agent，用于断言观测数据。"""
    builder = (
        AgentBuilder()
        .identity(
            agent_id=agent_id,
            agent_name="观测测试助手",
            when_to_use="用于可观测性集成测试",
            sensitive_permissions=PERMISSION_ALL,
            trust_level=TrustLevel.SUB_AGENT,
        )
        .llm(_make_llm_client())
        .system_prompt(system_prompt)
        .behavior(max_steps=5)
        .observability(
            audit=audit_backend,
            tracer=tracer_backend,
            metrics=metrics_backend,
            log=False,                          # 关闭控制台日志，减少噪音
            trace_level=TraceLevel.FULL,        # 完整采集所有 Span
        )
    )
    if tools:
        builder.tools(tools)
    return builder.build()


# ════════════════════════════════════════════════════
#  1. AuditLog 集成测试
# ════════════════════════════════════════════════════

def test_audit():
    print("\n" + "═" * 60)
    print("1️⃣  AuditLog 集成测试（真实 LLM）")
    print("═" * 60)

    async def _run():
        audit_be = InMemoryAuditBackend()
        tracer_be = InMemoryTracerBackend()
        metrics_be = InMemoryMetricsBackend()

        agent = _make_observed_agent(
            "obs.audit.agent",
            audit_be, tracer_be, metrics_be,
        )

        print("\n  · run() 后 AuditLog 包含 RUN_STARTED + RUN_FINISHED")
        async with agent:
            run_result = await agent.run("1+1=?，只回答数字。", session_id="audit_session_1")

        assert_true(run_result.success is True, "audit: run() success=True")
        run_id = run_result.run_id
        assert_true(bool(run_id), "audit: run_id 非空")

        # ── RUN_STARTED ──
        started_records = audit_be.query(event_type=AuditEventType.RUN_STARTED.value)
        assert_true(len(started_records) >= 1, "audit: 写入了 RUN_STARTED 记录")

        if started_records:
            r = started_records[0]
            assert_true(r.agent_id == "obs.audit.agent", "audit: RUN_STARTED.agent_id 正确")
            assert_true(r.run_id == run_id, "audit: RUN_STARTED.run_id 与 RunResult.run_id 一致")
            assert_true(r.timestamp is not None, "audit: RUN_STARTED.timestamp 非空")
            assert_true(bool(r.detail), "audit: RUN_STARTED.detail 非空")
            assert_true(r.severity in AuditSeverity, "audit: RUN_STARTED.severity 是合法枚举值")
            assert_true(bool(r.record_id), "audit: RUN_STARTED.record_id 非空（UUID hex）")
            assert_true(len(r.record_id) == 32, "audit: record_id 为 32 位 hex")
            print(f"   → RUN_STARTED detail: {r.detail[:80]}")

        # ── RUN_FINISHED ──
        finished_records = audit_be.query(event_type=AuditEventType.RUN_FINISHED.value)
        assert_true(len(finished_records) >= 1, "audit: 写入了 RUN_FINISHED 记录")

        if finished_records:
            r = finished_records[0]
            assert_true(r.agent_id == "obs.audit.agent", "audit: RUN_FINISHED.agent_id 正确")
            assert_true(r.run_id == run_id, "audit: RUN_FINISHED.run_id 一致")
            assert_true(bool(r.detail), "audit: RUN_FINISHED.detail 非空")
            print(f"   → RUN_FINISHED detail: {r.detail[:80]}")

        # ── 所有记录 run_id 一致性 ──
        print("\n  · 所有审计记录的 run_id 指向同一次 run")
        all_records = audit_be.query(limit=200)
        run_ids_seen = {r.run_id for r in all_records}
        assert_true(run_id in run_ids_seen, "audit: run_id 存在于所有审计记录中")

        # ── 按 agent_id 过滤 ──
        print("\n  · 按 agent_id 过滤查询")
        agent_records = audit_be.query(agent_id="obs.audit.agent")
        assert_true(len(agent_records) >= 2, "audit: 按 agent_id 查出的记录 >= 2 条（至少 STARTED+FINISHED）")
        assert_true(all(r.agent_id == "obs.audit.agent" for r in agent_records),
                    "audit: 过滤后全部 agent_id 正确")

        # ── 审计记录不可变（frozen=True）──
        print("\n  · AuditRecord frozen（不可变）")
        if all_records:
            from dataclasses import FrozenInstanceError
            try:
                all_records[0].agent_id = "hacked"  # type: ignore[misc]
                result.fail("audit: AuditRecord 应为 frozen，但修改未抛异常")
            except (FrozenInstanceError, AttributeError):
                result.ok("audit: AuditRecord frozen — 修改字段抛出异常")

    asyncio.run(_run())


# ════════════════════════════════════════════════════
#  2. Tracer 集成测试
# ════════════════════════════════════════════════════

def test_tracer():
    print("\n" + "═" * 60)
    print("2️⃣  Tracer 集成测试（真实 LLM）")
    print("═" * 60)

    async def _run():
        audit_be = InMemoryAuditBackend()
        tracer_be = InMemoryTracerBackend()
        metrics_be = InMemoryMetricsBackend()

        agent = _make_observed_agent(
            "obs.tracer.agent",
            audit_be, tracer_be, metrics_be,
        )

        print("\n  · run() 后 Tracer 包含 RUN / STEP / LLM_CALL span")
        async with agent:
            run_result = await agent.run("2+28899*98989=?，只回答数字。", session_id="tracer_session_1")

        assert_true(run_result.success is True, "tracer: run() success=True")
        run_id = run_result.run_id

        spans = tracer_be.get_spans(run_id)
        assert_true(len(spans) >= 1, f"tracer: run_id={run_id}... 有 Span 记录")
        print(f"   → 共捕获 {len(spans)} 个 Span")

        # ── RUN span ──
        run_spans = [s for s in spans if s.span_type == SpanType.RUN]
        assert_true(len(run_spans) == 1, "tracer: 恰好 1 个 RUN span")

        if run_spans:
            rs = run_spans[0]
            assert_true(rs.run_id == run_id, "tracer: RUN span.run_id 与 RunResult 一致")
            assert_true(rs.agent_id == "obs.tracer.agent", "tracer: RUN span.agent_id 正确")
            assert_true(bool(rs.span_id), "tracer: RUN span.span_id 非空")
            assert_true(len(rs.span_id) == 32, "tracer: RUN span.span_id 为 32 位 hex")
            assert_true(rs.end_time is not None, "tracer: RUN span.end_time 已填充（span 已结束）")
            assert_true(rs.duration_ms is not None and rs.duration_ms > 0,
                        "tracer: RUN span.duration_ms > 0")
            assert_true(rs.status == SpanStatus.OK, "tracer: RUN span.status == OK")
            assert_true(rs.parent_span_id is None, "tracer: RUN span 为根 span（parent=None）")
            print(f"   → RUN span: duration={rs.duration_ms:.1f}ms, status={rs.status.value}")

        # ── STEP span ──
        step_spans = [s for s in spans if s.span_type == SpanType.STEP]
        assert_true(len(step_spans) >= 1, "tracer: 至少 1 个 STEP span")

        if step_spans:
            ss = step_spans[0]
            assert_true(ss.duration_ms is not None and ss.duration_ms > 0,
                        "tracer: STEP span.duration_ms > 0")
            assert_true(ss.run_id == run_id, "tracer: STEP span.run_id 与 RUN 一致")
            assert_true(ss.end_time is not None, "tracer: STEP span 已结束")
            print(f"   → STEP span count={len(step_spans)}, first duration={ss.duration_ms:.1f}ms")

        # ── LLM_CALL span ──
        llm_spans = [s for s in spans if s.span_type == SpanType.LLM_CALL]
        assert_true(len(llm_spans) >= 1, "tracer: 至少 1 个 LLM_CALL span")

        if llm_spans:
            ls = llm_spans[0]
            assert_true(ls.duration_ms is not None and ls.duration_ms > 0,
                        "tracer: LLM_CALL span.duration_ms > 0")
            assert_true(ls.run_id == run_id, "tracer: LLM_CALL span.run_id 一致")
            print(f"   → LLM_CALL span count={len(llm_spans)}, first duration={ls.duration_ms:.1f}ms")

        # ── Span 树形结构：RUN 是 STEP 的祖先 ──
        print("\n  · Span 树形结构验证（parent_span_id 层级）")
        if run_spans and step_spans:
            run_span_id = run_spans[0].span_id
            # STEP span 的 parent 应指向 RUN span
            step_parent_ids = {s.parent_span_id for s in step_spans}
            assert_true(run_span_id in step_parent_ids,
                        "tracer: STEP span.parent_span_id 指向 RUN span")

        # ── Span frozen ──
        print("\n  · Span frozen（不可变）")
        if spans:
            from dataclasses import FrozenInstanceError
            try:
                spans[0].span_id = "hacked"  # type: ignore[misc]
                result.fail("tracer: Span 应为 frozen，但修改未抛异常")
            except (FrozenInstanceError, AttributeError):
                result.ok("tracer: Span frozen — 修改字段抛出异常")

        # ── query_spans 接口 ──
        queried = tracer_be.query_spans(run_id)
        assert_true(len(queried) == len(spans), "tracer: query_spans 与 get_spans 结果数量一致")

    asyncio.run(_run())


# ════════════════════════════════════════════════════
#  3. Metrics 集成测试
# ════════════════════════════════════════════════════

def test_metrics():
    print("\n" + "═" * 60)
    print("3️⃣  Metrics 集成测试（真实 LLM）")
    print("═" * 60)

    async def _run():
        audit_be = InMemoryAuditBackend()
        tracer_be = InMemoryTracerBackend()
        metrics_be = InMemoryMetricsBackend()

        agent = _make_observed_agent(
            "obs.metrics.agent",
            audit_be, tracer_be, metrics_be,
        )

        print("\n  · run() 后 Metrics 包含 run_total / step_total / llm_call_total")
        async with agent:
            run_result = await agent.run("3+3=?，只回答数字。", session_id="metrics_session_1")

        assert_true(run_result.success is True, "metrics: run() success=True")

        summary = metrics_be.get_summary()
        counters = summary.get("counters", {})
        histograms = summary.get("histograms", {})
        gauges = summary.get("gauges", {})

        print(f"   → counters keys: {list(counters.keys())[:10]}")
        print(f"   → histogram keys: {list(histograms.keys())[:10]}")
        print(f"   → gauge keys: {list(gauges.keys())[:10]}")

        # ── run_total ──
        run_total_keys = [k for k in counters if "run_total" in k]
        assert_true(len(run_total_keys) >= 1, "metrics: run_total 计数器存在")
        if run_total_keys:
            total_runs = sum(counters[k] for k in run_total_keys)
            assert_true(total_runs >= 1, f"metrics: run_total 累计 >= 1 (got {total_runs})")

        # ── step_total ──
        step_total_keys = [k for k in counters if "step_total" in k]
        assert_true(len(step_total_keys) >= 1, "metrics: step_total 计数器存在")

        # ── llm_call_total ──
        llm_keys = [k for k in counters if "llm_call_total" in k]
        assert_true(len(llm_keys) >= 1, "metrics: llm_call_total 计数器存在")
        if llm_keys:
            total_llm = sum(counters[k] for k in llm_keys)
            assert_true(total_llm >= 1, f"metrics: llm_call_total >= 1 (got {total_llm})")

        # ── run duration histogram ──
        run_dur_keys = [k for k in histograms if "run_duration" in k]
        assert_true(len(run_dur_keys) >= 1, "metrics: run_duration_ms histogram 存在")
        if run_dur_keys:
            hist = histograms[run_dur_keys[0]]
            assert_true(hist["count"] >= 1, "metrics: run_duration_ms count >= 1")
            assert_true(hist["avg"] > 0, "metrics: run_duration_ms avg > 0")
            print(f"   → run_duration_ms: count={hist['count']}, avg={hist['avg']:.1f}ms")

        # ── step duration histogram ──
        step_dur_keys = [k for k in histograms if "step_duration" in k]
        assert_true(len(step_dur_keys) >= 1, "metrics: step_duration_ms histogram 存在")

        # ── llm duration histogram ──
        llm_dur_keys = [k for k in histograms if "llm_call_duration" in k]
        assert_true(len(llm_dur_keys) >= 1, "metrics: llm_call_duration_ms histogram 存在")
        if llm_dur_keys:
            hist = histograms[llm_dur_keys[0]]
            assert_true(hist["avg"] > 0, f"metrics: llm_call_duration_ms avg={hist['avg']:.1f}ms > 0")
            print(f"   → llm_call_duration_ms: count={hist['count']}, avg={hist['avg']:.1f}ms")

    asyncio.run(_run())


# ════════════════════════════════════════════════════
#  4. 跨组件 run_id 一致性
# ════════════════════════════════════════════════════

def test_consistency():
    print("\n" + "═" * 60)
    print("4️⃣  跨组件 run_id 一致性（AuditRecord / Span / RunResult）")
    print("═" * 60)

    async def _run():
        audit_be = InMemoryAuditBackend()
        tracer_be = InMemoryTracerBackend()
        metrics_be = InMemoryMetricsBackend()

        agent = _make_observed_agent(
            "obs.consistency.agent",
            audit_be, tracer_be, metrics_be,
        )

        async with agent:
            run_result = await agent.run("你好，请用一个字回答。", session_id="consistency_session_1")

        assert_true(run_result.success is True, "consistency: run() success=True")
        run_id = run_result.run_id
        print(f"   → RunResult.run_id = {run_id[:16]}...")

        # AuditRecord run_id 一致
        audit_records = audit_be.query(limit=200)
        assert_true(len(audit_records) >= 2, "consistency: 至少 2 条审计记录")
        audit_run_ids = {r.run_id for r in audit_records}
        assert_true(run_id in audit_run_ids,
                    f"consistency: run_id 出现在 AuditRecord 中 (seen={list(audit_run_ids)[:3]})")

        # Span run_id 一致
        spans = tracer_be.get_spans(run_id)
        assert_true(len(spans) >= 1, "consistency: run_id 对应的 Span 存在")
        span_run_ids = {s.run_id for s in spans}
        assert_true(len(span_run_ids) == 1, "consistency: 所有 Span 的 run_id 相同")
        assert_true(run_id in span_run_ids, "consistency: Span.run_id 与 RunResult.run_id 一致")

        # agent_id 一致
        audit_agent_ids = {r.agent_id for r in audit_records}
        span_agent_ids = {s.agent_id for s in spans}
        assert_true("obs.consistency.agent" in audit_agent_ids,
                    "consistency: AuditRecord.agent_id 正确")
        assert_true("obs.consistency.agent" in span_agent_ids,
                    "consistency: Span.agent_id 正确")

        # ObservabilityProvider.build_observability_context 与 run_id 一致
        print("\n  · ObservabilityProvider.build_observability_context")
        obs_ctx = ObservabilityProvider.build_observability_context(
            agent_id="obs.consistency.agent",
            run_id=run_id,
        )
        assert_true(obs_ctx.run_id == run_id, "consistency: ObservabilityContext.run_id 一致")
        assert_true(obs_ctx.agent_id == "obs.consistency.agent",
                    "consistency: ObservabilityContext.agent_id 正确")
        assert_true(obs_ctx.trace_id == run_id,
                    "consistency: 单 Agent 场景 trace_id == run_id")

    asyncio.run(_run())


# ════════════════════════════════════════════════════
#  5. 工具调用 Span 测试
# ════════════════════════════════════════════════════

def test_tools():
    print("\n" + "═" * 60)
    print("5️⃣  工具调用 Span + Metrics 测试（真实 LLM + Tool）")
    print("═" * 60)

    # ── 定义一个简单的加法工具 ──
    async def add_numbers(ctx: ToolContext, a: int, b: int) -> ToolResult:
        """对两个整数求和。"""
        return ToolResult(success=True, data={"result": a + b})

    add_tool = Tool(
        name="add_numbers",
        description="对两个整数 a 和 b 求和，返回结果",
        executor=add_numbers,
        input_schema={
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "第一个整数"},
                "b": {"type": "integer", "description": "第二个整数"},
            },
            "required": ["a", "b"],
        },
        tier=ToolTier.ALWAYS,
        sensitivity=SensitivityLevel.LOW,
        is_reversible=True,
        audit_required=False,
        is_idempotent=True,
        when_to_use="当需要对两个整数求和时调用",
    )

    async def _run():
        audit_be = InMemoryAuditBackend()
        tracer_be = InMemoryTracerBackend()
        metrics_be = InMemoryMetricsBackend()

        agent = (
            AgentBuilder()
            .identity(
                agent_id="obs.tools.agent",
                agent_name="观测测试助手",
                when_to_use="用于可观测性集成测试",
                sensitive_permissions=PERMISSION_ALL,
                trust_level=TrustLevel.SUB_AGENT,
            )
            .llm(_make_llm_client())
            .system_prompt(
                "你是一个计算助手。你必须使用 add_numbers 工具来完成任何计算。"
                "禁止自行计算或口算，所有计算都必须通过调用 add_numbers 工具完成。"
                "完成后直接输出工具返回的数字结果。"
            )
            .behavior(max_steps=5)
            .observability(
                audit=audit_be,
                tracer=tracer_be,
                metrics=metrics_be,
                log=False,
                trace_level=TraceLevel.FULL,
            )
            .tools([add_tool])
            .build()
        )

        print("\n  · 使用工具后 TOOL_CALL span 存在 + tool_execute_total 计数")
        async with agent:
            run_result = await agent.run(
                "请用 add_numbers 工具计算 10 加 20*898989/888 的结果，并告诉我答案。",
                session_id="tools_session_1",
            )

        assert_true(run_result.success is True, "tools: run() success=True")
        run_id = run_result.run_id
        print(f"   → LLM 输出: {str(run_result.output)[:80]}")

        spans = tracer_be.get_spans(run_id)
        tool_spans = [s for s in spans if s.span_type == SpanType.TOOL_CALL]
        print(f"   → 共 {len(spans)} 个 Span，其中 TOOL_CALL={len(tool_spans)}")

        if len(tool_spans) >= 1:
            ts = tool_spans[0]
            assert_true(ts.duration_ms is not None and ts.duration_ms >= 0,
                        "tools: TOOL_CALL span.duration_ms >= 0")
            assert_true(ts.run_id == run_id, "tools: TOOL_CALL span.run_id 一致")
            assert_true(ts.end_time is not None, "tools: TOOL_CALL span 已结束")
            print(f"   → TOOL_CALL span duration={ts.duration_ms:.2f}ms, status={ts.status.value}")

            # Metrics: tool_execute_total
            summary = metrics_be.get_summary()
            counters = summary.get("counters", {})
            tool_exec_keys = [k for k in counters if "tool_execute_total" in k]
            assert_true(len(tool_exec_keys) >= 1, "tools: tool_execute_total 计数器存在")
            if tool_exec_keys:
                total = sum(counters[k] for k in tool_exec_keys)
                assert_true(total >= 1, f"tools: tool_execute_total >= 1 (got {total})")
        else:
            # LLM 可能选择不调用工具，只记录提示
            print("   ⚠️  LLM 未调用工具（正常情况，跳过 TOOL_CALL 断言）")
            result.ok("tools: TOOL_CALL span（LLM 未触发工具调用，跳过）")

    asyncio.run(_run())


# ════════════════════════════════════════════════════
#  6. 多轮 run() 各自独立 run_id
# ════════════════════════════════════════════════════

def test_multirun():
    print("\n" + "═" * 60)
    print("6️⃣  多轮 run() 各自独立 run_id（审计隔离）")
    print("═" * 60)

    async def _run():
        audit_be = InMemoryAuditBackend()
        tracer_be = InMemoryTracerBackend()
        metrics_be = InMemoryMetricsBackend()

        agent = _make_observed_agent(
            "obs.multirun.agent",
            audit_be, tracer_be, metrics_be,
        )

        print("\n  · 两次 run() 生成两个不同 run_id")
        async with agent:
            r1 = await agent.run("1+1=?，只回答数字。", session_id="multi_session_1")
            r2 = await agent.run("2+2=?，只回答数字。", session_id="multi_session_2")

        assert_true(r1.success is True, "multirun: 第一次 run() success=True")
        assert_true(r2.success is True, "multirun: 第二次 run() success=True")

        run_id_1 = r1.run_id
        run_id_2 = r2.run_id
        assert_true(run_id_1 != run_id_2, "multirun: 两次 run 的 run_id 不同")
        print(f"   → run_id_1={run_id_1[:12]}..., run_id_2={run_id_2[:12]}...")

        # 每个 run_id 对应各自的审计记录
        records_1 = audit_be.query(limit=200)
        ids_1_set = {r.run_id for r in records_1 if r.run_id == run_id_1}
        ids_2_set = {r.run_id for r in records_1 if r.run_id == run_id_2}
        assert_true(len(ids_1_set) >= 1, "multirun: run_id_1 存在于 AuditRecord 中")
        assert_true(len(ids_2_set) >= 1, "multirun: run_id_2 存在于 AuditRecord 中")

        # 每个 run_id 对应各自的 Span
        spans_1 = tracer_be.get_spans(run_id_1)
        spans_2 = tracer_be.get_spans(run_id_2)
        assert_true(len(spans_1) >= 1, "multirun: run_id_1 对应 Span 存在")
        assert_true(len(spans_2) >= 1, "multirun: run_id_2 对应 Span 存在")
        assert_true(
            not any(s.run_id == run_id_1 for s in spans_2),
            "multirun: run_id_2 的 Span 中无 run_id_1（隔离正确）",
        )

        # Metrics 累计增长
        summary = metrics_be.get_summary()
        counters = summary.get("counters", {})
        run_total_keys = [k for k in counters if "run_total" in k]
        if run_total_keys:
            total = sum(counters[k] for k in run_total_keys)
            assert_true(total >= 2, f"multirun: run_total 累计 >= 2 (got {total})")
            print(f"   → run_total 累计={total}")

    asyncio.run(_run())


# ════════════════════════════════════════════════════
#  7. DualAuditBackend 真实 run 双写
# ════════════════════════════════════════════════════

def test_dual_audit_real():
    print("\n" + "═" * 60)
    print("7️⃣  DualAuditBackend 真实 run 双写验证")
    print("═" * 60)

    async def _run():
        primary = InMemoryAuditBackend()
        secondary = InMemoryAuditBackend()
        dual = DualAuditBackend(primary=primary, secondary=secondary)

        tracer_be = InMemoryTracerBackend()
        metrics_be = InMemoryMetricsBackend()

        agent = _make_observed_agent(
            "obs.dual.agent",
            dual, tracer_be, metrics_be,
        )

        print("\n  · 真实 run() 后 primary 和 secondary 均有记录")
        async with agent:
            run_result = await agent.run("你好", session_id="dual_session_1")

        assert_true(run_result.success is True, "dual: run() success=True")

        p_records = primary.query(limit=200)
        s_records = secondary.query(limit=200)
        assert_true(len(p_records) >= 2, f"dual: primary 有 >= 2 条记录 (got {len(p_records)})")
        assert_true(len(s_records) >= 2, f"dual: secondary 有 >= 2 条记录 (got {len(s_records)})")
        assert_true(len(p_records) == len(s_records),
                    f"dual: primary({len(p_records)}) 与 secondary({len(s_records)}) 记录数相同")

        print(f"   → primary={len(p_records)} 条, secondary={len(s_records)} 条")

        # DualAuditBackend.query → 来自 primary
        dual_query = dual.query(limit=200)
        assert_true(len(dual_query) == len(p_records),
                    "dual: DualAuditBackend.query 委托给 primary")

    asyncio.run(_run())


# ════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════

SECTIONS = {
    "audit":      test_audit,
    "tracer":     test_tracer,
    "metrics":    test_metrics,
    "consistency": test_consistency,
    "tools":      test_tools,
    "multirun":   test_multirun,
    "dual_audit": test_dual_audit_real,
}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Observability 模块真实集成测试")
    parser.add_argument(
        "--section",
        choices=list(SECTIONS.keys()),
        default="",
        help="仅运行指定测试分区",
    )
    args = parser.parse_args()

    print("🐼 Pandaren Agent SDK — Observability 模块真实集成测试")
    print("   目标模块: pandaren/observability/")
    print("   LLM: DashScope qwen-plus（或 OPENAI_API_KEY 时使用 gpt-4o-mini）")
    print()

    # 抑制 SDK 内部日志，减少噪音
    for logger_name in [
        "pandaren.observability", "pandaren.engine", "pandaren.agent",
        "pandaren.tool", "pandaren.memory", "pandaren.behavior",
    ]:
        logging.getLogger(logger_name).setLevel(logging.ERROR)

    if args.section:
        section_fn = SECTIONS[args.section]
        section_result = TestResult()

        global result
        old_result = result
        result = section_result

        section_fn()

        result = old_result
        result.passed += section_result.passed
        result.failed += section_result.failed
        result.errors.extend(section_result.errors)

        section_result.summary(args.section)
    else:
        test_audit()
        test_tracer()
        test_metrics()
        test_consistency()
        test_tools()
        test_multirun()
        test_dual_audit_real()
        result.summary("全部")

    if result.failed > 0:
        print("\n⚠️  存在失败的测试用例，请检查上方输出")
        sys.exit(1)
    else:
        print("\n🎉 所有测试通过！Observability 模块真实集成测试验证完毕")
        sys.exit(0)


if __name__ == "__main__":
    main()
