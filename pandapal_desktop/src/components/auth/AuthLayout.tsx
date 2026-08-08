/**
 * src/components/auth/AuthLayout.tsx
 *
 * 认证页共享布局（登录 / 注册）：品牌渐变背景 + 居中卡片。
 * 样式类定义在 global-v2.css SECTION 29（.auth-*），跟随主题 token。
 */

import type { ReactNode } from "react";
import { Link } from "react-router-dom";

/** 页面骨架：渐变背景 + 卡片 + Logo/标题/副标题 */
export function AuthLayout({
  subtitle,
  children,
}: {
  subtitle: string;
  children: ReactNode;
}) {
  return (
    <div className="auth-container">
      <div className="auth-card">
        <div className="auth-header">
          <div className="auth-logo">🐼</div>
          <h1 className="auth-title">PandaPal</h1>
          <p className="auth-subtitle">{subtitle}</p>
        </div>
        {children}
      </div>
    </div>
  );
}

/** 错误提示条 */
export function AuthError({
  message,
  onClose,
}: {
  message: string;
  onClose: () => void;
}) {
  return (
    <div className="auth-error">
      <span className="auth-error-icon">⚠️</span>
      <span className="auth-error-text">{message}</span>
      <button className="auth-error-close" onClick={onClose} aria-label="关闭">
        ×
      </button>
    </div>
  );
}

/** 表单字段：label + 控件 */
export function AuthField({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="auth-field">
      <label className="auth-label">{label}</label>
      {children}
    </div>
  );
}

/** 底部链接行 */
export function AuthFooter({
  text,
  linkTo,
  linkText,
}: {
  text: string;
  linkTo: string;
  linkText: string;
}) {
  return (
    <div className="auth-footer">
      <span>{text}</span>
      <Link to={linkTo} className="auth-link">
        {linkText}
      </Link>
    </div>
  );
}
