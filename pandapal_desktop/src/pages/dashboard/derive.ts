/**
 * src/pages/dashboard/derive.ts — Dashboard 小系统 · 派生层（纯函数，无 React）
 *
 * 「调用事实表 CallFact」是成本/性能/会话三视图的共同底座（PRD §3.4）。
 * 全部从后端已提供的 SessionData[]（含 turns[].llm）flatten，不依赖数据层改动（P0）。
 */

import type { DegradationStat, SessionData, Turn, TurnLLM } from "../../types/dashboard";
import { HIGH_STAKES_CATEGORIES, LATENCY_BUCKETS, PERF_MIN_SAMPLE_SIZE } from "./constants";

/* ── 费用取数口径（全部只读后端 cost_of_call 算好的值，前端从不重算）───────────
 * 费用真相源只有一个：应用层价格表（APP_PRICE_TABLE），后端 cost_of_call 一处算清
 * （正向三项式：命中按缓存价 + 未命中按全价 + 输出价）。前端只做「取值 + sum」，绝不自算单价。
 *
 * 字段语义（后端 CallCost）：
 *   net_cost_usd  = 实际净费用（主口径）；input_cost_usd + output_cost_usd == net_cost_usd
 *   cache_saved_usd = 相对全价省下的钱；「全价」= net + saved（恒等式，见下）
 * 「全价」刻意用 net + saved 求得，而非 input+output——后者在新口径下等于 net。
 * SDK trace 已无 cost_usd，费用只认这张表。字段缺省（旧快照/旧二进制）→ 0，不在前端伪造口径。 */
export function callNetCost(llm: TurnLLM): number {
  return llm.net_cost_usd ?? 0;
}
export function callFullCost(llm: TurnLLM): number {
  // 全价基线 = 净费用 + 命中节省（恒等式 net + saved == full，后端保证自洽）
  return (llm.net_cost_usd ?? 0) + (llm.cache_saved_usd ?? 0);
}

/* ── 调用事实表（一行 = 一次 llm_call）─────────────────────────── */
export interface CallFact {
  sessionId: string;
  title: string;
  model: string;
  step: number;
  turn: number; // raw_log turn 号，用于深链精确展开
  inTokens: number;
  outTokens: number;
  cost: number;
  durationMs: number;
  cacheHit: number | null; // cache_hit_ratio %；旧数据 null
  cachedTokens: number; // 命中缓存的输入 token 数
  cacheSavedUsd: number; // 命中缓存相对全价省下的 USD（后端精算）
  inCost: number; // 输入 token 费用（USD）
  outCost: number; // 输出 token 费用（USD）
  netCost: number; // 实际净费用（取自后端 net_cost_usd，见 callNetCost；前端不算，下游只 sum）
  status: string;
}

export function flattenFacts(sessions: SessionData[]): CallFact[] {
  const facts: CallFact[] = [];
  for (const s of sessions) {
    const runStatus = s.runs[0]?.status ?? "ok";
    for (const t of s.turns) {
      if (!t.llm) continue;
      // 费用全部取自后端 cost_of_call（app 价格表一处算清），下游只做 sum
      const cost = callFullCost(t.llm); // 全价基线 = 净 + 命中节省（恒等式）
      const cacheSavedUsd = t.llm.cache_saved_usd ?? 0;
      const netCost = callNetCost(t.llm); // 净费用（源头算好，此处仅取）；inCost+outCost==netCost
      facts.push({
        sessionId: s.id,
        title: s.title || "未命名会话",
        model: t.llm.model || s.model || "未知模型",
        step: t.llm.step,
        turn: t.turn,
        inTokens: t.llm.input_tokens,
        outTokens: t.llm.output_tokens,
        cost,
        durationMs: t.llm.duration_ms,
        cacheHit: t.llm.cache_hit_ratio,
        cachedTokens: t.llm.cached_tokens ?? 0,
        cacheSavedUsd,
        inCost: t.llm.input_cost_usd ?? 0,
        outCost: t.llm.output_cost_usd ?? 0,
        netCost,
        status: runStatus,
      });
    }
  }
  return facts;
}

