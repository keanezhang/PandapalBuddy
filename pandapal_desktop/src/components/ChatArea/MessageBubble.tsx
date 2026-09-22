/**
 * src/components/ChatArea/MessageBubble.tsx — 时间线版
 *
 * 已完成消息气泡。
 * 用户消息：右侧气泡 | 系统消息：居中提示 | AI 消息：左侧头像 + 按 timeline 交错渲染
 */
import React, { memo } from "react";
import { useTranslation } from "react-i18next";
import type { CompletedMessage, PendingQuestionnaire } from "../../store/chatStore";
import type { ReplyUsage } from "../../types/api";
import { useChatStore } from "../../store/chatStore";
import { InteractionInline } from "../InteractionInline";
import { Timeline } from "./Timeline";
import { MessageContent } from "./MessageContent";

interface MessageBubbleProps { message: CompletedMessage }

/**
 * memo：流式输出时 chatStore 每来一个 token 都会生成新的 messages 数组，
 * 导致 MessageList 全量重渲染。completed 消息对象引用不变，加 memo 后
 * 历史气泡可直接跳过重渲染（流式期间只有 StreamingBubble 在变）。
 * 语言切换时 useTranslation 内部订阅仍会触发本组件重渲染，翻译照常更新。
 */
export const MessageBubble = memo(function MessageBubble({ message }: MessageBubbleProps) {
  const { t } = useTranslation();
  const isUser = message.role === "user";
  const isSystem = message.role === "system";
  const isResume = message.replyScope === "hitl_resume";

  if (isSystem) {
    return (
      <div style={{ textAlign: "center", padding: "var(--space-2) var(--space-4)", marginBottom: "var(--space-3)" }}>
        <span style={{
          display: "inline-block", fontSize: "var(--text-xs)", color: "var(--text-tertiary)",
          background: "var(--bg-elevated)", borderRadius: "var(--radius-md)",
          padding: "var(--space-2) var(--space-4)", maxWidth: "80%",
          whiteSpace: "pre-wrap", wordBreak: "break-word",
        }}>
          {message.text}
        </span>
      </div>
    );
  }

  const questionnaireMsg: PendingQuestionnaire | null =
    message.questionnaire && !message.questionnaire.replied ? message.questionnaire : null;

  if (isUser) {
    return (
      <div style={{
        display: "flex", justifyContent: "flex-end",
        marginBottom: "var(--space-3)", paddingLeft: "15%",
      }}>
        <div style={{
          background: "var(--bg-panel)", borderRadius: "var(--radius-md)",
          padding: "var(--space-3) var(--space-4)", maxWidth: "100%",
          transition: "background var(--duration-fast)",
        }}
          onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = "var(--bg-hover)"; }}
          onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.background = "var(--bg-panel)"; }}
        >
          <div style={{ fontSize: "var(--text-md)", lineHeight: 1.7, color: "var(--text-primary)", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
            {message.text}
          </div>
          <div style={{ marginTop: "var(--space-1)", fontSize: "var(--text-2xs)", color: "var(--text-muted)", textAlign: "right" }}>
            {formatTime(message.timestamp)}
          </div>
        </div>
        <div style={{
          width: 24, height: 24, borderRadius: "var(--radius-full)",
          background: "var(--bg-hover)", display: "flex", alignItems: "center",
          justifyContent: "center", fontSize: "var(--text-sm)", flexShrink: 0,
          marginLeft: "var(--space-3)", marginTop: 1,
        }}>
          👤
        </div>
      </div>
    );
  }

  // AI message
  const hasTimeline = message.timeline && message.timeline.length > 0;
  return (
    <div style={{
      display: "flex", gap: "var(--space-3)",
      padding: "var(--space-3) var(--space-4)",
      borderRadius: "var(--radius-md)",
      marginBottom: "var(--space-1)",
      transition: "background var(--duration-fast)",
    }}
      onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = "rgba(255,255,255,0.02)"; }}
      onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.background = "transparent"; }}
    >
      <div style={{
        width: 24, height: 24, borderRadius: "var(--radius-full)",
        background: "var(--gradient-avatar)",
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: "var(--text-sm)", flexShrink: 0, marginTop: 1, color: "var(--text-on-accent)",
      }}>
        🐼
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {isResume && (
          <div style={{ fontSize: "var(--text-2xs)", fontWeight: 500, color: "var(--accent-soft)", marginBottom: "var(--space-2)", display: "flex", alignItems: "center", gap: 4 }}>
            {t("chat.resumeLabel")}
          </div>
        )}

        {hasTimeline ? (
          <Timeline items={message.timeline} toolCalls={message.toolCalls} isStreaming={false} />
        ) : (
          message.text && <MessageContent content={message.text} />
        )}

        {questionnaireMsg && (
          <div style={{ marginTop: "var(--space-3)" }}>
            <QuestionnaireWrapper q={questionnaireMsg} replyId={message.id} />
          </div>
        )}

        <div style={{ marginTop: "var(--space-1)", fontSize: "var(--text-2xs)", color: "var(--text-muted)" }}>
          {formatTime(message.timestamp)}
        </div>
        {/* 本轮对话消耗（后端 CostBudgetGuard.summary 精算，前端只展示不重算）；缺省则不显示 */}
        {message.usage && <UsageFooter usage={message.usage} t={t} />}
      </div>
    </div>
  );
});

