/**
 * SearchSkillsRenderer — search_skills 技能加载卡。
 *
 * search_skills 是「按名称精准加载技能」的工具，成功后把技能内容注入上下文。
 * 后端成功返回格式：「🔧 已加载技能 [name]\n\n{content}」。
 * 本卡表头以徽章形式展示技能名（可扫读「刚才加载了什么知识」），
 * 展开正文展示技能内容；未命中/加载失败时默认展开并显示原始输出。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard } from "./_primitives";

const LOADED_MARKER = "🔧 已加载技能 [";

/** 解析「🔧 已加载技能 [name]\n\ncontent」→ { name, content }；非加载成功格式返回 null。 */
export function parseSkillLoaded(text: string): { name: string; content: string } | null {
  const idx = text.indexOf(LOADED_MARKER);
  if (idx < 0) return null;
  const rest = text.slice(idx + LOADED_MARKER.length);
  const end = rest.indexOf("]");
  if (end < 0) return null;
  const name = rest.slice(0, end).trim();
  if (!name) return null;
  return { name, content: rest.slice(end + 1).trim() };
}

export function SearchSkillsRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const isError = tc.status === "error";
  const output = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");
  const loaded = parseSkillLoaded(output);
  const hasBody = Boolean(loaded ? loaded.content : output);

  return (
    <CollapsibleCard
      icon="📙"
      name="Search_Skills"
      status={tc.status}
      durationMs={tc.durationMs}
      // 技能名徽章：加载成功显示技能名，失败显示红章（加载失败/未找到/门禁拒绝）
      meta={
        loaded ? (
          <span className="badge badge-purple" title={t("toolFeedback.skillLoaded")}>
            ✓ {loaded.name}
          </span>
        ) : isError ? (
          <span className="badge badge-red">{t("toolFeedback.skillLoadFailed")}</span>
        ) : undefined
      }
      // 异常情况（无技能/未找到/门禁拒绝）默认展开，便于直接看到原因
      defaultExpanded={Boolean(output && !loaded)}
    >
      {hasBody && (
        <div style={{
          padding: "var(--space-2) var(--space-3)",
          fontSize: "var(--text-11)", lineHeight: 1.55,
          whiteSpace: "pre-wrap", wordBreak: "break-word",
          color: isError ? "var(--danger)" : "var(--text-secondary)",
          fontFamily: "var(--font-mono)",
        }}>
          {loaded ? loaded.content : output}
        </div>
      )}
    </CollapsibleCard>
  );
}
