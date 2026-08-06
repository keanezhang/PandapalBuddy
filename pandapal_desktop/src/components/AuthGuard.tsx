/**
 * src/components/AuthGuard.tsx
 *
 * 认证守卫：包裹需要登录的页面。
 *
 * 职责分层（依赖链匹配）：
 *   AuthGuard（登录门禁）
 *     └─ WorkspaceGate（工作区门控 → 触发 sidecar 启动 → backend-ready）
 *          └─ CredentialGate（凭据门禁 → 需要 sidecar 才能拉取 CREDENTIALS_LIST）
 *               └─ children
 *
 * 关键不变式：
 *   - CredentialGate 永远在 WorkspaceGate 内部渲染，因此当 CredentialGate
 *     被执行时，sidecar 已经就绪，credentialStore.loading 一定能被 CREDENTIALS_LIST
 *     置为 false，不会再发生「凭据门禁等待 sidecar、sidecar 等待工作区、工作区
 *     等待凭据门禁」的三元死锁。
 *
 * 门禁流程：
 *   1. status === "loading"          → 显示启动加载
 *   2. status === "unauthenticated"  → 重定向到 /login
 *   3. status === "authenticated"    → 进入 WorkspaceGate → CredentialGate → children
 */

import { Navigate } from "react-router-dom";
import { useAuthStore } from "../store/authStore";
import { WorkspaceGate } from "./WorkspaceGate";
import { CredentialGate } from "./CredentialGate";

interface AuthGuardProps {
  children: React.ReactNode;
  /** 绕过凭据门禁（用于向导页面自身，避免死循环） */
  bypassCredentialCheck?: boolean;
}

export function AuthGuard({ children, bypassCredentialCheck = false }: AuthGuardProps) {
  const status = useAuthStore((s) => s.status);

  // 1. 登录态加载中
  if (status === "loading") {
    return (
      <div style={styles.loadingContainer}>
        <div style={styles.spinner}>🐼</div>
        <p style={styles.loadingText}>正在启动...</p>
      </div>
    );
  }

  // 2. 未登录 → 跳登录页
  if (status === "unauthenticated") {
    return <Navigate to="/login" replace />;
  }

  // 3. 已登录 → 工作区门控（触发 sidecar 启动）→ 凭据门禁（消费 sidecar 数据）→ 子组件
  // loadCredentials 由 BackendProvider 在 backend-ready 后统一触发，门禁只消费已加载状态。
  return (
    <WorkspaceGate>
      <CredentialGate bypassCredentialCheck={bypassCredentialCheck}>
        {children}
      </CredentialGate>
    </WorkspaceGate>
  );
}

const styles: Record<string, React.CSSProperties> = {
  loadingContainer: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    height: "100vh",
    background: "var(--bg-page)",
    gap: 16,
  },
  spinner: {
    fontSize: 48,
    animation: "thinking-pulse 1.2s ease-in-out infinite",
  },
  loadingText: {
    fontSize: 14,
    color: "var(--text-secondary)",
  },
};
