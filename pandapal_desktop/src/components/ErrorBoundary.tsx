/**
 * src/components/ErrorBoundary.tsx
 *
 * 全局渲染错误兜底。
 *
 * 背景：应用此前没有任何 ErrorBoundary —— 任一组件在渲染期抛错，React 会
 * 卸载整棵组件树（TopToolbar / Sidebar 全是 React 渲染的），用户看到的就是
 * 「整窗黑屏、只能重启进程」，且崩溃现场无从读取。
 *
 * 此组件把渲染崩溃变成一张可读的错误页：
 * - 展示错误 message + stack + componentStack
 * - 一键重载（window.location.reload）
 * - 一键复制错误文本（反馈给开发）
 * - 崩溃记录落 localStorage（key: pandapal.lastCrash），即便用户直接重启，
 *   下次启动也能通过 reportLastCrash() 在 console 取回现场。
 *
 * 注意：只能捕获 React 渲染/生命周期错误；webview 进程级崩溃（OOM/GPU）
 * 不经过 JS，本组件无从感知。
 */
import React from "react";

const CRASH_KEY = "pandapal.lastCrash";

interface CrashRecord {
  at: string;
  message: string;
  stack: string;
  componentStack: string;
}

interface BoundaryState {
  error: Error | null;
  info: React.ErrorInfo | null;
}

export class ErrorBoundary extends React.Component<React.PropsWithChildren, BoundaryState> {
  state: BoundaryState = { error: null, info: null };

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error, info: null };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    this.setState({ info });
    console.error("[ErrorBoundary] render crash:", error, info.componentStack);
    try {
      const record: CrashRecord = {
        at: new Date().toISOString(),
        message: String(error),
        stack: error.stack ?? "",
        componentStack: info.componentStack ?? "",
      };
      localStorage.setItem(CRASH_KEY, JSON.stringify(record));
    } catch {
      /* localStorage 不可用时静默 */
    }
  }

  private handleReload = () => {
    try {
      localStorage.removeItem(CRASH_KEY);
    } catch {
      /* noop */
    }
    window.location.reload();
  };

  private handleCopy = () => {
    const { error, info } = this.state;
    const text = `${String(error)}\n\n${error?.stack ?? ""}\n\n组件栈:${info?.componentStack ?? ""}`;
    navigator.clipboard.writeText(text).catch(() => {});
  };

  render() {
    const { error, info } = this.state;
    if (!error) return this.props.children;

    return (
      <div style={{
        position: "fixed", inset: 0, zIndex: 99999,
        background: "var(--bg-root, #0F0F0F)",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: 24,
      }}>
        <div style={{
          maxWidth: 720, width: "100%",
          background: "var(--bg-panel, #151515)",
          border: "1px solid var(--border-strong, rgba(255,255,255,0.10))",
          borderRadius: 12,
          padding: 24,
          fontFamily: "var(--font-sans, system-ui, sans-serif)",
          color: "var(--text-primary, rgba(255,255,255,0.95))",
        }}>
          <div style={{ fontSize: "var(--text-lg)", fontWeight: 600, marginBottom: 8 }}>
            ⚠ 界面渲染出错（已拦截，无需重启进程）
          </div>
          <div style={{
            fontSize: "var(--text-sm)", lineHeight: 1.6, color: "var(--text-secondary, #a3a3ad)",
            marginBottom: 12,
          }}>
            请点击「重新加载」恢复界面，并把下面的错误信息发给开发排查根因。
          </div>
          <pre style={{
            margin: 0, marginBottom: 16, padding: 12,
            background: "var(--color-code-bg)", borderRadius: 8,
            fontFamily: "var(--font-mono, monospace)", fontSize: "var(--text-xs)", lineHeight: 1.55,
            color: "var(--diff-remove, #F87171)",
            whiteSpace: "pre-wrap", wordBreak: "break-word",
            maxHeight: "40vh", overflowY: "auto",
          }}>
            {String(error)}
            {error.stack ? `\n\n${error.stack}` : ""}
            {info?.componentStack ? `\n\n组件栈:${info.componentStack}` : ""}
          </pre>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button
              onClick={this.handleCopy}
              style={{
                padding: "6px 14px", fontSize: "var(--text-sm)", cursor: "pointer",
                border: "1px solid var(--border-subtle, rgba(255,255,255,0.05))", borderRadius: 6,
                background: "transparent", color: "var(--text-secondary, rgba(255,255,255,0.55))",
              }}
            >
              复制错误信息
            </button>
            <button
              onClick={this.handleReload}
              style={{
                padding: "6px 14px", fontSize: "var(--text-sm)", cursor: "pointer",
                border: "none", borderRadius: 6,
                background: "var(--accent, #7C3AED)", color: "var(--text-on-accent)", fontWeight: 600,
              }}
            >
              重新加载
            </button>
          </div>
        </div>
      </div>
    );
  }
}

/**
 * 启动时调用：若上一次会话发生过渲染崩溃，把记录打到 console（不打扰 UI）。
 * 记录保留到用户在下一次崩溃页点「重新加载」时才清除，避免重复启动覆盖现场。
 */
export function reportLastCrash() {
  try {
    const raw = localStorage.getItem(CRASH_KEY);
    if (raw) {
      console.warn("[ErrorBoundary] 上一次会话发生渲染崩溃：", JSON.parse(raw));
    }
  } catch {
    /* noop */
  }
}
