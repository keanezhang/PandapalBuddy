"""pandapal/dashboard/base.py — Dashboard 聚合的**存储无关装配核心**。

markdown 与 sqlite 两条读取链路只在「怎么把原始数据取出来」不同；一旦取成同一套
**归一结构**（session meta / span 列表 / raw_log 轮次 / audit 结束原因），
后续「怎么拼成 DashboardSnapshot」——尤其是 assistant 轮 ↔ llm_call 的
(run_id, step) 精确 join + run 内顺序补配、费用精算——是完全一致的。

故此处把装配核心抽成基类 `BaseDashboardAggregator`，两个子类各自提供解析/查询、
再调用同一 `_assemble_session` / `_join_turns`，杜绝两套实现随时间漂移。

归一结构契约（子类必须产出）：
  - meta:   dict —— session_id/title/preview/created_at/last_active/message_count/group_id/model
  - spans:  list[dict] —— 每条 kind ∈ {llm, tool, run, step}，字段见 _assemble_session 内注释
  - raw_turns: list[dict] —— turn/role/timestamp/run_id/step/content/reasoning/tool_calls
  - audit_fin: dict[str, str] —— {run_id[:8]: run_finished detail}
  - groups: dict[str, str] —— {group_id: name}
  - system_prompt: str
  - counters: list[CounterPoint] —— **保 label** 的 counter 采样点（见 CounterPoint 文档）
  - gauges: dict[str, float] —— gauge 取最新值（无 label 维度需求）

字段出处/口径见 docs/prd/dashboard/dashboard-需求设计.md §3。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pandapal.config.budget.pricing import cost_of_call
from pandapal.dashboard.models import (
    DashboardSnapshot,
    DegradationStat,
    GlobalMetrics,
    RunInfo,
    SessionData,
    ToolSpan,
    ToolStat,
    Turn,
    TurnLLM,
)
from pandapal.degradation import DEGRADATION_COUNTER_NAME

logger = logging.getLogger("pandapal.dashboard.aggregator")


@dataclass(frozen=True)
class CounterPoint:
    """归一后的一个 counter 采样点：name + **原样保留的 labels** + 累计值。

    为什么必须保 labels（这是本类型存在的全部理由）：
      早先两条链路的归一结构是 `dict[name, float]`，labels 在进基类前就被拍平了。
      后果是任何「靠 label 区分档位」的指标都进不了看板，除非在**两个子类里各抄一段
      特例**把 label 拼进 key——`run_total` 就是这么被抄成 `run_total_{status}` 的。
      再多一个带 label 的指标（如 degradation 的 event_code），就得再抄一遍两处。

      改为保 label 后，投影（该看哪些 name、按哪些 label 切档）**只在基类发生一次**，
      子类退回纯粹的「把存储读成点」，与本模块 docstring 声称的分工一致。
    """
    name: str
    labels: dict[str, str] = field(default_factory=dict)
    value: float = 0.0


# 降级严重度排序权重（看板按「越严重越靠前」呈现）。与 degradation.Severity 三档对齐。
_SEVERITY_RANK: dict[str, int] = {"abort": 3, "ui_bubble": 2, "log_only": 1}


# ── 归一化 & 数值工具（两条链路共用）────────────────────────────────────
def _rid(run_id: str) -> str:
    """归一化 run_id → 前 8 位（与 traces 呈现一致），作 join key。"""
    return (run_id or "")[:8]


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _i(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# terminal_reason（trace 结束原因）→ 人类可读中文标签。暂停类不是失败，措辞中性。
_TERMINAL_REASON_LABEL: dict[str, str] = {
    "completed": "正常完成",
    "plan_complete": "规划完成（待审批）",
    "interaction_paused": "等待用户回复",
    "hitl_paused": "等待人工审批",
    "hitl_rejected": "人工拒绝",
    "halted_by_guard": "已停机（应用层守卫）",
    "max_steps_exceeded": "达到最大步数",
    "step_timeout": "单步超时",
    "total_timeout": "总时长超时",
    "context_overflow": "上下文溢出",
    "tool_halt": "工具主动终止",
    "cancelled": "已取消",
    "llm_error": "LLM 调用失败",
    "circuit_breaker": "熔断触发",
}

# 暂停/正常类结束原因：显示用友好中文标签而非冗长 audit 详情（错误类仍优先 audit 详情）。
_BENIGN_TERMINAL_REASONS: frozenset[str] = frozenset({
    "completed", "plan_complete", "interaction_paused", "hitl_paused",
})


def _friendly_terminal_reason(reason: str) -> str:
    """把 trace 的 terminal_reason 映射成中文标签；未知/空 → 原样或「未记录」。"""
    r = (reason or "").strip()
    if not r:
        return "未记录结束原因"
    return _TERMINAL_REASON_LABEL.get(r, r)


@runtime_checkable
class DashboardSource(Protocol):
    """看板数据源统一接口：markdown / sqlite 两态对 handler 透明。"""

    def build(self) -> DashboardSnapshot:
        ...


class BaseDashboardAggregator:
    """存储无关的装配核心。子类提供解析/查询，调用本类装配方法产出 DashboardSnapshot。"""

    # ── counter 投影（唯一投影点；两条链路共用）─────────────────────────
    @staticmethod
    def _sum_counters(points: list[CounterPoint], name: str, **label_eq: str) -> float:
        """按 name 求和；给了 label_eq 则再按这些 label **精确相等**过滤。

        这是「带 label 指标 → 标量」的通用表达，取代了原先在两个子类里各写一遍的
        `counters[f"run_total_{status}"]` 特例。
        """
        return sum(
            p.value for p in points
            if p.name == name
            and all(p.labels.get(k) == v for k, v in label_eq.items())
        )

    # ── 全局指标：CounterPoint 列表 → GlobalMetrics ──────────────────────
    @classmethod
    def _build_global_metrics(
        cls,
        last_updated: str,
        points: list[CounterPoint],
        gauges: dict[str, float],
    ) -> GlobalMetrics:
        # agent_id 从任一带该 label 的点上取（单 agent 部署下全局一致）；从前由两个
        # 子类各自 `agent_id or labels.get(...)` 推导，现收敛到此处一份。
        agent_id = next(
            (p.labels["agent_id"] for p in points if p.labels.get("agent_id")), "",
        )

        def n(name: str, **label_eq: str) -> int:
            return _i(cls._sum_counters(points, name, **label_eq))

        return GlobalMetrics(
            agent_id=agent_id,
            last_updated=last_updated,
            llm_call_total=n("llm_call_total"),
            llm_input_tokens_total=n("llm_input_tokens_total"),
            llm_output_tokens_total=n("llm_output_tokens_total"),
            error_total=n("error_total"),
            run_total_started=n("run_total", status="started"),
            run_total_success=n("run_total", status="success"),
            run_total_failed=n("run_total", status="failed"),
            step_total=n("step_total"),
            active_runs=gauges.get("active_runs", 0.0),
        )

    # ── 降级明细：CounterPoint 列表 → DegradationStat 列表 ────────────────
    @staticmethod
    def _build_degradations(points: list[CounterPoint]) -> list[DegradationStat]:
        """把 `degradation` counter 按 (event_code, category, severity, source) 归并。

        同一分组键可能有多个点（不同 agent_id、或 markdown 分片多行），故求和而非取值。
        排序：严重度降序 → 次数降序，让 abort 类永远排在最上面。
        """
        agg: dict[tuple[str, str, str, str], float] = {}
        for p in points:
            if p.name != DEGRADATION_COUNTER_NAME:
                continue
            key = (
                p.labels.get("event_code", ""),
                p.labels.get("category", ""),
                p.labels.get("severity", ""),
                p.labels.get("source", ""),
            )
            agg[key] = agg.get(key, 0.0) + p.value
        stats = [
            DegradationStat(
                event_code=ec, category=cat, severity=sev, source=src, count=_i(v),
            )
            for (ec, cat, sev, src), v in agg.items()
        ]
        stats.sort(key=lambda d: (-_SEVERITY_RANK.get(d.severity, 0), -d.count))
        return stats

    # ── 单会话装配（storage-agnostic）────────────────────────────────────
    def _assemble_session(
        self,
        meta: dict[str, Any],
        spans: list[dict[str, Any]],
        audit_fin: dict[str, str],
        raw_turns: list[dict[str, Any]],
        system_prompt: str,
        groups: dict[str, str],
        tools_schema: list[dict] | None = None,
        fallback_id: str = "",
    ) -> SessionData | None:
        """把一个会话的归一结构装配成 SessionData。

        spans 每条形状：
          llm  : kind=llm,  run, step, status, model, provider, input_tokens,
                 output_tokens, cached_tokens|None, cache_hit_ratio|None,
                 tool_calls_count, duration_ms
          tool : kind=tool, run, step, tool_name, status, duration_ms
          run  : kind=run,  run, status, terminal_reason, duration_ms
          step : kind=step, run, step
        """
        sid = meta.get("session_id") or fallback_id
        if not sid:
            return None

        llm_calls = [s for s in spans if s["kind"] == "llm"]
        tool_spans = [s for s in spans if s["kind"] == "tool"]
        run_spans = [s for s in spans if s["kind"] == "run"]
        step_spans = [s for s in spans if s["kind"] == "step"]

        # runs：一个 run_id 可能有多个 run span（pause/resume 各写一个）。以最后一个 span
        # 为最终结局；状态/原因优先取 audit run_finished（权威），缺失则回退 trace 的
        # terminal_reason。
        last_run_span: dict[str, dict[str, Any]] = {}
        run_dur_sum: dict[str, float] = {}
        for r in run_spans:
            rid = _rid(r["run"])
            last_run_span[rid] = r  # 后写覆盖 → 保留最后一个（= 最终结局）
            run_dur_sum[rid] = run_dur_sum.get(rid, 0.0) + r["duration_ms"]
        runs: list[RunInfo] = []
        for rid, r in last_run_span.items():
            audit_reason = audit_fin.get(rid, "")
            trace_reason = (r.get("terminal_reason", "") or "").strip()
            ok = audit_reason.startswith("Completed") or r["status"] == "ok"
            status = "ok" if ok else r["status"]  # 末 span 仍 cancelled=真暂停未恢复
            if trace_reason in _BENIGN_TERMINAL_REASONS:
                reason = _friendly_terminal_reason(trace_reason)
            else:
                reason = audit_reason or _friendly_terminal_reason(trace_reason)
            runs.append(RunInfo(
                id=rid, status=status, duration_ms=run_dur_sum.get(rid, 0.0),
                finish_reason=reason,
            ))

        # (run_id, step) → llm_call
        llm_by_key: dict[tuple[str, int], dict[str, Any]] = {}
        llm_ordered: list[dict[str, Any]] = []
        for c in llm_calls:
            llm_ordered.append(c)
            if c["step"] is not None:
                llm_by_key[(_rid(c["run"]), c["step"])] = c

        # (run_id, step) → tool_call spans（真实耗时）
        tool_by_key: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for t in tool_spans:
            if t["step"] is not None:
                tool_by_key.setdefault((_rid(t["run"]), t["step"]), []).append(t)

        turns = self._join_turns(
            raw_turns, llm_by_key, llm_ordered, tool_by_key, label=fallback_id or sid,
        )

        # 会话级聚合（token 与 cost 基于同一调用集：已 join 的 turns.llm）
        joined_llms = [t.llm for t in turns if t.llm is not None]
        input_tokens = sum(l.input_tokens for l in joined_llms)
        output_tokens = sum(l.output_tokens for l in joined_llms)
        cost = round(sum(l.net_cost_usd or 0.0 for l in joined_llms), 8)
        durations = [c["duration_ms"] for c in llm_calls]
        models = list(dict.fromkeys(c["model"] for c in llm_calls if c["model"]))
        model = models[0] if len(models) == 1 else ("混合" if models else meta.get("model", ""))

        # 工具聚合（by name）
        tool_map: dict[str, list[float]] = {}
        for t in tool_spans:
            tool_map.setdefault(t["tool_name"], []).append(t["duration_ms"])
        tools = [
            ToolStat(name=n, count=len(ds), duration_ms=round(sum(ds) / len(ds), 1))
            for n, ds in sorted(tool_map.items(), key=lambda kv: -len(kv[1]))
        ]

        step_count = len(step_spans) or len(
            {c["step"] for c in llm_calls if c["step"] is not None}
        )

        return SessionData(
            id=sid,
            session_id=_rid(sid) if sid.startswith("sess-") else sid[:12],
            title=meta.get("title", "") or "未命名会话",
            preview=meta.get("preview", ""),
            created_at=meta.get("created_at", ""),
            last_active=meta.get("last_active", ""),
            message_count=_i(meta.get("message_count", 0)),
            group_name=groups.get(meta.get("group_id") or "", None) or None,
            model=model,
            llm_calls=len(llm_calls),
            step_count=step_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            llm_durations=durations,
            tools=tools,
            runs=runs,
            turns=turns,
            system_prompt=system_prompt,
            tools_schema=tools_schema or [],
        )

    # ── raw_turns × llm_call/tool span → Turn 列表（storage-agnostic）────
    def _join_turns(
        self,
        raw_turns: list[dict[str, Any]],
        llm_by_key: dict[tuple[str, int], dict[str, Any]],
        llm_ordered: list[dict[str, Any]],
        tool_by_key: dict[tuple[str, int], list[dict[str, Any]]] | None = None,
        label: str = "",
    ) -> list[Turn]:
        tool_by_key = tool_by_key or {}
        turns: list[Turn] = []
        assistant_seq = 0  # 回退用（仅旧数据）：第 k 个 assistant ↔ 第 k 个 llm_call
        # run_id → 该 run 的 llm_call 有序列表（按 llm_call 自身 step），用于 run 内顺序补配。
        llm_by_run: dict[str, list[dict[str, Any]]] = {}
        for c in llm_ordered:
            if c["run"]:
                llm_by_run.setdefault(_rid(c["run"]), []).append(c)
        for seq in llm_by_run.values():
            seq.sort(key=lambda c: (c["step"] is None, c["step"] if c["step"] is not None else 0))
        consumed: set[int] = set()  # 已被认领的 llm span（id()），防重复归属
        recovered = 0  # 精确 key 漏配、靠 run 内顺序补配成功（正常现象，仅 debug）
        join_miss = 0  # 真丢配：run 内已无可认领 llm_call
        for rt in raw_turns:
            llm: TurnLLM | None = None
            tool_spans_turn: list[ToolSpan] = []
            if rt["role"] == "assistant" and rt["run_id"] and rt["step"] is not None:
                for t in tool_by_key.get((_rid(rt["run_id"]), rt["step"]), []):
                    tool_spans_turn.append(ToolSpan(
                        name=t["tool_name"], duration_ms=t["duration_ms"],
                        status=t.get("status", "ok"),
                    ))
            if rt["role"] == "assistant":
                c = None
                rid = _rid(rt["run_id"])
                if rt["run_id"]:
                    # ① 精确 key：(run_id, step) 命中且未被认领。
                    cand = llm_by_key.get((rid, rt["step"])) if rt["step"] is not None else None
                    if cand is not None and id(cand) not in consumed:
                        c = cand
                    else:
                        # ② run 内顺序补配：取该 run 尚未认领的第一个 llm_call。
                        for cand in llm_by_run.get(rid, []):
                            if id(cand) not in consumed:
                                c = cand
                                recovered += 1
                                break
                        if c is None:
                            join_miss += 1
                    if c is not None:
                        consumed.add(id(c))
                elif assistant_seq < len(llm_ordered):
                    # 旧数据（无 run_id）→ 全局顺序对齐（保留原行为）。
                    c = llm_ordered[assistant_seq]
                assistant_seq += 1
                if c is not None:
                    cc = cost_of_call(
                        c["model"], c["input_tokens"], c["output_tokens"], c["cached_tokens"] or 0
                    )
                    llm = TurnLLM(
                        model=c["model"],
                        provider=c.get("provider", ""),
                        input_tokens=c["input_tokens"],
                        output_tokens=c["output_tokens"],
                        duration_ms=c["duration_ms"],
                        cached_tokens=c["cached_tokens"],
                        cache_hit_ratio=c["cache_hit_ratio"],
                        step=c["step"] if c["step"] is not None else -1,
                        tool_calls_count=c["tool_calls_count"],
                        input_cost_usd=cc.input_usd,
                        output_cost_usd=cc.output_usd,
                        net_cost_usd=cc.net_usd,
                        cache_saved_usd=cc.saved_usd,
                    )
            turns.append(
                Turn(
                    turn=rt["turn"],
                    role=rt["role"],
                    timestamp=rt["timestamp"],
                    content=rt["content"],
                    run_id=_rid(rt["run_id"]),
                    reasoning=rt["reasoning"],
                    tool_calls=rt["tool_calls"],
                    tool_spans=tool_spans_turn,
                    llm=llm,
                )
            )
        if join_miss:
            logger.warning(
                "[aggregator] raw_log↔traces join miss: %d assistant turn(s) in %s "
                "had a run_id but no unclaimed llm_call span in that run "
                "(assistant turns outnumber llm_call spans); those turns show no token/cost "
                "(见 D2/R5)",
                join_miss, label,
            )
        if recovered:
            logger.debug(
                "[aggregator] %s: %d assistant turn(s) recovered via in-run ordered join "
                "(step-label divergence from interaction/plan pause-resume; not data loss).",
                label, recovered,
            )
        return turns

