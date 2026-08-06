/**
 * src/components/WorkspaceGate.tsx
 *
 * 工作区门控：登录之后、进入 Agent 之前的一道关卡。
 *
 * Agent 文件工具的根目录只能由用户显式选择（不做任何探测），因此在用户
 * 「打开一个文件夹」之前，sidecar 不启动、Agent 不可用，界面停在本组件。
 *
 * - current !== null              → 已打开工作区，渲染子组件（切换期间不回退）
 * - status === "opening"（首次）  → 显示「正在打开工作区」
 * - 其它（picking / error）       → 显示「打开文件夹」界面 + 最近打开列表
 */

import { useEffect } from "react";
import { useWorkspaceStore } from "../store/workspaceStore";

interface WorkspaceGateProps {
  children: React.ReactNode;
}

export function WorkspaceGate({ children }: WorkspaceGateProps) {
  const { current, status, recent, error, init, openWorkspace, pickAndOpen } =
    useWorkspaceStore();

  useEffect(() => {
    if (status === "idle") {
      void init();
    }
  }, [status, init]);

  // 已打开工作区：直接渲染（切换工作区时 current 保持非空，不闪回门控）
  if (current !== null) {
    return <>{children}</>;
  }

  if (status === "opening" || status === "idle") {
    return (
      <div style={styles.container}>
        <div style={styles.spinner}>🐼</div>
        <p style={styles.hint}>正在打开工作区...</p>
      </div>
    );
  }

  // picking / error：打开文件夹界面
  return (
    <div style={styles.container}>
      <div style={styles.logo}>🐼</div>
      <h1 style={styles.title}>打开一个文件夹</h1>
      <p style={styles.subtitle}>
        选择你要让 AI 处理的项目目录。AI 只会在这个目录内读写文件。
      </p>

      <button style={styles.primaryBtn} onClick={() => void pickAndOpen()}>
        选择文件夹…
      </button>

      {error && <p style={styles.error}>打开失败：{error}</p>}

      {recent.length > 0 && (
        <div style={styles.recentBox}>
          <p style={styles.recentTitle}>最近打开</p>
          <ul style={styles.recentList}>
            {recent.map((path) => (
              <li key={path}>
                <button
                  style={styles.recentItem}
                  title={path}
                  onClick={() => void openWorkspace(path)}
                >
                  <span style={styles.recentName}>{basename(path)}</span>
                  <span style={styles.recentPath}>{path}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/** 取路径最后一段作为显示名（兼容 / 与 \ 分隔符）。 */
function basename(path: string): string {
  const parts = path.split(/[/\\]/).filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : path;
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    height: "100vh",
    background: "var(--bg-page)",
    gap: 12,
    padding: 24,
    boxSizing: "border-box",
  },
  spinner: {
    fontSize: 48,
    animation: "thinking-pulse 1.2s ease-in-out infinite",
  },
  logo: { fontSize: 56, marginBottom: 4 },
  title: { fontSize: 22, fontWeight: 600, color: "var(--text-primary)", margin: 0 },
  subtitle: {
    fontSize: 14,
    color: "var(--text-secondary)",
    margin: "0 0 8px",
    maxWidth: 420,
    textAlign: "center",
    lineHeight: 1.6,
  },
  hint: { fontSize: 14, color: "var(--text-secondary)" },
  primaryBtn: {
    fontSize: 15,
    fontWeight: 600,
    color: "#fff",
    background: "var(--accent, #4f46e5)",
    border: "none",
    borderRadius: 10,
    padding: "12px 28px",
    cursor: "pointer",
  },
  error: { fontSize: 13, color: "var(--danger, #dc2626)", marginTop: 4 },
  recentBox: { marginTop: 20, width: "100%", maxWidth: 480 },
  recentTitle: {
    fontSize: 12,
    fontWeight: 600,
    color: "var(--text-secondary)",
    textTransform: "uppercase",
    letterSpacing: 0.5,
    marginBottom: 8,
  },
  recentList: { listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 4 },
  recentItem: {
    display: "flex",
    flexDirection: "column",
    alignItems: "flex-start",
    gap: 2,
    width: "100%",
    textAlign: "left",
    background: "var(--bg-elevated, rgba(127,127,127,0.06))",
    border: "1px solid var(--border-subtle, rgba(127,127,127,0.15))",
    borderRadius: 8,
    padding: "8px 12px",
    cursor: "pointer",
  },
  recentName: { fontSize: 14, fontWeight: 500, color: "var(--text-primary)" },
  recentPath: {
    fontSize: 12,
    color: "var(--text-secondary)",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    maxWidth: "100%",
  },
};
