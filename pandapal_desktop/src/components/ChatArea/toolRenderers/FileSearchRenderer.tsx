/**
 * FileSearchRenderer — glob / grep 的文件名与内容搜索。
 * 表头显示搜索模式，展开显示匹配结果。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard, IOBlock } from "./_primitives";

export function FileSearchRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const isGrep = tc.tool_name === "grep";
  const pattern = typeof tc.args?.pattern === "string" ? tc.args.pattern : "";
  const isError = tc.status === "error";
  const out = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");

  return (
    <CollapsibleCard
      icon="🔍"
      name={isGrep ? "Grep" : "Glob"}
      summary={pattern}
      status={tc.status}
      durationMs={tc.durationMs}
    >
      <IOBlock
        label={isError ? t("toolLabels.error") : t("toolLabels.result")}
        text={out}
        tone={isError ? "error" : "default"}
        maxHeight={300}
      />
    </CollapsibleCard>
  );
}
