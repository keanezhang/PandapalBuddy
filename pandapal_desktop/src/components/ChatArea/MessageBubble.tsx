/**
 * src/components/ChatArea/MessageBubble.tsx — 时间线版
 *
 * 已完成消息气泡。
 * 用户消息：右侧气泡 | 系统消息：居中提示 | AI 消息：左侧头像 + 按 timeline 交错渲染
 */
import React from "react";
import type { CompletedMessage, PendingQuestionnaire } from "../../store/chatStore";
import type { ReplyUsage } from "../../types/api";
import { useChatStore } from "../../store/chatStore";
import { InteractionInline } from "../InteractionInline";
import { Timeline } from "./Timeline";
import { MessageContent } from "./MessageContent";

interface MessageBubbleProps { message: CompletedMessage }

export function MessageBubble({ message }: MessageBubbleProps) {
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
          <div style={{ fontSize: 14, lineHeight: 1.7, color: "var(--text-primary)", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
            {message.text}
          </div>
          <div style={{ marginTop: "var(--space-1)", fontSize: 10, color: "var(--text-muted)", textAlign: "right" }}>
            {formatTime(message.timestamp)}
          </div>
        </div>
        <div style={{
          width: 24, height: 24, borderRadius: "var(--radius-full)",
          background: "var(--bg-hover)", display: "flex", alignItems: "center",
          justifyContent: "center", fontSize: 12, flexShrink: 0,
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
        background: "linear-gradient(135deg, var(--accent), #5B21B6)",
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: 12, flexShrink: 0, marginTop: 1, color: "#fff",
      }}>
        🐼
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {isResume && (
          <div style={{ fontSize: 10, fontWeight: 500, color: "var(--accent-soft)", marginBottom: "var(--space-2)", display: "flex", alignItems: "center", gap: 4 }}>
            👌🏻 go on baby
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

        <div style={{ marginTop: "var(--space-1)", fontSize: 10, color: "var(--text-muted)" }}>
          {formatTime(message.timestamp)}
        </div>
        {/* 本轮对话消耗（后端 CostBudgetGuard.summary 精算，前端只展示不重算）；缺省则不显示 */}
        {message.usage && <UsageFooter usage={message.usage} />}
      </div>
    </div>
  );
}

/* ── 本轮消耗页脚：净费用 · tokens 明细（命中/未命中/新写 · 回复/推理）· 命中率 · 耗时 ── */
function UsageFooter({ usage: u }: { usage: ReplyUsage }) {
  return (
    <div
      style={{
        marginTop: "var(--space-2)", fontSize: 10, lineHeight: 1.7,
        color: "var(--text-tertiary)", display: "flex", flexWrap: "wrap",
        alignItems: "center", gap: "4px 12px",
        borderTop: "1px solid var(--border-subtle, rgba(127,127,127,0.15))",
        paddingTop: "var(--space-2)",
      }}
    >
      <span style={{ color: "var(--warning)", fontWeight: 600 }}
            title={`全价基线 ${fmtCost(u.full_cost_usd)}${u.saved_usd > 0 ? ` · 缓存省 ${fmtCost(u.saved_usd)}` : ""}`}>
        💰 {fmtCost(u.net_cost_usd)}
        {u.saved_usd > 0 && <span style={{ color: "var(--success)", fontWeight: 400 }}> （省{fmtCost(u.saved_usd)}）</span>}
      </span>
      <span title="输入 token：命中缓存 / 未命中 / 本次新写入缓存">
        ↑ {fmtTok(u.input_tokens)}
        <span style={{ color: "var(--text-muted)" }}>
          {" ("}命中{fmtTok(u.cached_tokens)} · 未命中{fmtTok(u.miss_tokens)}
          {u.cache_creation_tokens > 0 && <> · 新写{fmtTok(u.cache_creation_tokens)}</>}
          {")"}
        </span>
      </span>
      <span title="输出 token：llm 回复 / 推理">
        ↓ {fmtTok(u.output_tokens)}
        <span style={{ color: "var(--text-muted)" }}>
          {" ("}回复{fmtTok(u.reply_tokens)}
          {u.reasoning_tokens > 0 && <> · 推理{fmtTok(u.reasoning_tokens)}</>}
          {")"}
        </span>
      </span>
      <span title="prefix cache 命中率 = 命中 / 输入">🎯 {(u.hit_rate * 100).toFixed(1)}%</span>
      <span title="本轮耗时（墙钟）">⏱ {fmtDuration(u.duration_ms)}</span>
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