/* ── 全局健康 ──────────────────────────────────────────────────── */
export interface Health {
  llmCalls: number;
  runTotal: number;
  runOk: number;
  runFail: number;
  errRate: number; // %
  totalCost: number;
  totalIn: number;
  totalOut: number;
  cachedTokens: number; // 命中缓存的输入 token 总数
  cacheSavedUsd: number; // 命中缓存省下的钱（相对全价基线）
  hitRate: number; // 整体命中率 % = Σcached / Σinput（按 input token 加权）
  inputCost: number; // 输入 token 费用合计（USD）
  outputCost: number; // 输出 token 费用合计（USD）
  netCost: number; // 实际净费用 = Σ 各调用 netCost（纯 sum；净费用只在后端 cost_breakdown 一处算）
}

export function computeHealth(sessions: SessionData[], facts: CallFact[]): Health {
  const runs = sessions.flatMap((s) => s.runs);
  const runOk = runs.filter((r) => r.status === "ok").length;
  // 失败只算真正的 error；cancelled = 暂停（等待人工/审批），是正常中间态，不计失败。
  const runFail = runs.filter((r) => r.status === "error").length;
  const totalIn = sessions.reduce((a, s) => a + s.input_tokens, 0);
  const cachedTokens = facts.reduce((a, f) => a + f.cachedTokens, 0);
  // 全部费用口径都是「对 facts 做 sum」——每次调用的费用已在 flattenFacts 一次算清
  return {
    llmCalls: facts.length,
    runTotal: runs.length,
    runOk,
    runFail,
    errRate: runs.length ? (runFail / runs.length) * 100 : 0,
    totalCost: facts.reduce((a, f) => a + f.cost, 0),
    totalIn,
    totalOut: sessions.reduce((a, s) => a + s.output_tokens, 0),
    cachedTokens,
    cacheSavedUsd: facts.reduce((a, f) => a + f.cacheSavedUsd, 0),
    hitRate: totalIn ? (cachedTokens / totalIn) * 100 : 0,
    inputCost: facts.reduce((a, f) => a + f.inCost, 0),
    outputCost: facts.reduce((a, f) => a + f.outCost, 0),
    netCost: facts.reduce((a, f) => a + f.netCost, 0),
  };
}

/* ── 按模型拆分 ────────────────────────────────────────────────── */
export interface ModelCost {
  model: string;
  tokens: number;
  inputTokens: number; // 本模型输入 token 合计（命中率分母，§3.4.4）
  cost: number;
  calls: number;
  share: number; // 0~1，按 cost 占比（全价）
  cachedTokens: number; // 命中缓存的输入 token 数
  hitRate: number; // 本模型命中率 % = cachedTokens / inputTokens（唯一口径 R2；分母 0 → 0）
  cacheSavedUsd: number; // 命中缓存省下的钱（相对全价基线）
  inCost: number; // 输入 token 费用（USD）
  outCost: number; // 输出 token 费用（USD）
  netCost: number; // 实际净费用（Σ 本模型各调用 netCost）
  netShare: number; // 0~1，按 netCost 占比
}

export function computeModels(facts: CallFact[]): ModelCost[] {
  const map = new Map<string, ModelCost>();
  for (const f of facts) {
    const e =
      map.get(f.model) ??
      { model: f.model, tokens: 0, inputTokens: 0, cost: 0, calls: 0, share: 0, cachedTokens: 0, hitRate: 0, cacheSavedUsd: 0, inCost: 0, outCost: 0, netCost: 0, netShare: 0 };
    e.tokens += f.inTokens + f.outTokens;
    e.inputTokens += f.inTokens;
    e.cost += f.cost;
    e.calls += 1;
    e.cachedTokens += f.cachedTokens;
    e.cacheSavedUsd += f.cacheSavedUsd;
    e.inCost += f.inCost;
    e.outCost += f.outCost;
    e.netCost += f.netCost;
    map.set(f.model, e);
  }
  const total = facts.reduce((a, f) => a + f.cost, 0) || 1;
  const totalNet = facts.reduce((a, f) => a + f.netCost, 0) || 1;
  return [...map.values()]
    .map((e) => ({
      ...e,
      share: e.cost / total,
      netShare: e.netCost / totalNet,
      // R2 唯一命中率口径：原始 cached / 原始 In（与 footer/run/global 同公式）
      hitRate: e.inputTokens ? (e.cachedTokens / e.inputTokens) * 100 : 0,
    }))
    .sort((a, b) => b.netCost - a.netCost);
}

