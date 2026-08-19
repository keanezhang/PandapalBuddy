"""pandapal/dashboard/tests/test_aggregator.py — DashboardAggregator 单测。

覆盖：metrics/session/traces/raw_log/audit/groups 解析 + turn↔llm_call 的
(run_id, step) key join + 旧数据（无 run_id/step）顺序回退 + 多 run 分段
+ 工具 per-call 真时长（tool_spans）+ run 失败原因（finish_reason）
+ turn.run_id + logs.md 纯读系统提示词（system_prompt，含 markdown \\| 转义还原）。
"""

from __future__ import annotations

from pathlib import Path

from pandapal.config.budget.pricing import cny_to_usd
from pandapal.dashboard.aggregator import DashboardAggregator


# ── fixture builders ────────────────────────────────────────────────
def _write_session(root: Path, sid: str, *, title: str, created: str,
                   traces: str, raw_log: str, audit: str, logs: str = "") -> None:
    d = root / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.md").write_text(
        '---\n{"session_id": "%s", "title": "%s", "preview": "%s", '
        '"created_at": "%s", "last_active": "%s", "message_count": 2, "group_id": null}\n---\n'
        % (sid, title, title, created, created),
        encoding="utf-8",
    )
    (d / "traces.md").write_text(traces, encoding="utf-8")
    (d / "raw_log.md").write_text(raw_log, encoding="utf-8")
    (d / "audit.md").write_text(audit, encoding="utf-8")
    if logs:
        (d / "logs.md").write_text(logs, encoding="utf-8")


