/**
 * src/pages/RegisterPage.tsx
 *
 * 注册页面：用户名 + 密码 + 确认密码表单。
 * 注册成功后自动跳转到登录页。
 */

import { useState, type FormEvent } from "react";
import { useNavigate, Link } from "react-router-dom";
import { useAuthStore } from "../store/authStore";

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
    <div style={styles.container}>
      <div style={styles.card}>
        {/* Logo / 标题 */}
        <div style={styles.header}>
          <div style={styles.logo}>🐼</div>
          <h1 style={styles.title}>PandaPal</h1>
          <p style={styles.subtitle}>创建新账号</p>
        </div>

        {/* 错误提示 */}
        {displayError && (
          <div style={styles.errorBox}>
            <span style={styles.errorIcon}>⚠️</span>
            <span style={styles.errorText}>{displayError}</span>
            <button
              style={styles.errorClose}
              onClick={() => {
                setLocalError(null);
                clearError();
              }}
              aria-label="关闭"
            >
              ×
            </button>
          </div>
        )}

        {/* 表单 */}
        <form onSubmit={handleSubmit} style={styles.form}>
          <div style={styles.field}>
            <label style={styles.label}>用户名</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="至少 3 个字符"
              style={styles.input}
              autoFocus
              disabled={loading}
            />
            <p style={styles.hint}>
              💡 如需与企业微信/飞书数据互通，请使用对应平台的账号名作为用户名
            </p>
          </div>

          <div style={styles.field}>
            <label style={styles.label}>密码</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少 8 位"
              style={styles.input}
              disabled={loading}
            />
          </div>

          <div style={styles.field}>
            <label style={styles.label}>确认密码</label>
            <input
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder="再次输入密码"
              style={styles.input}
              disabled={loading}
            />
          </div>

          <button
            type="submit"
            style={{
              ...styles.submitBtn,
              opacity: loading ? 0.7 : 1,
              cursor: loading ? "not-allowed" : "pointer",
            }}
            disabled={loading}
          >
            {loading ? "注册中..." : "注 册"}
          </button>
        </form>

        {/* 底部链接 */}
        <div style={styles.footer}>
          <span style={styles.footerText}>已有账号？</span>
          <Link to="/login" style={styles.link}>
            返回登录
          </Link>
        </div>
      </div>
    </div>
  );
}

// ── 样式 ──────────────────────────────────────────────────────────────────────

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    height: "100vh",
    background: "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
    padding: 20,
  },
  card: {
    width: "100%",
    maxWidth: 400,
    background: "#ffffff",
    borderRadius: 16,
    padding: "40px 32px",
    boxShadow: "0 20px 60px rgba(0, 0, 0, 0.15)",
  },
  header: {
    textAlign: "center" as const,
    marginBottom: 32,
  },
  logo: {
    fontSize: 48,
    marginBottom: 8,
  },
  title: {
    fontSize: 24,
    fontWeight: 700,
    color: "#1e293b",
    margin: "0 0 4px 0",
  },
  subtitle: {
    fontSize: 14,
    color: "#64748b",
    margin: 0,
  },
  errorBox: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "10px 12px",
    background: "#fef2f2",
    border: "1px solid #fecaca",
    borderRadius: 8,
    marginBottom: 20,
  },
  errorIcon: {
    fontSize: 14,
    flexShrink: 0,
  },
  errorText: {
    flex: 1,
    fontSize: 13,
    color: "#dc2626",
  },
  errorClose: {
    background: "none",
    border: "none",
    fontSize: 18,
    color: "#dc2626",
    cursor: "pointer",
    padding: "0 4px",
    lineHeight: 1,
  },
  form: {
    display: "flex",
    flexDirection: "column" as const,
    gap: 18,
  },
  field: {
    display: "flex",
    flexDirection: "column" as const,
    gap: 6,
  },
  label: {
    fontSize: 13,
    fontWeight: 500,
    color: "#374151",
  },
  hint: {
    fontSize: 12,
    color: "#94a3b8",
    margin: "4px 0 0 0",
    lineHeight: 1.4,
  },
  input: {
    padding: "10px 14px",
    fontSize: 14,
    border: "1px solid #d1d5db",
    borderRadius: 8,
    outline: "none",
    transition: "border-color 0.2s, box-shadow 0.2s",
    width: "100%",
    boxSizing: "border-box" as const,
  },
  submitBtn: {
    marginTop: 8,
    padding: "12px 0",
    fontSize: 15,
    fontWeight: 600,
    color: "#ffffff",
    background: "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
    border: "none",
    borderRadius: 8,
    transition: "opacity 0.2s, transform 0.1s",
  },
  footer: {
    textAlign: "center" as const,
    marginTop: 24,
  },
  footerText: {
    fontSize: 13,
    color: "#64748b",
  },
  link: {
    fontSize: 13,
    color: "#6366f1",
    textDecoration: "none",
    fontWeight: 500,
    marginLeft: 4,
  },
};
