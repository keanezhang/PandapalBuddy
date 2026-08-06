/**
 * src/components/ChatArea/MessageList.tsx — v2 重设计版
 *
 * 消息列表。使用 per-session buffers（chatStore）。
 * 纯 v2 Token。
 */
import React, { useEffect, useRef, useState } from "react";
import {
  useCurrentMessages,
  useIsStreaming,
  type ChatMessage,
  type CompletedMessage,
  type StreamingMessage,
} from "../../store/chatStore";
import { useConnectionStore } from "../../store/connectionStore";
import { MessageBubble } from "./MessageBubble";
import { StreamingBubble } from "./StreamingBubble";

export function MessageList() {
  const messages: ChatMessage[] = useCurrentMessages();
  const status = useConnectionStore((s) => s.status);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [userAtBottom, setUserAtBottom] = useState(true);
  const rafId = useRef(0);

  const completed: CompletedMessage[] = messages.filter(
    (m): m is CompletedMessage => m.kind === "completed"
  );
  const streamings: StreamingMessage[] = messages.filter(
    (m): m is StreamingMessage => m.kind === "streaming"
  );

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

  useEffect(() => {
    if (!userAtBottom) return;
    cancelAnimationFrame(rafId.current);
    rafId.current = requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({
        top: scrollRef.current.scrollHeight,
        behavior: streamings.length > 0 ? "auto" : "smooth",
      });
    });
  }, [messages, userAtBottom, streamings.length]);

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
          <div style={{ fontSize: 40, marginBottom: "var(--space-4)" }}>🐼</div>
          <div style={{
            fontSize: "var(--text-lg)", fontWeight: 600,
            color: "var(--text-primary)", marginBottom: "var(--space-2)",
          }}>
            {isConnected ? "今天想聊点什么？" : "等待后端连接…"}
          </div>
          <div style={{ fontSize: 13, color: "var(--text-tertiary)" }}>
            {isConnected ? "输入消息开始对话" : "正在启动 PandaPal 引擎"}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div ref={scrollRef} style={{
      flex: 1, overflowY: "auto", padding: "var(--space-6) 0",
    }}>
      <div style={{ width: "100%", maxWidth: 1200, margin: "0 auto", padding: "0 var(--space-6)" }}>
        {/* key 用 kind 前缀 + 索引兜底：Plan/HITL 多轮会 reuse reply_id，
            同一 buffer 可能出现 id 相同的两条消息（已完成的一轮 + resume 的一轮），
            completed 与 streaming 又渲染在同一父节点，纯 id 做 key 会撞。
            对话消息是 append-only 不重排，索引稳定，可安全参与 key。 */}
        {completed.map((msg, i) => (
          <MessageBubble key={`c-${msg.id}-${i}`} message={msg} />
        ))}
        {streamings.map((msg, i) => (
          <StreamingBubble key={`s-${msg.id}-${i}`} message={msg} />
        ))}
      </div>
    </div>
  );
}