/* ── 性能分布（分位 + 直方图）──────────────────────────────────── */
export interface PerfDist {
  count: number;
  p50: number;
  p95: number;
  p99: number;
  reliable: boolean; // 样本 ≥ PERF_MIN_SAMPLE_SIZE
  buckets: { lo: number; hi: number; count: number }[];
}

export function computePerf(facts: CallFact[]): PerfDist {
  const ds = facts.map((f) => f.durationMs).sort((a, b) => a - b);
  const pct = (p: number) => (ds.length ? ds[Math.min(ds.length - 1, Math.floor((p / 100) * ds.length))] : 0);
  return {
    count: ds.length,
    p50: pct(50),
    p95: pct(95),
    p99: pct(99),
    reliable: ds.length >= PERF_MIN_SAMPLE_SIZE,
    buckets: LATENCY_BUCKETS.map(([lo, hi]) => ({
      lo,
      hi,
      count: facts.filter((f) => f.durationMs >= lo && f.durationMs < hi).length,
    })),
  };
}

/* ── 慢调用 Top N ──────────────────────────────────────────────── */
export interface SlowCall extends CallFact {}

export function computeSlow(facts: CallFact[], n: number): SlowCall[] {
  return [...facts].sort((a, b) => b.durationMs - a.durationMs).slice(0, n);
}

/* ── 消费 Top 会话 ─────────────────────────────────────────────── */
export interface SessionSpend {
  session: SessionData;
  cost: number;
  tokens: number;
}

/** 会话级实际净费用 = Σ 该会话各调用 netCost（纯 sum 统计）。 */
export function sessionNetCost(s: SessionData): number {
  return s.turns.reduce((a, t) => a + (t.llm ? callNetCost(t.llm) : 0), 0);
}

export function topSpendSessions(sessions: SessionData[]): SessionSpend[] {
  return sessions
    .map((s) => ({ session: s, cost: sessionNetCost(s), tokens: s.input_tokens + s.output_tokens }))
    .sort((a, b) => b.cost - a.cost);
}

/* ── run 分组（1 session : N run；user 轮为 run 边界）─────────────── */
export interface RunGroup {
  runId: string;
  turns: Turn[];
  durationMs: number; // Σ 该 run 各 llm 时长
  steps: number;
  net: number; // run 级实际净费用 = Σ 本 run 各 llm net_cost_usd（与 footer 单 run 逐字段对齐，D1）
  inTokens: number; // run 级输入 token 合计（与 footer 对齐）
  outTokens: number; // run 级输出 token 合计（与 footer 对齐）
  cachedTokens: number; // run 级命中缓存输入 token（原始值，非从百分比反推）
  cacheHit: number; // run 级命中率 % = 原始 cached / 原始 In（唯一口径，R2）
  status: string;
  finishReason: string; // run 结束/失败原因
}

export function groupRuns(session: SessionData): RunGroup[] {
  // 权威状态/原因：run_id → status/finish_reason（session.runs 可能含重复项，dedupe）
  const statusById = new Map<string, string>();
  const reasonById = new Map<string, string>();
  for (const r of session.runs) {
    if (!statusById.has(r.id)) statusById.set(r.id, r.status);
    if (!reasonById.has(r.id) && r.finish_reason) reasonById.set(r.id, r.finish_reason);
  }
  // 每条 user 消息起一个新 run（run 边界），run 标签取组内真实 run_id
  const groups: Turn[][] = [];
  for (const t of session.turns) {
    if (t.role === "user" || groups.length === 0) groups.push([t]);
    else groups[groups.length - 1].push(t);
  }
  return groups.map((turns, i) => {
    const llm = turns.filter((t) => t.llm);
    const inTot = llm.reduce((a, t) => a + (t.llm!.input_tokens || 0), 0);
    const outTot = llm.reduce((a, t) => a + (t.llm!.output_tokens || 0), 0);
    // R2 唯一命中率口径：原始 cached / 原始 In。禁止从 2dp cache_hit_ratio 百分比反推 cached
    // （旧写法 input_tokens × (cache_hit_ratio/100) 会二次损失精度，与 footer/global 对不齐）。
    const cch = llm.reduce((a, t) => a + (t.llm!.cached_tokens ?? 0), 0);
    // D1 范围对齐：run 级 net/tokens 纯 sum 后端算好的 net_cost_usd，供与 footer（单 run Σ）逐字段核对。
    const net = llm.reduce((a, t) => a + callNetCost(t.llm!), 0);
    const rid = turns.map((t) => t.run_id).find(Boolean) || session.runs[i]?.id || `run #${i + 1}`;
    return {
      runId: rid,
      turns,
      durationMs: llm.reduce((a, t) => a + (t.llm!.duration_ms || 0), 0),
      steps: llm.length,
      net,
      inTokens: inTot,
      outTokens: outTot,
      cachedTokens: cch,
      cacheHit: inTot ? (cch / inTot) * 100 : 0,
      status: statusById.get(rid) || session.runs[0]?.status || "ok",
      finishReason: reasonById.get(rid) || "",
    };
  });
}

