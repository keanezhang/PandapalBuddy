/**
 * src/components/ChatArea/Timeline.tsx
 *
 * 步骤级时间线：按 message.timeline 的真实发生顺序渲染
 * 思考(ReasoningBlock) / 文本(MessageContent) / 工具(renderTool) 交错。
 * StreamingBubble 与 MessageBubble 共用；isStreaming 控制末段的流式光标/转圈。
 */
import React from "react";
import type { TimelineItem, ToolCallState } from "../../store/chatStore";
import { ReasoningBlock } from "./ReasoningBlock";
import { MessageContent } from "./MessageContent";
import { renderTool, shouldHideTool } from "./toolRenderers/registry";
import { SkillProgressBlock } from "./SkillProgressBlock";
import { InlineTaskPanel } from "../TaskPanel/TaskPanel";

interface Props {
  items: TimelineItem[];
  toolCalls: ToolCallState[];
  isStreaming: boolean;
}

export function Timeline({ items, toolCalls, isStreaming }: Props) {
  const lastIdx = items.length - 1;
  // create_agent_task 通常在一轮里被批量调用多次；任务卡是整个会话的实时聚合视图，
  // 每次调用都出一张会重复。只在本条消息里第一次出现的 task_create 处渲染一张实时面板。
  const firstTaskCreateIdx = items.findIndex((it) => {
    if (it.kind !== "tool") return false;
    const tc = toolCalls.find((t) => t.tool_call_id === it.toolCallId);
    return tc?.category === "task_create";
  });
  return (
    <>
      {items.map((item, i) => {
        const isLast = i === lastIdx;

        if (item.kind === "reasoning") {
          const stillOpen = isStreaming && isLast && item.durationMs == null;
          return (
            <ReasoningBlock
              key={i}
              tokens={item.tokens}
              isStreaming={stillOpen}
              durationMs={item.durationMs}
            />
          );
        }

        if (item.kind === "text") {
          return (
            <div key={i} style={{ position: "relative" }}>
              <MessageContent content={item.content} />
              {isStreaming && isLast && (
                <span style={{
                  animation: "cursor-blink 1s step-end infinite",
                  color: "var(--accent-soft)", fontWeight: "bold", marginLeft: 1,
                }}>▌</span>
              )}
            </div>
          );
        }

        if (item.kind === "skill_progress") {
          return <SkillProgressBlock key={i} group={item.group} />;
        }

        // kind === "tool"
        const tc = toolCalls.find((t) => t.tool_call_id === item.toolCallId);
        if (!tc || shouldHideTool(tc)) return null;
        // task_create：折叠成单张实时任务面板（数据源 agentTaskStore，由 LLM 维护状态）。
        if (tc.category === "task_create") {
          return i === firstTaskCreateIdx ? <div key={i}><InlineTaskPanel /></div> : null;
        }
        return <div key={i}>{renderTool(tc)}</div>;
      })}
    </>
  );
}
