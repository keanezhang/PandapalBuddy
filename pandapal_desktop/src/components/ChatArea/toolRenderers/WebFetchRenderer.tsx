/**
 * WebFetchRenderer — web_fetch 专属。
 *
 * 后端返回格式（成功）：
 *   # {url}\n状态：{code} | 内容类型：{ctype}\n{重定向?}\n{清洗后的正文}
 * 失败时返回纯错误串（无表头，如「请求失败：HTTP 404（url）」）。
 *
 * 卡片：🌐 URL 做标题，状态行做右侧徽标，正文可折叠。
 */
import React from "react";
import { useTranslation } from "react-i18next";
import type { ToolCallState } from "../../../store/chatStore";
import { CollapsibleCard, IOBlock } from "./_primitives";

/** 拆出表头状态行与正文；无表头（错误串）则原样返回。 */
function splitFetchResult(raw: string): { status: string; body: string } {
  const lines = raw.split("\n");
  if (lines[0]?.startsWith("# ") && lines[1]?.startsWith("状态：")) {
    const status = lines[1].slice("状态：".length).trim();
    // 表头后跟一个空行（或重定向说明 + 空行），去掉起始空行还原正文。
    const body = lines.slice(2).join("\n").replace(/^\n+/, "");
    return { status, body };
  }
  return { status: "", body: raw };
}

export function WebFetchRenderer({ tc }: { tc: ToolCallState }) {
  const { t } = useTranslation();
  const url = typeof tc.args?.url === "string" ? tc.args.url : "";
  const isError = tc.status === "error";
  const raw = isError
    ? (tc.result?.error ?? tc.result?.preview ?? "")
    : (tc.result?.full || tc.result?.preview || "");

  const { status, body } = isError
    ? { status: "", body: raw }
    : splitFetchResult(raw);

  return (
    <CollapsibleCard
      icon="🌐"
      name="Fetch"
      summary={url}
      meta={
        status ? (
          <span style={{ color: "var(--text-muted)", fontSize: "var(--text-2xs)", fontFamily: "var(--font-mono)" }}>
            {status}
          </span>
        ) : undefined
      }
      status={tc.status}
      durationMs={tc.durationMs}
    >
      <IOBlock
        label={isError ? t("toolLabels.error") : t("toolLabels.content")}
        text={body}
        tone={isError ? "error" : "default"}
        maxHeight={360}
      />
    </CollapsibleCard>
  );
}