/* ── waterfall span（run 内）──────────────────────────────────────
 * P0：llm_call 步的时长精确（来自 turns[].llm）；工具时长为 session.tools 的
 * 均值近似（per-call 真实时长待 P1，PRD §3.6）。 */
export interface Span {
  label: string;
  startMs: number;
  durationMs: number;
  kind: "run" | "llm" | "tool";
  over: boolean; // 越阈慢调用
  unknown: boolean; // 工具调用无 trace span，耗时未知（不伪造均值）
  meta: string;
}

export function buildSpans(run: RunGroup, session: SessionData, p99ThresholdMs: number): Span[] {
  // 每个 assistant 轮的工具：只用真实 tool_spans（P1）；拿不到真值 → 标 unknown，绝不用均值伪造
  const turnTools = (t: Turn): { name: string; d: number; unknown: boolean }[] => {
    if (t.tool_spans?.length) return t.tool_spans.map((ts) => ({ name: ts.name, d: ts.duration_ms, unknown: false }));
    return (t.tool_calls ?? []).map((tc) => ({ name: tc.name, d: 0, unknown: true }));
  };
  const spans: Span[] = [];
  let cursor = 0;
  // 总时长只累加真实值（unknown 贡献 0，不污染比例尺）
  const total =
    run.turns.filter((t) => t.llm).reduce((a, t) => a + (t.llm!.duration_ms || 0), 0) +
    run.turns.flatMap(turnTools).reduce((a, x) => a + x.d, 0);
  spans.push({
    label: `⬢ ${run.runId}`,
    startMs: 0,
    durationMs: total || 1,
    kind: "run",
    over: false,
    unknown: false,
    meta: `${run.steps} steps · ${Math.round(total)}ms`,
  });
  for (const t of run.turns) {
    if (t.llm) {
      const over = t.llm.duration_ms >= p99ThresholdMs;
      spans.push({
        label: `step ${t.llm.step}`,
        startMs: cursor,
        durationMs: t.llm.duration_ms,
        kind: "llm",
        over,
        unknown: false,
        meta:
          `llm_call · in ${t.llm.input_tokens} out ${t.llm.output_tokens} · 净$${callNetCost(t.llm).toFixed(4)}（全价$${callFullCost(t.llm).toFixed(4)}）· ` +
          `${Math.round(t.llm.duration_ms)}ms` +
          (t.llm.cache_hit_ratio != null ? ` · 🎯${t.llm.cache_hit_ratio.toFixed(1)}%` : ""),
      });
      cursor += t.llm.duration_ms;
    }
    for (const x of turnTools(t)) {
      spans.push({
        label: x.name,
        startMs: cursor,
        durationMs: x.d,
        kind: "tool",
        over: false,
        unknown: x.unknown,
        meta: x.unknown ? `tool_call · 耗时未知（无 trace span）` : `tool_call · ${Math.round(x.d)}ms`,
      });
      cursor += x.d; // unknown → +0，不推进（避免用假值占位）
    }
  }
  return spans;
}

