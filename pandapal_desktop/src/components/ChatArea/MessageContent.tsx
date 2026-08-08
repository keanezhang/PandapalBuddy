/**
 * src/components/ChatArea/MessageContent.tsx
 *
 * 助手正文渲染。
 * - 代码围栏 ``` → 自定义 CodeBlock（语言标签 + 复制按钮）
 * - 其余一切（标题/加粗/列表/表格/引用/行内代码/链接）→ marked 完整渲染
 *   经 DOMPurify 消毒后注入（LLM 输出不可信，必须防 XSS）。
 * 从 MessageBubble 抽离，供 Timeline 的 text 段与历史兜底共用。
 */
import React, { useMemo, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";

marked.setOptions({ gfm: true, breaks: false });

export function MessageContent({ content }: { content: string }) {
  const blocks = parseBlocks(content);
  return (
    <div style={{ fontSize: "var(--text-md)", lineHeight: 1.7, color: "var(--text-primary)", wordBreak: "break-word" }}>
      {blocks.map((block, i) =>
        block.type === "code" ? (
          <CodeBlock key={i} lang={block.lang} code={block.content} />
        ) : (
          <Markdown key={i} source={block.content} />
        ),
      )}
    </div>
  );
}

// ── 非代码段：marked → DOMPurify → 注入 ──────────────────────────────────────

function Markdown({ source }: { source: string }) {
  const html = useMemo(() => {
    if (!source.trim()) return "";
    try {
      const raw = marked.parse(source, { async: false }) as string;
      return DOMPurify.sanitize(raw);
    } catch {
      return "";
    }
  }, [source]);
  if (!html) return null;
  return (
    <div
      className="chat-md"
      onClick={handleLinkClick}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

/**
 * Tauri webview 里裸 <a> 点击会导航整个应用；一律拦截，走系统外部浏览器打开。
 */
function handleLinkClick(e: React.MouseEvent<HTMLDivElement>) {
  const anchor = (e.target as HTMLElement).closest("a");
  if (!anchor) return;
  const href = anchor.getAttribute("href");
  e.preventDefault();
  if (href && /^https?:\/\//i.test(href)) {
    window.open(href, "_blank", "noopener,noreferrer");
  }
}

// ── 代码块：语言标签 + 复制按钮 ──────────────────────────────────────────────

function CodeBlock({ lang, code }: { lang: string; code: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <div style={{ margin: "var(--space-3) 0", borderRadius: "var(--radius-md)", overflow: "hidden", border: "1px solid var(--border-default)" }}>
      <div style={{
        padding: "var(--space-2) var(--space-3)", background: "var(--bg-elevated)",
        borderBottom: "1px solid var(--border-default)",
        display: "flex", alignItems: "center", justifyContent: "space-between",
      }}>
        <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", fontFamily: "var(--font-mono)" }}>
          {lang || "code"}
        </span>
        <button onClick={handleCopy} style={{
          fontSize: "var(--text-2xs)", color: "var(--text-tertiary)", background: "none",
          border: "none", cursor: "pointer", padding: "2px 6px", borderRadius: 4,
          fontFamily: "inherit",
        }}>
          {copied ? "已复制 ✓" : "复制"}
        </button>
      </div>
      <pre style={{
        margin: 0, padding: "var(--space-3) var(--space-4)",
        background: "var(--color-code-bg)", color: "var(--color-code-text)",
        fontFamily: "var(--font-mono)", fontSize: "var(--text-sm)", lineHeight: 1.6,
        overflowX: "auto", whiteSpace: "pre",
      }}>
        <code>{code}</code>
      </pre>
    </div>
  );
}

// ── 分段：把代码围栏从正文中切出 ─────────────────────────────────────────────

type Block =
  | { type: "text"; content: string }
  | { type: "code"; lang: string; content: string };

function parseBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  const fenceRegex = /```(\w*)\n([\s\S]*?)```/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = fenceRegex.exec(text)) !== null) {
    if (match.index > cursor) blocks.push({ type: "text", content: text.slice(cursor, match.index) });
    blocks.push({ type: "code", lang: match[1] || "", content: match[2] });
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) blocks.push({ type: "text", content: text.slice(cursor) });
  return blocks.length > 0 ? blocks : [{ type: "text", content: text }];
}
