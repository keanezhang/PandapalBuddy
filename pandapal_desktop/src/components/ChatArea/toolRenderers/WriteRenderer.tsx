/**
 * WriteRenderer — write_file 专属。表头显示 文件名 + 行数，展开显示代码预览。
 */
import React from "react";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard, CodePreview, IOBlock, baseName, lineCount } from "./_primitives";

export function WriteRenderer({ tc }: { tc: ToolCallState }) {
  const filePath = tc.args?.file_path;
  const content = typeof tc.args?.content === "string" ? tc.args.content : "";
  const isError = tc.status === "error";
  const lines = lineCount(content);

  const meta = !isError && lines > 0
    ? <span style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{lines} lines</span>
    : undefined;

  return (
    <CollapsibleCard
      icon="✍"
      name="Write"
      summary={baseName(filePath)}
      meta={meta}
      status={tc.status}
      durationMs={tc.durationMs}
    >
      {isError ? (
        <IOBlock label="ERROR" text={tc.result?.error ?? tc.result?.preview ?? ""} tone="error" />
      ) : content ? (
        <CodePreview code={content} />
      ) : null}
    </CollapsibleCard>
  );
}
