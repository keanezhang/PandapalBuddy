/**
 * DeleteFileRenderer — delete_file 删除文件。
 * 表头显示文件名；permanently=true 时以红色徽标警示不可逆。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard, IOBlock, baseName } from "./_primitives";

export function DeleteFileRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const filePath = tc.args?.file_path;
  const permanently = tc.args?.permanently === true;
  const isError = tc.status === "error";
  const out = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");

  const meta = permanently ? (
    <span style={{ fontSize: "var(--text-2xs)", color: "var(--danger)", fontWeight: 600 }}>
      {t("toolLabels.permanent")}
    </span>
  ) : undefined;

  return (
    <CollapsibleCard
      icon="🗑️"
      name="Delete"
      summary={baseName(filePath)}
      meta={meta}
      status={tc.status}
      durationMs={tc.durationMs}
    >
      <IOBlock
        label={isError ? t("toolLabels.error") : t("toolLabels.result")}
        text={out}
        tone={isError ? "error" : "default"}
      />
    </CollapsibleCard>
  );
}
