/**
 * src/components/ChatArea/SkillProgressBlock.tsx
 *
 * 对话内技能/长任务进度块（由 SKILL_PROGRESS 事件驱动，作为 timeline 的一段）。
 *
 * - 运行中（group.status === "running"）：展开的活动卡，标题带转圈，
 *   已完成阶段打 ✓，当前阶段带跳动点 + 高亮。
 * - 终态（completed/failed）：收起成一行 chip（✓/✕ 活动名 · N步 · 用时），
 *   点击可展开看每一步。
 */
import React, { useState } from "react";
import type { SkillProgressGroup, SkillActivityStatus } from "../../store/chatStore";
import { formatDuration } from "./toolRenderers/_primitives";

const STEP_META: Record<SkillActivityStatus, { icon: string; color: string }> = {
  running:   { icon: "◔", color: "#60A5FA" },
  completed: { icon: "✓", color: "var(--success)" },
  failed:    { icon: "✕", color: "var(--danger)" },
};

export function SkillProgressBlock({ group }: { group: SkillProgressGroup }) {
  const running = group.status === "running";
  // 运行中默认展开（活的日志）；终态默认收起成 chip。
  const [expanded, setExpanded] = useState(running);

  const headColor =
    group.status === "failed" ? "var(--danger)"
    : group.status === "completed" ? "var(--success)"
    : "#60A5FA";

  const dur = formatDuration(group.durationMs);

  return (
    <div style={{
      marginTop: "var(--space-2)",
      border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-md)",
      background: "var(--bg-elevated)",
      borderLeft: `2px solid ${headColor}`,
      overflow: "hidden",
    }}>
      {/* 表头 */}
      <div
        onClick={() => !running && setExpanded((v) => !v)}
        style={{
          display: "flex", alignItems: "center", gap: "var(--space-2)",
          padding: "6px var(--space-3)", fontSize: 12, lineHeight: 1.4,
          cursor: running ? "default" : "pointer", userSelect: "none",
        }}
      >
        {running ? (
          <span style={{
            width: 10, height: 10, borderRadius: "50%",
            border: "2px solid rgba(96,165,250,0.25)", borderTopColor: "#60A5FA",
            animation: "spin 0.8s linear infinite", display: "inline-block", flexShrink: 0,
          }} />
        ) : (
          <span style={{ color: headColor, flexShrink: 0, width: 12, textAlign: "center" }}>
            {group.status === "failed" ? "✕" : "✓"}
          </span>
        )}
        <span style={{ flexShrink: 0 }}>⚡</span>
        <span style={{ fontWeight: 600, color: "var(--text-primary)", flexShrink: 0 }}>
          {group.activity}
        </span>
        {!running && (
          <span style={{ color: "var(--text-tertiary)", fontSize: 11, flexShrink: 0 }}>
            {group.steps.length} 步
          </span>
        )}
        <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: "var(--space-2)", flexShrink: 0 }}>
          {dur && <span style={{ color: "var(--text-muted)", fontSize: 10 }}>{dur}</span>}
          {!running && group.steps.length > 0 && (
            <span style={{ color: "var(--text-muted)", fontSize: 10 }}>{expanded ? "▾" : "▸"}</span>
          )}
        </span>
      </div>

      {/* 阶段清单 */}
      {expanded && group.steps.length > 0 && (
        <div style={{ borderTop: "1px solid var(--border-subtle)", padding: "var(--space-1) 0" }}>
          {group.steps.map((s, i) => {
            const meta = STEP_META[s.status];
            const isRunning = s.status === "running";
            return (
              <div key={i} style={{
                display: "flex", alignItems: "baseline", gap: "var(--space-2)",
                padding: "4px var(--space-3)", fontSize: 12, lineHeight: 1.4,
              }}>
                {isRunning ? (
                  <span style={{
                    width: 6, height: 6, borderRadius: "50%", background: "#60A5FA",
                    animation: "pulse 1.2s ease-in-out infinite",
                    display: "inline-block", flexShrink: 0, alignSelf: "center",
                    margin: "0 4px",
                  }} />
                ) : (
                  <span style={{ color: meta.color, flexShrink: 0, width: 14, textAlign: "center" }}>
                    {meta.icon}
                  </span>
                )}
                <span style={{
                  color: s.status === "completed" ? "var(--text-tertiary)" : "var(--text-secondary)",
                  fontWeight: isRunning ? 500 : 400,
                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0,
                }}>
                  {s.phase}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
