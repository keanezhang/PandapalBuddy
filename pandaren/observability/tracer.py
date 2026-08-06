"""pandaren/observability/tracer.py — 链路追踪 Facade
主要用途：是跟踪程序执行的链路，主要负责把agent每次run的完整执行链路，切成一棵带时间戳的span树，让你能看清“哪一步耗了多少时间，出了什么错误”。

Tracer 子系统：管理 trace span 的生命周期。
非 HC4 子系统——故障时优雅降级，不传播异常到 Loop。

后端实现在 backend/ 子目录中。

设计文档对齐：
  start_span(name, span_type, ...) → Span   参数顺序对齐设计文档
  end_span(span, status, ...)      → None    接收 Span 对象
  build_trace_context(parent_span) → TraceContext（跨 Agent 传播）
  _sanitize_attributes(attributes)           按 sanitizer 规则脱敏 span 属性
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .types import Span, SpanType, SpanStatus, TraceLevel, ObservabilityContext, generate_id
from .protocols import TracerBackend, Sanitizer

# 观测 Fail-Safe 边界：后端 export/query 失败不传播到 Loop，但 §九「降级必留痕」——
# 委托给可插拔后端的调用失败不再静默，降级为 debug 留痕（非终端 sink，可安全 log）。
logger = logging.getLogger(__name__)


class Tracer:
    """链路追踪系统。

    创建和管理 trace span，形成 RUN → STEP → LLM_CALL/TOOL_CALL 的层级树。
    故障时优雅降级（不传播异常到 Loop）。
    """

    def __init__(
        self,
        *,
        backend: TracerBackend | None = None,
        trace_level: TraceLevel = TraceLevel.FULL,
        agent_id: str = "",
        sanitizer: Sanitizer | None = None,
    ) -> None:
        self._backend = backend
        self._trace_level = trace_level
        self._agent_id = agent_id
        self._sanitizer = sanitizer
        self._active_spans: dict[str, _ActiveSpan] = {}

    def start_span(
        self,
        name: str,
        span_type: SpanType,
        *,
        run_id: str = "",
        step_n: int | None = None,
        parent_span_id: str | None = None,
        session_id: str = "",
        attributes: dict[str, Any] | None = None,
    ) -> Span:
        """开始一个 span，返回 Span 对象。

        当 trace_level=MINIMAL 时，只记录 RUN 级 span 和异常 span；
        其他 span 返回空 Span（noop span，span_id 为空字符串）。

        session_id：由调用方（hooks_adapter，源自 run 的 session）显式传入，
        用于后端按会话分片落盘；空串 → _no_session（无会话归属的全局级 span）。
        """
        try:
            if not self._should_record(span_type, is_error=False):
                return _noop_span()
            span_id = generate_id()
            trace_id = run_id or generate_id()
            sid = session_id
            sanitized_attrs = self._sanitize_attributes(dict(attributes or {}))
            active = _ActiveSpan(
                span_id=span_id, trace_id=trace_id,
                parent_span_id=parent_span_id, span_type=span_type,
                name=name, agent_id=self._agent_id, run_id=run_id,
                session_id=sid,
                step_n=step_n, start_time=datetime.now(timezone.utc),
                start_mono=time.perf_counter(), attributes=sanitized_attrs,
            )
            self._active_spans[span_id] = active
            # 返回 Span 对象，但 end_time/duration_ms 尚未填充
            return Span(
                span_id=span_id, trace_id=trace_id,
                parent_span_id=parent_span_id, span_type=span_type,
                name=name, agent_id=self._agent_id, run_id=run_id,
                session_id=sid,
                step_n=step_n,
                start_time=active.start_time, end_time=None, duration_ms=None,
                status=SpanStatus.OK, attributes=sanitized_attrs,
            )
        except Exception:
            return _noop_span()

    def end_span(
        self,
        span: Span,
        *,
        status: SpanStatus = SpanStatus.OK,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """结束 span，计算 duration_ms，构建不可变 Span 写入后端。

        接收 Span 对象（由 start_span 返回），从 _active_spans 中
        取出运行时状态，计算耗时后导出。

        MINIMAL 模式下，异常 span 也会被记录。
        """
        if not span or not span.span_id:
            return
        try:
            active = self._active_spans.pop(span.span_id, None)
            if active is None:
                return
            end_time = datetime.now(timezone.utc)
            duration_ms = (time.perf_counter() - active.start_mono) * 1000
            merged_attrs: dict[str, Any] = {**active.attributes, **(attributes or {})}
            # SUMMARY 模式：截断长字符串
            if self._trace_level == TraceLevel.SUMMARY:
                for k, v in merged_attrs.items():
                    if isinstance(v, str) and len(v) > 200:
                        merged_attrs[k] = v[:200] + "..."
            # MINIMAL 异常 span 强制记录
            if active._force_record:
                status = SpanStatus.ERROR
            completed = Span(
                span_id=active.span_id,
                trace_id=active.trace_id,
                parent_span_id=active.parent_span_id,
                span_type=active.span_type,
                name=active.name,
                agent_id=active.agent_id,
                run_id=active.run_id,
                session_id=active.session_id,
                step_n=active.step_n,
                start_time=active.start_time, 
                end_time=end_time,
                duration_ms=duration_ms, 
                status=status, 
                attributes=merged_attrs,
            )
            if self._backend is not None:
                self._backend.export_span(completed)
        except Exception:
            logger.debug("tracer export_span failed", exc_info=True)

    def mark_span_error(self, span: Span) -> None:
        """将当前活跃 span 标记为异常状态（用于 MINIMAL 模式下确保异常 span 被记录）。

        当 on_error 触发时，调用此方法确保 MINIMAL 模式下异常 span 不被丢弃。
        """
        if not span or not span.span_id:
            return
        active = self._active_spans.get(span.span_id)
        if active is not None:
            active._force_record = True

    def query_trace(self, run_id: str) -> list[Span]:
        """根据 run_id 查询完整链路（场景 1：开发调试）。

        委托给后端的 query_spans 方法。
        若后端不支持（返回空列表），返回空。
        """
        if self._backend is None:
            return []
        try:
            return self._backend.query_spans(run_id)
        except Exception:
            logger.debug("tracer query_spans failed run_id=%s", run_id, exc_info=True)
            return []

    def build_trace_context(self, parent_span: Span | None = None) -> ObservabilityContext:
        """构建跨 Agent 传播的 trace 上下文（场景 7：多 Agent 协作追踪）。

        从当前 Tracer 状态提取 trace_id、agent_id 等信息，
        构建 ObservabilityContext 传递给子 Agent。
        """
        # 如果有活跃的 run span，使用其 trace_id
        trace_id = ""
        parent_span_id = None
        if parent_span and parent_span.span_id:
            trace_id = parent_span.trace_id
            parent_span_id = parent_span.span_id
        if not trace_id:
            for active in self._active_spans.values():
                if active.span_type == SpanType.RUN:
                    trace_id = active.trace_id
                    parent_span_id = active.span_id
                    break
        if not trace_id:
            trace_id = generate_id()

        return ObservabilityContext(
            run_id="",  # 子 Agent 的 run_id 由子 Agent 自己生成
            agent_id=self._agent_id,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
        )

    def _should_record(self, span_type: SpanType, is_error: bool) -> bool:
        """按 trace_level 决定是否记录 span。

        FULL:    记录所有 span
        SUMMARY: 记录所有 span（但 end_span 时截断属性）
        MINIMAL: 只记录 RUN 级 span + 异常 span
        """
        if self._backend is None:
            return False
        if self._trace_level == TraceLevel.FULL:
            return True
        if self._trace_level == TraceLevel.SUMMARY:
            return True
        # MINIMAL: RUN + 异常 span（严格对齐设计文档 Step 4 O1）
        if span_type == SpanType.RUN:
            return True
        if is_error:
            return True
        return False

    def _sanitize_attributes(self, attributes: dict[str, Any]) -> dict[str, Any]:
        """按 sanitizer 规则脱敏 span 属性。"""
        if self._sanitizer is None:
            return attributes
        try:
            sanitized = {}
            for k, v in attributes.items():
                if isinstance(v, str):
                    sanitized[k] = self._sanitizer.sanitize(v, field_name=k)
                else:
                    sanitized[k] = v
            return sanitized
        except Exception:
            # O3：脱敏失败不能静默，但也不能崩溃——用占位符
            return {"_sanitize_error": True}

    def set_agent_id(self, agent_id: str) -> None:
        self._agent_id = agent_id

    def __repr__(self) -> str:
        return (
            f"Tracer(level={self._trace_level.value}, "
            f"agent_id='{self._agent_id}', active_spans={len(self._active_spans)})"
        )


class _ActiveSpan:
    __slots__ = (
        "span_id", "trace_id", "parent_span_id", "span_type", "name",
        "agent_id", "run_id", "session_id", "step_n", "start_time", "start_mono",
        "attributes", "_force_record",
    )

    def __init__(self, span_id, trace_id, parent_span_id, span_type, name,
                 agent_id, run_id, step_n, start_time, start_mono, attributes,
                 session_id=""):
        self.span_id = span_id
        self.trace_id = trace_id
        self.parent_span_id = parent_span_id
        self.span_type = span_type
        self.name = name
        self.agent_id = agent_id
        self.run_id = run_id
        self.session_id = session_id
        self.step_n = step_n
        self.start_time = start_time
        self.start_mono = start_mono
        self.attributes = attributes
        self._force_record = False


def _noop_span() -> Span:
    """创建空 Span 对象（span_id 为空，表示 noop）。"""
    return Span(
        span_id="", trace_id="", parent_span_id=None,
        span_type=SpanType.RUN, name="",
        agent_id="", run_id="", step_n=None,
        start_time=datetime.now(timezone.utc),
        end_time=None, duration_ms=None,
        status=SpanStatus.OK, attributes={},
    )
