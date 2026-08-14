/**
 * src/components/ChatArea/MessageList.tsx — v3 虚拟滚动版
 *
 * 消息列表。使用 per-session buffers（chatStore）。
 * 长会话（数百上千条）只渲染可视区附近的行（@tanstack/react-virtual），
 * 配合 MessageBubble 的 memo：流式输出时历史气泡既不重渲染、也不在 DOM 中。
 * 支持向上滚动触底加载更早历史（SESSION_HISTORY_REQUEST offset 分页）。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  useChatStore,
  useCurrentMessages,
  type ChatMessage,
  type CompletedMessage,
  type StreamingMessage,
} from "../../store/chatStore";
import { useSessionStore } from "../../store/sessionStore";
import { useConnectionStore } from "../../store/connectionStore";
import { useBackend } from "../../providers/BackendProvider";
import { MessageBubble } from "./MessageBubble";
import { StreamingBubble } from "./StreamingBubble";

/** 初始高度估算：真实高度由 measureElement 动态校正，估算只影响滚动条初值。 */
const ESTIMATE_ROW_HEIGHT = 120;
/** 每页加载的历史条数。 */
const HISTORY_PAGE_SIZE = 50;
/** 距顶部该像素内触发向上翻页。 */
const LOAD_MORE_THRESHOLD = 100;

export function MessageList() {
  const { t } = useTranslation();
  const messages: ChatMessage[] = useCurrentMessages();
  const status = useConnectionStore((s) => s.status);
  const sessionId = useSessionStore((s) => s.currentSessionId);
  const historyOffset = useChatStore((s) =>
    sessionId ? (s.buffers.get(sessionId)?.historyOffset ?? 0) : 0,
  );
  const hasMoreHistory = useChatStore((s) =>
    sessionId ? (s.buffers.get(sessionId)?.hasMoreHistory ?? false) : false,
  );
  const { requestSessionHistory } = useBackend();

  const scrollRef = useRef<HTMLDivElement>(null);
  const [userAtBottom, setUserAtBottom] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(false);
  const historyLoadingRef = useRef(false);
  const pendingAnchorIdRef = useRef<string | null>(null);
  const loadTimerRef = useRef<number>(0);
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

  // 用 ref 存最新首条消息 id，供滚动回调读取（避免闭包过期）。
  const firstRowIdRef = useRef<string | null>(null);
  firstRowIdRef.current = rows[0]?.id ?? null;

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

  // 会话切换时重置分页锁与锚点。
  useEffect(() => {
    historyLoadingRef.current = false;
    setHistoryLoading(false);
    pendingAnchorIdRef.current = null;
    if (loadTimerRef.current) window.clearTimeout(loadTimerRef.current);
    loadTimerRef.current = 0;
  }, [sessionId]);

  // 向上翻页：滚到顶部附近 + 还有更早历史 + 未在加载中。
  const loadMoreHistory = useCallback(() => {
    if (!sessionId || !hasMoreHistory || historyLoadingRef.current || historyOffset <= 0) {
      return;
    }
    historyLoadingRef.current = true;
    setHistoryLoading(true);
    // prepend 前记录锚点（当前第一条可见消息），完成后 scrollToIndex 回到原位。
    pendingAnchorIdRef.current = firstRowIdRef.current;
    requestSessionHistory(sessionId, HISTORY_PAGE_SIZE, historyOffset);
    // 兜底：5s 后无论成败都释放锁，避免网络异常卡死。
    loadTimerRef.current = window.setTimeout(() => {
      historyLoadingRef.current = false;
      setHistoryLoading(false);
    }, 5000);
  }, [sessionId, hasMoreHistory, historyOffset, requestSessionHistory]);

  // prepend 完成后恢复视口到锚点消息（防跳变），并释放加载锁。
  useEffect(() => {
    const anchorId = pendingAnchorIdRef.current;
    if (anchorId === null) return;
    pendingAnchorIdRef.current = null;
    const idx = rows.findIndex((m) => m.id === anchorId);
    if (idx >= 0) {
      rowVirtualizer.scrollToIndex(idx, { align: "start" });
    }
    historyLoadingRef.current = false;
    setHistoryLoading(false);
    if (loadTimerRef.current) window.clearTimeout(loadTimerRef.current);
    loadTimerRef.current = 0;
    // rows 由 messages 派生，仅需监听 messages；rowVirtualizer 为稳定句柄。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages]);

  // 用户是否贴在底部：贴底时新内容自动跟随；手动上翻后停止争抢滚动位置。
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const d = el.scrollHeight - el.scrollTop - el.clientHeight;
      setUserAtBottom(d < 80);
      if (el.scrollTop <= LOAD_MORE_THRESHOLD) loadMoreHistory();
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [loadMoreHistory]);

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
  const showTopHint = historyLoading || (!hasMoreHistory && historyOffset > 0);

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
      {showTopHint && (
        <div style={{
          textAlign: "center", padding: "var(--space-2) 0",
          color: "var(--text-tertiary)", fontSize: "var(--text-xs)",
        }}>
          {historyLoading ? "正在加载更早的消息…" : "已到最早消息"}
        </div>
      )}
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
