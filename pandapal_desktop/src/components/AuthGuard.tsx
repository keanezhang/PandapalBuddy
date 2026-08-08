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
import { useTranslation } from "react-i18next";
import { useAuthStore } from "../store/authStore";
import { WorkspaceGate } from "./WorkspaceGate";
import { CredentialGate } from "./CredentialGate";
import { GateLoading } from "./ui";

interface AuthGuardProps {
  children: React.ReactNode;
  /** 绕过凭据门禁（用于向导页面自身，避免死循环） */
  bypassCredentialCheck?: boolean;
}

export function AuthGuard({ children, bypassCredentialCheck = false }: AuthGuardProps) {
  const { t } = useTranslation();
  const status = useAuthStore((s) => s.status);

  // 1. 登录态加载中
  if (status === "loading") {
    return <GateLoading text={t("auth.starting")} />;
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
