/**
 * src/components/TopToolbar.tsx — v2 重设计版
 *
 * 顶部工具栏：模式切换 + AI 状态 + 布局切换
 * （深度思考 / 模型选择 已移至输入框底部；连接状态仅保留在侧边栏）
 * 全部使用 var(--xxx) v2 Token
 */
import React from "react";
import { usePreferenceStore } from "../store/preferenceStore";
import { useIsStreaming } from "../store/chatStore";

/* ── 工具栏图标（Lucide 线性风格，继承按钮 currentColor） ─────────── */
const iconBase = {
  width: 17, height: 17, viewBox: "0 0 24 24", fill: "none",
  stroke: "currentColor", strokeWidth: 2,
  strokeLinecap: "round" as const, strokeLinejoin: "round" as const,
};

/** 侧边栏开关：左栏高亮的面板 */
function IconPanelLeft() {
  return (
    <svg {...iconBase} aria-hidden="true">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M9 3v18" />
    </svg>
  );
}

/** 文件查看器开关：右栏高亮的面板 */
function IconPanelRight() {
  return (
    <svg {...iconBase} aria-hidden="true">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M15 3v18" />
    </svg>
  );
}

/** 面板交换：左右互换双箭头 */
function IconSwap() {
  return (
    <svg {...iconBase} aria-hidden="true">
      <path d="M8 3 4 7l4 4" />
      <path d="M4 7h16" />
      <path d="m16 21 4-4-4-4" />
      <path d="M20 17H4" />
    </svg>
  );
}

export function TopToolbar() {
  const isStreaming = useIsStreaming();

  // 模式切换（办公助手 / 编码）已移至左侧边栏 ModeSwitcher
  const sidebarCollapsed = usePreferenceStore((s) => s.sidebarCollapsed);
  const toggleSidebar = usePreferenceStore((s) => s.toggleSidebar);
  const swapped = usePreferenceStore((s) => s.swapped);
  const swapPanels = usePreferenceStore((s) => s.swapPanels);
  const splitRatio = usePreferenceStore((s) => s.splitRatio);
  const toggleViewer = usePreferenceStore((s) => s.toggleViewer);
  const viewerVisible = splitRatio < 0.99;

  const toolbarBtnStyle: React.CSSProperties = {
    width: 30, height: 30, borderRadius: "var(--radius-sm)",
    display: "flex", alignItems: "center", justifyContent: "center",
    color: "var(--text-tertiary)", fontSize: 15,
    transition: "background var(--duration-fast), color var(--duration-fast)",
    cursor: "pointer",
  };

  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between",
      height: 48, padding: "0 var(--space-5)",
      background: "var(--bg-root)",
      borderBottom: "1px solid var(--border-subtle)",
      flexShrink: 0,
      WebkitAppRegion: "drag",
    } as React.CSSProperties}>
      {/* 左侧 */}
      <div style={{
        display: "flex", alignItems: "center", gap: "var(--space-3)",
        WebkitAppRegion: "no-drag",
      } as React.CSSProperties}>
        {/* AI 思考中 */}
        {isStreaming && (
          <div style={{
            display: "flex", alignItems: "center", gap: 4,
            fontSize: 11, color: "var(--accent-soft)",
            padding: "2px 8px", borderRadius: 9999,
            background: "rgba(124,58,237,0.10)",
          }}>
            <span style={{
              width: 12, height: 12, borderRadius: "50%",
              border: "2px solid rgba(124,58,237,0.2)",
              borderTopColor: "var(--accent-soft)",
              animation: "spin 0.8s linear infinite",
              display: "inline-block",
            }} />
            AI 思考中
          </div>
        )}
      </div>

      {/* 右侧 */}
      <div style={{
        display: "flex", alignItems: "center", gap: "var(--space-2)",
        WebkitAppRegion: "no-drag",
      } as React.CSSProperties}>
        {/* 侧边栏开关 */}
        <button
          type="button"
          onClick={toggleSidebar}
          title={sidebarCollapsed ? "显示侧边栏" : "隐藏侧边栏"}
          style={toolbarBtnStyle}
          onMouseEnter={(e) => { e.currentTarget.style.background = "var(--bg-elevated)"; e.currentTarget.style.color = "var(--text-primary)"; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--text-tertiary)"; }}
        >
          <IconPanelLeft />
        </button>

        {/* 文件查看器开关 */}
        <button
          type="button"
          onClick={toggleViewer}
          title={viewerVisible ? "隐藏查看器" : "显示查看器"}
          style={{
            ...toolbarBtnStyle,
            color: viewerVisible ? "var(--accent)" : "var(--text-tertiary)",
            background: viewerVisible ? "var(--bg-selected)" : "transparent",
          }}
          onMouseEnter={(e) => { if (!viewerVisible) { e.currentTarget.style.background = "var(--bg-elevated)"; e.currentTarget.style.color = "var(--text-primary)"; } }}
          onMouseLeave={(e) => { if (!viewerVisible) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--text-tertiary)"; } }}
        >
          <IconPanelRight />
        </button>

        {/* 面板交换 */}
        <button
          type="button"
          onClick={swapPanels}
          title="交换面板位置"
          style={{
            ...toolbarBtnStyle,
            color: swapped ? "var(--accent)" : "var(--text-tertiary)",
            background: swapped ? "var(--bg-selected)" : "transparent",
          }}
          onMouseEnter={(e) => { if (!swapped) { e.currentTarget.style.background = "var(--bg-elevated)"; e.currentTarget.style.color = "var(--text-primary)"; } }}
          onMouseLeave={(e) => { if (!swapped) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--text-tertiary)"; } }}
        >
          <IconSwap />
        </button>
      </div>
    </div>
  );
}
