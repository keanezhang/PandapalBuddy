/**
 * src/components/ChatArea/SearchResultBlock.tsx — v2 重设计版
 *
 * 搜索结果卡片。纯 v2 Token。
 */
import React, { useState } from "react";

interface Props {
  resultSummary: string;
  collapsed: boolean;
}

export function SearchResultBlock({ resultSummary, collapsed }: Props) {
  const [expanded, setExpanded] = useState(!collapsed);

  if (!resultSummary) return null;

  return (
    <div style={{
      marginTop: "var(--space-2)",
      padding: "var(--space-3)",
      background: "var(--bg-elevated)",
      border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-md)",
      borderLeft: "2px solid #60A5FA",
    }}>
      <div
        onClick={() => collapsed && setExpanded(!expanded)}
        style={{
          display: "flex", alignItems: "center", gap: "var(--space-2)",
          cursor: collapsed ? "pointer" : "default",
          marginBottom: expanded ? "var(--space-2)" : 0,
        }}
      >
        <span style={{ fontSize: 12 }}>🔍</span>
        <span style={{ fontSize: 12, fontWeight: 600, color: "#60A5FA" }}>
          web_search 结果
        </span>
        {collapsed && (
          <span style={{ fontSize: 9, color: "var(--text-muted)", marginLeft: "auto" }}>
            {expanded ? "▾" : "▸"}
          </span>
        )}
      </div>
      {expanded && (
        <div style={{
          fontFamily: "var(--font-mono)", fontSize: 11, lineHeight: 1.6,
          color: "var(--text-secondary)", whiteSpace: "pre-wrap", wordBreak: "break-word",
          maxHeight: 200, overflowY: "auto",
        }}>
          {resultSummary}
        </div>
      )}
    </div>
  );
}
