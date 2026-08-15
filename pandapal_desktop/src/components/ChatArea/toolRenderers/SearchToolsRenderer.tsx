/**
 * SearchToolsRenderer — search_tools 工具加载卡。
 *
 * search_tools 是「按名称加载 DEFERRED 工具的完整 schema」的工具，加载后该工具才可被调用。
 * 后端成功返回格式：
 *   「找到 1 个匹配工具：\n  - {name}：{description}（适用场景：{when_to_use}）」
 * 未命中时返回 success=True 但正文为「未找到工具 '{name}'，请检查名称是否正确（区分大小写）」，
 * 状态仍是 done —— 因此不能只看 tc.status 判断成败，需解析正文。
 *
 * 本卡表头以紫色徽章高亮「被搜索的工具名」（可扫读「刚才搜了哪个工具」），
 * 展开正文展示 description + 适用场景；未命中/失败时默认展开并显示原始输出。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard } from "./_primitives";

const FOUND_MARKER = "找到 1 个匹配工具：";

/** 解析「找到 1 个匹配工具：\n  - {name}：{description}（适用场景：{when_to_use}）」→ 结构；非命中返回 null。 */
export function parseToolFound(text: string): { name: string; description: string; whenToUse: string } | null {
  const idx = text.indexOf(FOUND_MARKER);
  if (idx < 0) return null;
  const body = text.slice(idx + FOUND_MARKER.length).trim();
  // body 形如：- {name}：{description}（适用场景：{when_to_use}）
  const afterDash = body.replace(/^-\s+/, "");
  const colon = afterDash.indexOf("：");
  if (colon < 0) return null;
  const name = afterDash.slice(0, colon).trim();
  let rest = afterDash.slice(colon + 1).trim();
  let description = rest;
  let whenToUse = "";
  const sceneIdx = rest.lastIndexOf("（适用场景：");
  if (sceneIdx >= 0) {
    description = rest.slice(0, sceneIdx).trim();
    whenToUse = rest.slice(sceneIdx + "（适用场景：".length).replace(/）\s*$/, "").trim();
  }
  if (!name) return null;
  return { name, description, whenToUse };
}

export function SearchToolsRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const toolName = typeof tc.args?.tool_name === "string" ? tc.args.tool_name : "";
  const isError = tc.status === "error";
  const output = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");
  const found = parseToolFound(output);
  const hasBody = Boolean(found ? found.description || found.whenToUse : output);

  return (
    <CollapsibleCard
      icon="🧰"
      name="Search_Tools"
      status={tc.status}
      durationMs={tc.durationMs}
      // 被搜索的工具名徽章：与 search_skills 统一，紫色高亮「搜了哪个工具」；
      // 未命中时显示红章并保留工具名（能看出是哪个没找到）。
      meta={
        toolName ? (
          found ? (
            <span className="badge badge-purple" title={t("toolFeedback.toolLoaded")}>
              ✓ {toolName}
            </span>
          ) : (
            <span className="badge badge-red" title={t("toolFeedback.toolLoadFailed")}>
              ✗ {toolName}
            </span>
          )
        ) : isError ? (
          <span className="badge badge-red">{t("toolFeedback.toolLoadFailed")}</span>
        ) : undefined
      }
      // 未命中（成功但正文是「未找到」）默认展开，便于直接看到原因
      defaultExpanded={Boolean(output && !found)}
    >
      {found ? (
        <div style={{ padding: "var(--space-2) var(--space-3)", fontSize: "var(--text-11)", lineHeight: 1.6 }}>
          {found.description && (
            <div style={{ color: "var(--text-secondary)", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
              {found.description}
            </div>
          )}
          {found.whenToUse && (
            <div style={{ marginTop: 6, color: "var(--text-tertiary)", fontSize: "var(--text-2xs)" }}>
              {t("toolLabels.whenToUse")}：{found.whenToUse}
            </div>
          )}
        </div>
      ) : hasBody ? (
        <div style={{
          padding: "var(--space-2) var(--space-3)",
          fontSize: "var(--text-11)", lineHeight: 1.55,
          whiteSpace: "pre-wrap", wordBreak: "break-word",
          color: isError ? "var(--danger)" : "var(--text-secondary)",
          fontFamily: "var(--font-mono)",
        }}>
          {output}
        </div>
      ) : null}
    </CollapsibleCard>
  );
}