_TRACES_HDR = (
    "# Trace Spans\n\n| 时间 | 类型 | 名称 | 状态 | 结束原因 | 耗时(ms) | Step | Run | 属性 |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)


def _llm_row(t, step, run, model, i, o, cost, cached, ratio, tcc, dur):
    return (f"| {t} | 🤖 llm_call | `llm.call` | ✅ ok |  | **{int(dur)}** | {step} | `{run}` | "
            f"`model`={model}, `input_tokens`={i}, `output_tokens`={o}, `cached_tokens`={cached}, "
            f"`cache_hit_ratio`={ratio}, `cost_usd`={cost}, `tool_calls_count`={tcc}, `duration_ms`={dur} |\n")


def _run_row(t, run, dur, status="ok", reason=""):
    icon = "✅ ok" if status == "ok" else ("⏸️ cancelled" if status == "cancelled" else "❌ error")
    return f"| {t} | 🚀 run | `agent.run` | {icon} | {reason} | **{int(dur)}** |  | `{run}` | `duration_ms`={dur} |\n"


def _tool_row(t, step, run, name, dur):
    return (f"| {t} | 🔧 tool_call | `tool.{name}` | ✅ ok |  | **{int(dur)}** | {step} | `{run}` | "
            f"`tool_name`={name}, `duration_ms`={dur} |\n")


def _logs_with_system(run, step, system_content):
    """构造 logs.md：llm 日志详情列内嵌 messages JSON；内容里的 | 按 markdown 转义为 \\|。"""
    hdr = ("# Logs\n\n> Auto-generated.\n\n| 时间 | 级别 | 事件 | 详情 | Agent | Session | Run | Step |\n"
           "|---|---|---|---|---|---|---|---|\n")
    sys_escaped = system_content.replace("|", "\\|")  # 模拟 markdown 后端转义
    msgs = ('{"messages": [{"role": "system", "content": "%s"}, '
            '{"role": "user", "content": "q"}]}' % sys_escaped)
    return hdr + (f"| 00:00:01 | ℹ️ INFO | `llm` | LLM 调用中... | pandapal | `s` | `{run}` | {step} | {msgs} |\n")


def _logs_with_system_newfmt(run, step, system_content):
    """新格式（87aea2c 后）：log_id 是 record 首键，extra JSON 以 {\"log_id\": 开头。"""
    hdr = ("# Logs\n\n> Auto-generated.\n\n| 时间 | 级别 | 事件 | 详情 | Agent | Session | Run | Step |\n"
           "|---|---|---|---|---|---|---|---|\n")
    sys_escaped = system_content.replace("|", "\\|")
    msgs = ('{"log_id": "8f040f6a4076455c9be1ab6bc6e7fe5a", '
            '"messages": [{"role": "system", "content": "%s"}, '
            '{"role": "user", "content": "q"}], "tools": []}' % sys_escaped)
    return hdr + (f"| 00:00:01 | ℹ️ INFO | `llm` | LLM 调用中... | pandapal | `s` | `{run}` | {step} | {msgs} |\n")


def _audit_reason(run, detail):
    hdr = "# Audit Log\n\n| 时间 | 级别 | 事件 | Agent | Session | Run | Step | 详情 |\n|---|---|---|---|---|---|---|---|\n"
    return hdr + f"| 00:00:00 | ℹ️ INFO | `run_finished` | pandapal | `s` | `{run}` | 1 | {detail} |\n"


def _turn(idx, role, content, *, run_id=None, step=None, tool=None, reasoning=None):
    import json as _j
    msg: dict = {"role": role, "content": content}
    if tool:
        msg["tool_calls"] = [{"id": "c1", "type": "function",
                              "function": {"name": tool[0], "arguments": _j.dumps(tool[1])}}]
    if reasoning:
        msg["reasoning_content"] = reasoning
    s = f"\n## Turn {idx}\n\n- **role**: {role}\n- **timestamp**: 2026-07-13T00:00:0{idx}+00:00\n- **type**: message\n"
    if run_id is not None:
        s += f"- **run_id**: {run_id}\n- **step**: {'' if step is None else step}\n"
    s += f"\n```\n{content}\n```\n\n**message_json**:\n```json\n{_j.dumps(msg, ensure_ascii=False)}\n```\n"
    return s


def _audit(run, ok=True):
    hdr = "# Audit Log\n\n| 时间 | 级别 | 事件 | Agent | Session | Run | Step | 详情 |\n|---|---|---|---|---|---|---|---|\n"
    fin = "Completed normally" if ok else "Terminated"
    return hdr + f"| 00:00:00 | ℹ️ INFO | `run_finished` | pandapal | `s` | `{run}` | 1 | {fin} |\n"


def test_key_join_multi_run(tmp_path: Path):
    """同一 session 两个 run，raw_log 带 run_id/step → 按 (run_id, step) 精确 join，不串轮。"""
    root = tmp_path
    (root / "metrics.md").write_text(
        "# Metrics Summary\n\n> Last updated: 2026-07-13T00:00:00+00:00\n\n## Counters\n\n"
        "| llm_call_total{agent_id=pandapal,model=m,status=success} | 3 |\n"
        "| run_total{agent_id=pandapal,status=success} | 2 |\n\n## Gauges\n\n"
        "| token_cost_total_usd{agent_id=pandapal} | 0.0300 |\n",
        encoding="utf-8",
    )
    # run A: steps 0,1 ; run B: step 0  (两个 run 的 step 都从 0 开始 → 顺序 zip 会串)
    traces = _TRACES_HDR
    traces += _llm_row("00:00:01", 0, "aaaaaaaa", "m", 100, 10, 0.001, 90, 90.0, 1, 500)
    traces += _llm_row("00:00:02", 1, "aaaaaaaa", "m", 200, 20, 0.002, 180, 90.0, 0, 600)
    traces += _run_row("00:00:03", "aaaaaaaa", 1100)
    traces += _llm_row("00:00:04", 0, "bbbbbbbb", "m", 300, 30, 0.003, 270, 90.0, 0, 700)
    traces += _run_row("00:00:05", "bbbbbbbb", 700)
    # raw_log: run A (user, asst step0, tool, asst step1), run B (user, asst step0)
    raw = (_turn(0, "user", "qA", run_id="aaaaaaaa", step=None)
           + _turn(1, "assistant", "", run_id="aaaaaaaa", step=0, tool=("t", {}))
           + _turn(2, "tool", "rA", run_id="aaaaaaaa", step=0)
           + _turn(3, "assistant", "ansA", run_id="aaaaaaaa", step=1)
           + _turn(4, "user", "qB", run_id="bbbbbbbb", step=None)
           + _turn(5, "assistant", "ansB", run_id="bbbbbbbb", step=0))
    audit = _audit("aaaaaaaa") + "| 00:00:05 | ℹ️ INFO | `run_finished` | pandapal | `s` | `bbbbbbbb` | 0 | Completed normally |\n"
    _write_session(root, "sess-x", title="multi", created="2026-07-13 00:00:00",
                   traces=traces, raw_log=raw, audit=audit)

    snap = DashboardAggregator(root).build()
    assert len(snap.sessions) == 1
    s = snap.sessions[0]
    by_turn = {t.turn: t for t in s.turns}
    # 精确 join：T1→(A,0) in100, T3→(A,1) in200, T5→(B,0) in300
    assert by_turn[1].llm.input_tokens == 100 and by_turn[1].llm.step == 0
    assert by_turn[3].llm.input_tokens == 200 and by_turn[3].llm.step == 1
    assert by_turn[5].llm.input_tokens == 300 and by_turn[5].llm.step == 0
    # user/tool 轮无 llm
    assert by_turn[0].llm is None and by_turn[2].llm is None
    # 会话聚合
    assert s.llm_calls == 3
    assert s.input_tokens == 600 and s.output_tokens == 60
    # cost = Σ 轮次 net_cost_usd（应用层价格表精算）；model "m" 不在 APP_PRICE_TABLE
    # → 每轮 net 记 0（Fail-Safe，SDK 亦无兜底价）→ 会话净费用 0.0。
    assert s.cost == 0.0
    assert [r.status for r in s.runs] == ["ok", "ok"]


def test_fallback_order_join_legacy(tmp_path: Path):
    """旧数据：raw_log 无 run_id/step → 按 assistant 顺序回退 join（单 run）。"""
    root = tmp_path
    (root / "metrics.md").write_text("# Metrics Summary\n\n> Last updated: x\n", encoding="utf-8")
    traces = _TRACES_HDR
    traces += _llm_row("00:00:01", 0, "aaaaaaaa", "m", 100, 10, 0.001, 90, 90.0, 1, 500)
    traces += _llm_row("00:00:02", 1, "aaaaaaaa", "m", 200, 20, 0.002, 180, 90.0, 0, 600)
    traces += _run_row("00:00:03", "aaaaaaaa", 1100)
    raw = (_turn(0, "user", "q")           # 无 run_id/step
           + _turn(1, "assistant", "", tool=("t", {}))
           + _turn(2, "tool", "r")
           + _turn(3, "assistant", "ans"))
    _write_session(root, "sess-y", title="legacy", created="2026-07-12 00:00:00",
                   traces=traces, raw_log=raw, audit=_audit("aaaaaaaa"))
    snap = DashboardAggregator(root).build()
    s = snap.sessions[0]
    by_turn = {t.turn: t for t in s.turns}
    assert by_turn[1].llm.input_tokens == 100   # 第 1 个 assistant ↔ 第 1 个 llm
    assert by_turn[3].llm.input_tokens == 200   # 第 2 个 assistant ↔ 第 2 个 llm
    assert s.cost == 0.0  # model "m" 未在 APP_PRICE_TABLE → net 记 0


def test_tool_spans_and_finish_reason(tmp_path: Path):
    """P1-A/B：tool_call span 真时长按 (run_id, step) 挂到 assistant 轮；
    turn.run_id 落盘；失败 run 的 finish_reason 取 audit run_finished detail。"""
    root = tmp_path
    (root / "metrics.md").write_text("# Metrics Summary\n\n> Last updated: x\n", encoding="utf-8")
    traces = _TRACES_HDR
    traces += _llm_row("00:00:01", 0, "aaaaaaaa", "m", 100, 10, 0.001, 90, 90.0, 1, 500)
    traces += _tool_row("00:00:01", 0, "aaaaaaaa", "enter_plan_mode", 46.1)   # 与 llm 同 step
    traces += _llm_row("00:00:02", 1, "aaaaaaaa", "m", 200, 20, 0.002, 180, 90.0, 1, 600)
    traces += _tool_row("00:00:02", 1, "aaaaaaaa", "write_file", 2.1)
    traces += _run_row("00:00:03", "aaaaaaaa", 1100, status="error")           # span 层 error
    raw = (_turn(0, "user", "q", run_id="aaaaaaaa", step=None)
           + _turn(1, "assistant", "", run_id="aaaaaaaa", step=0, tool=("enter_plan_mode", {}))
           + _turn(2, "tool", "r", run_id="aaaaaaaa", step=0)
           + _turn(3, "assistant", "", run_id="aaaaaaaa", step=1, tool=("write_file", {})))
    # 无 "Completed" → 失败，reason = detail
    audit = _audit_reason("aaaaaaaa", "Terminated: HITL rejected for write_file")
    _write_session(root, "sess-t", title="tools", created="2026-07-13 00:00:00",
                   traces=traces, raw_log=raw, audit=audit)

    s = DashboardAggregator(root).build().sessions[0]
    by_turn = {t.turn: t for t in s.turns}
    # 工具真时长精确落位到对应 (run, step) 的 assistant 轮
    assert [(x.name, x.duration_ms) for x in by_turn[1].tool_spans] == [("enter_plan_mode", 46.1)]
    assert [(x.name, x.duration_ms) for x in by_turn[3].tool_spans] == [("write_file", 2.1)]
    # user/tool 轮不挂 tool_spans
    assert by_turn[0].tool_spans == [] and by_turn[2].tool_spans == []
    # turn.run_id 落盘（run_id[:8]）
    assert by_turn[1].run_id == "aaaaaaaa"
    # 失败原因（audit run_finished detail 非 Completed → error + reason）
    assert s.runs[0].status == "error"
    assert s.runs[0].finish_reason == "Terminated: HITL rejected for write_file"


def test_finish_reason_from_trace_terminal_reason(tmp_path: Path):
    """无 run_finished 审计时，finish_reason 回退取 trace 的 terminal_reason（映射中文）。"""
    root = tmp_path
    (root / "metrics.md").write_text("# Metrics Summary\n\n> Last updated: x\n", encoding="utf-8")
    traces = _TRACES_HDR
    traces += _llm_row("00:00:01", 0, "aaaaaaaa", "m", 100, 10, 0.001, 90, 90.0, 0, 500)
    traces += _run_row("00:00:02", "aaaaaaaa", 500, status="error", reason="llm_error")
    raw = (_turn(0, "user", "q", run_id="aaaaaaaa", step=None)
           + _turn(1, "assistant", "ans", run_id="aaaaaaaa", step=0))
    audit = "# Audit Log\n\n| 时间 | 级别 | 事件 | Agent | Session | Run | Step | 详情 |\n|---|---|---|---|---|---|---|---|\n"
    _write_session(root, "sess-nf", title="nofin", created="2026-07-13 00:00:00",
                   traces=traces, raw_log=raw, audit=audit)
    s = DashboardAggregator(root).build().sessions[0]
    assert s.runs[0].status == "error"
    assert s.runs[0].finish_reason == "LLM 调用失败"  # llm_error → 中文标签


def test_pause_then_resume_final_outcome_wins(tmp_path: Path):
    """一个 run_id 多个 run span（暂停→恢复）：以最后一个 span 为最终结局，不被中途暂停误导。"""
    root = tmp_path
    (root / "metrics.md").write_text("# Metrics Summary\n\n> Last updated: x\n", encoding="utf-8")
    traces = _TRACES_HDR
    traces += _llm_row("00:00:01", 0, "aaaaaaaa", "m", 100, 10, 0.001, 90, 90.0, 0, 500)
    # 中途因交互暂停（cancelled），恢复后规划完成（ok/plan_complete）——末 span 为准
    traces += _run_row("00:00:02", "aaaaaaaa", 500, status="cancelled", reason="interaction_paused")
    traces += _llm_row("00:00:05", 1, "aaaaaaaa", "m", 100, 10, 0.001, 90, 90.0, 1, 300)
    traces += _run_row("00:00:06", "aaaaaaaa", 800, status="ok", reason="plan_complete")
    raw = (_turn(0, "user", "q", run_id="aaaaaaaa", step=None)
           + _turn(1, "assistant", "ans", run_id="aaaaaaaa", step=0))
    audit = "# Audit Log\n\n| 时间 | 级别 | 事件 | Agent | Session | Run | Step | 详情 |\n|---|---|---|---|---|---|---|---|\n"
    _write_session(root, "sess-pr", title="pauseresume", created="2026-07-13 00:00:00",
                   traces=traces, raw_log=raw, audit=audit)
    s = DashboardAggregator(root).build().sessions[0]
    # 只出一条 run（去重），状态=最终 ok，原因=规划完成，耗时=两段之和
    assert len(s.runs) == 1
    assert s.runs[0].status == "ok"
    assert s.runs[0].finish_reason == "规划完成（待审批）"
    assert s.runs[0].duration_ms == 1300  # 500 + 800


def test_system_prompt_from_logs(tmp_path: Path):
    """P1-C：从 logs.md 首个 llm 日志的 messages[system] 纯读；还原 markdown \\| 转义。"""
    root = tmp_path
    (root / "metrics.md").write_text("# Metrics Summary\n\n> Last updated: x\n", encoding="utf-8")
    traces = _TRACES_HDR
    traces += _llm_row("00:00:01", 0, "aaaaaaaa", "m", 100, 10, 0.001, 90, 90.0, 0, 500)
    traces += _run_row("00:00:02", "aaaaaaaa", 500)
    raw = (_turn(0, "user", "q", run_id="aaaaaaaa", step=None)
           + _turn(1, "assistant", "ans", run_id="aaaaaaaa", step=0))
    # system 内容故意含 | ，logs 里会被转义成 \| ，聚合器需还原后才能解析
    logs = _logs_with_system("aaaaaaaa", 0, "你是助手 | 遵守规则")
    _write_session(root, "sess-sp", title="sp", created="2026-07-13 00:00:00",
                   traces=traces, raw_log=raw, audit=_audit("aaaaaaaa"), logs=logs)
    s = DashboardAggregator(root).build().sessions[0]
    assert s.system_prompt == "你是助手 | 遵守规则"


def test_system_prompt_from_logs_newfmt(tmp_path: Path):
    """回归（87aea2c 后）：extra JSON 以 {"log_id": 开头时仍能提取 system_prompt。"""
    root = tmp_path
    (root / "metrics.md").write_text("# Metrics Summary\n\n> Last updated: x\n", encoding="utf-8")
    traces = _TRACES_HDR
    traces += _llm_row("00:00:01", 0, "aaaaaaaa", "m", 100, 10, 0.001, 90, 90.0, 0, 500)
    traces += _run_row("00:00:02", "aaaaaaaa", 500)
    raw = (_turn(0, "user", "q", run_id="aaaaaaaa", step=None)
           + _turn(1, "assistant", "ans", run_id="aaaaaaaa", step=0))
    logs = _logs_with_system_newfmt("aaaaaaaa", 0, "你是助手 | 遵守规则")
    _write_session(root, "sess-sp2", title="sp2", created="2026-07-13 00:00:00",
                   traces=traces, raw_log=raw, audit=_audit("aaaaaaaa"), logs=logs)
    s = DashboardAggregator(root).build().sessions[0]
    assert s.system_prompt == "你是助手 | 遵守规则"


def test_system_prompt_absent_logs(tmp_path: Path):
    """logs.md 缺失 → system_prompt 降级为空串，不崩溃。"""
    root = tmp_path
    (root / "metrics.md").write_text("# Metrics Summary\n\n> Last updated: x\n", encoding="utf-8")
    traces = _TRACES_HDR + _llm_row("00:00:01", 0, "aaaaaaaa", "m", 100, 10, 0.001, 90, 90.0, 0, 500) + _run_row("00:00:02", "aaaaaaaa", 500)
    raw = _turn(0, "user", "q", run_id="aaaaaaaa", step=None) + _turn(1, "assistant", "ans", run_id="aaaaaaaa", step=0)
    _write_session(root, "sess-nl", title="nl", created="2026-07-13 00:00:00",
                   traces=traces, raw_log=raw, audit=_audit("aaaaaaaa"))  # 不写 logs.md
    s = DashboardAggregator(root).build().sessions[0]
    assert s.system_prompt == ""


def test_net_cost_priced_model(tmp_path: Path):
    """① 每轮净费用（应用层唯一计费函数 cost_of_call，正向三项式）：qwen-plus in/out/cache。

    公式（用户口径，正向三项相加）：
      net = 命中token×缓存价 + 未命中token×输入全价 + 输出token×输出价
    字段口径（CallCost）：input_cost_usd + output_cost_usd == net；full = net + saved。
    qwen-plus 系统价（CNY/1k）：input=0.0008, output=0.002, cache_read=0.00008。
    in=1000 out=1000 cached=500：
      input_cost_cny = 500/1k×0.00008 + 500/1k×0.0008 = 0.00004 + 0.0004 = 0.00044（净输入侧）
      output_cost_cny = 1000/1k×0.002 = 0.002
      net_cny = 0.00244；full_cny = 0.0028；saved_cny = 0.00036
    期望 USD = CNY ÷ EXCHANGE_RATE_USD（与 cost_of_call 同一归一口径）。
    """
    root = tmp_path
    (root / "metrics.md").write_text("# Metrics Summary\n\n> Last updated: x\n", encoding="utf-8")
    traces = _TRACES_HDR
    # cost_usd(0.0016) 是 SDK 全价，看板已不再据此显示；净费用由 cost_of_call 精算
    traces += _llm_row("00:00:01", 0, "aaaaaaaa", "qwen-plus", 1000, 1000, 0.0016, 500, 50.0, 0, 500)
    traces += _run_row("00:00:02", "aaaaaaaa", 500)
    raw = (_turn(0, "user", "q", run_id="aaaaaaaa", step=None)
           + _turn(1, "assistant", "ans", run_id="aaaaaaaa", step=0))
    _write_session(root, "sess-pc", title="priced", created="2026-07-13 00:00:00",
                   traces=traces, raw_log=raw, audit=_audit("aaaaaaaa"))
    s = DashboardAggregator(root).build().sessions[0]
    llm = {t.turn: t for t in s.turns}[1].llm
    assert llm is not None
    input_usd = round(cny_to_usd(0.00044), 8)   # 净输入侧（命中价+全价两段，CNY→USD）
    output_usd = round(cny_to_usd(0.002), 8)
    net_usd = round(cny_to_usd(0.00244), 8)
    full_usd = round(cny_to_usd(0.0028), 8)
    saved_usd = round(full_usd - net_usd, 8)
    assert abs(llm.input_cost_usd - input_usd) < 1e-9
    assert abs(llm.output_cost_usd - output_usd) < 1e-9
    assert abs(llm.cache_saved_usd - saved_usd) < 1e-9
    assert abs(llm.net_cost_usd - net_usd) < 1e-9
    # 输入+输出 = 净费用（正向三项式口径）；全价 = 净 + 节省
    assert abs((llm.input_cost_usd + llm.output_cost_usd) - net_usd) < 1e-9
    assert abs((llm.net_cost_usd + llm.cache_saved_usd) - full_usd) < 1e-9
    # 会话净费用 = Σ 轮次 net（单一口径）
    assert abs(s.cost - net_usd) < 1e-9


def test_empty_dir(tmp_path: Path):
    snap = DashboardAggregator(tmp_path).build()
    assert snap.sessions == []
    assert snap.global_.agent_id == ""
