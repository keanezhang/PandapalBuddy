/**
 * ListFilesRenderer — list_files 目录浏览。
 * 表头显示目标路径，展开显示目录条目。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard, IOBlock } from "./_primitives";

export function ListFilesRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const path = typeof tc.args?.path === "string" ? tc.args.path : "";
  const isError = tc.status === "error";
  const out = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");

  return (
    <CollapsibleCard
      icon="📂"
      name="List"
      summary={path}
      status={tc.status}
      durationMs={tc.durationMs}
    >
      <IOBlock
        label={isError ? t("toolLabels.error") : t("toolLabels.content")}
        text={out}
        tone={isError ? "error" : "default"}
        maxHeight={300}
      />
    </CollapsibleCard>
  );
}
