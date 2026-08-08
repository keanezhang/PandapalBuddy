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
import { useTranslation } from "react-i18next";
import { useAuthStore } from "../store/authStore";
import { AuthLayout, AuthError, AuthField, AuthFooter } from "../components/auth/AuthLayout";

type TabMode = "local" | "cloud";

export function LoginPage() {
  const { t } = useTranslation();
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
        useAuthStore.setState({ error: t("auth.errPassTooShort6") });
        return;
      }
      if (localPassword !== confirmPassword) {
        clearError();
        useAuthStore.setState({ error: t("auth.errPassMismatch") });
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
            ? t("auth.subtitleLocalRegistered")
            : t("auth.subtitleLocalCreate")
          : t("auth.subtitleCloud")
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
          {t("auth.localMode")}
        </button>
        <button
          type="button"
          className={tab === "cloud" ? "auth-tab active" : "auth-tab"}
          onClick={() => {
            clearError();
            setTab("cloud");
          }}
        >
          {t("auth.cloudLogin")}
        </button>
      </div>

      {/* 错误提示 */}
      {error && <AuthError message={error} onClose={clearError} />}

      {/* 本地模式面板 */}
      {tab === "local" && (
        <form onSubmit={handleLocalSubmit} className="auth-form">
          <div className="auth-hint">
            {localRegistered
              ? t("auth.hintLocalRegistered")
              : t("auth.hintLocalCreate")}
          </div>

          <AuthField label={t("auth.username")}>
            <input
              type="text"
              value={localUsername}
              onChange={(e) => setLocalUsername(e.target.value)}
              placeholder={t("auth.placeholderUsername")}
              className="auth-input"
              autoFocus
              disabled={loading}
            />
          </AuthField>

          <AuthField label={t("auth.password")}>
            <input
              type="password"
              value={localPassword}
              onChange={(e) => setLocalPassword(e.target.value)}
              placeholder={t("auth.placeholderPassword")}
              className="auth-input"
              disabled={loading}
            />
          </AuthField>

          {localRegistered === false && (
            <AuthField label={t("auth.confirmPassword")}>
              <input
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                placeholder={t("auth.placeholderConfirm")}
                className="auth-input"
                disabled={loading}
              />
            </AuthField>
          )}

          <button type="submit" className="auth-submit" disabled={loading}>
            {loading
              ? t("auth.processing")
              : localRegistered === false
                ? t("auth.createLocalAccount")
                : t("auth.login")}
          </button>
        </form>
      )}

      {/* 云端登录面板 */}
      {tab === "cloud" && (
        <form onSubmit={handleCloudSubmit} className="auth-form">
          <AuthField label={t("auth.username")}>
            <input
              type="text"
              value={cloudUsername}
              onChange={(e) => setCloudUsername(e.target.value)}
              placeholder={t("auth.placeholderUsername")}
              className="auth-input"
              autoFocus
              disabled={loading}
            />
          </AuthField>

          <AuthField label={t("auth.password")}>
            <input
              type="password"
              value={cloudPassword}
              onChange={(e) => setCloudPassword(e.target.value)}
              placeholder={t("auth.placeholderPassword")}
              className="auth-input"
              disabled={loading}
            />
          </AuthField>

          <button type="submit" className="auth-submit" disabled={loading}>
            {loading ? t("auth.loggingIn") : t("auth.login")}
          </button>

          {/* 底部链接 */}
          <AuthFooter text={t("auth.noCloudAccount")} linkTo="/register" linkText={t("auth.registerNow")} />
        </form>
      )}
    </AuthLayout>
  );
}
