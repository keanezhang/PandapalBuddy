/**
 * src/pages/DashboardPage.tsx — Agent 可观测性小系统（五视图）
 *
 * 概览 Overview / 成本 Cost / 性能 Performance / 会话·链路 Sessions（融合）/ 健康·降级 Health。
 * 数据源：后端 DASHBOARD_DATA → dashboardStore.snapshot（唯一真相，无 mock 回退）。
 * 前四视图的派生（CallFact 事实表 / 分位 / 模型拆分 / 慢调用 / waterfall / 上下文快照）
 * 均在前端从 snapshot.sessions[].turns[].llm flatten —— P0 不动数据层。
 *
 * 健康视图是唯一**不走 CallFact**的视图：降级事件非会话级（多数没有 session_id），
 * 直读 snapshot.degradations，且为累计口径、不受顶栏日期筛选影响（见 HealthView 注释）。
 * 设计/口径：docs/prd/dashboard/dashboard-小系统-PRD.md（v1.1）。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { DegradationStat, SessionData, Turn } from "../types/dashboard";
import { useDashboardStore } from "../store/dashboardStore";
import { useBackend } from "../providers/BackendProvider";
import { BudgetBar } from "../components/BudgetBar";
import "./dashboard/dashboard.css";
import {
  ALERT_P99_LATENCY_MS,
  CATEGORY_LABEL,
  PERF_MIN_SAMPLE_SIZE,
  SEVERITY_META,
  SLOW_CALL_TOP_N,
  colorForModel,
} from "./dashboard/constants";
import {
  buildContext,
  buildSpans,
  callFullCost,
  callNetCost,
  computeHealth,
  computeModels,
  computePerf,
  computeSlow,
  computeToolSlow,
  computeToolSuccessByModel,
  computeDegradations,
  computeToolUsage,
  dailySeries,
  flattenFacts,
  flattenToolCalls,
  groupRuns,
  sessionNetCost,
  shiftDate,
  topSpendSessions,
  type CallFact,
  type ToolCallFact,
  type ToolSuccessByModel,
  type ToolUsage,
} from "./dashboard/derive";

/* ── 格式化 ──────────────────────────────────────────────────── */
const fmt = (n: number) => n.toLocaleString("en-US");
const fmtCost = (c: number) => "$" + c.toFixed(4);
const fmtMs = (ms: number) => (ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : Math.round(ms) + "ms");

/** run 状态 → 展示元数据。三态清晰区分：ok(绿) / 暂停(琥珀，非失败) / 错误(红)。
 *  cancelled = 暂停（等待人工/用户/审批），是正常中间态，绝不用红色误导成失败。 */
