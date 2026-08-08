/**
 * src/components/InteractionInline.tsx — v2 重设计版
 *
 * 交互型工具内嵌问题渲染组件（多问题版）。纯 v2 Token。
 */
import { useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useBackend } from "../providers/BackendProvider";
import type { QuestionItem } from "../types/api";

interface InteractionInlineProps {
  questions: QuestionItem[];
  run_id: string;
  tool_name?: string;
  /** 该问卷所属会话：作答回复必须回到此 session，避免跨会话串台。 */
  sessionId: string;
  onResolved?: (resultText: string) => void;
}

export function InteractionInline({ questions, run_id, tool_name, sessionId, onResolved }: InteractionInlineProps) {
  const { t } = useTranslation();
  const { sendInteractionResponse } = useBackend();
  const [answers, setAnswers] = useState<Map<number, string[]>>(new Map());
  const [submitted, setSubmitted] = useState(false);

  const handleSelect = useCallback((qi: number, label: string, multiSelect: boolean) => {
    if (submitted) return;
    setAnswers((cur) => {
      const next = new Map(cur);
      const current = next.get(qi) ?? [];
      if (multiSelect) {
        next.set(qi, current.includes(label) ? current.filter((l) => l !== label) : [...current, label]);
      } else {
        next.set(qi, [label]);
      }
      return next;
    });
  }, [submitted]);

  const allAnswered = questions.every((_, qi) => {
    const ans = answers.get(qi);
    return ans && ans.length > 0 && ans.some((v) => v.trim().length > 0);
  });

  const handleSubmit = () => {
    if (submitted || !allAnswered) return;
    setSubmitted(true);
    // 不再往对话里插一条「用户消息」回显选择——作答完成后 ask_user 工具会以
    // AskUserRenderer 卡片（标注工具名 + 选择结果）在时间线里展示，这样运行时
    // 与历史还原保持一致，避免出现未标注来源的重复回显。
    const responseParts = questions.map((q, qi) => {
      const header = q.header || t("chat.questionN", { n: qi + 1 });
      const ans = answers.get(qi) ?? [];
      return `${header}=${ans.map(stripFreePrefix).join(",")}`;
    });
    const response = responseParts.join("; ");
    sendInteractionResponse(run_id, response, sessionId);
    // 与后端持久化 / 历史还原一致的结果文本（ask_user 的工具结果即 "用户选择了：{response}"）
    onResolved?.(`用户选择了：${response}`);
  };

  if (questions.length === 0) return null;

  const unansweredCount = questions.filter((_, qi) => {
    const ans = answers.get(qi);
    return !ans || ans.length === 0 || !ans.some((v) => v.trim());
  }).length;

  return (
    <div style={{
      marginTop: "var(--space-3)",
      display: "flex", flexDirection: "column", gap: "var(--space-4)",
      pointerEvents: submitted ? "none" : "auto",
      opacity: submitted ? 0.5 : 1,
      transition: "opacity var(--duration-normal)",
      maxHeight: "min(70vh, 560px)", overflowY: "auto",
    }}>
      {tool_name && (
        <div className="interaction-card" style={{ margin: 0 }}>
          <div className="interaction-header">{tool_name}</div>
          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
            {questions.map((q, qi) => {
              const ans = answers.get(qi) ?? [];
              return (
                <QuestionCard
                  key={qi}
                  index={qi}
                  question={q}
                  selected={ans}
                  onSelect={(label) => handleSelect(qi, label, q.multiSelect)}
                  disabled={submitted}
                />
              );
            })}
          </div>
          {!submitted && (
            <div className="interaction-footer" style={{ justifyContent: "center", marginTop: "var(--space-4)" }}>
              <button
                onClick={handleSubmit}
                disabled={!allAnswered}
                className="btn btn-primary btn-sm"
                style={{
                  opacity: allAnswered ? 1 : 0.4,
                  cursor: allAnswered ? "pointer" : "not-allowed",
                }}
              >
                {allAnswered ? t("interaction.submitAll") : t("interaction.pendingAll", { count: unansweredCount })}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const FREE_INPUT_PREFIX = "__free_input__";
const stripFreePrefix = (text: string) =>
  text.startsWith(FREE_INPUT_PREFIX) ? text.slice(FREE_INPUT_PREFIX.length) : text;

function QuestionCard({
  index, question, selected, onSelect, disabled,
}: {
  index: number; question: QuestionItem; selected: string[];
  onSelect: (label: string) => void; disabled: boolean;
}) {
  const { t } = useTranslation();
  const multiSelect = question.multiSelect;
  const freeSelected = selected.length === 1 && selected[0].startsWith(FREE_INPUT_PREFIX);
  const freeText = freeSelected ? selected[0].slice(FREE_INPUT_PREFIX.length) : "";
  const [freeInputActive, setFreeInputActive] = useState(false);

  const handleOptionClick = (label: string) => { if (disabled) return; setFreeInputActive(false); onSelect(label); };
  const handleFreeInputToggle = () => { if (disabled) return; setFreeInputActive(true); onSelect(""); };
  const handleFreeInputChange = (text: string) => {
    if (disabled) return;
    // 文本为空时回退到空串(不带前缀),这样父组件的 allAnswered 判定会把它当作未作答;
    // 非空时携带 FREE_INPUT_PREFIX,标识这是一段自由输入内容
    onSelect(text.length > 0 ? FREE_INPUT_PREFIX + text : "");
  };

  return (
    <div>
      <div className="interaction-q-label">
        {question.header && (
          <span className="badge" style={{
            background: freeSelected ? "color-mix(in srgb, var(--success) 12%, transparent)" : multiSelect ? "color-mix(in srgb, var(--accent-2) 12%, transparent)" : "color-mix(in srgb, var(--info) 12%, transparent)",
            color: freeSelected ? "var(--success)" : multiSelect ? "var(--accent-2)" : "var(--info)",
            marginRight: "var(--space-1)",
          }}>
            {question.header}{multiSelect ? t("interaction.multiSelectSuffix") : ""}
          </span>
        )}
        <span style={{ color: "var(--text-tertiary)", fontSize: "var(--text-2xs)" }}>{t("interaction.questionIndex", { index: index + 1 })}</span>
      </div>
      <div style={{ fontSize: "var(--text-base)", fontWeight: 500, color: "var(--text-primary)", marginBottom: "var(--space-2)", lineHeight: 1.5 }}>
        {question.question}
      </div>

      {!freeInputActive ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {question.options.map((opt, oi) => {
            const isSelected = selected.includes(opt.label);
            return (
              <div
                key={oi}
                onClick={() => handleOptionClick(opt.label)}
                className={`interaction-option${isSelected ? " selected" : ""}`}
              >
                <span className="radio" />
                <span style={{ flex: 1 }}>
                  <span style={{ fontWeight: 500 }}>{opt.label}</span>
                  {opt.description && (
                    <span style={{ color: "var(--text-muted)", marginLeft: 6, fontSize: "var(--text-sm)" }}>
                      — {opt.description}
                    </span>
                  )}
                </span>
              </div>
            );
          })}
          <div onClick={handleFreeInputToggle} className="interaction-option">
            <span className="radio" /> {t("interaction.freeInput")}
          </div>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <textarea
            value={freeText}
            onChange={(e) => handleFreeInputChange(e.target.value)}
            disabled={disabled}
            placeholder={t("interaction.freeInputPlaceholder")}
            rows={3}
            autoFocus
            style={{
              width: "100%", padding: "var(--space-2) var(--space-3)",
              borderRadius: "var(--radius-sm)",
              border: freeText.trim() ? "1px solid var(--accent)" : "1px solid var(--border-default)",
              background: "var(--bg-elevated)", color: "var(--text-primary)",
              fontFamily: "var(--font-sans)", fontSize: "var(--text-sm)", resize: "vertical",
              outline: "none", lineHeight: 1.5,
            }}
          />
          <button
            onClick={() => { setFreeInputActive(false); onSelect(""); }}
            disabled={disabled}
            className="btn btn-ghost btn-xs"
            style={{ alignSelf: "flex-start" }}
          >
            ← {t("interaction.backToOptions")}
          </button>
        </div>
      )}
    </div>
  );
}
