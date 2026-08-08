/**
 * src/components/ui/Toast.tsx
 *
 * <ToastHost />：全局 Toast 渲染出口，在 App 根挂载一次。
 * 样式类：.toast-container / .toast / .toast--type（SECTION 20）。
 */

import { useTranslation } from "react-i18next";
import { useToastStore, type ToastType } from "./toastStore";

const ICONS: Record<ToastType, string> = {
  success: "✓",
  error: "✕",
  info: "ℹ",
};

export function ToastHost() {
  const { t } = useTranslation();
  const toasts = useToastStore((s) => s.toasts);
  const dismiss = useToastStore((s) => s.dismiss);

  if (toasts.length === 0) return null;

  return (
    <div className="toast-container">
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast toast--${toast.type}`}>
          <span className="toast-icon">{ICONS[toast.type]}</span>
          <span className="toast-body">
            {toast.message}
            {toast.highlight && <span className="toast-highlight">{toast.highlight}</span>}
          </span>
          <button className="toast-close" onClick={() => dismiss(toast.id)} aria-label={t("common.close")}>
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}
