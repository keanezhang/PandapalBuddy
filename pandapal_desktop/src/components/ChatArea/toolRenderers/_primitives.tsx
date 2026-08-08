/**
 * src/components/ChatArea/toolRenderers/_primitives.tsx
 *
 * 工具渲染器共享积木：折叠卡 / diff / 代码预览 / 小工具函数。
 * 所有专属渲染器（Bash/Edit/Write/Read/Default）都基于这里的 CollapsibleCard。
 */
import React, { useState } from "react";
import type { ToolStatus } from "../../../store/chatStore";

// ── 状态视觉 ────────────────────────────────────────────────────────────────

const STATUS_META: Record<ToolStatus, { icon: string; color: string }> = {
  running: { icon: "⚙", color: "var(--accent-soft)" },
  done:    { icon: "✓", color: "var(--success)" },
  error:   { icon: "✗", color: "var(--danger)" },
  skipped: { icon: "—", color: "var(--text-muted)" },
};

export function statusMeta(status: ToolStatus) {
  return STATUS_META[status];
}

// ── 小工具函数 ──────────────────────────────────────────────────────────────

export function formatDuration(ms?: number | null): string {
  if (ms == null) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
}

/** 从 args 里挑一个「主参数」做表头摘要。 */
export function primaryArg(args?: Record<string, unknown>): string {
  if (!args) return "";
  const KEYS = ["command", "file_path", "path", "query", "pattern", "url", "name"];
  for (const k of KEYS) {
    const v = args[k];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  try {
    const json = JSON.stringify(args);
    return json.length > 80 ? json.slice(0, 80) + "…" : json;
  } catch {
    return "";
  }
}

export function baseName(p?: unknown): string {
  if (typeof p !== "string") return "";
  const parts = p.split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

export function lineCount(s?: unknown): number {
  if (typeof s !== "string" || s === "") return 0;
  return s.split("\n").length;
}

// ── CollapsibleCard：所有工具卡的统一外壳 ────────────────────────────────────

interface CardProps {
  icon: string;               // 工具图标
  name: string;               // 工具显示名
  summary?: string;           // 表头主摘要（灰色，中段）
  meta?: React.ReactNode;     // 表头右侧徽标（如 +53 -0 / 82 lines）
  status: ToolStatus;
  durationMs?: number | null;
  defaultExpanded?: boolean;
  children?: React.ReactNode; // 展开正文；无 children 时不显示展开箭头
}

export function CollapsibleCard({
  icon, name, summary, meta, status, durationMs, defaultExpanded = false, children,
}: CardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  // 后端若下发表外 status（旧数据/新版本协议），兜底而非让整棵树渲染崩溃
  const sm = STATUS_META[status] ?? { icon: "?", color: "var(--text-muted)" };
  const expandable = children != null && children !== false;
  const dur = formatDuration(durationMs);

  return (
    <div
      style={{
        marginTop: "var(--space-2)",
        border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-md)",
        background: "var(--bg-panel)",
        overflow: "hidden",
      }}
    >
      <div
        onClick={() => expandable && setExpanded((v) => !v)}
        style={{
          display: "flex", alignItems: "center", gap: "var(--space-2)",
          padding: "6px var(--space-3)",
          cursor: expandable ? "pointer" : "default",
          fontSize: "var(--text-12)", lineHeight: 1.4,
        }}
      >
        {status === "running" ? (
          <span style={{
            width: 10, height: 10, borderRadius: "50%",
            border: "2px solid rgba(167,139,250,0.25)",
            borderTopColor: "var(--accent-soft)",
            animation: "spin 0.8s linear infinite",
            display: "inline-block", flexShrink: 0,
          }} />
        ) : (
          <span style={{ color: sm.color, fontSize: "var(--text-11)", flexShrink: 0, width: 12, textAlign: "center" }}>{sm.icon}</span>
        )}
        <span style={{ flexShrink: 0 }}>{icon}</span>
        <span style={{ fontWeight: 600, color: "var(--text-primary)", flexShrink: 0 }}>{name}</span>
        {summary && (
          <span style={{
            color: "var(--text-tertiary)", overflow: "hidden",
            textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0,
            fontFamily: "var(--font-mono)", fontSize: "var(--text-11)",
          }}>
            {summary}
          </span>
        )}
        <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: "var(--space-2)", flexShrink: 0 }}>
          {meta}
          {dur && <span style={{ color: "var(--text-muted)", fontSize: "var(--text-10)" }}>{dur}</span>}
          {expandable && <span style={{ color: "var(--text-muted)", fontSize: "var(--text-10)" }}>{expanded ? "▾" : "▸"}</span>}
        </span>
      </div>

      {expandable && expanded && (
        <div style={{ borderTop: "1px solid var(--border-subtle)" }}>
          {children}
        </div>
      )}
    </div>
  );
}

