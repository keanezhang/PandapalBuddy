/**
 * src/components/knowledgeBase/ConfirmDialog.tsx
 *
 * 通用二次确认弹窗（删除文件夹 / 文档等不可逆操作前置确认）。
 * 对齐原型 #modal-host / .modal 视觉；危险操作用红色确认按钮。
 */
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  cancelLabel,
  danger = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const { t } = useTranslation();
  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={onCancel}
      style={{
        position: "fixed",
        inset: 0,
        background: "color-mix(in srgb, var(--bg-root) 60%, transparent)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
        padding: 24,
      }}
    >
      <div
        data-testid="kb-confirm-dialog"
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg-elevated, #1c1c1c)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-md, 10px)",
          width: "100%",
          maxWidth: 440,
          padding: 20,
          boxShadow: "0 20px 60px rgba(0,0,0,0.55)",
        }}
      >
        <div style={{ fontSize: "var(--text-md)", fontWeight: 600, color: "var(--text-primary)", marginBottom: 10 }}>
          {title}
        </div>
        <div style={{ fontSize: "var(--text-sm)", color: "var(--text-secondary)", lineHeight: 1.6 }}>
          {message}
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 9, marginTop: 20 }}>
          <button className="btn btn-ghost" onClick={onCancel}>
            {cancelLabel ?? t("common.cancel", "取消")}
          </button>
          <button
            className={`btn ${danger ? "btn-danger" : "btn-primary"}`}
            onClick={onConfirm}
          >
            {confirmLabel ?? t("common.confirm", "确认")}
          </button>
        </div>
      </div>
    </div>
  );
}

