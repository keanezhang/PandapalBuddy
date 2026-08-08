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
import { useAuthStore } from "../store/authStore";
import { AuthLayout, AuthError, AuthField, AuthFooter } from "../components/auth/AuthLayout";

export function RegisterPage() {
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
      setLocalError("请填写所有字段");
      return;
    }

    if (trimmedPass !== confirmPassword) {
      setLocalError("两次输入的密码不一致");
      return;
    }

    if (trimmedPass.length < 8) {
      setLocalError("密码长度不能少于 8 位");
      return;
    }

    if (trimmedUser.length < 3) {
      setLocalError("用户名长度不能少于 3 位");
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
    <AuthLayout subtitle="创建新账号">
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
        <AuthField label="用户名">
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="至少 3 个字符"
            className="auth-input"
            autoFocus
            disabled={loading}
          />
          <p className="auth-hint">
            💡 如需与企业微信/飞书数据互通，请使用对应平台的账号名作为用户名
          </p>
        </AuthField>

        <AuthField label="密码">
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="至少 8 位"
            className="auth-input"
            disabled={loading}
          />
        </AuthField>

        <AuthField label="确认密码">
          <input
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            placeholder="再次输入密码"
            className="auth-input"
            disabled={loading}
          />
        </AuthField>

        <button type="submit" className="auth-submit" disabled={loading}>
          {loading ? "注册中..." : "注 册"}
        </button>
      </form>

      {/* 底部链接 */}
      <AuthFooter text="已有账号？" linkTo="/login" linkText="返回登录" />
    </AuthLayout>
  );
}
