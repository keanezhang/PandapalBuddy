/**
 * src/components/TaskPanel/TaskPanel.tsx
 *
 * 任务列表面板：坐落在 InputBar 上方，展示当前会话的 AgentTask 实时进度。
 *
 * - 无任务 → 不渲染。
 * - 默认收起：单行摘要（进度 N/M + 当前进行中步骤 active_form）。
 * - 点击展开：完整清单，每项带状态图标。
 *
 * 数据源：useAgentTaskStore（由 AGENT_TASK_EVENT 透传的完整 task 填充）。
 */
import React, { useMemo, useState } from "react";
import type { AgentTaskData, AgentTaskStatus } from "../../types/api";
import { useSessionStore } from "../../store/sessionStore";
import {
  useAgentTaskStore,
  selectSessionTasks,
  computeProgress,
} from "../../store/agentTaskStore";

const STATUS_META: Record<AgentTaskStatus, { icon: string; color: string; label: string }> = {
  pending:     { icon: "○", color: "var(--text-muted)",   label: "待处理" },
  in_progress: { icon: "◔", color: "#60A5FA",             label: "进行中" },
  completed:   { icon: "●", color: "var(--success)",      label: "已完成" },
  failed:      { icon: "✕", color: "var(--danger)",       label: "失败" },
  cancelled:   { icon: "⊘", color: "var(--text-muted)",   label: "已取消" },
};

/**
 * InputBar 上方锚定版：读当前会话任务，包一层居中容器。
 */
export function TaskPanel() {
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const tasksMap = useAgentTaskStore((s) => s.tasks);

  const tasks = useMemo(
    () => selectSessionTasks(tasksMap, currentSessionId),
    [tasksMap, currentSessionId],
  );

  if (tasks.length === 0) return null;

  return (
    <div style={{ padding: "0 var(--space-6)" }}>
      <div style={{ width: "100%", maxWidth: 1200, margin: "0 auto var(--space-2)" }}>
        <TaskListCard tasks={tasks} />
      </div>
    </div>
  );
}

/**
 * 对话时间线内嵌版：与 TaskPanel 同一张卡、同一数据源（当前会话的实时任务），
 * 只是去掉了锚定容器、加一点上边距，好嵌进消息气泡。
 * 状态由 LLM 通过 AGENT_TASK_EVENT 维护，不再受工具调用完成态影响。
 */
export function InlineTaskPanel() {
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const tasksMap = useAgentTaskStore((s) => s.tasks);

  const tasks = useMemo(
    () => selectSessionTasks(tasksMap, currentSessionId),
    [tasksMap, currentSessionId],
  );

  if (tasks.length === 0) return null;

  return (
    <div style={{ marginTop: "var(--space-2)" }}>
      <TaskListCard tasks={tasks} />
    </div>
  );
}

/**
 * 纯展示卡片：可折叠的任务清单（摘要行 + 展开明细）。
 * 由 TaskPanel（锚定）与 InlineTaskPanel（时间线内嵌）共用。
 */
function TaskListCard({ tasks }: { tasks: AgentTaskData[] }) {
  const [expanded, setExpanded] = useState(false);
  const progress = useMemo(() => computeProgress(tasks), [tasks]);

  if (tasks.length === 0) return null;

  // 摘要行文案：全部结束 → 汇总；否则显示当前进行中步骤。
  const summary = progress.allDone
    ? progress.failed > 0
      ? `完成 ${progress.completed}/${progress.total}，${progress.failed} 失败`
      : `全部完成 ${progress.completed}/${progress.total}`
    : progress.inProgress
      ? (progress.inProgress.active_form || progress.inProgress.subject)
      : "等待开始…";

  return (
    <div
      style={{
        border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-md)",
        background: "var(--bg-elevated)",
        overflow: "hidden",
      }}
    >
      {/* 摘要行（点击展开/收起） */}
      <div
        onClick={() => setExpanded((v) => !v)}
        style={{
          display: "flex", alignItems: "center", gap: "var(--space-2)",
          padding: "8px var(--space-3)", cursor: "pointer",
          fontSize: 12, lineHeight: 1.4, userSelect: "none",
        }}
      >
        <span style={{ flexShrink: 0 }}>📋</span>
        <span style={{ fontWeight: 600, color: "var(--text-primary)", flexShrink: 0 }}>
          任务 {progress.done}/{progress.total}
        </span>
        <ProgressBar progress={progress} />
        <span style={{
          color: "var(--text-tertiary)", overflow: "hidden",
          textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0, flex: 1,
        }}>
          {summary}
        </span>
        <span style={{ color: "var(--text-muted)", fontSize: 10, flexShrink: 0 }}>
          {expanded ? "▾" : "▸"}
        </span>
      </div>

      {/* 展开：完整清单 */}
      {expanded && (
        <div style={{
          borderTop: "1px solid var(--border-subtle)",
          maxHeight: 260, overflowY: "auto",
          padding: "var(--space-1) 0",
        }}>
          {tasks.map((t) => <TaskRow key={t.task_id} task={t} />)}
        </div>
      )}
    </div>
  );
}

function ProgressBar({ progress }: { progress: ReturnType<typeof computeProgress> }) {
  const pct = progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0;
  const barColor = progress.failed > 0 ? "var(--danger)" : "var(--success)";
  return (
    <div style={{
      width: 56, height: 4, borderRadius: 2, flexShrink: 0,
      background: "var(--border-subtle)", overflow: "hidden",
    }}>
      <div style={{ width: `${pct}%`, height: "100%", background: barColor, transition: "width .3s" }} />
    </div>
  );
}

function TaskRow({ task }: { task: AgentTaskData }) {
  // 后端下发表外 status 时兜底展示原始值，避免 meta 为 undefined 导致整树渲染崩溃
  const meta = STATUS_META[task.status] ?? { icon: "?", color: "var(--text-muted)", label: String(task.status) };
  const running = task.status === "in_progress";
  const text = running && task.active_form ? task.active_form : task.subject;
  const dim = task.status === "cancelled";
  const strike = task.status === "completed";

  return (
    <div style={{
      display: "flex", alignItems: "baseline", gap: "var(--space-2)",
      padding: "5px var(--space-3)", fontSize: 12, lineHeight: 1.4,
    }}>
      <span style={{ color: meta.color, flexShrink: 0, width: 14, textAlign: "center" }}>
        {meta.icon}
      </span>
      <span style={{
        color: dim ? "var(--text-muted)" : "var(--text-secondary)",
        textDecoration: strike ? "line-through" : "none",
        opacity: dim ? 0.7 : 1,
        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, minWidth: 0,
      }}>
        {text}
      </span>
      <span style={{ color: meta.color, fontSize: 10, flexShrink: 0 }}>
        {meta.label}
      </span>
    </div>
  );
}
