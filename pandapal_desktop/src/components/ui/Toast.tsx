/**
 * src/components/ui/Toast.tsx
 *
 * <ToastHost />：全局 Toast 渲染出口，在 App 根挂载一次。
 * 样式类：.toast-container / .toast / .toast--type（SECTION 20）。
 */

import { useToastStore, type ToastType } from "./toastStore";

const ICONS: Record<ToastType, string> = {
  success: "✓",
  error: "✕",
  info: "ℹ",
};

export function ToastHost() {
  const toasts = useToastStore((s) => s.toasts);
  const dismiss = useToastStore((s) => s.dismiss);

  if (toasts.length === 0) return null;

  return (
    <div className="toast-container">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast--${t.type}`}>
          <span className="toast-icon">{ICONS[t.type]}</span>
          <span className="toast-body">
            {t.message}
            {t.highlight && <span className="toast-highlight">{t.highlight}</span>}
          </span>
          <button className="toast-close" onClick={() => dismiss(t.id)} aria-label="关闭">
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}
