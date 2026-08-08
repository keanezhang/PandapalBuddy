/**
 * src/App.tsx — 路由根组件
 *
 * 路由结构（BYOK）：
 * - /login          → 登录页
 * - /register       → 注册页
 * - /model-config   → 模型配置向导（首次/门禁拦截后）
 * - /               → ChatLayout（Sidebar + Outlet） [需要凭据 + 工作区]
 *   ├─ index        → ChatPage (聊天)
 *   ├─ skills       → SkillsPage (技能列表)
 *   ├─ skills/:skillName → SkillsPage (技能详情)
 *   ├─ tasks        → TasksPage (任务安排)
 *   ├─ groups/:groupId → SessionGroupPage (分组会话列表)
 * - /skills/new / /skills/:name/edit → SkillEditorPage (独立编辑器) [需要凭据]
 */

import { useEffect } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { LoginPage } from "./pages/LoginPage";
import { RegisterPage } from "./pages/RegisterPage";
import { ModelConfigWizard } from "./pages/ModelConfigWizard";
import { ChatPage } from "./pages/ChatPage";
import { SkillsPage } from "./pages/SkillsPage";
import { SkillEditorPage } from "./pages/SkillEditorPage";
import { TasksPage } from "./pages/TasksPage";
import { DashboardPage } from "./pages/DashboardPage";
import { SessionGroupPage } from "./pages/SessionGroupPage";
import { AuthGuard } from "./components/AuthGuard";
import { ChatLayout } from "./components/ChatLayout";
import { CommandPalette } from "./components/CommandPalette";
import { ToastHost } from "./components/ui";
import { useAuthStore } from "./store/authStore";
import "./styles/global-v2.css";

export default function App() {
  const initialize = useAuthStore((s) => s.initialize);
  const authStatus = useAuthStore((s) => s.status);

  useEffect(() => {
    initialize();
  }, [initialize]);

  return (
    <>
    {/* 全局命令面板（⌘K）与全局 Toast —— 仅登录后可用 */}
    {authStatus === "authenticated" && <CommandPalette />}
    {authStatus === "authenticated" && <ToastHost />}
    <Routes>
      {/* 公开路由 */}
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />

      {/* 模型配置向导（需要登录，但不需要凭据配置） */}
      <Route
        path="/model-config"
        element={
          <AuthGuard bypassCredentialCheck>
            <ModelConfigWizard />
          </AuthGuard>
        }
      />

      {/* === ChatLayout 父路由（Sidebar 持久）=== */}
      <Route
        element={
          <AuthGuard>
            <ChatLayout />
          </AuthGuard>
        }
      >
        <Route index element={<ChatPage />} />
        <Route path="skills" element={<SkillsPage />} />
        <Route path="skills/:skillName" element={<SkillsPage />} />
        <Route path="tasks" element={<TasksPage />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="groups/:groupId" element={<SessionGroupPage />} />
      </Route>

      {/* 独立页面（不走 ChatLayout） */}
      <Route
        path="/skills/new"
        element={
          <AuthGuard>
            <SkillEditorPage />
          </AuthGuard>
        }
      />
      <Route
        path="/skills/:skillName/edit"
        element={
          <AuthGuard>
            <SkillEditorPage />
          </AuthGuard>
        }
      />

      {/* 兜底 */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
    </>
  );
}
