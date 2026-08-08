/**
 * src/components/HitlModal.tsx — v2 重设计版
 *
 * HITL 人工审批弹窗。纯 v2 Token。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import { useCurrentPrompt } from "../store/hitlStore";
import { useBackend } from "../providers/BackendProvider";

export function HitlModal() {
  const { t } = useTranslation();
  const prompt = useCurrentPrompt();
  const { sendHitlDecision } = useBackend();

  if (!prompt) return null;

  const reject = () => sendHitlDecision(prompt.runId, "rejected", prompt.approvalId, prompt.sessionId);
  const approve = () => sendHitlDecision(prompt.runId, "approved", prompt.approvalId, prompt.sessionId);

  const params = prompt.toolArgsSummary;
  const paramsStr = params && Object.keys(params).length > 0
    ? JSON.stringify(params, null, 2)
    : "";

  return (
    <div className="modal-overlay">
      <div className="modal">
        <div className="modal-header">
          <span className="modal-title">⚠️ {t("hitl.title")}</span>
          <button
            className="modal-close"
            onClick={reject}
          >
            ✕
          </button>
        </div>
        <div className="modal-body">
          <div style={{ marginBottom: "var(--space-4)" }}>
            <div className="task-section-label">{t("hitl.toolName")}</div>
            <code style={{
              fontFamily: "var(--font-mono)", fontSize: "var(--text-sm)",
              background: "var(--bg-card-subtle)", padding: "3px 8px",
              borderRadius: "var(--radius-xs)", color: "var(--accent-2)",
            }}>
              {prompt.toolName}
            </code>
          </div>
          {paramsStr && (
            <div className="task-section">
              <div className="task-section-label">{t("hitl.params")}</div>
              <pre style={{
                fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)",
                background: "var(--color-code-bg)", padding: "var(--space-3)",
                borderRadius: "var(--radius-sm)", overflowX: "auto",
                color: "var(--text-secondary)", whiteSpace: "pre-wrap",
                maxHeight: 240, overflowY: "auto",
              }}>
                {paramsStr}
              </pre>
            </div>
          )}
        </div>
        <div className="modal-footer">
          <button
            className="btn btn-danger btn-sm"
            onClick={reject}
          >
            ✕ {t("hitl.reject")}
          </button>
          <button
            className="btn btn-success btn-sm"
            onClick={approve}
          >
            ✓ {t("hitl.approve")}
          </button>
        </div>
      </div>
    </div>
  );
}
