/**
 * CalculatorRenderer — math_calculator 数学计算。
 * 表头显示表达式，展开显示计算结果。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard, IOBlock } from "./_primitives";

export function CalculatorRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const expression = typeof tc.args?.expression === "string" ? tc.args.expression : "";
  const isError = tc.status === "error";
  const out = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");

  return (
    <CollapsibleCard
      icon="🧮"
      name="Calc"
      summary={expression}
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
