/**
 * src/components/PlanApprovalModal.tsx — v2 重设计版
 *
 * Plan Mode 计划审批弹窗。纯 v2 Token。
 * 数据源：planApprovalStore.useCurrentPlan（按 session 归档 + 当前视图门控），
 * 只渲染「当前正在看的 session」的待审批计划，切走则暂存、切回重现——
 * 与 HitlModal 同款，杜绝跨 session 错弹。
 */
import React, { useState } from "react";
import { useBackend } from "../providers/BackendProvider";
import { useCurrentPlan } from "../store/planApprovalStore";
import type { PlanApprovalRequestMsg } from "../types/api";

export function PlanApprovalModal() {
  const plan = useCurrentPlan();
  const { sendPlanApprovalDecision } = useBackend();

  if (!plan) return null;

  const sessionId = plan.session_id ?? "";
  const userId = plan.user_id ?? null;

  return (
    <div className="modal-overlay">
      <div className="modal" style={{ width: 520 }}>
        <div className="modal-header">
          <span className="modal-title">📋 执行计划审批</span>
          <button
            className="modal-close"
            onClick={() => sendPlanApprovalDecision(plan.run_id ?? "", "abandon", sessionId, userId)}
          >
            ✕
          </button>
        </div>
        <PlanBody plan={plan} sessionId={sessionId} userId={userId} />
      </div>
    </div>
  );
}

function PlanBody({ plan, sessionId, userId }: { plan: PlanApprovalRequestMsg; sessionId: string; userId: string | null }) {
  const { sendPlanApprovalDecision } = useBackend();
  const [mode, setMode] = useState<"approve" | "refine">();
  const [refineText, setRefineText] = useState("");

  const runId = plan.run_id || (plan as any).request_id || "";
  const content = plan.plan_content || (plan as any).content || "";

  const handleApprove = () => sendPlanApprovalDecision(runId, "approve", sessionId, userId);
  const handleRefine = () => sendPlanApprovalDecision(runId, "refine", sessionId, userId, refineText);
  const handleAbandon = () => sendPlanApprovalDecision(runId, "abandon", sessionId, userId);

  if (mode === "refine") {
    return (
      <>
        <div className="modal-body">
          <div className="task-section-label">请描述需要完善的内容</div>
          <textarea
            value={refineText}
            onChange={(e) => setRefineText(e.target.value)}
            placeholder="例如：需要增加单元测试步骤、步骤2应该先做代码审查…"
            autoFocus
            rows={4}
            style={{
              width: "100%", padding: "var(--space-3)",
              borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
              background: "var(--bg-elevated)", color: "var(--text-primary)",
              fontFamily: "var(--font-sans)", fontSize: "var(--text-base)", resize: "vertical",
              outline: "none", marginTop: "var(--space-1)",
            }}
            onFocus={(e) => {
              e.target.style.borderColor = "var(--border-focus)";
              e.target.style.boxShadow = "0 0 0 3px color-mix(in srgb, var(--accent) 10%, transparent)";
            }}
            onBlur={(e) => {
              e.target.style.borderColor = "var(--border-default)";
              e.target.style.boxShadow = "none";
            }}
          />
        </div>
        <div className="modal-footer">
          <button className="btn btn-ghost btn-sm" onClick={() => setMode(undefined)}>取消</button>
          <button className="btn btn-primary btn-sm" onClick={handleRefine} disabled={!refineText.trim()}>
            提交完善建议
          </button>
        </div>
      </>
    );
  }

  return (
    <>
      <div className="modal-body">
        <div style={{
          fontSize: "var(--text-base)", lineHeight: 1.6, color: "var(--text-secondary)",
          whiteSpace: "pre-wrap", wordBreak: "break-word",
          maxHeight: "40vh", overflowY: "auto",
          padding: "var(--space-3)", background: "var(--color-code-bg)",
          borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)",
        }}>
          {content}
        </div>
      </div>
      <div className="modal-footer">
        <button className="btn btn-danger btn-sm" onClick={handleAbandon}>✕ 放弃</button>
        <button className="btn btn-ghost btn-sm" onClick={() => setMode("refine")}>✎ 需完善</button>
        <button className="btn btn-primary btn-sm" onClick={handleApprove}>✓ 批准执行</button>
      </div>
    </>
  );
}
