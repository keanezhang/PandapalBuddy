/**
 * BashRenderer — 命令行工具专属。IN(命令) / OUT(stdout)。
 */
import React from "react";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard, IOBlock } from "./_primitives";

export function BashRenderer({ tc }: { tc: ToolCallState }) {
  const command = typeof tc.args?.command === "string" ? tc.args.command : "";
  const isError = tc.status === "error";
  const out = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");

  return (
    <CollapsibleCard
      icon="⌘"
      name="Bash"
      summary={command.split("\n")[0]}
      status={tc.status}
      durationMs={tc.durationMs}
    >
      <IOBlock label="IN" text={command} />
      <IOBlock label="OUT" text={out} tone={isError ? "error" : "default"} />
    </CollapsibleCard>
  );
}
