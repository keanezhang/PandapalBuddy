/**
 * src/components/fileRenderers/LogRenderer.tsx
 *
 * 日志渲染器 — 等宽字体 + 按级别着色 + 打开即滚动到底部。
 *
 * 大文件保护：仅渲染最后 MAX_LINES 行（日志通常只关心尾部），超出时顶部提示。
 * AI 建议模式（readOnly + original）委托 CodeRenderer 走 inline diff。
 */
import { useEffect, useMemo, useRef } from "react";
import { CodeRenderer } from "monaco-inline-diff-review";

interface LogRendererProps {
  content: string;
  language: string;
  original?: string;
  onChange?: (value: string) => void;
  readOnly?: boolean;
  fileId?: string;
  onAllResolved?: (savedContent: string) => void;
  onPartialSave?: (content: string, hunkKey: string) => void;
  initialAppliedKeys?: string[];
}

const MAX_LINES = 5000;

/** 日志级别 → 颜色（按优先级从高到低匹配） */
const LEVELS: { re: RegExp; color: string }[] = [
  { re: /\b(ERROR|FATAL|CRITICAL|EXCEPTION|TRACEBACK)\b/i, color: "var(--danger)" },
  { re: /\bWARN(ING)?\b/i, color: "var(--accent-2)" },
  { re: /\bDEBUG|TRACE\b/i, color: "var(--text-muted)" },
];

function lineColor(line: string): string {
  for (const l of LEVELS) if (l.re.test(line)) return l.color;
  return "var(--text-secondary)";
}

export function LogRenderer(props: LogRendererProps) {
  const isSuggestion = !!props.readOnly && props.original != null;
  const ref = useRef<HTMLDivElement>(null);

  const { lines, truncated, total } = useMemo(() => {
    if (isSuggestion) return { lines: [] as string[], truncated: false, total: 0 };
    const all = props.content.split("\n");
    if (all.length > MAX_LINES) {
      return { lines: all.slice(all.length - MAX_LINES), truncated: true, total: all.length };
    }
    return { lines: all, truncated: false, total: all.length };
  }, [isSuggestion, props.content]);

  // 打开 / 内容变更后滚动到底部（tail 行为）
  useEffect(() => {
    if (!isSuggestion && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [isSuggestion, props.content]);

  if (isSuggestion) return <CodeRenderer {...props} />;

  return (
    <div
      ref={ref}
      style={{
        flex: 1, overflow: "auto", padding: "8px 0",
        fontFamily: "'SF Mono','Cascadia Code','Fira Code',monospace",
        fontSize: 12, lineHeight: 1.5, background: "var(--bg-root)",
      }}
    >
      {truncated && (
        <div style={{
          padding: "4px 12px", fontSize: 11, color: "var(--text-muted)",
          borderBottom: "1px solid var(--border-subtle)", marginBottom: 4,
        }}>
          仅显示末尾 {MAX_LINES} 行（共 {total} 行）
        </div>
      )}
      {lines.map((line, i) => (
        <div
          key={i}
          style={{
            display: "flex", padding: "0 12px", whiteSpace: "pre-wrap",
            wordBreak: "break-all", color: lineColor(line),
          }}
        >
          <span style={{
            minWidth: 48, textAlign: "right", paddingRight: 12, userSelect: "none",
            color: "var(--text-muted)", opacity: 0.5, flexShrink: 0,
          }}>
            {truncated ? total - lines.length + i + 1 : i + 1}
          </span>
          <span style={{ flex: 1 }}>{line || " "}</span>
        </div>
      ))}
    </div>
  );
}
