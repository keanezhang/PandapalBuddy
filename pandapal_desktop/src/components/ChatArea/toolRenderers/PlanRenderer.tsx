/**
 * PlanRenderer — 规划模式三件套（enter_plan_mode / write_plan / exit_plan_mode）。
 * 表头显示计划文件路径，展开显示引导 / 写入 / 提交结果。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard, IOBlock } from "./_primitives";

function planLabel(toolName: string): string {
  switch (toolName) {
    case "write_plan": return "Write Plan";
    case "exit_plan_mode": return "Submit Plan";
    default: return "Plan";
  }
}

function planIcon(toolName: string): string {
  switch (toolName) {
    case "write_plan": return "📝";
    case "exit_plan_mode": return "📤";
    default: return "📋";
  }
}

export function PlanRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const path = typeof tc.args?.plan_file_path === "string" && tc.args.plan_file_path
    ? tc.args.plan_file_path
    : typeof tc.args?.plan_name === "string" ? tc.args.plan_name : "";
  const isError = tc.status === "error";
  const out = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");

  return (
    <CollapsibleCard
      icon={planIcon(tc.tool_name)}
      name={planLabel(tc.tool_name)}
      summary={path}
      status={tc.status}
      durationMs={tc.durationMs}
    >
      <IOBlock
        label={isError ? t("toolLabels.error") : t("toolLabels.content")}
        text={out}
        tone={isError ? "error" : "default"}
        maxHeight={280}
      />
    </CollapsibleCard>
  );
}
