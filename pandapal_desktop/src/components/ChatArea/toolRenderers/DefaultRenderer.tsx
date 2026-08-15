/**
 * DefaultRenderer — 通用兜底卡。
 * 任何没有专属渲染器的工具都走这里：表头(图标+名+主参数+耗时)，展开显示 args + result。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard, IOBlock, primaryArg, toolDisplayName } from "./_primitives";

export function DefaultRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const argsText = tc.args && Object.keys(tc.args).length > 0
    ? safeJson(tc.args)
    : "";
  const isError = tc.status === "error";
  const output = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");
  const hasBody = Boolean(argsText || output);
  const name = toolDisplayName(tc.tool_name);

  return (
    <CollapsibleCard
      icon="⚙"
      name={name}
      summary={primaryArg(tc.args)}
      status={tc.status}
      durationMs={tc.durationMs}
    >
      {hasBody && (
        <>
          <IOBlock label={t("toolLabels.args")} text={argsText} />
          <IOBlock label={t("toolLabels.result")} text={output} tone={isError ? "error" : "default"} />
          {tc.result?.truncated && (
            <div style={{ padding: "2px var(--space-3) var(--space-2)", fontSize: "var(--text-2xs)", color: "var(--text-muted)" }}>
              {tc.result.sizeBytes
                ? t("toolFeedback.outputTruncatedWithBytes", { bytes: tc.result.sizeBytes })
                : t("toolFeedback.outputTruncated")}
            </div>
          )}
        </>
      )}
    </CollapsibleCard>
  );
}

function safeJson(o: unknown): string {
  try {
    return JSON.stringify(o, null, 2);
  } catch {
    return String(o);
  }
}
