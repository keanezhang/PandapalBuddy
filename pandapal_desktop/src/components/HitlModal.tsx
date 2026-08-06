/**
 * src/components/HitlModal.tsx — v2 重设计版
 *
 * HITL 人工审批弹窗。纯 v2 Token。
 */
import React from "react";
import { useCurrentPrompt } from "../store/hitlStore";
import { useBackend } from "../providers/BackendProvider";

export function HitlModal() {
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
          <span className="modal-title">⚠️ 工具调用需要审批</span>
          <button
            className="modal-close"
            onClick={reject}
          >
            ✕
          </button>
        </div>
        <div className="modal-body">
          <div style={{ marginBottom: "var(--space-4)" }}>
            <div className="task-section-label">工具名称</div>
            <code style={{
              fontFamily: "var(--font-mono)", fontSize: 12,
              background: "rgba(255,255,255,0.04)", padding: "3px 8px",
              borderRadius: "var(--radius-xs)", color: "var(--accent-2)",
            }}>
              {prompt.toolName}
            </code>
          </div>
          {paramsStr && (
            <div className="task-section">
              <div className="task-section-label">参数</div>
              <pre style={{
                fontFamily: "var(--font-mono)", fontSize: 11,
                background: "#121212", padding: "var(--space-3)",
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
            ✕ 拒绝
          </button>
          <button
            className="btn btn-success btn-sm"
            onClick={approve}
          >
            ✓ 批准
          </button>
        </div>
      </div>
    </div>
  );
}
