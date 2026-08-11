/**
 * src/components/ChatArea/toolRenderers/registry.tsx
 *
 * 工具渲染分发：按 tool_name 命中专属渲染器，未命中 → DefaultRenderer。
 * shouldHideTool：infra / task_update / questionnaire 类不在时间线里出卡
 * （前者是内部噪声，questionnaire 由 InteractionInline 在气泡底部单独渲染）。
 */
import React from "react";
import type { ToolCallState } from "../../../store/chatStore";
import { SearchResultBlock } from "../SearchResultBlock";
import { DefaultRenderer } from "./DefaultRenderer";
import { BashRenderer } from "./BashRenderer";
import { EditRenderer } from "./EditRenderer";
import { WriteRenderer } from "./WriteRenderer";
import { ReadRenderer } from "./ReadRenderer";
import { AskUserRenderer } from "./AskUserRenderer";
import { WebFetchRenderer } from "./WebFetchRenderer";
import { SearchSkillsRenderer } from "./SearchSkillsRenderer";
import { ToolFeedbackBanner } from "./ToolFeedbackBanner";

/** 该工具是否不在时间线中直接出卡。 */
export function shouldHideTool(tc: ToolCallState): boolean {
  if (tc.category === "infra" || tc.category === "task_update") return true;
  // questionnaire（ask_user）：运行中由 InteractionInline 在气泡底部接管作答，此时隐藏；
  // 作答完成（done/error）或历史还原时，改由 AskUserRenderer 出卡，标注工具名 + 选择结果。
  if (tc.category === "questionnaire") return tc.status === "running";
  return false;
}

const RENDERERS: Record<string, (tc: ToolCallState) => React.ReactNode> = {
  bash: (tc) => <BashRenderer tc={tc} />,
  edit_file: (tc) => <EditRenderer tc={tc} />,
  write_file: (tc) => <WriteRenderer tc={tc} />,
  read_file: (tc) => <ReadRenderer tc={tc} />,
  ask_user: (tc) => <AskUserRenderer tc={tc} />,
  // create_agent_task 不走此处：Timeline 将其折叠成单张实时任务面板（InlineTaskPanel），
  // 状态由 agentTaskStore（LLM 维护）驱动，不再受工具调用完成态影响。
  web_search: (tc) => (
    <SearchResultBlock
      resultSummary={tc.result?.full || tc.result?.preview || ""}
      collapsed={false}
    />
  ),
  web_fetch: (tc) => <WebFetchRenderer tc={tc} />,
  search_skills: (tc) => <SearchSkillsRenderer tc={tc} />,
};

/** 渲染单个工具调用卡片（Timeline 的 tool 段调用）。 */
export function renderTool(tc: ToolCallState): React.ReactNode {
  const fn = RENDERERS[tc.tool_name];
  const card = fn ? fn(tc) : <DefaultRenderer tc={tc} />;
  // 反馈横幅挂在**分发点之外**而非各渲染器内部：反馈与"是哪个工具"正交，
  // 加一个新 provider 不该需要巡一遍所有渲染器。无 feedback 时 Banner 返回 null，
  // 对既有工具零视觉变化。
  if (!tc.feedback) return card;
  return (
    <>
      {card}
      <ToolFeedbackBanner feedback={tc.feedback} />
    </>
  );
}
