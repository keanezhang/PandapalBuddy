/**
 * src/pages/LoginPage.tsx
 *
 * 登录页面：本地模式（默认） + 云端登录（次要 Tab）。
 *
 * 本地模式：
 *   - 未注册本地账号 → 显示「创建本地账号」表单（注册即登录，完全离线）
 *   - 已注册本地账号 → 显示「本地登录」表单（bcrypt 本地校验，不连服务器）
 * 云端登录：原 relay HTTP 认证流程，保留「立即注册」入口。
 */

import { useEffect, useState, type FormEvent } from "react";
import { useNavigate, Link } from "react-router-dom";
import { useAuthStore } from "../store/authStore";

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
    <div style={styles.container}>
      <div style={styles.card}>
        {/* Logo / 标题 */}
        <div style={styles.header}>
          <div style={styles.logo}>🐼</div>
          <h1 style={styles.title}>PandaPal</h1>
          <p style={styles.subtitle}>
            {tab === "local"
              ? localRegistered
                ? "本地账号登录"
                : "创建你的本地账号"
              : "登录到你的云端账号"}
          </p>
        </div>

        {/* 模式 Tab */}
        <div style={styles.tabs}>
          <button
            type="button"
            style={{
              ...styles.tab,
              ...(tab === "local" ? styles.tabActive : {}),
            }}
            onClick={() => {
              clearError();
              setTab("local");
            }}
          >
            本地模式
          </button>
          <button
            type="button"
            style={{
              ...styles.tab,
              ...(tab === "cloud" ? styles.tabActive : {}),
            }}
            onClick={() => {
              clearError();
              setTab("cloud");
            }}
          >
            云端登录
          </button>
        </div>

        {/* 错误提示 */}
        {error && (
          <div style={styles.errorBox}>
            <span style={styles.errorIcon}>⚠️</span>
            <span style={styles.errorText}>{error}</span>
            <button
              style={styles.errorClose}
              onClick={clearError}
              aria-label="关闭"
            >
              ×
            </button>
          </div>
        )}

        {/* 本地模式面板 */}
        {tab === "local" && (
          <form onSubmit={handleLocalSubmit} style={styles.form}>
            <div style={styles.hint}>
              {localRegistered
                ? "已配置本地账号，密码校验在本地完成，完全离线可用。"
                : "本地账号存储在设备上（密码加密保存），无需服务器即可使用 PandaPal。"}
            </div>

            <div style={styles.field}>
              <label style={styles.label}>用户名</label>
              <input
                type="text"
                value={localUsername}
                onChange={(e) => setLocalUsername(e.target.value)}
                placeholder="请输入用户名"
                style={styles.input}
                autoFocus
                disabled={loading}
              />
            </div>

            <div style={styles.field}>
              <label style={styles.label}>密码</label>
              <input
                type="password"
                value={localPassword}
                onChange={(e) => setLocalPassword(e.target.value)}
                placeholder="请输入密码"
                style={styles.input}
                disabled={loading}
              />
            </div>

            {localRegistered === false && (
              <div style={styles.field}>
                <label style={styles.label}>确认密码</label>
                <input
                  type="password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  placeholder="请再次输入密码"
                  style={styles.input}
                  disabled={loading}
                />
              </div>
            )}

            <button
              type="submit"
              style={{
                ...styles.submitBtn,
                opacity: loading ? 0.7 : 1,
                cursor: loading ? "not-allowed" : "pointer",
              }}
              disabled={loading}
            >
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
          <form onSubmit={handleCloudSubmit} style={styles.form}>
            <div style={styles.field}>
              <label style={styles.label}>用户名</label>
              <input
                type="text"
                value={cloudUsername}
                onChange={(e) => setCloudUsername(e.target.value)}
                placeholder="请输入用户名"
                style={styles.input}
                autoFocus
                disabled={loading}
              />
            </div>

            <div style={styles.field}>
              <label style={styles.label}>密码</label>
              <input
                type="password"
                value={cloudPassword}
                onChange={(e) => setCloudPassword(e.target.value)}
                placeholder="请输入密码"
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
              {loading ? "登录中..." : "登 录"}
            </button>

            {/* 底部链接 */}
            <div style={styles.footer}>
              <span style={styles.footerText}>还没有云端账号？</span>
              <Link to="/register" style={styles.link}>
                立即注册
              </Link>
            </div>
          </form>
        )}
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
    marginBottom: 20,
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
  tabs: {
    display: "flex",
    gap: 8,
    marginBottom: 20,
    background: "#f1f5f9",
    borderRadius: 10,
    padding: 4,
  },
  tab: {
    flex: 1,
    padding: "8px 0",
    fontSize: 13,
    fontWeight: 600,
    color: "#64748b",
    background: "transparent",
    border: "none",
    borderRadius: 8,
    cursor: "pointer",
    transition: "background 0.2s, color 0.2s",
  },
  tabActive: {
    background: "#ffffff",
    color: "#4f46e5",
    boxShadow: "0 1px 3px rgba(0, 0, 0, 0.12)",
  },
  errorBox: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "10px 12px",
    background: "#fef2f2",
    border: "1px solid #fecaca",
    borderRadius: 8,
    marginBottom: 16,
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
  hint: {
    fontSize: 12,
    lineHeight: 1.5,
    color: "#64748b",
    background: "#f8fafc",
    border: "1px solid #e2e8f0",
    borderRadius: 8,
    padding: "8px 10px",
  },
  form: {
    display: "flex",
    flexDirection: "column" as const,
    gap: 16,
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
  input: {
    padding: "10px 14px",
    fontSize: 14,
    color: "#1e293b",
    border: "1px solid #d1d5db",
    borderRadius: 8,
    outline: "none",
    transition: "border-color 0.2s, box-shadow 0.2s",
    width: "100%",
    boxSizing: "border-box" as const,
  },
  submitBtn: {
    marginTop: 4,
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
    marginTop: 8,
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
