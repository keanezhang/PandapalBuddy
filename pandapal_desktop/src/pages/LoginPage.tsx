/**
 * src/pages/LoginPage.tsx
 *
 * 登录页面：本地模式（默认） + 云端登录（次要 Tab）。
 *
 * 本地模式：
 *   - 未注册本地账号 → 显示「创建本地账号」表单（注册即登录，完全离线）
 *   - 已注册本地账号 → 显示「本地登录」表单（bcrypt 本地校验，不连服务器）
 * 云端登录：原 relay HTTP 认证流程，保留「立即注册」入口。
 *
 * 布局与样式：components/auth/AuthLayout + global-v2.css SECTION 29（.auth-*）。
 */

import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "../store/authStore";
import { AuthLayout, AuthError, AuthField, AuthFooter } from "../components/auth/AuthLayout";

type TabMode = "local" | "cloud";

export function LoginPage() {
  const [tab, setTab] = useState<TabMode>("local");
  // null = 正在查询本地账号状态（避免闪烁创建/登录表单）
  const [localRegistered, setLocalRegistered] = useState<boolean | null>(null);
  const [localUsername, setLocalUsername] = useState("");
  const [localPassword, setLocalPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [cloudUsername, setCloudUsername] = useState("");
  const [cloudPassword, setCloudPassword] = useState("");
  const [loading, setLoading] = useState(false);

  const { login, registerLocal, loginLocal, fetchLocalStatus, error, clearError } =
    useAuthStore();
  const navigate = useNavigate();

  // 挂载时查询本地账号是否已注册
  useEffect(() => {
    fetchLocalStatus().then((status) => {
      setLocalRegistered(status.registered);
      if (status.registered) {
        setLocalUsername(status.username);
      }
    });
  }, [fetchLocalStatus]);

  const goHome = () => navigate("/", { replace: true });

  /** 本地模式提交：未注册→创建；已注册→登录 */
  const handleLocalSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!localUsername.trim() || !localPassword.trim()) return;

    if (localRegistered === false) {
      // 创建本地账号
      if (localPassword.length < 6) {
        clearError();
        useAuthStore.setState({ error: "密码至少需要 6 位" });
        return;
      }
      if (localPassword !== confirmPassword) {
        clearError();
        useAuthStore.setState({ error: "两次输入的密码不一致" });
        return;
      }
    }

    setLoading(true);
    const success = localRegistered
      ? await loginLocal(localUsername.trim(), localPassword)
      : await registerLocal(localUsername.trim(), localPassword);
    setLoading(false);

    if (success) goHome();
  };

  /** 云端模式提交：走 relay HTTP 登录 */
  const handleCloudSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!cloudUsername.trim() || !cloudPassword.trim()) return;

    setLoading(true);
    const success = await login(cloudUsername.trim(), cloudPassword);
    setLoading(false);

    if (success) goHome();
  };

  return (
    <AuthLayout
      subtitle={
        tab === "local"
          ? localRegistered
            ? "本地账号登录"
            : "创建你的本地账号"
          : "登录到你的云端账号"
      }
    >
      {/* 模式 Tab */}
      <div className="auth-tabs">
        <button
          type="button"
          className={tab === "local" ? "auth-tab active" : "auth-tab"}
          onClick={() => {
            clearError();
            setTab("local");
          }}
        >
          本地模式
        </button>
        <button
          type="button"
          className={tab === "cloud" ? "auth-tab active" : "auth-tab"}
          onClick={() => {
            clearError();
            setTab("cloud");
          }}
        >
          云端登录
        </button>
      </div>

      {/* 错误提示 */}
      {error && <AuthError message={error} onClose={clearError} />}

      {/* 本地模式面板 */}
      {tab === "local" && (
        <form onSubmit={handleLocalSubmit} className="auth-form">
          <div className="auth-hint">
            {localRegistered
              ? "已配置本地账号，密码校验在本地完成，完全离线可用。"
              : "本地账号存储在设备上（密码加密保存），无需服务器即可使用 PandaPal。"}
          </div>

          <AuthField label="用户名">
            <input
              type="text"
              value={localUsername}
              onChange={(e) => setLocalUsername(e.target.value)}
              placeholder="请输入用户名"
              className="auth-input"
              autoFocus
              disabled={loading}
            />
          </AuthField>

          <AuthField label="密码">
            <input
              type="password"
              value={localPassword}
              onChange={(e) => setLocalPassword(e.target.value)}
              placeholder="请输入密码"
              className="auth-input"
              disabled={loading}
            />
          </AuthField>

          {localRegistered === false && (
            <AuthField label="确认密码">
              <input
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder="请再次输入密码"
                className="auth-input"
                disabled={loading}
              />
            </AuthField>
          )}

          <button type="submit" className="auth-submit" disabled={loading}>
            {loading
              ? "处理中..."
              : localRegistered === false
                ? "创建本地账号"
                : "登 录"}
          </button>
        </form>
      )}

      {/* 云端登录面板 */}
      {tab === "cloud" && (
        <form onSubmit={handleCloudSubmit} className="auth-form">
          <AuthField label="用户名">
            <input
              type="text"
              value={cloudUsername}
              onChange={(e) => setCloudUsername(e.target.value)}
              placeholder="请输入用户名"
              className="auth-input"
              autoFocus
              disabled={loading}
            />
          </AuthField>

          <AuthField label="密码">
            <input
              type="password"
              value={cloudPassword}
              onChange={(e) => setCloudPassword(e.target.value)}
              placeholder="请输入密码"
              className="auth-input"
              disabled={loading}
            />
          </AuthField>

          <button type="submit" className="auth-submit" disabled={loading}>
            {loading ? "登录中..." : "登 录"}
          </button>

          {/* 底部链接 */}
          <AuthFooter text="还没有云端账号？" linkTo="/register" linkText="立即注册" />
        </form>
      )}
    </AuthLayout>
  );
}