/* ── 工具调用事实（per-call，用于工具耗时 Top）──────────────────── */
export interface ToolCallFact {
  sessionId: string;
  title: string;
  turn: number;
  name: string;
  durationMs: number;
  model: string; // 发起该工具调用的 llm.model（工具 span 与 llm_call 共享 run_id/step → 同轮归属）
  status: string | null; // 工具 span 状态 ok | error；旧快照无状态 → null（不计入成功率分母）
}

export function flattenToolCalls(sessions: SessionData[]): ToolCallFact[] {
  const out: ToolCallFact[] = [];
  for (const s of sessions) {
    for (const t of s.turns) {
      const model = t.llm?.model || s.model || "未知模型";
      for (const ts of t.tool_spans ?? []) {
        out.push({
          sessionId: s.id, title: s.title || "未命名会话", turn: t.turn,
          name: ts.name, durationMs: ts.duration_ms,
          model, status: ts.status ?? null,
        });
      }
    }
  }
  return out;
}

export function computeToolSlow(facts: ToolCallFact[], n: number): ToolCallFact[] {
  return [...facts].sort((a, b) => b.durationMs - a.durationMs).slice(0, n);
}

/* ── 工具成功率·按模型（★ 新功能 3a · D6）──────────────────────────────
 * 关联键：工具 span 与发起它的 llm_call 共享 (run_id, step)，归属到该轮 `llm.model`
 * （见 flattenToolCalls）。口径决策：分母 = **已 trace 到 status 的工具调用**（与慢工具榜/
 * 工具使用榜同源，口径统一）；旧快照无 status 的调用不计入。total==0 → successRate=null
 * （前端显示「—」，不显示 0%）。 */
export interface ToolSuccessByModel {
  model: string;
  total: number; // 该模型发起、已 trace 到 status 的工具调用总数
  ok: number; // 其中 status==ok 的数量
  successRate: number | null; // % = ok/total×100；total==0 → null
}

export function computeToolSuccessByModel(facts: ToolCallFact[]): ToolSuccessByModel[] {
  const map = new Map<string, { total: number; ok: number }>();
  for (const f of facts) {
    if (f.status == null) continue; // 无状态（旧快照）→ 不计入分母
    const e = map.get(f.model) ?? { total: 0, ok: 0 };
    e.total += 1;
    if (f.status === "ok") e.ok += 1;
    map.set(f.model, e);
  }
  return [...map.entries()]
    .map(([model, e]) => ({
      model, total: e.total, ok: e.ok,
      successRate: e.total ? (e.ok / e.total) * 100 : null,
    }))
    .sort((a, b) => b.total - a.total);
}

/* ── 工具使用排行（累积次数 + 累积时长）──────────────────────────────
 * 数据源：session.tools（后端 ToolStat：count = 该会话该工具的 trace span 数，
 * duration_ms = 该会话该工具的「平均」耗时）。跨会话聚合：
 *   累积次数 = Σ count；累积时长 = Σ (count × 平均耗时)；平均 = 累积时长 / 累积次数。
 * 与 flattenToolCalls（读 tool_spans）同源，口径一致（均为「已 trace 的工具调用」）。 */
export interface ToolUsage {
  name: string;
  count: number; // 累积调用次数
  totalMs: number; // 累积耗时（毫秒）
  avgMs: number; // 平均单次耗时（毫秒）
}

export function computeToolUsage(sessions: SessionData[]): ToolUsage[] {
  const map = new Map<string, { count: number; totalMs: number }>();
  for (const s of sessions) {
    for (const t of s.tools) {
      const e = map.get(t.name) ?? { count: 0, totalMs: 0 };
      e.count += t.count;
      e.totalMs += t.count * t.duration_ms; // duration_ms 是「均值」→ 还原为该会话总耗时
      map.set(t.name, e);
    }
  }
  return [...map.entries()]
    .map(([name, e]) => ({ name, count: e.count, totalMs: e.totalMs, avgMs: e.count ? e.totalMs / e.count : 0 }))
    .sort((a, b) => b.count - a.count);
}

/* ── 上下文快照（重建「发往模型的消息序列」）────────────────────────
 * P0：从 run 内截至该 assistant 轮之前的消息重建；system prompt 数据层未落盘，
 * 用占位（P1 由数据层直供真值）。 */
