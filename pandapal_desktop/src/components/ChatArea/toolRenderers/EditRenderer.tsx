/**
 * EditRenderer — edit_file 专属。表头显示 文件名 + (+add -del)，展开显示红绿 diff。
 * diff 直接来自 args.old_string → args.new_string，无需读文件。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard, DiffView, IOBlock, baseName, diffStat } from "./_primitives";

export function EditRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const filePath = tc.args?.file_path;
  const oldStr = typeof tc.args?.old_string === "string" ? tc.args.old_string : "";
  const newStr = typeof tc.args?.new_string === "string" ? tc.args.new_string : "";
  const isError = tc.status === "error";
  const { add, del } = diffStat(oldStr, newStr);

  const meta = !isError && (add > 0 || del > 0) ? (
    <span style={{ fontSize: "var(--text-2xs)", fontFamily: "var(--font-mono)" }}>
      {add > 0 && <span style={{ color: "var(--success)" }}>+{add} </span>}
      {del > 0 && <span style={{ color: "var(--danger)" }}>-{del}</span>}
    </span>
  ) : undefined;

  return (
    <CollapsibleCard
      icon="✎"
      name="Edit"
      summary={baseName(filePath)}
      meta={meta}
      status={tc.status}
      durationMs={tc.durationMs}
    >
      {isError ? (
        <IOBlock label={t("toolLabels.error")} text={tc.result?.error ?? tc.result?.preview ?? ""} tone="error" />
      ) : (
        <DiffView oldStr={oldStr} newStr={newStr} />
      )}
    </CollapsibleCard>
  );
}
