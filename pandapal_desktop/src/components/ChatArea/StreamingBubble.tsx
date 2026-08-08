/**
 * src/components/ChatArea/StreamingBubble.tsx — 时间线版
 *
 * 流式输出气泡。遍历 message.timeline 渲染交错的思考/文本/工具。
 */
import React, { memo } from "react";
import type { StreamingMessage } from "../../store/chatStore";
import { Timeline } from "./Timeline";

export const StreamingBubble = memo(function StreamingBubble({ message }: { message: StreamingMessage }) {
  const { timeline, toolCalls, replyScope } = message;
  const isResume = replyScope === "hitl_resume";
  const empty = timeline.length === 0;

  return (
    <div style={{
      display: "flex", gap: "var(--space-3)",
      padding: "var(--space-3) var(--space-4)",
      borderRadius: "var(--radius-md)",
      marginBottom: "var(--space-1)",
    }}>
      <div style={{
        width: 24, height: 24, borderRadius: "var(--radius-full)",
        background: "var(--gradient-avatar)",
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: "var(--text-sm)", flexShrink: 0, marginTop: 1, color: "var(--text-on-accent)",
      }}>
        🐼
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {isResume && (
          <div style={{ fontSize: "var(--text-2xs)", fontWeight: 500, color: "var(--accent-soft)", marginBottom: "var(--space-2)", display: "flex", alignItems: "center", gap: 4 }}>
            👌🏻 go on baby
          </div>
        )}

        <Timeline items={timeline} toolCalls={toolCalls} isStreaming={true} />

        {empty && (
          <div style={{ color: "var(--text-muted)", fontSize: "var(--text-md)" }}>
            <span style={{ animation: "cursor-blink 1s step-end infinite", color: "var(--accent-soft)", fontWeight: "bold" }}>▌</span>
          </div>
        )}
      </div>
    </div>
  );
});
