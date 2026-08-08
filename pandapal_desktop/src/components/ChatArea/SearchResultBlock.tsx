/**
 * src/components/ChatArea/SearchResultBlock.tsx — v2 重设计版
 *
 * 搜索结果卡片。纯 v2 Token。
 */
import React, { useState } from "react";
import { useTranslation } from "react-i18next";

interface Props {
  resultSummary: string;
  collapsed: boolean;
}

export function SearchResultBlock({ resultSummary, collapsed }: Props) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(!collapsed);

  if (!resultSummary) return null;

  return (
    <div style={{
      marginTop: "var(--space-2)",
      padding: "var(--space-3)",
      background: "var(--bg-elevated)",
      border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-md)",
      borderLeft: "2px solid var(--info)",
    }}>
      <div
        onClick={() => collapsed && setExpanded(!expanded)}
        style={{
          display: "flex", alignItems: "center", gap: "var(--space-2)",
          cursor: collapsed ? "pointer" : "default",
          marginBottom: expanded ? "var(--space-2)" : 0,
        }}
      >
        <span style={{ fontSize: "var(--text-sm)" }}>🔍</span>
        <span style={{ fontSize: "var(--text-sm)", fontWeight: 600, color: "var(--info)" }}>
          {t("chat.searchResultTitle")}
        </span>
        {collapsed && (
          <span style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", marginLeft: "auto" }}>
            {expanded ? "▾" : "▸"}
          </span>
        )}
      </div>
      {expanded && (
        <div style={{
          fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", lineHeight: 1.6,
          color: "var(--text-secondary)", whiteSpace: "pre-wrap", wordBreak: "break-word",
          maxHeight: 200, overflowY: "auto",
        }}>
          {resultSummary}
        </div>
      )}
    </div>
  );
}