// ── IOBlock：带标签的输入/输出块（Bash IN/OUT 等复用） ────────────────────────

export function IOBlock({ label, text, tone = "default", maxHeight = 280 }: {
  label: string;
  text: string;
  tone?: "default" | "error";
  maxHeight?: number;
}) {
  if (!text) return null;
  return (
    <div style={{ padding: "var(--space-2) var(--space-3)" }}>
      <div style={{
        fontSize: "var(--text-2xs)", fontWeight: 700, letterSpacing: 0.5,
        color: "var(--text-muted)", marginBottom: 4, fontFamily: "var(--font-mono)",
      }}>
        {label}
      </div>
      <pre style={{
        margin: 0, fontFamily: "var(--font-mono)", fontSize: "var(--text-11)", lineHeight: 1.55,
        color: tone === "error" ? "var(--danger)" : "var(--text-secondary)",
        whiteSpace: "pre-wrap", wordBreak: "break-word",
        maxHeight, overflowY: "auto",
      }}>
        {text}
      </pre>
    </div>
  );
}

// ── CodePreview：代码块预览（Write 用） ──────────────────────────────────────

export function CodePreview({ code, maxHeight = 320 }: { code: string; maxHeight?: number }) {
  return (
    <pre style={{
      margin: 0, padding: "var(--space-2) var(--space-3)",
      background: "var(--color-code-bg)", color: "var(--color-code-text)",
      fontFamily: "var(--font-mono)", fontSize: "var(--text-11)", lineHeight: 1.55,
      overflow: "auto", maxHeight, whiteSpace: "pre",
    }}>
      <code>{code}</code>
    </pre>
  );
}

// ── DiffView：轻量行级 diff（Edit 用，old_string → new_string） ───────────────

type DiffRow = { type: "ctx" | "del" | "add"; text: string };

function computeLineDiff(oldStr: string, newStr: string, context = 2): DiffRow[] {
  const a = oldStr.split("\n");
  const b = newStr.split("\n");
  // 公共前缀
  let p = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p++;
  // 公共后缀
  let s = 0;
  while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;

  const rows: DiffRow[] = [];
  const preStart = Math.max(0, p - context);
  for (let i = preStart; i < p; i++) rows.push({ type: "ctx", text: a[i] });
  for (let i = p; i < a.length - s; i++) rows.push({ type: "del", text: a[i] });
  for (let i = p; i < b.length - s; i++) rows.push({ type: "add", text: b[i] });
  const sufEnd = Math.min(a.length, a.length - s + context);
  for (let i = a.length - s; i < sufEnd; i++) rows.push({ type: "ctx", text: a[i] });
  return rows;
}

const DIFF_STYLE: Record<DiffRow["type"], React.CSSProperties> = {
  ctx: { color: "var(--text-tertiary)" },
  del: { color: "var(--diff-remove)", background: "color-mix(in srgb, var(--diff-remove) 10%, transparent)" },
  add: { color: "var(--diff-add)", background: "color-mix(in srgb, var(--diff-add) 10%, transparent)" },
};
const DIFF_SIGN: Record<DiffRow["type"], string> = { ctx: " ", del: "-", add: "+" };

export function DiffView({ oldStr, newStr, maxHeight = 320 }: {
  oldStr: string;
  newStr: string;
  maxHeight?: number;
}) {
  const rows = computeLineDiff(oldStr, newStr);
  return (
    <div style={{
      fontFamily: "var(--font-mono)", fontSize: "var(--text-11)", lineHeight: 1.5,
      overflow: "auto", maxHeight, background: "var(--color-code-bg)",
    }}>
      {rows.map((r, i) => (
        <div key={i} style={{ ...DIFF_STYLE[r.type], padding: "0 var(--space-3)", whiteSpace: "pre" }}>
          <span style={{ opacity: 0.5, marginRight: 8 }}>{DIFF_SIGN[r.type]}</span>
          {r.text || " "}
        </div>
      ))}
    </div>
  );
}

/** 统计 add/del 行数（表头徽标用）。 */
export function diffStat(oldStr: string, newStr: string): { add: number; del: number } {
  const rows = computeLineDiff(oldStr, newStr, 0);
  return {
    add: rows.filter((r) => r.type === "add").length,
    del: rows.filter((r) => r.type === "del").length,
  };
}