/* ── 本轮消耗页脚：净费用 · tokens 明细（命中/未命中/新写 · 回复/推理）· 命中率 · 耗时 · 上下文占用 ── */
function UsageFooter({ usage: u, t }: { usage: ReplyUsage; t: (key: string, opts?: Record<string, unknown>) => string }) {
  /* 上下文进度条：分子是「最后一次调用的单次输入」（= 当前上下文占用），不是 ↑ 那个跨步累计值。
     竖线 = 自动压缩阈值，与 Claude Code 的 auto-compact 线同义。context_window=0 → 后端未注入，整行不画。 */
  const ctxPct = u.context_window > 0
    ? Math.min(100, (u.last_input_tokens / u.context_window) * 100) : 0;
  const markPct = u.context_window > 0 && u.compact_threshold > 0
    ? Math.min(100, (u.compact_threshold / u.context_window) * 100) : 0;
  const overLine = u.compact_threshold > 0 && u.last_input_tokens >= u.compact_threshold;

  /* 当前上下文被谁占了：后端按 run 收敛的最后一步组成，四段之和 == last_input_tokens。
     后端未采集（旧 sidecar / 无 guard）→ 退化为单色单段进度条。
     实占/配额：system_prompt 与 tool_schema 是固定尺寸槽位，配额来自档位表。 */
  const bd = u.context_breakdown;
  const quota = u.context_quotas;
  const PART_STYLE: { key: "system" | "tools" | "attachments" | "history"; label: string; color: string }[] = [
    { key: "system", label: "chat.usage.partSystem", color: "var(--info)" },
    { key: "tools", label: "chat.usage.partTools", color: "var(--success)" },
    { key: "attachments", label: "chat.usage.partAttachments", color: "var(--warning)" },
    { key: "history", label: "chat.usage.partHistory", color: "var(--accent)" },
  ];
  const parts = bd
    ? PART_STYLE.map((p) => ({
        ...p,
        v: bd[p.key] ?? 0,
        q: p.key === "system" ? quota?.system_prompt
          : p.key === "tools" ? quota?.tool_schema : undefined,
      })).filter((p) => p.v > 0)
    : [];
  const segW = (v: number) => (u.context_window > 0 ? (v / u.context_window) * 100 : 0);

  return (
    <div
      style={{
        marginTop: "var(--space-2)", fontSize: "var(--text-2xs)", lineHeight: 1.7,
        color: "var(--text-tertiary)", display: "flex", flexWrap: "wrap",
        alignItems: "center", gap: "4px 12px",
        borderTop: "1px solid var(--border-subtle, rgba(127,127,127,0.15))",
        paddingTop: "var(--space-2)",
      }}
    >
      <span style={{ color: "var(--warning)", fontWeight: 600 }}
            title={`${t("chat.usage.costBaseline", { cost: fmtCost(u.full_cost_usd) })}${u.saved_usd > 0 ? t("chat.usage.cacheSaved", { cost: fmtCost(u.saved_usd) }) : ""}`}>
        💰 {fmtCost(u.net_cost_usd)}
        {u.saved_usd > 0 && <span style={{ color: "var(--success)", fontWeight: 400 }}>{t("chat.usage.saved", { cost: fmtCost(u.saved_usd) })}</span>}
      </span>
      <span title={t("chat.usage.inputTokensTitle")}>
        ↑ {fmtTok(u.input_tokens)}
        <span style={{ color: "var(--text-muted)" }}>
          {" ("}{t("chat.usage.cacheHit")}{fmtTok(u.cached_tokens)} · {t("chat.usage.cacheMiss")}{fmtTok(u.miss_tokens)}
          {u.cache_creation_tokens > 0 && <> · {t("chat.usage.cacheNewWrite")}{fmtTok(u.cache_creation_tokens)}</>}
          {")"}{u.step_count > 0 && t("chat.usage.stepsInline", { n: u.step_count })}
        </span>
      </span>
      <span title={t("chat.usage.outputTokensTitle")}>
        ↓ {fmtTok(u.output_tokens)}
        <span style={{ color: "var(--text-muted)" }}>
          {" ("}{t("chat.usage.reply")}{fmtTok(u.reply_tokens)}
          {u.reasoning_tokens > 0 && <> · {t("chat.usage.reasoning")}{fmtTok(u.reasoning_tokens)}</>}
          {")"}
        </span>
      </span>
      <span title={t("chat.usage.hitRateTitle")}>🎯 {(u.hit_rate * 100).toFixed(1)}%</span>
      <span title={t("chat.usage.durationTitle")}>⏱ {fmtDuration(u.duration_ms)}</span>
      {u.context_window > 0 && (
        <span
          style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "4px 8px", width: "100%" }}
          title={t("chat.usage.contextTitle", {
            used: fmtTok(u.last_input_tokens),
            total: fmtTok(u.context_window),
            line: fmtTok(u.compact_threshold),
          })}
        >
          <span style={{ color: "var(--text-muted)" }}>{t("chat.usage.contextLabel")}</span>
          <span style={{
            position: "relative", display: "flex", flex: "1 1 auto", minWidth: "100px",
            height: "6px", background: "var(--bg-hover)", borderRadius: "3px", overflow: "hidden",
          }}>
            {parts.length > 0 ? parts.map((p) => (
              <span key={p.key} style={{ width: `${segW(p.v)}%`, background: p.color }} />
            )) : (
              <span style={{
                width: `${ctxPct}%`,
                background: overLine ? "var(--danger)" : "var(--accent)",
              }} />
            )}
            {markPct > 0 && markPct < 100 && (
              <span style={{
                position: "absolute", top: 0, bottom: 0, left: `${markPct}%`,
                width: "2px", background: "var(--text-primary)",
              }} />
            )}
          </span>
          <span style={{ color: overLine ? "var(--danger)" : "var(--text-tertiary)" }}>
            {fmtTok(u.last_input_tokens)} / {fmtTok(u.context_window)} ({ctxPct.toFixed(0)}%)
          </span>
          {parts.map((p) => (
            <span key={p.key} style={{ color: "var(--text-muted)" }}>
              <span style={{ color: p.color }}>●</span> {t(p.label)} {fmtTok(p.v)}
              {p.q ? ` / ${fmtTok(p.q)}` : ""}
            </span>
          ))}
          {u.compact_threshold > 0 && (
            <span style={{ color: "var(--text-muted)" }}>
              {t("chat.usage.compactLine", { v: fmtTok(u.compact_threshold) })}
            </span>
          )}
        </span>
      )}
    </div>
  );
}

/** 费用展示：小额用更多小数位，避免 $0.0000。仅展示，不参与任何计算。 */
function fmtCost(usd: number): string {
  if (!usd || usd <= 0) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

/** token 数：≥1M 用 M，≥10k 用 k 简写。 */
function fmtTok(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/** 耗时：ms → s（<1min）或 m s。 */
function fmtDuration(ms: number): string {
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m${Math.round(s - m * 60)}s`;
}

function QuestionnaireWrapper({ q, replyId }: { q: PendingQuestionnaire; replyId: string }) {
  // ★ 用问卷自带的 owning session，而非「当前正在看的会话」：用户切走再作答也不会串台。
  const sid = q.sessionId;
  const handleResolved = (resultText: string) => {
    if (sid) useChatStore.getState().resolveQuestionnaire(sid, replyId, resultText);
  };
  return (
    <InteractionInline
      questions={q.questions}
      run_id={q.run_id}
      tool_name={q.tool_name}
      sessionId={sid}
      onResolved={handleResolved}
    />
  );
}

function formatTime(ts: number): string {
  const d = new Date(ts);
  return `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
}
