/**
 * ReadRenderer — read_file 专属。表头显示 文件名，展开显示读取到的内容。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard, IOBlock, baseName } from "./_primitives";

export function ReadRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const filePath = tc.args?.file_path;
  const isError = tc.status === "error";
  const out = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");

  return (
    <CollapsibleCard
      icon="📄"
      name="Read"
      summary={baseName(filePath)}
      status={tc.status}
      durationMs={tc.durationMs}
    >
      <IOBlock label={isError ? t("toolLabels.error") : t("toolLabels.out")} text={out} tone={isError ? "error" : "default"} maxHeight={320} />
    </CollapsibleCard>
  );
}
