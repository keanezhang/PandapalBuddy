/**
 * AskUserRenderer — ask_user 提问工具卡。
 *
 * ask_user 运行中由气泡底部的 InteractionInline 接管作答（此时该工具在时间线里隐藏）；
 * 一旦作答完成（done/error）或历史还原，改由本卡片展示——标注工具名 + 用户的选择。
 * 数据来源：result 文本形如「用户选择了：出行时间=秋季; 预算范围=舒适 10K-18K; ...」。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";

interface QA { header: string; answer: string }

const ANSWER_MARKER = "用户选择了：";

function parseSelections(result: string): QA[] {
  const idx = result.indexOf(ANSWER_MARKER);
  const body = idx >= 0 ? result.slice(idx + ANSWER_MARKER.length) : result;
  return body
    .split(/;\s*/)
    .map((part) => {
      const eq = part.indexOf("=");
      if (eq < 0) return null;
      const header = part.slice(0, eq).trim();
      const answer = part.slice(eq + 1).trim();
      return header && answer ? { header, answer } : null;
    })
    .filter((x): x is QA => x !== null);
}

export function AskUserRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const resultText = tc.result?.full || tc.result?.preview || tc.result?.error || "";
  const qas = parseSelections(resultText);
  // 以「是否解析出选择」判定成败，而非 tc.status——校验失败（如选项数超限）的结果
  // 后端不标 ❌ 前缀，状态仍是 done，但它其实是一次无效提问。
  const answered = qas.length > 0;

  return (
    <div style={{
      marginTop: "var(--space-2)",
      border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-md)",
      background: "var(--bg-panel)",
      overflow: "hidden",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", padding: "6px var(--space-3)", fontSize: "var(--text-sm)" }}>
        <span style={{ flexShrink: 0 }}>🙋</span>
        <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>Ask_Questions</span>
        <span
          className={answered ? "badge badge-green" : "badge badge-red"}
          style={{ marginLeft: "auto" }}
        >
          {answered ? t("toolFeedback.answered") : t("toolFeedback.invalid")}
        </span>
      </div>

      {answered ? (
        <div style={{
          borderTop: "1px solid var(--border-subtle)", padding: "var(--space-2) var(--space-3)",
          display: "flex", flexDirection: "column", gap: 4,
        }}>
          {qas.map((qa, i) => (
            <div key={i} style={{ fontSize: "var(--text-sm)", display: "flex", gap: "var(--space-2)", lineHeight: 1.5 }}>
              <span style={{ color: "var(--text-tertiary)", flexShrink: 0 }}>{qa.header}</span>
              <span style={{ color: "var(--text-primary)", fontWeight: 500, wordBreak: "break-word" }}>{qa.answer}</span>
            </div>
          ))}
        </div>
      ) : resultText ? (
        <div style={{
          borderTop: "1px solid var(--border-subtle)", padding: "var(--space-2) var(--space-3)",
          fontSize: "var(--text-xs)", color: "var(--danger)", whiteSpace: "pre-wrap", wordBreak: "break-word",
        }}>
          {resultText}
        </div>
      ) : null}
    </div>
  );
}
