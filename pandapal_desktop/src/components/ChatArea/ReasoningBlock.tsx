/**
 * src/components/ChatArea/ReasoningBlock.tsx — v2 重设计版
 *
 * 推理块：默认折叠在 200px 以内，内容超长时显示「展开 / 收起」。
 * 流式状态下自动滚到底部。
 */
import { useEffect, useRef, useState } from "react";

interface ReasoningBlockProps {
  tokens: string[];
  isStreaming: boolean;
  durationMs?: number;
}

const MAX_HEIGHT = 200;

function formatThinkDuration(ms?: number): string {
  if (ms == null) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
}

export function ReasoningBlock({ tokens, isStreaming, durationMs }: ReasoningBlockProps) {
  const text = tokens.join("");
  const textRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflow, setOverflow] = useState(false);
  // 用户是否贴着底部：只有贴底时才在流式期间自动滚底，
  // 一旦用户手动往上滚就停止争抢滚动位置（否则每个 token 都会把用户弹回底部 → 抖动）。
  const stickBottomRef = useRef(true);

  const handleScroll = () => {
    const el = textRef.current;
    if (!el) return;
    stickBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };

  // 检测是否超出 max-height
  useEffect(() => {
    const el = textRef.current;
    if (!el || isStreaming) return;
    setOverflow(el.scrollHeight > MAX_HEIGHT + 8);
  }, [text, isStreaming]);

  // 流式时滚到底部——仅当用户已贴底
  useEffect(() => {
    const el = textRef.current;
    if (!el || !isStreaming || !stickBottomRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [text, isStreaming]);

  // 完成时收起
  useEffect(() => {
    if (!isStreaming) setExpanded(false);
  }, [isStreaming]);

  return (
    <div className="reasoning-block">
      <div className="reasoning-title">
        {isStreaming && <span className="spinner" />}
        {isStreaming ? "思考中…" : "思考"}
        {!isStreaming && durationMs != null && (
          <span style={{ marginLeft: 6, fontSize: 10, color: "var(--text-muted)", fontWeight: 400 }}>
            {formatThinkDuration(durationMs)}
          </span>
        )}
      </div>
      <div
        ref={textRef}
        className={`reasoning-text${expanded ? " expanded" : ""}`}
        onScroll={handleScroll}
      >
        {text}
        {isStreaming && (
          <span style={{ animation: "cursor-blink 1s step-end infinite", color: "var(--accent-soft)", marginLeft: 1 }}>▌</span>
        )}
      </div>
      {!isStreaming && overflow && (
        <button
          type="button"
          className="reasoning-toggle"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "▲ 收起" : "▼ 展开全部"}
        </button>
      )}
    </div>
  );
}
