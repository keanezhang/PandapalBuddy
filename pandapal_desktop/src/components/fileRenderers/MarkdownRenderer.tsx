/**
 * src/components/fileRenderers/MarkdownRenderer.tsx
 *
 * Markdown 渲染器 — 顶部工具栏可在「预览 / 源码」之间切换：
 *   - 预览：marked 渲染为 HTML
 *   - 源码：委托 CodeRenderer（可编辑 Monaco；命中 AI 建议时为 inline diff）
 *
 * AI 建议模式（readOnly + original）无法预览 diff，强制走源码/diff 视图，
 * 并隐藏切换按钮，保证与 CodeRenderer 的 InlineDiffEditor 行为一致。
 */
import { useMemo, useState } from "react";
import { marked } from "marked";
import { CodeRenderer } from "../../monacoInlineDiff";

interface MarkdownRendererProps {
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

export function MarkdownRenderer(props: MarkdownRendererProps) {
  // AI 建议模式：readOnly 且带 original → 交给 CodeRenderer 走 diff
  const isSuggestion = !!props.readOnly && props.original != null;
  const [view, setView] = useState<"preview" | "source">("preview");
  const showPreview = view === "preview" && !isSuggestion;

  const html = useMemo(() => {
    if (!showPreview) return "";
    if (!props.content.trim()) return "";
    try {
      return marked.parse(props.content, { async: false }) as string;
    } catch {
      return "<p style='color:var(--danger)'>Markdown 解析错误</p>";
    }
  }, [showPreview, props.content]);

  const tabBtn = (mode: "preview" | "source", label: string) => (
    <button
      onClick={() => setView(mode)}
      style={{
        padding: "2px 10px", fontSize: 11, cursor: "pointer",
        border: "1px solid var(--border-subtle)", borderRadius: 4,
        background: view === mode ? "var(--bg-elevated)" : "transparent",
        color: view === mode ? "var(--accent)" : "var(--text-tertiary)",
        fontWeight: view === mode ? 600 : 400,
      }}
    >
      {label}
    </button>
  );

  return (
    <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      {!isSuggestion && (
        <div style={{
          display: "flex", alignItems: "center", gap: 6, padding: "4px 8px",
          background: "var(--bg-panel)", borderBottom: "1px solid var(--border-subtle)",
          flexShrink: 0,
        }}>
          {tabBtn("preview", "预览")}
          {tabBtn("source", "源码")}
        </div>
      )}
      {showPreview ? (
        <div
          className="markdown-preview"
          dangerouslySetInnerHTML={{ __html: html }}
          style={{
            flex: 1, overflowY: "auto", padding: "16px 20px",
            fontSize: 14, lineHeight: 1.75, color: "var(--text-primary)",
          }}
        />
      ) : (
        <CodeRenderer {...props} />
      )}
    </div>
  );
}
