/**
 * src/store/agentTaskStore.ts
 *
 * AgentTask 任务面板状态。
 *
 * 数据来源：AGENT_TASK_EVENT（Python 透传完整 task 对象）→ BackendProvider 分发到此。
 *   created / updated → upsertTask（写入/覆盖完整数据）
 *   deleted           → removeTask
 *
 * 面板（TaskPanel）按「当前会话」过滤展示，因此切会话无需显式清空 ——
 * 选择器只取 currentSessionId 对应的任务。跨会话数据仍保留在 store 里，
 * 切回时立即可见。
 */
import { create } from "zustand";
import type { AgentTaskData } from "../types/api";

interface AgentTaskState {
  /** 全部任务，按 task_id 索引（跨会话）。 */
  tasks: Record<string, AgentTaskData>;

  upsertTask: (task: AgentTaskData) => void;
  removeTask: (taskId: string) => void;
  /** 清空某会话的任务（可选，用于显式重置）。 */
  clearSession: (sessionId: string) => void;
  clearAll: () => void;
}

export const useAgentTaskStore = create<AgentTaskState>((set) => ({
  tasks: {},

  upsertTask: (task) =>
    set((state) => ({ tasks: { ...state.tasks, [task.task_id]: task } })),

  removeTask: (taskId) =>
    set((state) => {
      if (!(taskId in state.tasks)) return state;
      const next = { ...state.tasks };
      delete next[taskId];
      return { tasks: next };
    }),

  clearSession: (sessionId) =>
    set((state) => {
      const next: Record<string, AgentTaskData> = {};
      for (const [id, t] of Object.entries(state.tasks)) {
        if (t.session_id !== sessionId) next[id] = t;
      }
      return { tasks: next };
    }),

  clearAll: () => set({ tasks: {} }),
}));

// ── 派生选择器（组件内用；均按当前会话过滤） ──────────────────────────────

/** 当前会话的任务，按 order → created_at 升序。 */
export function selectSessionTasks(
  tasks: Record<string, AgentTaskData>,
  sessionId: string | null,
): AgentTaskData[] {
  if (!sessionId) return [];
  return Object.values(tasks)
    .filter((t) => t.session_id === sessionId)
    .sort((a, b) => a.order - b.order || (a.created_at ?? "").localeCompare(b.created_at ?? ""));
}

const TERMINAL: ReadonlySet<AgentTaskData["status"]> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

export interface TaskProgress {
  total: number;
  done: number;                 // 所有终态（completed/failed/cancelled）
  completed: number;            // 仅成功
  failed: number;
  inProgress: AgentTaskData | null;   // 第一个进行中的任务
  allDone: boolean;
}

export function computeProgress(tasks: AgentTaskData[]): TaskProgress {
  let done = 0, completed = 0, failed = 0;
  let inProgress: AgentTaskData | null = null;
  for (const t of tasks) {
    if (TERMINAL.has(t.status)) done++;
    if (t.status === "completed") completed++;
    if (t.status === "failed") failed++;
    if (t.status === "in_progress" && !inProgress) inProgress = t;
  }
  return {
    total: tasks.length,
    done,
    completed,
    failed,
    inProgress,
    allDone: tasks.length > 0 && done === tasks.length,
  };
}
