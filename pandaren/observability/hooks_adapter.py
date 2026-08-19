"""pandaren/observability/hooks_adapter.py — AgentHooks → Observability 桥接适配器

将 AgentHooks 统一协议的 19 个生命周期事件桥接到 Observability 四子系统
（hooks.py 共 21 个 hook，on_skill_activated/cleared 由 SkillManager 直接处理，不桥接）：
  - Logger：结构化日志
  - Tracer：创建/关闭 trace span
  - Metrics：记录指标
  - AuditLog：审计日志（已由 Loop 硬编码调用，此处不重复）

分区（编号对应 hooks.py AgentHooks 协议顺序）：
  A. Run 生命周期（2）      — Hook 1-2
  B. Step 生命周期（2）     — Hook 3-4
  C. LLM 调用（2）          — Hook 5-6
  D. Tool 执行（2）         — Hook 7-8 （合并了原 ToolHooks.execute_start/end）
  E. Tool 管理（6）         — Hook 9-14 （register/discover/disabled/circuit_open/circuit_close/output_truncated）
  F. 并发失败（1）          — Hook 15 （concurrent_execution_failure）
  G. HITL 审批（2）         — Hook 16-17 （requested/resolved）
  H. 控制流（2）            — Hook 18-19 （error/halt；on_halt 合并了原 on_run_halt）
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .logger import Logger
from .tracer import Tracer
from .metrics import Metrics
from .types import Span, SpanType, SpanStatus

# run「暂停」类终止原因（等待人工/用户/审批，run 仍活跃、不是失败）。
# 与 TerminalReason.{HITL_PAUSED, INTERACTION_PAUSED, PLAN_COMPLETE} 对齐。
# on_run_end 对这些原因统一按「暂停」处理：run/子 span 记 CANCELLED（非 ERROR）、
# 指标记 paused、active_runs 不减——避免正常暂停被误记为 error（结束原因列为空的历史事故）。
_PAUSE_REASONS: frozenset[str] = frozenset({
    "hitl_paused", "interaction_paused", "plan_complete",
})


def _fp8(s: str) -> str:
    """8 位十六进制短指纹（跨进程稳定，仅用于 prefix cache 稳定性对比，不用于加密）。"""
    return hashlib.md5(s.encode(), usedforsecurity=False).hexdigest()[:8]


def _tool_name(t: dict[str, Any]) -> str:
    fn = t.get("function", {})
    return fn.get("name") or t.get("name", "") or ""


def _split_tools_by_search(
    tools: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]]:
    """按 search_tools 的位置把 tools 数组切成三段。

    返回 (before_search, search_item, after_search)：
      - before_search：位置上在 search_tools 之前的条目（三段式重排下必为 ALWAYS 段前半）
      - search_item  ：search_tools 本身（含动态收缩的 enum）
      - after_search ：位置上在 search_tools 之后的条目（ALWAYS 段尾部 + DEFERRED-loaded 段）

    PC5 三段重排（registry.build_tool_schemas）：
      1. sorted(ALWAYS 除去 search_tools)  ← 稳定前缀
      2. search_tools                      ← 内部 enum 抖动
      3. sorted(DEFERRED-loaded)           ← 追加型变长

    注：之所以按"位置"而不是"语义"切分，是因为 hooks 层拿不到 tier 分类元数据；
    但 after_search 段里 ALWAYS 尾部条目的 schema 本身也是稳定的，和 DEFERRED-loaded
    的"追加型跳变"语义一致，合并成单一指纹不丢失观测价值。
    """
    before: list[dict[str, Any]] = []
    search: dict[str, Any] | None = None
    after: list[dict[str, Any]] = []
    seen_search = False
    for t in tools:
        if _tool_name(t) == "search_tools":
            search = t
            seen_search = True
            continue
        (after if seen_search else before).append(t)
    return before, search, after


class ObservabilityHooksAdapter:
    """AgentHooks → Observability 适配器（18 个 hook 全部实现）。"""

    def __init__(
        self,
        *,
        logger: Logger,
        tracer: Tracer,
        metrics: Metrics,
    ) -> None:
        self._logger = logger
        self._tracer = tracer
        self._metrics = metrics
        # SDK 不计价：llm_call span 只记 token/命中等事实，不再挂金额 cost_usd
        # （价格/预算全归应用层，见 pandapal.config.llm_pricing）。
        # 运行时状态：持有 Span 对象。
        # ★ 并发隔离：所有 per-run 缓冲字段按 run_id 隔离（dict keyed by run_id）——
        # 本 adapter 在 AgentBlueprint.materialize() 后由多个 session 的 Agent **共享同一实例**
        # （SessionAgentPool 多会话并发，默认 max_concurrent=5）。单槽字段在并发 run 交错
        # （LLM/tool 的 await 点）时互相覆盖：run A 的 span 被 run B 覆盖 → A 的 on_run_end
        # 关闭的是 B 的 span，造成跨会话 span 串扰 + 泄漏 + duration 失真。
        # run_id 隔离后各 run 互不干扰；on_run_end 统一清理本 run 的 key（防 dict 泄漏）。
        self._run_spans: dict[str, Span] = {}                       # run_id → run span
        self._step_spans: dict[str, Span] = {}                      # run_id → step span
        self._step_start_mono_by_run: dict[str, float] = {}         # run_id → step start（perf_counter）
        self._llm_spans: dict[str, Span] = {}                       # run_id → llm span（run 内串行）
        self._llm_call_start_by_run: dict[str, float] = {}          # run_id → llm call start
        self._llm_call_model_by_run: dict[str, str] = {}            # run_id → model（on_after 使用）
        self._llm_call_provider_by_run: dict[str, str] = {}         # run_id → provider（平台名）
        # Bug 1 Fix: 同名工具并发调用时用列表队列（FIFO），避免 key 碰撞覆盖；
        # 并发多 run 下按 run_id 再分桶。
        self._tool_spans_by_run: dict[str, dict[str, list[Span]]] = {}       # run_id → tool_name → FIFO
        self._tool_call_starts_by_run: dict[str, dict[str, list[float]]] = {}  # run_id → tool_name → FIFO
        # Bug 2 Fix: HITL span 独立存放，不覆盖 _step_span
        self._hitl_spans: dict[str, Span] = {}                      # run_id → hitl span
        self._run_start_mono_by_run: dict[str, float] = {}          # run_id → run start（perf_counter）
        # 以下字段跨 run 共享（run_id 本身就是 key，或语义为全局）：
        self._step_n_by_run: dict[str, int] = {}   # run_id → current step_n（并发安全）
        self._active_run_count: int = 0  # 当前活跃 run 数（用于 Gauge 精确更新）
        self._active_run_ids: set[str] = set()  # 已计入 active 的 run_id（防止 resume 重复 +1）

    # ═══ Hook 1: on_run_start ═══
    def on_run_start(self, task: str, run_id: str, *, session_id: str = "") -> None:
        self._run_start_mono_by_run[run_id] = time.perf_counter()
        task_preview = task[:80] if isinstance(task, str) else "(resume)"
        self._logger.info(f"Run 开始: {task_preview}", module="loop", run_id=run_id, session_id=session_id)
        # 仅首次启动时计数 run_total{started}，HITL resume 同一 run_id 不重复计数
        # Bug 4 Fix: 只用 set_active_runs 精确设值，不再额外调用 inc_active_runs
        if run_id not in self._active_run_ids:
            self._metrics.inc_run_total("started")
            self._active_run_ids.add(run_id)
            self._active_run_count += 1
            self._metrics.set_active_runs(self._active_run_count)
        self._run_spans[run_id] = self._tracer.start_span(
            "agent.run", SpanType.RUN,
            run_id=run_id, session_id=session_id,
            attributes={"task": task[:200] if isinstance(task, str) else "(resume)"},
        )

    # ═══ Hook 2: on_run_end ═══
    def on_run_end(self, run_id: str, success: bool, *, terminal_reason: str = "", session_id: str = "") -> None:
        duration_ms = (time.perf_counter() - self._run_start_mono_by_run.get(run_id, 0.0)) * 1000 \
            if run_id in self._run_start_mono_by_run else 0
        # 暂停（等待人工/用户/审批）不是失败：HITL / 交互 / Plan 审批统一按「暂停」处理，
        # 否则正常暂停会被误记为 error（结束原因列为空的历史事故根因）。
        paused = terminal_reason in _PAUSE_REASONS
        if success:
            status_str = "成功"
        elif paused:
            status_str = f"暂停({terminal_reason})"
        else:
            status_str = "失败"
        self._logger.info(f"Run 结束: {status_str}", module="loop", run_id=run_id, session_id=session_id)
        if success:
            metric_status = "success"
        elif paused:
            metric_status = "paused"
        else:
            metric_status = "failed"
        self._metrics.inc_run_total(metric_status)
        # 「仍活跃」当且仅当 run 被真正挂起等待人工/用户/审批，即 success=False 的暂停
        # （hitl_paused / interaction_paused）。plan_complete 虽也在 _PAUSE_REASONS（用于
        # sub/run span 记 CANCELLED/OK 的语义），但它是**成功终止**（success=True），run 已
        # 结束，必须释放 active_runs 与 _active_run_ids——否则每个 plan 模式 run 都会永久
        # 泄漏 +1，active_runs 单调虚高（历史事故：会话结束后 active_runs 停在非 0）。
        # Bug 4 Fix: 只用 set_active_runs 精确设值
        suspended = paused and not success
        if not suspended:
            self._active_run_ids.discard(run_id)
            self._active_run_count = max(0, self._active_run_count - 1)
            self._metrics.set_active_runs(self._active_run_count)
        self._metrics.observe_run_duration_ms(duration_ms)
        # 费用归应用层：SDK 不再累计/记录 run 花费（无 token_cost gauge）。

        # Bug 3 Fix: 终止时兜底关闭所有未关闭的子 span（step / llm / tool / hitl）。
        # 暂停时这些子 span 是「被挂起」而非「出错」，故记 CANCELLED；异常终止才记 ERROR。
        _sub_status = SpanStatus.CANCELLED if paused else SpanStatus.ERROR
        _closed = {"paused": True} if paused else {"aborted": True}
        _llm_span = self._llm_spans.get(run_id)
        if _llm_span and _llm_span.span_id:
            self._tracer.end_span(_llm_span, status=_sub_status, attributes=_closed)
            self._llm_spans.pop(run_id, None)
            self._llm_call_start_by_run.pop(run_id, None)
        for spans in self._tool_spans_by_run.get(run_id, {}).values():
            for span in spans:
                if span and span.span_id:
                    self._tracer.end_span(span, status=_sub_status, attributes=_closed)
        self._tool_spans_by_run.pop(run_id, None)
        self._tool_call_starts_by_run.pop(run_id, None)
        _hitl_span = self._hitl_spans.get(run_id)
        if _hitl_span and _hitl_span.span_id:
            self._tracer.end_span(_hitl_span, status=_sub_status, attributes=_closed)
            self._hitl_spans.pop(run_id, None)
        _step_span = self._step_spans.get(run_id)
        if _step_span and _step_span.span_id:
            self._tracer.end_span(_step_span, status=_sub_status, attributes=_closed)
            self._step_spans.pop(run_id, None)

        _run_span = self._run_spans.get(run_id)
        if _run_span and _run_span.span_id:
            # 暂停（HITL/交互/Plan 审批）= 主动挂起等待，用 CANCELLED（success 时用 OK，如 plan_complete）
            # hitl_rejected / cancelled = 人工拒绝或强杀终止，用 ERROR
            if paused:
                span_status = SpanStatus.OK if success else SpanStatus.CANCELLED
            elif terminal_reason in ("hitl_rejected", "cancelled"):
                span_status = SpanStatus.ERROR
            else:
                span_status = SpanStatus.OK if success else SpanStatus.ERROR
            attrs: dict = {"success": success, "duration_ms": round(duration_ms, 1)}
            if terminal_reason:
                attrs["terminal_reason"] = terminal_reason
            self._tracer.end_span(_run_span, status=span_status, attributes=attrs)
            self._run_spans.pop(run_id, None)
        self._step_n_by_run.pop(run_id, None)
        # ★ 清理本 run 的全部 per-run 缓冲（防 dict 泄漏；暂停 resume 时由下一次 on_run_start 重建）
        self._run_start_mono_by_run.pop(run_id, None)
        self._step_start_mono_by_run.pop(run_id, None)
        self._llm_call_model_by_run.pop(run_id, None)
        self._llm_call_provider_by_run.pop(run_id, None)
        # run 边界强制刷盘：把本 run 段落（含 run_total / active_runs / run_duration）
        # 落地，避免 metrics 后端的按批刷盘让裸文件停在旧快照。暂停段落（hitl/交互/plan
        # resume）同样经此收口，故每个 run/暂停/恢复边界都会刷新一次。
        self._metrics.flush()

    # ═══ Hook 3: on_step_start ═══
    def on_step_start(self, step_n: int, run_id: str, *, session_id: str = "") -> None:
        self._step_n_by_run[run_id] = step_n
        self._step_start_mono_by_run[run_id] = time.perf_counter()
        self._logger.info(f"Step {step_n} 开始", module="loop", run_id=run_id, step_n=step_n, session_id=session_id)
        self._metrics.inc_step_total()
        _run_span = self._run_spans.get(run_id)
        self._step_spans[run_id] = self._tracer.start_span(
            f"step.{step_n}", SpanType.STEP,
            run_id=run_id, step_n=step_n, session_id=session_id,
            parent_span_id=_run_span.span_id if _run_span else None,
        )

    # ═══ Hook 4: on_step_end ═══
    def on_step_end(self, step_n: int, run_id: str, *, session_id: str = "") -> None:
        duration_ms = (time.perf_counter() - self._step_start_mono_by_run.get(run_id, 0.0)) * 1000 \
            if run_id in self._step_start_mono_by_run else 0
        self._logger.info(
            f"Step {step_n} 结束 ({duration_ms:.0f}ms)",
            module="loop", run_id=run_id, step_n=step_n, session_id=session_id,
        )
        self._metrics.observe_step_duration_ms(duration_ms)
        _step_span = self._step_spans.get(run_id)
        if _step_span and _step_span.span_id:
            self._tracer.end_span(
                _step_span,
                attributes={"duration_ms": round(duration_ms, 1)},
            )
            self._step_spans.pop(run_id, None)
        # Bug 6 Fix: 不在 on_step_end 弹出 step_n；保留到 on_run_end 或下一个 on_step_start 覆盖。
        # 这样 on_error / on_halt 在步骤间窗口期触发时仍能拿到正确的 step_n，而不是 0。

    # ═══ Hook 5: on_before_llm_call ═══
    def on_before_llm_call(
        self,
        messages: list[dict[str, Any]],
        run_id: str,
        model: str = "",
        tools: list[dict[str, Any]] | None = None,
        *,
        call_type: str = "main",
        session_id: str = "",
        provider: str = "",
    ) -> None:
        self._llm_call_start_by_run[run_id] = time.perf_counter()
        messages_preview: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content")
            # 不截断：完整保留所有消息内容（system/tool/user/assistant）
            entry: dict[str, Any] = {"role": role, "content": content}
            if m.get("tool_calls"): entry["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"): entry["tool_call_id"] = m["tool_call_id"]
            if m.get("name"): entry["name"] = m["name"]
            messages_preview.append(entry)
        # 记录工具名列表
        tool_names: list[str] = []
        if tools:
            for t in tools:
                fn = t.get("function", {})
                name = fn.get("name") or t.get("name", "")
                if name:
                    tool_names.append(name)

        # ── Prefix Cache 稳定性观测 ──
        # PC1：system content 字节稳定（静态前缀）
        sys_content: Any = messages[0].get("content") if messages else ""
        sys_text = sys_content if isinstance(sys_content, str) else str(sys_content)
        prefix_len = len(sys_text)
        prefix_fp = _fp8(sys_text)

        # PC5：tools 数组三段式稳定性
        #   before_search_fp ← 第 ① 段前半（应永远稳定；若变化 → PC1 破产）
        #   search_fp        ← 第 ② 段（search_tools 一项，enum 每次 discovery 后抖动）
        #   after_search_fp  ← 第 ③ 段 + ALWAYS 段尾部（追加型跳变，跳变后应长期稳定）
        _before, _search, _after = _split_tools_by_search(tools or [])
        _dump = lambda obj: json.dumps(obj, sort_keys=True, ensure_ascii=False)
        tools_before_search_fp = _fp8(_dump(_before)) if _before else "00000000"
        tools_search_fp = _fp8(_dump(_search)) if _search is not None else "--------"
        tools_after_search_fp = _fp8(_dump(_after)) if _after else "00000000"
        tools_after_search_len = len(_after)
        # search_tools.enum 的长度（未发现 DEFERRED 数；enum 单调收缩）
        search_enum_len = 0
        if _search is not None:
            try:
                _enum = (
                    _search.get("function", {})
                    .get("parameters", {})
                    .get("properties", {})
                    .get("tool_name", {})
                    .get("enum")
                )
                search_enum_len = len(_enum) if isinstance(_enum, list) else 0
            except Exception:
                search_enum_len = 0

        # ── cache_control 断点观测 ──
        # 扫描 tools 上的断点
        cache_breakpoints_tools: list[str] = []
        if tools:
            for idx, t in enumerate(tools):
                if "cache_control" in t:
                    tname = t.get("function", {}).get("name") or t.get("name", f"tool[{idx}]")
                    cache_breakpoints_tools.append(f"tools[{idx}]({tname})")
        # 扫描 messages 上的断点
        cache_breakpoints_msgs: list[str] = []
        for idx, m in enumerate(messages):
            # 顶层 cache_control
            if "cache_control" in m:
                role = m.get("role", "?")
                cache_breakpoints_msgs.append(f"messages[{idx}]({role})")
            # content 为 list 时，检查 content block 级别的 cache_control
            content = m.get("content")
            if isinstance(content, list):
                for blk_idx, blk in enumerate(content):
                    if isinstance(blk, dict) and "cache_control" in blk:
                        role = m.get("role", "?")
                        cache_breakpoints_msgs.append(
                            f"messages[{idx}]({role}).content[{blk_idx}]"
                        )
        total_breakpoints = len(cache_breakpoints_tools) + len(cache_breakpoints_msgs)
        breakpoints_summary = (
            f"cache_breakpoints={total_breakpoints}"
            f" (tools: [{', '.join(cache_breakpoints_tools)}]"
            f" | msgs: [{', '.join(cache_breakpoints_msgs)}])"
            if total_breakpoints > 0
            else "cache_breakpoints=0"
        )

        self._logger.info(
            f"LLM 调用中... (messages={len(messages)}, tools={len(tool_names)}"
            f", prefix_len={prefix_len}, prefix_fp={prefix_fp}"
            f" | tools_fp: before={tools_before_search_fp}"
            f" search={tools_search_fp}(enum_len={search_enum_len})"
            f" after={tools_after_search_fp}(len={tools_after_search_len})"
            f" | {breakpoints_summary})",
            module="llm", run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0),
            session_id=session_id,
            messages=messages_preview,
            tools=tool_names,
            tools_schema=tools or [],
            prefix_len=prefix_len,
            prefix_fp=prefix_fp,
            tools_before_search_fp=tools_before_search_fp,
            tools_search_fp=tools_search_fp,
            tools_search_enum_len=search_enum_len,
            tools_after_search_fp=tools_after_search_fp,
            tools_after_search_len=tools_after_search_len,
            cache_breakpoints_tools=cache_breakpoints_tools,
            cache_breakpoints_msgs=cache_breakpoints_msgs,
            total_cache_breakpoints=total_breakpoints,
        )
        # NOTE: inc_llm_call_total 已移至 on_after_llm_call，确保与 duration 计数对称
        # （HITL pause/resume 边界可能导致 on_before 被触发但 on_after 被跳过）
        self._llm_call_model_by_run[run_id] = model  # 暂存 model，供 on_after 使用
        self._llm_call_provider_by_run[run_id] = provider  # 暂存 provider（平台名），供 on_after 写 trace/metrics
        _step_span = self._step_spans.get(run_id)
        self._llm_spans[run_id] = self._tracer.start_span(
            "llm.call", SpanType.LLM_CALL,
            run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0), session_id=session_id,
            parent_span_id=_step_span.span_id if _step_span else None,
            attributes={"message_count": len(messages), "model": model},
        )

    # ═══ Hook 6: on_after_llm_call ═══
    def on_after_llm_call(
        self, response: Any, run_id: str, model: str = "",
        *, duration_ms: float | None = None, call_type: str | None = None,
        session_id: str = "", provider: str = "",
    ) -> None:
        # 幂等保护：如果 _llm_call_start_by_run 已被消费（不存在），说明 on_after 已被调用过，跳过
        if run_id not in self._llm_call_start_by_run:
            return
        duration_ms = (time.perf_counter() - self._llm_call_start_by_run[run_id]) * 1000
        self._llm_call_start_by_run.pop(run_id, None)  # 消费后移除，防止重复记录
        usage, tool_calls_count = {}, 0
        if isinstance(response, dict):
            usage = response.get("usage", {})
            tc = response.get("tool_calls")
            tool_calls_count = len(tc) if tc else 0
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        # ── Prefix Cache 命中观测（PC1/PC3 效果验证）──
        # usage.prompt_tokens_details 由 llm/client.py 按 capabilities 声明统一解析，
        # 这里直接消费标准字段；不同 provider 的命中口径已归一：
        #   - cached_tokens：本次 prompt 中命中 prefix cache 的 token 数（OpenAI/百炼/火山/DeepSeek）
        #   - cache_creation_input_tokens：本次触发的缓存写入 token 数（仅 Anthropic 语义）
        ptd = usage.get("prompt_tokens_details", {}) or {}
        cached_tokens = ptd.get("cached_tokens", 0) or 0
        cache_created = ptd.get("cache_creation_input_tokens", 0) or 0
        hit_ratio = (100.0 * cached_tokens / input_tokens) if input_tokens else 0.0

        self._logger.info(
            f"LLM 响应: {duration_ms:.0f}ms | tokens: {input_tokens}->{output_tokens}"
            f" | cached: {cached_tokens} ({hit_ratio:.1f}%) | created: {cache_created}"
            f" | tool_calls: {tool_calls_count}",
            module="llm", run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0),
            session_id=session_id,
        )
        # total 和 duration 绑定在同一位置记录，确保 HITL pause/resume 不破坏对称性
        # Bug 8 Fix: _llm_call_model 在 __init__ 中已初始化，直接访问无需 getattr
        _model = model or self._llm_call_model_by_run.get(run_id, "")
        _provider = provider or self._llm_call_provider_by_run.get(run_id, "")  # 平台名，供按 provider 分账/统计
        # on_after 消费完暂存值即清理（防止 HITL pause 边界后重复读取旧值）
        self._llm_call_model_by_run.pop(run_id, None)
        self._llm_call_provider_by_run.pop(run_id, None)
        self._metrics.inc_llm_call_total(model=_model, provider=_provider)
        self._metrics.observe_llm_call_duration_ms(duration_ms, model=_model, provider=_provider)
        if input_tokens or output_tokens:
            self._metrics.record_tokens(input_tokens, output_tokens, model_name=_model, provider=_provider)
        _llm_span = self._llm_spans.get(run_id)
        if _llm_span and _llm_span.span_id:
            # SDK 不计价：只记 token/命中等**事实**，不挂金额 cost_usd。
            # 费用由应用层从这些事实 + 价格表自算（看板 cost_of_call / 运行时 StepGuard）。
            _attrs: dict[str, Any] = {
                "model": _model,
                # provider（平台名）：按 provider 分账/分组的一等事实。空串表示纯透传客户端
                # （无能力声明）；看板据此按 provider 聚合，不再靠 model 名反推（火山 endpoint id 反推必错）。
                "provider": _provider,
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
                "cache_creation_input_tokens": cache_created,
                "cache_hit_ratio": round(hit_ratio, 2),
                "tool_calls_count": tool_calls_count, "duration_ms": round(duration_ms, 1),
            }
            self._tracer.end_span(_llm_span, attributes=_attrs)
            self._llm_spans.pop(run_id, None)

    # ═══ Hook 7: on_before_tool_call（合并了原 ToolHooks.on_tool_execute_start）═══
    def on_before_tool_call(
        self, tool_name: str, args: dict[str, Any], run_id: str,
        *, step_n: int = 0, session_id: str = "",
    ) -> None:
        # Bug 1 Fix: 用列表队列支持同名工具并发调用，FIFO 顺序对应 on_after 的完成顺序；
        # 按 run_id 分桶，隔离多 session 并发。
        self._tool_call_starts_by_run.setdefault(run_id, {}).setdefault(tool_name, []).append(time.perf_counter())
        args_preview = str(args)[:80]
        self._logger.info(
            f"Tool 调用: {tool_name}({args_preview})",
            module="tool", run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0),
            session_id=session_id,
        )
        _step_span = self._step_spans.get(run_id)
        span = self._tracer.start_span(
            f"tool.{tool_name}", SpanType.TOOL_CALL,
            run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0), session_id=session_id,
            parent_span_id=_step_span.span_id if _step_span else None,
            attributes={"tool_name": tool_name},
        )
        self._tool_spans_by_run.setdefault(run_id, {}).setdefault(tool_name, []).append(span)

    # ═══ Hook 8: on_after_tool_call（合并了原 ToolHooks.on_tool_execute_end）═══
    def on_after_tool_call(
        self, tool_name: str, result: Any, run_id: str,
        *, step_n: int = 0, duration_ms: float = 0.0, session_id: str = "",
    ) -> None:
        # Bug 1 Fix: FIFO pop(0) 从队头取出最早的 start time 和 span（按 run_id 分桶）
        starts_bucket = self._tool_call_starts_by_run.get(run_id, {})
        starts = starts_bucket.get(tool_name)
        start = starts.pop(0) if starts else 0
        if starts is not None and not starts:
            starts_bucket.pop(tool_name, None)
        if not starts_bucket:
            self._tool_call_starts_by_run.pop(run_id, None)

        duration_ms = (time.perf_counter() - start) * 1000 if start else 0
        success, result_preview = True, ""
        if hasattr(result, "success"):
            success = result.success
            result_preview = (str(result.data)[:60] if result.data else "") if success else (result.error or "")
        else:
            result_preview = str(result)[:60]
        icon = "✅" if success else "❌"
        self._logger.info(
            f"Tool 结果: {tool_name} {icon} {duration_ms:.0f}ms -> {result_preview}",
            module="tool", run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0),
            session_id=session_id,
        )
        self._metrics.inc_tool_execute_total(tool_name, "success" if success else "error")
        self._metrics.observe_tool_execute_duration_ms(duration_ms, tool_name)

        spans_bucket = self._tool_spans_by_run.get(run_id, {})
        spans = spans_bucket.get(tool_name)
        span = spans.pop(0) if spans else None
        if spans is not None and not spans:
            spans_bucket.pop(tool_name, None)
        if not spans_bucket:
            self._tool_spans_by_run.pop(run_id, None)
        if span and span.span_id:
            self._tracer.end_span(
                span,
                status=SpanStatus.OK if success else SpanStatus.ERROR,
                attributes={"success": success, "duration_ms": round(duration_ms, 1)},
            )

    # ═══ Hook 9: on_hitl_requested ═══
    def on_hitl_requested(self, tool_name: str, run_id: str, *, session_id: str = "") -> None:
        """HITL 审批请求——记录 HITL 指标和日志，关闭当前 step span，开启独立 HITL span。"""
        self._logger.warn(
            f"HITL 审批请求: {tool_name}",
            module="loop", run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0),
            session_id=session_id,
        )
        self._metrics.inc_hitl_approval_total("need_approval")
        # Bug 2 Fix: step span 正常关闭（步骤被暂停），HITL span 存入独立字段
        # 不再把 HITL span 赋值给 _step_span，避免 on_step_end 错误地将其关闭
        _step_span = self._step_spans.get(run_id)
        if _step_span and _step_span.span_id:
            self._tracer.end_span(
                _step_span,
                status=SpanStatus.CANCELLED,
                attributes={"hitl_requested": True, "hitl_tool": tool_name},
            )
            self._step_spans.pop(run_id, None)
        _run_span = self._run_spans.get(run_id)
        self._hitl_spans[run_id] = self._tracer.start_span(
            f"hitl.{tool_name}", SpanType.HITL_CHECK,
            run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0), session_id=session_id,
            parent_span_id=_run_span.span_id if _run_span else None,
            attributes={"tool_name": tool_name},
        )

    # ═══ Hook 9b: on_hitl_resolved ═══
    def on_hitl_resolved(self, tool_name: str, decision: str, run_id: str, *, session_id: str = "") -> None:
        """HITL 审批被人工裁决——把审批「结果」计入指标，补齐 need_approval 的观测缺口。

        与 on_hitl_requested（记 need_approval）配对：
          decision="approved" → hitl_approval_total{result=approved}
          decision="rejected" → hitl_approval_total{result=rejected}
        使审批通过率 = approved / (approved + rejected) 在指标层可算。
        """
        _result = decision if decision in ("approved", "rejected") else "resolved"
        self._logger.info(
            f"HITL 审批裁决: {tool_name} → {_result}",
            module="loop", run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0),
            session_id=session_id,
        )
        self._metrics.inc_hitl_approval_total(_result)

    # ═══ Hook 10: on_error ═══
    def on_error(self, error: Exception, run_id: str, *, session_id: str = "") -> None:
        """错误事件——标记当前 span 为 error，记录 error_total 指标。"""
        self._logger.error(
            f"Error: {type(error).__name__}: {error}",
            module="loop", run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0),
            session_id=session_id,
        )
        self._metrics.inc_error_total(type(error).__name__)
        _step_span = self._step_spans.get(run_id)
        if _step_span:
            self._tracer.mark_span_error(_step_span)

    # ═══ Hook 11: on_halt ═══
    def on_halt(self, reason: str, run_id: str, *, session_id: str = "") -> None:
        """Agent 被终止/暂停——只记录日志。"""
        self._logger.warn(
            f"Halt: {reason}",
            module="loop", run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0),
            session_id=session_id,
        )

    # ════════════════════════════════════════════════════════════════════════
    #  E. Tool 管理事件（新增）
    # ════════════════════════════════════════════════════════════════════════

    # ═══ Hook 12: on_tool_register ═══
    def on_tool_register(
        self, tool_name: str,
        tier: Any, sensitivity: Any,
        namespace: str | None,
    ) -> None:
        """工具注册成功——记录日志。"""
        self._logger.debug(
            f"工具注册: {tool_name} [tier={tier.name if hasattr(tier, 'name') else tier},"
            f" sensitivity={sensitivity.name if hasattr(sensitivity, 'name') else sensitivity},"
            f" namespace={namespace}]",
            module="tool",
        )

    # ═══ Hook 13: on_tool_discover ═══
    def on_tool_discover(self, tool_name: str, query: str, run_id: str, *, session_id: str = "") -> None:
        """LLM 通过 ToolSearch 发现了新工具——记录日志。"""
        self._logger.info(
            f"Tool 发现: {tool_name} (query={query!r})",
            module="tool", run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0),
            session_id=session_id,
        )

    # ═══ Hook 14: on_tool_disabled ═══
    def on_tool_disabled(self, tool_name: str, reason: str, run_id: str, *, session_id: str = "") -> None:
        """工具变为不可用——记录日志。"""
        self._logger.warn(
            f"Tool 不可用: {tool_name} ({reason})",
            module="tool", run_id=run_id, step_n=self._step_n_by_run.get(run_id, 0),
            session_id=session_id,
        )

    # ════════════════════════════════════════════════════════════════════════
    #  F. Harness 事件（新增——填补原来的观测盲区）
    # ════════════════════════════════════════════════════════════════════════

    # ═══ Hook 15: on_tool_circuit_open ═══
    def on_tool_circuit_open(
        self, tool_name: str,
        failure_count: int, recovery_timeout: float,
    ) -> None:
        """熔断器触发——记录日志 + 指标。"""
        self._logger.warn(
            f"熔断器触发: {tool_name} (failures={failure_count},"
            f" recovery_timeout={recovery_timeout:.1f}s)",
            module="harness",
        )
        self._metrics.increment_counter(
            "tool_circuit_breaker_total",
            labels={"tool_name": tool_name, "action": "open"},
        )

    # ═══ Hook 16: on_tool_circuit_close ═══
    def on_tool_circuit_close(self, tool_name: str) -> None:
        """熔断器恢复——记录日志 + 指标。"""
        self._logger.info(
            f"熔断器恢复: {tool_name}",
            module="harness",
        )
        self._metrics.increment_counter(
            "tool_circuit_breaker_total",
            labels={"tool_name": tool_name, "action": "close"},
        )

    # ═══ Hook 17: on_tool_output_truncated ═══
    def on_tool_output_truncated(
        self, tool_name: str,
        original_size: int, max_size: int,
    ) -> None:
        """输出被截断——记录日志。"""
        self._logger.warn(
            f"输出截断: {tool_name} ({original_size} 字节 → {max_size} 字节)",
            module="harness",
        )

    # ═══ Hook 18: on_concurrent_execution_failure ═══
    def on_concurrent_execution_failure(
        self, tool_names: list[str],
        run_id: str, step_n: int, *, session_id: str = "",
    ) -> None:
        """并发执行中有工具失败——记录日志 + 指标。"""
        self._logger.warn(
            f"并发执行失败: {tool_names}",
            module="harness", run_id=run_id, step_n=step_n, session_id=session_id,
        )
        self._metrics.inc_error_total("concurrent_tool_failure")

    def __repr__(self) -> str:
        return "ObservabilityHooksAdapter(logger, tracer, metrics)"
