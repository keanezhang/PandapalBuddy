/**
 * src/components/fileRenderers/HtmlRenderer.tsx
 *
 * HTML 渲染器 — 顶部工具栏在「预览 / 源码」间切换：
 *   - 预览：<iframe srcDoc> 渲染整页（沙箱内允许脚本，支持自包含页面 / WebGL PPT）
 *   - 源码：委托 CodeRenderer（可编辑 Monaco；命中 AI 建议时为 inline diff）
 *
 * AI 建议模式（readOnly + original）无法预览 diff，强制走源码/diff 视图并隐藏切换。
 *
 * 说明：srcDoc 预览无独立 origin，相对路径引用的外部资源（图片/脚本）不会解析；
 *       对内联自包含的 HTML（本项目 PPT skill 产物即是）完全适用。
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { CodeRenderer } from "../../monacoInlineDiff";

interface HtmlRendererProps {
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

export function HtmlRenderer(props: HtmlRendererProps) {
  const { t } = useTranslation();
  const isSuggestion = !!props.readOnly && props.original != null;
  const [view, setView] = useState<"preview" | "source">("preview");
  const showPreview = view === "preview" && !isSuggestion;

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
          {tabBtn("preview", t("fileRenderers.preview"))}
          {tabBtn("source", t("fileRenderers.source"))}
        </div>
      )}
      {showPreview ? (
        <iframe
          title="html-preview"
          srcDoc={props.content}
          sandbox="allow-scripts allow-same-origin allow-popups allow-modals"
          style={{ flex: 1, width: "100%", height: "100%", border: "none", background: "#fff" }}
        />
      ) : (
        <CodeRenderer {...props} />
      )}
    </div>
  );
}
