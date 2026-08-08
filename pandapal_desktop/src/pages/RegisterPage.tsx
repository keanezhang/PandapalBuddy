/**
 * src/pages/RegisterPage.tsx
 *
 * 注册页面：用户名 + 密码 + 确认密码表单。
 * 注册成功后自动跳转到登录页。
 *
 * 布局与样式：components/auth/AuthLayout + global-v2.css SECTION 29（.auth-*）。
 */

import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useAuthStore } from "../store/authStore";
import { AuthLayout, AuthError, AuthField, AuthFooter } from "../components/auth/AuthLayout";

export function RegisterPage() {
  const { t } = useTranslation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  const { register, error, clearError } = useAuthStore();
  const navigate = useNavigate();

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setLocalError(null);
    clearError();

    const trimmedUser = username.trim();
    const trimmedPass = password;

    if (!trimmedUser || !trimmedPass) {
      setLocalError(t("auth.errFillAll"));
      return;
    }

    if (trimmedPass !== confirmPassword) {
      setLocalError(t("auth.errPassMismatch"));
      return;
    }

    if (trimmedPass.length < 8) {
      setLocalError(t("auth.errPassTooShort8"));
      return;
    }

    if (trimmedUser.length < 3) {
      setLocalError(t("auth.errUserTooShort3"));
      return;
    }

    setLoading(true);
    const success = await register(trimmedUser, trimmedPass);
    setLoading(false);

    if (success) {
      // 注册成功后已自动登录（Python 已签发 token + Phase 2 初始化）
      // 等待 systemReady 后 AuthGuard 会自动放行
      navigate("/", { replace: true });
    }
  };

  const displayError = localError || error;

  return (
    <AuthLayout subtitle={t("auth.subtitleRegister")}>
      {/* 错误提示 */}
      {displayError && (
        <AuthError
          message={displayError}
          onClose={() => {
            setLocalError(null);
            clearError();
          }}
        />
      )}

      {/* 表单 */}
      <form onSubmit={handleSubmit} className="auth-form">
        <AuthField label={t("auth.username")}>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder={t("auth.placeholderUserMin")}
            className="auth-input"
            autoFocus
            disabled={loading}
          />
          <p className="auth-hint">
            {t("auth.hintWecom")}
          </p>
        </AuthField>

        <AuthField label={t("auth.password")}>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={t("auth.placeholderPassMin")}
            className="auth-input"
            disabled={loading}
          />
        </AuthField>

        <AuthField label={t("auth.confirmPassword")}>
          <input
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            placeholder={t("auth.placeholderConfirmShort")}
            className="auth-input"
            disabled={loading}
          />
        </AuthField>

        <button type="submit" className="auth-submit" disabled={loading}>
          {loading ? t("auth.registering") : t("auth.register")}
        </button>
      </form>

      {/* 底部链接 */}
      <AuthFooter text={t("auth.haveAccount")} linkTo="/login" linkText={t("auth.backToLogin")} />
    </AuthLayout>
  );
}