export interface CtxMsg {
  role: string;
  text: string;
  isSystemRef?: boolean;
}
export const SYSTEM_PROMPT_PLACEHOLDER =
  "系统提示词未在数据层落盘 —— P1 由数据层直供真值。";

export function buildContext(runTurns: Turn[], idxInRun: number): CtxMsg[] {
  const msgs: CtxMsg[] = [{ role: "system", text: SYSTEM_PROMPT_PLACEHOLDER, isSystemRef: true }];
  for (let i = 0; i < idxInRun; i++) {
    const t = runTurns[i];
    let text = t.content || "";
    if (!text && t.tool_calls?.length) text = `[调用工具 ${t.tool_calls.map((x) => x.name).join(", ")}]`;
    msgs.push({ role: t.role, text });
  }
  return msgs;
}

/* ── 每日聚合（趋势）──────────────────────────────────────────────── */
export function dailySeries(
  sessions: SessionData[],
  start: string,
  end: string,
): { date: string; input: number; output: number; cost: number }[] {
  // s.cost 已是会话「净费用」（后端 Σ net_cost_usd，app 价格表）→ 每日趋势即净费用趋势
  const map = new Map<string, { input: number; output: number; cost: number }>();
  for (const s of sessions) {
    const d = s.created_at.slice(0, 10);
    const e = map.get(d) ?? { input: 0, output: 0, cost: 0 };
    e.input += s.input_tokens;
    e.output += s.output_tokens;
    e.cost += s.cost;
    map.set(d, e);
  }
  const out: { date: string; input: number; output: number; cost: number }[] = [];
  let cur = start;
  let guard = 0;
  while (cur && end && cur <= end && guard++ < 400) {
    const e = map.get(cur) ?? { input: 0, output: 0, cost: 0 };
    out.push({ date: cur, ...e });
    cur = shiftDate(cur, 1);
  }
  return out;
}

export function shiftDate(ymd: string, days: number): string {
  const [y, m, d] = ymd.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + days);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${dt.getUTCFullYear()}-${p(dt.getUTCMonth() + 1)}-${p(dt.getUTCDate())}`;
}

/* ── 降级事件派生（健康视图）─────────────────────────────────────
 * 输入是后端已按 label 归并好的 DegradationStat[]（分组/求和/排序都在
 * pandapal/dashboard/base.py 做完），前端只做「再聚合成概览口径」，不重排也不重算。
 *
 * 注意口径：counter 无时间维度，这些数字是**自有数据以来的累计**，与 CallFact
 * 系列（受日期筛选）不同源、不可混算——故本函数不接受日期参数，调用方也不得筛。 */

export interface DegradationCategoryStat {
  category: string;
  count: number;
  highStakes: boolean; // 决策/ID/金额三类：「本该失败却兜了底」，治理优先级最高
}

export interface DegradationSummary {
  total: number; // 累计降级总次数
  abortCount: number; // 其中 severity=abort（直接中止）的次数
  highStakesCount: number; // 其中决策/ID/金额三类的次数
  distinctCodes: number; // 涉及的 event_code 种类数
  top: DegradationStat | null; // 次数最多的一类
  byCategory: DegradationCategoryStat[]; // 按类别汇总，次数降序
}

export function computeDegradations(stats: DegradationStat[]): DegradationSummary {
  const byCat = new Map<string, number>();
  const codes = new Set<string>();
  let total = 0;
  let abortCount = 0;
  let highStakesCount = 0;
  let top: DegradationStat | null = null;

  for (const s of stats) {
    total += s.count;
    codes.add(s.event_code);
    if (s.severity === "abort") abortCount += s.count;
    if (HIGH_STAKES_CATEGORIES.has(s.category)) highStakesCount += s.count;
    byCat.set(s.category, (byCat.get(s.category) ?? 0) + s.count);
    if (top == null || s.count > top.count) top = s;
  }

  const byCategory: DegradationCategoryStat[] = [...byCat.entries()]
    .map(([category, count]) => ({
      category,
      count,
      highStakes: HIGH_STAKES_CATEGORIES.has(category),
    }))
    .sort((a, b) => b.count - a.count);

  return { total, abortCount, highStakesCount, distinctCodes: codes.size, top, byCategory };
}
