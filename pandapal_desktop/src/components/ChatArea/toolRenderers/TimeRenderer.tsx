/**
 * TimeRenderer — time_get_current_time 获取当前时间。
 * 结果一行「当前时间：…」直接放表头摘要，无需展开。
 */
import React from "react";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard } from "./_primitives";

export function TimeRenderer({ tc }: { tc: ToolCallState }) {
  const isError = tc.status === "error";
  const out = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");

  return (
    <CollapsibleCard
      icon="🕐"
      name="Time"
      summary={out}
      status={tc.status}
      durationMs={tc.durationMs}
    />
  );
}
