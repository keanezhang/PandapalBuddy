/**
 * src/components/TaskNotificationModal.tsx — v2 重设计版
 *
 * 任务通知弹窗。纯 v2 Token。
 * 数据源：BackendProvider context（pendingTaskNotification）。
 */
import React from "react";
import { useBackend } from "../providers/BackendProvider";

const LEVEL_INFO: Record<string, { icon: string; color: string; bg: string }> = {
  info: { icon: "ℹ️", color: "var(--accent-soft)", bg: "var(--bg-selected)" },
  warning: { icon: "⚠️", color: "var(--warning)", bg: "rgba(245,158,11,0.08)" },
  error: { icon: "❌", color: "var(--danger)", bg: "rgba(239,68,68,0.08)" },
};

export function TaskNotificationModal() {
  const { pendingTaskNotification, clearTaskNotification } = useBackend();

  if (!pendingTaskNotification) return null;

  const level = pendingTaskNotification.level ?? "info";
  const info = LEVEL_INFO[level] || LEVEL_INFO.info;

  return (
    <div className="modal-overlay">
      <div className="modal" style={{ width: 400 }}>
        <div className="modal-header">
          <span className="modal-title" style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
            <span>{info.icon}</span>
            {pendingTaskNotification.title || "任务通知"}
          </span>
          <button className="modal-close" onClick={clearTaskNotification}>✕</button>
        </div>
        <div className="modal-body">
          {pendingTaskNotification.body && (
            <div style={{ fontSize: "var(--text-base)", lineHeight: 1.6, color: "var(--text-secondary)" }}>
              {pendingTaskNotification.body}
            </div>
          )}
          <div style={{ marginTop: "var(--space-3)" }}>
            <span className="badge" style={{ background: info.bg, color: info.color }}>
              {level.toUpperCase()}
            </span>
          </div>
        </div>
        <div className="modal-footer">
          <button className="btn btn-primary btn-sm" onClick={clearTaskNotification}>我知道了</button>
        </div>
      </div>
    </div>
  );
}
