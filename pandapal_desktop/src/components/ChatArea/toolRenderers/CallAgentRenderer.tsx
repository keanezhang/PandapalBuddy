/**
 * CallAgentRenderer — call_agent 委派子 Agent。
 * 表头以紫色徽章展示子 Agent 名（与 search_skills 风格统一），
 * 展开显示子 Agent 产出。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard, IOBlock } from "./_primitives";

export function CallAgentRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const agentName = typeof tc.args?.agent_name === "string" ? tc.args.agent_name : "";
  const isError = tc.status === "error";
  const out = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");

  return (
    <CollapsibleCard
      icon="🤖"
      name="Call_SubAgent"
      status={tc.status}
      durationMs={tc.durationMs}
      // 子 Agent 名徽章：与 search_skills 统一，紫色高亮「委派给了谁」；失败显示红章。
      meta={
        isError ? (
          <span className="badge badge-red">{t("toolLabels.error")}</span>
        ) : agentName ? (
          <span className="badge badge-purple" title={t("toolFeedback.agentDelegated")}>
            ✓ {agentName}
          </span>
        ) : undefined
      }
    >
      <IOBlock
        label={isError ? t("toolLabels.error") : t("toolLabels.result")}
        text={out}
        tone={isError ? "error" : "default"}
        maxHeight={320}
      />
    </CollapsibleCard>
  );
}