function runStatusMeta(status: string): { pill: string; icon: string; label: string } {
  if (status === "ok") return { pill: "ok", icon: "✓", label: "ok" };
  if (status === "cancelled") return { pill: "warn", icon: "⏸", label: "暂停" };
  return { pill: "danger", icon: "✕", label: status || "error" };
}
const dateOf = (s: SessionData) => s.created_at.slice(0, 10);
function fmtLocalTime(iso: string): string {
  try {
    const d = new Date(iso);
    const p = (n: number) => String(n).padStart(2, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  } catch {
    return iso;
  }
}
const ACCENT = "var(--accent-soft)";
const ROLE_META: Record<Turn["role"], { label: string; color: string }> = {
  user: { label: "用户", color: "var(--accent-soft)" },
  assistant: { label: "助手", color: "var(--success)" },
  tool: { label: "工具", color: "var(--info)" },
};
type TabId = "overview" | "cost" | "perf" | "sessions" | "health";

/* ═══════════════ 页面容器 ═══════════════ */
export function DashboardPage() {
  const { requestDashboard } = useBackend();
  const snapshot = useDashboardStore((s) => s.snapshot);
  const loading = useDashboardStore((s) => s.loading);
  useEffect(() => {
    requestDashboard();
  }, [requestDashboard]);

  // 过滤掉「刚创建、还没说话」的空会话（无 LLM 调用 / 无轮次 / 无消息）——不进 Dashboard 统计与列表
  const allSessions = useMemo(
    () => (snapshot?.sessions ?? []).filter((s) => s.llm_calls > 0 || s.turns.length > 0 || s.message_count > 0),
    [snapshot],
  );

  // 日期范围
  const dateBounds = useMemo(() => {
    const ds = allSessions.map(dateOf).sort();
    return { min: ds[0], max: ds[ds.length - 1] };
  }, [allSessions]);
  const [startRaw, setStart] = useState<string | undefined>();
  const [endRaw, setEnd] = useState<string | undefined>();
  const start = startRaw ?? dateBounds.min;
  const end = endRaw ?? dateBounds.max;
  const filtered = useMemo(
    () => allSessions.filter((s) => { const d = dateOf(s); return start != null && end != null && d >= start && d <= end; }),
    [allSessions, start, end],
  );

  // 派生（全部基于筛选后的会话）
  const facts = useMemo(() => flattenFacts(filtered), [filtered]);
  const health = useMemo(() => computeHealth(filtered, facts), [filtered, facts]);
  const models = useMemo(() => computeModels(facts), [facts]);
  const perf = useMemo(() => computePerf(facts), [facts]);
  const slow = useMemo(() => computeSlow(facts, SLOW_CALL_TOP_N), [facts]);
  const toolCalls = useMemo(() => flattenToolCalls(filtered), [filtered]);
  const toolSlow = useMemo(() => computeToolSlow(toolCalls, SLOW_CALL_TOP_N), [toolCalls]);
  const toolSuccess = useMemo(() => computeToolSuccessByModel(toolCalls), [toolCalls]);
  const toolUsage = useMemo(() => computeToolUsage(filtered), [filtered]);
  const daily = useMemo(() => dailySeries(filtered, start, end), [filtered, start, end]);
  const topSpend = useMemo(() => topSpendSessions(filtered), [filtered]);

  // 降级事件：**不受日期筛选**（counter 无时间维度，是自有数据以来的累计），
  // 故从 snapshot 直读而非 filtered，与上面这批派生刻意不同源。
  const degradations = useMemo(() => snapshot?.degradations ?? [], [snapshot]);
  const degSummary = useMemo(() => computeDegradations(degradations), [degradations]);

  // 视图 + 深链状态
  const [tab, setTab] = useState<TabId>("overview");
  // 会话卡片：多开 Set，各卡独立收放、可全部折叠（不再强制保留一个展开）
  const [openSessions, setOpenSessions] = useState<Set<string>>(new Set());
  const [openTurns, setOpenTurns] = useState<Set<string>>(new Set());
  const [flashId, setFlashId] = useState<string | null>(null);
  const pendingScroll = useRef<string | null>(null);
  const [scrollNonce, setScrollNonce] = useState(0);
  const didInitOpen = useRef(false);

  // 仅首次拿到数据时默认展开首个会话；此后用户可自由全部折叠，不再自动回弹
  useEffect(() => {
    if (!didInitOpen.current && filtered.length) {
      didInitOpen.current = true;
      setOpenSessions(new Set([filtered[0].id]));
    }
  }, [filtered]);

  // 深链滚动 + 高亮（由 scrollNonce 触发，与卡片开合状态解耦）
  useEffect(() => {
    if (tab !== "sessions" || !pendingScroll.current) return;
    const id = pendingScroll.current;
    pendingScroll.current = null;
    const t = setTimeout(() => {
      const el = document.getElementById("dash-sc-" + id);
      if (el) el.scrollIntoView({ block: "start", behavior: "smooth" });
    }, 60);
    return () => clearTimeout(t);
  }, [tab, scrollNonce]);

  const toggleSession = (id: string) =>
    setOpenSessions((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  const jumpToTurn = (sessionId: string, turn: number) => {
    setOpenSessions((prev) => new Set(prev).add(sessionId));
    setOpenTurns(new Set([`${sessionId}#${turn}`]));
    pendingScroll.current = sessionId;
    setScrollNonce((n) => n + 1);
    setFlashId(sessionId);
    setTab("sessions");
    setTimeout(() => setFlashId(null), 1400);
  };
  const jumpToSession = (sessionId: string) => jumpToTurn(sessionId, -1);
  const jumpToFail = () => {
    // 只定位真正失败（error）的会话；cancelled=暂停不算失败
    const f = filtered.find((s) => s.runs.some((r) => r.status === "error"));
    if (f) jumpToSession(f.id);
  };
  const toggleTurn = (key: string) =>
    setOpenTurns((prev) => { const n = new Set(prev); n.has(key) ? n.delete(key) : n.add(key); return n; });

  const modelLabel = models.length === 0 ? "—" : models.length === 1 ? models[0].model : `混合 · ${models.length} 个模型`;
  const p99Over = perf.p99 >= ALERT_P99_LATENCY_MS && perf.count > 0;

  // Tab 去 icon（AC-04）：只留文字标签 + 计数徽标，减少视觉噪音
  const TABS: [TabId, string, string][] = [
    ["overview", "概览", `${facts.length}`],
    ["cost", "成本", ""],
    ["perf", "性能", p99Over ? "P99↑" : ""],
    ["sessions", "会话 · 链路", `${filtered.length}`],
    // 徽标只在有 abort 类降级时告警（其余情况显示总数或留空），避免 log_only 噪音刷存在感
    ["health", "健康 · 降级", degSummary.abortCount > 0 ? `⚠ ${degSummary.abortCount}` : degSummary.total ? `${degSummary.total}` : ""],
  ];

  return (
    <div className="page-root">

      {/* 顶栏 */}
      <div style={{
        padding: "12px 24px 0", background: "linear-gradient(135deg, color-mix(in srgb, var(--accent) 6%, transparent), transparent 70%)",
        borderBottom: "1px solid var(--border-subtle)", flexShrink: 0,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <h1 className="page-title">
            <span style={{ width: 32, height: 32, display: "grid", placeItems: "center", borderRadius: 10, fontSize: "var(--text-lg)", background: "color-mix(in srgb, var(--accent) 15%, transparent)" }}>🚀</span>
            Dashboard
          </h1>
          <span className="dash-badge" style={{ background: "color-mix(in srgb, var(--accent) 12%, transparent)", color: ACCENT }}>{modelLabel}</span>
          <span style={{ flex: 1 }} />
          <input type="date" value={start ?? ""} min={dateBounds.min} max={end} onChange={(e) => setStart(e.target.value)} className="dash-date" />
          <span style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>→</span>
          <input type="date" value={end ?? ""} min={start} max={dateBounds.max} onChange={(e) => setEnd(e.target.value)} className="dash-date" />
          <QuickRange label="近 3 天" onClick={() => { setStart(shiftDate(dateBounds.max, -2)); setEnd(dateBounds.max); }} />
          <QuickRange label="全部" onClick={() => { setStart(dateBounds.min); setEnd(dateBounds.max); }} />
          <span className="dash-badge" onClick={() => requestDashboard()} title="刷新" style={{ cursor: "pointer", background: "color-mix(in srgb, var(--success) 12%, transparent)", color: "var(--success)" }}>
            {loading ? "⟳ 加载中" : "⟳ 刷新"}
          </span>
        </div>
        {/* Tab 导航 */}
        <div style={{ display: "flex", gap: 12, marginTop: 10 }}>
          {TABS.map(([id, label, cnt]) => (
            <div key={id} className={"dash-tab" + (tab === id ? " on" : "")} onClick={() => setTab(id)}>
              {label}
              {cnt && <span className="dash-cnt">{cnt}</span>}
            </div>
          ))}
        </div>
      </div>

      {/* 内容 */}
      <div style={{ flex: 1, overflowY: "auto" }}>
        <div style={{ maxWidth: 1120, margin: "0 auto", padding: "18px 24px 60px" }}>
          {snapshot == null ? (
            <Empty text={loading ? "正在加载运行数据…" : "暂无运行数据，点击右上角「刷新」重试"} />
          ) : tab === "health" ? (
            // 健康视图刻意排在「没有会话」守卫之前：降级事件是非会话级的累计数据，
            // 零会话时依然可能有降级（如启动期 backend_unavailable），不该被空态吞掉。
            <HealthView stats={degradations} summary={degSummary} />
          ) : filtered.length === 0 ? (
            <Empty text="该日期范围内没有会话" />
          ) : (
            <>
              {tab === "overview" && <OverviewView health={health} daily={daily} perf={perf} onGoPerf={() => setTab("perf")} onErrorClick={jumpToFail} />}
              {tab === "cost" && <CostView models={models} health={health} topSpend={topSpend} onSessionClick={jumpToSession} />}
              {tab === "perf" && <PerfView perf={perf} slow={slow} toolSlow={toolSlow} toolUsage={toolUsage} toolSuccess={toolSuccess} onSlowClick={jumpToTurn} />}
              {tab === "sessions" && (
                <SessionsView
                  sessions={filtered}
                  openSessions={openSessions}
                  toggleSession={toggleSession}
                  openTurns={openTurns}
                  toggleTurn={toggleTurn}
                  flashId={flashId}
                />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/* ═══════════════ 健康 · 降级 ═══════════════
 * 数据源：snapshot.degradations（后端统一降级通道 pandapal/degradation.py 的
 * counter，按 event_code/category/severity/source 分组）。
 *
 * 口径提示（必须显式告知，否则会被误读）：
 *   - counter 无时间维度 → 这里是**自有数据以来的累计**，不受顶栏日期筛选影响；
 *   - 它统计的是「在哪兜了底」，不是「报了多少错」——两者不同源，别与错误率混看。 */
function HealthView({ stats, summary }: {
  stats: DegradationStat[]; summary: ReturnType<typeof computeDegradations>;
}) {
  if (stats.length === 0) {
    return (
      <>
        <div className="dash-sect">健康 · 降级</div>
        <Panel title="🛡 降级事件">
          <div style={{ padding: "36px 0", textAlign: "center" }}>
            <div style={{ fontSize: "var(--text-md)", color: "var(--success)", fontWeight: 600, marginBottom: 6 }}>
              ✓ 未记录到任何降级
            </div>
            <Muted text="没有「本该失败却兜了底」的事件。这是期望状态，不是数据缺失。" />
          </div>
        </Panel>
      </>
    );
  }

  const topLabel = summary.top
    ? `${summary.top.event_code} × ${summary.top.count}`
    : "—";

  return (
    <>
      {summary.abortCount > 0 && (
        <div className="dash-alert" style={{
          background: "linear-gradient(90deg,rgba(239,68,68,0.12),transparent 70%)",
          borderColor: "color-mix(in srgb, var(--danger) 28%, transparent)",
        }}>
          <span style={{ fontSize: "var(--text-xl)" }}>⛔</span>
          <div>
            <div style={{ fontSize: "var(--text-base)", fontWeight: 600, color: "var(--text-primary)" }}>
              {summary.abortCount} 次「直接中止」类降级
            </div>
            <div style={{ fontSize: "var(--text-sm)", color: "var(--text-tertiary)", marginTop: 2 }}>
              决策 / ID / 金额类字段缺失时拒绝放行——这是设计如此（fail-fast），但频繁触发说明上游在丢数据
            </div>
          </div>
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 14 }}>
        <Kpi label="🛡 累计降级" value={fmt(summary.total)} sub={`${summary.distinctCodes} 种 event_code`} color={ACCENT} />
        <Kpi label="⛔ 直接中止" value={fmt(summary.abortCount)} sub="fail-fast，拒绝放行" color={summary.abortCount > 0 ? "var(--danger)" : "var(--text-muted)"} />
        <Kpi label="🎯 高风险类" value={fmt(summary.highStakesCount)} sub="决策 / ID / 金额" color={summary.highStakesCount > 0 ? "var(--warning)" : "var(--text-muted)"} />
        <Kpi label="🔺 最高频" value={summary.top ? fmt(summary.top.count) : "—"} sub={summary.top?.event_code ?? "无"} />
      </div>

      <Panel title={<>📦 按字段类别 <span className="dash-subtle">决策 / ID / 金额三类＝「本该失败却兜了底」，治理优先级最高</span></>} style={{ marginBottom: 14 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {summary.byCategory.map((c) => {
            const pct = summary.total ? (c.count / summary.total) * 100 : 0;
            const color = c.highStakes ? "var(--warning)" : "var(--text-tertiary)";
            return (
              <div key={c.category} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <span style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", minWidth: 96 }}>
                  {CATEGORY_LABEL[c.category] ?? c.category}
                </span>
                <div style={{ flex: 1, height: 8, borderRadius: 4, background: "var(--bg-elevated)", overflow: "hidden" }}>
                  <div style={{ width: `${pct}%`, height: "100%", borderRadius: 4, background: color }} />
                </div>
                <span className="mono" style={{ fontSize: "var(--text-sm)", fontWeight: 700, color, minWidth: 40, textAlign: "right" }}>{fmt(c.count)}</span>
                <span className="mono" style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", minWidth: 42, textAlign: "right" }}>{pct.toFixed(0)}%</span>
              </div>
            );
          })}
        </div>
      </Panel>

      <Panel title={<>📋 降级明细 <span className="dash-subtle">按严重度 → 次数排序；累计口径，不受日期筛选影响</span></>}>
        <div style={{ display: "flex", flexDirection: "column" }}>
          <div className="dash-deg-row" style={{ color: "var(--text-muted)", fontSize: "var(--text-2xs)", textTransform: "uppercase", letterSpacing: "0.4px", borderBottom: "1px solid var(--border-subtle)" }}>
            <span>严重度</span><span>event_code</span><span>类别</span><span>触发点</span><span style={{ textAlign: "right" }}>次数</span>
          </div>
          {stats.map((d) => {
            const sev = SEVERITY_META[d.severity] ?? { label: d.severity || "未知", color: "var(--text-tertiary)" };
            return (
              <div key={`${d.event_code}|${d.source}|${d.severity}`} className="dash-deg-row">
                <span>
                  <span className="dash-badge" style={{ height: 20, padding: "0 8px", fontSize: "var(--text-2xs)", background: "var(--bg-track)", color: sev.color }}>
                    {sev.label}
                  </span>
                </span>
                <span className="mono" style={{ fontSize: "var(--text-sm)", color: "var(--text-primary)", fontWeight: 550 }}>{d.event_code || "（无 event_code）"}</span>
                <span style={{ fontSize: "var(--text-sm)", color: "var(--text-tertiary)" }}>{CATEGORY_LABEL[d.category] ?? d.category}</span>
                <span className="mono" style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={d.source}>{d.source}</span>
                <span className="mono" style={{ fontSize: "var(--text-base)", fontWeight: 700, color: sev.color, textAlign: "right" }}>{fmt(d.count)}</span>
              </div>
            );
          })}
        </div>
      </Panel>
    </>
  );
}

/* ═══════════════ 概览 ═══════════════ */
function OverviewView({ health, daily, perf, onGoPerf, onErrorClick }: {
  health: ReturnType<typeof computeHealth>; daily: ReturnType<typeof dailySeries>;
  perf: ReturnType<typeof computePerf>; onGoPerf: () => void; onErrorClick: () => void;
}) {
  return (
    <>
      <BudgetBar />
      {/* KPI 4 个宏观总量（AC-04）：净费用 / 总 tokens / LLM 调用量 / 成功率。删失败率 KPI；
          「定位失败会话」深链移到成功率卡（无失败时禁用），保留能力仅换承载（决策 1）。 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 14 }}>
        <Kpi label="💰 累计净费用" value={fmtCost(health.netCost)} sub={health.cacheSavedUsd > 0 ? `缓存已节省 ${fmtCost(health.cacheSavedUsd)}` : "实际净费用"} color="var(--warning)" />
        <Kpi label="🔢 总 Tokens" value={fmt(health.totalIn + health.totalOut)} sub={`${fmt(health.totalIn)} 入 / ${fmt(health.totalOut)} 出`} />
        <Kpi label="📊 LLM 调用量" value={fmt(health.llmCalls)} sub={`${health.runTotal} 次 run`} color={ACCENT} />
        <Kpi label="✅ 成功率" value={health.runTotal ? Math.round((health.runOk / health.runTotal) * 100) + "%" : "—"} sub={health.runFail > 0 ? "点击定位失败会话 →" : `${health.runOk} 成功 · ${health.runFail} 失败`} color="var(--success)" clickable={health.runFail > 0} onClick={health.runFail > 0 ? onErrorClick : undefined} />
      </div>
      <CacheMacro health={health} />
      {/* 总体 P50 stat（AC-04）：一眼看中位延迟，细分分位/直方图在性能页 */}
      <Panel title="⏱ 总体延迟 P50" style={{ marginBottom: 14 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
          <span className="mono" style={{ fontSize: "var(--text-3xl)", fontWeight: 700, color: "var(--success)" }}>{fmtMs(perf.p50)}</span>
          <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>中位 LLM 调用延迟{perf.reliable ? "" : `（样本 ${perf.count} 偏少，仅供参考）`}</span>
          <span style={{ marginLeft: "auto" }}><span className="dash-alert-act" onClick={onGoPerf}>查看性能分布 →</span></span>
        </div>
      </Panel>
      {/* 双曲线趋势（AC-04）：费用-日期 + token-日期，曲线替代直方图；每日费用图从成本页迁入此处 */}
      <Panel title="📈 净费用 趋势" style={{ marginBottom: 14 }}>
        <LineTrend data={daily} fmtVal={fmtCost}
          lines={[{ values: daily.map((d) => d.cost), color: "var(--warning)", label: "净费用 / 天" }]} />
      </Panel>
      <Panel title="📈 Tokens日用量 趋势">
        <LineTrend data={daily} fmtVal={fmt}
          lines={[
            { values: daily.map((d) => d.input), color: ACCENT, label: "输入 Token" },
            { values: daily.map((d) => d.output), color: "var(--success)", label: "输出 Token" },
          ]} />
      </Panel>
    </>
  );
}

/* 缓存命中宏观统计：整体命中率 + 命中 token + 相对全价省下的钱 */
function CacheMacro({ health }: { health: ReturnType<typeof computeHealth> }) {
  const active = health.cachedTokens > 0;
  return (
    <div style={{ marginBottom: 14 }}>
      <Panel title={<>🎯 缓存命中 · 宏观节省 <span className="dash-subtle">因命中 prefix cache 省下的 token 与费用（相对「全价」基线）</span></>}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12, alignItems: "center" }}>
          <CacheStat label="整体命中率" value={health.hitRate.toFixed(1) + "%"} sub={`Σ命中 / Σ输入 · 按 token 加权`} color={active ? ACCENT : "var(--text-muted)"} big />
          <CacheStat label="命中 Token" value={fmt(health.cachedTokens)} sub={`共 ${fmt(health.totalIn)} 输入 token`} color={active ? "var(--success)" : "var(--text-muted)"} />
          <CacheStat label="命中节省费用" value={fmtCost(health.cacheSavedUsd)} sub={active ? `全价 ${fmtCost(health.totalCost)} → 缓存后约 ${fmtCost(health.netCost)}` : "无命中 / 未配置缓存价"} color={active ? "var(--warning)" : "var(--text-muted)"} />
        </div>
        {!active && (
          <div style={{ marginTop: 10, fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
            💡 暂无缓存命中数据（或该模型未在价格表配置 <span className="mono">cache_read_price</span>，节省不臆造，记 0）。
          </div>
        )}
      </Panel>
    </div>
  );
}

function CacheStat({ label, value, sub, color, big }: { label: string; value: string; sub: string; color?: string; big?: boolean }) {
  return (
    <div>
      <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.5px" }}>{label}</div>
      <div className="mono" style={{ fontSize: big ? 30 : 24, fontWeight: 700, margin: "3px 0 3px", color: color || "var(--text-primary)" }}>{value}</div>
      <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-tertiary)" }}>{sub}</div>
    </div>
  );
}

/* ═══════════════ 成本 ═══════════════ */
function CostView({ models, health, topSpend, onSessionClick }: {
  models: ReturnType<typeof computeModels>; health: ReturnType<typeof computeHealth>;
  topSpend: ReturnType<typeof topSpendSessions>; onSessionClick: (id: string) => void;
}) {
  // 费用全部引用 derive 派生层算好的值（netCost / m.netCost / m.netShare），本视图不再自算任何费用
  const { totalCost, totalIn, totalOut, netCost, cacheSavedUsd, llmCalls: callCount } = health;
  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12, marginBottom: 14 }}>
        <Kpi label="总费用" value={fmtCost(netCost)} sub={cacheSavedUsd > 0 ? `实际净费用 · 全价 ${fmtCost(totalCost)}` : `${fmt(totalIn + totalOut)} tokens`} color="var(--warning)" />
        <Kpi label="模型数" value={String(models.length)} sub={models.map((m) => m.model.split("-")[0]).join(" · ") || "—"} />
        <Kpi label="单次均价" value={callCount ? fmtCost(netCost / callCount) : "—"} sub="每次 LLM 调用（净）" />
      </div>
      {/* 费用构成 + Token 构成 并列（AC：入/出 token 从 KPI 行下移至此，与费用环形图并排） */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 14 }}>
        <CostComposition health={health} />
        <TokenComposition health={health} />
      </div>
      <ModelBreakdown models={models} netCost={netCost} />
      {/* 每日费用图已迁移至概览（去与概览重叠，AC-05·2e）。此处仅保留消费 Top10 深链。 */}
      <Panel title={<>🔥 消费 Top 会话 <span className="dash-subtle">净费用降序前 10（不足按实际）</span></>}>
        {topSpend.slice(0, 10).map((s, i) => (
          <div key={s.session.id} className="dash-lrow" onClick={() => onSessionClick(s.session.id)}>
            <span className={"dash-rank" + (i === 0 ? " top" : "")}>{i + 1}</span>
            <div className="dash-flex-main">
              <div style={{ fontSize: "var(--text-base)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.session.title || "未命名会话"}</div>
              <div className="mono" style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", marginTop: 2 }}>{s.session.model} · {s.session.llm_calls} 次</div>
            </div>
            <span className="mono" style={{ fontWeight: 700, color: "var(--warning)" }}>{fmtCost(s.cost)}</span>
            <span className="dash-go">›</span>
          </div>
        ))}
      </Panel>
    </>
  );
}

/* 费用构成：输入费用 + 输出费用 = 实际净费用；净 + 命中节省 = 全价基线（正向三项式口径）*/
function CostComposition({ health }: { health: ReturnType<typeof computeHealth> }) {

  // 全部引用 derive 派生层算好的值，本组件不做任何费用计算
  // 新口径（cost_of_call 正向三项式）：inCost+outCost == net；net+saved == full（全价基线）
  const { inputCost: inCost, outputCost: outCost, totalCost, netCost: net } = health;
  const full = totalCost; // 全价基线 = Σ(净 + 命中节省)（derive.callFullCost，恒自洽）

  // 环形图口径：仅输入/输出两项，占比对净费用（inCost + outCost = net）
  const ioDenom = inCost + outCost || 1;
  const inNetPct = (inCost / ioDenom) * 100, outNetPct = (outCost / ioDenom) * 100;
  return (
    <Panel title={<>💰 费用构成 = <span className="dash-subtle">输入 + 输出</span></>}>
      {/* 环形图：输入费用 vs 输出费用 占比（仅两项，中心=实际净费用） */}
      <div style={{ display: "flex", gap: 28, alignItems: "center", flexWrap: "wrap" }}>
        <Donut segments={[{ v: inCost, c: ACCENT }, { v: outCost, c: "var(--success)" }]} center={fmtCost(net)} label="总净费用" />
        <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 220 }}>
          <DonutLegend color={ACCENT} label="输入 token 费用" value={fmtCost(inCost)} pct={inNetPct} />
          <DonutLegend color="var(--success)" label="输出 token 费用" value={fmtCost(outCost)} pct={outNetPct} />
        </div>
      </div>
    </Panel>
  );
}

/* Token 构成：输入 Tokens vs 输出 Tokens 占比（环形图，与费用构成并列）。 */
function TokenComposition({ health }: { health: ReturnType<typeof computeHealth> }) {
  const { totalIn, totalOut } = health;
  const total = totalIn + totalOut;
  const denom = total || 1;
  const inPct = (totalIn / denom) * 100, outPct = (totalOut / denom) * 100;
  const fmtK = (n: number) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));
  return (
    <Panel title={<>🔢 Tokens 构成 = <span className="dash-subtle">输入 + 输出</span></>}>
      {/* 环形图：输入 vs 输出 token 占比（中心=总 tokens） */}
      <div style={{ display: "flex", gap: 28, alignItems: "center", flexWrap: "wrap" }}>
        <Donut segments={[{ v: totalIn, c: ACCENT }, { v: totalOut, c: "var(--success)" }]} center={fmtK(total)} label="总 Tokens" />
        <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 220 }}>
          <DonutLegend color={ACCENT} label="输入 Tokens" value={fmt(totalIn)} pct={inPct} />
          <DonutLegend color="var(--success)" label="输出 Tokens" value={fmt(totalOut)} pct={outPct} />
        </div>
      </div>
    </Panel>
  );
}

/* 按模型统一明细：一张表把「实际净费用 + 占比 + tokens + 缓存节省」聚齐，
 * 取代此前重复三次的（费用占比 donut / 每模型明细 / 缓存命中节省·按模型）三块面板。 */
function ModelBreakdown({ models, netCost }: { models: ReturnType<typeof computeModels>; netCost: number }) {
  const maxNet = Math.max(1e-9, ...models.map((m) => m.netCost));
  const totalSaved = models.reduce((a, m) => a + m.cacheSavedUsd, 0);
  const totalTokens = models.reduce((a, m) => a + m.tokens, 0);
  const fmtK = (n: number) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));
  return (
    <Panel title={<>📊 按模型分类</>} style={{ marginBottom: 14 }}>
      {models.length === 0 ? (
        <Muted text="暂无模型调用数据。" />
      ) : (
        <>
          {/* 双环形（AC-05 2a/2b）：费用占比环 + tokens 占比环 */}
          <div style={{ display: "flex", gap: 36, alignItems: "center", justifyContent: "center", flexWrap: "wrap", marginBottom: 16 }}>
            <Donut segments={models.map((m) => ({ v: m.netCost, c: colorForModel(m.model) }))} center={fmtCost(netCost)} label="实际净费用" />
            <Donut segments={models.map((m) => ({ v: m.tokens, c: colorForModel(m.model) }))} center={fmtK(totalTokens)} label="Tokens" />
          </div>
          <div>
            {/* 表头 */}
            <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "0 0 6px", fontSize: "var(--text-2xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.4px", borderBottom: "1px solid var(--border-subtle)" }}>
              <span style={{ width: 150, flexShrink: 0 }}>模型</span>
              <span style={{ flex: 1 }} />
              <span style={{ minWidth: 40, textAlign: "right" }}>占比</span>
              <span style={{ minWidth: 62, textAlign: "right" }}>净费用</span>
              <span style={{ minWidth: 70, textAlign: "right" }}>Tokens</span>
              <span style={{ minWidth: 56, textAlign: "right" }}>命中率</span>
              <span style={{ minWidth: 64, textAlign: "right" }}>缓存省</span>
            </div>
            {models.map((m) => (
              <div key={m.model} style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 0", fontSize: "var(--text-sm)", borderBottom: "1px solid rgba(127,127,127,0.06)" }}>
                <span style={{ width: 150, display: "flex", alignItems: "center", gap: 7, flexShrink: 0 }}>
                  <span style={{ width: 9, height: 9, borderRadius: 3, background: colorForModel(m.model), flexShrink: 0 }} />
                  <span style={{ fontFamily: "var(--font-mono)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{m.model}</span>
                </span>
                <span className="dash-bar-track">
                  <span style={{ display: "block", height: "100%", width: `${(m.netCost / maxNet) * 100}%`, background: colorForModel(m.model), borderRadius: 5 }} />
                </span>
                <span className="mono" style={{ color: "var(--text-tertiary)", minWidth: 40, textAlign: "right" }}>{(m.netShare * 100).toFixed(0)}%</span>
                <span className="mono" style={{ color: "var(--success)", fontWeight: 700, minWidth: 62, textAlign: "right" }} title={`全价 ${fmtCost(m.cost)}`}>{fmtCost(m.netCost)}</span>
                <span className="mono" style={{ color: "var(--text-tertiary)", minWidth: 70, textAlign: "right" }}>{fmt(m.tokens)}</span>
                <span className="mono" style={{ color: m.cachedTokens > 0 ? ACCENT : "var(--text-muted)", minWidth: 56, textAlign: "right" }} title={m.inputTokens > 0 ? `命中 ${fmt(m.cachedTokens)} / 输入 ${fmt(m.inputTokens)} tok` : "无输入 token"}>{m.inputTokens > 0 ? m.hitRate.toFixed(1) + "%" : "—"}</span>
                <span className="mono" style={{ color: m.cacheSavedUsd > 0 ? "var(--warning)" : "var(--text-muted)", minWidth: 64, textAlign: "right" }} title={m.cachedTokens > 0 ? `命中 ${fmt(m.cachedTokens)} tok` : "无命中"}>{m.cacheSavedUsd > 0 ? fmtCost(m.cacheSavedUsd) : "—"}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </Panel>
  );
}

/* ═══════════════ 性能 ═══════════════ */
function PerfView({ perf, slow, toolSlow, toolUsage, toolSuccess, onSlowClick }: {
  perf: ReturnType<typeof computePerf>; slow: CallFact[]; toolSlow: ToolCallFact[]; toolUsage: ToolUsage[]; toolSuccess: ToolSuccessByModel[]; onSlowClick: (sid: string, turn: number) => void;
}) {
  const warn = !perf.reliable ? <span className="dash-pill warn">⚠ 样本 {perf.count} &lt; {PERF_MIN_SAMPLE_SIZE}，仅供参考</span> : null;
  const maxBucket = Math.max(1, ...perf.buckets.map((b) => b.count));
  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12, marginBottom: 14 }}>
        <Panel title={<>P50 中位延迟 {warn}</>}><BigStat value={fmtMs(perf.p50)} color="var(--success)" /></Panel>
        <Panel title="P95 延迟"><BigStat value={fmtMs(perf.p95)} color="var(--warning)" /></Panel>
        <Panel title={<>P99 延迟 {perf.p99 >= ALERT_P99_LATENCY_MS && <span className="dash-pill danger">越阈</span>}</>}><BigStat value={fmtMs(perf.p99)} color="var(--danger)" /></Panel>
      </div>
      <Panel title="延迟分布直方图" style={{ marginBottom: 14 }}>
        <svg viewBox="0 0 920 180" width="100%" style={{ display: "block" }}>
          <line x1={0} y1={150} x2={920} y2={150} stroke="var(--border-subtle)" />
          {perf.buckets.map((b, i) => {
            const slot = 920 / perf.buckets.length, x = i * slot + slot / 2, h = (b.count / maxBucket) * 150;
            const over = b.lo >= ALERT_P99_LATENCY_MS;
            const lbl = b.hi === Infinity ? `${b.lo / 1000}s+` : `${b.lo / 1000}-${b.hi / 1000}`;
            return (
              <g key={i}>
                {b.count > 0 && <>
                  <rect x={x - slot * 0.36} y={150 - h} width={slot * 0.72} height={h} rx={4} fill={over ? "var(--danger)" : ACCENT} opacity={0.9} />
                  <text x={x} y={150 - h - 6} textAnchor="middle" fontSize={11} fill="var(--text-secondary)" fontFamily="var(--font-mono)">{b.count}</text>
                </>}
                <text x={x} y={172} textAnchor="middle" fontSize={9.5} fill="var(--text-muted)" fontFamily="var(--font-mono)">{lbl}</text>
              </g>
            );
          })}
        </svg>
      </Panel>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Panel title={`慢 LLM 调用 Top ${SLOW_CALL_TOP_N} · 点击深链`}>
          {slow.length === 0 ? <Muted text="无调用记录" /> : slow.map((f, i) => (
            <div key={f.sessionId + f.turn} className="dash-lrow" onClick={() => onSlowClick(f.sessionId, f.turn)}>
              <span className={"dash-rank" + (i === 0 ? " top" : "")}>{i + 1}</span>
              <div className="dash-flex-main">
                <div style={{ fontSize: "var(--text-base)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.title}</div>
                <div className="mono" style={{ fontSize: "var(--text-xs)", marginTop: 2 }}>
                  <span style={{ color: colorForModel(f.model) }}>{f.model}</span>
                  <span style={{ color: "var(--text-tertiary)" }}> · step {f.step}</span>
                </div>
              </div>
              <span className="mono" style={{ fontWeight: 700, color: f.durationMs >= ALERT_P99_LATENCY_MS ? "var(--danger)" : "var(--text-secondary)" }}>{fmtMs(f.durationMs)}</span>
              <span className="dash-go">›</span>
            </div>
          ))}
        </Panel>
        <Panel title={`慢工具调用 Top ${SLOW_CALL_TOP_N} · 点击深链`}>
          {toolSlow.length === 0 ? <Muted text="无工具调用记录（或旧数据无 per-call 耗时）" /> : toolSlow.map((f, i) => (
            <div key={f.sessionId + f.turn + f.name + i} className="dash-lrow" onClick={() => onSlowClick(f.sessionId, f.turn)}>
              <span className={"dash-rank" + (i === 0 ? " top" : "")}>{i + 1}</span>
              <div className="dash-flex-main">
                <div className="mono" style={{ fontSize: "var(--text-base)", color: "var(--info)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.name}</div>
                <div style={{ fontSize: "var(--text-xs)", marginTop: 2, color: "var(--text-tertiary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.title}</div>
              </div>
              <span className="mono" style={{ fontWeight: 700, color: "var(--text-secondary)" }}>{fmtMs(f.durationMs)}</span>
              <span className="dash-go">›</span>
            </div>
          ))}
        </Panel>
      </div>
      <ToolSuccessByModelPanel rows={toolSuccess} />
      <ToolUsageRank usage={toolUsage} />
    </>
  );
}

/* 工具成功率·按模型（★ 新功能 3a · D6）：该模型发起的工具调用成功率。
 * 口径：分母 = 已 trace 到 status 的工具调用（与慢工具榜/工具使用榜同源）；
 * total==0 显示「—」不显示 0%；旧快照无 status 的调用不计入（标注说明）。 */
function ToolSuccessByModelPanel({ rows }: { rows: ToolSuccessByModel[] }) {
  const maxTotal = Math.max(1, ...rows.map((r) => r.total));
  return (
    <Panel
      title={
        <>
          🎯 工具成功率 · 按模型
          <span className="dash-subtle">
            各模型发起的工具调用成功率
          </span>
        </>
      }
      style={{ marginTop: 12 }}
    >
      {rows.length === 0 ? (
        <Muted text="暂无带状态的工具调用。" />
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "0 12px 6px", fontSize: "var(--text-2xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.4px", borderBottom: "1px solid var(--border-subtle)" }}>
            <span style={{ width: 170, flexShrink: 0 }}>模型</span>
            <span style={{ flex: 1 }} />
            <span style={{ minWidth: 90, textAlign: "right" }}>成功 / 总数</span>
            <span style={{ minWidth: 64, textAlign: "right" }}>成功率</span>
          </div>
          {rows.map((r) => {
            // 成功率配色：≥95% 绿；≥80% 琥珀；<80% 红（诊断信号，非严格阈值）
            const rate = r.successRate;
            const col = rate == null ? "var(--text-muted)" : rate >= 95 ? "var(--success)" : rate >= 80 ? "var(--warning)" : "var(--danger)";
            return (
              <div key={r.model} className="dash-lrow" style={{ cursor: "default" }}>
                <span style={{ width: 170, flexShrink: 0, display: "flex", alignItems: "center", gap: 7 }}>
                  <span style={{ width: 9, height: 9, borderRadius: 3, background: colorForModel(r.model), flexShrink: 0 }} />
                  <span className="mono" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.model}</span>
                </span>
                <span className="dash-bar-track">
                  <span style={{ display: "block", height: "100%", width: `${rate == null ? 0 : (r.total / maxTotal) * 100}%`, background: col, opacity: 0.85, borderRadius: 5 }} />
                </span>
                <span className="mono" style={{ minWidth: 90, textAlign: "right", color: "var(--text-tertiary)" }}>{r.ok} / {r.total}</span>
                <span className="mono" style={{ minWidth: 64, textAlign: "right", fontWeight: 700, color: col }}>{rate == null ? "—" : rate.toFixed(1) + "%"}</span>
              </div>
            );
          })}
        </>
      )}
    </Panel>
  );
}

/* 工具使用排行：跨会话累积「调用次数 + 累积时长」，可按次数 / 时长切换排序。
 * 数据同源于慢工具榜（tool_spans / session.tools），口径一致（已 trace 的调用）。 */
function ToolUsageRank({ usage }: { usage: ToolUsage[] }) {
  const [sortBy, setSortBy] = useState<"count" | "time">("count");
  const sorted = useMemo(
    () => [...usage].sort((a, b) => (sortBy === "count" ? b.count - a.count : b.totalMs - a.totalMs)),
    [usage, sortBy],
  );
  const max = Math.max(1, ...sorted.map((u) => (sortBy === "count" ? u.count : u.totalMs)));
  const totalCount = usage.reduce((a, u) => a + u.count, 0);
  const totalMs = usage.reduce((a, u) => a + u.totalMs, 0);
  return (
    <Panel
      title={
        <>
          🔧 工具使用排行
          <span className="dash-subtle">累积 {fmt(totalCount)} 次 · {fmtMs(totalMs)}（按{sortBy === "count" ? "次数" : "时长"}排序）</span>
          <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
            <button type="button" className={"dash-seg" + (sortBy === "count" ? " on" : "")} onClick={() => setSortBy("count")}>次数</button>
            <button type="button" className={"dash-seg" + (sortBy === "time" ? " on" : "")} onClick={() => setSortBy("time")}>时长</button>
          </span>
        </>
      }
      style={{ marginTop: 12 }}
    >
      {sorted.length === 0 ? (
        <Muted text="暂无工具调用记录（或旧数据无 per-call 耗时）。" />
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "0 12px 6px", fontSize: "var(--text-2xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.4px", borderBottom: "1px solid var(--border-subtle)" }}>
            <span style={{ width: 20, flexShrink: 0 }} />
            <span style={{ width: 160, flexShrink: 0 }}>工具</span>
            <span style={{ flex: 1 }} />
            <span style={{ minWidth: 64, textAlign: "right" }}>累积次数</span>
            <span style={{ minWidth: 72, textAlign: "right" }}>累积时长</span>
            <span style={{ minWidth: 72, textAlign: "right" }}>平均单次</span>
          </div>
          {sorted.map((u, i) => (
            <div key={u.name} className="dash-lrow" style={{ cursor: "default" }}>
              <span className={"dash-rank" + (i === 0 ? " top" : "")}>{i + 1}</span>
              <span className="mono" style={{ width: 160, flexShrink: 0, color: "var(--info)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{u.name}</span>
              <span className="dash-bar-track">
                <span style={{ display: "block", height: "100%", width: `${((sortBy === "count" ? u.count : u.totalMs) / max) * 100}%`, background: "var(--info)", opacity: 0.85, borderRadius: 5 }} />
              </span>
              <span className="mono" style={{ minWidth: 64, textAlign: "right", fontWeight: sortBy === "count" ? 700 : 400, color: sortBy === "count" ? "var(--text-primary)" : "var(--text-secondary)" }}>{fmt(u.count)} 次</span>
              <span className="mono" style={{ minWidth: 72, textAlign: "right", fontWeight: sortBy === "time" ? 700 : 400, color: sortBy === "time" ? "var(--text-primary)" : "var(--text-secondary)" }}>{fmtMs(u.totalMs)}</span>
              <span className="mono" style={{ minWidth: 72, textAlign: "right", color: "var(--text-tertiary)" }}>{fmtMs(u.avgMs)}</span>
            </div>
          ))}
        </>
      )}
    </Panel>
  );
}

/* ═══════════════ 会话 · 链路（融合）═══════════════ */
function SessionsView({ sessions, openSessions, toggleSession, openTurns, toggleTurn, flashId }: {
  sessions: SessionData[]; openSessions: Set<string>; toggleSession: (id: string) => void;
  openTurns: Set<string>; toggleTurn: (k: string) => void; flashId: string | null;
}) {
  return (
    <>
      <div className="dash-sect">会话明细 · {sessions.length}（run 链路 + 逐轮 + 上下文 · 融合）</div>
      {sessions.map((s) => (
        <SessionCard
          key={s.id} session={s}
          open={openSessions.has(s.id)}
          onToggle={() => toggleSession(s.id)}
          openTurns={openTurns} toggleTurn={toggleTurn}
          flash={flashId === s.id}
        />
      ))}
    </>
  );
}

function SessionCard({ session, open, onToggle, openTurns, toggleTurn, flash }: {
  session: SessionData; open: boolean; onToggle: () => void;
  openTurns: Set<string>; toggleTurn: (k: string) => void; flash: boolean;
}) {
  const cost = sessionNetCost(session), tok = session.input_tokens + session.output_tokens;
  const runs = useMemo(() => groupRuns(session), [session]);
  return (
    <div id={"dash-sc-" + session.id} className={"dash-scard" + (open ? " open" : "") + (flash ? " flash" : "")}>
      <div className="dash-shead" onClick={onToggle}>
        <span className="dash-arw" style={{ transform: open ? "rotate(90deg)" : "none" }}>▶</span>
        <div className="dash-flex-main">
          <div style={{ fontSize: "var(--text-md)", fontWeight: 600, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{session.title || "未命名会话"}</div>
          <div className="mono" style={{ display: "flex", gap: 10, marginTop: 3, fontSize: "var(--text-xs)", color: "var(--text-tertiary)", flexWrap: "wrap" }}>
            <span style={{ color: colorForModel(session.model) }}>{session.model}</span>
            <span>{session.created_at.slice(5, 16)}</span>
            <span>{session.llm_calls} 次 LLM</span>
            {(() => { const st = runStatusMeta(session.runs[0]?.status ?? "ok");
              const c = st.pill === "ok" ? "var(--success)" : st.pill === "warn" ? "var(--warning)" : "var(--danger)";
              return <span style={{ color: c }}>{st.icon} {st.pill === "ok" ? "完成" : st.label}</span>; })()}
          </div>
        </div>
        <div style={{ display: "flex", gap: 18, flexShrink: 0, textAlign: "right" }}>
          <Stat val={fmt(tok)} lbl="Tokens" color={ACCENT} />
          <Stat val={fmtCost(cost)} lbl="净费用" color="var(--warning)" />
        </div>
      </div>
      {open && (
        <div style={{ padding: "0 12px 12px" }}>
          <details className="dash-sysfold">
            <summary>⚙️ 系统提示词 system prompt · 本会话所有调用共享（逐轮不重复展示）{session.system_prompt ? ` · ${session.system_prompt.length} 字` : ""}</summary>
            <div className="dash-sysbody">{session.system_prompt || "系统提示词未记录（该会话 logs.md 缺失或为旧格式）。"}</div>
          </details>

          {/* 按 run 分组：每个 run = 链路条 + 该 run 的 waterfall + 该 run 的逐轮明细（整体一块）*/}
          {runs.map((run, ri) => (
            <div key={ri} style={{ marginBottom: 18, paddingTop: ri ? 16 : 0, borderTop: ri ? "1px dashed var(--border-subtle)" : "none" }}>
              {(() => { const st = runStatusMeta(run.status); return (
              <div className="dash-runstrip">
                <span className={"dash-pill " + st.pill} title={run.finishReason}>{st.icon} {st.label}</span>
                <span className="mono" style={{ color: ACCENT, fontSize: "var(--text-sm)" }}>{run.runId}</span>
                <span className="mono" style={{ color: "var(--text-tertiary)", fontSize: "var(--text-xs)" }}>{run.steps} steps · {fmtMs(run.durationMs)}</span>
                {/* D1 range 对齐：run 级 net/tokens/命中率 = 对话后 footer 单 run 数字，供逐字段核对 */}
                <span className="mono" style={{ color: ACCENT, fontSize: "var(--text-xs)" }} title="本 run 输入/输出 token（与对话后 footer 单 run 数字对齐）">in {fmt(run.inTokens)} · out {fmt(run.outTokens)}</span>
                <span className="mono" style={{ color: "var(--warning)", fontSize: "var(--text-xs)" }} title="本 run 实际净费用 = Σ 各步 net_cost_usd（与对话后 footer 逐字段相等，D1）">净 {fmtCost(run.net)}</span>
                <span className={"dash-pill " + (run.cacheHit > 0 ? "acc" : "mut")} title="run 级命中率 = 原始 cached / 原始 In（唯一口径，与 footer 同公式，R2）">🎯 run 命中率 {run.cacheHit.toFixed(1)}%</span>
                <span style={{ marginLeft: "auto", fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
                  {runs.length > 1 ? `run ${ri + 1} / ${runs.length} · 会话总计为 ${runs.length} run 合计` : "本会话 1 个 run（多轮对话 = 多个 run，各自一条链路）"}
                </span>
              </div>
              ); })()}
              {/* 结束原因横幅：仅真正的错误用红色告警；暂停（cancelled）用中性琥珀色说明，不吓人 */}
              {run.status !== "ok" && run.finishReason && (() => { const err = run.status === "error"; return (
                <div style={{ display: "flex", alignItems: "center", gap: 8, margin: "0 4px 10px", padding: "7px 11px", borderRadius: 8,
                  background: err ? "color-mix(in srgb, var(--danger) 8%, transparent)" : "color-mix(in srgb, var(--warning) 8%, transparent)",
                  border: err ? "1px solid rgba(239,68,68,0.22)" : "1px solid rgba(245,158,11,0.22)",
                  fontSize: "var(--text-sm)", color: err ? "var(--danger)" : "var(--warning)" }}>
                  <span>{err ? "⚠️ 失败原因" : "⏸ 结束原因"}</span>
                  <span style={{ color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}>{run.finishReason}</span>
                </div>
              ); })()}
              <div className="dash-wf-embed"><Waterfall run={run} session={session} /></div>
              <div className="dash-tdiv">逐轮明细 · 点开看「发往模型的上下文 / 推理 / 工具 / 回复」</div>
              {run.turns.map((t, i) => (
                <TurnRow key={t.turn} turn={t} runTurns={run.turns} idxInRun={i}
                  open={openTurns.has(session.id + "#" + t.turn)}
                  onToggle={() => toggleTurn(session.id + "#" + t.turn)} />
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TurnRow({ turn, runTurns, idxInRun, open, onToggle }: {
  turn: Turn; runTurns: Turn[]; idxInRun: number; open: boolean; onToggle: () => void;
}) {
  const rm = ROLE_META[turn.role];
  let summary = turn.content ? turn.content.replace(/\n/g, " ") : "";
  if (!summary && turn.tool_calls?.length) summary = "调用工具：" + turn.tool_calls.map((tc) => tc.name).join(", ");
  const ctx = useMemo(() => (turn.role === "assistant" ? buildContext(runTurns, idxInRun) : null), [turn, runTurns, idxInRun]);
  return (
    <div className="dash-turnwrap">
      <div className={"dash-turn" + (open ? " open" : "")} onClick={onToggle}>
        <span style={{ width: 6, height: 6, borderRadius: "50%", background: rm.color, flexShrink: 0 }} />
        <span style={{ fontSize: "var(--text-sm)", fontWeight: 600, color: rm.color, flexShrink: 0, minWidth: 62 }}>T{turn.turn}·{rm.label}</span>
        <span style={{ flex: 1, fontSize: "var(--text-sm)", color: "var(--text-tertiary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{summary.slice(0, 72)}</span>
        {turn.llm ? (
          <span className="mono" style={{ display: "flex", gap: 11, flexShrink: 0, fontSize: "var(--text-xs)", alignItems: "center" }}>
            <span style={{ color: ACCENT }}>in {fmt(turn.llm.input_tokens)}</span>
            <span style={{ color: "var(--success)" }}>out {fmt(turn.llm.output_tokens)}</span>
            <span style={{ color: "var(--warning)" }} title={`实际净费用 · 全价 ${fmtCost(callFullCost(turn.llm))}${(turn.llm.cache_saved_usd ?? 0) > 0 ? ` · 命中省 ${fmtCost(turn.llm.cache_saved_usd ?? 0)}` : ""}`}>{fmtCost(callNetCost(turn.llm))}</span>
            <span style={{ color: turn.llm.duration_ms >= ALERT_P99_LATENCY_MS ? "var(--danger)" : "var(--text-muted)" }}>{fmtMs(turn.llm.duration_ms)}</span>
            {turn.llm.cache_hit_ratio != null && <span style={{ color: ACCENT }}>🎯 {turn.llm.cache_hit_ratio.toFixed(1)}%</span>}
          </span>
        ) : (
          <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", flexShrink: 0 }}>{turn.role === "tool" ? "工具返回" : "用户输入"}</span>
        )}
        <span className="dash-tchev" style={{ transform: open ? "rotate(90deg)" : "none" }}>›</span>
      </div>
      {open && (
        <div className="dash-tdetails">
          {turn.role === "user" && <Block label="💬 用户消息" mono={false}>{turn.content}</Block>}
          {turn.role === "assistant" && (
            <>
              {ctx && <CtxBlock msgs={ctx} />}
              {turn.reasoning && <Block label="🧠 推理 reasoning" color={ACCENT}>{turn.reasoning}</Block>}
              {turn.tool_calls?.map((tc, i) => (
                <Block key={i} label={"🔧 工具调用 · " + tc.name} color="var(--info)">{JSON.stringify(tc.args, null, 2)}</Block>
              ))}
              {turn.content && <Block label="💬 回复" mono={false} color="var(--success)">{turn.content}</Block>}
              {turn.llm && <div className="mono" style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)" }}>{turn.llm.model} · step {turn.llm.step} · {fmtLocalTime(turn.timestamp)}</div>}
            </>
          )}
          {turn.role === "tool" && <Block label="📤 工具结果">{turn.content}</Block>}
        </div>
      )}
    </div>
  );
}

/* ═══════════════ 图表 & 小组件 ═══════════════ */
function Waterfall({ run, session }: { run: ReturnType<typeof groupRuns>[number]; session: SessionData }) {
  const spans = useMemo(() => buildSpans(run, session, ALERT_P99_LATENCY_MS), [run, session]);
  const total = spans[0]?.durationMs || 1;
  const anyUnknown = spans.some((s) => s.kind === "tool" && s.unknown);
  // over = 超 p99 的「慢调用」高亮，用琥珀色（warning）——不是错误，不用红色（红仅表失败）。
  const color = (s: (typeof spans)[number]) => s.over ? "var(--warning)" : s.kind === "tool" ? "var(--info)" : s.kind === "run" ? "rgba(127,127,127,0.4)" : ACCENT;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      {spans.map((s, i) => (
        <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, height: 28, fontSize: "var(--text-xs)" }}>
          <span className="mono" style={{ width: 150, flexShrink: 0, color: s.kind === "run" ? "var(--text-secondary)" : "var(--text-tertiary)", paddingLeft: s.kind === "tool" ? 26 : s.kind === "llm" ? 14 : 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{s.label}</span>
          <span style={{ flex: 1, position: "relative", height: "100%" }}>
            {s.unknown ? (
              // 无真实 span：不伪造宽度，画一个空心虚线标记 + 「耗时未知」
              <span title={s.meta} style={{
                position: "absolute", top: 5, height: 18, left: `${(s.startMs / total) * 100}%`,
                padding: "0 8px", borderRadius: 5, border: "1px dashed var(--text-muted)", background: "transparent",
                display: "flex", alignItems: "center", color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "var(--text-2xs)", whiteSpace: "nowrap",
              }}>耗时未知</span>
            ) : (
              <span title={s.meta} style={{
                position: "absolute", top: 5, height: 18, left: `${(s.startMs / total) * 100}%`,
                width: `${Math.max((s.durationMs / total) * 100, 2)}%`, background: color(s), opacity: s.kind === "run" ? 0.5 : 0.9,
                borderRadius: 5, display: "flex", alignItems: "center", padding: "0 7px", color: "var(--text-on-accent)", fontFamily: "var(--font-mono)", fontSize: "var(--text-2xs)", whiteSpace: "nowrap",
              }}>{s.durationMs / total > 0.12 ? fmtMs(s.durationMs) : ""}{s.over ? " 🐢慢" : ""}</span>
            )}
          </span>
        </div>
      ))}
      {anyUnknown && <div style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", marginTop: 4 }}>💡 虚线标记 = 该工具调用无 trace span，无法获取真实耗时（不以均值伪造）</div>}
    </div>
  );
}

function Donut({ segments, center, label }: { segments: { v: number; c: string }[]; center: string; label: string }) {
  const R = 46, C = 2 * Math.PI * R, tot = segments.reduce((a, s) => a + s.v, 0) || 1;
  let off = 0;
  return (
    <svg width={116} height={116} viewBox="0 0 120 120">
      <circle cx={60} cy={60} r={R} fill="none" stroke="rgba(127,127,127,0.14)" strokeWidth={13} />
      {segments.map((s, i) => {
        const len = (s.v / tot) * C, el = (
          <circle key={i} cx={60} cy={60} r={R} fill="none" stroke={s.c} strokeWidth={13} strokeDasharray={`${len} ${C - len}`} strokeDashoffset={-off} transform="rotate(-90 60 60)" />
        );
        off += len; return el;
      })}
      <text x={60} y={56} textAnchor="middle" fontSize={16} fontWeight={700} fill="var(--text-primary)" fontFamily="var(--font-mono)">{center}</text>
      <text x={60} y={74} textAnchor="middle" fontSize={10} fill="var(--text-tertiary)">{label}</text>
    </svg>
  );
}

/* 每日趋势曲线（AC-04）：折线 + 数据点 + 峰值标注，替代直方图。支持多条 series
 * （token 图叠加输入/输出两条）。数据点 x 均匀铺满；峰值取所有 series 的最大值统一比例尺。 */
function LineTrend({ data, lines, fmtVal }: {
  data: { date: string }[];
  lines: { values: number[]; color: string; label: string }[];
  fmtVal: (n: number) => string;
}) {
  const W = 900, H = 170, padL = 6, padR = 6, padT = 16, padB = 22;
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const n = data.length;
  const max = Math.max(1e-9, ...lines.flatMap((l) => l.values));
  const x = (i: number) => padL + (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW);
  const y = (v: number) => padT + innerH - (v / max) * innerH;
  const baseY = padT + innerH;
  // Catmull-Rom → 三次贝塞尔，得到柔和曲线（相邻点切线由前后邻点决定，张力 1/6）
  const smoothPath = (vals: number[]) => {
    const pts = vals.map((v, i) => ({ x: x(i), y: y(v) }));
    if (pts.length === 0) return "";
    if (pts.length === 1) return `M${pts[0].x.toFixed(1)},${pts[0].y.toFixed(1)}`;
    let d = `M${pts[0].x.toFixed(1)},${pts[0].y.toFixed(1)}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
      const c1x = p1.x + (p2.x - p0.x) / 6, c1y = p1.y + (p2.y - p0.y) / 6;
      const c2x = p2.x - (p3.x - p1.x) / 6, c2y = p2.y - (p3.y - p1.y) / 6;
      d += ` C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${p2.x.toFixed(1)},${p2.y.toFixed(1)}`;
    }
    return d;
  };
  const gid = (i: number) => `dash-trend-fill-${i}`;
  return (
    <div>
      <div style={{ display: "flex", gap: 16, marginBottom: 8, fontSize: "var(--text-xs)" }}>
        {lines.map((l) => <Legend key={l.label} color={l.color} label={l.label} />)}
        <span style={{ marginLeft: "auto", color: "var(--text-muted)" }}>峰值 {fmtVal(max)}</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block" }}>
        <defs>
          {lines.map((l, li) => (
            <linearGradient key={li} id={gid(li)} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={l.color} stopOpacity={0.18} />
              <stop offset="100%" stopColor={l.color} stopOpacity={0} />
            </linearGradient>
          ))}
        </defs>
        <line x1={padL} y1={baseY} x2={W - padR} y2={baseY} stroke="var(--border-subtle)" />
        {lines.map((l, li) => {
          const line = smoothPath(l.values);
          const area = line ? `${line} L${x(n - 1).toFixed(1)},${baseY} L${x(0).toFixed(1)},${baseY} Z` : "";
          return (
            <g key={l.label}>
              {area && <path d={area} fill={`url(#${gid(li)})`} stroke="none" />}
              <path d={line} fill="none" stroke={l.color} strokeWidth={2.4}
                strokeLinejoin="round" strokeLinecap="round" opacity={0.95} />
              {l.values.map((v, i) => (v > 0 ? <circle key={i} cx={x(i)} cy={y(v)} r={2.5} fill={l.color} /> : null))}
            </g>
          );
        })}
        {data.map((d, i) => (
          <text key={d.date} x={x(i)} y={H - 6} textAnchor="middle" fontSize={9.5} fill="var(--text-muted)">{d.date.slice(5)}</text>
        ))}
      </svg>
    </div>
  );
}

function CtxBlock({ msgs }: { msgs: ReturnType<typeof buildContext> }) {
  const col: Record<string, string> = { system: "var(--text-muted)", user: ACCENT, assistant: "var(--success)", tool: "var(--info)" };
  return (
    <div className="dash-tblk ctx">
      <div className="dash-bl">📥 发往模型的上下文 · {msgs.length} 条消息（完整原文 · system 不重复）</div>
      <div className="dash-bc" style={{ fontFamily: "inherit" }}>
        {msgs.map((m, i) => (
          <div key={i} style={{ display: "flex", gap: 8, padding: "5px 0", borderTop: i ? "1px dashed var(--border-subtle)" : "none", alignItems: "baseline" }}>
            <span className="mono" style={{ flexShrink: 0, fontSize: "var(--text-2xs)", padding: "1px 6px", borderRadius: 4, background: "var(--bg-track)", color: col[m.role] }}>{m.role}</span>
            <span style={{ flex: 1, fontSize: "var(--text-xs)", color: m.isSystemRef ? "var(--text-muted)" : "var(--text-secondary)" }}>
              {m.isSystemRef ? "共享系统提示 · 见会话顶部 ⚙️（此处不重复）" : m.text}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Block({ label, children, mono = true, color }: { label: string; children: React.ReactNode; mono?: boolean; color?: string }) {
  return (
    <div className="dash-tblk">
      <div className="dash-bl" style={{ color: color || "var(--text-tertiary)" }}>{label}</div>
      <div className="dash-bc" style={{ fontFamily: mono ? "var(--font-mono)" : "inherit" }}>{children}</div>
    </div>
  );
}

function Kpi({ label, value, sub, color, clickable, onClick }: { label: string; value: string; sub: string; color?: string; clickable?: boolean; onClick?: () => void }) {
  return (
    <div className={"dash-kpi" + (clickable ? " clk" : "")} onClick={onClick}>
      <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.5px" }}>{label}</div>
      <div className="mono" style={{ fontSize: "var(--text-3xl)", fontWeight: 700, margin: "4px 0 3px", color: color || "var(--text-primary)" }}>{value}</div>
      <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-tertiary)" }}>{sub}</div>
    </div>
  );
}
function Panel({ title, children, style }: { title: React.ReactNode; children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", padding: 16, ...style }}>
      <div style={{ fontSize: "var(--text-sm)", fontWeight: 600, color: "var(--text-tertiary)", marginBottom: 12, display: "flex", alignItems: "center", gap: 8 }}>{title}</div>
      {children}
    </div>
  );
}
function BigStat({ value, color }: { value: string; color: string }) {
  return <div className="mono" style={{ fontSize: "var(--text-3xl)", fontWeight: 700, textAlign: "center", color, padding: "6px 0" }}>{value}</div>;
}
function Stat({ val, lbl, color }: { val: string; lbl: string; color?: string }) {
  return (
    <div style={{ minWidth: 56 }}>
      <div className="mono" style={{ fontSize: "var(--text-md)", fontWeight: 700, color: color || "var(--text-primary)" }}>{val}</div>
      <div style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.3px" }}>{lbl}</div>
    </div>
  );
}
function Legend({ color, label }: { color: string; label: string }) {
  return <span style={{ display: "inline-flex", alignItems: "center", gap: 5, color: "var(--text-tertiary)" }}><span style={{ width: 9, height: 9, borderRadius: 2, background: color }} />{label}</span>;
}
/* 环形图图例行：色块 + 名称 + 金额 + 占比（用于费用构成的输入/输出饼图） */
function DonutLegend({ color, label, value, pct }: { color: string; label: string; value: string; pct: number }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
      <span style={{ width: 11, height: 11, borderRadius: 3, background: color, flexShrink: 0 }} />
      <span style={{ flex: 1, fontSize: "var(--text-sm)", color: "var(--text-secondary)" }}>{label}</span>
      <span className="mono" style={{ fontSize: "var(--text-sm)", fontWeight: 700, color }}>{value}</span>
      <span className="mono" style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", minWidth: 42, textAlign: "right" }}>{pct.toFixed(0)}%</span>
    </div>
  );
}
function QuickRange({ label, onClick }: { label: string; onClick: () => void }) {
  return <button type="button" onClick={onClick} className="dash-qr">{label}</button>;
}
function Empty({ text }: { text: string }) {
  return <div style={{ padding: "80px 0", textAlign: "center", color: "var(--text-muted)", fontSize: "var(--text-md)" }}>{text}</div>;
}
function Muted({ text }: { text: string }) {
  return <div style={{ fontSize: "var(--text-sm)", color: "var(--text-muted)" }}>{text}</div>;
}

/* ── 局部样式（scoped class，避免污染全局）── */

