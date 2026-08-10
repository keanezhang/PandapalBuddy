/**
 * src/components/ChatArea/MessageList.tsx — v3 虚拟滚动版
 *
 * 消息列表。使用 per-session buffers（chatStore）。
 * 长会话（数百上千条）只渲染可视区附近的行（@tanstack/react-virtual），
 * 配合 MessageBubble 的 memo：流式输出时历史气泡既不重渲染、也不在 DOM 中。
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  useCurrentMessages,
  type ChatMessage,
  type CompletedMessage,
  type StreamingMessage,
} from "../../store/chatStore";
import { useConnectionStore } from "../../store/connectionStore";
import { MessageBubble } from "./MessageBubble";
import { StreamingBubble } from "./StreamingBubble";

/** 初始高度估算：真实高度由 measureElement 动态校正，估算只影响滚动条初值。 */
const ESTIMATE_ROW_HEIGHT = 120;

export function MessageList() {
  const { t } = useTranslation();
  const messages: ChatMessage[] = useCurrentMessages();
  const status = useConnectionStore((s) => s.status);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [userAtBottom, setUserAtBottom] = useState(true);
  const rafId = useRef(0);

  // 渲染行：completed 在前、streaming 在后（与旧实现视觉顺序一致）。
  const rows = useMemo(() => {
    const completed: CompletedMessage[] = [];
    const streamings: StreamingMessage[] = [];
    for (const m of messages) {
      if (m.kind === "completed") completed.push(m);
      else streamings.push(m);
    }
    return [...completed, ...streamings];
  }, [messages]);

  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ESTIMATE_ROW_HEIGHT,
    overscan: 8,
    // key 沿用旧实现：kind 前缀 + id + 索引兜底（Plan/HITL 多轮会 reuse reply_id）。
    getItemKey: (index) => {
      const m = rows[index];
      return m ? `${m.kind}-${m.id}-${index}` : String(index);
    },
  });

  // 用户是否贴在底部：贴底时新内容自动跟随；手动上翻后停止争抢滚动位置。
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const d = el.scrollHeight - el.scrollTop - el.clientHeight;
      setUserAtBottom(d < 80);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  const hasStreaming = rows.some((r) => r.kind === "streaming");

  useEffect(() => {
    if (!userAtBottom || rows.length === 0) return;
    cancelAnimationFrame(rafId.current);
    rafId.current = requestAnimationFrame(() => {
      rowVirtualizer.scrollToIndex(rows.length - 1, {
        align: "end",
        behavior: hasStreaming ? "auto" : "smooth",
      });
    });
  }, [messages, userAtBottom, hasStreaming, rowVirtualizer]);

  const isEmpty = messages.length === 0;
  const isConnected = status === "connected";

  if (isEmpty) {
    return (
      <div ref={scrollRef} style={{
        flex: 1, overflowY: "auto", display: "flex",
        alignItems: "center", justifyContent: "center",
        padding: "var(--space-6)",
      }}>
        <div style={{ textAlign: "center" }}>
          <div style={{ fontSize: "var(--icon-empty)", marginBottom: "var(--space-4)" }}>🐼</div>
          <div style={{
            fontSize: "var(--text-lg)", fontWeight: 600,
            color: "var(--text-primary)", marginBottom: "var(--space-2)",
          }}>
            {isConnected ? t("chat.emptyTitleConnected") : t("chat.emptyTitleDisconnected")}
          </div>
          <div style={{ fontSize: "var(--text-base)", color: "var(--text-tertiary)" }}>
            {isConnected ? t("chat.emptyHintConnected") : t("chat.emptyHintDisconnected")}
          </div>
        </div>
      </div>
    );
  }

  const virtualItems = rowVirtualizer.getVirtualItems();

  return (
    <div ref={scrollRef} style={{
      flex: 1, overflowY: "auto", padding: "var(--space-6) 0",
    }}>
      <div style={{
        height: rowVirtualizer.getTotalSize(), width: "100%", position: "relative",
      }}>
        {virtualItems.map((virtualRow) => {
          const msg = rows[virtualRow.index];
          return (
            <div
              key={virtualRow.key}
              data-index={virtualRow.index}
              ref={rowVirtualizer.measureElement}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                transform: `translateY(${virtualRow.start}px)`,
              }}
            >
              <div style={{ width: "100%", maxWidth: 1200, margin: "0 auto", padding: "0 var(--space-6)" }}>
                {msg.kind === "completed" ? (
                  <MessageBubble message={msg} />
                ) : (
                  <StreamingBubble message={msg} />
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
